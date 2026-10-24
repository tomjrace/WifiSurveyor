import unittest

from wifisurveyor.net import bluetooth as bt

SAMPLE = """Discovery started
hci0 type 7 discovering on
hci0 dev_found: 30:05:5C:11:22:33 type BR/EDR rssi -58 flags 0x0000
name Brother HL-L2380DW
class 0x000680
hci0 dev_found: 00:1B:66:00:00:66 type BR/EDR rssi -71 flags 0x0000
name Jabra Speak 510
class 0x240404
hci0 dev_found: 4F:A2:9C:00:00:30 type LE Random rssi -80 flags 0x0000
AD flags 0x1a
hci0 dev_found: 30:05:5C:11:22:33 type BR/EDR rssi -55 flags 0x0000
"""


class BluetoothParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.devices = {d.address: d for d in bt.parse_find(SAMPLE)}

    def test_printer_identified_and_kept_at_strongest(self) -> None:
        printer = self.devices["30:05:5C:11:22:33"]
        self.assertEqual(printer.kind, "printer")
        self.assertEqual(printer.rssi, -55)          # the stronger of its two sightings
        self.assertTrue(printer.stable)

    def test_audio_and_random_phone(self) -> None:
        self.assertEqual(self.devices["00:1B:66:00:00:66"].kind, "audio")
        phone = self.devices["4F:A2:9C:00:00:30"]
        self.assertFalse(phone.public)
        self.assertFalse(phone.stable)              # random address: can't be placed
        self.assertEqual(phone.vendor, "")

    def test_class_of_device_decoding(self) -> None:
        self.assertEqual(bt.classify(0x000680), ("printer", "Printer"))
        self.assertEqual(bt.classify(0x240404)[0], "audio")
        self.assertEqual(bt.classify(0x00020C)[0], "phone")
        self.assertEqual(bt.classify(0)[0], "unknown")


if __name__ == "__main__":
    unittest.main()


class BtLocalizeTests(unittest.TestCase):
    def test_infrastructure_located_personal_ignored(self) -> None:
        import math
        from wifisurveyor.core import analysis, report
        from wifisurveyor.core.project import Point, Project

        def printer(rssi):
            return {"address": "30:05:5C:11:22:33", "name": "Brother HL", "rssi": rssi,
                    "public": True, "cod": 0x680, "kind": "printer", "detail": "Printer",
                    "vendor": "Brother"}

        def phone(rssi):
            return {"address": "4F:A2:9C:00:00:30", "name": "", "rssi": rssi, "public": False,
                    "cod": 0, "kind": "unknown", "detail": "", "vendor": ""}

        points = []
        for x, y in [(250, 250), (700, 300), (1150, 250), (300, 750), (800, 700)]:
            d = math.hypot(x - 300, y - 750)
            p = Point(x=x, y=y, networks=[])
            p.bluetooth = [printer(int(-40 - d / 12)), phone(-70)]
            points.append(p)
        project = Project(points=points)
        devices = analysis.bt_devices(project)
        self.assertEqual([d["address"] for d in devices], ["30:05:5C:11:22:33"])
        located = devices[0]["located"]
        self.assertLess(math.hypot(located["x"] - 300, located["y"] - 750), 150)
        html = report.build_html(project, None, lambda _p: None)
        self.assertIn("Bluetooth devices", html)
        self.assertIn("Brother HL", html)
        self.assertNotIn("4F:A2:9C:00:00:30", html)   # rotating: summarised, not listed by address
        self.assertIn("1 device", html)               # but still reported as heard
