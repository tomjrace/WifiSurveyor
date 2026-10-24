"""The RTL-SDR driver against fake rtl-sdr tools."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import settings
from wifisurveyor.rf import rtlsdr

FAKE_POWER = r'''
import os, signal, sys, time
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("rtl_power " + " ".join(args) + "\n")
if os.environ.get("FAKE_BUSY"):
    sys.stderr.write("usb_claim_interface error -6\nFailed to open rtlsdr device #0.\n")
    sys.exit(1)
spec = args[args.index("-f") + 1]
low, high, width = (int(v) for v in spec.split(":"))
once = "-1" in args
hop = 2_000_000
while True:
    f = low
    while f < high:
        top = min(f + hop, high)
        count = max(1, int((top - f) / width))
        levels = []
        for b in range(count):
            centre = (f + (b + 0.5) * (top - f) / count) / 1e6
            levels.append(-10.0 if abs(centre - 915.0) < 0.2 else -40.0)
        sys.stdout.write(f"2026-10-17, 10:00:00, {f}, {top}, {(top - f) / count:.2f}, 100, "
                         + ", ".join(f"{v:.2f}" for v in levels) + "\n")
        f = top
    sys.stdout.flush()
    if once:
        break
    time.sleep(0.05)
'''

FAKE_EEPROM = r'''
import os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("rtl_eeprom " + " ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_DENY"):
    sys.stderr.write("usb_open error -3\nFailed to open rtlsdr device #0.\n")
    sys.exit(1)
sys.stderr.write("Found 1 device(s):\n  0:  Generic RTL2832U OEM\n\nFound Rafael Micro R820T tuner\n")
'''


FAKE_SDR = r'''
import os, random, signal, sys
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
if os.environ.get("FAKE_DEAF"):
    sys.stderr.write("Found Rafael Micro R820T tuner\n[R82XX] PLL not locked!\n")
    sys.stderr.flush()
out = sys.stdout.buffer
while True:            # like the real one, it doesn't stop by itself
    if os.environ.get("FAKE_DEAF"):
        out.write(bytes(random.choice((127, 128)) for _ in range(65536)))
    else:
        out.write(bytes(random.randint(100, 155) for _ in range(65536)))
    out.flush()
'''


class RtlSdrTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in (("rtl_power", FAKE_POWER), ("rtl_eeprom", FAKE_EEPROM),
                           ("rtl_sdr", FAKE_SDR)):
            path = bin_dir / name
            path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.log = root / "log"
        self.log.write_text("")
        self.node = root / "usbnode"
        self.node.write_text("")
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "XDG_DATA_HOME": str(root / "data")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.usb = rtlsdr.UsbRtl(str(self.node), "Realtek", "RTL2838UHIDIR", "00000001")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_opens_names_the_tuner_and_selects_by_index(self):
        device = rtlsdr.RtlSdr(self.usb, index=0)
        try:
            self.assertEqual(device.tuner, "R820T")
            self.assertEqual((device.min_mhz, device.max_mhz), (24.0, 1766.0))
            self.assertEqual(device.wifi_ranges, ())
            # "00000001" reads as an index, so the index is used, never the serial
            self.assertIn("rtl_eeprom -d 0", self.log.read_text())
        finally:
            device.close()

    def test_permission_problem_is_explained(self):
        with mock.patch.dict(os.environ, {"FAKE_DENY": "1"}):
            with self.assertRaises(ConnectionError) as caught:
                rtlsdr.RtlSdr(self.usb)
        self.assertIn("No permission", str(caught.exception))

    def test_live_sweeps_with_offset_and_gain(self):
        settings.put("rtlsdr_gain", 20.0)
        rtlsdr.save_offset(-80.0, "test", self.usb.serial)
        device = rtlsdr.RtlSdr(self.usb)
        try:
            config = device.configure(902, 928)
            deadline = time.monotonic() + 5
            while device.latest() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            trace = device.latest()
            self.assertIsNotNone(trace)
            self.assertEqual(len(trace), config.points)
            peak = max(range(len(trace)), key=trace.__getitem__)
            self.assertAlmostEqual(config.start_mhz + peak * config.step_mhz, 915, delta=0.3)
            self.assertAlmostEqual(max(trace), -90, delta=0.5)        # -10 dBFS + -80
            self.assertIn("-g 19.7", self.log.read_text())             # the nearest step to 20
        finally:
            device.close()

    def test_gain_change_moves_the_offset(self):
        settings.put("rtlsdr_gain", 29.7)
        rtlsdr.save_offset(-80.0, None, "unit")
        settings.put("rtlsdr_gain", 39.7)                         # snaps to the 40.2 dB step
        self.assertAlmostEqual(rtlsdr.offset_db("unit"), -90.5, places=1)

    def test_fine_scan_peak_holds_and_crops(self):
        device = rtlsdr.RtlSdr(self.usb)
        try:
            trace = device.scan(905, 925, 25, sweeps=2)
            self.assertGreaterEqual(trace.freqs[0], 905)
            self.assertLessEqual(trace.freqs[-1], 925)
            self.assertEqual(self.log.read_text().count(" -1 "), 2)
            self.assertAlmostEqual(float(trace.freqs[trace.peak.argmax()]), 915, delta=0.3)
        finally:
            device.close()

    def test_held_by_another_program(self):
        device = rtlsdr.RtlSdr(self.usb)
        try:
            with mock.patch.dict(os.environ, {"FAKE_BUSY": "1"}):
                with self.assertRaises(RuntimeError) as caught:
                    device.scan(905, 925, 25, sweeps=1)
            self.assertIn("held by another program", str(caught.exception))
        finally:
            device.close()

    def test_reception_check_finds_a_deaf_tuner(self):
        device = rtlsdr.RtlSdr(self.usb)
        try:
            self.assertTrue(device.healthy)
            with mock.patch.dict(os.environ, {"FAKE_DEAF": "1"}):
                device.fault = device.check_reception()
            self.assertFalse(device.healthy)
            self.assertIn("PLL not locked", device.fault)
            self.assertIn("NOT RECEIVING", device.summary)
        finally:
            device.close()

    def test_model_chosen_in_setup(self):
        settings.put("rtlsdr_model", "v3")
        device = rtlsdr.RtlSdr(self.usb)
        try:
            self.assertEqual(device.name, "RTL-SDR Blog V3")
            self.assertEqual(device.min_mhz, 24.0)          # only the V4 reaches HF directly
        finally:
            device.close()
        settings.put("rtlsdr_model", "auto")
        self.assertEqual(rtlsdr.chosen_model(self.usb), "RTL-SDR")

    def test_plugged_in_reads_sysfs(self):
        root = Path(self.temp.name) / "sys"
        entry = root / "1-6"
        entry.mkdir(parents=True)
        for name, value in (("idVendor", "0bda"), ("idProduct", "2838"), ("busnum", "1"),
                            ("devnum", "9"), ("manufacturer", "RTLSDRBlog"),
                            ("product", "Blog V4"), ("serial", "00000001")):
            (entry / name).write_text(value + "\n")
        found = rtlsdr.plugged_in(root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].node, "/dev/bus/usb/001/009")
        self.assertEqual(found[0].model, "RTL-SDR Blog V4")


if __name__ == "__main__":
    unittest.main()
