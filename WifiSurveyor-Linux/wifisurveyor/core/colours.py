"""
Colours shared by the map, the spectrum page and the report: one per network name, so every
"AccessPoint1" looks the same wherever it appears, and a red/amber/green scale for quality.
"""

from __future__ import annotations

import colorsys

import numpy as np

# Waterfalls, wherever they're drawn: quiet to strong, dark blue, blue, cyan, yellow, red
WATERFALL_LOW, WATERFALL_HIGH = -110.0, -40.0
WATERFALL_STOPS = np.array([0.0, 0.3, 0.55, 0.78, 1.0])
WATERFALL_COLOURS = np.array([[12, 16, 40], [25, 70, 170], [30, 190, 210], [250, 215, 45],
                              [235, 55, 40]], dtype=float)

# Bright enough for the dark spectrum plots and chosen to stay clear of the trace's yellow and
# the peak's red. Past the end of the list, further colours are spread round the hue circle.
SSID_COLOURS = [
    (0.35, 0.80, 0.95), (0.45, 0.90, 0.45), (0.95, 0.45, 0.85), (1.00, 0.60, 0.20),
    (0.55, 0.60, 1.00), (0.75, 0.95, 0.30), (0.70, 0.50, 1.00), (0.30, 0.95, 0.80),
    (1.00, 0.75, 0.55), (0.60, 0.85, 1.00), (0.95, 0.35, 0.60), (0.85, 0.85, 0.95),
]
HIDDEN_COLOUR = (0.72, 0.74, 0.78)
_index: dict[str, int] = {}

GOOD = (0.09, 0.64, 0.29)
FAIR = (0.92, 0.70, 0.03)
POOR = (0.86, 0.15, 0.15)


def ssid_colour(ssid: str | None) -> tuple[float, float, float]:
    """One colour per network name, handed out in the order names are first seen."""
    if not ssid:
        return HIDDEN_COLOUR
    if ssid not in _index:
        _index[ssid] = len(_index)
    index = _index[ssid]
    if index < len(SSID_COLOURS):
        return SSID_COLOURS[index]
    hue = (index * 0.618034) % 1.0              # golden angle: neighbours land far apart
    return colorsys.hsv_to_rgb(hue, 0.55, 0.97)


def signal_quality(rssi: float | None, target: float = -70) -> str:
    """"good", "fair" or "poor" for a signal level against the requirement's target."""
    if rssi is None:
        return "poor"
    if rssi >= target + 3:
        return "good"
    if rssi >= target - 5:
        return "fair"
    return "poor"


def snr_quality(snr: float | None, target: float = 20) -> str:
    """"good", "fair" or "poor" for a signal-to-noise ratio: 5 dB of margin over the target is
    good, as in the survey's advice; just meeting it is fair."""
    if snr is None:
        return "poor"
    if snr >= target + 5:
        return "good"
    if snr >= target:
        return "fair"
    return "poor"


def quality_colour(quality: str) -> tuple[float, float, float]:
    return {"good": GOOD, "fair": FAIR}.get(quality, POOR)


def hex_colour(rgb) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02X}" for c in rgb)
