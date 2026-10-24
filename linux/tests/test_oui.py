import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.net import oui

HEADER = "Registry,Assignment,Organization Name,Organization Address\n"


class OuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        (self.folder / "oui.csv").write_text(
            HEADER + 'MA-L,40F4C9,FN-LINK TECHNOLOGY Ltd.,"Somewhere, CN"\n'
                     "MA-L,70B3D5,IEEE Registration Authority,\n")
        (self.folder / "oui36.csv").write_text(HEADER + "MA-S,70B3D5001,SOREDI touch systems GmbH,\n")
        patches = [mock.patch.object(oui, "download_dir", lambda: self.folder),
                   mock.patch.object(oui, "SYSTEM_DIRS", ())]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        oui.reload()
        self.addCleanup(oui.reload)

    def test_longest_block_wins(self) -> None:
        self.assertEqual(oui.vendor("40:f4:c9:00:00:ae"), "FN-LINK TECHNOLOGY Ltd.")
        self.assertEqual(oui.vendor("70-B3-D5-00-1F-FF"), "SOREDI touch systems GmbH")
        self.assertEqual(oui.vendor("00:00:00:00:00:01"), "")

    def test_locally_administered_addresses(self) -> None:
        self.assertEqual(oui.vendor("42:F4:C9:00:00:AE", access_point=True),
                         "FN-LINK TECHNOLOGY Ltd. (virtual BSSID)")
        self.assertEqual(oui.vendor("42:F4:C9:00:00:AE"), "Private address")

    def test_no_database(self) -> None:
        (self.folder / "oui.csv").unlink()
        oui.reload()
        self.assertEqual(oui.vendor("40:f4:c9:00:00:ae"), "")
        self.assertIn("not found", oui.describe())


if __name__ == "__main__":
    unittest.main()
