import unittest

from scapy.layers.dot11 import (
    Dot11, Dot11Beacon, Dot11Deauth, Dot11Elt, Dot11ProbeReq, RadioTap,
)

from wifisurveyor.net import capture

AP, AP2, TWIN = "00:11:22:33:44:55", "00:11:22:33:44:66", "00:c0:ca:00:00:01"
PHONE = "aa:bb:cc:dd:ee:01"

# RSN: version 1, group CCMP, pairwise CCMP, AKMs PSK + SAE, capabilities 0 (no 802.11w)
RSN_TRANSITION = bytes.fromhex("0100" "000fac04" "0100" "000fac04" "0200" "000fac02" "000fac08" "0000")
RSN_WPA3 = bytes.fromhex("0100" "000fac04" "0100" "000fac04" "0100" "000fac08" "c000")
WPS = bytes.fromhex("0050f204") + b"\x10\x4a\x00\x01\x10"


def beacon(bssid, ssid, rsn=None, extra=(), when=0.0, freq=2437):
    frame = (RadioTap(present="Flags+Channel+dBm_AntSignal", ChannelFrequency=freq, dBm_AntSignal=-50)
             / Dot11(type=0, subtype=8, addr1="ff:ff:ff:ff:ff:ff", addr2=bssid, addr3=bssid)
             / Dot11Beacon(cap="ESS+privacy" if rsn else "ESS", beacon_interval=100)
             / Dot11Elt(ID=0, info=ssid) / Dot11Elt(ID=3, info=b"\x06"))
    if rsn:
        frame = frame / Dot11Elt(ID=48, info=rsn)
    for element_id, info in extra:
        frame = frame / Dot11Elt(ID=element_id, info=info)
    frame.time = when
    return frame


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        frames = [
            beacon(AP, b"Office", RSN_TRANSITION, extra=[(221, WPS)], when=1.0),
            beacon(AP2, b"Office", RSN_WPA3, when=1.1, freq=5180),
            beacon(TWIN, b"Office", None, when=1.2),
            beacon("00:11:22:33:44:77", b"\x00\x00\x00", RSN_WPA3, when=1.3),
        ]
        for n in range(60):                        # a minute's worth of broadcast deauths
            deauth = (RadioTap() / Dot11(type=0, subtype=12, addr1="ff:ff:ff:ff:ff:ff", addr2=AP,
                                         addr3=AP) / Dot11Deauth(reason=7))
            deauth.time = 2.0 + n * 0.5
            frames.append(deauth)
        probe = (RadioTap() / Dot11(type=0, subtype=4, addr1="ff:ff:ff:ff:ff:ff", addr2=PHONE,
                                    addr3="ff:ff:ff:ff:ff:ff") / Dot11ProbeReq() / Dot11Elt(ID=0, info=b"HomeNet"))
        frames.append(probe)
        data = (RadioTap() / Dot11(type=2, subtype=0, FCfield="to_DS", addr1=AP, addr2=PHONE,
                                   addr3=AP))
        data.time = 30.0
        frames.append(data)
        for frame in frames:
            if frame.time > 1e6:               # built without one: Scapy stamps it with now
                frame.time = 32.0
        self.summary = capture.analyze(frames, "test")

    def test_beacon_details(self) -> None:
        stats = {s.bssid: s for s in self.summary.bss}
        office = stats[AP]
        self.assertEqual(office.security, "WPA2/WPA3-Personal")
        self.assertEqual(office.pmf, "off")
        self.assertTrue(office.wps)
        self.assertEqual(office.freq_mhz, 2437)
        self.assertEqual(stats[AP2].security, "WPA3-Personal (SAE)")
        self.assertEqual(stats[AP2].pmf, "required")
        self.assertEqual(stats[TWIN].security, "Open")
        self.assertTrue(stats["00:11:22:33:44:77"].hidden)

    def test_devices_and_probes(self) -> None:
        phone = next(d for d in self.summary.devices if d.mac == PHONE)
        self.assertEqual(phone.probes, {"HomeNet"})
        self.assertIn(AP, phone.bssids)
        self.assertNotIn(AP, {d.mac for d in self.summary.devices})

    def test_findings(self) -> None:
        titles = [f.title for f in capture.findings(self.summary)]
        for expected in ("Possible deauthentication attack", "Same SSID, different security",
                         "WPS enabled", "WPA3 transition mode", "Management frames unprotected",
                         "Open network", "Hidden SSID",
                         "Devices naming networks they know"):
            self.assertIn(expected, titles)
        self.assertEqual(capture.findings(self.summary)[0].severity, "High")


if __name__ == "__main__":
    unittest.main()
