"""
Long-term monitoring: the Spectrum page's sweeps and access point scans written to a file as
they happen, and what hours of them add up to.

A recording is JSON lines. A sweep line carries the range and its trace(s); a scan line
carries the access points heard. Levels are rounded to half a dB, which is finer than the
analyzer's accuracy and keeps a day's recording to tens of megabytes.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np

from ..rf import spectrum as spec

BUSY_ABOVE_FLOOR_DB = 10.0


def folder() -> Path:
    from .project import data_dir, make_dir

    return make_dir(data_dir() / "recordings")


class Recorder:
    """Appends to one recording file; safe to call from the sweep and scan threads at once."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else folder() / time.strftime("spectrum %Y-%m-%d %H.%M.%S.jsonl")
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        from .project import hand_back

        hand_back(self.path)
        self.started = time.time()
        self.lines = 0

    def sweep(self, range_index: int, parts: list[tuple]) -> None:
        """``parts``: (start MHz, step MHz, resolution bandwidth kHz, trace) for each piece."""
        self._write({"t": round(time.time(), 2), "range": range_index,
                     "parts": [{"start": start, "step": step, "rbw": rbw,
                                "trace": [round(v * 2) / 2 for v in trace]}
                               for start, step, rbw, trace in parts]})

    def scan(self, readings: list) -> None:
        self._write({"t": round(time.time(), 2), "aps": [
            {"ssid": r.ssid, "bssid": r.bssid, "freq": r.freq_mhz, "rssi": r.rssi,
             "center": r.span_center_mhz, "width": r.width_mhz, "vendor": r.vendor}
            for r in readings]})

    def _write(self, record: dict) -> None:
        with self._lock:
            if self._file.closed:
                return
            self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
            self._file.flush()
            self.lines += 1

    def close(self) -> None:
        with self._lock:
            self._file.close()

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def read(path: Path) -> tuple[list[dict], list[dict]]:
    sweeps, scans = [], []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue                      # a line cut short when the app stopped
            if "range" in record:
                sweeps.append(record)
            elif "aps" in record:
                scans.append(record)
    return sweeps, scans


def summarize(path: Path, rows: int = 400) -> dict:
    """
    What a recording shows: its span, a waterfall of each range squeezed into ``rows`` time
    slices (the strongest level in each slice, so short bursts aren't averaged away), how often
    each 20 MHz channel was busy and in how many separate bursts, and how reliably each access
    point was heard.
    """
    sweeps, scans = read(path)
    times = [r["t"] for r in sweeps] + [r["t"] for r in scans]
    if not times:
        return {"start": None, "end": None, "ranges": {}, "channels": [], "aps": []}
    start, end = min(times), max(times)
    length = max(end - start, 1e-6)

    # Waterfalls: each range's traces on a common frequency grid, binned by time
    ranges: dict[int, dict] = {}
    for record in sweeps:
        entry = ranges.setdefault(record["range"], {"records": []})
        entry["records"].append(record)
    for index, entry in ranges.items():
        grid_start = min(p["start"] for r in entry["records"] for p in r["parts"])
        grid_end = max(p["start"] + p["step"] * (len(p["trace"]) - 1)
                       for r in entry["records"] for p in r["parts"])
        freqs = np.linspace(grid_start, grid_end, 300)
        image = np.full((rows, len(freqs)), np.nan)
        for record in entry["records"]:
            row = min(rows - 1, int((record["t"] - start) / length * rows))
            f, v = _flatten(record)
            levels = np.interp(freqs, f, v, left=np.nan, right=np.nan)
            image[row] = np.fmax(image[row], levels)
        entry.update(start_mhz=grid_start, end_mhz=grid_end, image=image)

    # Channels: power per 20 MHz channel each time its range was swept
    channels = []
    for number, centre in spec.all_channels():
        powers, stamps = [], []
        for record in sweeps:
            f, v = _flatten(record)
            if not f or centre - 10 < f[0] - 0.5 or centre + 10 > f[-1] + 0.5:
                continue
            step = (f[-1] - f[0]) / max(1, len(f) - 1)
            rbw = record["parts"][0].get("rbw") or 600
            power = spec.channel_power(v, f[0], step, rbw, centre)
            if power is not None:
                powers.append(power)
                stamps.append(record["t"])
        if len(powers) < 3:
            continue
        levels = np.array(powers)
        floor = float(np.percentile(levels, 10))
        busy = levels > floor + BUSY_ABOVE_FLOOR_DB
        bursts = int(busy[0]) + int(np.sum(busy[1:] & ~busy[:-1]))
        hours = [time.localtime(t).tm_hour for t, b in zip(stamps, busy) if b]
        channels.append({
            "channel": number, "center_mhz": centre, "band": spec.band_of(centre),
            "busy": float(busy.mean()), "peak_dbm": float(levels.max()), "floor_dbm": floor,
            "bursts": bursts, "samples": len(levels),
            "busiest_hour": max(set(hours), key=hours.count) if hours else None,
        })

    # Access points: how often each was heard, and how steady its signal was
    heard: dict[str, dict] = {}
    for record in scans:
        for ap in record["aps"]:
            entry = heard.setdefault(ap["bssid"], {**ap, "levels": [], "last": record["t"]})
            entry["levels"].append(ap["rssi"])
            entry["last"] = record["t"]
    aps = []
    for entry in heard.values():
        levels = entry.pop("levels")
        aps.append({**entry, "heard": len(levels) / max(1, len(scans)), "min": min(levels),
                    "avg": float(np.mean(levels)), "max": max(levels)})
    aps.sort(key=lambda ap: (-ap["heard"], -ap["avg"]))

    return {"start": start, "end": end, "sweeps": len(sweeps), "scans": len(scans),
            "ranges": ranges, "channels": channels, "aps": aps}


def _flatten(record: dict) -> tuple[list[float], list[float]]:
    freqs, levels = [], []
    for part in record["parts"]:
        freqs.extend(part["start"] + i * part["step"] for i in range(len(part["trace"])))
        levels.extend(part["trace"])
    return freqs, levels
