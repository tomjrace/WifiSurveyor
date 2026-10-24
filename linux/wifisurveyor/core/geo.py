"""Map maths for placing a floor plan on the ground: Web Mercator pixels, scale, buildings.

Web map tiles (OpenStreetMap's and the rest) are 256-pixel squares of the Web Mercator
projection. At zoom z the world is 256 * 2^z pixels across, and a pixel covers
cos(latitude) * 40,075 km / (256 * 2^z) on the ground. That is the whole trick: once the
map's latitude and zoom are known, so is its scale, and a blueprint laid over it to match the
building takes the map's scale with it.
"""

from __future__ import annotations

import math

EARTH_CIRCUMFERENCE_M = 40_075_016.686
TILE = 256
FEET_PER_M = 3.28084
MAX_LAT = 85.05112878


def world_px(lat: float, lon: float, zoom: float) -> tuple[float, float]:
    """Where a place is on the whole-world map at a zoom, in pixels from the top left."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    size = TILE * 2 ** zoom
    x = (lon + 180.0) / 360.0 * size
    sin = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)) * size
    return x, y


def lat_lon(x: float, y: float, zoom: float) -> tuple[float, float]:
    size = TILE * 2 ** zoom
    lon = x / size * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size))))
    return lat, lon


def meters_per_pixel(lat: float, zoom: float) -> float:
    return math.cos(math.radians(lat)) * EARTH_CIRCUMFERENCE_M / (TILE * 2 ** zoom)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def describe_scale(mpp: float) -> str:
    return f"{mpp * FEET_PER_M:.3f} ft per pixel ({1 / (mpp * FEET_PER_M):.1f} pixels per foot)"


def _hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def building_frame(points: list[tuple[float, float]]) -> tuple[float, float, float, float, float] | None:
    """
    The smallest rectangle round a building's outline (pixel points): its centre, its length
    and width (length the longer), and the angle of its length in degrees, clockwise from
    pointing right on the map (y grows downwards). Most buildings are rectangles, or nearly,
    so this is how they're oriented.
    """
    hull = _hull(points)
    if len(hull) < 3:
        return None
    best = None
    for i in range(len(hull)):
        (x1, y1), (x2, y2) = hull[i], hull[(i + 1) % len(hull)]
        angle = math.atan2(y2 - y1, x2 - x1)
        c, s = math.cos(angle), math.sin(angle)
        along = [x * c + y * s for x, y in hull]
        across = [-x * s + y * c for x, y in hull]
        area = (max(along) - min(along)) * (max(across) - min(across))
        if best is None or area < best[0]:
            best = (area, angle, min(along), max(along), min(across), max(across))
    _area, angle, a0, a1, b0, b1 = best
    c, s = math.cos(angle), math.sin(angle)
    ma, mb = (a0 + a1) / 2, (b0 + b1) / 2
    cx, cy = ma * c - mb * s, ma * s + mb * c
    length, width = a1 - a0, b1 - b0
    degrees = math.degrees(angle)
    if width > length:
        length, width, degrees = width, length, degrees + 90
    degrees = (degrees + 90) % 180 - 90           # a rectangle looks the same turned half round
    return cx, cy, length, width, degrees


def fit_into(image_w: float, image_h: float,
             frame: tuple[float, float, float, float, float]) -> tuple[float, float, float, float]:
    """
    How to lay an image over a building frame: (centre x, centre y, scale, rotation degrees),
    its longer side along the building's length, sized so the longer side spans it.
    """
    cx, cy, length, _width, degrees = frame
    if image_h > image_w:
        degrees += 90
    return cx, cy, length / max(image_w, image_h), degrees


def polygon_area_m2(latlons: list[tuple[float, float]]) -> float:
    """A small outline's area, from a local flat projection around its first corner."""
    if len(latlons) < 3:
        return 0.0
    lat0 = latlons[0][0]
    k = math.cos(math.radians(lat0)) * 111_320.0
    pts = [((lon - latlons[0][1]) * k, (lat - lat0) * 110_574.0) for lat, lon in latlons]
    total = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
    return abs(total) / 2
