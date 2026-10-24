"""The Advanced table: public iperf3 servers, parallel speed tests, and split scanning."""

import json
import threading
import time
import unittest
from unittest import mock

from wifisurveyor.core import analysis
from wifisurveyor.core.project import Point, Project
from wifisurveyor.net import connectivity, iperf_servers, throughput, wifi

LIST = json.dumps([
    {"IP/HOST": "a.example", "PORT": "5201-5203", "OPTIONS": "-R", "GB/S": "10",
     "CONTINENT": "Europe", "COUNTRY": "NL", "SITE": "Amsterdam", "PROVIDER": "One"},
    {"IP/HOST": "b.example", "PORT": "5201", "CONTINENT": "Europe", "COUNTRY": "DE",
     "SITE": "Frankfurt", "PROVIDER": "Two"},
    {"IP/HOST": "c.example", "PORT": "9200-9201", "CONTINENT": "North America", "COUNTRY": "US",
     "SITE": "Dallas", "PROVIDER": "Three"},
    {"IP/HOST": "", "PORT": "5201"},                            # no host: skipped
    {"IP/HOST": "d.example", "PORT": "nonsense", "CONTINENT": "Asia"},
])


class ServerListTest(unittest.TestCase):
    def test_parse_matches_the_windows_reading(self):
        servers = iperf_servers.parse(LIST)
        self.assertEqual([s.host for s in servers], ["a.example", "b.example", "c.example", "d.example"])
        self.assertEqual((servers[0].first_port, servers[0].last_port), (5201, 5203))
        self.assertEqual((servers[3].first_port, servers[3].last_port), (5201, 5201))
        self.assertIn("NL Amsterdam · One (10 Gb/s) — a.example:5201-5203", servers[0].display)
        self.assertEqual(iperf_servers.continents(servers), ["Asia", "Europe", "North America"])
        self.assertEqual(iperf_servers.parse("not json"), [])

    def test_random_slots_stay_in_region_and_never_share_a_port(self):
        slots = iperf_servers.Slots(iperf_servers.parse(LIST), "Europe")
        taken = [slots.take(iperf_servers.RANDOM) for _ in range(4)]
        self.assertEqual(len(set(taken)), 4)
        self.assertTrue(all(k.startswith(("a.example", "b.example")) for k in taken))
        self.assertIsNone(slots.take(iperf_servers.RANDOM))          # all four ports in use
        slots.give_back(taken[0])
        self.assertEqual(slots.take(iperf_servers.RANDOM), taken[0])

    def test_random_avoids_a_busy_port(self):
        slots = iperf_servers.Slots(iperf_servers.parse(LIST), "North America")
        first = slots.take(iperf_servers.RANDOM)
        slots.give_back(first, busy=True)
        self.assertNotEqual(slots.take(iperf_servers.RANDOM), first)

    def test_a_chosen_server_spreads_over_its_ports_then_queues(self):
        slots = iperf_servers.Slots(iperf_servers.parse(LIST))
        self.assertEqual([slots.take("a.example") for _ in range(3)],
                         ["a.example:5201", "a.example:5202", "a.example:5203"])
        self.assertIsNone(slots.take("a.example"))
        self.assertEqual(slots.take("10.0.0.5:5201"), "10.0.0.5:5201")
        self.assertIsNone(slots.take("10.0.0.5:5201"))              # one test at a time


class SplitScanTest(unittest.TestCase):
    def test_each_channel_once_and_always_the_same_adapter(self):
        supported = {"wlan0": [2412, 2437, 2462, 5180, 5200, 5220, 5240],
                     "wlan1": [2412, 2437, 2462, 5180, 5200, 5220, 5240, 5745, 5765],
                     "wlan2": [2412, 2437, 2462]}
        devices = ["wlan0", "wlan1", "wlan2"]
        shares = wifi.split_channels(devices, supported)
        flat = [f for freqs in shares.values() for f in freqs]
        self.assertEqual(sorted(flat), sorted({int(f) for v in supported.values() for f in v}))
        self.assertEqual(len(flat), len(set(flat)))                    # nothing scanned twice
        self.assertTrue(set(shares["wlan2"]) <= {2412, 2437, 2462})    # only what it can hear
        self.assertIn(5745, shares["wlan1"])
        self.assertEqual(shares, wifi.split_channels(devices, supported))
        self.assertLessEqual(max(map(len, shares.values())) - min(map(len, shares.values())), 2)

    def test_merged_scan_lists_each_access_point_once(self):
        def fake_scan(device, passive=False, freqs=None):
            same = wifi.Reading("shared", "AA:00:00:00:00:01", -50 if device == "wlan0" else -40,
                                5180, 36, True)
            own = wifi.Reading(device, f"AA:00:00:00:00:{device[-1]}2", -60, 2412, 1, True)
            return [same, own]

        with mock.patch.object(wifi, "scan", fake_scan), \
                mock.patch("wifisurveyor.net.monitor.hop_list", return_value=[2412, 5180]):
            readings, notes = wifi.scan_split(["wlan0", "wlan1"])
        self.assertEqual(sorted(r.bssid for r in readings),
                         ["AA:00:00:00:00:01", "AA:00:00:00:00:02", "AA:00:00:00:00:12"])
        # 5180 MHz is wlan1's channel: its on-channel reading is kept, not the first adapter's
        self.assertEqual(next(r for r in readings if r.ssid == "shared").rssi, -40)
        self.assertEqual(notes, [])

    def test_an_off_channel_reading_never_beats_an_on_channel_one(self):
        shares = {"wlan0": [2412], "wlan1": [2437]}
        # wlan1 decoded channel 1's beacon from next door, and reads it stronger by chance
        results = {"wlan0": [wifi.Reading("ap", "AA:00:00:00:00:01", -62, 2412, 1, True)],
                   "wlan1": [wifi.Reading("ap", "AA:00:00:00:00:01", -58, 2412, 1, True)]}
        merged = wifi.merge_split(["wlan0", "wlan1"], shares, results)
        self.assertEqual([r.rssi for r in merged], [-62])
        # Neither on its own channel: the strongest
        results["wlan0"][0].freq_mhz = 2462
        self.assertEqual([r.rssi for r in wifi.merge_split(["wlan0", "wlan1"], shares, results)], [-58])

    def test_every_adapter_shares_the_scan_but_the_monitor_one(self):
        ifaces = [wifi.Interface("wlan0", "iwlwifi", "managed", False),
                  wifi.Interface("wlan1", "mt7921u", "managed", True),
                  wifi.Interface("wlan2", "mt76x2u", "managed", True),
                  wifi.Interface("mon0", "mt76x2u", "monitor", True)]
        self.assertEqual(wifi.scan_adapters("wlan0", "wlan2", True, [], ifaces), ["wlan0", "wlan1"])
        self.assertEqual(wifi.scan_adapters("wlan0", "wlan2", False, [], ifaces), ["wlan0"])
        # The speed-test plan can still add one, as before
        self.assertEqual(wifi.scan_adapters("wlan0", "wlan2", False, ["wlan2", "mon0"], ifaces),
                         ["wlan0", "wlan2"])
        # One adapter doing everything
        self.assertEqual(wifi.scan_adapters("wlan0", "wlan0", True, [], ifaces[:1]), ["wlan0"])


class RunPlanTest(unittest.TestCase):
    def test_adapters_run_at_once_and_share_an_adapter_in_turn(self):
        active, peak, lock = set(), [0], threading.Lock()

        def fake_test(device, server, seconds=4, slots=None):
            with lock:
                active.add(device)
                peak[0] = max(peak[0], len(active))
            time.sleep(0.2)
            with lock:
                active.discard(device)
            return {"server": server, "down_mbps": 100.0, "up_mbps": 50.0}

        plan = [{"ssid": "office5g", "adapter": "wlan0", "server": "10.0.0.5"},
                {"ssid": "guest", "adapter": "wlan1", "server": iperf_servers.RANDOM},
                {"ssid": "voip", "adapter": "wlan2", "server": ""},
                {"ssid": "cameras", "adapter": "wlan2", "server": ""}]
        slots = iperf_servers.Slots(iperf_servers.parse(LIST))
        with mock.patch.object(connectivity, "join", return_value=None), \
                mock.patch.object(throughput, "test", fake_test):
            began = time.monotonic()
            results = throughput.run_plan(plan, "192.168.1.10", slots)
            took = time.monotonic() - began
        self.assertEqual([r["ssid"] for r in results], ["office5g", "guest", "voip", "cameras"])
        self.assertEqual(peak[0], 3)                    # three adapters at once
        self.assertLess(took, 0.6)                      # wlan2's two in turn: ~0.4 s, not 0.8
        self.assertEqual(results[2]["server"], "192.168.1.10")   # blank uses the main server

    def test_failures_are_kept_per_network(self):
        def join(adapter, ssid):
            return f"couldn't join {ssid}" if ssid == "locked" else None

        plan = [{"ssid": "locked", "adapter": "wlan0", "server": "x"},
                {"ssid": "noserver", "adapter": "wlan1", "server": ""}]
        with mock.patch.object(connectivity, "join", join):
            results = throughput.run_plan(plan, "", iperf_servers.Slots([]))
        self.assertEqual(results[0]["error"], "couldn't join locked")
        self.assertIn("no iperf3 server", results[1]["error"])


class PerNetworkResultsTest(unittest.TestCase):
    def test_heatmap_shows_the_chosen_network_and_saving_keeps_them(self):
        point = Point(x=1, y=2, networks=[])
        point.throughputs = [{"ssid": "office5g", "adapter": "wlan0", "down_mbps": 220.0},
                             {"ssid": "guest", "adapter": "wlan1", "down_mbps": 30.0}]
        context = analysis.Context(Project(points=[point]))
        self.assertEqual(analysis.metric(point, "throughput", context, ssid="guest"), 30.0)
        self.assertEqual(analysis.metric(point, "throughput", context), 220.0)
        self.assertIsNone(analysis.metric(point, "throughput", context, ssid="other"))
        again = Project.from_dict(json.loads(json.dumps(Project(points=[point]).to_dict())))
        self.assertEqual(again.points[0].throughputs, point.throughputs)


if __name__ == "__main__":
    unittest.main()
