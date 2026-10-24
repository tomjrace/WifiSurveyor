"""RTL-SDR dongles: cheap, wide sweeps below 1.8 GHz, through the rtl-sdr tools.

An RTL-SDR (RTL2832U with an R820T/R828D tuner, 0bda:2838) is a USB device driven by the
programs in the rtl-sdr package:
  rtl_power   -f LOW:HIGH:BIN -i SECONDS -g GAIN -p PPM -c CROP -d DEVICE -  prints CSV lines
              "date, time, hz_low, hz_high, hz_step, samples, dB, …", one per hop of about
              2.8 MHz, the same layout as hackrf_sweep, so the HackRF's reader assembles them:
              a sweep is over when a hop comes round again. It rounds the bin up to what its
              FFT gives (100 kHz asked, 86.8 kHz given).
  rtl_eeprom  -d DEVICE  names the tuner ("Found Rafael Micro R820T tuner") and the dongle.
The tools detach the kernel's DVB-T driver (dvb_usb_rtl28xxu) by themselves while they run,
so nothing needs blacklisting. Only one program can have the dongle open at once.

It covers 24-1766 MHz with an R820T or R828D; the RTL-SDR Blog V4's upconverter takes it down
to 0.5 MHz. That's every wireless mic band below 1.8 GHz, the sub-GHz industrial bands and
business radio, which is what it's for here: Wi-Fi is out of its reach.

Like the HackRF, its levels are relative (dBFS), not dBm: an offset turns them into dBm, set
by hand or by matching an analyzer that reads true dBm, and kept per dongle by serial number
with the gain it was set at, so changing the gain moves it to match.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core import settings
from . import spectrum as spec
from .analyzer import Analyzer, Config, Trace
from .hackrf import Assembler, Tail, _end, bins, parse_line

USB_IDS = {("0bda", "2838"): "RTL-SDR", ("0bda", "2832"): "RTL-SDR"}
TARGET_POINTS = 900
MIN_BIN_HZ, MAX_BIN_HZ = 1_000, 1_000_000
CROP = "25%"                      # each hop's edges roll off in the dongle's filter
DEFAULT_GAIN = 29.7               # dB: fixed, so calibration holds (no AGC)
GAINS = (0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4,
         28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6)
# Until it's calibrated: about where an R820T at 29.7 dB gain reads, so levels land in the
# right region (its floor near -120 dBm in 25 kHz). Calibrating replaces it.
DEFAULT_OFFSET_DB = -82.0
# (low, high) MHz the tuner covers, by the name rtl_eeprom/rtl_test print
TUNERS = {"R820T": (24.0, 1766.0), "R828D": (24.0, 1766.0), "E4000": (52.0, 2200.0),
          "FC0013": (22.0, 1100.0), "FC0012": (22.0, 948.0), "FC2580": (146.0, 924.0)}
BLOG_V4_LOW_MHZ = 0.5
# What the dongle is, when its USB strings don't say: many RTL-SDR Blog V3s report the generic
# "Realtek RTL2838UHIDIR" like any other dongle. Setup → RTL-SDR → Model sets it.
MODELS = {"auto": "Detect from USB", "generic": "Generic RTL-SDR", "v3": "RTL-SDR Blog V3",
          "v4": "RTL-SDR Blog V4"}
# The reception check: a short capture at 100 MHz. A tuner whose PLL never locks has no local
# oscillator, so the ADC sees almost nothing (well under 2 levels rms), antenna or not.
HEALTH_HZ = 100_000_000
DEAD_RMS = 2.0


def available() -> bool:
    return shutil.which("rtl_power") is not None


def why_unavailable() -> str:
    return "" if available() else "rtl_power isn't installed (sudo apt install rtl-sdr)."


@dataclass(slots=True)
class UsbRtl:
    node: str                     # /dev/bus/usb/BBB/DDD
    manufacturer: str
    product: str
    serial: str

    @property
    def model(self) -> str:
        if "blog" in f"{self.manufacturer} {self.product}".lower():
            return "RTL-SDR Blog " + ("V4" if "v4" in self.product.lower() else "V3")
        return "RTL-SDR"


def plugged_in(root: Path = Path("/sys/bus/usb/devices")) -> list[UsbRtl]:
    """RTL-SDR dongles on USB, from sysfs, without opening anything."""
    found = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            ids = ((entry / "idVendor").read_text().strip().lower(),
                   (entry / "idProduct").read_text().strip().lower())
            if ids not in USB_IDS:
                continue
            bus, dev = int((entry / "busnum").read_text()), int((entry / "devnum").read_text())
        except (OSError, ValueError):
            continue

        def read(name: str) -> str:
            try:
                return (entry / name).read_text().strip()
            except OSError:
                return ""

        found.append(UsbRtl(f"/dev/bus/usb/{bus:03d}/{dev:03d}", read("manufacturer"),
                            read("product"), read("serial")))
    return found


def chosen_model(usb: UsbRtl, saved: dict | None = None) -> str:
    """What the dongle is called: Setup's choice, else what its USB strings say."""
    saved = settings.load() if saved is None else saved
    choice = saved.get("rtlsdr_model", "auto")
    if choice in ("generic", "v3", "v4"):
        return "RTL-SDR" if choice == "generic" else MODELS[choice]
    return usb.model


def bias_tee() -> bool:
    """5 V on the antenna port (Blog V3/V4) for an active antenna or LNA. Needs rtl_biast."""
    return bool(settings.get("rtlsdr_bias_tee", False))


def tuner_from(text: str) -> str:
    match = re.search(r"Found (?:Rafael Micro |Elonics |Fitipower |FCI )?(\w+) tuner", text)
    return match.group(1) if match else ""


# ---------- gain and calibration, per dongle ----------

def gain(saved: dict | None = None) -> float:
    saved = settings.load() if saved is None else saved
    wanted = float(saved.get("rtlsdr_gain", DEFAULT_GAIN))
    return min(GAINS, key=lambda g: abs(g - wanted))


def ppm(saved: dict | None = None) -> int:
    saved = settings.load() if saved is None else saved
    return int(saved.get("rtlsdr_ppm", 0))


def _unit(saved: dict, unit: str | None) -> dict:
    return (saved.get("rtlsdr_units") or {}).get(unit or "") or {}


def calibrated(unit: str | None = None) -> bool:
    return _unit(settings.load(), unit).get("offset_db") is not None


def calibrated_with(unit: str | None = None) -> str | None:
    return _unit(settings.load(), unit).get("calibrated_with")


def offset_db(unit: str | None = None, saved: dict | None = None) -> float:
    """dB to add to a reading from this dongle to make it dBm, at today's gain."""
    saved = settings.load() if saved is None else saved
    cal = _unit(saved, unit)
    base = cal.get("offset_db")
    at = cal.get("cal_gain", DEFAULT_GAIN)
    if base is None:
        base, at = DEFAULT_OFFSET_DB, DEFAULT_GAIN
    return float(base) - (gain(saved) - float(at))


def save_offset(value: float, with_what: str | None, unit: str | None = None) -> None:
    units = dict(settings.get("rtlsdr_units") or {})
    units[unit or ""] = {"offset_db": round(float(value), 1), "cal_gain": gain(),
                         "calibrated_with": with_what}
    settings.put("rtlsdr_units", units)


def snapshot(unit: str | None = None) -> dict:
    saved = settings.load()
    return {"calibrated": _unit(saved, unit).get("offset_db") is not None,
            "offset_db": round(offset_db(unit, saved), 1), "gain": gain(saved), "ppm": ppm(saved),
            "calibrated_with": _unit(saved, unit).get("calibrated_with")}


def bin_width_hz(start_mhz: float, end_mhz: float, points: int = TARGET_POINTS) -> int:
    width = (end_mhz - start_mhz) * 1e6 / max(1, points)
    return int(min(MAX_BIN_HZ, max(MIN_BIN_HZ, width)))


class RtlSdr(Analyzer):
    name = "RTL-SDR"
    # rtl_power reports once a second, and the first sweep after tuning is thrown away
    dwell_seconds = 3.2

    def __init__(self, usb: UsbRtl, index: int = 0, unique_serial: bool = True) -> None:
        super().__init__(usb.node)
        self.usb = usb
        self.name = chosen_model(usb)
        self.model = self.name + (f" {usb.serial}" if usb.serial and usb.serial != "00000001" else "")
        self.unit = usb.serial or usb.node
        # rtl_power takes a serial or an index, rtl_eeprom only an index. A serial of digits
        # ("00000001", which most dongles share) reads as an index, so then it's the index too
        self._index = str(index)
        self._select = (usb.serial if usb.serial and unique_serial and not usb.serial.isdigit()
                        else self._index)
        self._proc: subprocess.Popen | None = None
        self._tail: Tail | None = None
        self._target: tuple[float, float, int] | None = None
        self._failed = ""
        self._running = True
        self.tuner = ""
        self._low, self._high = 24.0, 1766.0
        # Why it can't receive, when the reception check found it deaf: "" when it's fine
        self.fault = ""
        self.bias_note = ""
        self._probe()
        self.fault = self.check_reception()
        self.apply_bias_tee()

    def _probe(self) -> None:
        """Opens it once to name the tuner, which says how far it reaches. Proves access too."""
        try:
            done = subprocess.run(["rtl_eeprom", "-d", self._index], capture_output=True,
                                  text=True, timeout=10)
        except FileNotFoundError:
            done = None
        except subprocess.TimeoutExpired as error:
            raise ConnectionError("The RTL-SDR didn't answer rtl_eeprom.") from error
        text = (done.stdout + done.stderr) if done else ""
        trouble = self._trouble(text)
        if done is not None and ("usb_open error" in text or "Failed to open" in text
                                 or "No supported devices" in text or "No matching" in text):
            raise ConnectionError(trouble)
        self.tuner = tuner_from(text)
        self._low, self._high = TUNERS.get(self.tuner, (24.0, 1766.0))
        if self.name.endswith("V4"):
            self._low = BLOG_V4_LOW_MHZ

    def check_reception(self) -> str:
        """
        A quarter-second capture at 100 MHz: "" when the tuner locks and signal reaches the
        ADC, otherwise what's wrong, in words. Can't-tell (no rtl_sdr, a timeout) counts as fine.
        """
        # rtl_sdr doesn't always stop by itself after -n samples, so it's read and then ended
        command = ["rtl_sdr", "-d", self._select, "-f", str(HEALTH_HZ), "-s", "2048000",
                   "-g", f"{gain():g}", "-"]
        wanted = 1 << 20
        with self.lock:
            target, self._target = self._target, None
            self._stop_process()
            try:
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError:
                proc = None
            data = b""
            tail = Tail(proc.stderr) if proc is not None else None
            try:
                if proc is not None:
                    deadline = time.monotonic() + 8
                    while len(data) < wanted and time.monotonic() < deadline:
                        chunk = proc.stdout.read1(wanted - len(data))
                        if not chunk:
                            break
                        data += chunk
            finally:
                if proc is not None:
                    _end(proc, tail)
                if target is not None and self._running:
                    self._target = target
                    self._start(*target)
        said = tail.text(0.5) if tail is not None else ""
        samples = np.frombuffer(data, dtype=np.uint8)[262144:]          # past the settling
        if samples.size < 10_000:
            return ""
        rms = float((samples.astype(float) - 127.5).std())
        if "PLL not locked" in said and rms < DEAD_RMS:
            return (f"Its tuner isn't locking (\"PLL not locked\" at every frequency) and almost "
                    f"nothing reaches its ADC ({rms:.1f} levels rms), so it can't receive, antenna "
                    "or not. Unplug it and plug it into another port; if that doesn't fix it, "
                    "the dongle's tuner is faulty. It's left out of band scans and surveys meanwhile.")
        return ""

    def apply_bias_tee(self) -> None:
        """Sets the antenna port's 5 V to Setup's choice, with rtl_biast (RTL-SDR Blog tools)."""
        if not shutil.which("rtl_biast"):
            self.bias_note = ("Antenna power needs the RTL-SDR Blog tools (Setup → Device software "
                              "→ RTL-SDR)." if bias_tee() else "")
            return
        try:
            done = subprocess.run(["rtl_biast", "-d", self._index, "-b", "1" if bias_tee() else "0"],
                                  capture_output=True, text=True, timeout=10)
            self.bias_note = "" if done.returncode == 0 else f"rtl_biast: {self._trouble(done.stderr)}"
        except (OSError, subprocess.TimeoutExpired) as error:
            self.bias_note = f"rtl_biast: {error}"

    @staticmethod
    def _trouble(text: str) -> str:
        if "usb_claim_interface error" in text:
            return ("The RTL-SDR is held by another program or by the DVB-T driver. Close other "
                    "SDR programs, or run: sudo modprobe -r dvb_usb_rtl28xxu")
        if "Failed to open" in text or "usb_open error" in text or "LIBUSB_ERROR_ACCESS" in text:
            return ("No permission to open the RTL-SDR. Install the rtl-sdr package (its udev "
                    "rule gives you access) and replug it, or run as root.")
        if "No supported devices" in text or "No matching" in text:
            return "The RTL-SDR isn't answering. Is it still plugged in?"
        lines = [line for line in text.splitlines() if line.strip() and "PLL not locked" not in line]
        return lines[-1] if lines else "the rtl-sdr tool failed"

    # ---------- what the app asks of an analyzer ----------

    @property
    def min_mhz(self) -> float:
        return self._low

    @property
    def max_mhz(self) -> float:
        return self._high

    @property
    def wifi_ranges(self):
        return ()                                  # it can't reach Wi-Fi

    @property
    def summary(self) -> str:
        state = "calibrated" if calibrated(self.unit) else "not calibrated yet, so levels are estimates"
        tuner = f"{self.tuner} tuner, " if self.tuner else ""
        text = f"{self.model}, {tuner}{self._low:g}-{self._high:.0f} MHz, {state}"
        if self.fault:
            text += f". NOT RECEIVING: {self.fault}"
        if self.bias_note:
            text += f". {self.bias_note}"
        return text

    @property
    def healthy(self) -> bool:
        return self.alive and not self.fault

    @property
    def alive(self) -> bool:
        return self._running and not self._failed and os.path.exists(self.usb.node)

    @property
    def failure(self) -> str:
        return self._failed

    def close(self) -> None:
        self._running = False
        self._stop_process()

    def _gone(self) -> ConnectionError:
        if self._failed:
            return ConnectionError(f"The {self.name}: {self._failed}")
        return super()._gone()

    def _command(self, start_mhz: float, end_mhz: float, width_hz: int, once: bool = False) -> list[str]:
        saved = settings.load()
        command = ["rtl_power", "-d", self._select,
                   "-f", f"{int(start_mhz * 1e6)}:{int(math.ceil(end_mhz * 1e6))}:{width_hz}",
                   "-i", "1", "-g", f"{gain(saved):g}", "-c", CROP]
        if ppm(saved):
            command += ["-p", str(ppm(saved))]
        if once:
            command.append("-1")
        return command + ["-"]

    def _stop_process(self) -> None:
        proc, self._proc, tail = self._proc, None, self._tail
        if proc is not None:
            _end(proc, tail)

    def _start(self, start_mhz: float, end_mhz: float, width_hz: int) -> None:
        self._stop_process()
        proc = subprocess.Popen(self._command(start_mhz, end_mhz, width_hz), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        tail = Tail(proc.stderr)
        self._proc, self._tail = proc, tail
        threading.Thread(target=self._read, args=(proc, tail, (start_mhz, end_mhz, width_hz)),
                         daemon=True).start()

    def _read(self, proc: subprocess.Popen, tail: Tail, target: tuple[float, float, int]) -> None:
        assembler = Assembler()
        start, end, width = target
        step = width / 1e6
        grid = start + np.arange(int(round((end - start) / step)) + 1) * step
        offset, offset_at = offset_db(self.unit), time.monotonic()
        try:
            for line in proc.stdout:
                pieces = assembler.add(line)
                if pieces is None:
                    continue
                freqs, levels = bins(pieces)
                if freqs.size == 0 or freqs[0] > start + 3 or freqs[-1] < end - 3:
                    continue
                if self._target != target or self._proc is not proc:
                    break
                if time.monotonic() - offset_at >= 1.0:
                    offset, offset_at = offset_db(self.unit), time.monotonic()
                self._add_sweep((np.interp(grid, freqs, levels) + offset).tolist())
        except (ValueError, OSError):
            return
        if proc.wait() != 0 and self._proc is proc and self._running:
            self._failed = self._trouble(tail.text()) if tail.lines else "rtl_power stopped"

    def _configure(self, start_mhz: float, end_mhz: float, timeout: float) -> Config:
        if not self.alive:
            raise self._gone()
        if start_mhz < self._low - 0.5 or end_mhz > self._high + 0.5:
            raise ValueError(f"The {self.name} reaches {self._low:g}-{self._high:.0f} MHz, so it "
                             f"can't sweep {start_mhz:.0f}-{end_mhz:.0f} MHz.")
        width = bin_width_hz(start_mhz, end_mhz)
        self._target = (start_mhz, end_mhz, width)
        self.clear_sweeps()
        self._start(start_mhz, end_mhz, width)
        step = width / 1e6
        self.config = Config(start_mhz=start_mhz, step_mhz=step,
                             points=int(round((end_mhz - start_mhz) / step)) + 1, expansion=False,
                             min_mhz=self._low, max_mhz=self._high, max_span_mhz=self._high,
                             rbw_khz=width / 1e3)
        return self.config

    def apply_settings(self) -> None:
        """Picks up a new gain or ppm: restarts sweeping with it, in the background."""
        def restart() -> None:
            with self.lock:
                self.apply_bias_tee()
                if self._target is not None and self._running:
                    self.clear_sweeps()
                    self._start(*self._target)

        threading.Thread(target=restart, daemon=True).start()

    def scan(self, start_mhz: float, end_mhz: float, step_khz: float, sweeps: int = 3,
             timeout: float = 60) -> Trace:
        """
        One fine pass of rtl_power at the bin width asked for, repeated ``sweeps`` times
        (each a second or so), averaged and peak-held. The live sweep pauses meanwhile.
        """
        if start_mhz < self._low - 0.5 or end_mhz > self._high + 0.5:
            raise ValueError(f"The {self.name} reaches {self._low:g}-{self._high:.0f} MHz.")
        width = int(min(MAX_BIN_HZ, max(MIN_BIN_HZ, step_khz * 1000)))
        totals: dict[int, list] = {}
        with self.lock:
            target, self._target = self._target, None
            self._stop_process()
            try:
                for _ in range(max(1, sweeps)):
                    try:
                        done = subprocess.run(self._command(start_mhz, end_mhz, width, once=True),
                                              capture_output=True, text=True, timeout=timeout)
                    except subprocess.TimeoutExpired as error:
                        raise TimeoutError("rtl_power didn't finish the scan.") from error
                    if done.returncode != 0 and "," not in done.stdout:
                        raise RuntimeError(self._trouble(done.stderr))
                    for line in done.stdout.splitlines():
                        parsed = parse_line(line)
                        if parsed is None:
                            continue
                        low, _high, step, levels = parsed
                        power = spec.to_mw(levels)
                        entry = totals.setdefault(low, [step, np.zeros_like(power), np.full_like(power, -1e9), 0])
                        if entry[1].size == power.size:
                            entry[1] += power
                            entry[2] = np.maximum(entry[2], np.asarray(levels, dtype=float))
                            entry[3] += 1
            finally:
                if target is not None and self._running:
                    self._target = target
                    self.clear_sweeps()
                    self._start(*target)
        if not totals:
            raise RuntimeError("rtl_power returned no bins.")
        offset = offset_db(self.unit)
        average = {low: (step, spec.to_dbm(total / count).tolist())
                   for low, (step, total, _peak, count) in totals.items() if count}
        peaks = {low: (step, peak.tolist()) for low, (step, _t, peak, count) in totals.items() if count}
        freqs, avg = bins(average)
        _f, peak = bins(peaks)
        keep = (freqs >= start_mhz) & (freqs <= end_mhz)
        step = next(iter(totals.values()))[0]
        return Trace(freqs[keep], avg[keep] + offset, peak[keep] + offset, step / 1e3)


def open_all(skip: set[str] | None = None) -> tuple[list[RtlSdr], list[str]]:
    """Opens every RTL-SDR plugged in that isn't open yet. Returns (opened, errors)."""
    found = plugged_in()
    serials = [usb.serial for usb in found]
    opened, errors = [], []
    for index, usb in enumerate(found):
        if skip and usb.node in skip:
            continue
        try:
            opened.append(RtlSdr(usb, index, unique_serial=serials.count(usb.serial) == 1))
        except Exception as error:
            errors.append(f"{usb.model}: {error}")
    return opened, errors


def match_reference(device: RtlSdr, reference, low_mhz: float, high_mhz: float,
                    step_khz: float = 50.0) -> tuple[float, int]:
    """
    Sets the dongle's offset so it reads like ``reference`` (an analyzer reading true dBm) on
    the strong signals both hear in low-high: the median difference of their peaks over the
    bins where the reference is 15 dB above its floor. Returns (offset, bins compared).
    """
    theirs = reference.scan(low_mhz, high_mhz, step_khz, sweeps=5)
    ours = device.scan(low_mhz, high_mhz, step_khz, sweeps=5)
    floor = float(np.percentile(theirs.peak, 10))
    strong = theirs.peak > floor + 15
    if strong.sum() < 3:
        raise RuntimeError(f"Too few strong signals between {low_mhz:.0f} and {high_mhz:.0f} MHz "
                           "to compare. FM broadcast (88-108 MHz) usually has plenty.")
    mine = np.interp(theirs.freqs[strong], ours.freqs, ours.peak)
    difference = float(np.median(theirs.peak[strong] - mine))
    value = offset_db(device.unit) + difference
    name = getattr(reference, "model", "") or reference.name
    save_offset(value, f"{name}, {time.strftime('%d %b %Y')}", device.unit)
    return value, int(strong.sum())
