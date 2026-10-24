"""Checks for the analysis: verdicts, source location, walls, and the heatmap maths."""

from __future__ import annotations

import math
import random
import unittest

import numpy as np

from wifisurveyor.core import analysis, heatmap
from wifisurveyor.core.project import Point, Project, Wall


def network(rssi: float, channel: int = 1, freq: float = 2412, ssid: str = "Office",
            bssid: str = "AA:AA:AA:AA:AA:01", band: str = "2.4") -> dict:
    return {"ssid": ssid, "bssid": bssid, "rssi": rssi, "freq_mhz": freq,
            "channel": channel, "band": band, "secured": True, "security": "RSN"}


def spectrum_with_occupancy(channel: int, fraction: float, noise: float = -95) -> dict:
    # One flat 2.4 GHz segment, enough for segment_for to find it
    return {
        "segments": [{"start_mhz": 2400.0, "step_mhz": 1.0, "rbw_khz": 600, "sweeps": 10,
                      "avg": [-100.0] * 101, "max": [-100.0] * 101, "min": [-100.0] * 101,
                      "occupancy": {str(channel): fraction}}],
        "noise_24": noise, "noise_5": None, "noise_6": None,
    }


class VerdictTests(unittest.TestCase):
    def verdict(self, point: Point) -> str:
        return analysis.diagnose(point, analysis.Context(Project(points=[point])))[0]

    def test_no_scan_is_no_data(self) -> None:
        self.assertEqual(self.verdict(Point(0, 0)), "nodata")

    def test_weak_signal_is_coverage(self) -> None:
        self.assertEqual(self.verdict(Point(0, 0, networks=[network(-80)])), "coverage")

    def test_low_snr_with_networks_explaining_it_is_cochannel(self) -> None:
        point = Point(0, 0, networks=[network(-60)], spectrum=spectrum_with_occupancy(1, 0.1, noise=-65))
        self.assertEqual(self.verdict(point), "cochannel")

    def test_busy_channel_is_congestion(self) -> None:
        point = Point(0, 0, networks=[network(-50)], spectrum=spectrum_with_occupancy(1, 0.8))
        self.assertEqual(self.verdict(point), "congestion")

    def test_retries_are_collisions(self) -> None:
        point = Point(0, 0, networks=[network(-50)],
                      capture={"retry_rate": 0.3, "retry_eligible": 500, "airtime": 0.1, "clients": 4})
        self.assertEqual(self.verdict(point), "collisions")

    def test_slow_link_is_configuration(self) -> None:
        point = Point(0, 0, networks=[network(-50)], link={"tx_mbps": 24.0})
        self.assertEqual(self.verdict(point), "config")

    def test_all_good(self) -> None:
        point = Point(0, 0, networks=[network(-50)], link={"tx_mbps": 400.0})
        self.assertEqual(self.verdict(point), "good")


class OccupancyTests(unittest.TestCase):
    def test_defaults_to_the_serving_networks_channel(self) -> None:
        point = Point(0, 0, networks=[network(-50, channel=6, freq=2437)],
                      spectrum=spectrum_with_occupancy(6, 0.4))
        context = analysis.Context(Project(points=[point]))
        self.assertAlmostEqual(analysis.metric(point, "occupancy", context), 40.0)
        self.assertIsNone(analysis.metric(point, "occupancy", context, channel=11))


class WallTests(unittest.TestCase):
    def test_grid_matches_the_scalar_test(self) -> None:
        random.seed(7)
        project = Project(walls=[Wall(*(random.uniform(0, 500) for _ in range(4)), db=random.choice([3, 12]))
                                 for _ in range(8)])
        xs = np.array([random.uniform(0, 500) for _ in range(6)])
        ys = np.array([random.uniform(0, 500) for _ in range(6)])
        cx, cy = np.meshgrid(np.linspace(0, 500, 9), np.linspace(0, 500, 7))
        grid = analysis.grid_wall_loss(project.walls, xs, ys, cx, cy)
        for (row, col), _ in np.ndenumerate(cx):
            for index in range(len(xs)):
                expected = analysis.wall_loss(project, xs[index], ys[index], cx[row, col], cy[row, col])
                self.assertEqual(grid[row, col, index], expected)

    def test_no_walls_is_zero(self) -> None:
        cx, cy = np.meshgrid(np.arange(3.0), np.arange(2.0))
        self.assertFalse(analysis.grid_wall_loss([], np.zeros(4), np.zeros(4), cx, cy).any())


class LocateTests(unittest.TestCase):
    def test_finds_a_transmitter_from_the_signal_around_it(self) -> None:
        source = (420.0, 310.0)
        project = Project(width=800, height=600, meters_per_pixel=0.05)
        for x in range(60, 800, 140):
            for y in range(60, 600, 140):
                metres = max(0.5, math.hypot(x - source[0], y - source[1]) * 0.05)
                rssi = -30 - 10 * 3.0 * math.log10(metres)
                project.points.append(Point(x, y, networks=[network(round(rssi))]))
        found = analysis.locate(project, analysis.ap_samples(project, "AA:AA:AA:AA:AA:01"))
        self.assertIsNotNone(found)
        assert found is not None
        self.assertLess(math.hypot(found["x"] - source[0], found["y"] - source[1]), 40)
        self.assertFalse(found["outside"])

    def test_too_few_points_is_no_answer(self) -> None:
        project = Project(points=[Point(0, 0, networks=[network(-50)])])
        self.assertIsNone(analysis.locate(project, analysis.ap_samples(project, "AA:AA:AA:AA:AA:01")))


class HeatmapTests(unittest.TestCase):
    def test_bands_of_rows_give_the_same_picture(self) -> None:
        random.seed(11)
        samples = [(random.uniform(0, 300), random.uniform(0, 200), random.uniform(-85, -45)) for _ in range(25)]
        whole = bytes(heatmap.render(300, 200, samples, -85, -45, True, 60).get_data())
        saved = heatmap.CHUNK_CELLS
        try:
            heatmap.CHUNK_CELLS = 100          # forces one row per band
            banded = bytes(heatmap.render(300, 200, samples, -85, -45, True, 60).get_data())
            verdicts = [(x, y, random.randint(0, 7)) for x, y, _ in samples]
            colours = [c for _, _, c in analysis.VERDICTS]
            banded_c = bytes(heatmap.categorical(300, 200, verdicts, colours, 60).get_data())
        finally:
            heatmap.CHUNK_CELLS = saved
        whole_c = bytes(heatmap.categorical(300, 200, verdicts, colours, 60).get_data())
        self.assertEqual(whole, banded)
        self.assertEqual(whole_c, banded_c)


if __name__ == "__main__":
    unittest.main()
