"""Heatmap rendering: inverse distance weighting in NumPy, straight onto a Cairo surface."""

from __future__ import annotations

import cairo
import numpy as np

# poor to good
RAMP = np.array([
    [0.00, 220, 38, 38],
    [0.30, 249, 115, 22],
    [0.55, 250, 204, 21],
    [0.75, 120, 205, 40],
    [1.00, 34, 170, 70],
])


def colour(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0, 1)
    channels = [np.interp(t, RAMP[:, 0], RAMP[:, i]) for i in (1, 2, 3)]
    return np.stack(channels, axis=-1)


# Cells times samples held in memory at once. Each is a handful of float64s, so this keeps
# a render near 100 MB whatever the plan size or point count.
CHUNK_CELLS = 2_000_000


def _rows_per_chunk(gw: int, samples: int) -> int:
    return max(1, CHUNK_CELLS // max(1, gw * samples))


def grid(width: int, height: int, samples: list[tuple[float, float, float]],
         radius_px: float, cell: int = 6) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    One weighted average per cell, vectorised over every sample at once and done in bands of
    rows so memory stays bounded. Returns the values, the distance to the nearest sample, and
    whether any sample reaches each cell at all.
    """
    gw, gh = max(1, width // cell), max(1, height // cell)
    sample = np.array(samples, dtype=float)
    value = np.empty((gh, gw))
    nearest = np.empty((gh, gw))
    total = np.empty((gh, gw))
    px = (np.arange(gw) + 0.5) * cell
    cutoff = (3 * radius_px) ** 2

    step = _rows_per_chunk(gw, len(sample))
    for top in range(0, gh, step):
        rows = slice(top, min(gh, top + step))
        py = (np.arange(rows.start, rows.stop) + 0.5) * cell
        dx = px[None, :, None] - sample[:, 0]
        dy = py[:, None, None] - sample[:, 1]
        distance2 = np.maximum(dx * dx + dy * dy, 1e-6)

        weight = 1.0 / distance2                  # inverse distance, power two
        weight[distance2 > cutoff] = 0.0
        band_total = weight.sum(axis=-1)
        with np.errstate(invalid="ignore", divide="ignore"):
            value[rows] = (weight * sample[:, 2]).sum(axis=-1) / band_total
        total[rows] = band_total
        nearest[rows] = np.sqrt(distance2.min(axis=-1))
    return value, nearest, total > 0


def render(width: int, height: int, samples: list[tuple[float, float, float]],
           low: float, high: float, higher_is_better: bool, radius_px: float,
           cell: int = 6, alpha: float = 0.6) -> cairo.ImageSurface | None:
    """Cells with no measurement within reach stay transparent rather than inventing coverage."""
    if not samples:
        return None
    value, nearest, reached = grid(width, height, samples, radius_px, cell)
    covered = (nearest <= radius_px) & reached

    span = (high - low) or 1.0
    t = (value - low) / span
    if not higher_is_better:
        t = 1 - t
    rgb = colour(np.nan_to_num(t))

    edge = np.clip(1 - (nearest / radius_px - 0.75) / 0.25, 0, 1)
    a = np.where(covered, alpha * edge, 0.0)
    return _finish(rgb, a, cell)


# worse, unchanged, better
CHANGE_RAMP = np.array([
    [0.00, 220, 38, 38],
    [0.40, 250, 180, 150],
    [0.50, 235, 235, 235],
    [0.60, 170, 220, 150],
    [1.00, 21, 128, 61],
])


def difference(width: int, height: int, now: list[tuple[float, float, float]],
               before: list[tuple[float, float, float]], full_scale: float,
               higher_is_better: bool, radius_px: float, cell: int = 6,
               alpha: float = 0.65) -> tuple[cairo.ImageSurface | None, dict]:
    """
    How much better or worse each spot got, where both surveys measured near it. Green is
    better, red worse, by ``full_scale`` or more. Also returns the share of that area that
    improved or got worse by more than a tenth of ``full_scale``, and the average change.
    """
    stats = {"better": 0.0, "worse": 0.0, "average": None, "cells": 0}
    if not now or not before:
        return None, stats
    value_now, near_now, reach_now = grid(width, height, now, radius_px, cell)
    value_before, near_before, reach_before = grid(width, height, before, radius_px, cell)
    both = (near_now <= radius_px) & (near_before <= radius_px) & reach_now & reach_before
    change = value_now - value_before
    if not higher_is_better:
        change = -change
    if both.any():
        moved = change[both]
        threshold = full_scale / 10
        stats = {"better": float((moved > threshold).mean()), "worse": float((moved < -threshold).mean()),
                 "average": float(moved.mean()), "cells": int(both.sum())}
    t = np.clip(0.5 + np.nan_to_num(change) / (2 * full_scale), 0, 1)
    channels = [np.interp(t, CHANGE_RAMP[:, 0], CHANGE_RAMP[:, i]) for i in (1, 2, 3)]
    rgb = np.stack(channels, axis=-1)
    edge = np.clip(1 - (np.maximum(near_now, near_before) / radius_px - 0.75) / 0.25, 0, 1)
    a = np.where(both, alpha * edge, 0.0)
    return _finish(rgb, a, cell), stats


def _finish(rgb: np.ndarray, a: np.ndarray, cell: int) -> cairo.ImageSurface:
    gh, gw = a.shape
    # Cairo wants premultiplied BGRA
    argb = np.zeros((gh, gw, 4), dtype=np.uint8)
    argb[..., 0] = np.clip(rgb[..., 2] * a, 0, 255)
    argb[..., 1] = np.clip(rgb[..., 1] * a, 0, 255)
    argb[..., 2] = np.clip(rgb[..., 0] * a, 0, 255)
    argb[..., 3] = np.clip(a * 255, 0, 255)

    surface = _surface(argb)
    surface.set_device_scale(1 / cell, 1 / cell)
    return surface


# ImageSurface can't grow a Python attribute, and Cairo will not keep the
# caller's buffer alive. The newest surfaces stay referenced here with their bytes.
_LIVE_SURFACES: list[tuple[cairo.ImageSurface, bytearray]] = []


def _surface(argb: np.ndarray) -> cairo.ImageSurface:
    height, width = argb.shape[:2]
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, width)
    padded = np.zeros((height, stride // 4, 4), dtype=np.uint8)
    padded[:, :width] = argb
    buffer = bytearray(padded.tobytes())
    surface = cairo.ImageSurface.create_for_data(buffer, cairo.FORMAT_ARGB32, width, height, stride)
    _LIVE_SURFACES.append((surface, buffer))
    del _LIVE_SURFACES[:-8]
    return surface


def categorical(width: int, height: int, samples: list[tuple[float, float, float]],
                colours: list[tuple[float, float, float]], radius_px: float,
                cell: int = 6, alpha: float = 0.6) -> cairo.ImageSurface | None:
    """Nearest measurement wins, because averaging two different verdicts is meaningless."""
    if not samples:
        return None

    gw, gh = max(1, width // cell), max(1, height // cell)
    sample = np.array(samples, dtype=float)
    index = np.empty((gh, gw), dtype=int)
    nearest = np.empty((gh, gw))
    px = (np.arange(gw) + 0.5) * cell

    step = _rows_per_chunk(gw, len(sample))
    for top in range(0, gh, step):
        rows = slice(top, min(gh, top + step))
        py = (np.arange(rows.start, rows.stop) + 0.5) * cell
        distance2 = (px[None, :, None] - sample[:, 0]) ** 2 + (py[:, None, None] - sample[:, 1]) ** 2
        band_index = np.argmin(distance2, axis=-1)
        index[rows] = band_index
        nearest[rows] = np.sqrt(np.take_along_axis(distance2, band_index[..., None], axis=-1)[..., 0])

    palette = np.array(colours) * 255
    rgb = palette[np.clip(sample[index, 2].astype(int), 0, len(colours) - 1)]
    a = np.where(nearest <= radius_px, alpha, 0.0)
    return _finish(rgb, a, cell)
