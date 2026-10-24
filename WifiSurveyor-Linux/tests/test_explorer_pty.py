"""The RF Explorer driver against a simulated analyzer on a pseudo-terminal."""

from __future__ import annotations

import os
import pty
import select
import threading
import time
import tty
import unittest

from wifisurveyor.rf import explorer


class FakeAnalyzer:
    """Answers C0 and C2-F like the real thing and streams $S sweeps of 112 points."""

    def __init__(self) -> None:
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        self.start_khz, self.end_khz = 2400000, 2500000
        self.max_span_khz = 600000
        self.set_sweeps: list[tuple[int, int]] = []
        self.running = True
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._commands, daemon=True),
                         threading.Thread(target=self._stream, daemon=True)]
        for thread in self._threads:
            thread.start()

    def _config(self) -> bytes:
        step_hz = (self.end_khz - self.start_khz) * 1000 // 111
        return (f"#C2-F:{self.start_khz:07d},{step_hz:07d},-010,-120,0112,0,000,"
                f"0015000,2700000,{self.max_span_khz:07d},00600\r\n").encode()

    def _write(self, data: bytes) -> None:
        with self._lock:
            os.write(self.master, data)

    def _commands(self) -> None:
        buffer = b""
        while self.running:
            # Polls rather than blocking in read(), which would hold the pty open after unplug()
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            try:
                buffer += os.read(self.master, 256)
            except OSError:
                return
            while b"#" in buffer:
                buffer = buffer[buffer.index(b"#"):]
                if len(buffer) < 2 or len(buffer) < buffer[1]:
                    break
                body, buffer = buffer[2:buffer[1]], buffer[buffer[1]:]
                if body.startswith(b"C2-F:"):
                    start, end = body[5:].split(b",")[:2]
                    self.start_khz, self.end_khz = int(start), int(end)
                    self.set_sweeps.append((self.start_khz, self.end_khz))
                if body.startswith((b"C0", b"C2-F:")):
                    self._write(self._config())

    def _stream(self) -> None:
        while self.running:
            try:
                self._write(b"$S" + bytes([112]) + bytes([180] * 112) + b"\r\n")
            except OSError:
                return
            time.sleep(0.01)

    def unplug(self) -> None:
        self.running = False
        for thread in self._threads:
            thread.join(1)
        os.close(self.master)


class PtyExplorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeAnalyzer()

    def tearDown(self) -> None:
        if self.fake.running:
            self.fake.unplug()

    def test_probe_and_sweep(self) -> None:
        self.assertEqual(explorer.probe(self.fake.path), 500000)
        device = explorer.RfExplorer(self.fake.path)
        try:
            config, sweeps = device.sweep(2410, 2490, count=5, timeout=5)
            self.assertAlmostEqual(config.start_mhz, 2410, places=0)
            self.assertGreaterEqual(len(sweeps), 5)
            self.assertEqual(sweeps[0][0], -90.0)
            self.assertEqual(device.generation, 1)
        finally:
            device.close()

    def test_a_range_wider_than_the_module_allows_is_swept_in_pieces(self) -> None:
        self.fake.max_span_khz = 40000                  # this module sweeps 40 MHz at most
        device = explorer.RfExplorer(self.fake.path)
        try:
            pieces = device.sweep_range(2400, 2500, count=2, timeout=5)
            self.assertEqual(len(pieces), 3)
            self.assertAlmostEqual(pieces[0][0].start_mhz, 2400, places=0)
            self.assertAlmostEqual(pieces[-1][0].end_mhz, 2500, delta=1)
            # The split is remembered: the whole range isn't asked for, and refused, again
            self.fake.set_sweeps.clear()
            device.sweep_range(2400, 2500, count=1, timeout=5)
            self.assertEqual(len(self.fake.set_sweeps), 3)
        finally:
            device.close()

    def test_a_survey_holds_the_analyzer_until_it_finishes(self) -> None:
        device = explorer.RfExplorer(self.fake.path)
        order: list[str] = []
        try:
            with device.lock:
                other = threading.Thread(
                    target=lambda: (device.lock.acquire(), order.append("live"), device.lock.release()))
                other.start()
                device.sweep(2400, 2500, count=2, timeout=5)
                order.append("survey")
            other.join(2)
            self.assertEqual(order, ["survey", "live"])
        finally:
            device.close()

    def test_unplugging_is_noticed(self) -> None:
        device = explorer.RfExplorer(self.fake.path)
        try:
            device.sweep(2400, 2500, count=1, timeout=5)
            self.assertTrue(device.alive)
            self.fake.unplug()
            deadline = time.monotonic() + 3
            while device.alive and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(device.alive)
            with self.assertRaises(ConnectionError):
                device.sweep(2400, 2500, count=1, timeout=1)
        finally:
            device.close()


if __name__ == "__main__":
    unittest.main()
