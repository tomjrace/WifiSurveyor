"""The survey setup's types, the Spectrum page's range choices, and the checkbox lists."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from wifisurveyor.core import surveysetup as ss
from wifisurveyor.core.project import Project
from wifisurveyor.rf import bands as rfbands


class SetupTest(unittest.TestCase):
    def test_every_type_matches_itself_and_nothing_else(self):
        for key, _name, _about, _setup in ss.TYPES:
            if key == "custom":
                continue
            self.assertEqual(ss.matching_kind(ss.preset(key)), key)
        changed = ss.preset("standard")
        changed.sweeps = 5
        self.assertEqual(ss.matching_kind(changed), "custom")

    def test_production_type_ticks_the_mic_bands(self):
        event = ss.preset("event")
        self.assertIn("tv_uhf", event.bands)
        self.assertIn("als72", event.bands)                 # venue: assistive listening
        self.assertEqual(sorted(event.band_groups), ["mics", "production", "venue"])
        self.assertIn("other range", ss.describe(event))

    def test_thoroughness_costs_time(self):
        quick = ss.seconds_per_point(ss.preset("quick"), analyzers=1, hackrf=True)
        thorough = ss.seconds_per_point(ss.preset("thorough"), analyzers=1, hackrf=True)
        self.assertLess(quick, 15)
        self.assertGreater(thorough, 2 * quick)
        # Two analyzers share the sweeping
        self.assertLess(ss.seconds_per_point(ss.preset("event"), 2), ss.seconds_per_point(ss.preset("event"), 1))

    def test_unknown_values_are_dropped(self):
        setup = ss.Setup.from_dict({"kind": "event", "bands": ["tv_uhf", "gone"], "wifi_ranges": [0, 9],
                                    "future_option": True})
        self.assertEqual(setup.bands, ["tv_uhf"])
        self.assertEqual(setup.wifi_ranges, [0])

    def test_kept_with_the_survey_and_its_follow_up(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": folder}):
            project = Project(name="Hall", survey_setup=ss.preset("event").to_dict())
            path = project.save(os.path.join(folder, "hall.wsurvey"))
            loaded = Project.load(path)
            self.assertEqual(loaded.survey_setup["kind"], "event")
            self.assertEqual(loaded.follow_up().survey_setup, loaded.survey_setup)


class SharedChoiceTest(unittest.TestCase):
    """The Spectrum page's ticked ranges are what the survey measures, and the other way round."""

    def setUp(self):
        from wifisurveyor.core import rangechoice

        self.rc = rangechoice
        self.folder = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.folder.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_wifi_until_something_else_is_chosen(self):
        wifi, bands, extras, groups = self.rc.setup_choice()
        self.assertEqual((wifi, bands, extras, groups), ([0, 1, 2, 3], [], [], []))

    def test_a_survey_type_ticks_its_ranges_on_the_spectrum_page(self):
        event = ss.preset("event")
        self.rc.choose_for_setup(event.wifi_ranges, event.bands, event.extra_ranges, event.band_groups)
        wifi, bands, extras, groups = self.rc.setup_choice()
        self.assertEqual(wifi, event.wifi_ranges)
        self.assertEqual(sorted(bands), sorted(event.bands))
        self.assertEqual(extras, [])
        self.assertEqual(sorted(groups), sorted(event.band_groups))
        self.assertIn(self.rc.WIFI_SET, self.rc.chosen_sets())
        # So the survey still reads as the type it was set to
        back = ss.Setup(kind="event", wifi_ranges=wifi, bands=bands, band_groups=groups,
                        extra_ranges=extras, sweeps=event.sweeps, dwell_count=event.dwell_count,
                        bluetooth=True)
        self.assertEqual(ss.matching_kind(back), "event")

    def test_ranges_that_arent_bands_are_kept_as_they_are(self):
        overview = self.rc.OVERVIEWS["mics"][0]
        bands = {self.rc.range_key(self.rc.band_range(b)) for b in rfbands.BANDS}
        cellular = next(r for name, ranges in self.rc.band_sets() if name == self.rc.CELLULAR_SET
                        for r in ranges if self.rc.range_key(r) not in bands)
        telemetry = self.rc.band_range(rfbands.BY_KEY["wmts1395"])
        self.rc.choose(["Wi-Fi"], [self.rc.range_key(r) for r in (overview, cellular, telemetry)])
        wifi, bands, extras, _groups = self.rc.setup_choice()
        self.assertEqual(wifi, [])                          # unticking Wi-Fi is a choice too
        self.assertEqual(bands, ["wmts1395"])
        self.assertEqual([e[0] for e in extras], [overview[0], cellular[0]])
        # Written back from the survey's side, the sets that list them are ticked with them
        self.rc.choose_for_setup(wifi, bands, extras, [])
        self.assertIn(self.rc.CELLULAR_SET, self.rc.chosen_sets())
        self.assertEqual(len(self.rc.chosen()), 3)

    def test_the_setup_carries_other_ranges_and_counts_their_time(self):
        plain = ss.preset("standard")
        more = ss.Setup.from_dict(dict(plain.to_dict(), kind="custom",
                                       extra_ranges=[["UHF overview", 470, 663]]))
        self.assertEqual(more.extra_ranges, [["UHF overview", 470.0, 663.0]])
        self.assertGreater(ss.seconds_per_point(more, 1), ss.seconds_per_point(plain, 1))
        self.assertIn("1 other range", ss.describe(more))
        self.assertEqual(ss.matching_kind(more), "custom")


class SurveyPointRangesTest(unittest.TestCase):
    """A survey point sweeps the other ranges ticked, and keeps each one's trace."""

    def test_bands_are_summarised_and_every_range_kept(self):
        import threading

        import numpy as np

        from wifisurveyor.rf.analyzer import Trace
        from wifisurveyor.ui import survey_view

        class Analyzer:
            name = model = "tinySA Ultra+"
            min_mhz, max_mhz = 0.1, 7300.0
            lock = threading.RLock()

            def scan(self, low, high, step_khz, sweeps):
                freqs = np.linspace(low, high, 450)
                levels = np.full(450, -100.0)
                levels[200] = -50.0
                return Trace(freqs, levels, levels + 2, 30.0)

        session = mock.Mock(band_analyzers=lambda: [Analyzer()])
        found, traces, notes = survey_view.measure_other_ranges(
            session, [rfbands.BY_KEY["wmts1395"]], [["UHF mics overview", 470.0, 663.0]],
            lambda *_a: None)
        self.assertEqual(notes, [])
        self.assertEqual(list(found), ["wmts1395"])          # only real bands get a summary
        self.assertEqual(found["wmts1395"]["analyzer"], "tinySA Ultra+")
        self.assertEqual([t["name"] for t in traces], ["UHF mics overview", "Medical telemetry 1395-1400 MHz"])
        self.assertEqual((traces[0]["start_mhz"], traces[0]["stop_mhz"]), (470.0, 663.0))
        self.assertEqual(len(traces[0]["avg"]), 450)
        self.assertEqual(traces[1]["max"][200], -48.0)

    def test_without_an_analyzer_it_says_so(self):
        from wifisurveyor.ui import survey_view

        session = mock.Mock(band_analyzers=lambda: [])
        found, traces, notes = survey_view.measure_other_ranges(
            session, [rfbands.BY_KEY["wmts1395"]], [], lambda *_a: None)
        self.assertIsNone(found)
        self.assertEqual(traces, [])
        self.assertIn("weren't measured", notes[0])


class RangeChoiceTest(unittest.TestCase):
    def setUp(self):
        from wifisurveyor.ui import spectrum_view

        self.sv = spectrum_view

    def test_nearby_ranges_share_a_panel(self):
        chosen = [("900 MHz ISM", 902.0, 928.0), ("941-960 MHz", 941.0, 960.0),
                  ("DECT", 1920.0, 1930.0), ("UHF TV", 470.0, 608.0), ("Guard band", 614.0, 616.0)]
        merged = self.sv.merge_ranges(chosen)
        self.assertEqual([(round(s), round(e)) for _t, s, e in merged],
                         [(470, 616), (902, 960), (1920, 1930)])
        self.assertIn("UHF TV", merged[0][0])
        self.assertEqual(merged[2][0], "DECT")

    def test_frequency_steps_are_round_and_fit(self):
        self.assertEqual(self.sv.nice_step(138, 8), 20)
        self.assertEqual(self.sv.nice_step(100, 10), 10)
        self.assertEqual(self.sv.nice_step(2.0, 8), 0.25)
        self.assertEqual(self.sv.freq_label(470.0, 20), "470")
        self.assertEqual(self.sv.freq_label(915.25, 0.25), "915.25")
        self.assertEqual(self.sv.freq_label(2412.5, 0.5), "2412.5")

    def test_every_set_has_ranges_and_wifi_comes_first(self):
        sets = self.sv.band_sets()
        self.assertEqual(sets[0][0], self.sv.WIFI_SET)
        self.assertEqual(len(sets[0][1]), 4)
        self.assertEqual(len(sets), len(rfbands.GROUPS) + 2)
        self.assertTrue(all(ranges for _name, ranges in sets))


def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        return bool(Gtk.init_check())
    except Exception:
        return False


@unittest.skipUnless(_display(), "needs a display")
class CheckListTest(unittest.TestCase):
    def test_all_follows_the_items_and_sets_them(self):
        from wifisurveyor.ui.choosers import CheckList

        changes = []
        check = CheckList(changes.append)
        check.set_items([("Wi-Fi", [("a", "A"), ("b", "B")]), ("Mics", [("b", "B again"), ("c", "C")])],
                        ["a"])
        self.assertEqual(check.keys(), ["a", "b", "c"])          # "b" listed once
        self.assertTrue(check.all.get_inconsistent())
        check.all.set_active(True)
        self.assertEqual(changes[-1], ["a", "b", "c"])
        check._checks["c"].set_active(False)
        self.assertEqual(changes[-1], ["a", "b"])
        self.assertFalse(check.all.get_active())
        check.set_selected([])                                    # by code: no change reported
        self.assertEqual(len(changes), 2)


if __name__ == "__main__":
    unittest.main()
