"""Telling why a network dropped its clients from the evidence it left."""

import os
import tempfile
import unittest
from unittest import mock

from wifisurveyor.core import outage
from wifisurveyor.core.outage import BeaconTrack, ChannelLog, Deauth, Incident
from wifisurveyor.net import connectivity, linkwatch, wifi

AP = "00:00:00:02:5e:41"
AP_24 = "00:00:00:02:5e:40"          # the same unit's 2.4 GHz radio
ELSEWHERE = "a4:2b:b0:11:22:33"
T0 = 1_800_000_000.0


def beacons(track_or_log, start, seconds, rssi=-60.0, tsf_start=10_000_000_000, seq_start=100,
            bssid=AP):
    """Beacons every 102.4 ms, as an access point sends them."""
    count = int(seconds / 0.1024)
    for n in range(count):
        when = start + n * 0.1024
        fields = dict(tsf=tsf_start + int(n * 102_400), seq=(seq_start + n) % 4096, rssi=rssi,
                      interval_tu=100, channel=44)
        if isinstance(track_or_log, ChannelLog):
            track_or_log.beacon(bssid, when, **fields)
            track_or_log.frame(when)
        else:
            track_or_log.add(when, **fields)
    return start + count * 0.1024


def incident(**fields):
    base = dict(id=1, start=T0, end=T0 + 60, kind="disconnect", ssid="office5g", bssid=AP.upper(),
                channel=44, freq_mhz=5220.0, sibling_bssids=[AP_24])
    base.update(fields)
    return Incident(**base)


class SiblingTests(unittest.TestCase):
    def test_same_unit_found_and_strangers_left_out(self):
        found = outage.siblings(AP, [AP_24, "02:00:00:02:5e:42", ELSEWHERE, AP])
        self.assertEqual(found, [AP_24, "02:00:00:02:5e:42"])


class BeaconTrackTests(unittest.TestCase):
    def test_gap_and_clock_reset_are_seen(self):
        track = BeaconTrack(AP)
        end = beacons(track, T0, 30, tsf_start=86_400_000_000)      # up a day
        shown = track.add(end + 90, tsf=2_000_000, seq=5, rssi=-60)   # back 90 s later, from zero
        self.assertIn("gap", shown)
        self.assertIn("reset", shown)
        self.assertAlmostEqual(track.gaps[0]["seconds"], 90, delta=1)
        self.assertAlmostEqual(track.resets[0]["uptime_before_s"], 86_430, delta=2)

    def test_received_share_uses_the_access_points_clock(self):
        track = BeaconTrack(AP)
        for n in range(0, 100, 2):                                     # every other beacon heard
            track.add(T0 + n * 0.1024, tsf=n * 102_400, seq=n, rssi=-60)
        self.assertAlmostEqual(track.received_ratio(T0 - 1), 0.5, delta=0.05)


class ForgedTests(unittest.TestCase):
    def setUp(self):
        self.track = BeaconTrack(AP)
        beacons(self.track, T0, 40, rssi=-62)

    def deauth(self, rssi, seq, protected=False):
        return Deauth(time=T0 + 40, src=AP, dst="ff:ff:ff:ff:ff:ff", bssid=AP, reason=7,
                      seq=seq, rssi=rssi, protected=protected)

    def test_wrong_place_wrong_sequence_unprotected_is_forged(self):
        self.track.pmf = "required"
        result = outage.forged([self.deauth(-35, 3000)], self.track)
        self.assertGreaterEqual(result["score"], 0.9)
        self.assertTrue(any("stronger" in w for w in result["why"]))

    def test_matching_signal_and_sequence_is_the_access_point(self):
        near = self.track.seq_near(T0 + 40)
        result = outage.forged([self.deauth(-61, near + 1)], self.track)
        self.assertLess(result["score"], 0.2)


class DiagnoseTests(unittest.TestCase):
    def verdict(self, item, earlier=None):
        return outage.diagnose(item, earlier)

    def test_whole_unit_down(self):
        item = incident(
            end=T0 + 120,
            monitor={"beacons_heard": True, "gap": {"start": T0, "end": T0 + 110, "seconds": 110},
                     "others_heard_in_gap": 400, "tsf_reset": {"uptime_before_s": 86_400},
                     "forged": {"score": 0}},
            scans=[{"time": T0 + 10, "heard": {}}, {"time": T0 + 50, "heard": {}},
                   {"time": T0 + 115, "heard": {AP.lower(): {"channel": 44, "tsf_us": 5_000_000},
                                                AP_24: {"freq": 2437, "tsf_us": 5_100_000}}}])
        found = self.verdict(item)
        self.assertEqual(found["cause"], "unit_down")
        self.assertEqual(found["confidence"], "high")
        self.assertTrue(found["do"])

    def test_one_radio_restarting_while_the_other_stays_up(self):
        def restart(number):
            return incident(
                id=number, start=T0 + number * 3600, end=T0 + number * 3600 + 15,
                by_ap=True, reason=3,
                monitor={"beacons_heard": True, "others_heard_in_gap": 50,
                         "gap": {"start": T0, "end": T0 + 12, "seconds": 12},
                         "tsf_reset": {"uptime_before_s": 3600}, "forged": {"score": 0}},
                scans=[{"time": T0 + number * 3600 + 5,
                        "heard": {AP_24: {"freq": 2437, "tsf_us": 9_000_000_000}}}])
        first = restart(1)
        first.diagnosis = self.verdict(first)
        self.assertEqual(first.diagnosis["cause"], "radio_restart")
        second = restart(2)
        second.diagnosis = self.verdict(second, [first])
        self.assertEqual(second.diagnosis["cause"], "radio_failing")
        self.assertIn("2 times", " ".join(second.diagnosis["why"]))

    def test_forged_deauthentication(self):
        item = incident(
            end=T0 + 8, by_ap=True, reason=7,
            monitor={"beacons_heard": True, "gap": None, "deauth_count": 40,
                     "broadcast_deauth": True,
                     "forged": {"score": 0.95, "why": ["they arrived 25 dB stronger"]}})
        found = self.verdict(item)
        self.assertEqual(found["cause"], "deauth_forged")
        self.assertIn("25 dB", " ".join(found["why"]))

    def test_genuine_deauthentication_with_a_key_reason(self):
        item = incident(end=T0 + 6, by_ap=True, reason=16,
                        monitor={"beacons_heard": True, "gap": None, "deauth_count": 12,
                                 "broadcast_deauth": False, "clients_deauthed": 12,
                                 "forged": {"score": 0.0}})
        self.assertEqual(self.verdict(item)["cause"], "key_handshake")

    def test_ap_kicked_everyone_on_purpose(self):
        item = incident(end=T0 + 6, by_ap=True, reason=1,
                        monitor={"beacons_heard": True, "gap": None, "deauth_count": 12,
                                 "broadcast_deauth": True, "forged": {"score": 0.0}})
        self.assertEqual(self.verdict(item)["cause"], "deauth_ap")

    def test_radar_moved_it(self):
        item = incident(channel=100, freq_mhz=5500, end=T0 + 70,
                        monitor={"beacons_heard": True, "others_heard_in_gap": 30,
                                 "gap": {"start": T0, "end": None, "seconds": 70},
                                 "csa": {"channel": 36, "count": 5}, "forged": {"score": 0}},
                        scans=[{"time": T0 + 65,
                                "heard": {AP.lower(): {"channel": 36, "tsf_us": 9_000_000_000}}}])
        found = self.verdict(item)
        self.assertEqual(found["cause"], "channel_change")
        self.assertIn("DFS", " ".join(found["why"]))

    def test_interference(self):
        item = incident(end=T0 + 20, locally_generated=True, reason=4,
                        monitor={"beacons_heard": True, "others_heard_in_gap": 0,
                                 "gap": {"start": T0, "end": T0 + 18, "seconds": 18},
                                 "forged": {"score": 0}},
                        spectrum={"loud_during": True, "quiet_during": False,
                                  "loud_words": "the analyzer saw the channel busy 95% of the time"})
        self.assertEqual(self.verdict(item)["cause"], "interference")

    def test_only_this_adapter(self):
        item = incident(end=T0 + 10, locally_generated=True, by_ap=False, reason=4,
                        signal_before=-82,
                        monitor={"beacons_heard": True, "gap": None, "deauth_count": 0,
                                 "forged": {"score": 0}})
        self.assertEqual(self.verdict(item)["cause"], "client_side")

    def test_upstream_failure(self):
        found = self.verdict(incident(kind="no_internet"))
        self.assertEqual(found["cause"], "upstream")

    def test_without_a_monitor_it_says_what_would_help(self):
        item = incident(by_ap=True, reason=3, end=T0 + 20,
                        scans=[{"time": T0 + 5, "heard": {AP_24: {"freq": 2437}}}])
        found = self.verdict(item)
        self.assertEqual(found["cause"], "radio_restart")
        self.assertTrue(any("monitor mode" in m for m in found["missing"]))


class PatternTests(unittest.TestCase):
    def test_regular_interval(self):
        done = []
        for n in range(4):
            item = incident(id=n, start=T0 + n * 7200, end=T0 + n * 7200 + 30)
            item.diagnosis = {"cause": "key_handshake"}
            done.append(item)
        notes = outage.patterns(done)
        self.assertTrue(any("every 2 h" in n for n in notes))
        self.assertTrue(any("4 of 4" in n for n in notes))


class SessionLogTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": folder}):
            log = outage.SessionLog("office5g", AP)
            log.write({"type": "beat", "state": "up"})
            item = incident()
            item.diagnosis = outage.diagnose(item)
            log.write({"type": "incident", "incident": item.to_json()})
            log.close()
            start, beats, incidents = outage.read_session(log.path)
            self.assertEqual(start["ssid"], "office5g")
            self.assertEqual(len(beats), 1)
            self.assertEqual(incidents[0].bssid, AP.upper())
            self.assertIn("most likely cause", outage.incidents_csv(incidents))
            self.assertTrue(str(log.folder).startswith(folder))


class LinkEvidenceTests(unittest.TestCase):
    def test_kernel_events(self):
        parse = linkwatch.parse_event
        event = parse(f"1800000000.5: wlan1 (phy #2): deauth {AP} -> 02:00:00:04:0d:ae reason 3: "
                      "Deauthenticated because sending STA is leaving")
        self.assertEqual((event["kind"], event["src"], event["reason"]), ("deauth", AP, 3))
        event = parse("1800000000.5: wlan1 (phy #2): disconnected (local request) reason: 4: "
                      "Disassociated due to inactivity")
        self.assertEqual((event["by_ap"], event["reason"]), (False, 4))
        event = parse(f"1800000000.5: wlan1 (phy #2): unprotected deauth {AP} -> ff:ff:ff:ff:ff:ff")
        self.assertEqual(event["kind"], "unprotected")
        event = parse("1800000000.5: wlan1 (phy #2): channel switch started (count=5) freq=5180 width=80")
        self.assertEqual(event["freq"], 5180)
        self.assertIsNone(parse("1800000000.5: wlan1 (phy #2): scan started"))

    def test_station_counters(self):
        found = linkwatch.parse_station(f"""Station {AP} (on wlan1)
\tMFP:\t\tyes
\tbeacon loss:\t324
\t\tbeacon rx:\t22097
\tsignal:  \t-62 [-62] dBm
\tbeacon interval:100""")
        self.assertEqual(found["bssid"], AP)
        self.assertEqual((found["beacon_loss"], found["beacon_rx"]), (324, 22097))
        self.assertEqual(found["signal"], -62)
        self.assertTrue(found["mfp"])

    def test_dns_query_and_answer(self):
        query = linkwatch.dns_query("example.com", 0x1234)
        self.assertIn(b"\x07example\x03com\x00", query)
        reply = bytes.fromhex("1234818000010001") + b"\x00" * 4
        self.assertEqual(linkwatch.dns_answer(reply, 0x1234), 1)
        with self.assertRaises(linkwatch.DnsError):
            linkwatch.dns_answer(bytes.fromhex("1234818300010000") + b"\x00" * 4, 0x1234)

    def test_scan_reads_the_access_points_clock(self):
        readings = wifi._parse_iw(f"""BSS {AP}(on wlan1)
\tTSF: 2092182049405 usec (24d, 05:09:42)
\tfreq: 5220.0
\tsignal: -60.00 dBm
\tSSID: office5g""")
        self.assertEqual(readings[0].tsf_us, 2092182049405)


class HeldAdapterTests(unittest.TestCase):
    def test_a_held_adapter_isnt_moved(self):
        connectivity.hold("wlan9", "the SSID monitor")
        self.addCleanup(connectivity.release, "wlan9")
        self.assertIn("in use by the SSID monitor", connectivity.join("wlan9", "Other"))


class FrameTests(unittest.TestCase):
    """Frames built as the monitor adapter would hear them."""

    def setUp(self):
        from scapy.layers.dot11 import (
            Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt, RadioTap,
        )
        from wifisurveyor.net.beaconwatch import read_frame

        self.read = read_frame
        self.layers = (Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt)
        self.RadioTap, self.Dot11 = RadioTap, Dot11
        self.Beacon, self.Deauth, self.Elt = Dot11Beacon, Dot11Deauth, Dot11Elt

    def frame(self, body, when, rssi=-60, **dot11):
        packet = self.RadioTap(present="dBm_AntSignal", dBm_AntSignal=rssi) / self.Dot11(**dot11) / body
        packet = self.RadioTap(bytes(packet))
        packet.time = when
        return packet

    def test_beacons_csa_and_deauth(self):
        log = ChannelLog(AP)
        for n in range(20):
            body = (self.Beacon(timestamp=5_000_000 + n * 102_400, beacon_interval=100)
                    / self.Elt(ID=0, info=b"office5g") / self.Elt(ID=3, info=bytes([44])))
            if n == 19:
                body = body / self.Elt(ID=37, info=bytes([1, 36, 3]))
            self.read(self.frame(body, T0 + n * 0.1024, type=0, subtype=8, addr1="ff:ff:ff:ff:ff:ff",
                                 addr2=AP, addr3=AP, SC=(100 + n) << 4), log, self.layers)
        self.read(self.frame(self.Deauth(reason=7), T0 + 2.1, rssi=-30, type=0, subtype=12,
                             addr1="ff:ff:ff:ff:ff:ff", addr2=AP, addr3=AP, SC=3000 << 4),
                  log, self.layers)
        track = log.track
        self.assertEqual(track.count, 20)
        self.assertEqual(track.last_tsf, 5_000_000 + 19 * 102_400)
        self.assertEqual(track.csa[0]["channel"], 36)
        found = log.evidence(T0 + 2, T0 + 3, T0 + 3)
        self.assertEqual(found["deauth_count"], 1)
        self.assertTrue(found["broadcast_deauth"])
        self.assertGreater(found["forged"]["score"], 0.3)

    def test_headers_read_without_dissecting(self):
        from scapy.layers.dot11 import Dot11QoS

        from wifisurveyor.net.beaconwatch import frame_header, read_header, worth_dissecting

        other = "aa:bb:cc:dd:ee:01"
        beacon = self.frame(self.Beacon() / self.Elt(ID=0, info=b"x"), T0, type=0, subtype=8,
                            addr1="ff:ff:ff:ff:ff:ff", addr2=AP, addr3=AP)
        theirs = self.frame(self.Beacon(), T0, type=0, subtype=8, addr1="ff:ff:ff:ff:ff:ff",
                            addr2=other, addr3=other)
        deauth = self.frame(self.Deauth(reason=7), T0, type=0, subtype=12, addr1=other,
                            addr2=other, addr3=other)
        self.assertEqual(frame_header(bytes(beacon))[:2], (0, 8))
        self.assertTrue(worth_dissecting(frame_header(bytes(beacon)), AP, {AP}))
        self.assertFalse(worth_dissecting(frame_header(bytes(theirs)), AP, {AP}))
        self.assertTrue(worth_dissecting(frame_header(bytes(deauth)), AP, {AP}))
        # A client's data to the access point (to DS), and the access point's to a client
        log = ChannelLog(AP)
        up = self.frame(Dot11QoS() / b"payload", T0, type=2, subtype=8, FCfield=0x01,
                        addr1=AP, addr2="11:22:33:44:55:66", addr3=AP)
        down = self.frame(b"payload", T0 + 1, type=2, subtype=0, FCfield=0x02,
                          addr1="22:33:44:55:66:77", addr2=AP, addr3=AP)
        self.assertFalse(worth_dissecting(frame_header(bytes(up)), AP, {AP}))
        for packet in (up, down):
            read_header(frame_header(bytes(packet)), packet.time, log)
        self.assertEqual(set(log.clients), {"11:22:33:44:55:66", "22:33:44:55:66:77"})
        self.assertEqual(log.heard_between(T0 - 1, T0 + 2), 2)

    def test_load_on_the_access_point(self):
        from scapy.layers.dot11 import Dot11QoS

        from wifisurveyor.net.beaconwatch import bss_load, frame_header, read_frame, read_header

        self.assertEqual(bss_load(bytes([7, 0, 51, 0, 0])), (7, 0.2))
        self.assertIsNone(bss_load(b"\x01"))
        log = ChannelLog(AP)
        beacon = self.frame(self.Beacon() / self.Elt(ID=0, info=b"x") /
                            self.Elt(ID=11, info=bytes([12, 0, 64, 0, 0])), T0, type=0, subtype=8,
                            addr1="ff:ff:ff:ff:ff:ff", addr2=AP, addr3=AP)
        read_frame(beacon, log, self.layers)
        neighbour = self.frame(self.Beacon(), T0, type=0, subtype=8, addr1="ff:ff:ff:ff:ff:ff",
                               addr2="aa:bb:cc:dd:ee:01", addr3="aa:bb:cc:dd:ee:01")
        read_header(frame_header(bytes(neighbour)), T0, log)
        for n, retry in enumerate((0, 0, 0x08, 0)):
            data = self.frame(Dot11QoS() / b"payload", T0 + n, type=2, subtype=8,
                              FCfield=0x01 | retry, addr1=AP, addr2=f"11:22:33:44:55:6{n}", addr3=AP)
            read_header(frame_header(bytes(data)), data.time, log)
        load = log.load(T0 + 10, own="11:22:33:44:55:60")
        self.assertEqual(load["reported"]["stations"], 12)
        self.assertAlmostEqual(load["reported"]["busy"], 0.251, places=3)
        self.assertEqual(load["heard"], 3)              # four clients, one of them ourselves
        self.assertEqual(load["networks"], 1)
        self.assertEqual((load["data"], load["retries"]), (4, 1))
        self.assertIsNone(log.load(T0 + 60)["reported"])  # an old report isn't news

    def test_a_bad_checksum_after_the_clock_is_found(self):
        from wifisurveyor.net.beaconwatch import frame_header

        good = self.RadioTap(present=0b11, mac_timestamp=1, Flags=0) / self.Dot11(
            type=0, subtype=8, addr1="ff:ff:ff:ff:ff:ff", addr2=AP, addr3=AP) / self.Beacon()
        bad = self.RadioTap(present=0b11, mac_timestamp=1, Flags=0x40) / self.Dot11(
            type=0, subtype=8, addr1="ff:ff:ff:ff:ff:ff", addr2=AP, addr3=AP) / self.Beacon()
        self.assertEqual(frame_header(bytes(good))[4], AP)
        self.assertIsNone(frame_header(bytes(bad)))


if __name__ == "__main__":
    unittest.main()
