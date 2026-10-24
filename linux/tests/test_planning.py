import json
import unittest

from wifisurveyor.core import analysis, planning, report
from wifisurveyor.core.project import Point, Project


def network(ssid, rssi, bssid, freq=5180, channel=36):
    return {"ssid": ssid, "bssid": bssid, "rssi": rssi, "freq_mhz": freq, "channel": channel,
            "band": "5", "rssi_source": "iw"}


def survey() -> Project:
    project = Project(width=400, height=200)
    project.set_scale(400, 100)                          # 4 px a foot
    levels = [(20, 20, -45), (200, 20, -60), (380, 20, -72), (20, 180, -55), (200, 180, -66),
              (380, 180, -78)]
    project.points = [Point(x=x, y=y, networks=[network("Corp", rssi, "AA:BB:CC:00:00:01")])
                      for x, y, rssi in levels]
    return project


class PlanningTests(unittest.TestCase):
    def test_area_coverage_counts_floor_not_points(self) -> None:
        project = survey()
        ctx = analysis.Context(project)
        found = planning.area_coverage(project, ctx, "signal", -70)
        self.assertGreater(found["share"], 0.4)
        self.assertLess(found["share"], 1.0)
        self.assertGreater(found["surveyed_sqft"], found["meeting_sqft"])

    def test_phone_allowance_raises_every_target(self) -> None:
        project = survey()
        project.client_allowance_db = 5
        self.assertEqual(analysis.Context(project).profile.primary, -65)

    def test_moved_access_point_uses_its_real_position(self) -> None:
        project = survey()
        estimated = analysis.ap_sites(project)
        self.assertEqual(len(estimated), 1)
        project.ap_pins = [{"bssids": estimated[0]["bssids"], "name": "Lobby AP",
                            "networks": [["Corp", ["5"]]], "x": 30.0, "y": 30.0}]
        site = analysis.ap_sites(project)[0]
        self.assertTrue(site["placed"])
        self.assertEqual((site["x"], site["y"], site["name"]), (30.0, 30.0, "Lobby AP"))
        self.assertFalse(site["fits"][0]["rough"])

    def test_planned_access_point_raises_coverage_where_it_goes(self) -> None:
        project = survey()
        ctx = analysis.Context(project)
        project.virtual_aps = [{"x": 380, "y": 100, "bands": ["5"], "name": "East"}]
        gain = planning.planned_gain(project, ctx)
        self.assertGreater(gain["after"], gain["now"])

    def test_report_has_the_new_sections(self) -> None:
        project = survey()
        project.virtual_aps = [{"x": 380, "y": 100, "bands": ["5"], "name": "East"}]
        project.points[0].note = "By the door"
        project.points[0].throughput = {"down_mbps": 8.0, "up_mbps": 3.0}
        html = report.build_html(project, b"\x89PNG", lambda _p: None,
                                 details={"company": "Acme Wi-Fi", "client": "Bobs Market"},
                                 version="9.9")
        for expected in ("Acme Wi-Fi", "Bobs Market", "Floor area", "Checklist", "Planned access points",
                         "Access points", "How this survey was made", "By the door",
                         "Low throughput", "Wi-Fi Surveyor 9.9"):
            self.assertIn(expected, html)


class MeasuringTests(unittest.TestCase):
    def test_second_scan_fills_in_what_the_passive_one_missed(self) -> None:
        from wifisurveyor.ui.survey_view import _merge_scans

        first = [network("Corp", -60, "A"), network("Corp", -80, "B")]
        second = [network("Corp", -58, "A"), network("Guest", -70, "C")]
        merged = {n["bssid"]: n for n in _merge_scans(first, second)}
        # Heard by both: the mean of its two readings, not the stronger
        self.assertEqual({k: n["rssi"] for k, n in merged.items()}, {"A": -59, "B": -80, "C": -70})
        self.assertEqual(merged["A"]["readings"], 2)
        # A third scan weighs each reading the same
        again = {n["bssid"]: n["rssi"] for n in _merge_scans(list(merged.values()),
                                                              [network("Corp", -65, "A")])}
        self.assertEqual(again["A"], -61)

    def test_iperf3_json_is_read_as_mbps(self) -> None:
        from wifisurveyor.net.throughput import _mbps

        output = json.dumps({"end": {"sum_received": {"bits_per_second": 94_500_000}}})
        self.assertAlmostEqual(_mbps(output), 94.5)


if __name__ == "__main__":
    unittest.main()
