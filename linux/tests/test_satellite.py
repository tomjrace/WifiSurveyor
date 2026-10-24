"""The satellite agent (with its pretend tinySA) and the app's client, over real HTTP and UDP."""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from wifisurveyor.net import satellite as sat
from wifisurveyor.rf import linktest, txrules

AGENT_PATH = Path(__file__).resolve().parent.parent / "satellite" / "wifisurveyor_satellite.py"
spec = importlib.util.spec_from_file_location("wifisurveyor_satellite", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

CODE = "ABCD-EFGH-JKLM"


class Running:
    """An agent serving on a free local port, with a pretend tinySA."""

    def __init__(self) -> None:
        self.device = agent.FakeTinySa()
        self.satellite = agent.Satellite("bench-pi", CODE, lambda: self.device,
                                         lambda: {"generator": {"model": self.device.model}})
        self.server = agent.ThreadingHTTPServer(("127.0.0.1", 0), agent.make_handler(self.satellite))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def client(self, code: str = CODE) -> sat.Satellite:
        return sat.Satellite(sat.Paired("bench-pi", "127.0.0.1", self.port, code), timeout=3)

    def stop(self) -> None:
        self.satellite.tx.stop()
        self.server.shutdown()
        self.server.server_close()


class SatelliteTest(unittest.TestCase):
    def setUp(self):
        self.running = Running()
        self.client = self.running.client()

    def tearDown(self):
        self.running.stop()

    def test_rules_are_the_same_on_both_ends(self):
        for name in ("OVER_THE_AIR", "EDGE_MHZ", "MAX_OTA_DBM", "MAX_CABLED_DBM", "TX_MINUTES",
                     "LEASE_SECONDS"):
            self.assertEqual(getattr(agent, name), getattr(txrules, name), name)
        for freq, level, cabled in ((915, -30, False), (500, -30, False), (500, -30, True),
                                    (2450, -10, False), (500, 5, True), (902.2, -30, False)):
            self.assertEqual(bool(agent.check(freq, level, cabled)),
                             bool(txrules.check(freq, level, cabled)), (freq, level, cabled))

    def test_info_needs_no_code_but_commands_do(self):
        info = self.running.client("WRONG").info()
        self.assertEqual(info["name"], "bench-pi")
        with self.assertRaises(sat.SatelliteError) as caught:
            self.running.client("WRONG").status()
        self.assertIn("pairing code", str(caught.exception))
        self.assertEqual(self.client.status()["sending"], None)

    def test_code_is_forgiving_about_case_and_dashes(self):
        self.assertIn("sending", self.running.client("abcd efgh jklm").status())

    def test_tone_over_the_air_only_in_ism(self):
        answer = self.client.tone(915.0, -30, cabled=False)
        self.assertEqual(answer["sending"]["freq_mhz"], 915.0)
        self.assertEqual(self.running.device.sent[-1], ("generate", 915.0, -30.0, "off"))
        with self.assertRaises(sat.SatelliteError):
            self.client.tone(520.0, -30, cabled=False)              # refused by the app
        # and by the satellite itself, whatever the app says
        with self.assertRaises(sat.SatelliteError) as caught:
            with mock.patch.object(txrules, "check", return_value=""):
                self.client.tone(520.0, -30, cabled=False)
        self.assertIn("over the air", str(caught.exception))

    def test_cabled_allows_any_band_but_caps_the_level(self):
        self.client.tone(520.0, -40, cabled=True)
        self.assertEqual(self.client.status()["sending"]["freq_mhz"], 520.0)
        with self.assertRaises(sat.SatelliteError):
            self.client.tone(520.0, 5, cabled=True)
        self.client.stop()
        self.assertIsNone(self.client.status()["sending"])
        self.assertEqual(self.running.device.sent[-1], ("stop",))

    def test_a_replayed_command_is_refused(self):
        body = json.dumps({"freq_mhz": 915.0, "level_dbm": -30}).encode()
        stamp, nonce = f"{time.time():.3f}", "fixednonce"
        auth = f"{stamp}:{nonce}:" + sat.signature(CODE, stamp, nonce, "POST", "/tone", body)

        def send() -> int:
            request = urllib.request.Request(f"http://127.0.0.1:{self.running.port}/tone",
                                             data=body, method="POST",
                                             headers={"X-Satellite-Auth": auth})
            try:
                with urllib.request.urlopen(request, timeout=3) as answer:
                    return answer.status
            except urllib.error.HTTPError as error:
                error.close()
                return error.code

        self.assertEqual(send(), 200)
        self.assertEqual(send(), 401)

    def test_it_stops_when_the_app_goes_quiet(self):
        with mock.patch.object(agent, "LEASE_SECONDS", 1):
            self.client.tone(915.0, -30, cabled=False)
            time.sleep(2.6)
            status = self.running.satellite.tx.status()
        self.assertIsNone(status["sending"])
        self.assertIn("hadn't checked in", status["note"])

    def test_plan_steps_through_frequencies(self):
        self.client.plan([(903.0, -30, 0.3), (915.0, -30, 0.3), (926.0, -30, 0.3)], cabled=False)
        time.sleep(1.3)
        sent = [s for s in self.running.device.sent if s[0] in ("generate", "retune")]
        self.assertEqual([s[1] for s in sent], [903.0, 915.0, 926.0])
        self.assertIn("finished", self.running.satellite.tx.status()["note"])

    def test_remote_scan(self):
        freqs, levels = self.client.scan(900, 930, 100)
        self.assertEqual(len(freqs), 100)
        self.assertAlmostEqual(freqs[-1], 930)
        self.assertTrue(all(v < -90 for v in levels))

    def test_discovery_answers_the_question(self):
        stop = threading.Event()
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        thread = threading.Thread(target=agent.answer_discovery,
                                  args=(self.running.satellite, port, stop), daemon=True)
        thread.start()
        time.sleep(0.2)
        asker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        asker.settimeout(2)
        asker.sendto(sat.DISCOVER, ("127.0.0.1", port))
        data, _sender = asker.recvfrom(4096)
        asker.close()
        stop.set()
        info = json.loads(data)
        self.assertEqual(info["service"], "wifisurveyor-satellite")
        self.assertEqual(info["port"], port)
        self.assertNotIn("code", json.dumps(info).lower().replace("pairing", ""))


class LinkTestTest(unittest.TestCase):
    def test_link_test_steps_and_always_stops_the_generator(self):
        running = Running()
        try:
            generator = linktest.RemoteGenerator(running.client())

            class Receiver:
                name = model = "receiver"
                min_mhz, max_mhz = 1.0, 6000.0

                def dwell(self, centre, seconds, width_mhz):
                    from wifisurveyor.rf.analyzer import Config

                    step = width_mhz / 100
                    config = Config(centre - width_mhz / 2, step, 101, False, 1, 6000, 100, 10)
                    level = -60.0 if centre < 920 else -101.0           # the top one isn't heard
                    trace = [-100.0] * 101
                    trace[50] = level
                    return config, [list(trace) for _ in range(4)]

            freqs = linktest.frequencies(903, 927, 4)
            result = linktest.run(generator, Receiver(), freqs, -30, cabled=False)
            self.assertEqual([round(s.freq_mhz) for s in result.steps], [903, 911, 919, 927])
            self.assertEqual(result.steps[0].loss_db, 30)
            self.assertIsNone(result.steps[-1].heard_dbm)
            self.assertIn("Heard 3 of 4", result.summary())
            self.assertIsNone(running.client().status()["sending"])     # stopped afterwards
        finally:
            running.stop()

    def test_refuses_before_sending_anything(self):
        class Generator:
            name = "g"
            started = False

            def start(self, *_a):
                self.started = True

            def stop(self):
                pass

        generator = Generator()
        receiver = mock.Mock(min_mhz=1.0, max_mhz=6000.0, model="r")
        result = linktest.run(generator, receiver, [915.0, 520.0], -30, cabled=False)
        self.assertFalse(generator.started)
        self.assertIn("over the air", result.error)


class PairedSettingsTest(unittest.TestCase):
    def test_remember_and_forget(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"XDG_DATA_HOME": folder}):
            sat.remember(sat.Paired("a", "10.0.0.5", 8737, CODE))
            sat.remember(sat.Paired("a-renamed", "10.0.0.5", 8737, CODE))
            sat.remember(sat.Paired("b", "10.0.0.6", 8737, CODE))
            self.assertEqual([p.name for p in sat.saved()], ["a-renamed", "b"])
            sat.forget("10.0.0.5")
            self.assertEqual([p.host for p in sat.saved()], ["10.0.0.6"])


if __name__ == "__main__":
    unittest.main()
