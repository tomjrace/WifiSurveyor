"""The Monitor page's drawing, off screen."""

import unittest
from types import SimpleNamespace

import cairo
import numpy as np

from wifisurveyor.ui import monitor_view as mv


class LabelTests(unittest.TestCase):
    def test_a_stretch_at_the_right_edge_is_labelled(self) -> None:
        # Its words move left to fit; that once made the stretch's own position a float and
        # broke the frame, leaving the tiles beside the traces black
        width, a, spp = 800, 1000.0, 1.0
        codes = np.ones(width, dtype=np.int8)
        codes[-30:] = 3
        tops = np.full(width, 0.5)
        t = a + np.arange(width) + 0.5
        beats = [{"t": x, "assoc": False, "state": "down", "stage": "disconnect"} for x in t]
        history = SimpleNamespace(t=t, beats=beats)
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_RGB24, width, 100))
        mv.MonitorView._label_runs(None, cr, (tops, tops, codes), mv.BEACON_COLOURS, 0, width,
                                   a, spp, history, mv.beacon_words)

    def test_beacons_go_red_only_when_the_wifi_link_failed(self) -> None:
        self.assertEqual(mv.beacon_code(0.3, False, None), 1)
        self.assertEqual(mv.beacon_code(0.3, False, "no_internet"), 1)
        self.assertEqual(mv.beacon_code(0.3, False, "no_gateway"), 3)
        self.assertEqual(mv.beacon_code(1.0, True, "disconnect"), 3)


if __name__ == "__main__":
    unittest.main()
