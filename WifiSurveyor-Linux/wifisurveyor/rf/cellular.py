"""Cellular and other non-Wi-Fi bands on the survey map, measured with the HackRF.

What's recorded is downlink power: the strongest 5 MHz anywhere in each US band, and the
band's total. It isn't a phone's RSRP, which needs the signal decoded, but it tracks it: a
fully loaded LTE carrier puts about 25 dB more into 5 MHz than RSRP measures in one resource
element, so -65 dBm per 5 MHz is roughly RSRP -90, which phones treat as good.
"""

from __future__ import annotations

import numpy as np

from . import spectrum as spec

# (key, name, low MHz, high MHz): US downlinks, then two non-cellular bands worth knowing
BANDS = (
    ("600", "600 MHz (band 71)", 617.0, 652.0),
    ("700", "700 MHz (bands 12, 13, 14)", 728.0, 768.0),
    ("850", "850 MHz (band 5)", 869.0, 894.0),
    ("pcs", "PCS 1900 MHz (bands 2, 25)", 1930.0, 1995.0),
    ("aws", "AWS 2100 MHz (bands 4, 66)", 2110.0, 2200.0),
    ("2500", "2.5 GHz (band 41)", 2496.0, 2690.0),
    ("cbrs", "CBRS 3.5 GHz (band 48)", 3550.0, 3700.0),
    ("cband", "C-band 3.7 GHz (n77)", 3700.0, 3980.0),
    ("ism900", "900 MHz ISM (Wi-Fi HaLow, LoRa)", 902.0, 928.0),
    ("dect", "DECT 6.0 cordless phones", 1920.0, 1930.0),
)
CELLULAR = ("600", "700", "850", "pcs", "aws", "2500", "cbrs", "cband")
NAMES = {key: name for key, name, _low, _high in BANDS}
BLOCK_MHZ = 5.0
GOOD_DBM, FAIR_DBM = -65.0, -80.0       # per 5 MHz: roughly RSRP -90 and -105


def sweep_ranges() -> list[tuple[float, float]]:
    """The bands as hackrf_sweep ranges, neighbours merged so there are no more than ten."""
    merged: list[list[float]] = []
    for _key, _name, low, high in sorted(BANDS, key=lambda band: band[2]):
        if merged and low <= merged[-1][1] + 20:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    return [(low, high) for low, high in merged]


def summarize(freqs_mhz: np.ndarray, levels_dbm: np.ndarray) -> dict:
    """
    Per band: the strongest 5 MHz (its power and where), and the whole band's power. Bins are
    each one bin wide, so adding them in milliwatts gives the power across the block.
    """
    result = {}
    freqs = np.asarray(freqs_mhz, dtype=float)
    power = spec.to_mw(levels_dbm)
    for key, _name, low, high in BANDS:
        inside = (freqs >= low) & (freqs <= high)
        if inside.sum() < 4:
            continue
        band_freqs, band_power = freqs[inside], power[inside]
        best, where = None, None
        for start in np.arange(low, high - BLOCK_MHZ + 0.01, 1.0):
            block = (band_freqs >= start) & (band_freqs < start + BLOCK_MHZ)
            if block.any():
                total = float(band_power[block].sum())
                if best is None or total > best:
                    best, where = total, start + BLOCK_MHZ / 2
        result[key] = {
            "peak_dbm": round(float(spec.to_dbm(best)), 1) if best else None,
            "peak_mhz": round(float(where), 1) if where else None,
            "total_dbm": round(float(spec.to_dbm(band_power.sum())), 1),
        }
    return result


def strongest(cellular: dict | None, band: str | None = None) -> float | None:
    """One band's strongest 5 MHz, or the best cellular band's when none is named."""
    if not cellular:
        return None
    keys = [band] if band else list(CELLULAR)
    values = [cellular[key]["peak_dbm"] for key in keys
              if key in cellular and cellular[key].get("peak_dbm") is not None]
    return max(values) if values else None


def quality(level: float | None) -> str:
    if level is None:
        return "none"
    return "good" if level >= GOOD_DBM else "fair" if level >= FAIR_DBM else "poor"
