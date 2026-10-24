import unittest
from unittest import mock

from wifisurveyor.net import connectivity, throughput

PING = """10 packets transmitted, 8 received, 20% packet loss, time 1810ms
rtt min/avg/max/mdev = 2.101/3.922/9.004/0.873 ms"""


class ConnectivityTests(unittest.TestCase):
    def test_ping_summary(self) -> None:
        self.assertEqual(connectivity.parse_ping(PING),
                         {"loss": 20.0, "latency_ms": 3.922, "jitter_ms": 0.873})

    def test_problems(self) -> None:
        self.assertIsNone(connectivity.problem({"loss": 0.0, "latency_ms": 3}))
        self.assertEqual(connectivity.problem({"loss": 20.0}), "20% packet loss to the gateway")
        self.assertEqual(connectivity.problem({"error": "no gateway"}), "no gateway")
        self.assertIn("20% loss", connectivity.describe({**connectivity.parse_ping(PING), "dns_ms": 30}))



def fake_nmcli(profiles: dict[str, tuple[str, str]]):
    """nmcli answering from {profile name: (ssid, key-mgmt)}."""
    def run(*args, timeout=None):
        if args[:6] == ("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"):
            return 0, "\n".join(f"{name}:802-11-wireless" for name in profiles)
        if args[:3] == ("nmcli", "-g", "802-11-wireless.ssid"):
            return 0, profiles[args[-1]][0]
        if args[:3] == ("nmcli", "-g", "802-11-wireless-security.key-mgmt"):
            return 0, profiles[args[-1]][1]
        return 1, ""
    return run


class WhoToSpeedTestTests(unittest.TestCase):
    HEARD = [{"ssid": "Office", "secured": True}, {"ssid": "Guest", "secured": False},
             {"ssid": "Lab", "secured": True}, {"ssid": "OldOpen", "secured": True}]
    PROFILES = {"Office": ("Office", "wpa-psk"), "Office (survey wlan0)": ("Office", "wpa-psk"),
                "OldOpen": ("OldOpen", "")}

    def setUp(self) -> None:
        patches = [mock.patch.object(connectivity, "run", fake_nmcli(self.PROFILES)),
                   mock.patch.object(connectivity, "have", lambda _name: True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_only_heard_encrypted_and_saved(self) -> None:
        self.assertIsNone(connectivity.why_not_test("Office", self.HEARD))
        self.assertEqual(connectivity.why_not_test("Elsewhere", self.HEARD), "not heard here")
        self.assertIn("open network", connectivity.why_not_test("Guest", self.HEARD))
        self.assertIn("no saved password", connectivity.why_not_test("Lab", self.HEARD))
        self.assertIn("isn't encrypted", connectivity.why_not_test("OldOpen", self.HEARD))

    def test_the_temporary_copy_isnt_the_saved_profile(self) -> None:
        self.assertEqual(connectivity.saved_profile("Office"), "Office")

    def test_plan_skips_rather_than_fails(self) -> None:
        rows = [{"ssid": "Office", "adapter": "wlan0", "server": "srv"},
                {"ssid": "Elsewhere", "adapter": "wlan0", "server": "srv"},
                {"ssid": "Guest", "adapter": "wlan1", "server": "srv"}]
        with mock.patch.object(connectivity, "join", return_value=None) as join, \
                mock.patch.object(throughput, "test", return_value={"down_mbps": 90.0, "up_mbps": 40.0}):
            results = throughput.run_plan(rows, "", None, heard=self.HEARD)
        join.assert_called_once_with("wlan0", "Office")
        self.assertEqual(results[0]["down_mbps"], 90.0)
        self.assertEqual(results[1]["skipped"], "not heard here")
        self.assertIn("open network", results[2]["skipped"])
        self.assertFalse(any(r.get("error") for r in results))
        self.assertIn("skipped, not heard here", throughput.describe(results[1]))


if __name__ == "__main__":
    unittest.main()
