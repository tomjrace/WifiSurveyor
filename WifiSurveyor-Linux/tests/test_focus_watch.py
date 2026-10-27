"""Focus networks, locating access points off the plan, and watches added to a survey."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import analysis, geo, report, watchreport
from wifisurveyor.core.project import Point, Project
from wifisurveyor.net import connectivity

MPP = 0.02          # 1000 x 800 pixels is a 20 x 16 m floor


def path_loss(x, y, source, p0=-28.0, exponent=2.6) -> float:
    metres = max(0.5, math.hypot(x - source[0], y - source[1]) * MPP)
    return p0 - 10 * exponent * math.log10(metres)


def network(rssi, bssid, ssid, channel=36, freq=5180, band="5") -> dict:
    return {"ssid": ssid, "bssid": bssid, "rssi": round(rssi), "freq_mhz": freq,
            "channel": channel, "band": band, "secured": True, "security": "RSN"}


def grid_points():
    return [(x, y) for x in range(80, 1000, 140) for y in range(80, 800, 140)]


def write_log(folder: Path, ssid: str, freq: float, start: float, drops: list[tuple[float, float]]) -> Path:
    folder.mkdir(parents=True)
    lines = [{"t": start, "type": "start", "ssid": ssid, "bssid": "02:00:00:00:00:01"},
             {"t": start, "type": "target", "ssid": ssid, "bssid": "02:00:00:00:00:01",
              "freq": freq, "monitor": "wlan1"}]
    for second in range(0, 600, 2):
        lines.append({"t": start + second, "type": "beat", "signal": -60.0, "gw": 2.0,
                      "state": "up"})
    for number, (begin, end) in enumerate(drops, start=1):
        lines.append({"t": start + end, "type": "incident", "incident": {
            "id": number, "start": start + begin, "end": start + end, "kind": "disconnect",
            "ssid": ssid, "bssid": "02:00:00:00:00:01", "freq_mhz": freq,
            "diagnosis": {"title": "The access point went down (rebooted or lost power)",
                          "confidence": "likely", "why": ["Beacons stopped"],
                          "do": ["Check its power"]}}})
    lines.append({"t": start + 600, "type": "stop"})
    path = folder / "session.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


class OffPlanTests(unittest.TestCase):
    def test_a_neighbours_access_point_is_placed_past_the_plan(self) -> None:
        there = (1500.0, 300.0)                      # 10 m past the right-hand edge
        points = [Point(x, y, networks=[network(path_loss(x, y, there), "02:11:22:33:44:01",
                                                "Neighbour")]) for x, y in grid_points()]
        project = Project(width=1000, height=800, meters_per_pixel=MPP, points=points)
        site = analysis.ap_sites(project)[0]
        self.assertIsNotNone(site["beyond"])
        self.assertIn("right", site["beyond"]["way"])
        self.assertLess(math.hypot(site["x"] - there[0], site["y"] - there[1]) * MPP, 3)

    def test_a_focus_networks_access_point_is_kept_to_the_property(self) -> None:
        there = (1500.0, 300.0)
        points = [Point(x, y, networks=[network(path_loss(x, y, there), "02:11:22:33:44:01",
                                                "Office")]) for x, y in grid_points()]
        project = Project(width=1000, height=800, meters_per_pixel=MPP, points=points,
                          focus_networks=["Office"])
        site = analysis.ap_sites(project)[0]
        self.assertIsNone(site["beyond"])
        self.assertLessEqual(site["x"], 1000)

    def test_without_a_scale_nothing_is_placed_off_the_plan(self) -> None:
        there = (1500.0, 300.0)
        points = [Point(x, y, networks=[network(path_loss(x, y, there), "02:11:22:33:44:01",
                                                "Neighbour")]) for x, y in grid_points()]
        project = Project(width=1000, height=800, points=points)
        site = analysis.ap_sites(project)[0]
        self.assertIsNone(site["beyond"])
        self.assertTrue(0 <= site["x"] <= 1000 and 0 <= site["y"] <= 800)

    def test_a_plan_on_the_map_gives_coordinates_and_compass_directions(self) -> None:
        project = Project(width=1000, height=800, meters_per_pixel=MPP,
                          geo={"lat": 40.0, "lon": -75.0, "rotation_deg": 90.0})
        lat, lon = geo.plan_lat_lon(project, 500, 400)
        self.assertAlmostEqual(lat, 40.0)
        self.assertAlmostEqual(lon, -75.0)
        # Turned 90° clockwise, the plan's top points east
        self.assertEqual(geo.compass(project, 0, -10), "east")
        lat, lon = geo.plan_lat_lon(project, 500, 0)           # 8 m up the plan: east
        self.assertAlmostEqual(lat, 40.0, places=6)
        self.assertGreater(lon, -75.0)


class FocusTests(unittest.TestCase):
    def survey(self) -> Project:
        here = (300.0, 250.0)
        points = [Point(x, y, networks=[
            network(path_loss(x, y, here), "02:00:00:00:00:01", "Office"),
            network(path_loss(x, y, here) + 12, "02:00:00:00:00:09", "Neighbour", channel=40,
                    freq=5200)]) for x, y in grid_points()]
        return Project(width=1000, height=800, meters_per_pixel=MPP, points=points,
                       focus_networks=["Office"])

    def test_each_point_is_judged_for_the_focus_network(self) -> None:
        project = self.survey()
        self.assertEqual(analysis.focus_here(project, project.points[0]), "Office")
        html = report.build_html(project, None, lambda _p: None)
        self.assertIn("the strongest of Office at each point", html)
        self.assertIn("Networks surveyed", html)

    def test_checks_on_several_networks_are_each_judged(self) -> None:
        point = Point(0, 0, checks=[
            {"ssid": "Office", "latency_ms": 2.0, "loss": 0.0, "jitter_ms": 0.5},
            {"ssid": "Guest", "error": "connected, but no gateway: DHCP may have failed"},
            {"ssid": "Lab", "skipped": "not heard here"}])
        self.assertEqual([c["ssid"] for c in point.checks_made()], ["Office", "Guest"])
        self.assertEqual(point.check_for("Guest")["error"][:9], "connected")
        self.assertIsNone(point.check_for("Lab"))
        # Only skipped ones: no check was made, and the single result isn't used instead
        self.assertEqual(Point(0, 0, connectivity={"loss": 0}, checks=[
            {"ssid": "Lab", "skipped": "not heard here"}]).checks_made(), [])

    def test_a_chosen_open_network_may_be_joined(self) -> None:
        heard = [dict(network(-50, "02:00:00:00:00:01", "Cafe"), secured=False)]
        self.assertIn("open network", connectivity.why_not_test("Cafe", heard))
        with mock.patch.object(connectivity, "have", return_value=True):
            self.assertIsNone(connectivity.why_not_test("Cafe", heard, allow_open=True))

    def test_focus_networks_and_watches_are_saved(self) -> None:
        project = Project(focus_networks=["Office"], watches=[{"ssid": "Office", "x": 1.0,
                                                              "y": 2.0, "bands": []}],
                          points=[Point(1, 2, checks=[{"ssid": "Office", "loss": 0}])])
        again = Project.from_dict(json.loads(json.dumps(project.to_dict())))
        self.assertEqual(again.focus_networks, ["Office"])
        self.assertEqual(again.watches[0]["x"], 1.0)
        self.assertEqual(again.points[0].checks, [{"ssid": "Office", "loss": 0}])
        self.assertEqual(project.follow_up().focus_networks, ["Office"])


class WatchTests(unittest.TestCase):
    def test_both_bands_of_one_watch_make_one_summary(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            five = write_log(base / "a 5 GHz", "Office", 5180, 1000.0, [(100, 160)])
            write_log(base / "a 2.4 GHz", "Office", 2412, 1004.0, [])
            write_log(base / "b 2.4 GHz", "Office", 2412, 1090.0, [])   # a later, separate watch
            logs = watchreport.partners(five)
            self.assertEqual([p.parent.name for p in logs], ["a 5 GHz", "a 2.4 GHz"])
            watch = watchreport.summary(logs)
        self.assertEqual([b["band"] for b in watch["bands"]], ["2.4 GHz", "5 GHz"])
        five_ghz = watch["bands"][1]
        self.assertAlmostEqual(five_ghz["availability"], 0.9)
        self.assertEqual(five_ghz["incidents"][0]["cause"][:20], "The access point wen")
        self.assertIn("1 dropout", watchreport.headline(watch))
        self.assertEqual(watchreport.availability_class(five_ghz["availability"]), "poor")

    def test_a_watch_placed_on_a_survey_is_in_its_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log = write_log(Path(folder) / "a 5 GHz", "Office", 5180, 1000.0, [(100, 160)])
            watch = watchreport.summary([log])
        project = Project(width=1000, height=800, points=[Point(100, 100)],
                          watches=[dict(watch, x=110.0, y=100.0)])
        html = report.build_html(project, b"\x89PNG", lambda _p: None)
        self.assertIn("Networks watched", html)
        self.assertIn("watched from near point 1", html)
        self.assertIn("watchpin", html)
        self.assertIn("Check its power", html)
        alone = watchreport.build_html(watch)
        self.assertIn("<title>Watch: Office</title>", alone)


if __name__ == "__main__":
    unittest.main()
