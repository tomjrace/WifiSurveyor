"""HackRF One: fast sweeps, raw I/Q, and a test tone, through the hackrf tools.

The HackRF is a USB device (1d50:6089) driven by the programs in the hackrf package rather
than a serial shell:
  hackrf_sweep     -f MIN:MAX (MHz; up to ten of them) -w BIN_HZ -l LNA -g VGA -a AMP prints
                   CSV lines "date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, …",
                   each covering 5 MHz, in no fixed order within a sweep. A sweep is over when
                   a 5 MHz piece comes round again.
  hackrf_transfer  -r - -f HZ -s RATE -n SAMPLES writes signed 8-bit I/Q pairs to stdout;
                   -c AMPLITUDE -x TX_VGA transmits a plain carrier until stopped.
Both print progress to stderr every second, which is read as it comes so neither ever stalls
on a full pipe. Only one program can have the device open, so sweeping pauses while I/Q is
captured or the tone is on. It reaches 1-6000 MHz by specification; recent hackrf tools tune
to 7250 MHz, outside the specification, where it's much less sensitive.

Its levels are dB relative to the ADC's full scale, not dBm. An offset turns them into dBm
(Setup can set it by comparing with the Wi-Fi card or another analyzer), with its own value
for 2.4 and 5 GHz when calibrating found one, and follows the gains: turning them up by some
dB takes as much off the offset. Until it's calibrated every reading is marked uncalibrated.
"""

from __future__ import annotations

import collections
import json
import math
import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core import settings
from . import iq
from .analyzer import Analyzer, Config, Trace

MODELS = {("1d50", "6089"): "HackRF One", ("1d50", "604b"): "HackRF Jawbreaker",
          ("1d50", "cc15"): "rad1o"}
SPEC_TOP_MHZ = 6000.0
TUNE_TOP_MHZ = 7250.0
MIN_MHZ = 1.0
MIN_BIN_HZ, MAX_BIN_HZ = 2445, 5_000_000
TARGET_POINTS = 900               # bins across whatever range is asked for
LNA_STEPS = (0, 8, 16, 24, 32, 40)
DEFAULT_LNA, DEFAULT_VGA = 16, 20
AMP_DB = 11.0                     # what the front-end amplifier adds, about
# Until it's calibrated: roughly where a HackRF at the default gains reads, so levels land in
# the right region. Calibrating replaces it.
DEFAULT_OFFSET_DB = -40.0
# The bands that get an offset of their own when calibrating measured two channels in them;
# everything else (cellular, 6 GHz) uses the overall one
CAL_BANDS = {"2.4": (2300.0, 2600.0), "5": (5000.0, 5950.0)}
IQ_RATE = 20e6
IQ_TOP_MHZ = 6000.0               # hackrf_transfer tunes no higher, unlike hackrf_sweep
# The baseband filter: 15 MHz rolls off before the capture's edges, so less from beyond them
# folds back in. A 20 MHz Wi-Fi frame still measures 12-20 MHz wide through it.
IQ_FILTER_HZ = 15_000_000
# A channel the card or the analyzer hears strongly should stand this far above the HackRF's
# floor; if nothing in a band does, its antenna probably doesn't cover that band
HEARD_DB = 6.0
STRONG_DBM = -65.0

# The test tone stays in the unlicensed ISM bands, clear of US Wi-Fi channels, at a level
# far below what Part 15 allows there. The amplifier is never used.
TONES = {
    "2.4": (2477.0, "2477 MHz, above channel 11"),
    "5.8": (5732.0, "5732 MHz, between channels 144 and 149"),
}
TONE_RANGES = ((2400.0, 2483.5), (5725.0, 5875.0))
MAX_TX_VGA = 30
TONE_MINUTES = 30                 # it turns itself off after this

# The US DFS channels, 20 MHz at a time, in the order the radar watch listens: the terminal
# Doppler weather radar channels (5600-5650 MHz) first, since that's where radar is most often
# heard, then the rest of UNII-2C and UNII-2A.
RADAR_CHANNELS = (120, 124, 128, 116, 132, 100, 104, 108, 112, 136, 140, 144, 52, 56, 60, 64)

# Raw I/Q kept behind what couldn't be named, and behind radar, as SigMF recordings. The
# oldest go once they add up to more than this.
KEEP_KINDS = {"bursty", "video", "continuous", "radar"}
KEEP_IQ_MB = 1000
RADAR_SNIPPET_CHUNKS = 4          # about 100 ms around the most pulses, at 20 MS/s


def available() -> bool:
    return shutil.which("hackrf_sweep") is not None and shutil.which("hackrf_transfer") is not None


def why_unavailable() -> str:
    if not shutil.which("hackrf_sweep"):
        return "hackrf_sweep isn't installed (sudo apt install hackrf)."
    if not shutil.which("hackrf_transfer"):
        return "hackrf_transfer isn't installed (sudo apt install hackrf)."
    return ""


@dataclass(slots=True)
class UsbHackrf:
    node: str                     # /dev/bus/usb/BBB/DDD, gone when it's unplugged
    model: str
    serial: str


def plugged_in(root: Path = Path("/sys/bus/usb/devices")) -> list[UsbHackrf]:
    """HackRFs on USB, from sysfs, without opening anything."""
    found = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            ids = ((entry / "idVendor").read_text().strip().lower(),
                   (entry / "idProduct").read_text().strip().lower())
        except OSError:
            continue
        if ids not in MODELS:
            continue
        try:
            bus = int((entry / "busnum").read_text())
            dev = int((entry / "devnum").read_text())
        except (OSError, ValueError):
            continue
        try:
            serial = (entry / "serial").read_text().strip()
        except OSError:
            serial = ""
        found.append(UsbHackrf(f"/dev/bus/usb/{bus:03d}/{dev:03d}", MODELS[ids], serial))
    return found


# ---------- reading hackrf_sweep ----------

def parse_line(line: str) -> tuple[int, int, float, list[float]] | None:
    """(hz_low, hz_high, bin_hz, levels) from one CSV line, or None if it isn't one."""
    parts = line.split(",")
    if len(parts) < 7:
        return None
    try:
        low, high = int(parts[2]), int(parts[3])
        width = float(parts[4])
        levels = [float(p) for p in parts[6:]]
    except ValueError:
        return None
    if width <= 0 or high <= low or not levels:
        return None
    return low, high, width, levels


class Assembler:
    """Collects 5 MHz pieces until one comes round again, which means a sweep is complete."""

    def __init__(self) -> None:
        self.pieces: dict[int, tuple[float, list[float]]] = {}

    def add(self, line: str) -> dict[int, tuple[float, list[float]]] | None:
        parsed = parse_line(line)
        if parsed is None:
            return None
        low, _high, width, levels = parsed
        done = None
        if low in self.pieces:
            done, self.pieces = self.pieces, {}
        self.pieces[low] = (width, levels)
        return done


def bins(pieces: dict[int, tuple[float, list[float]]]) -> tuple[np.ndarray, np.ndarray]:
    """Bin centres in MHz and levels, in frequency order."""
    freqs, levels = [], []
    for low in sorted(pieces):
        width, values = pieces[low]
        freqs.append((low + (np.arange(len(values)) + 0.5) * width) / 1e6)
        levels.append(np.asarray(values, dtype=float))
    if not freqs:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(freqs), np.concatenate(levels)


def bin_width_hz(start_mhz: float, end_mhz: float) -> int:
    width = (end_mhz - start_mhz) * 1e6 / TARGET_POINTS
    return int(min(MAX_BIN_HZ, max(MIN_BIN_HZ, width)))


class Tail:
    """
    Reads a tool's stderr as it comes, keeping the last few lines: left unread, the progress
    the hackrf tools print every second fills the pipe in minutes and they stop dead.
    """

    def __init__(self, pipe, lines: int = 20) -> None:
        self.lines: collections.deque[str] = collections.deque(maxlen=lines)
        self._thread = threading.Thread(target=self._read, args=(pipe,), daemon=True)
        self._thread.start()

    def _read(self, pipe) -> None:
        try:
            for line in pipe:
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                self.lines.append(line.rstrip("\n"))
        except (ValueError, OSError):
            pass                                   # closed: the tool was stopped

    def text(self, wait: float = 1.0) -> str:
        """What it said, once it has finished saying it (or after ``wait`` seconds)."""
        self._thread.join(wait)
        return "\n".join(self.lines)


def _end(proc: subprocess.Popen, tail: Tail | None = None) -> None:
    """
    Stops a hackrf tool the way Ctrl-C would, so it releases the device, and closes its pipes
    once ``tail`` has read the last of what it said.
    """
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if tail is not None:
        tail.text(1.0)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _check_iq_reach(center_mhz: float) -> None:
    if not MIN_MHZ <= center_mhz <= IQ_TOP_MHZ:
        raise ValueError(f"The HackRF records raw I/Q only up to {IQ_TOP_MHZ:.0f} MHz, so it can't "
                         f"listen at {center_mhz:.0f} MHz.")


def tone_allowed(freq_mhz: float) -> bool:
    return any(low + 1 <= freq_mhz <= high - 1 for low, high in TONE_RANGES)


# ---------- the calibration and gains this machine uses ----------

def gains(saved: dict | None = None) -> tuple[int, int, bool]:
    saved = settings.load() if saved is None else saved
    lna = int(saved.get("hackrf_lna", DEFAULT_LNA))
    vga = int(saved.get("hackrf_vga", DEFAULT_VGA))
    return (min(LNA_STEPS, key=lambda s: abs(s - lna)), max(0, min(62, vga // 2 * 2)),
            bool(saved.get("hackrf_amp", False)))


def gain_db(lna: float, vga: float, amp: bool) -> float:
    return float(lna) + float(vga) + (AMP_DB if amp else 0.0)


def bias_tee() -> bool:
    """Power on the antenna port, for an active antenna or an inline LNA. Never while sending."""
    return bool(settings.get("hackrf_bias_tee", False))


# Each HackRF reads differently, so each unit (by serial number) has its own calibration and
# its own record of what its antenna hears, under "hackrf_units". The gains are Setup's, shared.
# A calibration from before there were units sits in the old top-level keys until a HackRF is
# opened, which takes it over (see adopt_legacy).
LEGACY_KEYS = {"offset_db": "hackrf_offset_db", "band_offsets": "hackrf_band_offsets",
               "cal_gains": "hackrf_cal_gains", "calibrated_with": "hackrf_calibrated_with",
               "deaf": "hackrf_deaf"}


def _unit_cal(saved: dict, unit: str | None) -> dict:
    """One unit's calibration: its own entry, or the old one nobody has taken over yet."""
    units = saved.get("hackrf_units") or {}
    if (unit or "") in units:
        return units[unit or ""] or {}
    return {key: saved.get(old) for key, old in LEGACY_KEYS.items() if saved.get(old) is not None}


def _put_unit(unit: str | None, **changes) -> None:
    units = dict(settings.get("hackrf_units") or {})
    entry = dict(_unit_cal(settings.load(), unit))
    entry.update(changes)
    units[unit or ""] = entry
    settings.put("hackrf_units", units)


def adopt_legacy(unit: str | None) -> None:
    """
    The first HackRF opened takes over a calibration saved before there were units. One saved
    before gains were kept with it was made at the gains in use until now, so those become its
    gains: from here on, changing them moves the offset to match.
    """
    saved = settings.load()
    units = saved.get("hackrf_units") or {}
    legacy = {key: saved.get(old) for key, old in LEGACY_KEYS.items() if saved.get(old) is not None}
    if (unit or "") in units:
        entry = units[unit or ""] or {}
        if entry.get("offset_db") is not None and entry.get("cal_gains") is None:
            _put_unit(unit, cal_gains=list(gains(saved)))
        return
    if not legacy:
        return
    if legacy.get("offset_db") is not None and legacy.get("cal_gains") is None:
        legacy["cal_gains"] = list(gains(saved))
    _put_unit(unit, **legacy)
    for old in LEGACY_KEYS.values():
        if saved.get(old) is not None:
            settings.put(old, None)


def gain_shift(saved: dict | None = None, unit: str | None = None) -> float:
    """
    The dB of gain added since the unit's offset was set: what to take off it. An offset set
    before gains were recorded with it is taken as is; the default estimate is for the default
    gains.
    """
    saved = settings.load() if saved is None else saved
    cal = _unit_cal(saved, unit)
    stored = cal.get("cal_gains")
    if stored is None:
        if cal.get("offset_db") is not None:
            return 0.0
        stored = (DEFAULT_LNA, DEFAULT_VGA, False)
    return gain_db(*gains(saved)) - gain_db(*stored)


def cal_band(freq_mhz: float) -> str | None:
    return next((band for band, (low, high) in CAL_BANDS.items() if low <= freq_mhz < high), None)


def offset_db(freq_mhz: float | None = None, saved: dict | None = None, unit: str | None = None) -> float:
    """
    dB to add to a reading from this unit to make it dBm, at today's gains: the band's own
    offset where calibrating found one, otherwise the overall one.
    """
    saved = settings.load() if saved is None else saved
    cal = _unit_cal(saved, unit)
    value = cal.get("offset_db")
    band = cal_band(freq_mhz) if freq_mhz is not None else None
    if band is not None:
        value = (cal.get("band_offsets") or {}).get(band, value)
    base = DEFAULT_OFFSET_DB if value is None else float(value)
    return base - gain_shift(saved, unit)


def offsets(freqs, unit: str | None = None) -> np.ndarray:
    """offset_db for every frequency (MHz) in an array, reading the settings once."""
    freqs = np.asarray(freqs, dtype=float)
    saved = settings.load()
    result = np.full(freqs.shape, offset_db(saved=saved, unit=unit))
    for band, (low, high) in CAL_BANDS.items():
        if band in (_unit_cal(saved, unit).get("band_offsets") or {}):
            result[(freqs >= low) & (freqs < high)] = offset_db((low + high) / 2, saved, unit)
    return result


def band_offsets(unit: str | None = None) -> dict[str, float]:
    """The bands with an offset of their own, as they stand at today's gains."""
    saved = settings.load()
    return {band: offset_db(sum(CAL_BANDS[band]) / 2, saved, unit)
            for band in (_unit_cal(saved, unit).get("band_offsets") or {}) if band in CAL_BANDS}


def calibrated(unit: str | None = None) -> bool:
    return _unit_cal(settings.load(), unit).get("offset_db") is not None


def calibrated_with(unit: str | None = None) -> str | None:
    return _unit_cal(settings.load(), unit).get("calibrated_with")


def save_offsets(overall: float, bands: dict[str, float], with_what: str | None,
                 unit: str | None = None) -> None:
    """New offsets, good at today's gains, which are kept with them so later changes follow."""
    _put_unit(unit, offset_db=round(overall, 1),
              band_offsets={band: round(value, 1) for band, value in bands.items()},
              cal_gains=list(gains()), calibrated_with=with_what)


def set_offset_by_hand(value: float, unit: str | None = None) -> None:
    save_offsets(value, {}, None, unit)


def deaf_bands(unit: str | None = None) -> list[str]:
    """Bands where the unit last heard nothing of strong signals: probably its antenna."""
    return list((_unit_cal(settings.load(), unit).get("deaf") or {}).get("bands") or [])


def deaf_since(unit: str | None = None) -> str:
    return (_unit_cal(settings.load(), unit).get("deaf") or {}).get("when") or ""


def note_hearing(checked: dict[str, bool], unit: str | None = None) -> None:
    """Remembers, per band checked, whether the unit heard strong signals there."""
    if not checked:
        return
    deaf = set(deaf_bands(unit))
    deaf |= {band for band, heard in checked.items() if not heard}
    deaf -= {band for band, heard in checked.items() if heard}
    _put_unit(unit, deaf={"bands": sorted(deaf), "when": time.strftime("%d %b %Y")} if deaf else None)


def snapshot(unit: str | None = None) -> dict:
    """The unit's calibration as it stands, kept with each survey point for the report."""
    saved = settings.load()
    cal = _unit_cal(saved, unit)
    return {"calibrated": cal.get("offset_db") is not None,
            "offset_db": round(offset_db(saved=saved, unit=unit), 1),
            "band_offsets": {band: round(value, 1) for band, value in band_offsets(unit).items()},
            "gain_shift": round(gain_shift(saved, unit), 1),
            "gains": list(gains(saved)), "bias_tee": bool(saved.get("hackrf_bias_tee", False)),
            "calibrated_with": cal.get("calibrated_with"),
            "deaf": list((cal.get("deaf") or {}).get("bands") or [])}


def antenna_note(bands: list[str]) -> str:
    if not bands:
        return ""
    names = " and ".join(f"{band} GHz" for band in bands)
    return (f"The HackRF heard nothing of the strong signals on {names}: its antenna probably "
            f"doesn't cover {'them' if len(bands) > 1 else 'it'} (many HackRF antennas are "
            "2.4 GHz only). Use a dual-band or wideband antenna.")


def band_hearing(networks: list[dict], segments: list[dict]) -> dict[str, bool]:
    """
    For each band where the card heard a strong network, whether the HackRF's sweep (its
    segments) saw any of those channels clearly above its floor. It takes the median bin
    across the channel's middle 16 MHz, so it needs energy as wide as Wi-Fi: the HackRF's own
    narrow spurs (one sits at 5186 MHz) don't count.
    """
    from ..core import analysis
    from ..core.project import Point
    from . import spectrum as spec

    point = Point(x=0, y=0, networks=networks, spectrum={"segments": segments})
    result: dict[str, bool] = {}
    for _number, centre in spec.all_channels():
        strong = analysis.wifi_power(point, centre)
        segment = spec.segment_for(point.spectrum, centre)
        if strong is None or strong < STRONG_DBM or segment is None:
            continue
        peaks = np.asarray(segment["max"], dtype=float)
        freqs = segment["start_mhz"] + np.arange(peaks.size) * segment["step_mhz"]
        inside = peaks[np.abs(freqs - centre) <= 8]
        if inside.size == 0:
            continue
        floor = float(np.percentile(np.asarray(segment["min"], dtype=float), 10))
        band = spec.band_of(centre)
        result[band] = result.get(band, False) or bool(np.median(inside) >= floor + HEARD_DB)
    return result


# ---------- raw I/Q kept as SigMF ----------

def keep_iq() -> bool:
    return bool(settings.get("hackrf_keep_iq", True))


def recordings_dir() -> Path:
    from ..core.project import data_dir

    return data_dir() / "iq"


def recordings_size() -> tuple[int, int]:
    """How many recordings are kept, and their size in bytes."""
    try:
        files = list(recordings_dir().glob("*.sigmf-data"))
    except OSError:
        return 0, 0
    return len(files), sum(f.stat().st_size for f in files if f.exists())


def save_sigmf(raw: bytes, center_mhz: float, rate: float, hardware: str, description: str,
               annotations: list[dict]) -> str:
    """
    Raw I/Q as a SigMF recording (ci8, as hackrf_transfer writes it), for a closer look
    later in inspectrum, GNU Radio or SigMF tools. Returns the data file's path.
    """
    from ..core.project import hand_back, make_dir

    folder = make_dir(recordings_dir())
    stem = time.strftime("%Y%m%d-%H%M%S") + f"-{center_mhz:.0f}MHz"
    base, number = folder / stem, 2
    while base.with_suffix(".sigmf-data").exists():
        base, number = folder / f"{stem}-{number}", number + 1
    data, meta = base.with_suffix(".sigmf-data"), base.with_suffix(".sigmf-meta")
    data.write_bytes(raw)
    meta.write_text(json.dumps({
        "global": {"core:datatype": "ci8", "core:sample_rate": rate, "core:version": "1.0.0",
                   "core:hw": hardware, "core:recorder": "Wi-Fi Surveyor",
                   "core:description": description},
        "captures": [{"core:sample_start": 0, "core:frequency": center_mhz * 1e6,
                      "core:datetime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}],
        "annotations": annotations,
    }, indent=1))
    for path in (data, meta):
        hand_back(path)
    _prune(folder)
    return str(data)


def _prune(folder: Path) -> None:
    """Deletes the oldest recordings while they add up to more than KEEP_IQ_MB."""
    files = sorted(folder.glob("*.sigmf-data"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while files and total > KEEP_IQ_MB * 1e6:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        for path in (oldest, oldest.with_suffix(".sigmf-meta")):
            try:
                path.unlink()
            except OSError:
                pass


class HackRf(Analyzer):
    name = "HackRF One"

    def __init__(self, usb: UsbHackrf) -> None:
        super().__init__(usb.node)
        self.usb = usb
        self.name = usb.model
        self.model = usb.model + (f" {usb.serial[-8:]}" if usb.serial else "")
        self.unit = usb.serial or usb.node      # whose calibration its readings use
        self._proc: subprocess.Popen | None = None
        self._tail: Tail | None = None
        self._target: tuple[float, float, int] | None = None
        self._failed = ""
        self._running = True
        self._tone: subprocess.Popen | None = None
        self._tone_tail: Tail | None = None
        self._tone_timer: threading.Timer | None = None
        self._tone_lock = threading.Lock()
        self._resume_target: tuple[float, float, int] | None = None
        self.tone_mhz: float | None = None
        self.tone_until: float | None = None
        # Why the tone stopped, when it wasn't asked to: for the app to say once, then clear
        self.tone_note = ""
        self._reach = self._find_reach()
        adopt_legacy(self.unit)

    # ---------- running the tools ----------

    def _device_args(self) -> list[str]:
        return ["-d", self.usb.serial] if self.usb.serial else []

    def _gain_args(self) -> list[str]:
        """Gains and antenna power for receiving."""
        lna, vga, amp = gains()
        return ["-l", str(lna), "-g", str(vga), "-a", "1" if amp else "0",
                *(["-p", "1"] if bias_tee() else [])]

    def _run(self, command: list[str], timeout: float) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(command, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"{command[0]} didn't finish in {timeout:.0f} s.") from error

    @staticmethod
    def _trouble(stderr: bytes | str) -> str:
        text = (stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr).strip()
        if "insufficient permissions" in text or "Access denied" in text or "LIBUSB_ERROR_ACCESS" in text:
            return ("No permission to open the HackRF. Install the hackrf package (its udev rule "
                    "gives you access) and replug it, or run as root.")
        if "not supported by installed firmware" in text or "(-1005)" in text:
            return ("The HackRF's firmware is too old for this. Update it: sudo apt install "
                    "hackrf-firmware, then hackrf_spiflash -w /usr/share/hackrf/hackrf_one_usb.bin "
                    "and replug it.")
        if "busy" in text.lower():
            return "The HackRF is in use by another program."
        if "not found" in text.lower() and "hackrf" in text.lower():
            return "The HackRF isn't answering. Is it still plugged in?"
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else "the hackrf tool failed"

    def _find_reach(self) -> float:
        """
        Whether these hackrf tools tune above 6 GHz: asks for one sweep near 7.1 GHz. That
        also proves the device can be opened, so a permission problem shows up now.
        """
        command = ["hackrf_sweep", *self._device_args(), "-f", "7100:7120", "-w", "1000000", "-1"]
        done = self._run(command, 10)
        if done.returncode == 0 and b"," in done.stdout:
            return TUNE_TOP_MHZ
        trouble = self._trouble(done.stderr)
        if "freq" in done.stderr.decode("utf-8", "replace").lower() or "argument" in trouble.lower():
            check = self._run(["hackrf_sweep", *self._device_args(), "-f", "2400:2420", "-1"], 10)
            if check.returncode == 0:
                return SPEC_TOP_MHZ
            trouble = self._trouble(check.stderr)
        raise ConnectionError(trouble)

    def _stop_process(self) -> None:
        proc, self._proc, tail = self._proc, None, self._tail
        if proc is not None:
            _end(proc, tail)

    def _start_sweeping(self, start_mhz: float, end_mhz: float, width_hz: int) -> None:
        self._stop_process()
        low, high = math.floor(start_mhz), math.ceil(end_mhz)
        if high - low < 20:
            high = low + 20
        command = ["hackrf_sweep", *self._device_args(), "-f", f"{low}:{high}",
                   "-w", str(width_hz), *self._gain_args()]
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        tail = Tail(proc.stderr)
        self._proc, self._tail = proc, tail
        threading.Thread(target=self._read, args=(proc, tail, (start_mhz, end_mhz, width_hz)),
                         daemon=True).start()

    def _read(self, proc: subprocess.Popen, tail: Tail, target: tuple[float, float, int]) -> None:
        assembler = Assembler()
        start, end, width = target
        step = width / 1e6
        points = int(round((end - start) / step)) + 1
        grid = start + np.arange(points) * step
        # Looked at again every second, so a new offset or calibration shows straight away
        offset, offset_at = offsets(grid, self.unit), time.monotonic()
        try:
            for line in proc.stdout:
                pieces = assembler.add(line)
                if pieces is None:
                    continue
                freqs, levels = bins(pieces)
                # A sweep that doesn't cover the range (the first, cut short) is no use
                if freqs.size == 0 or freqs[0] > start + 5.5 or freqs[-1] < end - 5.5:
                    continue
                if self._target != target or self._proc is not proc:
                    break
                if time.monotonic() - offset_at >= 1.0:
                    offset, offset_at = offsets(grid, self.unit), time.monotonic()
                self._add_sweep((np.interp(grid, freqs, levels) + offset).tolist())
        except (ValueError, OSError):
            return                                 # its pipe was closed: it was stopped
        # Stopped on purpose, it's no longer self._proc; otherwise it died by itself
        if proc.wait() != 0 and self._proc is proc and self._running:
            self._failed = self._trouble(tail.text()) if tail.lines else "hackrf_sweep stopped"

    def apply_settings(self) -> None:
        """
        Picks up Setup's gains and antenna power: restarts sweeping with them, in the
        background, once whatever has the device lets go of it.
        """
        def restart() -> None:
            with self.lock:
                if self._target is not None and self._running and not self._tone_on():
                    self.clear_sweeps()
                    self._start_sweeping(*self._target)

        threading.Thread(target=restart, daemon=True).start()

    # ---------- what the app asks of an analyzer ----------

    @property
    def max_mhz(self) -> float:
        return self._reach

    @property
    def summary(self) -> str:
        reach = (f"reaches {self._reach:.0f} MHz (above 6000 outside its specification)"
                 if self._reach > SPEC_TOP_MHZ else f"reaches {self._reach:.0f} MHz")
        state = "calibrated" if calibrated(self.unit) else "not calibrated yet, so levels are estimates"
        return f"{self.model}, {reach}, {state}"

    @property
    def alive(self) -> bool:
        return self._running and not self._failed and os.path.exists(self.usb.node)

    @property
    def failure(self) -> str:
        return self._failed

    def close(self) -> None:
        self._running = False
        self._end_tone()
        self._stop_process()

    def _gone(self) -> ConnectionError:
        if self._failed:
            return ConnectionError(f"The {self.name}: {self._failed}")
        return super()._gone()

    def _configure(self, start_mhz: float, end_mhz: float, timeout: float) -> Config:
        if not self.alive:
            raise self._gone()
        if self._tone_on():
            raise RuntimeError(f"The {self.name} is sending the test tone. Stop the tone to sweep.")
        if start_mhz < MIN_MHZ - 0.5 or end_mhz > self._reach + 0.5:
            raise ValueError(f"The {self.name} reaches {self._reach:.0f} MHz, so it can't sweep "
                             f"{start_mhz:.0f}-{end_mhz:.0f} MHz.")
        width = bin_width_hz(start_mhz, end_mhz)
        self._target = (start_mhz, end_mhz, width)
        self.clear_sweeps()
        self._start_sweeping(start_mhz, end_mhz, width)
        step = width / 1e6
        self.config = Config(
            start_mhz=start_mhz, step_mhz=step, points=int(round((end_mhz - start_mhz) / step)) + 1,
            expansion=False, min_mhz=MIN_MHZ, max_mhz=self._reach, max_span_mhz=self._reach,
            # An FFT bin is its own resolution bandwidth
            rbw_khz=width / 1e3,
        )
        return self.config

    # ---------- the device to itself ----------

    @contextmanager
    def exclusive(self):
        """Pauses sweeping for something else that needs the device, then picks up again."""
        with self.lock:
            if self._tone_on():
                raise RuntimeError(f"The {self.name} is sending the test tone.")
            target = self._target
            self._target = None
            self._stop_process()
            try:
                yield
            finally:
                if target is not None and self._running:
                    self._target = target
                    self.clear_sweeps()
                    self._start_sweeping(*target)

    def _transfer_command(self, center_mhz: float, count: int, rate: float) -> list[str]:
        return ["hackrf_transfer", *self._device_args(), "-r", "-",
                "-f", str(int(center_mhz * 1e6)), "-s", str(int(rate)),
                "-b", str(IQ_FILTER_HZ), "-n", str(count), *self._gain_args()]

    def capture_raw(self, center_mhz: float, seconds: float = 0.25, rate: float = IQ_RATE) -> bytes:
        """Raw I/Q centred on a frequency, as hackrf_transfer gives it: 8-bit I and Q in turn."""
        _check_iq_reach(center_mhz)
        count = int(rate * seconds)
        with self.exclusive():
            done = self._run(self._transfer_command(center_mhz, count, rate), seconds + 15)
        if done.returncode != 0 and len(done.stdout) < 2 * count:
            raise RuntimeError(self._trouble(done.stderr))
        if len(done.stdout) < 2 * count:      # two bytes a sample: it stopped early
            raise RuntimeError(f"The {self.name} sent {len(done.stdout) // 2} of {count} samples.")
        return done.stdout[: 2 * count]

    def capture(self, center_mhz: float, seconds: float = 0.25, rate: float = IQ_RATE) -> np.ndarray:
        """Raw I/Q centred on a frequency, as complex samples with DC taken out."""
        return iq.from_int8(self.capture_raw(center_mhz, seconds, rate))

    def identify(self, center_mhz: float, seconds: float = 0.25) -> dict:
        """
        What's transmitting on a channel: see rf.iq.classify. Also says how much of the
        capture clipped ("clipped"), and where its raw I/Q was kept ("recording") when it
        holds something that couldn't be named, or radar.
        """
        raw = self.capture_raw(center_mhz, seconds)
        result = iq.classify(iq.from_int8(raw), center_mhz)
        result["clipped"] = round(iq.clipped_share(raw), 5)
        keep = [f for f in result["findings"] if f["kind"] in KEEP_KINDS]
        if keep and keep_iq():
            try:
                result["recording"] = save_sigmf(
                    raw, center_mhz, IQ_RATE, self.model,
                    "; ".join(iq.describe(f) for f in result["findings"]),
                    [{"core:sample_start": 0, "core:sample_count": len(raw) // 2,
                      "core:freq_lower_edge": (f["center_mhz"] - f["width_mhz"] / 2) * 1e6,
                      "core:freq_upper_edge": (f["center_mhz"] + f["width_mhz"] / 2) * 1e6,
                      "core:label": f["name"], "core:comment": iq.describe(f)} for f in keep])
            except OSError:
                pass                               # a full disk mustn't lose the result
        return result

    def watch_pulses(self, center_mhz: float, seconds: float, stop: threading.Event | None = None,
                     rate: float = IQ_RATE) -> iq.PulseWatch:
        """
        Streams I/Q for a while looking for radar pulses, keeping only what it finds, and the
        ~100 ms with the most pulses as a recording when it hears radar. Sets ``behind`` on the
        watch if this computer couldn't keep up (samples were lost), and counts clipping.
        """
        _check_iq_reach(center_mhz)
        watch = iq.PulseWatch(rate)
        count = int(rate * seconds)
        chunk = 1 << 20
        recent: collections.deque[tuple[bytes, int]] = collections.deque(maxlen=RADAR_SNIPPET_CHUNKS)
        best: tuple[int, int, bytes] = (0, 0, b"")       # pulses, first byte, the bytes
        received, began = 0, None
        with self.exclusive():
            proc = subprocess.Popen(self._transfer_command(center_mhz, count, rate),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            tail = Tail(proc.stderr)
            try:
                while not (stop and stop.is_set()):
                    data = proc.stdout.read(chunk)
                    if not data:
                        break
                    if began is None:
                        began = time.monotonic()
                    before = watch.found
                    watch.feed_int8(data)
                    recent.append((data, before))
                    received += len(data)
                    heard = watch.found - recent[0][1]
                    if heard > best[0]:
                        start = received - sum(len(d) for d, _ in recent)
                        best = (heard, start, b"".join(d for d, _ in recent))
            finally:
                _end(proc, tail)
        if watch.position == 0 and not (stop and stop.is_set()):
            raise RuntimeError(self._trouble(tail.text()))
        if began is not None and received:
            took = time.monotonic() - began
            watch.behind = took > 1.2 * (received / 2 / rate) + 0.5
        trains = watch.trains()
        if trains and best[2] and keep_iq():
            first = best[1] // 2
            try:
                watch.recording = save_sigmf(
                    best[2], center_mhz, rate, self.model,
                    f"Radar pulses at {center_mhz:.0f} MHz, from sample {first} of the watch",
                    [{"core:sample_start": max(0, int(t["time_us"] * rate / 1e6) - first),
                      "core:sample_count": int(t["pri_us"] * t["pulses"] * rate / 1e6),
                      "core:label": "Radar",
                      "core:comment": f"pulses every {t['pri_us']:.0f} µs, {t['width_us']:.0f} µs long"}
                     for t in trains])
            except OSError:
                pass
        return watch

    def sweep_bands(self, ranges: list[tuple[float, float]], sweeps: int = 6,
                    width_hz: int = 100_000) -> tuple[np.ndarray, np.ndarray]:
        """
        Several ranges in one pass of hackrf_sweep (it takes up to ten), averaged over a few
        sweeps. Returns bin centres in MHz and levels in dBm (with the offset applied).
        """
        freqs, levels, _peak = self._sweep_stats(ranges, sweeps, width_hz)
        return freqs, levels

    def scan(self, start_mhz: float, end_mhz: float, step_khz: float, sweeps: int = 3,
             timeout: float = 30) -> Trace:
        """A range at about ``step_khz`` bins in one hackrf_sweep run: averaged and peak-held."""
        width = int(min(MAX_BIN_HZ, max(MIN_BIN_HZ, step_khz * 1000)))
        freqs, avg, peak = self._sweep_stats([(start_mhz, end_mhz)], sweeps, width)
        keep = (freqs >= start_mhz) & (freqs <= end_mhz)
        if not keep.any():
            raise RuntimeError(f"hackrf_sweep returned nothing for {start_mhz:.0f}-{end_mhz:.0f} MHz.")
        return Trace(freqs[keep], avg[keep], peak[keep], width / 1e3)

    @property
    def min_mhz(self) -> float:
        return MIN_MHZ

    def firmware(self) -> str:
        """The firmware version hackrf_info reports, e.g. "2026.01.3". Pauses sweeping."""
        with self.exclusive():
            done = self._run(["hackrf_info"], 10)
        text = done.stdout.decode("utf-8", "replace")
        serial = self.usb.serial
        # With several HackRFs, the block for this one
        blocks = text.split("Found HackRF")
        block = next((b for b in blocks if serial and serial in b), text)
        for line in block.splitlines():
            if line.strip().startswith("Firmware Version:"):
                return line.split(":", 1)[1].split("(")[0].strip()
        raise RuntimeError(self._trouble(done.stderr) if done.stderr else "hackrf_info said no version")

    def _sweep_stats(self, ranges: list[tuple[float, float]], sweeps: int,
                     width_hz: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Bin centres (MHz), average and peak (dBm, offset applied) over ``sweeps`` passes."""
        args = []
        for low, high in ranges[:10]:
            args += ["-f", f"{math.floor(low)}:{max(math.ceil(high), math.floor(low) + 20)}"]
        command = ["hackrf_sweep", *self._device_args(), *args, "-w", str(width_hz),
                   "-N", str(sweeps), *self._gain_args()]
        with self.exclusive():
            done = self._run(command, 20 + 3 * sweeps)
        if done.returncode != 0 and b"," not in done.stdout:
            raise RuntimeError(self._trouble(done.stderr))
        totals: dict[int, list] = {}
        for line in done.stdout.decode("utf-8", "replace").splitlines():
            parsed = parse_line(line)
            if parsed is None:
                continue
            low, _high, width, levels = parsed
            values = np.asarray(levels, dtype=float)
            power = np.power(10.0, values / 10)
            entry = totals.setdefault(low, [width, np.zeros_like(power), np.full_like(values, -1e9), 0])
            if entry[1].size == power.size:
                entry[1] += power
                entry[2] = np.maximum(entry[2], values)
                entry[3] += 1
        pieces = {low: (width, (10 * np.log10(np.maximum(total / count, 1e-20))).tolist())
                  for low, (width, total, _peak, count) in totals.items() if count}
        peaks = {low: (width, peak.tolist()) for low, (width, _t, peak, count) in totals.items() if count}
        freqs, levels = bins(pieces)
        _freqs, peak = bins(peaks)
        offset = offsets(freqs, self.unit)
        return freqs, levels + offset, peak + offset

    # ---------- the test tone ----------

    def start_tone(self, freq_mhz: float, tx_vga: int = 10, minutes: float = TONE_MINUTES) -> None:
        """
        A plain carrier for measuring walls, kept to the ISM bands and a low level. It stops by
        itself after ``minutes`` in case it's forgotten. Sweeping picks up again afterwards.
        """
        if not tone_allowed(freq_mhz):
            raise ValueError(f"{freq_mhz:.0f} MHz isn't in the 2.4 or 5.8 GHz ISM band.")
        tx_vga = max(0, min(MAX_TX_VGA, int(tx_vga)))
        # Antenna power off: never into whatever is on the port while sending
        command = ["hackrf_transfer", *self._device_args(), "-c", "127",
                   "-f", str(int(freq_mhz * 1e6)), "-s", "2000000", "-x", str(tx_vga), "-a", "0",
                   "-p", "0"]
        with self.lock:
            if self._end_tone() is None:
                self._resume_target = self._target
            self._target = None
            self._stop_process()
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            tail = Tail(proc.stderr)
            time.sleep(0.8)
            if proc.poll() is not None:
                _end(proc, tail)
                self._resume()
                raise RuntimeError(self._trouble(tail.text()))
            with self._tone_lock:
                self._tone, self._tone_tail, self.tone_mhz = proc, tail, freq_mhz
                self.tone_until = time.time() + minutes * 60
                self.tone_note = ""
                self._tone_timer = threading.Timer(minutes * 60, self._time_up, args=(proc, minutes))
                self._tone_timer.daemon = True
                self._tone_timer.start()

    def _time_up(self, proc: subprocess.Popen, minutes: float) -> None:
        if self._tone is proc:
            self.tone_note = f"The test tone turned itself off after {minutes:.0f} minutes."
            self.stop_tone()

    def stop_tone(self) -> None:
        if self._end_tone() is not None:
            self._resume()

    def _end_tone(self) -> subprocess.Popen | None:
        """Stops the tone if there is one, and returns the process it was."""
        with self._tone_lock:
            if self._tone_timer is not None:
                self._tone_timer.cancel()
                self._tone_timer = None
            proc, self._tone, self.tone_mhz, self.tone_until = self._tone, None, None, None
            tail = self._tone_tail
        if proc is not None:
            _end(proc, tail)
        return proc

    def _resume(self) -> None:
        """Sweeping again where it was before the tone, in the background: it may need the lock."""
        target, self._resume_target = self._resume_target, None
        if target is None or not self._running:
            return

        def go() -> None:
            with self.lock:
                if self._running and self._target is None and self._tone is None:
                    self._target = target
                    self.clear_sweeps()
                    self._start_sweeping(*target)

        threading.Thread(target=go, daemon=True).start()

    def _tone_on(self) -> bool:
        """Whether the tone is on. One that stopped by itself is cleaned up, and why is kept."""
        proc = self._tone
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        tail = self._tone_tail
        why = self._trouble(tail.text(0.2)) if tail is not None and tail.lines else "hackrf_transfer stopped"
        if self._end_tone() is proc:
            self.tone_note = f"The test tone stopped by itself: {why}"
            self._resume()
        return False

    @property
    def sending(self) -> bool:
        return self._tone_on()

    @property
    def tone_left(self) -> float | None:
        """Seconds until the tone turns itself off, while it's on."""
        until = self.tone_until
        return None if until is None or not self.sending else max(0.0, until - time.time())


def calibration_offset(reference: list[tuple[float, float]], measured: list[tuple[float, float]]
                       ) -> tuple[float, int] | None:
    """
    The dB to add so the HackRF reads like a reference: given (channel centre, power) from
    each, the median difference over channels both measured. None if fewer than two match.
    """
    theirs = dict(reference)
    differences = [theirs[centre] - power for centre, power in measured if centre in theirs]
    if len(differences) < 2:
        return None
    return float(np.median(differences)), len(differences)


# ---------- calibrating against something that reads true dBm ----------

CALIBRATION_RANGES = ((2400.0, 2500.0), (5150.0, 5350.0))


@dataclass(slots=True)
class Calibration:
    offset: float                     # the overall offset now
    channels: int                     # how many channels it was worked out from
    bands: dict[str, float] = field(default_factory=dict)    # bands with their own offset
    deaf: list[str] = field(default_factory=list)            # bands left out: nothing heard


def _recalibrate(differences: list[tuple[float, float]], with_what: str, deaf: list[str],
                 unit: str | None = None) -> Calibration:
    """
    New offsets from (channel centre, dB the HackRF reads low by there): the median over every
    channel, and over each band that has two of them. Saved for the unit, and returned.
    """
    saved = settings.load()
    absolute = [(centre, offset_db(centre, saved, unit) + low_by) for centre, low_by in differences]
    overall = float(np.median([value for _c, value in absolute]))
    bands = {}
    for band in CAL_BANDS:
        inside = [value for centre, value in absolute if cal_band(centre) == band]
        if len(inside) >= 2:
            bands[band] = float(np.median(inside))
    save_offsets(overall, bands, with_what, unit)
    return Calibration(offset_db(unit=unit), len(differences), band_offsets(unit), deaf)


def channel_levels(analyzer, sweeps: int = 20, trace: str = "avg") -> list[tuple[float, float, float]]:
    """(channel centre, power, noise floor per 20 MHz) for every Wi-Fi channel in the ranges."""
    from . import spectrum as spec

    found = []
    for start, end in CALIBRATION_RANGES:
        for config, data in analyzer.sweep_range(start, end, count=sweeps):
            segment = spec.build_segment(config, data)
            floor = spec.noise_floor_per_20mhz(segment["min"], config.rbw_khz)
            top = config.start_mhz + config.step_mhz * (len(segment[trace]) - 1)
            for _number, centre in spec.all_channels():
                if centre - 10 >= config.start_mhz and centre + 10 <= top:
                    power = spec.channel_power(segment[trace], config.start_mhz, config.step_mhz,
                                               config.rbw_khz, centre)
                    if power is not None:
                        found.append((centre, power, floor))
    return found


def match_analyzer(device: HackRf, reference) -> Calibration:
    """
    Sets the offsets so the HackRF reads like another analyzer on the channels where that one
    sees something well above its floor. A band where the HackRF hears none of those above
    its own floor is left out: its antenna doesn't reach there.
    """
    from . import spectrum as spec

    theirs = [(c, p) for c, p, floor in channel_levels(reference) if p > floor + 10]
    ours = {c: (p, floor) for c, p, floor in channel_levels(device)}
    checked: dict[str, bool] = {}
    for centre, _p in theirs:
        if centre in ours:
            band = spec.band_of(centre)
            power, floor = ours[centre]
            checked[band] = checked.get(band, False) or power >= floor + HEARD_DB
    note_hearing(checked, device.unit)
    deaf = sorted(band for band, heard in checked.items() if not heard)
    differences = [(c, p - ours[c][0]) for c, p in theirs
                   if c in ours and spec.band_of(c) not in deaf]
    if len(differences) < 2:
        raise RuntimeError("Too few busy channels to compare. Try where Wi-Fi is strong."
                           + (" " + antenna_note(deaf) if deaf else ""))
    name = getattr(reference, "model", "") or reference.name
    return _recalibrate(differences, f"{name}, {time.strftime('%d %b %Y')}", deaf, device.unit)


def match_wifi(device: HackRf, networks: list[dict]) -> Calibration:
    """
    Sets the offsets so the HackRF's peak power on each channel matches what the Wi-Fi card
    heard there, on channels where one strong network dominates: the comparison the survey
    already makes between card and analyzer (analysis.point_offset). A band where the card
    hears strong networks and the HackRF none of them is left out, as its antenna's fault.
    """
    from ..core import analysis
    from ..core.project import Point
    from . import spectrum as spec

    segments = []
    for start, end in CALIBRATION_RANGES:
        for config, data in device.sweep_range(start, end, count=20):
            segments.append(spec.build_segment(config, data))
    checked = band_hearing(networks, segments)
    note_hearing(checked, device.unit)
    deaf = sorted(band for band, heard in checked.items() if not heard)
    point = Point(x=0, y=0, networks=networks, spectrum={"segments": segments})
    differences = []
    for _number, centre in spec.all_channels():
        card = analysis.wifi_power(point, centre)
        measured = analysis.measured_power(point, centre)
        if card is None or measured is None or card < -70 or spec.band_of(centre) in deaf:
            continue
        differences.append((centre, card - measured))
    if len(differences) < 2:
        raise RuntimeError("Too few strong networks to compare (it needs two channels with "
                           "one heard at -70 dBm or better)."
                           + (" " + antenna_note(deaf) if deaf else ""))
    return _recalibrate(differences, f"the Wi-Fi card, {time.strftime('%d %b %Y')}", deaf, device.unit)


# ---------- the radar watch ----------

@dataclass(slots=True)
class RadarWatch:
    events: list[dict] = field(default_factory=list)
    channels: list[int] = field(default_factory=list)       # watched, wholly or in part
    overloaded: list[int] = field(default_factory=list)
    behind: list[int] = field(default_factory=list)         # samples were lost there
    error: str = ""                   # why it ended early, if it did


def watch_dfs(devices, seconds: float, stop: threading.Event,
              progress=lambda text: None, channels=RADAR_CHANNELS) -> RadarWatch:
    """
    Listens on each DFS channel for radar pulse trains. With several HackRFs the channels are
    shared out and watched at once, so each gets as much longer as there are HackRFs; the time
    is split evenly. Stopping, or a HackRF failing partway, keeps everything heard until then,
    and the others carry on.
    """
    from . import spectrum as spec

    devices = list(devices) if isinstance(devices, (list, tuple)) else [devices]
    shares = [list(channels[i::len(devices)]) for i in range(len(devices))]
    each = seconds * len(devices) / len(channels)
    results = [RadarWatch() for _ in devices]
    done = [0]
    lock = threading.Lock()

    def run(device, mine: list[int], result: RadarWatch) -> None:
        for channel in mine:
            if stop.is_set():
                return
            centre = spec.CHANNELS[channel]
            with lock:
                done[0] += 1
                heard = sum(len(r.events) for r in results)
                progress(f"Channel {channel} ({centre:.0f} MHz), {done[0]} of {len(channels)}"
                         + (f" on {len(devices)} HackRFs" if len(devices) > 1 else "")
                         + f": {heard} radar so far")
            began = time.time()
            try:
                watch = device.watch_pulses(centre, each, stop)
            except Exception as error:
                result.error = f"on channel {channel}: {error}"
                return
            if watch.position == 0:
                return                         # stopped before it heard anything
            result.channels.append(channel)
            if watch.clipped_share >= iq.OVERLOAD_SHARE:
                result.overloaded.append(channel)
            if watch.behind:
                result.behind.append(channel)
            for train in watch.trains():
                event = {"time": began + train["time_us"] / 1e6, "center_mhz": centre,
                         "channel": channel, "pri_us": round(train["pri_us"], 1),
                         "width_us": round(train["width_us"], 1),
                         "pulses": train["pulses"], "above_db": round(train["above_db"], 1)}
                if watch.recording:
                    event["recording"] = watch.recording
                if len(devices) > 1:
                    event["hackrf"] = getattr(device, "model", "")
                result.events.append(event)

    if len(devices) == 1:
        run(devices[0], shares[0], results[0])
    else:
        threads = [threading.Thread(target=run, args=job, daemon=True)
                   for job in zip(devices, shares, results)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    order = {channel: i for i, channel in enumerate(channels)}
    merged = RadarWatch()
    for result in results:
        merged.events += result.events
        merged.channels += result.channels
        merged.overloaded += result.overloaded
        merged.behind += result.behind
    merged.events.sort(key=lambda e: e["time"])
    for key in ("channels", "overloaded", "behind"):
        setattr(merged, key, sorted(getattr(merged, key), key=order.get))
    merged.error = "; ".join(r.error for r in results if r.error)
    return merged


def identify_all(devices: list, centres: list[float], progress=lambda text: None
                 ) -> tuple[list[dict], list[str]]:
    """
    The close look at several channels, shared out between the HackRFs so they listen at once.
    Results come back in the order of ``centres``; one that fails is a note, not the end.
    """
    shares = [centres[i::len(devices)] for i in range(len(devices))] if devices else []
    found: dict[float, dict] = {}
    notes: list[str] = []
    lock = threading.Lock()
    counter = [0]

    def run(device, mine: list[float]) -> None:
        for centre in mine:
            with lock:
                counter[0] += 1
                progress(f"HackRF: listening at {centre:.0f} MHz, {counter[0]} of {len(centres)}…")
            try:
                result = device.identify(centre)
            except Exception as error:
                with lock:
                    notes.append(f"HackRF at {centre:.0f} MHz: {error}")
                continue
            if len(devices) > 1:
                result["hackrf"] = getattr(device, "model", "")
            overload = iq.overload_note(result.get("clipped", 0.0), f"at {centre:.0f} MHz")
            with lock:
                found[centre] = result
                if overload:
                    notes.append(overload)

    threads = [threading.Thread(target=run, args=job, daemon=True) for job in zip(devices, shares)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [found[c] for c in centres if c in found], notes
