"""Where the surveyor is, from the access points heard now."""

import math
import unittest

from wifisurveyor.core import positioning
from wifisurveyor.core.project import Point, Project

MPP = 0.05                                   # 1600 x 1000 plan pixels = 80 x 50 m
APS = {"aa:00:00:00:00:01": (150, 150), "aa:00:00:00:00:02": (1450, 150),
       "aa:00:00:00:00:03": (150, 850), "aa:00:00:00:00:04": (1450, 850),
       "aa:00:00:00:00:05": (800, 500)}
PHONE = "a4:83:e7:12:34:56"                  # a client, captured at the points
BEACON = "30:05:5c:11:22:33"                 # a Bluetooth printer


def level(bssid, x, y):
    ax, ay = APS[bssid]
    metres = max(1.0, math.hypot(x - ax, y - ay) * MPP)
    return -32 - 30 * math.log10(metres)


def heard_at(x, y, shift=0.0):
    return {b: round(level(b, x, y) + shift) for b in APS if level(b, x, y) + shift >= -90}


def survey() -> Project:
    points = []
    for x in range(100, 1600, 250):
        for y in range(100, 1000, 250):
            point = Point(x=x, y=y, networks=[
                {"ssid": "office5g", "bssid": b, "rssi": r, "band": "5", "freq_mhz": 5180 + 20 * i,
                 "channel": 36 + 4 * i}
                for i, (b, r) in enumerate(heard_at(x, y).items())])
            point.capture = {"clients_heard": [{"mac": PHONE, "rssi": -30, "vendor": "Apple",
                                                "ssid": "office5g", "bssid": "aa:00:00:00:00:01"}]}
            point.bluetooth = [{"address": BEACON, "rssi": -30, "public": True}]
            points.append(point)
    return Project(points=points, meters_per_pixel=MPP)


class PositioningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = survey()
        cls.model = positioning.build_model(cls.project)

    def feet_off(self, fix, x, y):
        return math.hypot(fix.x - x, fix.y - y) * MPP / 0.3048

    def test_model_uses_access_points_only(self):
        self.assertEqual(self.model.known, set(APS))
        self.assertEqual(sorted(self.model.aps), sorted(APS))
        self.assertNotIn(PHONE, self.model.known)
        self.assertNotIn(BEACON, self.model.known)
        self.assertEqual(self.model.freqs, [5180, 5200, 5220, 5240, 5260])

    def test_places_you_between_survey_points(self):
        for x, y in [(480, 420), (1200, 700), (900, 250)]:
            fix = positioning.estimate(self.model, heard_at(x, y))
            self.assertIsNotNone(fix)
            self.assertEqual(fix.method, "both")
            self.assertLess(self.feet_off(fix, x, y), 25, (x, y))

    def test_devices_never_move_the_estimate(self):
        x, y = 480, 420
        clean = positioning.estimate(self.model, heard_at(x, y))
        noisy = positioning.estimate(self.model, {**heard_at(x, y), PHONE: -25, BEACON: -20,
                                                  "de:ad:be:ef:00:00": -30})
        self.assertEqual((clean.x, clean.y, clean.aps), (noisy.x, noisy.y, noisy.aps))

    def test_too_few_access_points_gives_no_fix(self):
        two = dict(list(heard_at(800, 500).items())[:2])
        self.assertIsNone(positioning.estimate(self.model, {**two, PHONE: -30, BEACON: -30}))

    def test_tap_calibrates_out_a_body_shift(self):
        locator = positioning.Locator()
        x, y = 1200, 700
        weak = heard_at(x, y, shift=-9)               # held low, body in the way
        locator.update(self.model, weak)
        shift = locator.calibrate(self.model, x, y)
        self.assertAlmostEqual(shift, -9, delta=2.5)
        self.assertEqual(locator.track, (x, y))
        # Walk on: the calibrated locator follows a new spot better than an uncalibrated one
        later = heard_at(500, 300, shift=-9)
        fresh = positioning.estimate(self.model, later)
        calibrated = positioning.estimate(self.model, later, locator.bias_db)
        self.assertLessEqual(self.feet_off(calibrated, 500, 300), self.feet_off(fresh, 500, 300) + 1)
        self.assertLess(self.feet_off(calibrated, 500, 300), 25)

    def test_smoothing_follows_without_jumping(self):
        locator = positioning.Locator()
        first = locator.update(self.model, heard_at(400, 400))
        second = locator.update(self.model, heard_at(1200, 400))
        self.assertGreater(second.x, first.x)
        self.assertLess(second.x, 1200)               # moves towards the new fix, not onto it


if __name__ == "__main__":
    unittest.main()
