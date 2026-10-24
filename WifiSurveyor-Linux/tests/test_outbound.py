"""The one door to the internet: what it refuses, who it says it is, and that nothing goes round it."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import settings
from wifisurveyor.net import outbound

ROOT = Path(__file__).resolve().parent.parent


class GateTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.folder.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_identity_is_fixed_and_says_nothing_about_the_user(self):
        request = outbound.build("https://tile.openstreetmap.org/1/0/0.png",
                                 headers={"User-Agent": "me (someone@example.com)",
                                          "From": "someone@example.com", "Accept": "image/png"})
        self.assertEqual(request.get_header("User-agent"), outbound.USER_AGENT)
        self.assertIsNone(request.get_header("From"))
        self.assertEqual(request.get_header("Accept"), "image/png")
        self.assertNotIn("@", outbound.USER_AGENT + outbound.BROWSER_LIKE)
        ieee = outbound.build("https://standards-oui.ieee.org/oui/oui.csv")
        self.assertEqual(ieee.get_header("User-agent"), outbound.BROWSER_LIKE)

    def test_email_addresses_never_leave(self):
        for url, data, headers in (
                ("https://nominatim.openstreetmap.org/search?q=someone@example.com", None, None),
                ("https://nominatim.openstreetmap.org/search?q=someone%40example.com", None, None),
                ("https://api.beacondb.net/v1/geolocate", b'{"contact": "a.b@mail.example.org"}', None),
                ("https://api.github.com/repos/x/y", None, {"X-Note": "write to me@example.net"})):
            with self.assertRaises(outbound.PersonalDataError):
                outbound.build(url, data, headers)

    def test_report_details_never_leave(self):
        settings.put("report_surveyor", "Jordan Example")
        settings.put("report_contact", "555 0100, Jordan Example Surveys")
        with self.assertRaises(outbound.PersonalDataError):
            outbound.build("https://nominatim.openstreetmap.org/search?q=Jordan+Example")
        # An ordinary address search is fine
        outbound.build("https://nominatim.openstreetmap.org/search?q=350+5th+Avenue+New+York")

    def test_only_listed_hosts_and_this_network(self):
        with self.assertRaises(outbound.HostNotAllowed):
            outbound.build("https://tracker.example.com/collect")
        with self.assertRaises(outbound.HostNotAllowed):
            outbound.build("file:///etc/passwd")
        with self.assertRaises(outbound.PersonalDataError):
            outbound.build("https://user:pw@api.github.com/")
        for local in ("http://192.168.1.40:8737/status", "http://127.0.0.1:8737/info",
                      "http://benchpi:8737/info", "http://benchpi.local:8737/info"):
            outbound.build(local)

    def test_a_redirect_elsewhere_is_refused(self):
        handler = outbound._CheckedRedirects()
        request = outbound.build("https://github.com/x/y/releases/download/v1/a.tar.xz")
        with self.assertRaises(outbound.HostNotAllowed):
            handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.example/a")
        allowed = handler.redirect_request(request, None, 302, "Found", {},
                                           "https://objects.githubusercontent.com/a")
        self.assertEqual(allowed.get_header("User-agent"), outbound.USER_AGENT)


class NothingGoesRoundTheGateTest(unittest.TestCase):
    def test_no_other_code_opens_web_requests(self):
        offenders = []
        pattern = re.compile(r"urlopen|urllib\.request|http\.client|import requests|httpx|aiohttp")
        for path in (ROOT / "wifisurveyor").rglob("*.py"):
            if path.name == "outbound.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "web requests must go through net/outbound.py")

    def test_no_email_addresses_in_the_app(self):
        found = []
        for path in [*(ROOT / "wifisurveyor").rglob("*.py"), *(ROOT / "satellite").rglob("*")]:
            if path.is_file():
                for match in outbound.EMAIL.finditer(path.read_text(errors="replace")):
                    if not match.group().endswith(("example.com", "example.net", "example.org")):
                        found.append(f"{path.relative_to(ROOT)}: {match.group()}")
        self.assertEqual(found, [])

    def test_privacy_document_lists_every_host(self):
        text = (ROOT / "PRIVACY.md").read_text()
        missing = [host for host in outbound.HOSTS if f"`{host}`" not in text]
        self.assertEqual(missing, [], "PRIVACY.md must list every host the app contacts")
        listed = set(re.findall(r"^\| `([a-z0-9.\-]+)`", text, re.M))
        self.assertEqual(listed - set(outbound.HOSTS), set(), "PRIVACY.md lists a host the app doesn't use")


class SettingsCacheTests(unittest.TestCase):
    """Lookups must not re-read the file, and must not keep another folder's settings."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.folder.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.folder.cleanup()

    def test_a_lookup_does_not_reread_or_keep_a_callers_change(self) -> None:
        settings.put("report_logo", {"mime": "image/png", "data": "x" * 1000})
        path = settings._path()
        reads = {"n": 0}
        original = Path.read_text

        def counting(target, *args, **kwargs):
            if target == path:
                reads["n"] += 1
            return original(target, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counting):
            logo = settings.get("report_logo")
            self.assertEqual(logo["data"], "x" * 1000)
            logo["data"] = "changed by the caller"
            self.assertEqual(settings.get("report_logo")["data"], "x" * 1000)
            self.assertIsNone(settings.get("easy_mode"))
            self.assertEqual(reads["n"], 0)
        settings.put("easy_mode", True)
        self.assertTrue(settings.get("easy_mode"))

    def test_an_edit_outside_this_process_is_read_again(self) -> None:
        settings.put("easy_mode", False)
        settings._path().write_text(json.dumps({"easy_mode": True}))
        self.assertTrue(settings.get("easy_mode"))

    def test_another_data_folder_is_not_the_cached_one(self) -> None:
        settings.put("report_surveyor", "Ada")
        with tempfile.TemporaryDirectory() as other, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": other}):
            self.assertIsNone(settings.get("report_surveyor"))
        self.assertEqual(settings.get("report_surveyor"), "Ada")


if __name__ == "__main__":
    unittest.main()
