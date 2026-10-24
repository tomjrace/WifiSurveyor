"""The watch's state machine, beat by beat, through outages played out on a fake clock."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wifisurveyor.core import outage
from wifisurveyor.net import connectivity, linkwatch, ssidmonitor, wifi
from wifisurveyor.net.ssidmonitor import SsidMonitor, Target

AP = "00:00:00:02:5E:41"
AP_24 = "00:00:00:02:5e:40"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def strftime(self, *args):
        import time
        return time.strftime(*args)

    def localtime(self, *args):
        import time
        return time.localtime(*args)


class World:
    """The access point and the adapter, as the mocks report them."""

    def __init__(self) -> None:
        self.associated = True
        self.ap_up = True
        self.sibling_up = True
        self.internet = True
        self.booted_at = T0 - 30 * 86400        # the radios' clocks: up a month
        self.beacons = 0
        self.beacons_per_beat = 20              # all of them, at a beat every two seconds
        self.sent = 0
        self.retried = 0
        self.gateway_misses = 0                 # pings to the gateway still to lose

    def station(self, _device):
        if not self.associated:
            return {}
        self.beacons += self.beacons_per_beat
        self.sent += 90
        self.retried += 10
        return {"bssid": AP.lower(), "beacon_rx": self.beacons, "beacon_loss": 0,
                "signal": -60.0, "beacon_interval": 100, "tx_packets": self.sent,
                "tx_retries": self.retried, "tx_bitrate": 144.4, "rx_bitrate": 130.0}

    def scan(self, _device, passive=False, freqs=None):
        readings = []
        uptime_us = int((clock.now - self.booted_at) * 1e6)
        if self.ap_up:
            readings.append(wifi.Reading("office5g", AP, -60, 5220.0, 44, True, tsf_us=uptime_us))
        if self.sibling_up:
            readings.append(wifi.Reading("sales", AP_24.upper(), -55, 2437.0, 6, True,
                                         tsf_us=uptime_us + 1000))
        return readings

    def join(self, _device, _ssid, _bssid=None, holder=None):
        if not self.ap_up:
            return "no network with that BSSID found"
        self.associated = True
        return None

    def beat(self, *_args, **_kwargs):
        lost = self.gateway_misses > 0
        self.gateway_misses -= lost
        return {"gw_ms": None if lost else 2.0, "net_ms": 3.0 if self.internet else None}


clock = Clock()


class StateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        clock.now = T0
        self.world = world = World()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patches = [
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": folder.name}),
            mock.patch.object(ssidmonitor, "time", clock),
            mock.patch.object(linkwatch, "station", world.station),
            mock.patch.object(linkwatch, "address", lambda d: "10.0.0.5" if world.associated else None),
            mock.patch.object(linkwatch, "dns_servers", lambda d: []),
            mock.patch.object(linkwatch, "beat", world.beat),
            mock.patch.object(connectivity, "gateway", lambda d: "10.0.0.1"),
            mock.patch.object(connectivity, "join", world.join),
            mock.patch.object(wifi, "scan", world.scan),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        target = Target("office5g", AP, 5220.0, 5210.0, 80, siblings={AP_24: 2437.0})
        self.watch = SsidMonitor(target, "wlan1", interval=2.0)
        self.watch.started = T0
        self.watch.log = outage.SessionLog("office5g", AP)
        self.addCleanup(self.watch.log.close)

    def run_until(self, until: float) -> None:
        while clock.now < until:
            self.watch._beat()
            clock.now += 2.0

    def test_the_whole_unit_rebooting(self):
        world = self.world
        self.run_until(T0 + 120)
        self.assertEqual(self.watch.state, "up")
        # The access point says goodbye and every radio goes
        self.watch._event({"time": clock.now, "device": "wlan1", "kind": "deauth",
                           "src": AP.lower(), "dst": "02:00:00:04:0d:ae", "reason": 3})
        self.watch._event({"time": clock.now, "device": "wlan1", "kind": "disconnected",
                           "by_ap": True, "reason": 3})
        world.associated = world.ap_up = world.sibling_up = False
        down_at = clock.now
        self.run_until(T0 + 210)
        self.assertEqual(self.watch.state, "down")
        self.assertIsNotNone(self.watch.current)
        # Ninety seconds later both radios are back, their clocks started again
        world.ap_up = world.sibling_up = True
        world.booted_at = clock.now - 20
        self.run_until(T0 + 300)
        self.assertEqual(len(self.watch.incidents), 1)
        incident = self.watch.incidents[0]
        self.assertAlmostEqual(incident.start, down_at, delta=1)
        self.assertIsNotNone(incident.end)
        self.assertEqual((incident.by_ap, incident.reason), (True, 3))
        self.assertTrue(incident.scans)
        self.assertIsNone(self.watch.current, "settled and written down")
        self.assertEqual(incident.diagnosis["cause"], "unit_down")
        _start, _beats, logged = outage.read_session(self.watch.log.path)
        self.assertEqual(logged[0].diagnosis["cause"], "unit_down")

    def test_one_radio_restarting(self):
        world = self.world
        self.run_until(T0 + 60)
        self.watch._event({"time": clock.now, "device": "wlan1", "kind": "disconnected",
                           "by_ap": True, "reason": 3})
        world.associated = world.ap_up = False           # the 2.4 GHz radio stays up
        self.run_until(T0 + 80)
        world.ap_up = True
        self.run_until(T0 + 160)
        found = self.watch.incidents[0].diagnosis
        self.assertEqual(found["cause"], "radio_restart")
        self.assertIn("other radios stayed up", " ".join(found["why"]))

    def test_internet_outage_behind_a_good_link(self):
        world = self.world
        self.run_until(T0 + 30)
        world.internet = False
        self.run_until(T0 + 60)
        self.assertEqual(self.watch.current.kind, "no_internet")
        world.internet = True
        self.run_until(T0 + 120)
        incident = self.watch.incidents[0]
        self.assertAlmostEqual(incident.end - incident.start, 30, delta=3)
        self.assertEqual(incident.diagnosis["cause"], "upstream")

    def test_one_failed_check_isnt_an_outage(self):
        world = self.world
        self.run_until(T0 + 30)
        world.internet = False
        self.run_until(T0 + 32)
        world.internet = True
        self.run_until(T0 + 90)
        self.assertEqual(self.watch.incidents, [])

    def test_missed_beacons_alone_arent_a_fault(self):
        self.world.beacons_per_beat = 6         # 30% arriving, but everything answers
        self.run_until(T0 + 30)
        beats = list(self.watch.beats)
        self.assertLess(beats[-1]["beacons"], 0.6)
        self.assertEqual({b["state"] for b in beats}, {"up"})
        self.assertEqual(ssidmonitor.trouble_words(beats), [])

    def test_one_lost_gateway_ping_doesnt_relabel_an_internet_outage(self):
        world = self.world
        self.run_until(T0 + 30)
        world.internet = False
        self.run_until(T0 + 40)
        world.gateway_misses = 1
        self.run_until(T0 + 50)
        self.assertNotIn("no_gateway", {b["stage"] for b in self.watch.beats})
        world.gateway_misses = 3
        self.run_until(T0 + 60)
        self.assertIn("no_gateway", {b["stage"] for b in self.watch.beats})

    def test_the_adapter_dropping_out_isnt_the_network(self):
        indexes = iter([7] * 15 + [8] * 100)            # it comes back as a new interface
        with mock.patch.object(ssidmonitor, "_ifindex", lambda _d: next(indexes)):
            self.watch._ifindex = 7
            self.run_until(T0 + 20)
            self.world.associated = False
            self.run_until(T0 + 40)
            self.world.associated = True
            self.run_until(T0 + 120)
        incident = self.watch.incidents[0]
        self.assertTrue(incident.adapter_lost)
        self.assertEqual(incident.diagnosis["cause"], "adapter_lost")
        self.assertEqual(outage.kind_name(incident), "Adapter dropped out")
        self.assertEqual(len(self.watch.adapter_resets), 1)
        self.assertEqual(outage.availability(T0, T0 + 120, [incident]), 1.0)

    def test_link_rates_and_resends(self):
        self.run_until(T0 + 10)
        last = self.watch.beats[-1]
        self.assertEqual((last["tx_rate"], last["rx_rate"]), (144.4, 130.0))
        self.assertAlmostEqual(last["retries"], 0.1)


class BothBandsTests(unittest.TestCase):
    """Watching a network's two radios at once: what goes with what."""

    adapters = [
        {"name": "wlp1s0", "freqs": [2412.0, 2417.0, 5180.0, 5765.0], "monitor": True, "rank": (5, 0, "wlp1s0")},
        {"name": "old", "freqs": [2412.0, 2417.0], "monitor": True, "rank": (5, 0, "old")},
        {"name": "alfa", "freqs": [2412.0, 2417.0, 5180.0, 5765.0], "monitor": True, "rank": (1, 0, "alfa")},
    ]

    def test_adapters_shared_out(self):
        # The 2.4-only adapter joins 2.4, the other joins 5, the Alfa listens on the chosen band
        self.assertEqual(ssidmonitor.share_adapters([2417.0, 5765.0], self.adapters),
                         [("old", "alfa"), ("wlp1s0", None)])
        self.assertEqual(ssidmonitor.share_adapters([5765.0, 2417.0], self.adapters),
                         [("wlp1s0", "alfa"), ("old", None)])
        # Nothing left that reaches 5 GHz
        self.assertEqual(ssidmonitor.share_adapters([2417.0, 5765.0], self.adapters[1:2]),
                         [("old", None), (None, None)])

    def test_the_other_band_of_the_same_network(self):
        def reading(ssid, bssid, freq, rssi):
            return wifi.Reading(ssid, bssid, rssi, freq, 0, True)

        chosen = reading("home", "88:00:00:01:BC:F0", 2417.0, -50)
        readings = [chosen, reading("home", "86:00:00:01:BC:F3", 5765.0, -60),
                    reading("home", "00:11:22:33:44:55", 5180.0, -40),     # another access point
                    reading("guest", "86:00:00:01:BC:F4", 5765.0, -60)]
        self.assertEqual(ssidmonitor.partner(chosen, readings).bssid, "86:00:00:01:BC:F3")
        self.assertIsNone(ssidmonitor.partner(chosen, [chosen, readings[3]]))

    def test_one_analyzer_takes_turns(self):
        done = []

        class Watch:
            def __init__(self, name, rounds):
                self.name, self.rounds = name, rounds
                self.analyzer = analyzer
                self._stop = threading.Event()

            def _dwell_once(self):
                done.append(self.name)
                self.rounds -= 1
                if self.rounds <= 0:
                    self._stop.set()
                return True

        analyzer = SimpleNamespace(lock=threading.RLock())
        first, second = Watch("2.4", 3), Watch("5", 3)
        with analyzer.lock:                     # both join before the turns begin
            ssidmonitor._share_analyzer(first)
            ssidmonitor._share_analyzer(second)
        for _ in range(100):
            if not ssidmonitor._shares:
                break
            threading.Event().wait(0.02)
        self.assertEqual(done, ["2.4", "5"] * 3)
        self.assertEqual(ssidmonitor._shares, {})

    def test_arp_set_while_watching_and_put_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("arp_ignore", "arp_announce"):
                (root / name).write_text("0\n")
            ssidmonitor._own_arp(True, root)
            ssidmonitor._own_arp(True, root)                # the second band
            self.assertEqual((root / "arp_ignore").read_text(), "1")
            ssidmonitor._own_arp(False, root)
            self.assertEqual((root / "arp_announce").read_text(), "2")   # one still watching
            ssidmonitor._own_arp(False, root)
            self.assertEqual((root / "arp_ignore").read_text(), "0")

    def test_bands_compared(self):
        def at(start):
            return SimpleNamespace(start=start)

        notes = ssidmonitor.bands_compared([("2.4 GHz", [at(100), at(5000)]), ("5 GHz", [at(130)])])
        self.assertTrue(notes[0].startswith("1 time both bands dropped"))
        self.assertIn("1 time only 2.4 GHz dropped while 5 GHz stayed up", notes[1])
        self.assertEqual(len(notes), 2)
        self.assertEqual(ssidmonitor.bands_compared([("2.4 GHz", []), ("5 GHz", [])]), [])
        self.assertEqual(ssidmonitor.bands_compared([("2.4 GHz", [at(1)])]), [])

    def test_each_band_has_its_own_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            low = outage.SessionLog("home", AP, Path(folder), label="2.4 GHz")
            high = outage.SessionLog("home", AP, Path(folder), label="5 GHz")
            self.addCleanup(low.close)
            self.addCleanup(high.close)
            self.assertNotEqual(low.folder, high.folder)
            self.assertTrue(low.folder.name.endswith("home 2.4 GHz"))


if __name__ == "__main__":
    unittest.main()
