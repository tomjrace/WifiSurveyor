"""The HackRF: its driver against fake hackrf tools, the I/Q classifier, and what's built on them."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from wifisurveyor.core import advice, analysis, report, rfplan, settings, tonetest
from wifisurveyor.core.project import Point, Project, Wall
from wifisurveyor.rf import cellular, hackrf, iq
from wifisurveyor.rf import spectrum as spec

RATE = iq.SAMPLE_RATE

FAKE_SWEEP = r'''
import os, random, signal, sys, time
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("hackrf_sweep " + " ".join(args) + "\n")
if os.environ.get("FAKE_DENY"):
    sys.stderr.write("hackrf_open() failed: Access denied (insufficient permissions) (-1000)\n")
    sys.exit(1)
top = int(os.environ.get("FAKE_TOP", "7250"))
ranges, width, sweeps = [], 1000000.0, None
i = 0
while i < len(args):
    if args[i] == "-f":
        low, high = (int(v) for v in args[i + 1].split(":")); ranges.append((low, high)); i += 2
    elif args[i] == "-w":
        width = float(args[i + 1]); i += 2
    elif args[i] == "-N":
        sweeps = int(args[i + 1]); i += 2
    elif args[i] == "-1":
        sweeps = 1; i += 1
    elif args[i] in ("-l", "-g", "-a", "-d"):
        i += 2
    else:
        i += 1
if any(high > top for _low, high in ranges):
    sys.stderr.write(f"argument error: freq_max must be <= {top} MHz.\n")
    sys.exit(1)
count = int(5e6 / width)
done = 0
if os.environ.get("FAKE_CHATTY"):
    # More than a pipe holds: the real one's progress lines, after a few minutes
    sys.stderr.write("12 total sweeps completed, 40.00 sweeps/second\n" * 4000)
    sys.stderr.flush()
while sweeps is None or done < sweeps:
    lines = []
    for low, high in ranges:
        span = max(20, high - low)
        span = (span + 19) // 20 * 20
        for f in range(low, low + span, 20):
            for part in (0, 10, 5, 15):
                hz = (f + part) * 1000000
                levels = []
                for b in range(count):
                    centre = (hz + (b + 0.5) * width) / 1e6
                    levels.append(-30.0 if abs(centre - 2437) < 8 else -70.0)
                lines.append(f"2026-10-16, 10:00:00.0, {hz}, {hz + 5000000}, {width:.2f}, 20, "
                             + ", ".join(f"{v:.2f}" for v in levels))
    random.shuffle(lines)
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    done += 1
    time.sleep(0.02)
'''

FAKE_TRANSFER = r'''
import os, signal, sys, time
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("hackrf_transfer " + " ".join(args) + "\n")
if "-c" in args:
    began = time.time()
    while True:
        time.sleep(0.1)
        if os.environ.get("FAKE_TONE_DIES") and time.time() - began > 1.2:
            sys.stderr.write("hackrf_is_streaming() result: HACKRF_ERROR_STREAMING_THREAD_ERR (-1003)\n")
            sys.exit(1)
count = int(args[args.index("-n") + 1])
if os.environ.get("FAKE_SHORT"):
    count //= 2
import random
rng = random.Random(1)
out = sys.stdout.buffer
left = count
loud = bool(os.environ.get("FAKE_LOUD"))
while left > 0:
    chunk = min(left, 262144)
    if loud:
        out.write(bytes([127, 129]) * chunk)
    else:
        out.write(bytes(rng.randrange(-3, 4) & 0xFF for _ in range(2 * chunk)))
    left -= chunk
out.flush()
'''


def noise(n: int, rng: np.random.Generator) -> np.ndarray:
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.01 / np.sqrt(2)).astype(np.complex64)


def shaped(n: int, width_mhz: float, offset_mhz: float, amplitude: float,
           rng: np.random.Generator) -> np.ndarray:
    """Band-limited noise: stands in for an OFDM or other wideband signal."""
    x = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    spectrum_ = np.fft.fft(x)
    freqs = np.fft.fftfreq(n, 1 / RATE) / 1e6
    spectrum_[np.abs(freqs - offset_mhz) > width_mhz / 2] = 0
    y = np.fft.ifft(spectrum_)
    return (y / np.sqrt(np.mean(np.abs(y) ** 2)) * amplitude).astype(np.complex64)


class ClassifierTests(unittest.TestCase):
    """Synthetic captures of each kind of transmitter, a quarter of a second at 20 MS/s."""

    N = int(0.25 * RATE)

    def setUp(self) -> None:
        self.rng = np.random.default_rng(7)

    def kinds(self, samples, centre) -> list[str]:
        return [f["kind"] for f in iq.classify(samples, centre)["findings"]]

    def test_wifi_frames(self) -> None:
        x = noise(self.N, self.rng)
        for start in range(0, self.N - 40000, 60000):
            length = int(self.rng.integers(400, 30000))
            x[start:start + length] += shaped(max(length, 2048), 17, 0, 0.1, self.rng)[:length]
        result = iq.classify(x, 2437)
        self.assertEqual([f["kind"] for f in result["findings"]], ["wifi"])
        self.assertAlmostEqual(result["findings"][0]["width_mhz"], 17, delta=2)
        self.assertGreater(result["airtime"], 0.1)

    def test_bluetooth_hops(self) -> None:
        x = noise(self.N, self.rng)
        for start in range(0, self.N - 10000, 12500):
            offset = self.rng.uniform(-9, 9)
            length = int(self.rng.choice([2000, 7000]))
            bits = np.repeat(self.rng.choice([-1, 1], length // 20 + 1), 20)[:length]
            phase = np.cumsum(2 * np.pi * (offset * 1e6 + 250e3 * bits) / RATE)
            x[start:start + length] += (0.05 * np.exp(1j * phase)).astype(np.complex64)
        self.assertEqual(self.kinds(x, 2437), ["bluetooth"])

    def test_zigbee_on_its_channel(self) -> None:
        x = noise(self.N, self.rng)
        for start in range(0, self.N - 90000, 100000):
            x[start:start + 40000] += shaped(40000, 2.0, -2.0, 0.05, self.rng)
        found = iq.classify(x, 2427)["findings"]
        self.assertEqual([f["kind"] for f in found], ["zigbee"])
        self.assertEqual(found[0]["channel"], 15)          # 2425 MHz

    def test_microwave_follows_the_mains(self) -> None:
        x = noise(self.N, self.rng)
        period = int(RATE / 60)
        for start in range(0, self.N, period):
            length = min(period // 2, self.N - start)
            x[start:start + length] += shaped(length, 10, 3, 0.2, self.rng)
        found = iq.classify(x, 2452)["findings"]
        self.assertEqual(found[0]["kind"], "microwave")
        self.assertAlmostEqual(found[0]["period_ms"], 16.7, delta=0.2)

    def test_microwave_is_only_looked_for_in_2_4(self) -> None:
        x = noise(self.N, self.rng)
        period = int(RATE / 60)
        for start in range(0, self.N, period):
            length = min(period // 2, self.N - start)
            x[start:start + length] += shaped(length, 10, 3, 0.2, self.rng)
        self.assertNotIn("microwave", self.kinds(x, 5500))

    def test_carrier_that_never_stops(self) -> None:
        x = noise(self.N, self.rng)
        x += (0.03 * np.exp(2j * np.pi * 2.3e6 * np.arange(self.N) / RATE)).astype(np.complex64)
        result = iq.classify(x, 2437)
        self.assertEqual([f["kind"] for f in result["findings"]], ["continuous"])
        self.assertGreater(result["airtime"], 0.95)

    def test_radar_pulse_train(self) -> None:
        x = noise(self.N, self.rng)
        for k in range(18):
            start = 100000 + int(k * 1428e-6 * RATE)
            x[start:start + 20] += 0.2
        found = iq.classify(x, 5600)["findings"]
        self.assertEqual([f["kind"] for f in found], ["radar"])
        self.assertAlmostEqual(found[0]["pri_us"], 1428, delta=2)

    def test_quiet_channel(self) -> None:
        result = iq.classify(noise(self.N, self.rng), 2437)
        self.assertEqual(result["findings"], [])
        self.assertLess(result["airtime"], 0.01)

    def test_a_flickering_narrow_signal_is_not_radar(self) -> None:
        """Faded narrowband bursts break into short runs; none of them is isolated."""
        x = noise(self.N, self.rng)
        for start in range(0, self.N - 10000, 12500):
            x[start:start + 7000] += shaped(8192, 1.0, self.rng.uniform(-9, 9), 0.05, self.rng)[:7000]
        self.assertNotIn("radar", self.kinds(x, 2437))

    def test_pulse_watch_across_chunks(self) -> None:
        x = noise(int(1.2 * RATE), self.rng)
        for k in range(18):
            start = int(0.5 * RATE) + int(k * 1428e-6 * RATE)
            x[start:start + 30] += 0.2
        watch = iq.PulseWatch()
        # Chunks that don't line up with the envelope steps or the pulses
        for at in range(0, x.size, 524287):
            watch.feed(x[at:at + 524287])
        trains = watch.trains()
        self.assertEqual(len(trains), 1)
        self.assertAlmostEqual(trains[0]["pri_us"], 1428, delta=2)
        self.assertGreaterEqual(trains[0]["pulses"], 17)

    def test_pulse_spanning_a_chunk_boundary_is_one_pulse(self) -> None:
        x = noise(400_000, self.rng)
        x[199_990:200_030] += 0.2
        watch = iq.PulseWatch()
        watch.feed(x[:200_000])
        watch.feed(x[200_000:])
        self.assertEqual(len(watch.trains()), 0)
        self.assertEqual(len(watch.pulses) + (watch._pending is not None), 1)

    def test_short_real_frames_are_wifi(self) -> None:
        """Real frames last tens of µs, so each one's spectrum is ragged; still Wi-Fi."""
        x = noise(self.N, self.rng)
        for start in range(0, self.N - 2000, 9000):
            length = int(self.rng.integers(600, 1200))          # 30-60 µs
            x[start:start + length] += shaped(4096, 17, 0, 0.1, self.rng)[:length]
        found = iq.classify(x, 2437)["findings"]
        self.assertEqual(found[0]["kind"], "wifi")
        self.assertGreater(found[0]["airtime"], 0.05)

    def test_overlapping_channel(self) -> None:
        """A network 15 MHz up: only its lower part is inside, running off the edge."""
        x = noise(self.N, self.rng)
        for start in range(0, self.N - 30000, 40000):
            x[start:start + 20000] += shaped(20000, 17, 9.5, 0.1, self.rng)
        found = iq.classify(x, 2437)["findings"]
        self.assertEqual(found[0]["kind"], "overlap")
        self.assertGreater(found[0]["center_mhz"], 2437)

    def test_int8_samples(self) -> None:
        raw = np.array([10, -10, 20, 0, 0, 0, -30, 10], dtype=np.int8).tobytes()
        samples = iq.from_int8(raw)
        self.assertEqual(samples.size, 4)
        self.assertAlmostEqual(float(abs(samples.mean())), 0.0, places=6)     # DC removed


class SweepParsingTests(unittest.TestCase):
    def test_line(self) -> None:
        parsed = hackrf.parse_line("2026-10-16, 10:00:00.0, 2400000000, 2405000000, 1000000.00, 20, "
                                   "-70.1, -71, -69.5, -70, -72")
        self.assertEqual(parsed[:3], (2400000000, 2405000000, 1000000.0))
        self.assertEqual(len(parsed[3]), 5)
        self.assertIsNone(hackrf.parse_line("call hackrf_sample_rate_set(20.000 MHz)"))

    def test_a_sweep_ends_when_a_piece_comes_round_again(self) -> None:
        assembler = hackrf.Assembler()
        line = "d, t, {0}, {1}, 1000000.00, 20, -70, -70, -70, -70, -70"
        for low in (2405, 2400, 2415, 2410):
            self.assertIsNone(assembler.add(line.format(low * 10**6, (low + 5) * 10**6)))
        done = assembler.add(line.format(2400 * 10**6, 2405 * 10**6))
        self.assertEqual(sorted(done), [low * 10**6 for low in (2400, 2405, 2410, 2415)])
        freqs, levels = hackrf.bins(done)
        self.assertEqual(freqs.size, 20)
        self.assertTrue(np.all(np.diff(freqs) > 0))
        self.assertAlmostEqual(freqs[0], 2400.5)

    def test_bin_width_limits(self) -> None:
        self.assertEqual(hackrf.bin_width_hz(2400, 2401), hackrf.MIN_BIN_HZ)
        self.assertEqual(hackrf.bin_width_hz(1, 6000), hackrf.MAX_BIN_HZ)

    def test_tone_stays_in_ism_bands(self) -> None:
        self.assertTrue(hackrf.tone_allowed(2477))
        self.assertTrue(hackrf.tone_allowed(5732))
        self.assertFalse(hackrf.tone_allowed(2437 + 100))
        self.assertFalse(hackrf.tone_allowed(5500))

    def test_usb_detection_from_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            for name, ids, serial in (("1-2", ("1d50", "6089"), "0000000000000000675c62dc21a5c5f1"),
                                      ("1-3", ("046d", "c52b"), "")):
                device = Path(root) / name
                device.mkdir()
                (device / "idVendor").write_text(ids[0] + "\n")
                (device / "idProduct").write_text(ids[1] + "\n")
                (device / "busnum").write_text("1\n")
                (device / "devnum").write_text("7\n")
                if serial:
                    (device / "serial").write_text(serial + "\n")
            found = hackrf.plugged_in(Path(root))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].model, "HackRF One")
        self.assertEqual(found[0].node, "/dev/bus/usb/001/007")
        self.assertTrue(found[0].serial.endswith("21a5c5f1"))


class FakeTools:
    """hackrf_sweep and hackrf_transfer stand-ins on PATH, and settings kept in memory."""

    def __init__(self, **env: str) -> None:
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        for name, body in (("hackrf_sweep", FAKE_SWEEP), ("hackrf_transfer", FAKE_TRANSFER)):
            path = base / name
            path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.log = base / "log"
        self.log.write_text("")
        self.node = base / "usbnode"
        self.node.write_text("")
        self.settings: dict = {}
        self.patches = [
            mock.patch.dict(os.environ, {"PATH": f"{base}:{os.environ['PATH']}",
                                         "FAKE_LOG": str(self.log), **env}),
            mock.patch.object(settings, "load", lambda: dict(self.settings)),
            mock.patch.object(settings, "put", lambda key, value: self.settings.__setitem__(key, value)),
        ]

    def __enter__(self) -> "FakeTools":
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_exc) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.dir.cleanup()

    def open(self) -> hackrf.HackRf:
        return hackrf.HackRf(hackrf.UsbHackrf(str(self.node), "HackRF One", "abc123"))

    def commands(self) -> list[str]:
        return self.log.read_text().splitlines()


class DriverTests(unittest.TestCase):
    def test_reach_above_six_gigahertz_when_the_tools_allow(self) -> None:
        with FakeTools() as fake:
            device = fake.open()
            try:
                self.assertEqual(device.max_mhz, hackrf.TUNE_TOP_MHZ)
                self.assertEqual(device.wifi_ranges[-1], ("6 GHz", 5925.0, 7125.0))
                self.assertIn("-d abc123", fake.commands()[0])
            finally:
                device.close()

    def test_older_tools_stop_at_six_gigahertz(self) -> None:
        with FakeTools(FAKE_TOP="6000") as fake:
            device = fake.open()
            try:
                self.assertEqual(device.max_mhz, hackrf.SPEC_TOP_MHZ)
                with self.assertRaises(ValueError):
                    device.configure(5925, 6100)
            finally:
                device.close()

    def test_permission_problem_is_explained(self) -> None:
        with FakeTools(FAKE_DENY="1") as fake:
            with self.assertRaises(ConnectionError) as caught:
                fake.open()
        self.assertIn("No permission", str(caught.exception))

    def test_sweeps_land_on_an_even_grid_with_the_offset(self) -> None:
        with FakeTools() as fake:
            fake.settings["hackrf_offset_db"] = -10.0
            device = fake.open()
            try:
                config, sweeps = device.sweep(2400, 2500, count=3, timeout=20)
            finally:
                device.close()
        self.assertGreaterEqual(len(sweeps), 3)
        self.assertEqual(len(sweeps[0]), config.points)
        freqs = config.start_mhz + np.arange(config.points) * config.step_mhz
        trace = np.array(sweeps[0])
        self.assertAlmostEqual(float(trace[np.argmin(abs(freqs - 2437))]), -40.0, places=1)
        self.assertAlmostEqual(float(trace[np.argmin(abs(freqs - 2480))]), -80.0, places=1)
        self.assertEqual(config.rbw_khz, config.step_mhz * 1000)
        # It feeds the survey's figures like any analyzer
        segment = spec.build_segment(config, sweeps)
        self.assertGreater(segment["occupancy"][6], 0.9)
        self.assertLess(segment["occupancy"][11], 0.1)

    def test_capture_pauses_sweeping_and_resumes(self) -> None:
        with FakeTools() as fake:
            device = fake.open()
            try:
                device.configure(2400, 2500)
                samples = device.capture(2437, seconds=0.01)
                self.assertEqual(samples.size, int(0.01 * hackrf.IQ_RATE))
                self.assertEqual(samples.dtype, np.complex64)
                deadline = time.time() + 10
                while device.latest() is None and time.time() < deadline:
                    time.sleep(0.05)
                self.assertIsNotNone(device.latest(), "sweeping didn't pick up again")
                result = device.identify(2437, seconds=0.01)
                self.assertEqual(result["findings"], [])
            finally:
                device.close()
            transfer = [c for c in fake.commands() if c.startswith("hackrf_transfer")][0]
        self.assertIn("-b 15000000", transfer)
        self.assertIn("-f 2437000000", transfer)

    def test_raw_iq_stops_at_six_gigahertz(self) -> None:
        with FakeTools() as fake:
            device = fake.open()
            try:
                with self.assertRaises(ValueError):
                    device.capture(6525, 0.01)
            finally:
                device.close()

    def test_cellular_bands_in_one_pass(self) -> None:
        with FakeTools() as fake:
            fake.settings["hackrf_offset_db"] = 0.0
            device = fake.open()
            try:
                freqs, levels = device.sweep_bands(cellular.sweep_ranges(), sweeps=2)
            finally:
                device.close()
            sweep = [c for c in fake.commands() if " -N 2" in c][0]
        result = cellular.summarize(freqs, levels)
        self.assertEqual(set(result), {key for key, *_ in cellular.BANDS})
        # -70 dBm in each 100 kHz bin is 50 of them in 5 MHz: about -53
        self.assertAlmostEqual(result["pcs"]["peak_dbm"], -70 + 10 * np.log10(50), delta=0.5)
        self.assertEqual(sweep.count(" -f "), len(cellular.sweep_ranges()))

    def test_tone_is_capped_and_blocks_sweeping(self) -> None:
        with FakeTools() as fake:
            device = fake.open()
            try:
                with self.assertRaises(ValueError):
                    device.start_tone(2437 + 300)
                device.start_tone(2477, tx_vga=47)
                self.assertTrue(device.sending)
                with self.assertRaises(RuntimeError):
                    device.configure(2400, 2500)
                with self.assertRaises(RuntimeError):
                    device.capture(2437, 0.01)
                device.stop_tone()
                self.assertFalse(device.sending)
                device.configure(2400, 2500)
            finally:
                device.close()
            tone = [c for c in fake.commands() if " -c " in c][0]
        self.assertIn(f"-x {hackrf.MAX_TX_VGA}", tone)
        self.assertIn("-a 0", tone)
        self.assertIn("-f 2477000000", tone)

    def test_tone_turns_itself_off(self) -> None:
        with FakeTools() as fake:
            device = fake.open()
            try:
                device.start_tone(2477, minutes=0.5 / 60)
                deadline = time.time() + 5
                while device.sending and time.time() < deadline:
                    time.sleep(0.1)
                self.assertFalse(device.sending)
            finally:
                device.close()

    def test_radar_watch_streams(self) -> None:
        with FakeTools() as fake:
            device = fake.open()
            try:
                watch = device.watch_pulses(5600, 0.05)
            finally:
                device.close()
        self.assertGreater(watch.position, 0.04 * 1e6)
        self.assertEqual(watch.trains(), [])

    def test_calibrating_against_another_analyzer(self) -> None:
        with FakeTools() as fake:
            fake.settings["hackrf_offset_db"] = -10.0
            device = fake.open()
            reference = mock.Mock()
            reference.name, reference.model = "tinySA", "tinySA4"
            # The reference reads every channel 7 dB above what the HackRF does now
            reference.sweep_range.side_effect = lambda start, end, count: [
                (config, [list(np.array(trace) + 7) for trace in traces])
                for config, traces in device.sweep_range(start, end, count=3)]
            try:
                result = hackrf.match_analyzer(device, reference)
            finally:
                device.close()
        self.assertAlmostEqual(result.offset, -3.0, places=1)
        self.assertGreaterEqual(result.channels, 2)
        self.assertEqual(result.deaf, [])
        # The unit opened took over the old calibration, and now has its own
        unit = fake.settings["hackrf_units"]["abc123"]
        self.assertEqual(unit["offset_db"], -3.0)
        self.assertEqual(unit["cal_gains"], [16, 20, False])
        self.assertIsNone(fake.settings["hackrf_offset_db"])


    def test_a_tone_that_dies_frees_the_device(self) -> None:
        with FakeTools(FAKE_TONE_DIES="1") as fake:
            device = fake.open()
            try:
                device.configure(2400, 2500)
                device.start_tone(2477)
                deadline = time.time() + 5
                while device.sending and time.time() < deadline:
                    time.sleep(0.1)
                self.assertFalse(device.sending)
                self.assertIn("stopped by itself", device.tone_note)
                self.assertIn("-1003", device.tone_note)
                # Sweeping picks up again by itself, and anything else may use it
                deadline = time.time() + 10
                while device.latest() is None and time.time() < deadline:
                    time.sleep(0.05)
                self.assertIsNotNone(device.latest())
                device.capture(2437, 0.01)
            finally:
                device.close()

    def test_sweeping_picks_up_after_the_tone(self) -> None:
        with FakeTools() as fake:
            device = fake.open()
            try:
                device.configure(2400, 2500)
                device.start_tone(2477)
                self.assertIsNotNone(device.tone_left)
                device.stop_tone()
                self.assertIsNone(device.tone_left)
                deadline = time.time() + 10
                while device.latest() is None and time.time() < deadline:
                    time.sleep(0.05)
                self.assertIsNotNone(device.latest(), "sweeping didn't pick up after the tone")
                self.assertEqual(device.tone_note, "")
            finally:
                device.close()

    def test_a_short_capture_is_refused(self) -> None:
        with FakeTools(FAKE_SHORT="1") as fake:
            device = fake.open()
            try:
                with self.assertRaises(RuntimeError) as caught:
                    device.capture(2437, 0.01)
            finally:
                device.close()
        self.assertIn("of 200000 samples", str(caught.exception))

    def test_overload_is_noticed(self) -> None:
        with FakeTools(FAKE_LOUD="1") as fake:
            device = fake.open()
            try:
                result = device.identify(2437, seconds=0.01)
            finally:
                device.close()
        self.assertGreater(result["clipped"], 0.9)
        self.assertIn("overloaded", iq.overload_note(result["clipped"], "at 2437 MHz"))
        self.assertEqual(iq.overload_note(0.0, "at 2437 MHz"), "")

    def test_progress_on_stderr_never_stalls_a_sweep(self) -> None:
        with FakeTools(FAKE_CHATTY="1") as fake:
            device = fake.open()
            try:
                config, sweeps = device.sweep(2400, 2500, count=3, timeout=20)
            finally:
                device.close()
        self.assertGreaterEqual(len(sweeps), 3)

    def test_new_gains_and_antenna_power_reach_a_running_sweep(self) -> None:
        with FakeTools() as fake:
            device = fake.open()
            try:
                device.configure(2400, 2500)
                fake.settings.update(hackrf_lna=32, hackrf_bias_tee=True)
                device.apply_settings()
                deadline = time.time() + 10
                while not any("-l 32" in c for c in fake.commands()) and time.time() < deadline:
                    time.sleep(0.05)
                device.start_tone(2477)
            finally:
                device.close()
            commands = fake.commands()
        sweep = [c for c in commands if c.startswith("hackrf_sweep") and "-l 32" in c]
        self.assertTrue(sweep, "the sweep wasn't restarted with the new gain")
        self.assertIn("-p 1", sweep[0])
        tone = [c for c in commands if " -c " in c][0]
        self.assertIn("-p 0", tone)
        self.assertNotIn("-p 1", tone)

    def test_a_band_the_antenna_misses_is_left_out(self) -> None:
        with FakeTools() as fake:
            fake.settings["hackrf_offset_db"] = -10.0
            device = fake.open()
            networks = [
                {"ssid": "Office", "bssid": "aa:bb:cc:00:00:01", "rssi": -50, "freq_mhz": 2437,
                 "channel": 6, "band": "2.4"},
                # Strong, but the fake HackRF hears only noise at 5 GHz
                {"ssid": "Office", "bssid": "aa:bb:cc:00:00:02", "rssi": -45, "freq_mhz": 5200,
                 "channel": 40, "band": "5"}]
            try:
                result = hackrf.match_wifi(device, networks)
            finally:
                device.close()
        self.assertEqual(result.deaf, ["5"])
        self.assertNotIn("5", result.bands)
        self.assertEqual(fake.settings["hackrf_units"]["abc123"]["deaf"]["bands"], ["5"])
        self.assertIn("5 GHz", hackrf.antenna_note(result.deaf))


class HearingTests(unittest.TestCase):
    @staticmethod
    def segment(start: float, levels: np.ndarray) -> dict:
        return {"start_mhz": start, "step_mhz": 0.2, "rbw_khz": 200, "sweeps": 1, "occupancy": {},
                "avg": levels.tolist(), "max": levels.tolist(), "min": np.full(levels.size, -100.0).tolist()}

    def test_a_spur_is_not_hearing_the_band(self) -> None:
        freqs = 5150 + np.arange(1001) * 0.2
        levels = np.full(freqs.size, -100.0)
        levels[np.abs(freqs - 5186) < 0.3] = -69                  # a narrow spur, 31 dB up
        networks = [{"rssi": -50, "freq_mhz": 5180}]
        self.assertEqual(hackrf.band_hearing(networks, [self.segment(5150, levels)]), {"5": False})
        levels[np.abs(freqs - 5180) <= 9] = -80                   # Wi-Fi: 20 dB up across 18 MHz
        self.assertEqual(hackrf.band_hearing(networks, [self.segment(5150, levels)]), {"5": True})


class CalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings: dict = {}
        for patch in (mock.patch.object(settings, "load", lambda: dict(self.settings)),
                      mock.patch.object(settings, "put",
                                        lambda key, value: self.settings.__setitem__(key, value))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_offset_follows_the_gains(self) -> None:
        self.assertEqual(hackrf.offset_db(), hackrf.DEFAULT_OFFSET_DB)
        self.settings["hackrf_lna"] = 24                  # 8 dB more than the default
        self.assertEqual(hackrf.offset_db(), hackrf.DEFAULT_OFFSET_DB - 8)
        hackrf.set_offset_by_hand(-10.0)
        self.assertEqual(hackrf.offset_db(), -10.0)
        self.settings.update(hackrf_lna=16, hackrf_amp=True)
        self.assertAlmostEqual(hackrf.offset_db(), -10.0 + 8 - hackrf.AMP_DB)
        self.assertAlmostEqual(hackrf.gain_shift(), hackrf.AMP_DB - 8)

    def test_an_old_offset_without_gains_is_taken_as_is(self) -> None:
        self.settings.update(hackrf_offset_db=-12.0, hackrf_lna=40)
        self.assertEqual(hackrf.offset_db(), -12.0)

    def test_an_old_offset_adopts_the_gains_it_was_used_at(self) -> None:
        self.settings.update(hackrf_offset_db=-12.0, hackrf_lna=24)
        hackrf.adopt_legacy("unit-a")
        self.assertEqual(self.settings["hackrf_units"]["unit-a"]["cal_gains"], [24, 20, False])
        self.settings["hackrf_lna"] = 8
        self.assertEqual(hackrf.offset_db(unit="unit-a"), -12.0 + 16)
        hackrf.adopt_legacy("unit-a")                 # only once
        self.assertEqual(self.settings["hackrf_units"]["unit-a"]["cal_gains"], [24, 20, False])

    def test_each_hackrf_keeps_its_own_calibration(self) -> None:
        self.settings["hackrf_offset_db"] = -12.0
        hackrf.adopt_legacy("unit-a")                 # the first opened takes the old one
        hackrf.adopt_legacy("unit-b")
        self.assertTrue(hackrf.calibrated("unit-a"))
        self.assertFalse(hackrf.calibrated("unit-b"))
        self.assertEqual(hackrf.offset_db(unit="unit-b"), hackrf.DEFAULT_OFFSET_DB)
        hackrf.set_offset_by_hand(-20.0, "unit-b")
        self.assertEqual(hackrf.offset_db(unit="unit-a"), -12.0)
        self.assertEqual(hackrf.offset_db(unit="unit-b"), -20.0)
        hackrf.note_hearing({"5": False}, "unit-b")
        self.assertEqual((hackrf.deaf_bands("unit-a"), hackrf.deaf_bands("unit-b")), ([], ["5"]))
        snap = hackrf.snapshot("unit-b")
        self.assertEqual((snap["calibrated"], snap["offset_db"], snap["deaf"]), (True, -20.0, ["5"]))

    def test_each_band_gets_its_own_offset(self) -> None:
        self.settings["hackrf_offset_db"] = -10.0
        differences = [(2412, 7.0), (2437, 7.0), (2462, 7.5), (5180, 2.0), (5200, 2.0)]
        result = hackrf._recalibrate(differences, "a test", [])
        self.assertAlmostEqual(hackrf.offset_db(2437), -3.0, places=1)
        self.assertAlmostEqual(hackrf.offset_db(5200), -8.0, places=1)
        self.assertAlmostEqual(hackrf.offset_db(700), result.offset)      # cellular: overall
        levels = hackrf.offsets(np.array([700.0, 2437.0, 5200.0, 6500.0]))
        self.assertAlmostEqual(levels[1], -3.0, places=1)
        self.assertAlmostEqual(levels[2], -8.0, places=1)
        self.assertAlmostEqual(levels[3], result.offset)
        self.assertEqual(set(result.bands), {"2.4", "5"})

    def test_hearing_a_band_again_clears_it(self) -> None:
        hackrf.note_hearing({"5": False, "2.4": True})
        self.assertEqual(hackrf.deaf_bands(), ["5"])
        hackrf.note_hearing({"5": True})
        self.assertEqual(hackrf.deaf_bands(), [])


class RecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch = mock.patch.object(hackrf, "recordings_dir", lambda: Path(self.dir.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_sigmf_recording(self) -> None:
        import json

        raw = bytes(range(256)) * 8
        path = Path(hackrf.save_sigmf(raw, 2437, 20e6, "HackRF One", "test",
                                      [{"core:sample_start": 0, "core:label": "Bursts"}]))
        self.assertEqual(path.read_bytes(), raw)
        meta = json.loads(path.with_suffix(".sigmf-meta").read_text())
        self.assertEqual(meta["global"]["core:datatype"], "ci8")
        self.assertEqual(meta["captures"][0]["core:frequency"], 2437e6)
        self.assertEqual(hackrf.recordings_size(), (1, len(raw)))

    def test_oldest_recordings_go_first(self) -> None:
        with mock.patch.object(hackrf, "KEEP_IQ_MB", 0.0025):          # 2500 bytes
            first = Path(hackrf.save_sigmf(bytes(1000), 2437, 20e6, "HackRF", "", []))
            os.utime(first, (1, 1))
            hackrf.save_sigmf(bytes(1000), 2437, 20e6, "HackRF", "", [])
            hackrf.save_sigmf(bytes(1000), 2437, 20e6, "HackRF", "", [])
        self.assertFalse(first.exists())
        self.assertFalse(first.with_suffix(".sigmf-meta").exists())
        self.assertEqual(hackrf.recordings_size()[0], 2)


class RadarWatchTests(unittest.TestCase):
    def test_what_was_heard_survives_a_failure(self) -> None:
        rng = np.random.default_rng(3)
        x = noise(int(0.2 * RATE), rng)
        for k in range(18):
            start = 20000 + int(k * 1428e-6 * RATE)
            x[start:start + 30] += 0.2
        calls = []

        def watch_pulses(centre, seconds, stop):
            calls.append(centre)
            if len(calls) == 3:
                raise RuntimeError("The HackRF isn't answering. Is it still plugged in?")
            watch = iq.PulseWatch()
            watch.feed(x if len(calls) == 1 else noise(x.size, rng))
            return watch

        device = mock.Mock(watch_pulses=watch_pulses)
        result = hackrf.watch_dfs(device, 16 * 60, threading.Event())
        self.assertEqual(result.channels, [120, 124])
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0]["channel"], 120)
        self.assertIn("channel 128", result.error)

    def test_stopping_before_a_channel_starts_is_not_a_failure(self) -> None:
        stop = threading.Event()

        def watch_pulses(centre, seconds, stop_):
            stop.set()
            return iq.PulseWatch()                          # stopped before any samples

        result = hackrf.watch_dfs(mock.Mock(watch_pulses=watch_pulses), 60, stop)
        self.assertEqual((result.channels, result.error), ([], ""))

    def test_the_fast_feed_finds_the_same_radar(self) -> None:
        rng = np.random.default_rng(5)
        # A few LSBs of noise and a DC offset bigger than it, as a real HackRF gives with
        # nothing much on the channel
        x = noise(int(1.2 * RATE), rng) * 1.2 + 0.023j
        for k in range(18):
            start = int(0.5 * RATE) + int(k * 1428e-6 * RATE)
            x[start:start + 30] += 0.5
        pairs = np.empty(2 * x.size, dtype=np.float32)
        pairs[0::2], pairs[1::2] = x.real, x.imag
        raw = np.clip(np.round(pairs * 128), -128, 127).astype(np.int8).tobytes()
        watch = iq.PulseWatch()
        for at in range(0, len(raw), 1048573):                  # odd sizes: halves of a sample
            watch.feed_int8(raw[at:at + 1048573])
        trains = watch.trains()
        self.assertEqual(len(trains), 1)
        self.assertAlmostEqual(trains[0]["pri_us"], 1428, delta=2)
        self.assertGreaterEqual(trains[0]["pulses"], 17)
        self.assertEqual(watch.clipped_share, 0.0)
        # Short noise spikes aren't pulses: no more than the complex path finds
        quiet = np.clip(np.round(pairs[: 2 * int(0.4 * RATE)] * 128), -128, 127).astype(np.int8)
        fast, slow = iq.PulseWatch(), iq.PulseWatch()
        fast.feed_int8(quiet.tobytes())
        slow.feed(iq.from_int8(quiet.tobytes()))
        self.assertLessEqual(fast.found, slow.found + 2)


def project_with_points() -> Project:
    project = Project(meters_per_pixel=0.05, width=1000, height=600)
    for index in range(6):
        project.points.append(Point(x=100 + index * 150, y=300, networks=[
            {"ssid": "Office", "bssid": f"aa:bb:cc:00:00:0{index}", "rssi": -55, "freq_mhz": 2437,
             "channel": 6, "band": "2.4"},
            {"ssid": "Office", "bssid": "aa:bb:cc:00:01:00", "rssi": -60, "freq_mhz": 5600,
             "channel": 120, "band": "5", "center_mhz": 5600, "width_mhz": 20}]))
    return project


class AnalysisTests(unittest.TestCase):
    def test_walls_fitted_from_the_tone(self) -> None:
        project = Project(meters_per_pixel=0.05, width=1000, height=600)
        project.walls = [Wall(400, 0, 400, 600, db=3), Wall(700, 0, 700, 600, db=3),
                         Wall(0, 550, 1000, 550, db=3)]
        project.tone_source = {"x": 100, "y": 300, "freq_mhz": 2477}
        rng = np.random.default_rng(0)
        for x in range(150, 1000, 70):
            for y in (100, 250, 400):
                distance = max(np.hypot(x - 100, y - 300) * 0.05, 0.5)
                walls = 12.0 * (x > 400) + 5.0 * (x > 700)
                level = -30 - 28 * np.log10(distance) - walls + rng.normal(0, 1.0)
                project.points.append(Point(x=x, y=y, tone={"freq_mhz": 2477, "dbm": float(level)}))
        fit = analysis.fit_walls(project)
        measured = {w["index"]: w["measured"] for w in fit["walls"]}
        self.assertEqual(set(measured), {0, 1})          # nothing crosses the third
        self.assertAlmostEqual(measured[0], 12, delta=1.5)
        self.assertAlmostEqual(measured[1], 5, delta=1.5)
        self.assertAlmostEqual(fit["exponent"], 2.8, delta=0.3)
        self.assertLess(fit["rms"], 2)

    def test_wall_losses_never_go_negative(self) -> None:
        project = Project(meters_per_pixel=0.05, width=1000, height=600)
        project.walls = [Wall(400, 0, 400, 600, db=3)]
        project.tone_source = {"x": 100, "y": 300, "freq_mhz": 2477}
        for x in range(150, 1000, 80):
            distance = max(abs(x - 100) * 0.05, 0.5)
            # Louder beyond the wall than distance alone would allow
            level = -30 - 20 * np.log10(distance) + (4 if x > 400 else 0)
            project.points.append(Point(x=x, y=300, tone={"freq_mhz": 2477, "dbm": float(level)}))
        fit = analysis.fit_walls(project)
        self.assertGreaterEqual(fit["walls"][0]["measured"], 0)

    def test_too_few_tone_points(self) -> None:
        project = Project()
        project.tone_source = {"x": 1, "y": 1, "freq_mhz": 2477}
        self.assertIsNone(analysis.fit_walls(project))

    def test_cellular_and_tone_maps(self) -> None:
        point = Point(x=0, y=0, cellular={"700": {"peak_dbm": -70.0, "peak_mhz": 740, "total_dbm": -65},
                                         "pcs": {"peak_dbm": -60.0, "peak_mhz": 1960, "total_dbm": -55},
                                         "ism900": {"peak_dbm": -40.0, "peak_mhz": 915, "total_dbm": -40}},
                      tone={"freq_mhz": 2477, "dbm": -66.0})
        ctx = analysis.Context(Project(points=[point]))
        self.assertEqual(analysis.metric(point, "cellular", ctx), -60.0)    # ISM isn't cellular
        ctx.cellular_band = "700"
        self.assertEqual(analysis.metric(point, "cellular", ctx), -70.0)
        self.assertEqual(analysis.metric(point, "tone", ctx), -66.0)

    def test_airtime_from_the_hackrf_without_a_capture(self) -> None:
        project = project_with_points()
        point = project.points[0]
        point.iq = [{"center_mhz": 2437, "airtime": 0.62, "findings": []}]
        ctx = analysis.Context(project)
        self.assertAlmostEqual(analysis.metric(point, "airtime", ctx, ssid="Office", band="2.4"), 62)
        self.assertEqual(analysis.diagnose(point, ctx, "Office", band="2.4")[0], "congestion")

    def test_identified_interferer_drives_advice(self) -> None:
        project = project_with_points()
        point = project.points[0]
        point.iq = [{"center_mhz": 2437, "airtime": 0.3, "findings": [
            {"kind": "video", "name": "Continuous wideband", "description": "…",
             "center_mhz": 2440, "width_mhz": 8, "bursts": 1, "airtime": 0.99, "above_db": 20}]}]
        found = advice.improvements(point, analysis.Context(project), "Office", "2.4")
        named = [f for f in found if f.title.startswith("Continuous wideband")]
        self.assertEqual(named[0].severity, "High")
        self.assertIn("video sender", named[0].fix)

    def test_noise_source_kind_comes_from_the_hackrf(self) -> None:
        project = project_with_points()
        project.points[2].iq = [{"center_mhz": 2427, "airtime": 0.4, "findings": [
            {"kind": "zigbee", "name": "Zigbee / Thread", "description": "", "center_mhz": 2425,
             "width_mhz": 2.2, "bursts": 30, "airtime": 0.4, "above_db": 15, "channel": 15}]}]
        self.assertEqual(analysis.identified_kind(project, 2425, 4), "zigbee")
        self.assertIsNone(analysis.identified_kind(project, 2470, 4))

    def test_radar_steers_the_channel_plan_and_audit(self) -> None:
        project = project_with_points()
        project.radar = {"watches": [{"time": 0, "seconds": 600, "channels": [120]}],
                         "events": [{"time": 0, "center_mhz": 5600, "channel": 120, "pri_us": 1428,
                                     "width_us": 1, "pulses": 18}]}
        plan = rfplan.channel_plan(project, "Office", allow_dfs=True)
        five = [s for s in plan if s.radio.band == "5"][0]
        self.assertTrue(five.changed)
        self.assertNotIn("120", five.suggested)
        findings = [f for f in rfplan.audit(project) if f.title == "Radar on this radio's channel"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "High")

    def test_report_sections(self) -> None:
        project = project_with_points()
        project.points[0].iq = [{"center_mhz": 2437, "airtime": 0.3, "findings": [
            {"kind": "microwave", "name": "Microwave oven", "description": "", "center_mhz": 2452,
             "width_mhz": 10, "bursts": 8, "airtime": 0.4, "above_db": 20, "period_ms": 16.7}]}]
        project.points[1].cellular = {"pcs": {"peak_dbm": -85.0, "peak_mhz": 1960, "total_dbm": -80}}
        project.radar = {"watches": [{"time": time.time(), "seconds": 480, "channels": [120]}],
                         "events": []}
        html = report.build_html(project, None, lambda p: None)
        self.assertIn("Non-Wi-Fi transmitters identified", html)
        self.assertIn("Microwave oven", html)
        self.assertIn("No radar heard", html)
        self.assertIn("Cellular coverage", html)
        self.assertIn("Weak cellular at 1 of 1 points", html)
        self.assertIn("Identified by the HackRF", html)

    def test_new_fields_survive_saving(self) -> None:
        project = project_with_points()
        project.points[0].iq = [{"center_mhz": 2437, "airtime": 0.1, "findings": []}]
        project.points[0].cellular = {"pcs": {"peak_dbm": -70.0}}
        project.points[0].tone = {"freq_mhz": 2477, "dbm": -50.0}
        project.radar = {"watches": [], "events": [{"center_mhz": 5600}]}
        project.tone_source = {"x": 1, "y": 2, "freq_mhz": 2477}
        again = Project.from_dict(project.to_dict())
        self.assertEqual(again.points[0].iq, project.points[0].iq)
        self.assertEqual(again.points[0].cellular, project.points[0].cellular)
        self.assertEqual(again.points[0].tone, project.points[0].tone)
        self.assertEqual(again.radar, project.radar)
        self.assertEqual(again.tone_source, project.tone_source)

    def test_tone_level_from_a_dwell(self) -> None:
        config = mock.Mock(start_mhz=2474.0, step_mhz=0.01, rbw_khz=10)
        trace = [-100.0] * 600
        trace[302] = -55.0                     # 2477.02 MHz: a little off frequency
        found = spec.tone_level(config, [trace, trace], 2477)
        self.assertEqual(found["dbm"], -55.0)
        quiet = spec.tone_level(config, [[-100.0] * 600] * 2, 2477)
        self.assertIsNone(quiet["dbm"])


class ToneTestGuideTests(unittest.TestCase):
    def test_instructions_follow_what_is_connected(self) -> None:
        both = tonetest.setup(2477, "HackRF One", "tinySA Ultra+")
        self.assertTrue(both.start_here)
        self.assertIn("extension", " ".join(both.steps))
        only_hackrf = tonetest.setup(2477, "HackRF One", None)
        self.assertFalse(only_hackrf.start_here)
        self.assertIn("can't send and listen at once", " ".join(only_hackrf.steps))
        only_analyzer = tonetest.setup(2477, None, "tinySA Ultra+")
        self.assertIn("second computer", " ".join(only_analyzer.steps))
        nothing = tonetest.setup(2477, None, None)
        self.assertFalse(nothing.ready)
        self.assertEqual(both.command, "hackrf_transfer -c 127 -f 2477000000 -s 2000000 -x 10 -a 0")
        self.assertIn(both.command, " ".join(both.steps))

    def test_steps_in_order(self) -> None:
        project = Project(meters_per_pixel=0.05, width=1000, height=600)
        self.assertEqual(tonetest.next_step(project, True).number, 1)
        project.tone_source = {"x": 100, "y": 300, "freq_mhz": 2477}
        self.assertEqual(tonetest.next_step(project, False).action, "setup")
        step = tonetest.next_step(project, True)
        self.assertEqual((step.number, step.action), (2, "setup"))
        project.points.append(Point(x=150, y=300, tone={"freq_mhz": 2477, "dbm": None}))
        self.assertIn("hasn't been heard", tonetest.next_step(project, True).text)
        for x in (200, 300, 500, 600, 700):
            project.points.append(Point(x=x, y=300, tone={"freq_mhz": 2477, "dbm": -50.0 - x / 20}))
        self.assertIn("Draw the walls", tonetest.next_step(project, True).text)
        project.walls = [Wall(400, 0, 400, 600), Wall(800, 0, 800, 600)]
        step = tonetest.next_step(project, True)
        self.assertEqual((step.number, step.action), (4, "fit"))
        self.assertIn("1 wall measured", step.text)
        self.assertEqual(tonetest.wall_paths(project), {0: 3})


if __name__ == "__main__":
    unittest.main()
