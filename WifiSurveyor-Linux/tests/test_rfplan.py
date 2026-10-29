import unittest

from wifisurveyor.core import overview, report, rfplan
from wifisurveyor.core.project import Point, Project


def net(ssid, bssid, rssi, freq, center=None, width=20, **extra):
    return {"ssid": ssid, "bssid": bssid, "rssi": rssi, "freq_mhz": freq,
            "center_mhz": center or freq, "width_mhz": width, "channel": 0,
            "band": rfplan.band_of(freq), **extra}


class RadioTests(unittest.TestCase):
    def test_bssids_of_one_radio_are_grouped(self) -> None:
        point = Point(x=0, y=0, networks=[
            net("Corp", "88:71:b1:00:00:f3", -50, 5180),
            net("Guest", "86:71:b1:00:00:f3", -51, 5180),
            net("Corp", "88:71:b1:00:00:f0", -55, 2437),
        ])
        found = rfplan.radios(point.networks)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].ssids, {"Corp", "Guest"})

    def test_contention_second_ap_and_requirements(self) -> None:
        point = Point(x=0, y=0, networks=[
            net("Corp", "00:00:00:00:01:01", -60, 5180, 5210, 80),
            net("Corp", "00:00:00:00:02:01", -66, 5745, 5775, 80),
            net("Other", "00:00:00:00:03:01", -75, 5200),       # inside Corp's 80 MHz
            net("Other", "00:00:00:00:04:01", -90, 5200),       # too weak to matter
        ])
        self.assertEqual(len(rfplan.contenders(point, "Corp")), 1)
        self.assertEqual(rfplan.nth_strongest(point, 2, "Corp"), -66)
        ok, why = rfplan.evaluate(point, rfplan.PROFILES["voice"], "Corp", "All", -95, 0.2)
        self.assertTrue(ok, why)
        strict = rfplan.Profile("Strict", -55, None, None, 20, 0, 0.1)
        ok, why = rfplan.evaluate(point, strict, "Corp", "All", -95, 0.2)
        self.assertFalse(ok)
        self.assertEqual(why, ["signal -60 dBm, needs -55", "1 radios share the channel, at most 0",
                               "channel busy 20%, at most 10%"])


class AuditAndPlanTests(unittest.TestCase):
    def test_audit_findings(self) -> None:
        low = dict(basic_rates=[1.0, 2.0, 5.5, 11.0], roam_k=False, roam_v=False, roam_r=False,
                   auth="PSK", beacon_interval=100)
        project = Project(points=[Point(x=0, y=0, networks=[
            net("Corp", "00:00:00:00:01:01", -50, 2427, **low),
            net("Corp", "00:00:00:00:02:01", -60, 2437, **{**low, "auth": "PSK SAE"}),
        ] + [net(f"S{i}", f"00:00:00:00:03:0{i}", -60, 2437, **low) for i in range(6)])])
        titles = {f.title for f in rfplan.audit(project)}
        for expected in ("802.11b rates still required", "Overlapping 2.4 GHz channel",
                         "Security differs between access points", "Beacon overhead",
                         "Many SSIDs on one radio", "No roaming assistance"):
            self.assertIn(expected, titles)

    def test_channel_plan_separates_own_radios(self) -> None:
        # Two of the network's radios on the same channel, heard together at two points
        points = [Point(x=i, y=0, networks=[
            net("Corp", "00:00:00:00:01:01", -55, 5180, 5210, 80),
            net("Corp", "00:00:00:00:02:01", -60, 5180, 5210, 80),
        ]) for i in range(2)]
        plan = rfplan.channel_plan(Project(points=points), "Corp")
        self.assertEqual(len(plan), 2)
        self.assertEqual(sum(s.changed for s in plan), 1)
        self.assertEqual([s.shared_before for s in plan], [1, 1])
        self.assertEqual([s.shared_after for s in plan], [0, 0])
        self.assertIn("ch 149–161 (80 MHz)", {s.suggested for s in plan})
        self.assertEqual([s.clashes_before for s in plan], [2, 2])     # heard together twice
        self.assertEqual([s.clashes_after for s in plan], [0, 0])
        steps = overview.channel_steps(Project(points=points), [], ["Corp"], plan)
        self.assertEqual(len(steps), 1)
        self.assertIn("heard strongest at point 1", steps[0])
        self.assertIn("from channel 36–48 (80 MHz) to channel 149–161 (80 MHz)", steps[0])
        self.assertIn("2 times now, 0 after", steps[0])

    def test_channel_plan_leaves_a_slight_clash_alone(self) -> None:
        # Heard together at one point only: not worth the work of a change
        points = [Point(x=0, y=0, networks=[
            net("Corp", "00:00:00:00:01:01", -55, 5180, 5210, 80),
            net("Corp", "00:00:00:00:02:01", -60, 5180, 5210, 80),
        ]), Point(x=1, y=0, networks=[net("Corp", "00:00:00:00:01:01", -55, 5180, 5210, 80)])]
        plan = rfplan.channel_plan(Project(points=points), "Corp")
        self.assertFalse(any(s.changed for s in plan))

    def test_channel_plan_moves_only_the_radio_that_clashes(self) -> None:
        # Three radios on 2.4 GHz, two of them on channel 6 heard together everywhere
        points = [Point(x=i, y=0, networks=[
            net("Corp", "00:00:00:00:01:01", -50, 2412),
            net("Corp", "00:00:00:00:02:01", -55, 2437),
            net("Corp", "00:00:00:00:03:01", -60, 2437),
        ]) for i in range(4)]
        plan = rfplan.channel_plan(Project(points=points), "Corp")
        self.assertEqual(sum(s.changed for s in plan), 1)
        self.assertEqual(sum(s.clashes_after for s in plan), 0)    # channel 11 was free


    def test_report_sections(self) -> None:
        points = [Point(x=i, y=0, networks=[
            net("Corp", "00:00:00:00:01:01", -55, 5180, 5210, 80),
            net("Corp", "00:00:00:00:02:01", -60, 5180, 5210, 80),
            net("Corp", "00:00:00:00:03:01", -60, 2427, basic_rates=[1.0]),
        ]) for i in range(2)]
        html = report.build_html(Project(points=points), None, lambda _p: None, "voice", "Corp")
        for expected in ("Voice requirement", "2 of 2 points", "Configuration audit",
                         "802.11b rates still required", "Suggested channel plan for Corp"):
            self.assertIn(expected, html)


if __name__ == "__main__":
    unittest.main()
