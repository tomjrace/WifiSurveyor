import unittest

from wifisurveyor.core import report
from wifisurveyor.core.analysis import access_points
from wifisurveyor.core.project import Point, Project


def network(ssid, rssi, freq=5180, channel=36, **extra):
    return {"ssid": ssid, "bssid": f"AA:{rssi}", "rssi": rssi, "freq_mhz": freq,
            "channel": channel, "band": "5", "rssi_source": "iw", **extra}


class ReportTests(unittest.TestCase):
    def test_access_points_keep_the_strongest_per_channel(self) -> None:
        aps = access_points([network("Home", -60), network("Home", -50, width_mhz=80,
                                                           center_mhz=5210),
                             network("Cafe", -70, freq=5745, channel=149)])
        self.assertEqual([(ap["ssid"], ap["rssi"], ap["width"]) for ap in aps],
                         [("Home", -50, 80), ("Cafe", -70, 20)])

    def test_every_bssid_keeps_separate_aps_on_one_channel(self) -> None:
        aps = access_points([network("Home", -56, freq=2462, channel=11),
                             network("Home", -62, freq=2462, channel=11),
                             network("Home", -63, freq=2462, channel=11)], every_bssid=True)
        self.assertEqual([ap["rssi"] for ap in aps], [-56, -62, -63])

    def test_report_holds_each_point_and_its_spectrum_picture(self) -> None:
        project = Project(name="Office <1>")
        project.points = [
            Point(x=1, y=1, networks=[network("Home", -50)], spectrum={"segments": []}),
            Point(x=2, y=2, networks=[network("Home", -80)]),
        ]
        asked = []

        def picture(point):
            asked.append(point)
            return b"\x89PNG"

        html = report.build_html(project, b"\x89PNG", picture)
        self.assertIn("Office &lt;1&gt;", html)
        self.assertIn("Point 2", html)
        self.assertEqual(asked, [project.points[0]])       # only points with a sweep
        self.assertEqual(html.count("<img"), 2)             # the map and one spectrum
        self.assertIn("Weak coverage", html)
        self.assertIn('querySelectorAll("section.point table")', html)   # sortable

    def test_each_range_swept_is_pictured_folded_away(self) -> None:
        project = Project(name="Hall")
        telemetry = {"name": "Medical telemetry 1395-1400 MHz", "start_mhz": 1395.0,
                     "stop_mhz": 1400.0, "analyzer": "tinySA Ultra+", "avg": [-100.0] * 450,
                     "max": [-95.0] * 450}
        project.points = [Point(x=1, y=1, networks=[network("Home", -50)], spectrum={"segments": []},
                                ranges=[telemetry, dict(telemetry, name="DECT", start_mhz=1920.0,
                                                        stop_mhz=1930.0)])]
        drawn = []
        html = report.build_html(project, b"\x89PNG", lambda _p: b"\x89PNG",
                                 range_png=lambda record: drawn.append(record["name"]) or b"\x89PNG")
        self.assertEqual(drawn, ["Medical telemetry 1395-1400 MHz", "DECT"])
        # Closed until someone opens it, Wi-Fi first, then each range with where it was
        self.assertIn('<details class="sweeps"><summary>', html)
        self.assertNotIn('<details class="sweeps" open', html)
        self.assertIn("<summary>Wi-Fi</summary>", html)     # no bands drawn: the one picture
        self.assertIn('Medical telemetry 1395-1400 MHz <span class="muted">1395-1400 MHz · '
                      'tinySA Ultra+</span>', html)
        self.assertEqual(html.count("<figure>"), 3)
        self.assertIn('addEventListener("beforeprint"', html)    # printing shows them all

    def test_each_wifi_band_folds_on_its_own(self) -> None:
        project = Project(name="Hall")
        project.points = [Point(x=1, y=1, networks=[network("Home", -50)],
                                spectrum={"segments": [], "noise_5": -91.2})]
        asked = []
        html = report.build_html(project, b"\x89PNG", lambda _p: b"\x89PNG",
                                 band_png=lambda _p, band: asked.append(band) or (
                                     b"\x89PNG" if band != "6" else None))
        self.assertEqual(asked, ["2.4", "5", "6"])
        self.assertIn("<summary>2.4 GHz", html)
        self.assertIn('5 GHz <span class="muted">noise floor -91 dBm · 1 access point</span>', html)
        self.assertNotIn("<summary>6 GHz", html)          # not swept: not shown
        self.assertNotIn("<summary>Wi-Fi</summary>", html)

    def test_sections_and_points_fold_with_expand_and_collapse_all(self) -> None:
        project = Project(name="Hall")
        project.points = [Point(x=1, y=1, networks=[network("Home", -50)])]
        html = report.build_html(project, b"\x89PNG", lambda _p: None)
        self.assertIn('data-fold="open">Expand all', html)
        self.assertIn('data-fold="close">Collapse all', html)
        self.assertEqual(html.count("<h2"), html.count('<details class="fold section" open>'
                                                       '<summary><h2'))
        self.assertIn('<details class="fold point-fold" open><summary>', html)
        # Every fold closed again: no section swallowed another
        self.assertEqual(html.count("<details"), html.count("</details>"))

    def test_a_point_without_sweeps_has_no_sweeps_section(self) -> None:
        project = Project(name="Hall")
        project.points = [Point(x=1, y=1, networks=[network("Home", -50)])]
        html = report.build_html(project, b"\x89PNG", lambda _p: None, range_png=lambda _r: None)
        self.assertNotIn('<details class="sweeps">', html)


if __name__ == "__main__":
    unittest.main()


class ScaleAndAdviceTests(unittest.TestCase):
    def test_scale_turns_pixels_into_feet(self) -> None:
        project = Project(width=1000, height=500)
        self.assertIsNone(project.feet(100))
        project.set_scale(200, 10)                   # 200 px is 10 ft
        self.assertAlmostEqual(project.feet(1000), 50)
        self.assertAlmostEqual(project.pixels(5), 100)

    def test_weak_spot_gets_high_severity_advice(self) -> None:
        from wifisurveyor.core import advice, analysis

        project = Project()
        project.points = [Point(x=1, y=1, networks=[network("Home", -80)])]
        found = advice.improvements(project.points[0], analysis.Context(project))
        self.assertEqual(found[0].severity, "High")
        self.assertEqual(found[0].title, "Weak signal")
        self.assertEqual(advice.quality(found), "poor")

    def test_report_pins_each_point_and_never_says_fine(self) -> None:
        project = Project(width=100, height=50)
        project.set_scale(100, 50)
        project.points = [Point(x=10, y=10, networks=[network("Home", -50)]),
                          Point(x=90, y=40, networks=[network("Home", -75), network("Cafe", -60)])]
        html = report.build_html(project, b"\x89PNG", lambda _p: None)
        self.assertIn('href="#point-2"', html)
        self.assertIn('id="point-1"', html)
        self.assertIn("left:90.00%;top:80.00%", html)
        self.assertNotIn("Fine", html)
        self.assertIn("What to improve", html)


class ColourTests(unittest.TestCase):
    def test_one_colour_per_network_name(self) -> None:
        from wifisurveyor.core.colours import signal_quality, ssid_colour

        self.assertEqual(ssid_colour("Same"), ssid_colour("Same"))
        self.assertNotEqual(ssid_colour("First"), ssid_colour("Second"))
        self.assertEqual([signal_quality(r, -70) for r in (-60, -72, -80)], ["good", "fair", "poor"])


class OverviewTests(unittest.TestCase):
    def test_overview_recommends_roaming_overlap_and_links_nothing(self) -> None:
        project = Project(width=100, height=50)
        project.points = [Point(x=10, y=10, networks=[network("Home", -60)]),
                          Point(x=90, y=40, networks=[network("Home", -62)])]
        html = report.build_html(project, b"\x89PNG", lambda _p: None, overview_only=True)
        self.assertIn("Overview and recommendations", html)
        self.assertIn("second access point", html)
        self.assertIn("Users will notice", html)
        self.assertNotIn('href="#point-', html)            # no point sections to jump to
        self.assertNotIn('id="point-1"', html)

    def test_every_advice_title_has_a_theme(self) -> None:
        import re
        from pathlib import Path

        from wifisurveyor.core import advice, overview

        source = Path(advice.__file__).read_text()
        titles = set(re.findall(r'add\([^,]+, "([^"]+)"', source))
        self.assertTrue(titles)
        self.assertEqual(titles - set(overview.TITLE_THEMES), set())
