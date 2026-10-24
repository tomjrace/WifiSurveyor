"""Checks for survey files (both formats, recovery) and the RF Explorer's detection."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import interop, project as project_module
from wifisurveyor.core.project import Point, Project, Wall
from wifisurveyor.net import gps, wifi
from wifisurveyor.rf import detect, explorer

WINDOWS_SURVEY = {
    "FormatVersion": 1,
    "Name": "Head office",
    "FloorPlanBase64": None,
    "FloorPlanWidth": 1200,
    "FloorPlanHeight": 900,
    "MetersPerPixel": 0.04,
    "GeoRef": {"Anchors": [{"X": 1, "Y": 2, "Lat": 51.5, "Lon": -0.1}]},
    "Barriers": [
        {"X1": 0, "Y1": 0, "X2": 100, "Y2": 0, "Material": "Concrete", "Db": 12, "Suggested": False, "Inferred": False},
        {"X1": 5, "Y1": 5, "X2": 9, "Y2": 9, "Material": "Drywall", "Db": 3, "Suggested": True, "Inferred": False},
    ],
    "Track": [],
    "Issues": [],
    "Points": [{
        "Id": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "X": 120.5, "Y": 80.0,
        "Time": "2026-09-01T10:15:30.1234567",
        "Networks": [{"Ssid": "Office", "Bssid": "aa:bb:cc:dd:ee:ff", "Rssi": -52, "LinkQuality": 96,
                      "Channel": 36, "FrequencyMHz": 5180, "BandGHz": 5, "Secured": True, "Vendor": "Ubiquiti"}],
        "Connection": {"Ssid": "Office", "Bssid": "AA:BB:CC:DD:EE:FF", "SignalQuality": 90,
                       "RxRateMbps": 866.7, "TxRateMbps": 780, "PhyType": "802.11ac", "Auth": "WPA2-Personal"},
        "Links": [],
        "Spectrum": {"Segments": [{"StartMHz": 2400, "StepMHz": 0.9, "RbwKHz": 600, "SweepCount": 12,
                                   "Avg": [-90.0], "Max": [-80.0], "Min": [-99.0],
                                   "ChannelOccupancy": {"1": 0.25}}],
                     "NoiseFloor24": -91.5, "NoiseFloor5": -93.0, "Dwell": None, "DwellCenterMHz": None},
        "Gps": {"Lat": 51.5, "Lon": -0.12, "AltM": 20, "Quality": "RtkFixed", "Satellites": 14,
                "Hdop": 0.6, "AccuracyM": 0.02, "UtcTime": "2026-09-01T09:15:30Z", "Source": "NMEA"},
        "ThroughputDownMbps": 412.0, "ThroughputUpMbps": 305.5,
        "Capture": {"Channel": 36, "RetryRate": 0.12, "RetryEligible": 900, "AirtimeFraction": 0.33,
                    "Clients": 7, "Frames": 5000, "Bss": []},
        "Notes": ["by the lift"],
    }],
}


class WindowsFormatTests(unittest.TestCase):
    def test_reads_a_windows_survey(self) -> None:
        loaded = Project.from_dict(WINDOWS_SURVEY)
        self.assertEqual(loaded.name, "Head office")
        self.assertEqual((loaded.width, loaded.height, loaded.meters_per_pixel), (1200, 900, 0.04))
        self.assertEqual(len(loaded.walls), 1, "an unconfirmed Windows suggestion stays out of the maths")
        self.assertEqual(loaded.walls[0].db, 12)
        point = loaded.points[0]
        self.assertEqual(point.networks[0]["bssid"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(point.networks[0]["band"], "5")
        self.assertEqual(point.spectrum["noise_24"], -91.5)
        self.assertEqual(point.spectrum["segments"][0]["occupancy"]["1"], 0.25)
        self.assertEqual(point.gps["quality"], 4)
        self.assertEqual(point.capture["retry_eligible"], 900)
        self.assertEqual(point.link["tx_mbps"], 780)
        self.assertEqual(point.notes, ["by the lift"])

    def test_round_trip_keeps_what_only_windows_uses(self) -> None:
        back = interop.to_windows(Project.from_dict(WINDOWS_SURVEY))
        original = WINDOWS_SURVEY["Points"][0]
        point = back["Points"][0]
        self.assertEqual(back["GeoRef"], WINDOWS_SURVEY["GeoRef"])
        self.assertEqual(len(back["Barriers"]), 2)
        self.assertEqual(point["Id"], original["Id"])
        self.assertEqual(point["ThroughputDownMbps"], 412.0)
        self.assertEqual(point["Gps"]["Quality"], "RtkFixed")
        self.assertEqual(point["Gps"]["AltM"], 20)
        self.assertEqual(point["Connection"]["Auth"], "WPA2-Personal")
        self.assertEqual(point["Capture"]["Frames"], 5000)
        self.assertEqual(point["Networks"][0]["Rssi"], -52)
        self.assertEqual(point["Spectrum"]["Segments"][0]["ChannelOccupancy"], {"1": 0.25})

    def test_a_linux_survey_exports_with_windows_names(self) -> None:
        survey = Project(name="Lab", points=[Point(3, 4, networks=[{
            "ssid": "Lab", "bssid": "11:22:33:44:55:66", "rssi": -61, "freq_mhz": 2437,
            "channel": 6, "band": "2.4", "secured": False, "security": "Open"}])])
        survey.walls.append(Wall(0, 0, 1, 1, db=8, material="Brick"))
        exported = interop.to_windows(survey)
        self.assertTrue(interop.is_windows(exported))
        self.assertEqual(exported["Points"][0]["Networks"][0]["LinkQuality"], 78)
        self.assertEqual(exported["Barriers"][0]["Material"], "Brick")
        again = Project.from_dict(json.loads(json.dumps(exported)))
        self.assertEqual(again.points[0].networks[0]["rssi"], -61)
        self.assertAlmostEqual(again.points[0].time, survey.points[0].time, delta=1)

    def test_seven_digit_fractions_and_utc(self) -> None:
        self.assertIsNotNone(interop._parse_time("2026-09-01T10:15:30.1234567"))
        self.assertEqual(interop._parse_time("1970-01-01T00:00:10Z"), 10.0)
        self.assertIsNone(interop._parse_time("yesterday"))

    def test_something_else_is_refused_not_opened_empty(self) -> None:
        with self.assertRaises(ValueError):
            Project.from_dict({"hello": "world"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notes.wsurvey"
            path.write_text("not json")
            with self.assertRaises(ValueError):
                Project.load(path)


class RootCopyTests(unittest.TestCase):
    """The root copy (sudo keeps HOME=/root) must still save into the person's own folders."""

    def test_data_goes_to_the_person_not_root(self) -> None:
        import pwd

        with tempfile.TemporaryDirectory() as home:
            person = pwd.struct_passwd(("alex", "x", 1000, 1000, "", home, "/bin/bash"))
            with mock.patch("os.geteuid", return_value=0), \
                    mock.patch("os.chown") as chown, \
                    mock.patch("pwd.getpwuid", return_value=person), \
                    mock.patch.dict(os.environ, {"SUDO_UID": "1000", "HOME": "/root"}):
                os.environ.pop("XDG_DATA_HOME", None)
                folder = project_module.data_dir()
                self.assertEqual(folder, Path(home) / ".local" / "share" / "wifisurveyor")
                self.assertTrue(any(call.args[0] == folder for call in chown.call_args_list),
                                "the folders it makes are handed to the person")
                saved = Project(name="Site").save()
                # Surveys go to the shared folder in the person's Documents, handed to them
                shared = Path(home) / "Documents" / "Wi-Fi Surveys"
                self.assertEqual(saved.parent, shared)
                self.assertTrue(any(call.args[0] == shared for call in chown.call_args_list))
                self.assertIn("(as root)", json.loads(saved.read_text())["saved"]["by"])
                self.assertTrue(project_module.in_root_home("/root/.local/share/x.wsurvey"))
                with self.assertRaises(ValueError):
                    Project().save("/root/site.wsurvey")

    def test_not_root_leaves_paths_alone(self) -> None:
        with mock.patch("os.geteuid", return_value=1000):
            self.assertIsNone(project_module.invoking_user())
            self.assertFalse(project_module.in_root_home("/root/x"))


class RecoveryTests(unittest.TestCase):
    def test_recovery_names_its_file_and_clears(self) -> None:
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": folder}):
            survey = Project(points=[Point(1, 1)])
            survey.path = Path(folder) / "site.wsurvey"
            project_module.write_recovery(survey)
            self.assertFalse(survey.path.exists(), "the real file is not touched")
            found = project_module.read_recovery()
            self.assertIsNotNone(found)
            assert found is not None
            recovered, origin, _when = found
            self.assertEqual(origin, str(survey.path))
            self.assertTrue(recovered.modified)
            self.assertEqual(len(recovered.points), 1)
            project_module.clear_recovery()
            self.assertIsNone(project_module.read_recovery())

    def test_saving_clears_the_modified_flag(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            survey = Project(modified=True)
            survey.save(Path(folder) / "a.wsurvey")
            self.assertFalse(survey.modified)


class WifiTests(unittest.TestCase):
    def test_generation_comes_from_bitrates_not_the_ssid(self) -> None:
        self.assertEqual(wifi.link_generation("tx bitrate: 54.0 MBit/s"), "legacy")
        self.assertEqual(wifi.link_generation("tx bitrate: 1200.9 MBit/s 80MHz HE-MCS 11 HE-NSS 2"), "Wi-Fi 6 (ax)")
        self.assertEqual(wifi.link_generation("tx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz VHT-NSS 2"), "Wi-Fi 5 (ac)")
        self.assertEqual(wifi.link_generation("tx bitrate: 144.4 MBit/s MCS 15 short GI"), "Wi-Fi 4 (n)")
        self.assertEqual(wifi.link_generation("tx bitrate: 2882 MBit/s EHT-MCS 13"), "Wi-Fi 7 (be)")

    def test_six_gigahertz_channels(self) -> None:
        self.assertEqual(wifi.channel_for(5935), 2)
        self.assertEqual(wifi.channel_for(5955), 1)
        self.assertEqual(wifi.channel_for(7115), 233)

    def test_nmcli_readings_are_marked_as_estimates(self) -> None:
        reading = wifi._parse_nmcli(r"AA\:BB\:CC\:DD\:EE\:FF:Net:2412 MHz:80:WPA2")[0]
        self.assertEqual(reading.source, "nmcli")
        self.assertEqual(wifi._parse_iw("BSS aa:bb:cc:dd:ee:ff(on wlan0)\n freq: 2412\n signal: -40 dBm")[0].source, "iw")


class SerialPortTests(unittest.TestCase):
    def port(self, device, vid=None, pid=None, description="n/a", **extra):
        return mock.Mock(device=device, vid=vid, pid=pid, description=description,
                         manufacturer=extra.get("manufacturer"), product=extra.get("product"),
                         hwid=extra.get("hwid", "n/a"), serial_number=None)

    def test_placeholder_uarts_are_hidden_and_bridges_named(self) -> None:
        ports = [self.port("/dev/ttyS0"), self.port("/dev/ttyS31"),
                 self.port("/dev/ttyUSB0", 0x10C4, 0xEA60, "CP2102 USB to UART Bridge Controller")]
        with mock.patch("serial.tools.list_ports.comports", return_value=ports), \
                mock.patch("pathlib.Path.is_dir", return_value=False):
            found = gps.serial_devices()
        self.assertEqual([d.device for d in found], ["/dev/ttyUSB0"])
        self.assertEqual(found[0].description, "USB serial (CP2102)")


class ExplorerDetectionTests(unittest.TestCase):
    def test_permission_is_reported_not_swallowed(self) -> None:
        denied = OSError(13, "Permission denied")
        denied.errno = 13
        with mock.patch("serial.Serial", side_effect=denied):
            with self.assertRaises(explorer.PortPermissionError):
                explorer.probe("/dev/ttyUSB0")
        with mock.patch.object(gps, "rf_probe_order", return_value=["/dev/ttyUSB0"]), \
                mock.patch.object(detect, "usb_ids", return_value=None), \
                mock.patch.object(detect.explorer, "probe",
                                  side_effect=explorer.PortPermissionError("/dev/ttyUSB0")):
            found = detect.find()
        self.assertIsNone(found.explorer)
        self.assertEqual(found.denied, ["/dev/ttyUSB0"])
        self.assertIn("Grant serial access", found.message)

    def test_first_port_that_answers_is_opened_at_its_baud(self) -> None:
        opened = mock.Mock(device="/dev/ttyUSB1")
        opened.name = "RF Explorer"
        with mock.patch.object(gps, "rf_probe_order", return_value=["/dev/ttyUSB0", "/dev/ttyUSB1"]), \
                mock.patch.object(detect, "usb_ids", return_value=None), \
                mock.patch.object(detect.tinysa, "probe", return_value=None), \
                mock.patch.object(detect.explorer, "probe", side_effect=[None, 2400]), \
                mock.patch.object(detect.explorer, "RfExplorer", return_value=opened) as make:
            found = detect.find()
        make.assert_called_once_with("/dev/ttyUSB1", 2400)
        self.assertIs(found.explorer, opened)
        self.assertEqual(found.message, "RF Explorer found on /dev/ttyUSB1")

    def test_probe_falls_back_to_the_slow_baud_rate(self) -> None:
        def fake_serial(port, baud, **_kwargs):
            link = mock.Mock()
            link.read.return_value = b"#C2-F:0024000,0000100" if baud == 2400 else b""
            return link

        with mock.patch("serial.Serial", side_effect=fake_serial), \
                mock.patch("time.monotonic", side_effect=[0, 0, 5, 0, 0, 5]):
            self.assertEqual(explorer.probe("/dev/ttyUSB0"), 2400)

    def test_udev_rule_grants_the_seat_user(self) -> None:
        rule = detect.udev_rule([("10c4", "ea60"), ("10c4", "ea60")])
        self.assertEqual(rule.count("TAG+=\"uaccess\""), 1)
        self.assertIn('ATTRS{idVendor}=="10c4"', rule)

    def test_config_line_is_parsed(self) -> None:
        device = explorer.RfExplorer.__new__(explorer.RfExplorer)
        device.config = None
        device.model = ""
        device._config_event = mock.Mock()
        device._handle("C2-F:2400000,0892857,-010,-120,0112,1,000,2350000,2550000,0100000,00600")
        self.assertAlmostEqual(device.config.start_mhz, 2400.0)
        self.assertEqual(device.config.points, 112)
        self.assertTrue(device.config.expansion)
        self.assertAlmostEqual(device.config.max_mhz, 2550.0)


if __name__ == "__main__":
    unittest.main()
