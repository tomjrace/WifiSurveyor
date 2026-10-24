"""Maps and places without API keys: OpenStreetMap, USGS aerial imagery, and BeaconDB.

  tiles        OpenStreetMap's standard map (tile.openstreetmap.org, to zoom 19, worldwide),
               or USGS The National Map's aerial imagery (public domain, the US only; the
               cached service goes to zoom 16, and is enlarged beyond that).
  search       an address or place name, through OpenStreetMap's Nominatim, with the
               building's outline when OpenStreetMap has one for it.
  buildings    building outlines near a place, from OpenStreetMap through Overpass.
  wifi_locate  where the access points heard nearby are, from BeaconDB, a community Wi-Fi
               location database (Mozilla's Location Service's successor). This sends their
               MAC addresses and levels to beacondb.net, so it only runs when asked; networks
               marked "_nomap" and randomised MAC addresses are never sent.

None needs a key or an account. Each is a public service with a fair-use policy, which this
keeps to: it says who it is (User-Agent), downloads only what's shown, keeps tiles on disk so
the same map isn't fetched twice, and only asks when someone presses a button. Nothing here
runs in the background. Maps from OpenStreetMap carry its attribution wherever they're shown.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
from pathlib import Path

from ..core import geo
from . import outbound
# key: (name, URL template, the deepest zoom it has, attribution)
SOURCES = {
    "osm": ("Map (OpenStreetMap)", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19,
            "© OpenStreetMap contributors"),
    "usgs": ("Aerial (USGS, US only)",
             "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
             16, "Imagery: USGS The National Map"),
}
MAX_ZOOM = 19
TILE_DAYS = 30                       # how long a tile on disk is used before it's fetched again
MAX_TILES = 48                       # one map is never more than this many tiles
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# The main Overpass server, then public mirrors: any can be slow (half a minute) or down
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter")
BEACONDB = "https://api.beacondb.net/v1/geolocate"


def _request(url: str, data: bytes | None = None, headers: dict | None = None,
             timeout: float = 20) -> bytes:
    """Through net.outbound: a listed host, the app's own identity, nothing personal."""
    return outbound.fetch(url, data, headers, timeout)


def _cache_dir() -> Path:
    from ..core.project import data_dir, make_dir

    return make_dir(data_dir() / "tiles")


def tile(source: str, z: int, x: int, y: int) -> bytes:
    """One tile, from disk when it was fetched in the last TILE_DAYS days."""
    _name, template, _top, _credit = SOURCES[source]
    from ..core.project import hand_back, make_dir

    path = _cache_dir() / source / str(z) / str(x) / f"{y}.img"
    try:
        if time.time() - path.stat().st_mtime < TILE_DAYS * 86400:
            return path.read_bytes()
    except OSError:
        pass
    data = _request(template.format(z=z, x=x, y=y))
    make_dir(path.parent)
    path.write_bytes(data)
    hand_back(path)
    return data


class MapImage:
    """A stitched map: PNG bytes, its zoom, and where its top-left corner is in world pixels."""

    def __init__(self, png: bytes, width: int, height: int, zoom: int, x0: float, y0: float,
                 source: str) -> None:
        self.png, self.width, self.height = png, width, height
        self.zoom, self.x0, self.y0, self.source = zoom, x0, y0, source

    @property
    def credit(self) -> str:
        return SOURCES[self.source][3]

    def to_px(self, lat: float, lon: float) -> tuple[float, float]:
        x, y = geo.world_px(lat, lon, self.zoom)
        return x - self.x0, y - self.y0

    def to_lat_lon(self, x: float, y: float) -> tuple[float, float]:
        return geo.lat_lon(x + self.x0, y + self.y0, self.zoom)

    def meters_per_pixel(self, y: float | None = None) -> float:
        lat, _lon = self.to_lat_lon(self.width / 2, self.height / 2 if y is None else y)
        return geo.meters_per_pixel(lat, self.zoom)


def tiles_needed(lat: float, lon: float, zoom: int, width: int, height: int,
                 native: int) -> tuple[float, float, list[tuple[int, int]], int]:
    """
    The area (top-left in world pixels at ``zoom``) and the tiles at zoom ``native`` (no deeper
    than zoom) that cover a width x height map centred on a place. Tile columns are unwrapped,
    so a map across 180° keeps its order; fetch column x % 2^native.
    """
    cx, cy = geo.world_px(lat, lon, zoom)
    x0, y0 = cx - width / 2, cy - height / 2
    factor = 2 ** (zoom - native)
    span = geo.TILE * factor                       # a native tile's size at the zoom shown
    count = 2 ** native
    xs = range(math.floor(x0 / span), math.floor((x0 + width - 1) / span) + 1)
    ys = range(max(0, math.floor(y0 / span)), min(count - 1, math.floor((y0 + height - 1) / span)) + 1)
    return x0, y0, [(x, y) for y in ys for x in xs], factor


def stitch(source: str, lat: float, lon: float, zoom: int, width: int = 1280,
           height: int = 960, progress=lambda text: None) -> MapImage:
    """A width x height map centred on a place, from tiles (enlarged where the source stops)."""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    zoom = max(1, min(MAX_ZOOM, int(zoom)))
    native = min(zoom, SOURCES[source][2])
    x0, y0, needed, factor = tiles_needed(lat, lon, zoom, width, height, native)
    if len(needed) > MAX_TILES:
        raise ValueError("That map would need too many tiles; make it smaller.")
    span = geo.TILE * factor
    canvas = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    canvas.fill(0xDDDDDDFF)
    count = 2 ** native
    for number, (tx, ty) in enumerate(needed, start=1):
        progress(f"Map tile {number} of {len(needed)}…")
        loader = GdkPixbuf.PixbufLoader()
        try:
            loader.write(tile(source, native, tx % count, ty))
            loader.close()
        except Exception:
            continue                                # a missing tile stays grey
        piece = loader.get_pixbuf()
        if piece is None:
            continue
        left, top = tx * span - x0, ty * span - y0
        scaled = piece if factor == 1 else piece.scale_simple(int(span), int(span),
                                                               GdkPixbuf.InterpType.BILINEAR)
        dest_x, dest_y = max(0, int(left)), max(0, int(top))
        right, bottom = min(width, int(left + span)), min(height, int(top + span))
        if right <= dest_x or bottom <= dest_y:
            continue
        scaled.copy_area(int(dest_x - left), int(dest_y - top), right - dest_x, bottom - dest_y,
                         canvas, dest_x, dest_y)
    ok, png = canvas.save_to_bufferv("png", [], [])
    return MapImage(bytes(png), width, height, zoom, x0, y0, source)


def search(text: str) -> list[tuple[str, float, float, list[tuple[float, float]] | None]]:
    """
    Places matching an address or name, best first: (description, lat, lon, outline), the
    outline being the building's (or site's) as [(lat, lon), …] when OpenStreetMap has one.
    """
    query = urllib.parse.urlencode({"q": text, "format": "jsonv2", "limit": 5,
                                    "polygon_geojson": 1, "polygon_threshold": 0.000005})
    found = json.loads(_request(f"{NOMINATIM}?{query}"))
    return [(item.get("display_name", ""), float(item["lat"]), float(item["lon"]),
             outline_from_geojson(item.get("geojson"))) for item in found]


def outline_from_geojson(shape: dict | None) -> list[tuple[float, float]] | None:
    """The outer ring of a polygon (the largest, for a multipolygon) as [(lat, lon), …]."""
    if not shape:
        return None
    if shape.get("type") == "Polygon":
        rings = [shape["coordinates"][0]] if shape.get("coordinates") else []
    elif shape.get("type") == "MultiPolygon":
        rings = [polygon[0] for polygon in shape.get("coordinates") or [] if polygon]
    else:
        return None
    if not rings:
        return None
    best = max(rings, key=lambda ring: geo.polygon_area_m2([(lat, lon) for lon, lat in ring]))
    points = [(float(lat), float(lon)) for lon, lat in best]
    return points if len(points) >= 4 else None


def buildings(lat: float, lon: float, radius_m: float = 150) -> list[dict]:
    """
    Building outlines near a place: {"id", "name", "points": [(lat, lon), …]}, from
    OpenStreetMap through Overpass.
    """
    query = (f"[out:json][timeout:25];way[\"building\"](around:{int(radius_m)},{lat:.6f},{lon:.6f});"
             "out geom 400;")
    data = urllib.parse.urlencode({"data": query}).encode()
    problems = []
    for server in OVERPASS:
        try:
            return parse_buildings(json.loads(_request(server, data=data, timeout=60)))
        except (OSError, ValueError) as error:
            problems.append(f"{urllib.parse.urlparse(server).hostname}: {error}")
    raise ConnectionError("No Overpass server answered (" + "; ".join(problems) + "). They're "
                          "busy at times; try again in a minute.")


def parse_buildings(answer: dict) -> list[dict]:
    found = []
    for element in answer.get("elements", []):
        points = [(float(p["lat"]), float(p["lon"])) for p in element.get("geometry") or []]
        if len(points) >= 4:
            tags = element.get("tags") or {}
            name = tags.get("name") or " ".join(filter(None, (tags.get("addr:housenumber"),
                                                              tags.get("addr:street"))))
            found.append({"id": element.get("id"), "name": name, "points": points})
    return found


def _randomised(mac: str) -> bool:
    """A locally administered (randomised) MAC: it moves with its device, so it's no landmark."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return True


def wifi_payload(readings) -> dict:
    """What BeaconDB is sent: fixed access points only, never ones that asked not to be mapped."""
    aps = []
    for r in readings:
        if r.ssid and r.ssid.endswith("_nomap"):
            continue
        if _randomised(r.bssid):
            continue
        entry = {"macAddress": r.bssid.lower(), "signalStrength": int(r.rssi)}
        if getattr(r, "freq_mhz", None):
            entry["frequency"] = int(r.freq_mhz)
        aps.append(entry)
    return {"considerIp": False, "wifiAccessPoints": aps}


def wifi_locate(readings) -> tuple[float, float, float]:
    """(lat, lon, accuracy in metres) from the access points heard, through BeaconDB."""
    payload = wifi_payload(readings)
    if len(payload["wifiAccessPoints"]) < 2:
        raise ValueError("It needs at least two fixed access points heard nearby.")
    try:
        answer = json.loads(_request(BEACONDB, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}))
    except outbound.HTTPError as error:
        if error.code == 404:
            raise LookupError("BeaconDB doesn't know these access points yet. Use the GPS or "
                              "search for the address instead.") from error
        raise
    location = answer.get("location") or {}
    return float(location["lat"]), float(location["lng"]), float(answer.get("accuracy") or 0)
