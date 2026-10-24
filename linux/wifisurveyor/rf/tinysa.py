"""tinySA serial driver: the tinySA Ultra and Ultra+ (and the original, which can't reach Wi-Fi).

The tinySA is a USB CDC device (0483:5740, /dev/ttyACM*) running a text shell, so the baud
rate doesn't matter:
  to the device   a command and "\\r". "version" names it ("tinySA4_v1.4-…" on the Ultra
                  family), "sweep" with no arguments says "start stop points" in Hz, "rbw N"
                  sets the resolution bandwidth in kHz, "scan START STOP POINTS 3" measures.
  from the device the command echoed, its output, then the prompt "ch> ". scan's output is
                  one "frequency level" line per point, the level in dBm.

The tinySA only measures when asked, so a thread here keeps scanning the range it was last
tuned to. To the rest of the app it then looks like the RF Explorer, which streams sweeps.
"""

from __future__ import annotations

import re
import termios
import threading
import time

import serial

from .analyzer import Analyzer, Config, PortPermissionError, SpanError, denied

USB_ID = ("0483", "5740")          # STM32 virtual COM port, which NanoVNAs use as well
PROMPT = b"ch> "
# The Ultra family: up to 450 points a scan, RBW to 850 kHz. The original: 290, 600 kHz.
ULTRA_POINTS, ORIGINAL_POINTS = 450, 290
ULTRA_RBW_KHZ = (3, 10, 30, 100, 300, 600, 850)
ORIGINAL_RBW_KHZ = (3, 10, 30, 100, 300, 600)
# Wider than this and a 450-point scan's bins get coarser than the widest RBW, so the
# survey's channel power would be estimated from too few samples. Wider ranges go in pieces.
MAX_SPAN_MHZ = 500.0
# If the device won't say how far it reaches: the ZS405 Ultra's specified top, the original's.
ULTRA_REACH_MHZ, ORIGINAL_REACH_MHZ = 5300.0, 960.0
ASK_FOR_MHZ = 12000.0              # asked as a stop frequency; older firmware clamps it to its top
# How far each model reaches, by the code "info" names it with ("tinySA ULTRA+ ZS407"). Firmware
# from late 2026 on takes any stop frequency as asked, so this is how it's told apart there.
MODEL_REACH_MHZ = {"ZS407": 7300.0, "ZS406": 5400.0, "ZS405": 5300.0}
MIN_MHZ = 0.1
# Its generator: "mode low output" is a clean sine up to here, "mode high output" (a square
# wave's harmonics, levels rougher) above. The original tinySA's high output starts at 240 MHz.
ULTRA_LOW_OUT_MHZ, ORIGINAL_LOW_OUT_MHZ = 800.0, 350.0
ULTRA_OUT_TOP_MHZ, ORIGINAL_OUT_TOP_MHZ = 4400.0, 960.0
MODULATIONS = ("off", "AM_1kHz", "NFM", "WFM")


def output_mode(freq_mhz: float, ultra: bool) -> str:
    """Which of the tinySA's outputs reaches a frequency: "low" (sine) or "high"."""
    return "low" if freq_mhz <= (ULTRA_LOW_OUT_MHZ if ultra else ORIGINAL_LOW_OUT_MHZ) else "high"


def generator_commands(freq_mhz: float, level_dbm: float, modulation: str, ultra: bool) -> list[str]:
    """The shell commands that start the generator, output last so nothing goes out half set."""
    if modulation not in MODULATIONS:
        raise ValueError(f"Modulation {modulation!r} isn't one of {', '.join(MODULATIONS)}.")
    return [f"mode {output_mode(freq_mhz, ultra)} output", f"freq {int(round(freq_mhz * 1e6))}",
            f"level {level_dbm:g}", f"modulation {modulation}", "output on"]


def _open(port: str, timeout: float) -> serial.Serial:
    try:
        return serial.Serial(port, 115200, timeout=timeout, write_timeout=1)
    except (serial.SerialException, OSError) as error:
        if denied(error):
            raise PortPermissionError(port) from error
        raise


def _exchange(link: serial.Serial, command: str, timeout: float) -> str:
    """Sends one command and returns what it printed, without the echo and the prompt."""
    link.reset_input_buffer()
    link.write(command.encode("ascii") + b"\r")
    echo = command.encode("ascii") + b"\r\n"
    buffer = b""
    begins = -1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buffer += link.read(max(1, link.in_waiting))
        # The answer is what follows this command's echo, up to the prompt after it. Anything
        # before the echo is the rest of an earlier command's answer: one that timed out, or
        # was still running when the port was opened, and would otherwise be taken for this one.
        if begins < 0:
            begins = buffer.find(echo)
        if begins >= 0 and buffer.endswith(PROMPT) and len(buffer) >= begins + len(echo) + len(PROMPT):
            break
    else:
        raise TimeoutError(f"The tinySA didn't finish '{command}'.")
    body = buffer[begins + len(echo):-len(PROMPT)]
    lines = body.decode("ascii", "replace").replace("\r", "").split("\n")
    return "\n".join(line for line in lines if line.strip())


def probe(port: str, timeout: float = 1.0) -> str | None:
    """
    The version line if a tinySA answers on this port, or None. Raises PortPermissionError
    when the port can't be opened for lack of rights.
    """
    try:
        link = _open(port, 0.1)
    except PortPermissionError:
        raise
    except (serial.SerialException, OSError):
        return None
    try:
        link.write(b"\r")                    # finishes anything half typed
        time.sleep(0.05)
        version = _exchange(link, "version", timeout)
        return version.splitlines()[0].strip() if "tinySA" in version else None
    except (serial.SerialException, OSError, TimeoutError, IndexError):
        return None
    finally:
        try:
            link.close()
        except Exception:
            pass


def parse_scan(text: str) -> list[float]:
    """The levels from scan's "frequency level" lines."""
    levels = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                levels.append(float(parts[1]))
            except ValueError:
                continue
    return levels


def nearest_rbw(step_khz: float, choices: tuple[int, ...]) -> int:
    """The narrowest RBW at least as wide as the step, so no frequency falls between bins."""
    return next((rbw for rbw in choices if rbw >= step_khz), choices[-1])


class TinySa(Analyzer):
    name = "tinySA"
    # A retune waits for the scan in progress (both hold the shell), so every scan after it
    # is of the new range, whole
    first_sweep_partial = False

    def __init__(self, port: str) -> None:
        super().__init__(port)
        self.port = _open(port, 0.2)
        # One command at a time on the shell. Reentrant, so a retune can hold it across the
        # rbw command and the new range together (see _configure)
        self._io = threading.RLock()
        self._target_lock = threading.Lock()
        self._target: tuple[float, float, int] | None = None
        self._running = True
        self._reach: float | None = None
        self.generating: float | None = None     # MHz, while it's a signal generator
        try:
            self.port.write(b"\r")
            time.sleep(0.05)
            # Left mid-scan by the last program to use it, it's still printing that scan: let it
            # finish, or its numbers are taken for the version (and the model for the original)
            self._settle(limit=25)
            self.model = self.command("version").splitlines()[0].strip()
            if not self.model.startswith("tinySA"):
                self._settle(limit=25)
                self.model = self.command("version").splitlines()[0].strip()
            self.ultra = self.model.startswith("tinySA4")
            if self.ultra:
                self.name = "tinySA Ultra"
                # Above 800 MHz the Ultra works in its "ultra" mode, which Wi-Fi needs
                self._quietly("ultra on")
            self._quietly("mode low input")
            hardware = self._hardware()
            self._reach = MODEL_REACH_MHZ.get(hardware.split()[-1] if hardware else "") \
                or self._find_reach()
            if self.ultra and (self._reach > 6500 or "ULTRA+" in hardware.upper()):
                self.name = "tinySA Ultra+"
        except (serial.SerialException, OSError, TimeoutError, IndexError) as error:
            self.close()
            raise ConnectionError(f"{port} opened, but the tinySA didn't answer: {error}") from error
        self.points = ULTRA_POINTS if self.ultra else ORIGINAL_POINTS
        self.rbw_choices = ULTRA_RBW_KHZ if self.ultra else ORIGINAL_RBW_KHZ
        self._scanner = threading.Thread(target=self._scan_loop, daemon=True)
        self._scanner.start()

    # ---------- the shell ----------

    def command(self, text: str, timeout: float = 3.0) -> str:
        if not self._running:
            raise self._gone()
        with self._io:
            try:
                return _exchange(self.port, text, timeout)
            except (serial.SerialException, OSError, termios.error):
                # The port went away (unplugged, or the driver reset): stop, and say so via alive.
                self._running = False
                raise
            except TimeoutError:
                # It may still be busy with the command and print its answer later, where the
                # next command would take it for its own: let it finish first
                self._settle()
                raise
            except Exception:
                if not self._running:        # closed while this was reading
                    raise self._gone() from None
                raise

    def _settle(self, limit: float = 60.0) -> None:
        """Waits, up to ``limit`` seconds, for the prompt or two quiet seconds."""
        tail = b""
        quiet_since = time.monotonic()
        deadline = quiet_since + limit
        try:
            while time.monotonic() < deadline:
                chunk = self.port.read(max(1, self.port.in_waiting))
                if chunk:
                    tail = (tail + chunk)[-len(PROMPT):]
                    quiet_since = time.monotonic()
                if tail == PROMPT or time.monotonic() - quiet_since > 2:
                    return
        except (serial.SerialException, OSError, termios.error):
            self._running = False

    def _hardware(self) -> str:
        """The model line "info" starts with ("tinySA ULTRA+ ZS407"), or "" without one."""
        try:
            lines = self.command("info").splitlines()
        except TimeoutError:
            return ""
        return lines[0].strip() if lines and lines[0].lower().startswith("tinysa") else ""

    def _quietly(self, text: str) -> None:
        """A setting the firmware may not have; it's fine to go without."""
        try:
            self.command(text)
        except TimeoutError:
            pass

    def _sweep_setting(self) -> tuple[float, float] | None:
        numbers = re.findall(r"\d+", self.command("sweep"))
        return (int(numbers[0]) / 1e6, int(numbers[1]) / 1e6) if len(numbers) >= 2 else None

    def _find_reach(self) -> float:
        """
        How far up it sweeps. The tinySA clamps a stop frequency to the top of its range, so
        it's asked for far too much and then asked what it set. That tells a ZS407 Ultra+
        (7.3 GHz) from a ZS406 (5.4 GHz) or a ZS405 Ultra. It starts from a known low stop, so
        a firmware that refuses the request rather than clamping it can't pass for one that
        tops out where it already was. Its own sweep is put back afterwards.
        """
        fallback = ULTRA_REACH_MHZ if self.ultra else ORIGINAL_REACH_MHZ
        try:
            before = self._sweep_setting()
            self.command("sweep 1000000 100000000")
            self.command(f"sweep stop {int(ASK_FOR_MHZ * 1e6)}")
            after = self._sweep_setting()
            if before:
                self.command(f"sweep {int(before[0] * 1e6)} {int(before[1] * 1e6)}")
        except TimeoutError:
            return fallback
        if after is None or not 500 < after[1] < ASK_FOR_MHZ - 1:
            return fallback                  # refused, or didn't clamp: the answer means nothing
        return after[1]

    # ---------- what the app asks of an analyzer ----------

    @property
    def max_mhz(self) -> float:
        return self._reach or ULTRA_REACH_MHZ

    @property
    def min_mhz(self) -> float:
        return MIN_MHZ

    @property
    def scan_points(self) -> int:
        return self.points

    @property
    def summary(self) -> str:
        return f"{self.name} on {self.device} ({self.model}), reaches {self.max_mhz:.0f} MHz"

    @property
    def alive(self) -> bool:
        """False once the port has failed, which is what unplugging looks like."""
        return self._running and self._scanner.is_alive()

    def close(self) -> None:
        self._running = False
        try:
            self.port.close()
        except Exception:
            pass

    def _configure(self, start_mhz: float, end_mhz: float, timeout: float) -> Config:
        if not self.alive:
            raise self._gone()
        if self.generating is not None:
            raise RuntimeError(f"The {self.name} is a signal generator now. Stop it to sweep.")
        if start_mhz < MIN_MHZ - 0.5 or end_mhz > self.max_mhz + 0.5:
            raise ValueError(f"The {self.name} reaches {self.max_mhz:.0f} MHz, so it can't sweep "
                             f"{start_mhz:.0f}-{end_mhz:.0f} MHz.")
        if end_mhz - start_mhz > MAX_SPAN_MHZ + 0.5:
            raise SpanError(end_mhz - start_mhz, MAX_SPAN_MHZ)
        step = (end_mhz - start_mhz) / (self.points - 1)
        rbw = nearest_rbw(step * 1000, self.rbw_choices)
        # The RBW and the range change together. Otherwise the scan thread can start the old
        # range at the new RBW: a wide range at a narrow one takes many seconds, long enough
        # to time out, and is thrown away anyway.
        with self._io:
            self.command(f"rbw {rbw}", timeout)
            with self._target_lock:
                self._target = (start_mhz, end_mhz, self.points)
        self.config = Config(
            start_mhz=start_mhz, step_mhz=step, points=self.points, expansion=False,
            min_mhz=MIN_MHZ, max_mhz=self.max_mhz, max_span_mhz=MAX_SPAN_MHZ, rbw_khz=rbw,
        )
        return self.config

    # ---------- the signal generator ----------

    @property
    def output_top_mhz(self) -> float:
        return ULTRA_OUT_TOP_MHZ if self.ultra else ORIGINAL_OUT_TOP_MHZ

    def generate(self, freq_mhz: float, level_dbm: float, modulation: str = "off") -> None:
        """
        Turns it into a signal generator at a frequency and level. Sweeping stops until
        ``stop_generating``. The caller checks rf.txrules first.
        """
        if not MIN_MHZ <= freq_mhz <= self.output_top_mhz:
            raise ValueError(f"The {self.name} generates {MIN_MHZ:g}-{self.output_top_mhz:.0f} MHz.")
        with self.lock:
            with self._target_lock:
                self._target = None
            self.generating = freq_mhz
            try:
                for text in generator_commands(freq_mhz, level_dbm, modulation, self.ultra):
                    self.command(text)
            except Exception:
                self.stop_generating()
                raise

    def stop_generating(self) -> None:
        """Output off and back to measuring. Safe to call when it isn't generating."""
        with self.lock:
            try:
                self._quietly("output off")
                self._quietly("mode low input")
            finally:
                self.generating = None

    # ---------- measuring ----------

    def _scan_loop(self) -> None:
        idle = threading.Event()
        while self._running:
            # The range is read under the shell's lock, so a retune can't slip in between
            # reading it and starting the scan
            with self._io:
                target = self._target
                if target is not None:
                    start, end, points = target
                    try:
                        text = self.command(f"scan {int(start * 1e6)} {int(end * 1e6)} {points} 3",
                                            timeout=20)
                    except (serial.SerialException, OSError, termios.error, ConnectionError):
                        return               # command() has already marked it gone
                    except TimeoutError:
                        continue             # a scan cut short: the next one starts afresh
            if target is None:
                idle.wait(0.1)
                continue
            trace = parse_scan(text)
            with self._target_lock:
                # Only if it's still tuned there: a retune clears the sweeps it's waiting for
                if self._target is target and len(trace) == points:
                    self._add_sweep(trace)
