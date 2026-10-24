import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import recording
from wifisurveyor.net.wifi import Reading


class RecordingTests(unittest.TestCase):
    def test_record_and_summarize(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.jsonl"
            recorder = recording.Recorder(path)
            clock = iter(range(1000, 2000))
            with mock.patch.object(recording.time, "time", lambda: next(clock)):
                for n in range(40):
                    # 2.4 GHz with channel 6 on for every fourth pass, in bursts
                    busy = n % 8 < 2
                    trace = [(-60.0 if busy and 2427 <= 2400 + i * 100 / 111 <= 2447 else -100.0)
                             for i in range(112)]
                    recorder.sweep(0, [(2400.0, 100 / 111, 600, trace)])
                    ap = Reading("Corp", "00:11:22:33:44:55", -50 - n % 3, 2437, 6, True)
                    recorder.scan([ap] if n % 5 else [])     # missing from one scan in five
            recorder.close()
            with path.open("a") as handle:
                handle.write('{"t": 3000, "range": 0, "par')     # cut short by a crash
            summary = recording.summarize(path, rows=20)

        self.assertEqual(summary["sweeps"], 40)
        self.assertEqual(summary["ranges"][0]["image"].shape, (20, 300))
        six = next(c for c in summary["channels"] if c["channel"] == 6 and c["band"] == "2.4")
        self.assertAlmostEqual(six["busy"], 0.25)
        self.assertEqual(six["bursts"], 5)
        corp = summary["aps"][0]
        self.assertAlmostEqual(corp["heard"], 0.8)
        self.assertEqual((corp["min"], corp["max"]), (-52, -50))


if __name__ == "__main__":
    unittest.main()
