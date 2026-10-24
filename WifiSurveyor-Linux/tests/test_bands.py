"""The bands beyond Wi-Fi, mic frequency coordination, and the generic fine scan."""

from __future__ import annotations

import itertools
import threading
import unittest

import numpy as np

from wifisurveyor.core import analysis, report
from wifisurveyor.core.project import Point, Project
from wifisurveyor.rf import bands as rfbands
from wifisurveyor.rf import coordination as coord
from wifisurveyor.rf.analyzer import Analyzer, Config


def flat(low: float, high: float, step: float = 0.025, floor: float = -100.0, seed: int = 1):
    freqs = np.arange(low, high + step / 2, step)
    rng = np.random.default_rng(seed)
    return freqs, floor + rng.random(freqs.size)


class CatalogTest(unittest.TestCase):
    def test_every_band_is_sane(self):
        groups = {key for key, _n, _a in rfbands.GROUPS}
        keys = [b.key for b in rfbands.BANDS]
        self.assertEqual(len(keys), len(set(keys)))
        for band in rfbands.BANDS:
            self.assertLess(band.low, band.high, band.key)
            self.assertTrue(set(band.groups) <= groups, band.key)
            self.assertIn(band.licence, (rfbands.UNLICENSED, rfbands.LICENSED, rfbands.AVOID,
                                         rfbands.LISTEN))
        for key in groups:
            self.assertTrue(rfbands.in_group(key), key)

    def test_groups_combine_without_repeats(self):
        both = rfbands.for_groups(["mics", "production"])
        self.assertEqual(len(both), len({b.key for b in both}))
        self.assertIn(rfbands.BY_KEY["ism900"], both)
        self.assertEqual([b.low for b in both], sorted(b.low for b in both))

    def test_mic_rules_since_the_repack(self):
        # Channel 37 and the 600 MHz downlink are no-go; the duplex gap's lower part is licensed
        self.assertEqual(coord.legal_ranges(608, 614), [])
        self.assertEqual(coord.legal_ranges(617, 652), [])
        self.assertEqual(coord.legal_ranges(653, 663), [(657.0, 663.0)])
        self.assertEqual(coord.legal_ranges(653, 663, licensed=True), [(653.0, 663.0)])
        self.assertEqual(coord.legal_ranges(470, 616), [(470.0, 608.0), (614.0, 616.0)])
        self.assertTrue(coord.mic_spectrum_at(615.0, False))
        self.assertFalse(coord.mic_spectrum_at(611.0, True))

    def test_tv_channels(self):
        channels = rfbands.tv_channels(470, 608)
        self.assertEqual(channels[0], (14, 470.0, 476.0))
        self.assertEqual(channels[-1], (36, 602.0, 608.0))
        self.assertEqual(rfbands.tv_channels(174, 216)[0], (7, 174.0, 180.0))


class SummaryTest(unittest.TestCase):
    def test_station_signal_and_open_channels(self):
        freqs, levels = flat(470, 608)
        levels[(freqs >= 500) & (freqs < 506)] = -70          # a DTV station on 19
        levels[np.abs(freqs - 500.309) < 0.02] = -55          # with an ATSC 1.0 pilot
        levels[np.abs(freqs - 532.0) < 0.1] = -60             # a mic on channel 24
        summary = rfbands.summarize(freqs, levels, [rfbands.BY_KEY["tv_uhf"]], 25)["tv_uhf"]
        states = {c["channel"]: c for c in summary["tv"]}
        self.assertEqual(states[19]["state"], "station")
        self.assertEqual(states[19]["standard"], "ATSC 1.0")
        self.assertEqual(states[24]["state"], "busy")
        self.assertEqual(states[30]["state"], "open")
        self.assertAlmostEqual(summary["peak_dbm"], -55, places=0)
        self.assertGreater(rfbands.activity(summary), 40)
        self.assertEqual(rfbands.grade(summary), "poor")
        text = rfbands.advice(rfbands.BY_KEY["tv_uhf"], summary)
        self.assertIn("channel 19", text)
        self.assertIn("open channels", text)

    def test_strongest_signals_are_separate_runs(self):
        freqs, levels = flat(902, 928)
        levels[np.abs(freqs - 915.0) < 0.06] = -50
        levels[np.abs(freqs - 908.42) < 0.03] = -65
        found = rfbands.strongest_signals(freqs, levels, -100)
        self.assertEqual([round(s["mhz"], 1) for s in found], [915.0, 908.4])
        self.assertGreater(found[0]["width_khz"], 50)

    def test_quiet_band_advice_still_says_what_to_do(self):
        freqs, levels = flat(433, 435)
        entry = rfbands.summarize(freqs, levels, [rfbands.BY_KEY["ism433"]])["ism433"]
        self.assertEqual(rfbands.grade(entry), "good")
        self.assertIn("recheck", rfbands.advice(rfbands.BY_KEY["ism433"], entry))

    def test_sharing_bands_between_analyzers(self):
        class Fake:
            def __init__(self, name, low, high):
                self.name, self.min_mhz, self.max_mhz = name, low, high

        rtl, tiny = Fake("rtl", 24, 1766), Fake("tiny", 0.1, 5300)
        wanted = [rfbands.BY_KEY[k] for k in ("tv_uhf", "ism900", "ism24", "als72")]
        shared = dict((a.name, [b.key for b in mine]) for a, mine in rfbands.share_bands(wanted, [rtl, tiny]))
        self.assertIn("ism24", shared["tiny"])                 # only the tinySA reaches it
        self.assertEqual(sorted(sum(shared.values(), [])), sorted(b.key for b in wanted))
        self.assertEqual(rfbands.out_of_reach([rfbands.BY_KEY["mic7g"]], [rtl, tiny]),
                         [rfbands.BY_KEY["mic7g"]])


class CoordinationTest(unittest.TestCase):
    def assert_compatible(self, freqs, profile):
        for a, b in itertools.combinations(freqs, 2):
            self.assertGreaterEqual(abs(a - b) + 1e-9, profile.spacing_khz / 1000, (a, b))
        for p, guard, kind in coord.products(freqs, profile):
            for f in freqs:
                self.assertGreaterEqual(abs(p - f) + 1e-9, guard, (kind, p, f))

    def test_picks_are_quiet_legal_and_intermod_free(self):
        freqs, levels = flat(470, 616)
        levels[(freqs >= 512) & (freqs < 518)] = -65          # a station on 21
        levels[np.abs(freqs - 490.0) < 0.15] = -60            # something on 490
        profile = coord.PROFILE_BY_KEY["analog"]
        plan = coord.coordinate(freqs, levels, 470, 534, 16, profile, fixed=[480.0])
        picks = [p["mhz"] for p in plan.picks]
        self.assertEqual(len(picks), 16)
        self.assertEqual(plan.stations, [21])
        self.assertFalse(any(512 <= f < 518 for f in picks))
        self.assertFalse(any(abs(f - 490.0) < 0.3 for f in picks))
        self.assert_compatible([480.0, *picks], profile)
        # Everything picked passes the check against the same scan
        self.assertTrue(all(not r["problems"] for r in coord.check([480.0, *picks], freqs, levels, profile)))

    def test_dense_profile_fits_more(self):
        freqs, levels = flat(572, 616)
        loose = coord.coordinate(freqs, levels, 572, 616, 60, coord.PROFILE_BY_KEY["iem"])
        dense = coord.coordinate(freqs, levels, 572, 616, 60, coord.PROFILE_BY_KEY["dense"])
        self.assertGreater(len(dense.picks), len(loose.picks))
        self.assertTrue(loose.notes)                           # says it couldn't fit them all
        self.assertTrue(all(608 > p["mhz"] or p["mhz"] >= 614 for p in dense.picks))

    def test_check_names_the_problems(self):
        freqs, levels = flat(470, 664)
        levels[(freqs >= 500) & (freqs < 506)] = -65
        profile = coord.PROFILE_BY_KEY["analog"]
        results = {r["mhz"]: r["problems"] for r in coord.check(
            [500.5, 610.0, 655.0, 520.0, 540.0, 540.2, 560.0], freqs, levels, profile)}
        self.assertTrue(any("TV channel 19" in p for p in results[500.5]))
        self.assertIn("outside US wireless mic spectrum", results[610.0])
        self.assertIn("needs a Part 74 licence", results[655.0])
        self.assertTrue(any("product" in p for p in results[520.0]))     # 2 x 540.0 - 560.0
        self.assertTrue(any("within" in p for p in results[540.0]))

    def test_parse_frequencies(self):
        self.assertEqual(coord.parse_frequencies("470.125, 512.5\n600"), [470.125, 512.5, 600.0])
        with self.assertRaises(ValueError):
            coord.parse_frequencies("470 abc")


class ScanTest(unittest.TestCase):
    def test_generic_scan_pieces_to_the_resolution(self):
        class Swept(Analyzer):
            name = "Fake"
            points = 101

            def __init__(self):
                super().__init__("/dev/fake")
                self.asked = []

            @property
            def alive(self):
                return True

            def close(self):
                pass

            @property
            def max_mhz(self):
                return 3000.0

            def _configure(self, start, end, timeout):
                self.asked.append((start, end))
                step = (end - start) / (self.points - 1)
                self.config = Config(start, step, self.points, False, 1, 3000, 1000, step * 1000)

                def feed():
                    for n in range(4):
                        self._add_sweep([-90.0 - n] * self.points)

                threading.Timer(0.05, feed).start()
                return self.config

        fake = Swept()
        fake.config = Config(0, 1, 101, False, 1, 3000, 1000, 1000)
        trace = fake.scan(470, 520, 25, sweeps=2, timeout=5)
        self.assertEqual(len(fake.asked), 20)                  # 50 MHz at 25 kHz, 100 bins a piece
        self.assertAlmostEqual(trace.freqs[0], 470)
        self.assertAlmostEqual(trace.freqs[-1], 520)
        self.assertTrue(np.all(np.diff(trace.freqs) >= 0))
        self.assertTrue(np.all(trace.peak >= trace.avg - 1e-9))


class ReportTest(unittest.TestCase):
    def test_other_bands_in_the_report_and_on_the_map(self):
        freqs, levels = flat(470, 608)
        levels[(freqs >= 500) & (freqs < 506)] = -70
        quiet = rfbands.summarize(*flat(902, 928), [rfbands.BY_KEY["ism900"]])
        busy_freqs, busy_levels = flat(902, 928)
        busy_levels[np.abs(busy_freqs - 915) < 0.1] = -50
        busy = rfbands.summarize(busy_freqs, busy_levels, [rfbands.BY_KEY["ism900"]])
        tv = rfbands.summarize(freqs, levels, [rfbands.BY_KEY["tv_uhf"]])
        project = Project(points=[Point(x=10, y=10, bands={**quiet, **tv}),
                                  Point(x=50, y=50, bands={**busy, **tv})])
        context = analysis.Context(project)
        context.other_band = "ism900"
        self.assertLess(analysis.metric(project.points[0], "bands", context), 10)
        self.assertGreater(analysis.metric(project.points[1], "bands", context), 25)
        html = report._other_bands(project, lambda n: f"[{n}]")
        self.assertIn("Other bands", html)
        self.assertIn("Stations on 19", html)
        self.assertIn("[2]", html)                              # point 2 is where 900 MHz is busy
        self.assertIn("q-poor", html)


if __name__ == "__main__":
    unittest.main()
