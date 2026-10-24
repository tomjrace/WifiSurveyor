"""The Discover page's lists: graded access points, clients from monitor mode, Bluetooth."""

from __future__ import annotations

import math
import threading
import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from wifisurveyor.core import rfplan  # noqa: E402
from wifisurveyor.core.colours import signal_quality, snr_quality
from wifisurveyor.net import bluetooth, capture
from wifisurveyor.net.wifi import Reading
from wifisurveyor.rf.analyzer import Config
from wifisurveyor.ui import discover

DATA = rfplan.PROFILES["data"]          # -70 dBm, 20 dB SNR


def reading(bssid, rssi, freq=2437.0, ssid="Office"):
    return Reading(ssid=ssid, bssid=bssid, rssi=rssi, freq_mhz=freq,
                   channel=6 if freq < 3000 else 36, secured=True)


class FakeAnalyzer:
    """Sweeps a flat floor at -100 dBm in a 300 kHz resolution, whatever the range."""

    def __init__(self, max_mhz=7300.0):
        self.max_mhz = max_mhz
        self.lock = threading.RLock()
        self.swept = []

    def sweep_range(self, start, end, count=3):
        self.swept.append((start, end))
        config = Config(start, (end - start) / 99, 100, False, 0, self.max_mhz, 1e9, 300.0)
        return [(config, [[-100.0] * 100 for _ in range(count)])]


class GradingTest(unittest.TestCase):
    def test_signal_and_snr_grades(self):
        self.assertEqual(signal_quality(-60, DATA.primary), "good")
        self.assertEqual(signal_quality(-72, DATA.primary), "fair")
        self.assertEqual(signal_quality(-80, DATA.primary), "poor")
        self.assertEqual(snr_quality(30, 20), "good")
        self.assertEqual(snr_quality(22, 20), "fair")
        self.assertEqual(snr_quality(12, 20), "poor")
        self.assertEqual(snr_quality(None), "poor")

    def test_access_points_get_snr_from_their_bands_floor(self):
        rows = discover.ap_rows(
            [reading("aa:00:00:00:00:01", -80), reading("aa:00:00:00:00:02", -55, freq=5180.0)],
            DATA, floors={"2.4": -95.0, "5": -92.0}, estimated={"5"},
            clients={"aa:00:00:00:00:02": 4})
        strong, weak = rows
        self.assertEqual(strong["bssid"], "aa:00:00:00:00:02")
        self.assertEqual((strong["snr"], strong["_grade_snr"], strong["_grade_signal"]),
                         ("≈37 dB", "good", "good"))
        self.assertEqual(strong["clients"], 4)
        self.assertEqual((weak["snr"], weak["_grade_snr"], weak["_grade_signal"]),
                         ("15 dB", "poor", "poor"))
        self.assertEqual(weak["clients"], 0)

    def test_without_an_analyzer_there_is_no_snr(self):
        row = discover.ap_rows([reading("aa:00:00:00:00:01", -68)], DATA)[0]
        self.assertEqual((row["snr"], row["clients"]), ("", ""))
        self.assertNotIn("_grade_snr", row)
        self.assertEqual(row["_grade_signal"], "fair")


class NoiseTest(unittest.TestCase):
    def test_every_wifi_band_is_measured_per_20_mhz(self):
        analyzer = FakeAnalyzer()
        floors, sources, notes = discover.measure_noise([analyzer])
        self.assertEqual(set(floors), {"2.4", "5", "6"})
        # -100 dBm in 300 kHz is about -81.8 dBm across 20 MHz
        self.assertAlmostEqual(floors["2.4"], -100 + 10 * math.log10(20000 / 300), 1)
        self.assertIs(sources["5"], analyzer)
        self.assertEqual(notes, [])

    def test_a_failed_range_is_a_note_not_a_failure(self):
        analyzer = FakeAnalyzer()
        real = analyzer.sweep_range

        def sweep(start, end, count=3):
            if start > 5900:
                raise TimeoutError("No sweeps arrived.")
            return real(start, end, count)

        analyzer.sweep_range = sweep
        floors, _sources, notes = discover.measure_noise([analyzer])
        self.assertNotIn("6", floors)
        self.assertIn("2.4", floors)
        self.assertEqual(len(notes), 1)


class ClientsTest(unittest.TestCase):
    def summary(self):
        office = capture.BssStat(bssid="78:9f:6a:00:00:d1", ssid="Office")
        office.clients = {"a4:83:e7:00:00:01"}
        laptop = capture.DeviceStat(mac="a4:83:e7:00:00:01", frames=40, signal_sum=-2400,
                                    signal_count=40, last_seen=1000.0)
        laptop.bssids = {"78:9f:6a:00:00:d1"}
        phone = capture.DeviceStat(mac="6a:11:22:33:44:55", frames=3, signal_sum=-240,
                                   signal_count=3, last_seen=990.0)
        phone.probes = {"HomeWifi", ""}
        gone = capture.DeviceStat(mac="00:11:22:33:44:55", frames=1, last_seen=100.0)
        return capture.CaptureSummary(bss=[office], devices=[phone, laptop, gone])

    def test_connected_and_probing_clients_strongest_first(self):
        rows = discover.client_rows(self.summary(), DATA, now=1005.0)
        self.assertEqual([r["mac"] for r in rows], ["A4:83:E7:00:00:01", "6A:11:22:33:44:55"])
        laptop, phone = rows
        self.assertEqual((laptop["network"], laptop["bssid"]), ("Office", "78:9F:6A:00:00:D1"))
        self.assertEqual((laptop["signal"], laptop["_grade_signal"]), ("-60 dBm", "good"))
        self.assertEqual((phone["network"], phone["probes"]), ("Not connected", "HomeWifi"))
        self.assertEqual(phone["vendor"], "Private address")
        self.assertEqual(phone["_grade_signal"], "poor")
        self.assertEqual(laptop["heard"], "5 s ago")


class BluetoothTest(unittest.TestCase):
    def test_devices_and_tags_merge_and_age_out(self):
        printer = bluetooth.BtDevice("00:11:22:33:44:55", "Office printer", -58, True, 0x06_80)
        old = bluetooth.BtDevice("00:11:22:33:44:66", "Gone", -40, True, 0)
        tag = {"address": "5A:00:00:00:00:01", "rssi": -70, "kind": "airtag", "label": "AirTag",
               "away": True, "state": "away", "battery": ""}
        rows = discover.bt_rows({printer.address: (100.0, printer), old.address: (-100.0, old)},
                                {tag["address"]: (98.0, tag)}, now=100.0)
        self.assertEqual([r["address"] for r in rows], ["00:11:22:33:44:55", "5A:00:00:00:00:01"])
        self.assertEqual((rows[0]["kind"], rows[0]["fixed"]), ("Printer", "Fixed"))
        self.assertEqual(rows[1]["kind"], "AirTag, away from its owner")
        self.assertEqual(rows[1]["_grade_kind"], "poor")


if __name__ == "__main__":
    unittest.main()
