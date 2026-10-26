"""Where the access points and the noise are: sites, emitters, and the US Wi-Fi ranges."""

from __future__ import annotations

import math
import unittest
from unittest import mock

from wifisurveyor.core import analysis
from wifisurveyor.core.project import Point, Project, Wall
from wifisurveyor.net import wifi
from wifisurveyor.rf import explorer

MPP = 0.02          # 1000 x 800 pixels is a 20 x 16 m floor


def path_loss(x, y, source, p0=-30.0, exponent=2.0) -> float:
    metres = max(0.5, math.hypot(x - source[0], y - source[1]) * MPP)
    return p0 - 10 * exponent * math.log10(metres)


def network(rssi, bssid, ssid="Office", freq=2412, channel=1, band="2.4") -> dict:
    return {"ssid": ssid, "bssid": bssid, "rssi": round(rssi), "freq_mhz": freq,
            "channel": channel, "band": band, "secured": True, "security": "RSN"}


def grid_points():
    return [(x, y) for x in range(100, 1000, 200) for y in range(100, 800, 200)]


class ApSiteTests(unittest.TestCase):
    def test_radios_of_one_access_point_become_one_site(self) -> None:
        here, there = (300.0, 250.0), (850.0, 650.0)
        points = []
        for x, y in grid_points():
            points.append(Point(x, y, networks=[
                network(path_loss(x, y, here), "AA:00:00:00:00:01"),
                network(path_loss(x, y, here) - 6, "AA:00:00:00:00:02", freq=5180, channel=36, band="5"),
                network(path_loss(x, y, there), "BB:00:00:00:00:01", ssid="Guest"),
            ]))
        project = Project(width=1000, height=800, meters_per_pixel=MPP, points=points)

        sites = analysis.ap_sites(project)

        self.assertEqual(len(sites), 2)
        office = next(s for s in sites if s["networks"][0][0] == "Office")
        self.assertEqual(office["networks"], [("Office", ["2.4", "5"])])
        self.assertLess(math.hypot(office["x"] - here[0], office["y"] - here[1]), 60)

    def test_band_filter_leaves_other_radios_out(self) -> None:
        here = (300.0, 250.0)
        points = [Point(x, y, networks=[
            network(path_loss(x, y, here), "AA:00:00:00:00:01"),
            network(path_loss(x, y, here), "AA:00:00:00:00:02", freq=5180, channel=36, band="5"),
        ]) for x, y in grid_points()]
        project = Project(width=1000, height=800, meters_per_pixel=MPP, points=points)
        self.assertEqual(analysis.ap_sites(project, band="5")[0]["networks"], [("Office", ["5"])])

    def test_ssids_on_one_radio_count_once_towards_the_limit(self) -> None:
        here, there = (300.0, 250.0), (850.0, 650.0)
        points = [Point(x, y, networks=[
            network(path_loss(x, y, here), "AA:00:00:00:00:01"),
            network(path_loss(x, y, here) + 1, "AA:00:00:40:00:01", ssid="Guest"),
            network(path_loss(x, y, there), "CC:00:00:00:00:01", ssid="Lab", channel=6, freq=2437),
        ]) for x, y in grid_points()]
        project = Project(width=1000, height=800, meters_per_pixel=MPP, points=points)

        sites = analysis.ap_sites(project, limit=2)

        self.assertEqual(len(sites), 2)
        office = next(s for s in sites if s["networks"][0][0] == "Guest")
        self.assertEqual(office["networks"], [("Guest", ["2.4"]), ("Office", ["2.4"])])
        self.assertEqual(sorted(office["bssids"]), ["AA:00:00:00:00:01", "AA:00:00:40:00:01"])

    def test_neighbouring_addresses_in_different_places_stay_apart(self) -> None:
        # Two access points out of one box: consecutive addresses, one channel, but each reads
        # loudest where it is, so they're two radios.
        here, there = (300.0, 250.0), (850.0, 650.0)
        heard = {}
        points = []
        for index, (x, y) in enumerate(grid_points()):
            heard.setdefault("AA:00:00:00:00:01", {})[index] = path_loss(x, y, here)
            heard.setdefault("AA:00:00:00:00:02", {})[index] = path_loss(x, y, there)
            points.append(Point(x, y, networks=[
                network(path_loss(x, y, here), "AA:00:00:00:00:01"),
                network(path_loss(x, y, there), "AA:00:00:00:00:02", ssid="Guest")]))
        channels = {"AA:00:00:00:00:01": 1, "AA:00:00:00:00:02": 1}
        self.assertFalse(analysis.same_radio("AA:00:00:00:00:01", "AA:00:00:00:00:02",
                                             heard, channels))
        project = Project(width=1000, height=800, meters_per_pixel=MPP, points=points)
        self.assertEqual(len(analysis.ap_sites(project, limit=1)), 1)

    def test_one_radio_needs_the_same_channel(self) -> None:
        levels = {0: -40.0, 1: -60.0, 2: -70.0}
        heard = {"00:00:00:00:00:01": levels, "00:00:00:40:00:01": dict(levels)}
        self.assertTrue(analysis.same_radio(*heard, heard, dict.fromkeys(heard, 36)))
        self.assertFalse(analysis.same_radio(*heard, heard, {"00:00:00:00:00:01": 36,
                                                             "00:00:00:40:00:01": 40}))

    def test_walls_shared_between_access_points_give_the_same_fit(self) -> None:
        here = (300.0, 250.0)
        walls = [Wall(500, 0, 500, 800, db=8), Wall(0, 400, 1000, 400, db=5)]
        points = [Point(x, y, networks=[network(path_loss(x, y, here), "AA:00:00:00:00:01")])
                  for x, y in grid_points()]
        project = Project(width=1000, height=800, meters_per_pixel=MPP, points=points, walls=walls)
        site = analysis.ap_sites(project)[0]
        alone = analysis.locate(project, analysis.ap_samples(project, "AA:00:00:00:00:01"))
        self.assertEqual((site["x"], site["y"]), (alone["x"], alone["y"]))


def segment_with(points_levels: dict[float, float], floor: float = -100.0) -> dict:
    """A 2.4 GHz sweep at 1 MHz steps, flat at the floor except where levels are given."""
    trace = [floor] * 101
    for freq, level in points_levels.items():
        trace[int(freq - 2400)] = level
    return {"start_mhz": 2400.0, "step_mhz": 1.0, "rbw_khz": 1000, "sweeps": 8,
            "avg": list(trace), "max": list(trace), "min": [floor] * 101, "occupancy": {}}


class NoiseSourceTests(unittest.TestCase):
    def build(self, source=(700.0, 300.0)) -> Project:
        points = []
        for x, y in grid_points():
            # Two networks well away from the emitter give the card-to-analyzer offset
            levels = {}
            for centre in (2412, 2417):
                for bin_freq in range(centre - 9, centre + 10):
                    levels[bin_freq] = -50.0
            # A steady 6 MHz-wide carrier at 2480 MHz that no network explains
            for bin_freq in range(2477, 2484):
                levels[bin_freq] = path_loss(x, y, source, p0=-35)
            points.append(Point(x, y, networks=[
                network(-50, "AA:00:00:00:00:01"),
                network(-50, "AA:00:00:00:00:02", freq=2417, channel=2),
            ], spectrum={"segments": [segment_with(levels)], "noise_24": -95.0,
                         "noise_5": None, "noise_6": None}))
        return Project(width=1000, height=800, meters_per_pixel=MPP, points=points)

    def test_finds_names_and_places_an_unexplained_carrier(self) -> None:
        project = self.build()
        sources = analysis.noise_sources(project, analysis.Context(project))

        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertAlmostEqual(source["center"], 2480, delta=1)
        self.assertEqual(source["kind"], "continuous")
        self.assertEqual(source["band"], "2.4")
        self.assertIn(13, source["channels"])
        self.assertIsNotNone(source["located"])
        self.assertLess(math.hypot(source["located"]["x"] - 700, source["located"]["y"] - 300), 120)

    def test_networks_that_explain_the_energy_are_not_noise(self) -> None:
        project = self.build()
        for point in project.points:
            point.spectrum["segments"][0] = segment_with(
                {f: -50.0 for c in (2412, 2417) for f in range(c - 9, c + 10)})
        self.assertEqual(analysis.noise_sources(project, analysis.Context(project)), [])

    def test_channels_touched(self) -> None:
        self.assertEqual(analysis.channels_touched(2437, 20), [3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(analysis.channels_touched(5180, 10), [36])


class RangeTests(unittest.TestCase):
    def test_us_ranges_fit_the_analyzer_and_cover_every_us_channel(self) -> None:
        from wifisurveyor.rf import spectrum as spec

        for _name, start, end in explorer.WIFI_RANGES:
            self.assertLessEqual(end - start, 600)
        covered = lambda centre: any(start <= centre - 10 and centre + 10 <= end
                                     for _n, start, end in explorer.WIFI_RANGES)
        self.assertTrue(all(covered(c) for c in spec.CHANNELS_24.values() if c < 2480))
        self.assertTrue(all(covered(c) for c in spec.CHANNELS_5.values()))
        self.assertTrue(covered(spec.CHANNELS_6[1]))

    def test_passive_scan_asks_iw_not_to_probe(self) -> None:
        with mock.patch.object(wifi, "have", return_value=True), \
             mock.patch.object(wifi, "run", return_value=(0, "")) as run:
            wifi.scan("wlan0", passive=True)
        self.assertEqual(run.call_args_list[0].args, ("iw", "dev", "wlan0", "scan", "passive"))


if __name__ == "__main__":
    unittest.main()
