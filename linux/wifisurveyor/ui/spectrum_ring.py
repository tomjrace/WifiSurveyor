"""
A point's spectrum drawn as a ring on the survey map: frequency runs clockwise from the top
through 2.4, 5 and 6 GHz, and level runs from the bottom of the scale at the centre to the
top at the rim. The access points heard there sit on it as dots in their own colours.
"""

from __future__ import annotations

import math

import cairo

from ..core.analysis import access_points
from ..rf import spectrum as spec
from ..rf.analyzer import WIFI_RANGES
from .common import ap_colour
from .spectrum_view import BAND_PANELS

# The ring's level scale. Tighter than the Spectrum page's, so the trace isn't crowded into the
# middle; an access point stronger than the rim sits on it, and its details give the level.
BOTTOM_DBM, TOP_DBM = -100.0, -40.0
RING_SHARE = 0.55          # of the heatmap circle's radius, so the ring zooms with the map
MIN_RADIUS = 14            # screen pixels: smaller than this, the point is drawn plainly
GAP = math.radians(8)      # between the bands
BAND_KEYS = {"2.4 GHz": "2.4", "5 GHz": "5", "6 GHz": "6"}


def _sectors() -> list[dict]:
    """Each band's stretch of the ring, sized as the spectrum page sizes its panels."""
    total = sum(weights[0] for _, _, weights, _ in BAND_PANELS)
    usable = 2 * math.pi - GAP * len(BAND_PANELS)
    angle = -math.pi / 2 + GAP / 2
    sectors = []
    for title, members, weights, span in BAND_PANELS:
        low, high = span or (min(WIFI_RANGES[i][1] for i in members),
                             max(WIFI_RANGES[i][2] for i in members))
        size = usable * weights[0] / total
        swept = sorted((WIFI_RANGES[i][1], WIFI_RANGES[i][2]) for i in members)
        sectors.append({"band": BAND_KEYS.get(title, title), "lo": low, "hi": high,
                        "a0": angle, "a1": angle + size, "swept": swept})
        angle += size + GAP
    return sectors


SECTORS = _sectors()


def angle_of(freq_mhz: float) -> float | None:
    for sector in SECTORS:
        if sector["lo"] <= freq_mhz <= sector["hi"]:
            return sector["a0"] + (freq_mhz - sector["lo"]) / (sector["hi"] - sector["lo"]) \
                * (sector["a1"] - sector["a0"])
    return None


def radius_of(dbm: float, outer: float) -> float:
    clamped = max(BOTTOM_DBM, min(TOP_DBM, dbm))
    return outer * (clamped - BOTTOM_DBM) / (TOP_DBM - BOTTOM_DBM)


def _unswept(sector: dict, reach: float = 0.0) -> list[tuple[float, float]]:
    """
    The parts of a band the analyzer doesn't sweep: not US Wi-Fi, or out of its reach.
    ``reach`` is how far up this point was swept, which a tinySA Ultra+ takes past 6100 MHz.
    """
    swept = list(sector["swept"])
    if swept and reach > swept[-1][1]:
        swept[-1] = (swept[-1][0], min(reach, sector["hi"]))
    gaps, edge = [], sector["lo"]
    for start, end in swept:
        if start > edge + 0.5:
            gaps.append((edge, start))
        edge = max(edge, end)
    if sector["hi"] > edge + 0.5:
        gaps.append((edge, sector["hi"]))
    return gaps


def draw_ring(cr: cairo.Context, cx: float, cy: float, outer: float, point,
              quality: tuple[float, float, float], band_colours: dict) -> list[tuple[float, float, dict]]:
    """
    Draws one point's ring centred on (cx, cy), ``outer`` screen pixels across to its rim, with
    its quality colour as the rim and the centre dot, and returns where each access point's
    dot landed. The trace is power per 20 MHz, like the access points' levels, so each access
    point's dot sits on top of the energy it puts out.
    """
    # No background of its own, so the plan and heatmap show through: the lines are dark
    # enough to read on the light plan
    cr.save()
    segments = (point.spectrum or {}).get("segments", [])
    reach = max((seg["start_mhz"] + seg["step_mhz"] * (len(seg["avg"]) - 1)
                 for seg in segments if seg.get("avg")), default=0.0)
    for sector in SECTORS:
        for start, end in _unswept(sector, reach):
            cr.move_to(cx, cy)
            cr.arc(cx, cy, outer, angle_of(start), angle_of(end))
            cr.close_path()
            cr.set_source_rgba(0.30, 0.33, 0.38, 0.28)
            cr.fill()

    # Level rings every 20 dB, and a spoke either side of each band
    cr.set_line_width(1)
    cr.set_source_rgba(0.10, 0.12, 0.16, 0.22)
    for level in range(int(BOTTOM_DBM) + 20, int(TOP_DBM), 20):
        cr.new_sub_path()
        cr.arc(cx, cy, radius_of(level, outer), 0, 2 * math.pi)
    cr.stroke()
    cr.set_source_rgba(0.10, 0.12, 0.16, 0.40)
    for sector in SECTORS:
        for angle in (sector["a0"], sector["a1"]):
            cr.move_to(cx, cy)
            cr.line_to(cx + outer * math.cos(angle), cy + outer * math.sin(angle))
    cr.stroke()

    # The averaged trace, filled out from the centre like the spectrum page's
    for segment in (point.spectrum or {}).get("segments", []):
        pts = []
        per_20mhz = 10 * math.log10(20000 / max(1.0, segment.get("rbw_khz") or 600))
        for index, level in enumerate(segment.get("avg", [])):
            angle = angle_of(segment["start_mhz"] + index * segment["step_mhz"])
            if angle is not None:
                r = radius_of(level + per_20mhz, outer)
                pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        if len(pts) < 2:
            continue
        cr.move_to(cx, cy)
        for x, y in pts:
            cr.line_to(x, y)
        cr.close_path()
        cr.set_source_rgba(0.95, 0.65, 0.05, 0.40)
        cr.fill()
        cr.move_to(*pts[0])
        for x, y in pts[1:]:
            cr.line_to(x, y)
        cr.set_source_rgba(0.62, 0.38, 0.0, 0.95)
        cr.set_line_width(1)
        cr.stroke()

    # Rim in the point's quality colour, and each band's colour just outside it
    cr.new_path()
    cr.arc(cx, cy, outer, 0, 2 * math.pi)
    cr.set_source_rgb(*quality)
    cr.set_line_width(3)
    cr.stroke()
    cr.set_line_width(2.5)
    for sector in SECTORS:
        cr.new_path()
        cr.arc(cx, cy, outer + 4, sector["a0"], sector["a1"])
        cr.set_source_rgb(*band_colours.get(sector["band"], (0.5, 0.5, 0.5)))
        cr.stroke()

    # Access points: an arc across the whole channel at the level heard, a dot on the primary
    hits = []
    for ap in reversed(access_points(point.networks)):        # strongest drawn last, on top
        angle = angle_of(ap["freq"])
        if angle is None or ap["rssi"] is None:
            continue
        colour = ap_colour(ap["ssid"], ap["freq"])
        r = radius_of(ap["rssi"], outer)
        start = angle_of(ap["center"] - ap["width"] / 2 + 0.5)
        end = angle_of(ap["center"] + ap["width"] / 2 - 0.5)
        if start is not None and end is not None and end > start:
            cr.new_path()
            cr.arc(cx, cy, r, start, end)
            cr.set_source_rgba(*colour, 0.9)
            cr.set_line_width(2)
            cr.stroke()
        x, y = cx + r * math.cos(angle), cy + r * math.sin(angle)
        cr.new_path()
        cr.arc(x, y, max(2.5, min(5.0, outer / 16)), 0, 2 * math.pi)
        if ap["estimate"]:
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(*colour)
            cr.set_line_width(1.5)
            cr.stroke()
        else:
            cr.set_source_rgb(*colour)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.set_line_width(0.75)
            cr.stroke()
        ap["dwell"] = spec.dwell_for(point.spectrum, ap["freq"])
        hits.append((x, y, ap))

    # Channels watched closely: a dashed arc across each at the power measured while something
    # transmitted, the analyzer's counterpart to the dots' RSSI
    cr.set_dash([3, 2])
    cr.set_line_width(2)
    for dwell in (point.spectrum or {}).get("dwells", []):
        if dwell.get("active_dbm") is None:
            continue
        start = angle_of(dwell["center_mhz"] - dwell["width_mhz"] / 2 + 0.5)
        end = angle_of(dwell["center_mhz"] + dwell["width_mhz"] / 2 - 0.5)
        if start is None or end is None or end <= start:
            continue
        cr.new_path()
        cr.arc(cx, cy, radius_of(dwell["active_dbm"], outer), start, end)
        cr.set_source_rgba(0.10, 0.12, 0.16, 0.85)
        cr.stroke()
    cr.set_dash([])

    # The point itself in the middle, as the plain marker draws it
    cr.new_path()
    cr.arc(cx, cy, 5, 0, 2 * math.pi)
    cr.set_source_rgb(*quality)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(1)
    cr.stroke()
    cr.restore()
    return hits


def describe(ap: dict, band_names: dict) -> str:
    """What a tap or hover on an access point's dot says about it."""
    band = band_names.get(ap["band"], ap["band"])
    channel = f"channel {ap['channel']}" if ap.get("channel") else f"{ap['freq']:.0f} MHz"
    width = ap["width"]
    span = (f", {width} MHz wide ({ap['center'] - width / 2:.0f}–{ap['center'] + width / 2:.0f} MHz)"
            if width > 20 else "")
    level = f"{'about ' if ap['estimate'] else ''}{ap['rssi']} dBm"
    return (f"{band}, {channel}{span}\n{level}"
            + _dwell_text(ap.get("dwell"))
            + (f"\n{ap['vendor']}" if ap.get("vendor") else "")
            + (f"\n{ap['bssid']}" if ap["bssid"] else ""))


def _dwell_text(dwell: dict | None) -> str:
    """The analyzer's close look at the channel, when it had one."""
    if not dwell:
        return ""
    parts = []
    if dwell.get("active_dbm") is not None:
        parts.append(f"analyzer {dwell['active_dbm']:.0f} dBm while transmitting")
    if dwell.get("airtime") is not None:
        parts.append(f"channel busy {dwell['airtime'] * 100:.0f}%")
    if dwell.get("noise_20mhz") is not None:
        parts.append(f"noise {dwell['noise_20mhz']:.0f} dBm/20 MHz")
    return ("\n" + ", ".join(parts)) if parts else ""

