"""What every spectrum analyzer driver shares: the range it's tuned to, and its sweeps."""

from __future__ import annotations

import errno
import math
import threading
import time
from dataclasses import dataclass

# The US Wi-Fi bands, each inside the RF Explorer's 600 MHz span limit. 5350-5470 MHz isn't
# Wi-Fi in the US, so it's skipped. The 6G Combo stops at 6100 MHz, which is the bottom of
# 6 GHz (UNII-5, channels 1-29). An analyzer that reaches further sweeps more of 6 GHz in the
# last range, so the order and meaning of the four never change.
WIFI_RANGES = (
    ("2.4 GHz", 2400.0, 2500.0),
    ("5 GHz UNII-1/2A", 5150.0, 5350.0),
    ("5 GHz UNII-2C/3/4", 5470.0, 5925.0),
    ("6 GHz UNII-5", 5925.0, 6100.0),
)
SIX_GHZ_TOP = 7125.0       # the top of US 6 GHz Wi-Fi (UNII-8)


def wifi_ranges(max_mhz: float) -> tuple[tuple[str, float, float], ...]:
    """The Wi-Fi ranges for an analyzer that reaches ``max_mhz``, 6 GHz as far as it goes."""
    top = min(SIX_GHZ_TOP, max_mhz)
    if top <= WIFI_RANGES[3][2]:
        return WIFI_RANGES
    name = "6 GHz" if top >= SIX_GHZ_TOP else f"6 GHz to {top:.0f} MHz"
    return (*WIFI_RANGES[:3], (name, WIFI_RANGES[3][1], top))


class PortPermissionError(PermissionError):
    """The port exists but this user may not open it, which is a fix-once setup problem."""

    def __init__(self, port: str) -> None:
        super().__init__(
            f"No permission to open {port}. Serial ports belong to the dialout group: "
            "use Setup → Grant serial access, or run 'sudo usermod -aG dialout $USER' "
            "and log in again."
        )
        self.port = port


class SpanError(ValueError):
    """The range is wider than the analyzer will sweep at once; ``limit_mhz`` is what it will."""

    def __init__(self, span_mhz: float, limit_mhz: float) -> None:
        super().__init__(f"{span_mhz:.0f} MHz is wider than the device's {limit_mhz:.0f} MHz limit.")
        self.limit_mhz = limit_mhz


def denied(error: Exception) -> bool:
    return getattr(error, "errno", None) == errno.EACCES or "Permission denied" in str(error)


@dataclass(slots=True)
class Trace:
    """A range measured finely: bin centres (MHz), averaged and peak-held levels (dBm)."""
    freqs: object                 # numpy arrays, lowest frequency first
    avg: object
    peak: object
    rbw_khz: float


@dataclass(slots=True)
class Config:
    start_mhz: float
    step_mhz: float
    points: int
    expansion: bool
    min_mhz: float
    max_mhz: float
    max_span_mhz: float
    rbw_khz: float

    @property
    def end_mhz(self) -> float:
        return self.start_mhz + self.step_mhz * max(0, self.points - 1)

    def covers(self, start: float, end: float) -> bool:
        return start >= self.min_mhz - 0.5 and end <= self.max_mhz + 0.5


class Analyzer:
    """
    A spectrum analyzer that, once tuned, keeps delivering sweeps of that range. A driver
    tunes it in ``_configure``, adds each sweep it gets with ``_add_sweep``, and reports
    being unplugged through ``alive``.
    """

    name = "Spectrum analyzer"
    # A streaming analyzer can be part way through a sweep when it's retuned, so a dwell
    # drops the first sweep after
    first_sweep_partial = True

    def __init__(self, device: str) -> None:
        self.device = device
        self.baud: int | None = None
        self.config: Config | None = None
        self.model = ""
        # Whoever holds this owns the analyzer's range: the live view and survey
        # measurements take turns rather than retuning it under each other.
        self.lock = threading.RLock()
        # Bumped on every retune, so a reader can tell its range was changed underneath it.
        self.generation = 0
        self._sweeps: list[list[float]] = []
        # Ranges this analyzer turned out to need in pieces, and how many, so the whole range
        # isn't tried again and refused on every pass
        self._pieces: dict[tuple[float, float], int] = {}
        self._sweep_lock = threading.Lock()

    # ---------- what a driver provides ----------

    @property
    def alive(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def _configure(self, start_mhz: float, end_mhz: float, timeout: float) -> Config:
        raise NotImplementedError

    @property
    def max_mhz(self) -> float:
        """The highest frequency it can sweep, as far as is known yet."""
        return WIFI_RANGES[3][2]

    @property
    def min_mhz(self) -> float:
        """The lowest frequency it can sweep."""
        return self.config.min_mhz if self.config is not None else 0.0

    @property
    def scan_points(self) -> int:
        """Bins in one sweep, which sets how wide a piece ``scan`` can take at a resolution."""
        return self.config.points if self.config is not None and self.config.points > 1 else 112

    @property
    def wifi_ranges(self) -> tuple[tuple[str, float, float], ...]:
        return wifi_ranges(self.max_mhz)

    @property
    def summary(self) -> str:
        """How it's connected, in a few words for the Setup page."""
        return f"{self.name} on {self.device}"

    # ---------- sweeps ----------

    def _add_sweep(self, trace: list[float]) -> None:
        with self._sweep_lock:
            self._sweeps.append(trace)
            del self._sweeps[:-400]

    def latest(self) -> list[float] | None:
        with self._sweep_lock:
            return list(self._sweeps[-1]) if self._sweeps else None

    def clear_sweeps(self) -> None:
        with self._sweep_lock:
            self._sweeps.clear()

    def _gone(self) -> ConnectionError:
        return ConnectionError(f"The {self.name} on {self.device} stopped answering. "
                               "Is it still plugged in?")

    def configure(self, start_mhz: float, end_mhz: float, timeout: float = 8) -> Config:
        with self.lock:
            self.generation += 1
            return self._configure(start_mhz, end_mhz, timeout)

    def sweep(self, start_mhz: float, end_mhz: float, count: int = 15,
              timeout: float = 30) -> tuple[Config, list[list[float]]]:
        with self.lock:
            return self._sweep(start_mhz, end_mhz, count, timeout)

    def _sweep(self, start_mhz: float, end_mhz: float, count: int,
               timeout: float) -> tuple[Config, list[list[float]]]:
        config = self.configure(start_mhz, end_mhz)
        with self._sweep_lock:
            self._sweeps.clear()
        waiter = threading.Event()
        remaining = timeout
        while remaining > 0:
            with self._sweep_lock:
                have = len(self._sweeps)
            if have >= count + 1 or not self.alive:
                break
            waiter.wait(0.2)
            remaining -= 0.2
        with self._sweep_lock:
            collected = self._sweeps[1:]  # the first after a reconfigure can be partial
        if not collected:
            if not self.alive:
                raise self._gone()
            raise TimeoutError("No sweeps arrived.")
        return config, collected

    def sweep_range(self, start_mhz: float, end_mhz: float, count: int = 15,
                    timeout: float = 30) -> list[tuple[Config, list[list[float]]]]:
        """
        Sweeps a range, in equal pieces when the analyzer won't take it at once: because it's
        wider than its span limit, or because a sweep that wide never arrives.
        Returns one (config, sweeps) per piece, lowest frequency first.
        """
        with self.lock:
            key = (start_mhz, end_mhz)
            pieces = self._pieces.get(key, 1)
            if pieces == 1:
                try:
                    return [self._sweep(start_mhz, end_mhz, count, timeout)]
                except SpanError as error:
                    pieces = max(2, math.ceil((end_mhz - start_mhz) / max(1.0, error.limit_mhz)))
                except TimeoutError:
                    if not self.alive or end_mhz - start_mhz <= 100:
                        raise
                    pieces = 2
                self._pieces[key] = pieces
            width = (end_mhz - start_mhz) / pieces
            return [self._sweep(start_mhz + i * width, start_mhz + (i + 1) * width, count, timeout)
                    for i in range(pieces)]

    def scan(self, start_mhz: float, end_mhz: float, step_khz: float, sweeps: int = 3,
             timeout: float = 30) -> Trace:
        """
        A range at a resolution of about ``step_khz``, however wide: in as many pieces as its
        sweeps need to have bins that fine, each averaged and peak-held over ``sweeps``.
        """
        import numpy as np

        from . import spectrum as spec

        piece = max(0.05, step_khz / 1000 * max(1, self.scan_points - 1))
        count = max(1, math.ceil((end_mhz - start_mhz) / piece - 1e-9))
        width = (end_mhz - start_mhz) / count
        freqs, avg, peak, rbws = [], [], [], []
        with self.lock:
            for number in range(count):
                low, high = start_mhz + number * width, start_mhz + (number + 1) * width
                for config, sweeps_got in self.sweep_range(low, high, count=sweeps, timeout=timeout):
                    data = np.array([s[: min(len(x) for x in sweeps_got)] for s in sweeps_got], dtype=float)
                    freqs.append(config.start_mhz + np.arange(data.shape[1]) * config.step_mhz)
                    avg.append(spec.to_dbm(spec.to_mw(data).mean(axis=0)))
                    peak.append(data.max(axis=0))
                    rbws.append(config.rbw_khz)
        freqs, avg, peak = np.concatenate(freqs), np.concatenate(avg), np.concatenate(peak)
        order = np.argsort(freqs, kind="stable")
        return Trace(freqs[order], avg[order], peak[order], float(max(rbws)))

    def remember_pieces(self, start_mhz: float, end_mhz: float, pieces: int) -> None:
        """Records that this range has to be swept in pieces, so it isn't tried whole again."""
        self._pieces[(start_mhz, end_mhz)] = pieces

    def dwell(self, center_mhz: float, seconds: float, width_mhz: float = 20,
              minimum: int = 2, timeout: float = 20) -> tuple[Config, list[list[float]]]:
        """
        Parks on one channel, which sweeps fast enough to catch most transmissions and measure
        airtime. A little either side of the channel is included so its edges are measured.
        It stays past ``seconds``, up to ``timeout`` more, until ``minimum`` sweeps are in: a
        slow analyzer (the tinySA takes 2 s over a 20 MHz channel) would otherwise come back
        with nothing every time.
        """
        margin = max(2.0, width_mhz * 0.05)
        skip = 1 if self.first_sweep_partial else 0
        with self.lock:
            config = self.configure(center_mhz - width_mhz / 2 - margin,
                                    center_mhz + width_mhz / 2 + margin)
            with self._sweep_lock:
                self._sweeps.clear()
            waiter = threading.Event()
            waiter.wait(seconds)
            deadline = time.monotonic() + timeout
            while True:
                with self._sweep_lock:
                    collected = list(self._sweeps[skip:])
                if len(collected) >= minimum or time.monotonic() >= deadline or not self.alive:
                    break
                waiter.wait(0.1)
        if not collected:
            raise TimeoutError("No sweeps arrived during the dwell.")
        return config, collected


# ---------- several analyzers at once ----------

def label(analyzer) -> str:
    """What an analyzer is called in a survey point and the report: its model, or its name."""
    return getattr(analyzer, "model", "") or analyzer.name


def ranges_for(pool: list) -> tuple[tuple[str, float, float], ...]:
    """The Wi-Fi ranges a set of analyzers can sweep between them: 6 GHz as far as any reaches."""
    return wifi_ranges(max((a.max_mhz for a in pool), default=WIFI_RANGES[3][2]))


def share_ranges(ranges, pool: list) -> list[tuple[object, tuple[str, float, float]]]:
    """
    Which analyzer sweeps which range, so they sweep at once. Ranges only some can reach go
    first; then each goes to the able analyzer with the least spectrum so far, the first in
    ``pool`` on ties. The same set of analyzers always shares the same way, so each range is
    measured by the same analyzer at every point. A range none reaches all of goes, cut to
    what it reaches, to the one that reaches furthest.
    """
    load = {id(a): 0.0 for a in pool}
    order = {id(a): i for i, a in enumerate(pool)}
    shared = []
    able = {name: [a for a in pool if a.max_mhz >= end - 0.5] for name, _s, end in ranges}
    for name, start, end in sorted(ranges, key=lambda r: (len(able[r[0]]), r[1])):
        candidates = able[name]
        if not candidates:
            furthest = max(pool, key=lambda a: a.max_mhz)
            if furthest.max_mhz <= start:
                continue
            candidates, end = [furthest], min(end, furthest.max_mhz)
        chosen = min(candidates, key=lambda a: (load[id(a)], order[id(a)]))
        load[id(chosen)] += end - start
        shared.append((chosen, (name, start, end)))
    return sorted(shared, key=lambda job: job[1][1])


def share_channels(channels: list[tuple[float, float]], pool: list
                   ) -> list[tuple[object, tuple[float, float]]]:
    """Channels (centre, width) to watch closely, shared out like ``share_ranges``."""
    load = {id(a): 0 for a in pool}
    order = {id(a): i for i, a in enumerate(pool)}
    shared = []
    for centre, width in channels:
        candidates = [a for a in pool if a.max_mhz >= centre + width / 2 - 0.5]
        if not candidates:
            continue
        chosen = min(candidates, key=lambda a: (load[id(a)], order[id(a)]))
        load[id(chosen)] += 1
        shared.append((chosen, (centre, width)))
    return shared
