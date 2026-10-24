import threading
import time
import unittest
from types import SimpleNamespace

from wifisurveyor.core import report
from wifisurveyor.core.project import Point, Project
from wifisurveyor.rf import spectrum as spec
from wifisurveyor.rf.analyzer import Analyzer, Config


def config(start, end, points=112, rbw=100):
    return SimpleNamespace(start_mhz=start, step_mhz=(end - start) / (points - 1), rbw_khz=rbw)


class DwellTests(unittest.TestCase):
    def test_busy_and_quiet_sweeps(self) -> None:
        cfg = config(5168, 5252)
        quiet = [-105.0] * 112
        busy = [-80.0 if 5170 <= cfg.start_mhz + i * cfg.step_mhz <= 5250 else -105.0
                for i in range(112)]
        dwell = spec.summarize_dwell(cfg, [quiet] * 6 + [busy] * 4, 5210, 80)
        self.assertAlmostEqual(dwell["airtime"], 0.4)
        # -80 dBm per 100 kHz slice over 80 MHz is about -51 dBm for the channel
        self.assertAlmostEqual(dwell["active_dbm"], -51, delta=1.5)
        self.assertAlmostEqual(dwell["noise_20mhz"], -105 + 23, delta=0.5)

    def test_airtime_counts_moments_not_sweeps(self) -> None:
        # A slow analyzer: each sweep takes long enough that every one catches some frames
        import random

        random.seed(4)
        cfg = config(2405, 2429, points=450)
        sweeps = [[-87.0 if random.random() < 0.2 else -103.0 for _ in range(450)]
                  for _ in range(3)]
        dwell = spec.summarize_dwell(cfg, sweeps, 2417, 20)
        self.assertAlmostEqual(dwell["airtime"], 0.2, delta=0.04)

    def test_channels_chosen_with_the_selected_network_first(self) -> None:
        networks = [
            {"ssid": "A", "rssi": -40, "freq_mhz": 5180, "center_mhz": 5210, "width_mhz": 80},
            {"ssid": "B", "rssi": -50, "freq_mhz": 5200, "center_mhz": 5200, "width_mhz": 20},
            {"ssid": "C", "rssi": -70, "freq_mhz": 2437},
            {"ssid": "D", "rssi": -80, "freq_mhz": 5745, "center_mhz": 5775, "width_mhz": 80},
        ]
        self.assertEqual(spec.dwell_channels(networks, 2), [(5210, 80), (2437, 20)])
        self.assertEqual(spec.dwell_channels(networks, 2, "D"), [(5775, 80), (5210, 80)])
        self.assertEqual(spec.dwell_channels(networks, 0), [])

    def test_dwell_is_preferred_and_reported(self) -> None:
        cfg = config(5168, 5252)
        dwell = spec.summarize_dwell(cfg, [[-100.0] * 112] * 3, 5210, 80)
        wide = spec.build_segment(config(5150, 5350), [[-95.0] * 112] * 3)
        point = Point(x=0, y=0, spectrum={"segments": [wide], "dwells": [dwell]})
        self.assertIs(spec.segment_for(point.spectrum, 5180), dwell)
        self.assertIs(spec.segment_for(point.spectrum, 5300), wide)
        project = Project(points=[point])
        html = report.build_html(project, None, lambda _p: None)
        self.assertIn("Channels watched closely", html)
        self.assertIn("5210 MHz", html)


class SlowAnalyzer(Analyzer):
    """Sweeps arrive every ``interval`` seconds once tuned, like a tinySA over a channel."""

    def __init__(self, interval: float, partial: bool) -> None:
        super().__init__("fake")
        self.first_sweep_partial = partial
        self.interval = interval
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def alive(self) -> bool:
        return self.running

    def _loop(self) -> None:
        while self.running:
            time.sleep(self.interval)
            if self.config is not None:
                self._add_sweep([-100.0] * self.config.points)

    def _configure(self, start_mhz, end_mhz, timeout):
        self.config = Config(start_mhz=start_mhz, step_mhz=(end_mhz - start_mhz) / 99, points=100,
                             expansion=False, min_mhz=0, max_mhz=6000, max_span_mhz=500, rbw_khz=300)
        return self.config


class SlowDwellTests(unittest.TestCase):
    def test_a_dwell_shorter_than_two_sweeps_still_returns_sweeps(self) -> None:
        for partial in (True, False):
            analyzer = SlowAnalyzer(0.3, partial)
            try:
                _config, sweeps = analyzer.dwell(2417, 0.2, 20, timeout=5)
            finally:
                analyzer.running = False
            self.assertGreaterEqual(len(sweeps), 2)

    def test_a_dead_analyzer_still_times_out(self) -> None:
        analyzer = SlowAnalyzer(0.05, True)
        analyzer.running = False
        with self.assertRaises(TimeoutError):
            analyzer.dwell(2417, 0.1, 20, timeout=5)


if __name__ == "__main__":
    unittest.main()
