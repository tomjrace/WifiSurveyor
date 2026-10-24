"""The tinySA driver against a simulated tinySA shell on a pseudo-terminal."""

from __future__ import annotations

import os
import pty
import select
import threading
import time
import tty
import unittest
from unittest import mock

from wifisurveyor.rf import analyzer, detect, tinysa


class FakeTinySa:
    """
    Answers like a tinySA Ultra+ ZS407: echoes each command, prints its output, then "ch> ".
    A stop frequency above what it reaches is clamped, as the firmware did until late 2026.
    ``info`` is the model line "info" starts with, or None for a shell without the command.
    ``leftover`` comes out before the first answer, as the end of a scan that was still
    running when the port was opened.
    """

    def __init__(self, reach_hz: int = 7_300_000_000, clamps: bool = True,
                 info: str | None = None, leftover: str = "") -> None:
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        self.reach_hz, self.clamps = reach_hz, clamps
        self.info, self.leftover = info, leftover
        self.scans: list[tuple[int, int, str]] = []      # (start, stop, rbw) of each scan
        self.start_hz, self.stop_hz = 0, 800_000_000
        self.commands: list[str] = []
        self.rbw: list[str] = []
        self.level = -80.0
        self.running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _answer(self, line: str) -> str:
        words = line.split()
        if not words:
            return ""
        if words[0] == "info" and self.info:
            return f"{self.info}\r\n2019-2024 Copyright @Erik Kaashoek\r\n"
        if words[0] == "version":
            return "tinySA4_v1.4-143-g864bb27\r\nHW Version:V0.5.4\r\n"
        if words[0] == "sweep":
            if len(words) == 1:
                return f"{self.start_hz} {self.stop_hz} 450\r\n"
            if words[1] == "stop":
                wanted = int(words[2])
                if wanted <= self.reach_hz or self.clamps:
                    self.stop_hz = min(wanted, self.reach_hz)
                return ""
            self.start_hz, self.stop_hz = int(words[1]), int(words[2])
            return ""
        if words[0] == "rbw":
            self.rbw.append(words[1])
            return ""
        if words[0] in ("ultra", "mode"):
            return ""
        if words[0] == "scan":
            start, stop, points = int(words[1]), int(words[2]), int(words[3])
            self.scans.append((start, stop, self.rbw[-1] if self.rbw else ""))
            step = (stop - start) / (points - 1)
            time.sleep(0.02)
            return "".join(f"{int(start + i * step)} {self.level:.6e} \r\n" for i in range(points))
        return f"{line}?\r\n"

    def _serve(self) -> None:
        line = b""
        while self.running:
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            try:
                data = os.read(self.master, 256)
            except OSError:
                return
            for byte in data:
                if byte != 0x0D:
                    line += bytes([byte])
                    continue
                text = line.decode("ascii", "replace").strip()
                line = b""
                self.commands.append(text)
                before, self.leftover = self.leftover, ""
                try:
                    os.write(self.master, (before + text + "\r\n" + self._answer(text)).encode() + b"ch> ")
                except OSError:
                    return

    def unplug(self) -> None:
        self.running = False
        self._thread.join(1)
        os.close(self.master)


class PtyTinySaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeTinySa()

    def tearDown(self) -> None:
        if self.fake.running:
            self.fake.unplug()

    def test_probe_names_it(self) -> None:
        self.assertEqual(tinysa.probe(self.fake.path), "tinySA4_v1.4-143-g864bb27")

    def test_an_ultra_plus_is_recognised_and_its_reach_found(self) -> None:
        device = tinysa.TinySa(self.fake.path)
        try:
            self.assertEqual(device.name, "tinySA Ultra+")
            self.assertEqual(device.max_mhz, 7300)
            self.assertIn("ultra on", self.fake.commands)
            # Its own sweep is put back after asking how far it goes
            self.assertEqual((self.fake.start_hz, self.fake.stop_hz), (0, 800_000_000))
            # So the survey sweeps all of 6 GHz, not just the bottom of it
            self.assertEqual(device.wifi_ranges[3], ("6 GHz", 5925.0, 7125.0))
            self.assertEqual(device.wifi_ranges[:3], analyzer.WIFI_RANGES[:3])
        finally:
            device.close()

    def test_sweep(self) -> None:
        device = tinysa.TinySa(self.fake.path)
        try:
            config, sweeps = device.sweep(2400, 2500, count=3, timeout=10)
            self.assertEqual(config.start_mhz, 2400)
            self.assertAlmostEqual(config.end_mhz, 2500)
            self.assertEqual(config.points, 450)
            # 100 MHz over 450 points is a 223 kHz step: the next RBW up is 300 kHz
            self.assertEqual(config.rbw_khz, 300)
            self.assertEqual(self.fake.rbw[-1], "300")
            self.assertGreaterEqual(len(sweeps), 3)
            self.assertEqual(len(sweeps[0]), 450)
            self.assertEqual(sweeps[0][0], -80.0)
        finally:
            device.close()

    def test_the_whole_of_6ghz_comes_in_pieces(self) -> None:
        device = tinysa.TinySa(self.fake.path)
        try:
            pieces = device.sweep_range(5925, 7125, count=1, timeout=10)
            self.assertEqual(len(pieces), 3)
            self.assertAlmostEqual(pieces[0][0].start_mhz, 5925)
            self.assertAlmostEqual(pieces[-1][0].end_mhz, 7125)
        finally:
            device.close()

    def test_a_dwell_watches_one_channel(self) -> None:
        device = tinysa.TinySa(self.fake.path)
        try:
            config, sweeps = device.dwell(2437, 0.5, 20)
            self.assertAlmostEqual(config.start_mhz, 2425)
            self.assertEqual(config.rbw_khz, 100)
            self.assertTrue(sweeps)
        finally:
            device.close()

    def test_a_retune_never_scans_the_old_range_at_the_new_rbw(self) -> None:
        # A wide range at a narrow RBW takes the real thing many seconds: long enough to time
        # out, which is what made it stop answering after the wide ranges before telemetry
        device = tinysa.TinySa(self.fake.path)
        try:
            for _ in range(5):
                device.sweep(470, 614, count=2, timeout=10)
                device.sweep(1395, 1400, count=2, timeout=10)
            self.assertNotIn((470_000_000, 614_000_000, "30"), self.fake.scans)
            self.assertNotIn((1_395_000_000, 1_400_000_000, "600"), self.fake.scans)
        finally:
            device.close()

    def test_unplugging_is_noticed(self) -> None:
        device = tinysa.TinySa(self.fake.path)
        try:
            device.sweep(2400, 2500, count=1, timeout=10)
            self.assertTrue(device.alive)
            self.fake.unplug()
            deadline = time.monotonic() + 5
            while device.alive and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(device.alive)
            with self.assertRaises(ConnectionError):
                device.sweep(2400, 2500, count=1, timeout=1)
        finally:
            device.close()


class ReachTests(unittest.TestCase):
    def test_a_zs406_stops_short_of_upper_5ghz(self) -> None:
        fake = FakeTinySa(reach_hz=5_400_000_000)
        device = tinysa.TinySa(fake.path)
        try:
            self.assertEqual(device.name, "tinySA Ultra")
            self.assertEqual(device.max_mhz, 5400)
            self.assertEqual(device.wifi_ranges, analyzer.WIFI_RANGES)
            with self.assertRaisesRegex(ValueError, "reaches 5400 MHz"):
                device.sweep(5470, 5925, count=1, timeout=5)
        finally:
            device.close()
            fake.unplug()

    def test_firmware_that_refuses_rather_than_clamps_gets_the_safe_guess(self) -> None:
        fake = FakeTinySa(clamps=False)
        device = tinysa.TinySa(fake.path)
        try:
            self.assertEqual(device.max_mhz, tinysa.ULTRA_REACH_MHZ)
        finally:
            device.close()
            fake.unplug()


class LateFirmwareTests(unittest.TestCase):
    def test_firmware_that_takes_any_stop_is_known_by_its_model(self) -> None:
        # v1.4-252 takes "sweep stop 12000000000" as asked, but "info" names the model
        fake = FakeTinySa(clamps=False, info="tinySA ULTRA+ ZS407")
        device = tinysa.TinySa(fake.path)
        try:
            self.assertEqual(device.name, "tinySA Ultra+")
            self.assertEqual(device.max_mhz, 7300)
        finally:
            device.close()
            fake.unplug()

    def test_the_end_of_an_earlier_scan_isnt_taken_for_the_answer(self) -> None:
        leftover = "1427000000 -1.107500e+02 0.000000000 \r\nch> "
        fake = FakeTinySa(leftover=leftover)
        try:
            self.assertEqual(tinysa.probe(fake.path), "tinySA4_v1.4-143-g864bb27")
        finally:
            fake.unplug()
        fake = FakeTinySa(leftover=leftover)
        device = tinysa.TinySa(fake.path)
        try:
            self.assertEqual(device.model, "tinySA4_v1.4-143-g864bb27")
            self.assertEqual(device.name, "tinySA Ultra+")
        finally:
            device.close()
            fake.unplug()


class IdentifyTests(unittest.TestCase):
    def test_a_tinysa_is_found_after_the_rf_explorer_question(self) -> None:
        # Not a ttyACM, so the RF Explorer is asked first: its bytes mustn't confuse the tinySA
        fake = FakeTinySa()
        try:
            with mock.patch.object(detect, "usb_ids", return_value=None):
                found = detect.identify(fake.path)
            self.assertIsInstance(found, tinysa.TinySa)
            found.close()
        finally:
            fake.unplug()

    def test_a_tinysa_usb_id_is_asked_first(self) -> None:
        with mock.patch.object(detect, "usb_ids", return_value=("0483", "5740")), \
                mock.patch.object(detect.tinysa, "probe", return_value="tinySA4_v1.4") as asked, \
                mock.patch.object(detect.tinysa, "TinySa", return_value="opened"), \
                mock.patch.object(detect.explorer, "probe") as explorer_probe:
            self.assertEqual(detect.identify("/dev/ttyACM0"), "opened")
        asked.assert_called_once()
        explorer_probe.assert_not_called()


class ParsingTests(unittest.TestCase):
    def test_scan_lines(self) -> None:
        text = "2400000000 -8.512e+01 \n2400222717 -9.0e+01 \nnonsense\n"
        self.assertEqual(tinysa.parse_scan(text), [-85.12, -90.0])

    def test_rbw_covers_the_step(self) -> None:
        self.assertEqual(tinysa.nearest_rbw(53, tinysa.ULTRA_RBW_KHZ), 100)
        self.assertEqual(tinysa.nearest_rbw(1100, tinysa.ULTRA_RBW_KHZ), 850)
        self.assertEqual(tinysa.nearest_rbw(1100, tinysa.ORIGINAL_RBW_KHZ), 600)

    def test_ranges_follow_reach(self) -> None:
        self.assertEqual(analyzer.wifi_ranges(6100), analyzer.WIFI_RANGES)
        self.assertEqual(analyzer.wifi_ranges(6500)[3], ("6 GHz to 6500 MHz", 5925.0, 6500.0))

    def test_tinysa_port_is_classified_as_an_analyzer(self) -> None:
        from wifisurveyor.net import gps

        self.assertEqual(gps.classify_serial(0x0483, 0x5740, "STM32 Virtual ComPort")[0], "rf")


if __name__ == "__main__":
    unittest.main()
