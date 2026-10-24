"""Finder tags: recognising them, placing them, reporting them; and btmgmt not hanging."""

import os
import stat
import tempfile
import unittest
from unittest import mock

from wifisurveyor.core import advice, analysis, report
from wifisurveyor.core.project import Point, Project
from wifisurveyor.net import bluetooth, trackers

APPLE = 0x004C
FEAA = "0000feaa-0000-1000-8000-00805f9b34fb"


def apple(hex_payload):
    return trackers.classify({APPLE: bytes.fromhex(hex_payload)}, {})


class ClassifyTest(unittest.TestCase):
    def test_real_payloads_heard_on_this_site(self):
        tag = apple("12025000")                       # status 0x50: AirTag, battery medium
        self.assertEqual((tag["kind"], tag["away"], tag["battery"]), ("airtag", False, "medium"))
        self.assertEqual(apple("12022401")["kind"], "findmy")
        self.assertIsNone(apple("12020003"))           # an iPhone or Mac near its owner
        self.assertIsNone(apple("121900570dc9fa7afdc4536d58f52cd6a4cea36a57c24ce2760200"))
        self.assertIsNone(apple("071106635cafec01cdedf1bd4842542dfe08bd"))   # not Find My at all

    def test_apple_types_and_separation(self):
        separated = "1219" + "10" + "00" * 22
        tag = apple(separated)
        self.assertEqual((tag["kind"], tag["away"]), ("airtag", True))
        self.assertIn("away", tag["state"])
        self.assertIsNone(apple("12023000"))           # AirPods
        self.assertEqual(apple("1202e000")["battery"], "very low")

    def test_google_find_hub(self):
        away = trackers.classify({}, {FEAA: bytes([0x41]) + bytes(20)}, flags=b"\x06")
        self.assertEqual((away["kind"], away["away"]), ("google", True))
        near = trackers.classify({}, {FEAA: bytes([0x40]) + bytes(20)}, flags=b"\x06")
        self.assertFalse(near["away"])
        self.assertIsNone(trackers.classify({}, {FEAA: bytes([0x40]) + bytes(20)}, flags=b"\x02"))
        self.assertIsNone(trackers.classify({}, {FEAA: bytes([0x00]) + bytes(17)}))   # Eddystone

    def test_samsung_tile_chipolo_pebblebee(self):
        smarttag = "0000fd5a-0000-1000-8000-00805f9b34fb"
        self.assertTrue(trackers.classify({}, {smarttag: bytes([0x13, 0, 0, 0])})["away"])
        self.assertFalse(trackers.classify({}, {smarttag: bytes([0x11, 0, 0, 0])})["away"])
        self.assertIsNone(trackers.classify({}, {smarttag: bytes([0x20, 0, 0, 0])}))
        self.assertEqual(trackers.classify({}, {"0000feed-0000-1000-8000-00805f9b34fb": b"\x02\x00\x01"})["kind"], "tile")
        self.assertEqual(trackers.classify({}, {}, ["0000fe33-0000-1000-8000-00805f9b34fb"])["kind"], "chipolo")
        self.assertEqual(trackers.classify({}, {}, ["0000fa25-0000-1000-8000-00805f9b34fb"])["kind"], "pebblebee")

    def test_only_devices_heard_in_this_scan(self):
        adapter = "/org/bluez/hci0"
        device = {"Address": "d9:a5:9e:00:00:d4", "ManufacturerData": {APPLE: bytes.fromhex("12025000")}}
        objects = {
            adapter + "/dev_1": {"org.bluez.Device1": {**device, "RSSI": -50}},
            adapter + "/dev_2": {"org.bluez.Device1": {**device, "Address": "aa:bb:cc:dd:ee:ff"}},  # remembered, not heard
            adapter + "/dev_3": {"org.bluez.Device1": {"Address": "11:22:33:44:55:66", "RSSI": -40,
                                                       "ManufacturerData": {APPLE: bytes.fromhex("12020003")}}},
        }
        found = trackers.from_objects(objects, adapter)
        self.assertEqual([(t["address"], t["rssi"], t["kind"]) for t in found],
                         [("D9:A5:9E:00:00:D4", -50, "airtag")])


class PlacementAndReportTest(unittest.TestCase):
    def survey(self):
        points = []
        for number, (x, y) in enumerate([(200, 200), (1200, 200), (200, 800), (1200, 800), (700, 500)]):
            point = Point(x=x, y=y, networks=[{"ssid": "office5g", "bssid": "78:9f:6a:00:00:d1",
                                               "rssi": -55, "band": "5", "channel": 48,
                                               "freq_mhz": 5240}])
            rssi = -40 - int(((x - 300) ** 2 + (y - 700) ** 2) ** 0.5 / 15)
            point.trackers = [{"address": "C0:FF:EE:00:00:01", "rssi": rssi, "kind": "airtag",
                               "label": "AirTag", "away": True, "state": "away from its owner",
                               "battery": "low"}]
            if number == 1:
                point.trackers.append({"address": "12:34:56:78:9A:BC", "rssi": -60, "kind": "google",
                                       "label": "Find Hub tag", "away": False,
                                       "state": "near its owner recently", "battery": ""})
            points.append(point)
        return Project(points=points, meters_per_pixel=0.05)

    def test_placed_away_tags_first_and_reported(self):
        project = self.survey()
        tags = analysis.finder_tags(project)
        self.assertEqual([t["address"] for t in tags], ["C0:FF:EE:00:00:01", "12:34:56:78:9A:BC"])
        located = tags[0]["located"]
        self.assertFalse(located.get("near"))
        self.assertLess(((located["x"] - 300) ** 2 + (located["y"] - 700) ** 2) ** 0.5, 200)
        self.assertTrue(tags[1]["located"]["near"])
        self.assertEqual(tags[1]["near_point"], 2)

        html = report.build_html(project, None, lambda _p: None)
        self.assertIn("Finder tags", html)
        self.assertIn("C0:FF:EE:00:00:01", html)
        self.assertIn("1 was away from their owner", html)

        found = advice.improvements(project.points[2], analysis.Context(project))
        self.assertIn("Finder tag away from its owner", [f.title for f in found])


class BtmgmtTest(unittest.TestCase):
    def test_btmgmt_gets_a_terminal_so_it_finishes(self):
        # Like BlueZ 5.8x's btmgmt: finishes only when its input is a terminal
        with tempfile.TemporaryDirectory() as folder:
            fake = os.path.join(folder, "btmgmt")
            with open(fake, "w") as script:
                script.write("#!/bin/sh\nif [ -t 0 ]; then echo 'hci0 Set Powered complete'; "
                             "exit 0; fi\nsleep 30\n")
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            with mock.patch.dict(os.environ, {"PATH": folder + os.pathsep + os.environ["PATH"]}):
                code, text = bluetooth._btmgmt("power", "on", timeout=5)
        self.assertEqual(code, 0)
        self.assertIn("complete", text)


if __name__ == "__main__":
    unittest.main()
