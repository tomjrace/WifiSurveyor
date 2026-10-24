"""Checking for device updates (with the network and apt faked), and the tinySA as a generator."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import updates
from wifisurveyor.rf import tinysa

LISTING = """<html><body><pre>
<a href="old/">old/</a>
<a href="tinySA4_v1.4-143-g864bb27.bin">tinySA4_v1.4-143-g864bb27.bin</a> 2026-01-01
<a href="tinySA4_v1.4-252-g2b4aa51.bin">tinySA4_v1.4-252-g2b4aa51.bin</a> 2026-09-30
<a href="tinySA4_v1.4-252-g2b4aa51.dfu">tinySA4_v1.4-252-g2b4aa51.dfu</a> 2026-09-30
<a href="tinySA4_v1.3-900-gaaaaaaa.bin">tinySA4_v1.3-900-gaaaaaaa.bin</a> 2025-01-01
</pre></body></html>"""


class Answer(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class VersionTest(unittest.TestCase):
    def test_comparisons(self):
        self.assertTrue(updates.newer("2.0.3", "2.0.2-2build1"))
        self.assertFalse(updates.newer("2026.01.3-1", "2026.01.3-1"))
        self.assertTrue(updates.newer("v2026.02.1", "2026.01.3"))
        self.assertEqual(updates._upstream("1:2.0.2-2build1"), "2.0.2")
        self.assertGreater(updates.tinysa_version_key("tinySA4_v1.4-252-g2b4aa51"),
                           updates.tinysa_version_key("tinySA4_v1.4-143-g864bb27-dirty"))
        self.assertGreater(updates.tinysa_version_key("tinySA4_v1.4-1-g0"),
                           updates.tinysa_version_key("tinySA4_v1.3-900-g0"))

    def test_apt_policy(self):
        policy = "rtl-sdr:\n  Installed: 2.0.2-2build1\n  Candidate: 2.0.2-3\n  Version table:\n"
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=policy)):
            self.assertEqual(updates.apt_versions("rtl-sdr"), ("2.0.2-2build1", "2.0.2-3"))
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="x:\n  Installed: (none)\n  Candidate: 1.0\n")):
            self.assertEqual(updates.apt_versions("x"), (None, "1.0"))

    def test_hackrf_firmware_image_version(self):
        with tempfile.NamedTemporaryFile(suffix=".bin") as image:
            image.write(b"\x00" * 100 + b"2026.01.3\x00more")
            image.flush()
            self.assertEqual(updates.firmware_file_version(Path(image.name)), "2026.01.3")


class TinySaUpdateTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.folder.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_finds_the_newest_and_offers_it(self):
        device = mock.Mock(ultra=True, model="tinySA4_v1.4-143-g864bb27", spec=["ultra", "model", "name"])
        device.name = "tinySA Ultra"
        with mock.patch("wifisurveyor.net.outbound._opener.open", return_value=Answer(LISTING.encode())):
            status = updates.check_tinysa(device)
        self.assertEqual([a.key for a in status.actions], ["tinysa_flash"])
        self.assertIn("v1.4-252", status.actions[0].label)
        self.assertIn("jog button", status.actions[0].confirm)

    def test_up_to_date(self):
        device = mock.Mock(ultra=True, model="tinySA4_v1.4-252-g2b4aa51", spec=["ultra", "model", "name"])
        with mock.patch("wifisurveyor.net.outbound._opener.open", return_value=Answer(LISTING.encode())):
            status = updates.check_tinysa(device)
        self.assertTrue(status.current)

    def test_refuses_a_file_that_isnt_that_firmware(self):
        version = "tinySA4_v1.4-252-g2b4aa51"
        with mock.patch("wifisurveyor.net.outbound._opener.open", return_value=Answer(b"\x00" * 50_000)), \
                mock.patch.object(updates, "run_logged") as flashed:
            with self.assertRaises(RuntimeError):
                updates.flash_tinysa(None, version, "http://x/" + version + ".bin", lambda _t: None)
        flashed.assert_not_called()

    def test_flashes_once_it_appears_in_dfu_mode(self):
        version = "tinySA4_v1.4-252-g2b4aa51"
        image = b"\x00" * 60_000 + version.encode() + b"-dirty"
        released = []
        appear = iter([False, False, True])
        with mock.patch("wifisurveyor.net.outbound._opener.open", return_value=Answer(image)), \
                mock.patch.object(updates, "usb_present", side_effect=lambda _ids: next(appear)), \
                mock.patch.object(updates.shutil, "which", return_value="/usr/bin/dfu-util"), \
                mock.patch.object(updates.time, "sleep"), \
                mock.patch.object(updates, "root_command", side_effect=lambda c: c), \
                mock.patch.object(updates, "run_logged", return_value=(0, "File downloaded successfully")) as run:
            message = updates.flash_tinysa(mock.Mock(), version, "http://x/f.bin", lambda _t: None,
                                           release=lambda: released.append(True))
        self.assertEqual(released, [True])
        command = run.call_args[0][0]
        self.assertEqual(command[:3], ["dfu-util", "-d", "0483:df11"])
        self.assertIn("0x08000000:leave", command)
        self.assertIn("Flashed", message)


class RfExplorerTest(unittest.TestCase):
    def test_firmware_from_the_model_line(self):
        from wifisurveyor.rf.explorer import RfExplorer

        device = RfExplorer.__new__(RfExplorer)
        device.model = "006,005,01.33"
        self.assertEqual(device.firmware, "01.33")

    def test_runs_the_uploader_as_the_person(self):
        with mock.patch.object(updates, "invoking_user", return_value=(1000, 1000, Path("/home/alex"))), \
                mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_name="alex")), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            command, env = updates.as_person(["mono", "RFEUploader.exe"])
        self.assertEqual(command[:4], ["runuser", "-u", "alex", "--"])
        self.assertIn("HOME=/home/alex", command)
        self.assertIn("DISPLAY=:0", command)
        self.assertEqual(command[-2:], ["mono", "RFEUploader.exe"])
        self.assertIn("PATH", env)


class TinySaGeneratorTest(unittest.TestCase):
    def test_commands_turn_the_output_on_last(self):
        commands = tinysa.generator_commands(915.0, -30, "off", ultra=True)
        self.assertEqual(commands[0], "mode high output")
        self.assertEqual(commands[-1], "output on")
        self.assertIn("freq 915000000", commands)
        self.assertEqual(tinysa.generator_commands(500, -30, "NFM", True)[0], "mode low output")
        self.assertEqual(tinysa.output_mode(500, ultra=False), "high")
        with self.assertRaises(ValueError):
            tinysa.generator_commands(500, -30, "FM stereo", True)

    def test_generating_stops_sweeping_until_stopped(self):
        from tests.test_tinysa_pty import FakeTinySa

        fake = FakeTinySa()
        device = tinysa.TinySa(fake.path)
        try:
            device.generate(915.0, -30)
            self.assertEqual(device.generating, 915.0)
            self.assertIn("output on", fake.commands)
            with self.assertRaises(RuntimeError):
                device.configure(900, 930)
            device.stop_generating()
            self.assertIsNone(device.generating)
            self.assertIn("output off", fake.commands)
            self.assertEqual(fake.commands[-1], "mode low input")
            device.configure(900, 930)
        finally:
            device.close()
            fake.unplug()


if __name__ == "__main__":
    unittest.main()
