"""What a watch keeps on disk: compacted logs, the spectrum a row per 30 s, the timeline, captures."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from wifisurveyor.core import outage, watchreport
from wifisurveyor.net.ssidmonitor import SsidMonitor, Target


def write_watch(folder: Path, freq: float = 5180, start: float = 1_800_000_000.0,
                down: tuple[int, int] | None = (3000, 3120), seconds: int = 7200) -> Path:
    """Two hours of a watch, beats every 2 s and a spectrum row every 30 s, one dropout."""
    folder.mkdir(parents=True)
    lines = [{"t": start, "type": "start", "ssid": "Office", "bssid": "02:00:00:00:00:01"},
             {"t": start, "type": "target", "freq": freq, "monitor": "wlan1"}]
    for second in range(0, seconds, 2):
        off = down is not None and down[0] <= second <= down[1]
        lines.append({"t": start + second, "type": "beat", "state": "down" if off else "up",
                      "signal": None if off else -60.0 - second % 3, "gw": None if off else 2.0,
                      "assoc": not off, "losses": 0})
    for second in range(0, seconds, 30):
        off = down is not None and down[0] <= second <= down[1]
        level = -95 if off else -60
        lines.append({"t": start + second + 30, "type": "spectrum", "at": start + second,
                      "start": freq - 10, "step": 0.5, "sweeps": 6,
                      "avg": outage.pack_levels([level] * 40), "max": outage.pack_levels([level + 6] * 40)})
        lines.append({"t": start + second, "type": "channel", "power": level - 5.0})
    if down:
        lines.append({"t": start + down[0], "type": "open", "id": 1, "kind": "disconnect",
                      "start": start + down[0]})
        lines.append({"t": start + down[1], "type": "incident", "incident": {
            "id": 1, "start": start + down[0], "end": start + down[1], "kind": "disconnect",
            "ssid": "Office", "bssid": "02:00:00:00:00:01", "freq_mhz": freq,
            "diagnosis": {"title": "The access point went down", "why": [], "do": []}}})
    lines.append({"t": start + seconds, "type": "stop"})
    lines.sort(key=lambda r: r["t"])
    path = folder / outage.LOG_NAME
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in lines) + "\n")
    return path


class CompactTests(unittest.TestCase):
    def test_a_finished_log_shrinks_and_reports_the_same(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log = write_watch(Path(folder) / "a")
            before = watchreport.summary([log])
            plain = log.stat().st_size
            small = outage.compact(log)
            self.assertFalse(log.exists())
            self.assertEqual(outage.session_file(Path(folder) / "a"), small)
            self.assertLess(small.stat().st_size, plain / 8)
            after = watchreport.summary([small])
            records = list(outage.read_records(small))
        old, new = before["bands"][0], after["bands"][0]
        for key in ("availability", "signal", "gateway_ms", "start", "end"):
            self.assertAlmostEqual(old[key], new[key], places=3, msg=key)
        self.assertAlmostEqual(old["healthy_share"], new["healthy_share"], places=3)
        self.assertEqual(old["incidents"], new["incidents"])
        self.assertEqual(before["bands"][0]["timeline"]["state"], after["bands"][0]["timeline"]["state"])
        # Every beat within ten minutes of the dropout is still there, one by one
        start = 1_800_000_000.0
        near = [r for r in records if r["type"] == "beat"
                and start + 3000 - 600 <= r["t"] <= start + 3120 + 600]
        self.assertEqual(len(near), (3120 + 600 - (3000 - 600)) // 2 + 1)
        # Everything else is kept whole: the spectrum rows, channel readings, the incident
        for kind in ("spectrum", "channel", "incident", "open", "stop"):
            self.assertTrue(any(r["type"] == kind for r in records), kind)
        self.assertEqual(sum(1 for r in records if r["type"] == "spectrum"), 240)

    def test_only_watches_that_are_over_are_compacted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            done = write_watch(Path(folder) / "done")
            running = write_watch(Path(folder) / "running")
            text = running.read_text().replace('{"t":1800007200.0,"type":"stop"}\n', "")
            running.write_text(text)
            self.assertEqual(outage.compact_finished(Path(folder)), 1)
            self.assertFalse(done.exists())
            self.assertTrue(running.exists())

    def test_levels_pack_a_byte_each(self) -> None:
        start, step, avg, peak = outage.thin_bins(5170.0, 0.1, [-60.0] * 400, [-50.0] * 400)
        self.assertLessEqual(len(avg), outage.SPECTRUM_BINS)
        self.assertAlmostEqual(step * len(avg), 0.1 * 400, delta=0.5)
        packed = outage.pack_levels(avg)
        self.assertEqual(outage.unpack_levels(packed), [-60.0] * len(avg))
        self.assertLess(len(packed), 4 * len(avg) / 3 + 4)


class SpectrumLogTests(unittest.TestCase):
    def test_the_log_gets_a_spectrum_row_per_30_seconds(self) -> None:
        watch = SsidMonitor(Target("Office", "02:00:00:00:00:01", 5180, 5180, 20), "wlan9")
        rows = []
        base = 1_800_000_000.0
        for second in range(0, 95, 5):
            trace = {"t": base + second, "start": 5170.0, "step": 0.1,
                     "avg": [-60.0 - (second >= 30)] * 200, "max": [-50.0] * 200}
            row = watch._fold_logged(trace)
            if row:
                rows.append(row)
        rows.append(watch._fold_logged(None))
        self.assertEqual([r["at"] for r in rows], [base, base + 30, base + 60, base + 90])
        self.assertEqual(outage.unpack_levels(rows[0]["avg"])[0], -60.0)
        self.assertEqual(outage.unpack_levels(rows[1]["avg"])[0], -61.0)
        self.assertEqual(rows[0]["sweeps"], 6)


class TimelineTests(unittest.TestCase):
    def test_the_timeline_puts_everything_on_one_axis(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            watch = watchreport.summary([write_watch(Path(folder) / "a")])
        axis, band = watch["timeline"], watch["bands"][0]["timeline"]
        self.assertEqual(axis["spp"], 30.0)
        self.assertEqual(axis["n"], 240)
        self.assertEqual(len(band["state"]), 240)
        self.assertEqual(band["state"][100], "d")              # 3000 s in
        self.assertEqual(band["state"][50], "u")
        spectrum = watchreport.unpack_dbm(band["spectrum"]["avg"])
        bins = band["spectrum"]["bins"]
        self.assertEqual(spectrum[100 * bins], -95.0)           # the dropout is quiet
        self.assertEqual(spectrum[50 * bins], -60.0)
        self.assertLess(len(json.dumps(watch)), 30_000)
        picture = watchreport.timeline_png(watch)
        self.assertTrue(picture.startswith(b"\x89PNG"))
        self.assertIn("one time axis, 30 s a column", watchreport.build_html(watch))

    def test_a_long_watch_gets_wider_columns(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            watch = watchreport.summary([write_watch(Path(folder) / "a", down=None,
                                                     seconds=48 * 3600)])
        self.assertLessEqual(watch["timeline"]["n"], watchreport.MAX_COLUMNS)
        self.assertGreater(watch["timeline"]["spp"], 30.0)

    def test_earlier_watches_are_listed_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            write_watch(base / "2026-01-01 10.00.00 Office 5 GHz", start=1_800_000_000.0)
            write_watch(base / "2026-01-01 10.00.04 Office 2.4 GHz", freq=2412,
                        start=1_800_000_004.0)
            write_watch(base / "2026-01-02 09.00.00 Office 5 GHz", start=1_800_090_000.0)
            found = watchreport.earlier(base)
            skipped = watchreport.earlier(base, skip={str(base / "2026-01-02 09.00.00 Office 5 GHz")})
        self.assertEqual([len(w["bands"]) for w in found], [1, 2])
        self.assertEqual(len(skipped), 1)


class CaptureTests(unittest.TestCase):
    def test_a_long_outage_keeps_its_start_and_its_recovery(self) -> None:
        from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt, RadioTap
        from scapy.utils import rdpcap

        from wifisurveyor.net import beaconwatch

        watch = beaconwatch.BeaconWatch("mon9", "02:00:00:00:00:01", 5180)
        watch._layers = (Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt)
        now = time.time()

        def frame(at: float):
            packet = RadioTap() / Dot11(addr1="ff:ff:ff:ff:ff:ff", addr2="02:00:00:00:00:09",
                                        addr3="02:00:00:00:00:09")
            packet.time = at
            return packet

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "incident.pcap"
            watch.begin_capture(path)
            # An hour of frames, one a second: the first CAPTURE_DURING go to disk
            for second in range(3600):
                watch._frame(frame(now + second))
            watch.end_capture()
            kept = rdpcap(str(path))
        times = sorted(float(p.time) for p in kept)
        during = [t for t in times if t <= now + beaconwatch.CAPTURE_DURING + 0.5]
        recovery = [t for t in times if t >= now + 3600 - beaconwatch.LOOKBACK - 2]
        self.assertAlmostEqual(len(during), beaconwatch.CAPTURE_DURING, delta=2)
        self.assertAlmostEqual(len(recovery), beaconwatch.LOOKBACK, delta=2)
        self.assertEqual(len(times), len(during) + len(recovery))


if __name__ == "__main__":
    unittest.main()
