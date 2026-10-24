"""Checks for the Linux surveyor's parsers, project files, and heatmap buffer."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import analysis
from wifisurveyor.core.heatmap import render
from wifisurveyor.core.project import Point, Project, Wall
from wifisurveyor.net import gps, wifi
from wifisurveyor.net.wifi import Interface, _parse_iw, _parse_nmcli, _split_nmcli
from wifisurveyor.rf import spectrum


class ParseTests(unittest.TestCase):
    def test_nmcli_keeps_colons_inside_fields(self) -> None:
        line = r"AA\:BB\:CC\:DD\:EE\:FF:Cafe\:Net:2412 MHz:80:WPA2"
        self.assertEqual(
            _split_nmcli(line),
            ["AA:BB:CC:DD:EE:FF", "Cafe:Net", "2412 MHz", "80", "WPA2"],
        )
        reading = _parse_nmcli(line)[0]
        self.assertEqual(reading.ssid, "Cafe:Net")
        self.assertEqual(reading.bssid, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(reading.channel, 1)
        self.assertEqual(reading.security, "WPA2")
        self.assertTrue(reading.secured)

    def test_iw_scan_text(self) -> None:
        text = """
        BSS aa:bb:cc:dd:ee:ff(on wlan0)
                freq: 5180
                signal: -42.00 dBm
                SSID: Office
                RSN: * Version: 1
        BSS 11:22:33:44:55:66(on wlan0)
                freq: 2437
                signal: -70 dBm
                SSID:
        """
        readings = _parse_iw(text)
        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0].ssid, "Office")
        self.assertEqual(readings[0].channel, 36)
        self.assertEqual(readings[0].security, "RSN")
        self.assertEqual(readings[1].ssid, "")
        self.assertFalse(readings[1].secured)

    def test_iw_channel_widths(self) -> None:
        # As iw 6.x prints them: HT 40, VHT 80, VHT 160 (new style), 6 GHz HE 160, EHT 320
        text = """
BSS 00:00:00:00:00:01(on wlan0)
\tfreq: 2437
\tsignal: -50.00 dBm
\tHT operation:
\t\t * primary channel: 6
\t\t * secondary channel offset: below
\t\t * STA channel width: any
BSS 00:00:00:00:00:02(on wlan0)
\tfreq: 5180
\tsignal: -50.00 dBm
\tHT operation:
\t\t * secondary channel offset: above
\tVHT operation:
\t\t * channel width: 1 (80 MHz)
\t\t * center freq segment 1: 42
\t\t * center freq segment 2: 0
BSS 00:00:00:00:00:03(on wlan0)
\tfreq: 5500
\tsignal: -50.00 dBm
\tVHT operation:
\t\t * channel width: 1 (80 MHz)
\t\t * center freq segment 1: 106
\t\t * center freq segment 2: 114
BSS 00:00:00:00:00:04(on wlan0)
\tfreq: 6135
\tsignal: -50.00 dBm
\tHE capabilities:
\t\tHE PHY Capabilities: (0x0c):
\t\t\tSupported Channel Width: 40MHz in 5GHz, 80MHz in 5GHz, 160MHz in 5GHz
\tHE Operation:
\t\t6 GHz Operation Information: 0x25030f1700
\t\t\tPrimary Channel: 37
\t\t\tChannel Width: 80+80 or 160 MHz
\t\t\tCenter Frequency Segment 0: 39
\t\t\tCenter Frequency Segment 1: 47
BSS 00:00:00:00:00:05(on wlan0)
\tfreq: 6135
\tsignal: -50.00 dBm
\tHE Operation:
\t\t\tChannel Width: 80+80 or 160 MHz
\t\t\tCenter Frequency Segment 0: 39
\t\t\tCenter Frequency Segment 1: 47
\tEHT Operation:
\t\tEHT Operation Info: 0x041f
\t\t\tChannel Width: 320 MHz
\t\t\tCenter Frequency Segment 0: 47
\t\t\tCenter Frequency Segment 1: 31
BSS 00:00:00:00:00:06(on wlan0)
\tfreq: 5745
\tsignal: -50.00 dBm
"""
        spans = [(r.center_mhz, r.width_mhz) for r in _parse_iw(text)]
        self.assertEqual(spans, [(2427, 40), (5210, 80), (5570, 160), (6185, 160),
                                 (6105, 320), (5745, 20)])

    def test_adapter_offset_is_applied_to_scans(self) -> None:
        alfa = Interface("wlan1", "mt7921u", "managed", True, "Alfa AWUS036AXML", "0e8d:7961")
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": home}), \
                mock.patch.object(wifi, "interfaces", return_value=[alfa]), \
                mock.patch.object(wifi, "_scan", side_effect=lambda *_: _parse_iw(
                    "BSS 00:00:00:00:00:01(on wlan1)\n\tfreq: 2412\n\tsignal: -60 dBm\n")):
            self.assertEqual(wifi.adapter_key("wlan1"), "usb:0e8d:7961")
            wifi.set_adapter_offset("wlan1", 3.5)
            reading = wifi.scan("wlan1")[0]
            self.assertEqual((reading.rssi, reading.offset_db), (-56, 3.5))
            wifi.set_adapter_offset("wlan1", 0)
            self.assertEqual(wifi.scan("wlan1")[0].rssi, -60)

    def test_beacon_configuration(self) -> None:
        text = """BSS 0c:00:00:03:00:46(on wlan0)
\tfreq: 2437
\tsignal: -50.00 dBm
\tbeacon interval: 100 TUs
\tSSID: HomeNet
\tSupported rates: 1.0* 2.0* 5.5* 11.0* 9.0 18.0 36.0 54.0
\tExtended supported rates: 6.0 12.0 24.0 48.0
\tRSN:\t * Version: 1
\t\t * Authentication suites: FT/PSK PSK SAE
\tRM enabled capabilities:
\t\tCapabilities: 0x73 0x00 0x00 0x00 0x00
\tExtended capabilities:
\t\t * BSS Transition
"""
        reading = _parse_iw(text)[0]
        self.assertEqual(reading.basic_rates, [1.0, 2.0, 5.5, 11.0])
        self.assertEqual(len(reading.rates), 12)
        self.assertEqual(reading.beacon_interval, 100)
        self.assertEqual((reading.roam_k, reading.roam_v, reading.roam_r), (True, True, True))

    def test_stale_scan_results_are_dropped(self) -> None:
        text = "BSS 00:00:00:00:00:01(on wlan0)\n\tfreq: 2412\n\tsignal: -40 dBm\n\tlast seen: 800 ms ago\n" \
               "BSS 00:00:00:00:00:02(on wlan0)\n\tfreq: 2437\n\tsignal: -60 dBm\n\tlast seen: 25000 ms ago\n"
        readings = _parse_iw(text)
        self.assertEqual([r.age_ms for r in readings], [800, 25000])
        self.assertEqual([r.bssid for r in wifi.fresh(readings, scan_ms=3000)], ["00:00:00:00:00:01"])

    def test_channel_span_ignores_a_channel_the_primary_isnt_in(self) -> None:
        self.assertEqual(wifi.channel_span(5180, {"vht_width": 1, "vht_seg1": 155}), (5180, 20))

    def test_channel_numbers(self) -> None:
        self.assertEqual(wifi.channel_for(2412), 1)
        self.assertEqual(wifi.channel_for(2484), 14)
        self.assertEqual(wifi.channel_for(5180), 36)
        self.assertEqual(wifi.channel_for(5955), 1)
        self.assertEqual(spectrum.CHANNELS[1], 2412)
        self.assertIn(5955, [freq for _number, freq in spectrum.all_channels()])

    def test_nmea_checksum_and_sign(self) -> None:
        # Hand-built GGA: lat 48 07.038 N, lon 011 31.000 E, fix quality 1, 8 sats, HDOP 0.9
        body = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
        checksum = 0
        for char in body:
            checksum ^= ord(char)
        fix = gps.parse_gga(f"${body}*{checksum:02X}")
        self.assertIsNotNone(fix)
        assert fix is not None
        self.assertAlmostEqual(fix.lat, 48 + 7.038 / 60, places=5)
        self.assertAlmostEqual(fix.lon, 11 + 31 / 60, places=5)
        self.assertIsNone(gps.parse_gga(f"${body}*00"))

    def test_project_round_trip_ignores_unknown_keys(self) -> None:
        import tempfile
        from pathlib import Path

        project = Project(name="A/B: test", points=[Point(x=1, y=2, networks=[{"ssid": "a", "bssid": "b", "rssi": -40}])])
        project.walls.append(Wall(0, 0, 10, 0, db=5))
        with tempfile.TemporaryDirectory() as folder:
            saved = project.save(Path(folder) / "site.wsurvey")
            text = saved.read_text()
            payload = __import__("json").loads(text)
            payload["points"][0]["future"] = 1
            saved.write_text(__import__("json").dumps(payload))
            loaded = Project.load(saved)
        self.assertEqual(loaded.points[0].x, 1)
        self.assertEqual(loaded.walls[0].db, 5)
        self.assertEqual(loaded.name, "A/B: test")

    def test_filtered_signal_is_not_invented(self) -> None:
        point = Point(x=0, y=0, networks=[{"ssid": "Other", "bssid": "aa", "rssi": -50,
                                           "freq_mhz": 2412, "channel": 1, "band": "2.4"}])
        project = Project(points=[point])
        context = analysis.Context(project)
        self.assertIsNone(analysis.metric(point, "signal", context, ssid="Missing"))
        self.assertEqual(analysis.metric(point, "signal", context), -50)

    def test_heatmap_keeps_its_pixels(self) -> None:
        surface = render(60, 40, [(10, 10, -40), (40, 20, -80)], -85, -45, True, 30, cell=4)
        self.assertIsNotNone(surface)
        assert surface is not None
        import gc
        gc.collect()
        data = bytes(surface.get_data())
        self.assertTrue(any(data))
        self.assertEqual(surface.get_format(), 0)


class DeviceTests(unittest.TestCase):
    def test_alfa_ids_and_drivers(self) -> None:
        self.assertEqual(wifi.model_for("0e8d", "7612", "mt76x2u"), "Alfa AWUS036ACM")
        self.assertEqual(wifi.model_for("0e8d", "7961", ""), "Alfa AWUS036AXML")
        self.assertEqual(wifi.model_for("", "", "mt7921u"), "Alfa AWUS036AXML")
        self.assertEqual(wifi.model_for("", "", "iwlwifi"), "")

    def test_alfa_is_kept_for_monitor_mode(self) -> None:
        builtin = Interface("wlp1s0", "iwlwifi", "managed", True)
        acm = Interface("wlxacm", "mt76x2u", "managed", True, model="Alfa AWUS036ACM", usb_id="0e8d:7612")
        axml = Interface("wlxaxml", "mt7921u", "managed", True, model="Alfa AWUS036AXML", usb_id="0e8d:7961")
        scan, monitor = wifi.choose_roles([builtin, acm])
        self.assertEqual(scan, "wlp1s0")
        self.assertEqual(monitor, "wlxacm")
        scan, monitor = wifi.choose_roles([builtin, acm, axml])
        self.assertEqual(monitor, "wlxaxml")
        self.assertEqual(scan, "wlxacm")
        scan, monitor = wifi.choose_roles([axml])
        self.assertEqual(scan, "wlxaxml")
        self.assertEqual(monitor, "wlxaxml")

    def test_label_round_trip(self) -> None:
        self.assertEqual(wifi.device_from_label("wlx1 — Alfa AWUS036AXML"), "wlx1")
        self.assertEqual(wifi.device_from_label("none"), "")

    def test_serial_classification(self) -> None:
        self.assertEqual(gps.classify_serial(0x1546, 0x01A8, "u-blox GNSS")[0], "gps")
        self.assertEqual(gps.classify_serial(0x091E, 0x0003, "Garmin")[0], "gps")
        self.assertEqual(gps.classify_serial(None, None, "globalsat BU-353")[0], "gps")
        kind, description = gps.classify_serial(0x10C4, 0xEA60, "RF Explorer ISM Combo")
        self.assertEqual(kind, "rf")
        self.assertEqual(description, "RF Explorer")
        self.assertEqual(gps.classify_serial(0x10C4, 0xEA60, "CP2102 USB to UART")[0], "uart")
        self.assertEqual(gps.port_device("/dev/ttyUSB0 — RF Explorer"), "/dev/ttyUSB0")

    def test_root_command_uses_sudo_when_passwordless(self) -> None:
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": ""}):
            command = wifi.root_argv(True)
            self.assertEqual(command[:5], ["sudo", "-n", "-E", "env", "GDK_BACKEND=x11"])
            polkit = wifi.root_argv(False)
        self.assertEqual(polkit[0], "pkexec")
        self.assertIn("GDK_BACKEND=x11", polkit)

    def test_root_copy_stays_on_wayland(self) -> None:
        # Through XWayland, tapping a dropdown closes its list before an item can be chosen
        with tempfile.TemporaryDirectory() as runtime:
            Path(runtime, "wayland-0").touch()
            with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0",
                                              "XDG_RUNTIME_DIR": runtime}):
                command = wifi.root_argv(True)
                polkit = wifi.root_argv(False)
        socket = f"WAYLAND_DISPLAY={runtime}/wayland-0"
        for argv in (command, polkit):
            self.assertIn(socket, argv)
            self.assertIn("GDK_BACKEND=wayland,x11", argv)
            self.assertIn("GTK_IM_MODULE=wayland", argv)       # so GNOME's on-screen keyboard opens
            self.assertNotIn("WAYLAND_DISPLAY=wayland-0", argv)


if __name__ == "__main__":
    unittest.main()
