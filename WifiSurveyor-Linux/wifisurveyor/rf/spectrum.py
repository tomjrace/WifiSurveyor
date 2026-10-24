"""Turning sweeps into the figures the survey uses."""

from __future__ import annotations

import numpy as np

OCCUPANCY_THRESHOLD_DB = 10.0

CHANNELS_24 = {n: 2412 + 5 * (n - 1) for n in range(1, 14)} | {14: 2484}
CHANNELS_5 = {n: 5000 + 5 * n for n in
              [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128,
               132, 136, 140, 144, 149, 153, 157, 161, 165, 169, 173, 177]}
# 20 MHz 6 GHz channels: 1, 5, 9, … centre frequency is 5950 + 5*channel.
# Those numbers also exist on 2.4 GHz, so they stay in their own map.
CHANNELS_6 = {n: 5950 + 5 * n for n in range(1, 234, 4)}
CHANNELS = CHANNELS_24 | CHANNELS_5


def all_channels() -> list[tuple[int, float]]:
    """Every 20 MHz centre. 2.4 and 6 GHz reuse channel numbers, so this is a list."""
    return [*CHANNELS_24.items(), *CHANNELS_5.items(), *CHANNELS_6.items()]


def band_of(freq_mhz: float) -> str:
    return "2.4" if freq_mhz < 3000 else "5" if freq_mhz < 5925 else "6"


def to_mw(dbm):
    return np.power(10.0, np.asarray(dbm, dtype=float) / 10.0)


def to_dbm(mw):
    mw = np.asarray(mw, dtype=float)
    return np.where(mw <= 0, -150.0, 10.0 * np.log10(np.maximum(mw, 1e-15)))


def channel_power(trace, start_mhz: float, step_mhz: float, rbw_khz: float,
                  center_mhz: float, width_mhz: float = 20) -> float | None:
    """Integrates the bins across a channel, correcting for the analyzer's resolution."""
    trace = np.asarray(trace, dtype=float)
    if step_mhz <= 0 or rbw_khz <= 0 or trace.size == 0:
        return None
    freqs = start_mhz + np.arange(trace.size) * step_mhz
    inside = (freqs >= center_mhz - width_mhz / 2) & (freqs <= center_mhz + width_mhz / 2)
    if not inside.any():
        return None
    total = to_mw(trace[inside]).sum() * step_mhz / (rbw_khz / 1000)
    return float(to_dbm(total))


def noise_floor_per_20mhz(min_hold, rbw_khz: float) -> float:
    """
    The tenth percentile of a min-hold trace is the floor in one resolution bandwidth;
    scaling it to 20 MHz makes it comparable with Wi-Fi signal levels.
    """
    floor = float(np.percentile(np.asarray(min_hold, dtype=float), 10))
    return floor + 10 * np.log10(20000 / max(1.0, rbw_khz))


def build_segment(config, sweeps: list[list[float]], noise_floor: float | None = None) -> dict:
    data = np.array([s[: min(len(x) for x in sweeps)] for s in sweeps], dtype=float)
    avg = to_dbm(to_mw(data).mean(axis=0))
    segment = {
        "start_mhz": config.start_mhz,
        "step_mhz": config.step_mhz,
        "rbw_khz": config.rbw_khz,
        "sweeps": len(sweeps),
        "avg": avg.tolist(),
        "max": data.max(axis=0).tolist(),
        "min": data.min(axis=0).tolist(),
        "occupancy": {},
    }

    floor = noise_floor if noise_floor is not None else noise_floor_per_20mhz(segment["min"], config.rbw_khz)
    end_mhz = config.start_mhz + config.step_mhz * (data.shape[1] - 1)
    for number, center in all_channels():
        if center - 10 < config.start_mhz - 0.5 or center + 10 > end_mhz + 0.5:
            continue
        busy = sum(
            1 for sweep in data
            if (channel_power(sweep, config.start_mhz, config.step_mhz, config.rbw_khz, center) or -200)
            > floor + OCCUPANCY_THRESHOLD_DB
        )
        segment["occupancy"][number] = busy / len(data)
    return segment


def band_noise_floor(segments: list[dict], band: str) -> float | None:
    floors = [
        noise_floor_per_20mhz(s["min"], s["rbw_khz"])
        for s in segments if s["min"] and band_of(s["start_mhz"]) == band
    ]
    return min(floors) if floors else None


def summarize_dwell(config, sweeps: list[list[float]], center_mhz: float,
                    width_mhz: float) -> dict:
    """
    A channel watched closely: the usual segment figures, plus what the whole channel does.
    Each sweep's power is added up across the channel's full width. The quietest sweeps give
    the noise floor, and the level of the busy ones is the power while something transmits,
    which is what a Wi-Fi card's RSSI measures. None of it needs anything transmitted.

    The airtime is the share of single measurements (one frequency, one moment) inside the
    channel that stand well above the floor. A whole sweep can't be the unit: the tinySA takes
    two seconds over a channel, so every sweep catches a few beacons somewhere and the channel
    would always look busy. Each point lasts far less than a frame, so it's a fair sample.
    """
    segment = build_segment(config, sweeps)
    powers = [channel_power(sweep, config.start_mhz, config.step_mhz, config.rbw_khz,
                            center_mhz, width_mhz) for sweep in sweeps]
    powers = [p for p in powers if p is not None]
    floor_20 = noise_floor_per_20mhz(segment["min"], config.rbw_khz)
    floor_channel = floor_20 + 10 * np.log10(width_mhz / 20)
    busy = [p for p in powers if p > floor_channel + OCCUPANCY_THRESHOLD_DB]
    data = np.array([s[: min(len(x) for x in sweeps)] for s in sweeps], dtype=float)
    freqs = config.start_mhz + np.arange(data.shape[1]) * config.step_mhz
    # The middle of the channel: Wi-Fi leaves its outer edges empty, which would read as idle
    inside = np.abs(freqs - center_mhz) <= width_mhz * 0.45
    floor_point = floor_20 - 10 * np.log10(20000 / max(1.0, config.rbw_khz))
    samples = data[:, inside]
    segment.update({
        "center_mhz": center_mhz,
        "width_mhz": width_mhz,
        "noise_20mhz": floor_20,
        "airtime": float((samples > floor_point + OCCUPANCY_THRESHOLD_DB).mean()) if samples.size else None,
        # The upper end of the busy sweeps: a sweep often catches only part of a transmission
        "active_dbm": float(np.percentile(busy, 90)) if busy else None,
        "average_dbm": float(to_dbm(to_mw(powers).mean())) if powers else None,
    })
    return segment


def dwell_for(spectrum: dict | None, freq_mhz: float) -> dict | None:
    """The closely watched channel that covers this frequency, if one does."""
    for dwell in (spectrum or {}).get("dwells", []):
        if abs(freq_mhz - dwell["center_mhz"]) < dwell["width_mhz"] / 2:
            return dwell
    return None


def segment_for(spectrum: dict | None, center_mhz: float) -> dict | None:
    if not spectrum:
        return None
    # A channel watched closely beats the wide sweep: finer resolution and many more sweeps
    for dwell in spectrum.get("dwells", []):
        end = dwell["start_mhz"] + dwell["step_mhz"] * (len(dwell["avg"]) - 1)
        if center_mhz - 10 >= dwell["start_mhz"] - 0.5 and center_mhz + 10 <= end + 0.5:
            return dwell
    dwell = spectrum.get("dwell")
    if dwell and abs(spectrum.get("dwell_center_mhz", -1) - center_mhz) < 1:
        return dwell
    for segment in spectrum.get("segments", []):
        end = segment["start_mhz"] + segment["step_mhz"] * (len(segment["avg"]) - 1)
        if center_mhz - 10 >= segment["start_mhz"] - 0.5 and center_mhz + 10 <= end + 0.5:
            return segment
    return None


def dwell_channels(networks: list[dict], count: int, ssid: str | None = None) -> list[tuple[float, float]]:
    """
    Which channels to watch closely at a point, as (centre, width): the chosen network's
    first, then the strongest others. A channel already inside a wider one chosen is skipped.
    """
    spans: dict[tuple[float, float], tuple[bool, float]] = {}
    for network in networks:
        center = float(network.get("center_mhz") or network.get("freq_mhz") or 0)
        width = float(network.get("width_mhz") or 20)
        if not center:
            continue
        key = (center, width)
        rank = (bool(ssid) and network.get("ssid") == ssid, float(network.get("rssi", -100)))
        spans[key] = max(spans.get(key, rank), rank)
    chosen: list[tuple[float, float]] = []
    for (center, width), _rank in sorted(spans.items(), key=lambda item: item[1], reverse=True):
        if len(chosen) >= count:
            break
        inside = any(abs(center - c) + width / 2 <= w / 2 for c, w in chosen)
        if not inside:
            chosen.append((center, width))
    return chosen


def tone_level(config, sweeps: list[list[float]], freq_mhz: float) -> dict:
    """
    A steady carrier's level from a narrow dwell: the strongest bin within 300 kHz of where it
    should be (the transmitter and analyzer can each be a few tens of kHz off), averaged over
    the sweeps. A carrier sits in one resolution bandwidth, so that bin is its power. Under
    6 dB above the floor it isn't counted as heard.
    """
    segment = build_segment(config, sweeps)
    average = np.asarray(segment["avg"], dtype=float)
    freqs = config.start_mhz + np.arange(average.size) * config.step_mhz
    near = np.abs(freqs - freq_mhz) <= max(0.3, config.step_mhz)
    floor = float(np.percentile(segment["min"], 10))
    level = float(average[near].max()) if near.any() else None
    heard = level is not None and level >= floor + 6
    return {"freq_mhz": freq_mhz, "dbm": round(level, 1) if heard else None,
            "floor_dbm": round(floor, 1)}
