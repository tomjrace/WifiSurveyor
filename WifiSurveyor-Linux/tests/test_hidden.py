import unittest

from scapy.layers.dot11 import (
    Dot11, Dot11AssoReq, Dot11Beacon, Dot11Elt, Dot11ProbeResp, RadioTap,
)

from wifisurveyor.core import hidden, report, surveysetup
from wifisurveyor.core.project import Point, Project
from wifisurveyor.net import capture
from wifisurveyor.net.wifi import _parse_iw

AP, PHONE = "00:11:22:33:44:55", "aa:bb:cc:dd:ee:01"


def frame(subtype, layer, ssid: bytes, sender, receiver):
    return (RadioTap(present="Flags+Channel+dBm_AntSignal", ChannelFrequency=2437, dBm_AntSignal=-50)
            / Dot11(type=0, subtype=subtype, addr1=receiver, addr2=sender, addr3=AP)
            / layer() / Dot11Elt(ID=0, info=ssid) / Dot11Elt(ID=3, info=b"\x06"))


def beacon(ssid: bytes):
    return frame(8, Dot11Beacon, ssid, AP, "ff:ff:ff:ff:ff:ff")


def heard(*frames) -> dict:
    analyzer = capture.Analyzer("test")
    for packet in frames:
        analyzer.add(RadioTap(bytes(packet)))
    return capture.hidden_names(analyzer.summary())


def network(ssid, bssid=AP, rssi=-60):
    return {"ssid": ssid, "bssid": bssid.upper(), "rssi": rssi, "band": "2.4", "channel": 6,
            "freq_mhz": 2437}


class CaptureNames(unittest.TestCase):
    def test_probe_response_names_a_hidden_access_point(self):
        answer = frame(5, Dot11ProbeResp, b"Back Office", AP, PHONE)
        self.assertEqual(heard(beacon(b"\x00" * 8), answer), {AP.upper(): "Back Office"})

    def test_probe_response_heard_before_the_beacon(self):
        answer = frame(5, Dot11ProbeResp, b"Back Office", AP, PHONE)
        self.assertEqual(heard(answer, beacon(b"")), {AP.upper(): "Back Office"})

    def test_device_joining_names_it(self):
        joining = frame(0, Dot11AssoReq, b"Back Office", PHONE, AP)
        self.assertEqual(heard(beacon(b""), joining), {AP.upper(): "Back Office"})

    def test_a_network_that_shows_its_name_isnt_hidden(self):
        answer = frame(5, Dot11ProbeResp, b"Guest", AP, PHONE)
        self.assertEqual(heard(beacon(b"Guest"), answer), {})

    def test_hidden_and_never_named(self):
        self.assertEqual(heard(beacon(b"")), {})


class SurveyNames(unittest.TestCase):
    def project(self):
        project = Project()
        project.points = [Point(x=1, y=1, networks=[network(""), network("Guest", "00:11:22:33:44:99")]),
                          Point(x=2, y=2, networks=[network("", rssi=-50)])]
        return project

    def test_is_hidden(self):
        for name in ("", "\x00\x00", "\\x00\\x00\\x00", "  "):
            self.assertTrue(hidden.is_hidden(name), repr(name))
        self.assertFalse(hidden.is_hidden("Office"))

    def test_iw_null_padded_name_reads_as_hidden(self):
        readings = _parse_iw("BSS 00:11:22:33:44:55(on wlan0)\n\tfreq: 2437\n\tsignal: -50.00 dBm\n"
                             "\tSSID: \\x00\\x00\\x00\\x00\n")
        self.assertEqual(readings[0].ssid, "")

    def test_learning_names_every_point(self):
        project = self.project()
        self.assertEqual(hidden.learn(project, {AP.lower(): "Back Office"}), [AP.upper()])
        for point in project.points:
            ap = next(n for n in point.networks if n["bssid"] == AP.upper())
            self.assertEqual((ap["ssid"], ap["hidden"]), ("Back Office", True))
        guest = project.points[0].networks[1]
        self.assertEqual(guest["ssid"], "Guest")
        self.assertNotIn("hidden", guest)
        self.assertEqual(hidden.learn(project, {AP: "Back Office"}), [])      # nothing new
        self.assertEqual(hidden.unnamed(project), [])

    def test_names_survive_saving_and_carry_into_a_follow_up(self):
        project = self.project()
        hidden.learn(project, {AP: "Back Office"})
        again = Project.from_dict(project.to_dict())
        self.assertEqual(again.hidden_names, {AP.upper(): "Back Office"})
        later = project.follow_up()
        later.points = [Point(x=0, y=0, networks=[network("")])]
        hidden.apply_all(later)
        self.assertEqual(later.points[0].networks[0]["ssid"], "Back Office")

    def test_report_lists_hidden_networks(self):
        project = self.project()
        rows = hidden.report_rows(project)
        self.assertEqual([(r["name"], r["points"], r["best"]) for r in rows], [("", 2, -50)])
        hidden.learn(project, {AP: "Back Office"})
        html = report._hidden_networks(project)
        self.assertIn("Hidden networks", html)
        self.assertIn("Back Office", html)
        self.assertIn("To improve", html)
        self.assertEqual(report._hidden_networks(Project()), "")

    def test_label(self):
        self.assertEqual(hidden.label("", True), "(hidden)")
        self.assertEqual(hidden.label("Back Office", True), "Back Office (hidden)")
        self.assertEqual(hidden.label("Guest"), "Guest")


class SetupChoice(unittest.TestCase):
    def test_on_for_thorough_and_security(self):
        on = {key for key, _n, _a, setup in surveysetup.TYPES if setup.hidden_names}
        self.assertEqual(on, {"thorough", "security"})

    def test_saved_before_the_choice_keeps_its_type(self):
        saved = surveysetup.preset("thorough").to_dict()
        del saved["hidden_names"]
        setup = surveysetup.Setup.from_dict(saved)
        self.assertTrue(setup.hidden_names)
        self.assertEqual(surveysetup.matching_kind(setup), "thorough")


if __name__ == "__main__":
    unittest.main()
