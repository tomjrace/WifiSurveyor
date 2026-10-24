"""Wi-Fi clients from captures attached to survey points, placed on the map."""

import math
import unittest

from wifisurveyor.core import analysis
from wifisurveyor.core.project import Point, Project


def heard(mac, rssi, vendor="Apple, Inc.", ssid="office5g"):
    return {"mac": mac, "rssi": rssi, "vendor": vendor, "ssid": ssid,
            "bssid": "78:9f:6a:00:00:d1", "frames": 40, "airtime": 0.01}


class WifiClientsTest(unittest.TestCase):
    def test_located_from_four_points_and_rough_from_one(self):
        laptop = (300, 750)
        points = []
        for x, y in [(250, 250), (700, 300), (1150, 250), (300, 750), (800, 700)]:
            metres = max(1.0, math.hypot(x - laptop[0], y - laptop[1]) * 0.05)
            point = Point(x=x, y=y, networks=[])
            point.capture = {"clients_heard": [heard("a4:83:e7:00:00:01",
                                                     round(-35 - 30 * math.log10(metres), 1))]}
            points.append(point)
        # A phone heard at one point only, behind a private address
        points[1].capture["clients_heard"].append(
            heard("6a:11:22:33:44:55", -72, vendor="Private address"))
        clients = analysis.wifi_clients(Project(points=points))

        self.assertEqual([c["mac"] for c in clients], ["a4:83:e7:00:00:01", "6a:11:22:33:44:55"])
        located = clients[0]["located"]
        self.assertFalse(located.get("near"))
        self.assertLess(math.hypot(located["x"] - laptop[0], located["y"] - laptop[1]), 150)
        self.assertEqual(clients[0]["points"], 5)
        self.assertEqual(clients[0]["near_point"], 4)

        phone = clients[1]
        self.assertTrue(phone["located"]["near"] and phone["located"]["rough"])
        self.assertEqual((phone["located"]["x"], phone["located"]["y"]), (700, 300))
        self.assertEqual(phone["best"], -72)

    def test_points_without_client_signals_are_ignored(self):
        old = Point(x=10, y=10, networks=[])
        old.capture = {"retry_rate": 0.1, "clients": 3}      # attached before signals were kept
        self.assertEqual(analysis.wifi_clients(Project(points=[old, Point(x=1, y=1)])), [])


if __name__ == "__main__":
    unittest.main()
