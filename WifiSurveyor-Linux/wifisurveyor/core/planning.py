"""
Floor area rather than points: how much of the surveyed floor meets a target, and what it
would look like with access points that don't exist yet ("what if we put one here?").
"""

from __future__ import annotations

import math

import numpy as np

from . import analysis, heatmap

RADIUS_METRES = 8.0             # how far each measurement's colour reaches on the heatmap
DEFAULT_MPP = 0.05              # metres per pixel assumed before the plan has a scale
CELL = 6                        # plan pixels per heatmap cell
# Received level 1 m from a typical enterprise access point, and how fast it falls with
# distance indoors, when the survey hasn't measured enough to fit its own
DEFAULT_MODEL = {"2.4": (-38.0, 3.0), "5": (-45.0, 3.2), "6": (-48.0, 3.3)}
PREDICT_FLOOR = -92.0           # predictions below this aren't coverage


def heat_radius(project) -> float:
    """How far each point's colour reaches on the heatmap, in plan pixels."""
    if project.meters_per_pixel:
        return RADIUS_METRES / project.meters_per_pixel
    return max(project.width, project.height) * 0.12


def _samples(project, ctx, key: str, ssid, band) -> list[tuple[float, float, float]]:
    found = []
    for point in project.points:
        value = analysis.metric(point, key, ctx, ssid=ssid, band=band)
        if value is not None:
            found.append((point.x, point.y, float(value)))
    return found


def area_coverage(project, ctx, key: str, threshold: float, ssid=None, band="All",
                  higher_is_better: bool = True) -> dict | None:
    """
    The share of the surveyed floor (within reach of a measurement) meeting ``threshold``,
    from the same interpolation the heatmap draws. Also the areas in square feet, once the
    plan has a scale.
    """
    samples = _samples(project, ctx, key, ssid, band)
    if not samples:
        return None
    radius = heat_radius(project)
    value, nearest, reached = heatmap.grid(project.width, project.height, samples, radius, CELL)
    surveyed = (nearest <= radius) & reached
    if not surveyed.any():
        return None
    good = (value >= threshold) if higher_is_better else (value <= threshold)
    meeting = surveyed & np.nan_to_num(good, nan=False).astype(bool)
    cell_sqft = (project.feet(CELL) or 0) ** 2
    return {"share": float(meeting.sum() / surveyed.sum()),
            "surveyed_sqft": float(surveyed.sum() * cell_sqft) if cell_sqft else None,
            "meeting_sqft": float(meeting.sum() * cell_sqft) if cell_sqft else None,
            "threshold": threshold}


# ---------- what if ----------

def model(project, band: str, sites: list[dict] | None = None) -> tuple[float, float, str]:
    """
    Received level at 1 m and fall-off exponent for ``band``: the median of the access points
    this survey could fit (so the building's own walls and clutter are in it), or a typical
    one. Also says which it is.
    """
    fits = [fit for site in sites or [] for fit in site.get("fits", [])
            if fit.get("band") == band and not fit.get("rough")]
    if len(fits) >= 1 and project.meters_per_pixel:
        p0 = float(np.median([f["p0"] for f in fits]))
        exponent = float(np.median([f["exponent"] for f in fits]))
        return p0, exponent, f"fitted from {len(fits)} measured access point{'s' if len(fits) != 1 else ''}"
    p0, exponent = DEFAULT_MODEL.get(band, DEFAULT_MODEL["5"])
    return p0, exponent, "a typical access point (not enough measured to fit this building)"


def virtual_band(ap: dict, band: str) -> str:
    """The band to predict a planned access point on: the chosen one, else 5 GHz if it has it."""
    bands = ap.get("bands") or ["2.4", "5"]
    if band != "All":
        return band
    return "5" if "5" in bands else bands[0]


def _wall_loss_grid(walls, ax: float, ay: float, cx: np.ndarray, cy: np.ndarray) -> np.ndarray:
    """Loss through every wall between one transmitter and each cell centre."""
    total = np.zeros(cx.shape)
    for wall in walls:
        wx, wy = wall.x2 - wall.x1, wall.y2 - wall.y1
        d1 = wx * (ay - wall.y1) - wy * (ax - wall.x1)             # which side the AP is on
        d2 = wx * (cy - wall.y1) - wy * (cx - wall.x1)             # which side each cell is on
        d3 = (cx - ax) * (wall.y1 - ay) - (cy - ay) * (wall.x1 - ax)
        d4 = (cx - ax) * (wall.y2 - ay) - (cy - ay) * (wall.x2 - ax)
        crosses = ((d1 > 0) != (d2 > 0)) & ((d3 > 0) != (d4 > 0))
        total += np.where(crosses, wall.db, 0.0)
    return total


def predict(project, ctx, ssid=None, band="All", sites=None, cell: int = CELL):
    """
    The best signal at each cell: measured (interpolated, where measured) or from a planned
    access point, whichever is stronger, as a client would pick. Returns the grid, the
    measured-only grid, which cells were surveyed, and a note on the model used.
    """
    gw, gh = max(1, project.width // cell), max(1, project.height // cell)
    px = (np.arange(gw) + 0.5) * cell
    py = (np.arange(gh) + 0.5) * cell
    cx, cy = np.meshgrid(px, py)
    radius = heat_radius(project)
    measured = np.full((gh, gw), -np.inf)
    surveyed = np.zeros((gh, gw), dtype=bool)
    samples = _samples(project, ctx, "signal", ssid, band)
    if samples:
        value, nearest, reached = heatmap.grid(project.width, project.height, samples, radius, cell)
        surveyed = (nearest <= radius) & reached
        measured = np.where(surveyed, np.nan_to_num(value, nan=-np.inf), -np.inf)
    best = measured.copy()
    mpp = project.meters_per_pixel or DEFAULT_MPP
    notes = set()
    for ap in project.virtual_aps:
        each = virtual_band(ap, band)
        p0, exponent, how = model(project, each, sites)
        notes.add(f"{each} GHz: {how}")
        metres = np.maximum(np.hypot(cx - ap["x"], cy - ap["y"]) * mpp, 0.5)
        level = p0 - 10 * exponent * np.log10(metres) - _wall_loss_grid(project.walls, ap["x"],
                                                                          ap["y"], cx, cy)
        best = np.maximum(best, np.where(level >= PREDICT_FLOOR, level, -np.inf))
    return best, measured, surveyed, "; ".join(sorted(notes))


def render_predicted(project, ctx, ssid=None, band="All", sites=None, alpha: float = 0.6):
    """The predicted map as an overlay, coloured on the signal map's scale."""
    if not project.virtual_aps:
        return None
    best, _measured, surveyed, _note = predict(project, ctx, ssid, band, sites)
    _title, _unit, low, high, _better = analysis.METRICS["signal"]
    # Beyond the surveyed floor, only where a planned access point would come close to the
    # target: elsewhere it would just paint the whole plan (and the car park) weak
    shown = np.isfinite(best) & (surveyed | (best >= ctx.profile.primary - 8))
    rgb = heatmap.colour(np.nan_to_num((best - low) / (high - low), neginf=0))
    return heatmap._finish(rgb, np.where(shown, alpha, 0.0), CELL)


def planned_gain(project, ctx, ssid=None, band="All", sites=None) -> dict | None:
    """How much of the floor meets the target now, and with the planned access points."""
    if not project.virtual_aps:
        return None
    best, measured, surveyed, note = predict(project, ctx, ssid, band, sites)
    target = ctx.profile.primary
    if not surveyed.any():
        return None
    now = float(((measured >= target) & surveyed).sum() / surveyed.sum())
    after = float(((best >= target) & surveyed).sum() / surveyed.sum())
    beyond = int(((best >= target) & ~surveyed).sum())
    cell_sqft = (project.feet(CELL) or 0) ** 2
    return {"now": now, "after": after, "target": target, "model": note,
            "beyond_sqft": beyond * cell_sqft if cell_sqft else None, "beyond_cells": beyond}


def fit_at(project, samples: list[tuple[float, float, float]], x: float, y: float) -> dict | None:
    """
    The path loss that best explains ``samples`` from a transmitter known to be at (x, y):
    level at 1 m and exponent, with the walls in between counted.
    """
    if len(samples) < 2:
        return None
    mpp = project.meters_per_pixel or DEFAULT_MPP
    levels = np.array([s[2] for s in samples])
    distance = np.array([max(math.hypot(s[0] - x, s[1] - y) * mpp, 0.5) for s in samples])
    walls = np.array([analysis.wall_loss(project, x, y, s[0], s[1]) for s in samples])
    best = None
    for exponent in np.arange(1.6, 4.51, 0.1):
        predicted = exponent * 10 * np.log10(distance) + walls
        p0 = float((levels + predicted).mean())
        rms = float(np.sqrt(((levels - (p0 - predicted)) ** 2).mean()))
        if best is None or rms < best["rms"]:
            best = {"p0": p0, "exponent": float(exponent), "rms": rms}
    return best
