"""Placing a plan on a map: the projection, building outlines, and the keyless services."""

from __future__ import annotations

import io
import json
import math
import os
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

from wifisurveyor.core import geo
from wifisurveyor.core.project import Project
from wifisurveyor.net import maps


class ProjectionTest(unittest.TestCase):
    def test_round_trip_and_known_scale(self):
        for lat, lon in ((40.7484, -73.9857), (-33.8568, 151.2153), (0.0, 0.0), (64.1, -21.9)):
            x, y = geo.world_px(lat, lon, 18)
            back = geo.lat_lon(x, y, 18)
            self.assertAlmostEqual(back[0], lat, places=7)
            self.assertAlmostEqual(back[1], lon, places=7)
        # The textbook figure: 156,543 m per pixel at the equator at zoom 0
        self.assertAlmostEqual(geo.meters_per_pixel(0, 0), 156543.03, places=1)
        self.assertAlmostEqual(geo.meters_per_pixel(60, 1), 156543.03 / 4, places=1)

    def test_map_pixels_agree_with_ground_distance(self):
        lat, lon = 40.7484, -73.9857
        x, y = geo.world_px(lat, lon, 18)
        east = geo.lat_lon(x + 100, y, 18)
        metres = geo.distance_m(lat, lon, *east)
        self.assertAlmostEqual(metres, 100 * geo.meters_per_pixel(lat, 18), delta=0.5)

    def test_building_frame_finds_a_turned_rectangle(self):
        # 120 x 50 px, turned 30° clockwise on the map, centred at (400, 300)
        angle = math.radians(30)
        corners = []
        for u, v in ((-60, -25), (60, -25), (60, 25), (-60, 25)):
            corners.append((400 + u * math.cos(angle) - v * math.sin(angle),
                            300 + u * math.sin(angle) + v * math.cos(angle)))
        cx, cy, length, width, degrees = geo.building_frame(corners + [corners[0]])
        self.assertAlmostEqual(cx, 400, places=6)
        self.assertAlmostEqual(cy, 300, places=6)
        self.assertAlmostEqual(length, 120, places=6)
        self.assertAlmostEqual(width, 50, places=6)
        self.assertAlmostEqual(degrees, 30, places=6)
        # A portrait blueprint 1000 x 2400 px fitted to it: turned a further 90°, 120/2400 the size
        _x, _y, scale, turn = geo.fit_into(1000, 2400, (cx, cy, length, width, degrees))
        self.assertAlmostEqual(scale, 0.05)
        self.assertAlmostEqual(turn, 120)

    def test_area(self):
        # A 0.001° square at the equator is about 111 m a side
        square = [(0, 0), (0, 0.001), (0.001, 0.001), (0.001, 0)]
        self.assertAlmostEqual(geo.polygon_area_m2(square), 111.32 * 110.57, delta=50)


class MapsTest(unittest.TestCase):
    def test_tiles_cover_the_map_and_enlarge_past_the_source(self):
        x0, y0, tiles, factor = maps.tiles_needed(40.7484, -73.9857, 18, 1024, 768, 18)
        self.assertEqual(factor, 1)
        self.assertIn(len(tiles), (20, 25))                 # 4-5 columns, 4-5 rows
        _x, _y, coarse, factor = maps.tiles_needed(40.7484, -73.9857, 18, 1024, 768, 16)
        self.assertEqual(factor, 4)
        self.assertLessEqual(len(coarse), 4)

    def test_stitched_tiles_land_where_they_belong(self):
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        def fake_tile(_source, z, x, y):
            # Each tile a flat colour from its own column and row
            pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 256, 256)
            pixbuf.fill(((x % 256) << 24) | ((y % 256) << 16) | 0x00FF)
            return bytes(pixbuf.save_to_bufferv("png", [], [])[1])

        with mock.patch.object(maps, "tile", side_effect=fake_tile):
            image = maps.stitch("osm", 40.7484, -73.9857, 18, 512, 384)
        loader = GdkPixbuf.PixbufLoader()
        loader.write(image.png)
        loader.close()
        pixels = loader.get_pixbuf()
        x, y = 100, 200
        wx, wy = image.x0 + x, image.y0 + y
        expected = (int(wx // 256) % 256, int(wy // 256) % 256)
        stride, data = pixels.get_rowstride(), pixels.get_pixels()
        at = y * stride + x * pixels.get_n_channels()
        self.assertEqual((data[at], data[at + 1]), expected)
        lat, lon = image.to_lat_lon(256, 192)
        self.assertAlmostEqual(lat, 40.7484, places=4)
        self.assertAlmostEqual(lon, -73.9857, places=4)

    def test_buildings_parsed_from_overpass(self):
        answer = {"elements": [
            {"type": "way", "id": 1, "tags": {"building": "yes", "name": "Hall"},
             "geometry": [{"lat": 1, "lon": 1}, {"lat": 1, "lon": 2}, {"lat": 2, "lon": 2},
                          {"lat": 1, "lon": 1}]},
            {"type": "way", "id": 2, "tags": {"building": "yes", "addr:housenumber": "12",
                                              "addr:street": "Main St"},
             "geometry": [{"lat": 0, "lon": 0}] * 5},
            {"type": "way", "id": 3, "tags": {"building": "yes"}, "geometry": [{"lat": 0, "lon": 0}]}]}
        found = maps.parse_buildings(answer)
        self.assertEqual([b["name"] for b in found], ["Hall", "12 Main St"])

    def test_wifi_lookup_sends_only_fixed_mapped_access_points(self):
        readings = [SimpleNamespace(ssid="Office", bssid="AC:84:C6:00:00:01", rssi=-55, freq_mhz=2437),
                    SimpleNamespace(ssid="Cafe_nomap", bssid="AC:84:C6:00:00:02", rssi=-60, freq_mhz=2412),
                    SimpleNamespace(ssid="Phone", bssid="DA:A1:19:00:00:03", rssi=-50, freq_mhz=5180),
                    SimpleNamespace(ssid="", bssid="10:27:F5:00:00:04", rssi=-70, freq_mhz=5745)]
        payload = maps.wifi_payload(readings)
        sent = [ap["macAddress"] for ap in payload["wifiAccessPoints"]]
        self.assertEqual(sent, ["ac:84:c6:00:00:01", "10:27:f5:00:00:04"])
        self.assertFalse(payload["considerIp"])
        self.assertNotIn("Office", json.dumps(payload))     # names never leave

        answer = io.BytesIO(json.dumps({"location": {"lat": 51.5, "lng": -0.12}, "accuracy": 35}).encode())
        answer.__enter__ = lambda *_: answer
        answer.__exit__ = lambda *_: None
        with mock.patch("wifisurveyor.net.outbound._opener.open", return_value=answer) as opened:
            self.assertEqual(maps.wifi_locate(readings), (51.5, -0.12, 35.0))
        request = opened.call_args[0][0]
        self.assertEqual(request.full_url, maps.BEACONDB)
        self.assertNotIn("@", request.get_header("User-agent"))   # no personal details

        missing = urllib.error.HTTPError(maps.BEACONDB, 404, "Not Found", {}, io.BytesIO(b"{}"))
        with mock.patch("wifisurveyor.net.outbound._opener.open", side_effect=missing):
            with self.assertRaises(LookupError):
                maps.wifi_locate(readings)
        missing.close()

    def test_address_search_brings_the_building_outline(self):
        square = [[-73.9862, 40.7480], [-73.9850, 40.7480], [-73.9850, 40.7488], [-73.9862, 40.7488],
                  [-73.9862, 40.7480]]
        small = [[0, 0], [0.0001, 0], [0.0001, 0.0001], [0, 0]]
        answer = [{"display_name": "Empire State Building, 350, 5th Avenue", "lat": "40.7484",
                   "lon": "-73.9857", "geojson": {"type": "Polygon", "coordinates": [square]}},
                  {"display_name": "A campus", "lat": "1", "lon": "2",
                   "geojson": {"type": "MultiPolygon", "coordinates": [[small], [square]]}},
                  {"display_name": "A mast", "lat": "1", "lon": "2",
                   "geojson": {"type": "Point", "coordinates": [2, 1]}}]
        with mock.patch.object(maps, "_request", return_value=json.dumps(answer).encode()) as asked:
            found = maps.search("350 5th Avenue, New York")
        self.assertIn("polygon_geojson=1", asked.call_args[0][0])
        name, lat, lon, outline = found[0]
        self.assertEqual(outline[0], (40.7480, -73.9862))          # (lat, lon), not GeoJSON's order
        self.assertEqual(len(found[1][3]), 5)                       # the bigger part of the two
        self.assertIsNone(found[2][3])                              # a point has no outline

    def test_tiles_are_kept_on_disk(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": folder}), \
                mock.patch.object(maps, "_request", return_value=b"tile") as fetched:
            self.assertEqual(maps.tile("osm", 18, 1, 2), b"tile")
            self.assertEqual(maps.tile("osm", 18, 1, 2), b"tile")
        self.assertEqual(fetched.call_count, 1)


class ProjectGeoTest(unittest.TestCase):
    def test_kept_with_the_survey(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": folder}):
            project = Project(geo={"lat": 40.7, "lon": -73.9, "meters_per_pixel": 0.05})
            loaded = Project.load(project.save(os.path.join(folder, "s.wsurvey")))
            self.assertEqual(loaded.geo["meters_per_pixel"], 0.05)
            self.assertEqual(loaded.follow_up().geo, loaded.geo)


if __name__ == "__main__":
    unittest.main()
