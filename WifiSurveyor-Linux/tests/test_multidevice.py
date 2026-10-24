"""Any mix of adapters and analyzers: sharing the work between them, and each detection once."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import report, settings
from wifisurveyor.core.project import Point, Project
from wifisurveyor.net import bluetooth, trackers
from wifisurveyor.rf import analyzer, detect, hackrf, iq


class FakeAnalyzer:
    def __init__(self, name: str, top: float, device: str = "") -> None:
        self.name = self.model = name
        self.max_mhz = top
        self.device = device or f"/dev/{name}"
        self.alive = True
        self.closed = False
        self.sending = False
        self.unit = name

    def close(self) -> None:
        self.closed = True
        self.alive = False


class SharingTests(unittest.TestCase):
    def test_ranges_go_where_they_can_be_reached_and_are_balanced(self) -> None:
        tinysa = FakeAnalyzer("tinySA Ultra+", 7300)
        explorer = FakeAnalyzer("RF Explorer", 6100)
        pool = [tinysa, explorer]
        ranges = analyzer.ranges_for(pool)
        self.assertEqual(ranges[-1], ("6 GHz", 5925.0, 7125.0))      # as far as any reaches
        jobs = analyzer.share_ranges(ranges, pool)
        given = {job[0]: who.name for who, job in jobs}
        # Only the tinySA reaches the top of 6 GHz; the rest is balanced between them
        self.assertEqual(given["6 GHz"], "tinySA Ultra+")
        self.assertEqual({who.name for who, _job in jobs}, {"tinySA Ultra+", "RF Explorer"})
        # The same pool always shares the same way, so each range keeps its analyzer
        self.assertEqual([(w.name, j) for w, j in analyzer.share_ranges(ranges, pool)],
                         [(w.name, j) for w, j in jobs])
        # In frequency order, for building the point's spectrum
        self.assertEqual([j[1] for _w, j in jobs], sorted(j[1] for _w, j in jobs))

    def test_a_range_nobody_reaches_all_of_is_cut_to_what_one_does(self) -> None:
        explorer = FakeAnalyzer("RF Explorer", 6100)
        jobs = analyzer.share_ranges((("6 GHz", 5925.0, 7125.0),), [explorer])
        self.assertEqual(jobs, [(explorer, ("6 GHz", 5925.0, 6100))])
        self.assertEqual(analyzer.share_ranges((("high", 6500.0, 7000.0),), [explorer]), [])

    def test_one_analyzer_does_everything(self) -> None:
        only = FakeAnalyzer("tinySA", 6000)
        jobs = analyzer.share_ranges(analyzer.ranges_for([only]), [only])
        self.assertTrue(all(who is only for who, _job in jobs))
        self.assertEqual(len(jobs), 4)

    def test_channels_watched_closely_are_shared(self) -> None:
        a, b = FakeAnalyzer("A", 6000), FakeAnalyzer("B", 2700)
        channels = [(2437.0, 20.0), (2462.0, 20.0), (5180.0, 80.0), (2412.0, 20.0)]
        jobs = analyzer.share_channels(channels, [a, b])
        self.assertEqual([(w.name, c[0]) for w, c in jobs],
                         [("A", 2437.0), ("B", 2462.0), ("A", 5180.0), ("B", 2412.0)])
        self.assertEqual(next(w.name for w, c in jobs if c[0] == 5180.0), "A")   # B can't reach it


class FakeRadio(FakeAnalyzer):
    """A HackRF stand-in for the work shared between several."""

    def __init__(self, name: str, fail_at: float | None = None, loud: bool = False) -> None:
        super().__init__(name, 7250)
        self.fail_at, self.loud = fail_at, loud
        self.heard: list[float] = []
        self.busy = threading.Lock()
        self.overlapped = False

    def identify(self, centre: float) -> dict:
        self.heard.append(centre)
        if centre == self.fail_at:
            raise RuntimeError("The HackRF isn't answering.")
        time.sleep(0.05)
        return {"center_mhz": centre, "airtime": 0.1, "findings": [],
                "clipped": 0.01 if self.loud else 0.0}

    def watch_pulses(self, centre: float, seconds: float, stop) -> iq.PulseWatch:
        self.heard.append((centre, seconds))
        if centre == self.fail_at:
            raise RuntimeError("The HackRF isn't answering.")
        watch = iq.PulseWatch()
        watch.position = 1000
        return watch


class HackRfSharingTests(unittest.TestCase):
    def test_the_radar_watch_shares_channels_and_gives_each_longer(self) -> None:
        one, two = FakeRadio("HackRF A"), FakeRadio("HackRF B")
        result = hackrf.watch_dfs([one, two], 16 * 60, threading.Event())
        self.assertEqual(result.channels, list(hackrf.RADAR_CHANNELS))     # in the usual order
        self.assertEqual(len(one.heard) + len(two.heard), 16)
        self.assertEqual({round(s) for _c, s in one.heard + two.heard}, {120})   # 2 x 60 s
        self.assertEqual(result.error, "")

    def test_one_hackrf_failing_leaves_the_other_listening(self) -> None:
        one, two = FakeRadio("HackRF A", fail_at=5600.0), FakeRadio("HackRF B")
        result = hackrf.watch_dfs([one, two], 16 * 60, threading.Event())
        self.assertIn("channel 120", result.error)
        self.assertEqual(len(two.heard), 8)                         # carried on with its share
        self.assertNotIn(120, result.channels)
        self.assertEqual(len(result.channels), 8)

    def test_the_close_look_is_shared_and_kept_in_order(self) -> None:
        one, two = FakeRadio("HackRF A", loud=True), FakeRadio("HackRF B", fail_at=5180.0)
        centres = [2412.0, 2437.0, 2462.0, 5180.0]
        results, notes = hackrf.identify_all([one, two], centres)
        self.assertEqual([r["center_mhz"] for r in results], [2412.0, 2437.0, 2462.0])
        self.assertEqual(sorted(one.heard), [2412.0, 2462.0])
        self.assertEqual(sorted(two.heard), [2437.0, 5180.0])
        self.assertEqual({r["hackrf"] for r in results}, {"HackRF A", "HackRF B"})
        self.assertTrue(any("5180" in n for n in notes))
        self.assertTrue(any("overloaded" in n for n in notes))


class BluetoothTests(unittest.TestCase):
    def test_every_controller_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            for name in ("hci0", "hci1", "hci0:12"):           # the last is a connection
                (Path(root) / name).mkdir()
            self.assertEqual(bluetooth.controllers(Path(root)), [0, 1])
            self.assertEqual(bluetooth.controller_names(Path(root)), ["hci0", "hci1"])

    def test_a_device_heard_by_two_adapters_is_listed_once(self) -> None:
        first = [bluetooth.BtDevice("AA:BB:CC:00:00:01", "", -80, True, 0)]
        second = [bluetooth.BtDevice("aa:bb:cc:00:00:01", "Printer", -62, True, 0x060680),
                  bluetooth.BtDevice("AA:BB:CC:00:00:02", "", -70, False, 0)]
        merged = bluetooth.merge([first, second])
        self.assertEqual(len(merged), 2)
        printer = next(d for d in merged if d.address == "AA:BB:CC:00:00:01")
        self.assertEqual((printer.rssi, printer.name, printer.kind, printer.adapters),
                         (-62, "Printer", "printer", 2))
        self.assertEqual(printer.as_dict()["adapters"], 2)

    def test_scans_every_adapter_at_once(self) -> None:
        calls = []

        def fake(*args, timeout=20, index=None):
            calls.append((args[0], index))
            if args[0] == "power":
                return 0, "Set Powered complete"
            rssi = -50 if index == 1 else -75
            return 0, (f"hci{index} dev_found: 11:22:33:44:55:66 type BR/EDR rssi {rssi} flags 0x0000\n"
                       "name Speaker\n")

        with mock.patch.object(bluetooth, "available", lambda: True), \
                mock.patch.object(bluetooth, "controllers", lambda: [0, 1]), \
                mock.patch.object(bluetooth, "_btmgmt", fake), \
                mock.patch.object(bluetooth, "run", lambda *a, **k: (0, "")):
            found = bluetooth.scan(1.0)
        self.assertEqual({index for kind, index in calls if kind == "find"}, {0, 1})
        self.assertEqual([(d.rssi, d.adapters) for d in found], [(-50, 2)])

    def test_finder_tags_heard_by_two_adapters(self) -> None:
        merged = trackers.merge([
            [{"address": "AA:00:00:00:00:01", "rssi": -70, "kind": "airtag", "label": "AirTag",
              "away": True, "state": "away from its owner"}],
            [{"address": "AA:00:00:00:00:01", "rssi": -55, "kind": "airtag", "label": "AirTag",
              "away": False, "state": ""},
             {"address": "AA:00:00:00:00:02", "rssi": -60, "kind": "smarttag", "label": "SmartTag",
              "away": False, "state": ""}]])
        tag = merged[0]
        self.assertEqual((tag["rssi"], tag["away"], tag["adapters"]), (-55, True, 2))
        self.assertEqual(tag["state"], "away from its owner")
        self.assertEqual(len(merged), 2)


class DetectionTests(unittest.TestCase):
    def test_every_analyzer_that_answers_is_opened(self) -> None:
        opened = {"/dev/ttyUSB0": FakeAnalyzer("RF Explorer", 6100, "/dev/ttyUSB0"),
                  "/dev/ttyACM0": FakeAnalyzer("tinySA", 7300, "/dev/ttyACM0")}
        with mock.patch("wifisurveyor.net.gps.rf_probe_order",
                        return_value=["/dev/ttyACM0", "/dev/ttyUSB0", "/dev/ttyUSB1"]), \
                mock.patch.object(detect, "identify", lambda port: opened.get(port)):
            found = detect.find_all()
        self.assertEqual([a.name for a in found.found], ["tinySA", "RF Explorer"])
        self.assertIs(found.explorer, opened["/dev/ttyACM0"])
        self.assertIn("RF Explorer on /dev/ttyUSB0", found.message)


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        from wifisurveyor.app import Session

        self.saved: dict = {}
        for patch in (mock.patch.object(settings, "load", lambda: dict(self.saved)),
                      mock.patch.object(settings, "put", lambda k, v: self.saved.__setitem__(k, v))):
            patch.start()
            self.addCleanup(patch.stop)
        self.session = Session()

    def test_analyzers_come_and_go_independently(self) -> None:
        a = FakeAnalyzer("tinySA", 7300, "/dev/ttyACM0")
        b = FakeAnalyzer("RF Explorer", 6100, "/dev/ttyUSB0")
        self.session.set_explorer(a)
        self.session.set_explorer(b)
        self.assertEqual(self.session.serial_analyzers, [a, b])
        self.assertIs(self.session.serial_analyzer, a)
        self.assertIs(self.session.explorer, a)
        again = FakeAnalyzer("tinySA", 7300, "/dev/ttyACM0")       # reopened on its port
        self.session.set_explorer(again)
        self.assertTrue(a.closed)
        self.assertEqual(self.session.serial_analyzers, [b, again])
        b.alive = False
        self.session.set_explorer(None)                             # drops the dead ones
        self.assertEqual(self.session.serial_analyzers, [again])

    def test_hackrfs_listen_unless_sending(self) -> None:
        one, two = FakeRadio("HackRF A"), FakeRadio("HackRF B")
        self.session.set_hackrf(one)
        self.session.set_hackrf(two)
        self.assertEqual(self.session.listening_hackrfs(), [one, two])
        one.sending = True
        self.assertIs(self.session.hackrf, two)                     # the one free to listen
        self.assertEqual(self.session.listening_hackrfs(), [two])
        # No serial analyzer: the free HackRFs sweep
        self.assertEqual(self.session.sweepers(), [two])
        serial = FakeAnalyzer("tinySA", 7300, "/dev/ttyACM0")
        self.session.set_explorer(serial)
        self.assertEqual(self.session.sweepers(), [serial])
        self.saved["hackrf_sweeps"] = True
        self.assertEqual(self.session.sweepers(), [two, serial])


class ReportTests(unittest.TestCase):
    def test_how_it_was_made_covers_every_device(self) -> None:
        project = Project(meters_per_pixel=0.05, width=1000, height=600)
        segment = {"start_mhz": 2400.0, "step_mhz": 0.5, "rbw_khz": 500, "sweeps": 3,
                   "avg": [-90.0] * 201, "max": [-80.0] * 201, "min": [-95.0] * 201,
                   "occupancy": {}, "analyzer": "tinySA Ultra+"}
        five = {**segment, "start_mhz": 5150.0, "analyzer": "RF Explorer"}
        cal = {"calibrated": True, "offset_db": -14.0, "band_offsets": {"2.4": -13.5, "5": -17.0},
               "gain_shift": 8.0, "gains": [24, 20, False], "bias_tee": True,
               "calibrated_with": "the Wi-Fi card, 17 Oct 2026", "deaf": ["5"]}
        for index in range(3):
            point = Point(x=100 + index * 200, y=300, networks=[
                {"ssid": "Office", "bssid": "aa:bb:cc:00:00:01", "rssi": -55, "freq_mhz": 2437,
                 "channel": 6, "band": "2.4", "readings": 2}])
            point.spectrum = {"segments": [segment, five], "dwells": []}
            point.iq = [{"center_mhz": 2437, "airtime": 0.2, "clipped": 0.02,
                         "recording": "/tmp/x/20261017-1000-2437MHz.sigmf-data",
                         "findings": [{"kind": "bursty", "name": "Unidentified bursts",
                                       "description": "", "center_mhz": 2437, "width_mhz": 3,
                                       "bursts": 9, "airtime": 0.05, "above_db": 12}]}]
            point.bluetooth = [{"address": "AA:00:00:00:00:01", "rssi": -60, "public": True,
                                "kind": "audio", "name": "Speaker", "adapters": 2}]
            point.method = {"adapter": "wlan0", "adapter_name": "Intel", "scan": "iw",
                            "scan_adapters": ["wlan0", "wlan1"], "wifi_readings": {"merged": 1},
                            "analyzer": "RF Explorer, tinySA Ultra+",
                            "hackrf": "HackRF One 0000abcd", "hackrf_cal": {"HackRF One 0000abcd": cal},
                            "bt_adapters": ["hci0 (btusb)", "hci1 (btusb)"], "sweeps_per_band": 3}
            project.points.append(point)
        html = report.build_html(project, None, lambda p: None)
        for expected in ("wlan0, wlan1 (3 points)", "averaged", "tinySA Ultra+: 2.4 GHz",
                         "RF Explorer: 5 GHz", "swept at once", "matched to the Wi-Fi card",
                         "2.4 GHz -13.5", "LNA 24 dB", "adjusted -8 dB", "antenna power on",
                         "HackRF overload", "HackRF antenna", "Raw I/Q kept",
                         "hci0 (btusb), hci1 (btusb)", "heard by more than one",
                         "overloaded</span> 2.0%", "20261017-1000-2437MHz.sigmf-data"):
            self.assertIn(expected, html)

    def test_uncalibrated_cellular_is_marked(self) -> None:
        project = Project(meters_per_pixel=0.05, width=1000, height=600)
        point = Point(x=100, y=100, networks=[])
        point.cellular = {"pcs": {"peak_dbm": -70.0, "peak_mhz": 1960, "total_dbm": -65}}
        point.method = {"hackrf_cal": {"HackRF One": {"calibrated": False}}}
        project.points.append(point)
        html = report.build_html(project, None, lambda p: None)
        self.assertIn("Estimates</span> at 1 of 1 points", html)
        self.assertIn("not calibrated, so its levels are estimates", html)


if __name__ == "__main__":
    unittest.main()
