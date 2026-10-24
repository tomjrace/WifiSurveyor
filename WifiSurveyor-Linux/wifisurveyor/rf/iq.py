"""What a stretch of raw I/Q says about who is transmitting: the HackRF's close look at a channel.

A swept analyzer sees a channel for a moment each sweep, so it can only guess at a burst's
shape. Twenty million samples a second see every transmission whole: how long it lasts, how
wide it is, and when the next one comes. That's enough to tell the usual 2.4 and 5 GHz
neighbours apart:

  Wi-Fi        16-20 MHz wide (wider channels fill the whole capture), 20 µs to 5.5 ms
  Bluetooth    about 1 MHz wide, under 3.2 ms, a different frequency nearly every burst
  Zigbee       about 2 MHz wide, under 4.3 ms, always on one 802.15.4 channel (2405 + 5k MHz)
  Microwave    on and off with the mains, every 16.7 ms (60 Hz) or 20 ms (50 Hz)
  Continuous   never stops: an analog video sender, camera or a stuck transmitter
  Radar        pulses of a few µs, spaced exactly evenly (FCC/ETSI DFS test patterns)

Levels here are relative to the capture's own noise floor; the HackRF's I/Q isn't calibrated.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 20e6
STEP_US = 1.0                 # the envelope's resolution
THRESHOLD_DB = 8.0            # above the floor to count as a transmission
MERGE_GAP_US = 2.0            # gaps this short inside a burst are the same burst
MAX_BURSTS = 400              # bursts whose spectrum is worked out, the strongest
WIFI_MAX_US = 5500            # the longest Wi-Fi PPDU (5.484 ms)
BT_MAX_US = 3200              # the longest Bluetooth packet (5 slots)
ZIGBEE_MAX_US = 4400          # 133 bytes at 250 kb/s
# The receiver's filter rolls off past here, and signals from beyond the capture fold back in
# near its edges, so a narrow burst out there can't be trusted to be what it looks like
EDGE_MHZ = 7.0
MAX_PULSE_US = 20.0           # shorter than any Wi-Fi frame (preamble alone is 20 µs)
# Quiet this long either side, or a "pulse" is more likely a dip-split piece of a longer
# transmission. Radar pulses are 150 µs or more apart.
ISOLATION_US = 50.0
PULSE_THRESHOLD_DB = 10.0
# The radar watch works the floor out again only every few chunks, from a slice of one: it's
# the costly part, and the floor barely moves between chunks
FLOOR_EVERY = 8
FLOOR_SAMPLES = 400 * 256
# An 8-bit sample at full scale: this much of a capture at full scale means the receiver was
# overloaded, and the findings may include signals that aren't there
CLIP_LEVEL = 127
OVERLOAD_SHARE = 0.001
ZIGBEE_CENTRES = [2405 + 5 * k for k in range(16)]

KINDS = {
    "wifi": ("Wi-Fi", "Wi-Fi frames, including networks too weak for the card to decode."),
    "bluetooth": ("Bluetooth", "Short narrow bursts hopping across the band: Bluetooth or "
                               "Bluetooth Low Energy devices nearby."),
    "zigbee": ("Zigbee / Thread", "2 MHz wide bursts on a fixed 802.15.4 channel: smart-home "
                                  "sensors, lighting, meters."),
    "microwave": ("Microwave oven", "Switching on and off with the mains supply. Ruins the top "
                                    "of 2.4 GHz while it runs."),
    "continuous": ("Continuous carrier", "A narrow signal that never stops: a cordless device, "
                                         "a faulty transmitter or a test signal."),
    "video": ("Continuous wideband", "A wide signal that never stops: an analog video sender, "
                                     "wireless camera or a jammer. It never yields airtime."),
    "overlap": ("Wi-Fi on an overlapping channel", "Wi-Fi frames running off the edge of the "
                                                    "channel: a network on a channel that "
                                                    "partly overlaps this one, such as 3 or 4 "
                                                    "next to 1 or 6."),
    "radar": ("Radar", "Short pulses repeated at an exact rate: radar on a DFS channel. Access "
                       "points must leave the channel when they hear it."),
    "bursty": ("Unidentified bursts", "Transmissions that aren't Wi-Fi, Bluetooth or Zigbee by "
                                      "their width and timing."),
}


def from_int8(raw: bytes) -> np.ndarray:
    """hackrf_transfer's interleaved signed 8-bit I/Q, with the receiver's DC offset taken out."""
    data = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int8).astype(np.float32) / 128.0
    samples = data[0::2] + 1j * data[1::2]
    return (samples - samples.mean()).astype(np.complex64) if samples.size else samples


def clipped_share(raw: bytes) -> float:
    """About the share of samples with I or Q at full scale, from hackrf_transfer's raw bytes."""
    data = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int8)
    if data.size == 0:
        return 0.0
    return min(1.0, _full_scale(data) / (data.size // 2))


def _full_scale(data: np.ndarray) -> int:
    """
    How many I and Q values sit at full scale (127, -127 or -128). Counted per value, not per
    sample, which only overcounts when both of a sample's halves clip at once.
    """
    return int(np.count_nonzero(data.view(np.uint8) - np.uint8(CLIP_LEVEL) <= 2))


def overload_note(share: float, where: str) -> str:
    """What to tell someone whose capture clipped, or "" if it didn't clip enough to matter."""
    if share < OVERLOAD_SHARE:
        return ""
    return (f"The HackRF overloaded {where} ({share * 100:.1f}% of samples at full scale), so "
            "what it names there may include signals that aren't there. Lower the LNA and VGA "
            "gain in Setup → HackRF, or turn its amplifier off.")


def envelope(samples: np.ndarray, rate: float = SAMPLE_RATE, step_us: float = STEP_US) -> np.ndarray:
    """Mean power in each step_us of the capture."""
    step = max(1, int(round(rate * step_us / 1e6)))
    usable = samples.size // step * step
    power = (samples[:usable].real ** 2 + samples[:usable].imag ** 2)
    return power.reshape(-1, step).mean(axis=1)


def welch(samples: np.ndarray, nfft: int = 256, typical: bool = False) -> np.ndarray:
    """
    Power per bin, scaled so white noise reads its per-sample power in every bin, which
    makes the noise floor directly comparable with the envelope. DC is in the middle.
    ``typical`` takes each bin's median over time rather than its mean, so bursts that come
    and go don't lift it: what's left is the floor plus anything that never stops.
    """
    window = np.hanning(nfft).astype(np.float32)
    count = samples.size // nfft
    if count == 0:
        padded = np.zeros(nfft, dtype=np.complex64)
        padded[: samples.size] = samples
        segments, count = padded.reshape(1, nfft), 1
    else:
        segments = samples[: count * nfft].reshape(count, nfft)
    # Every segment would cost a lot on a long capture; a few hundred spread through it is plenty
    if count > 400:
        segments = segments[np.linspace(0, count - 1, 400).astype(int)]
    power = np.abs(np.fft.fftshift(np.fft.fft(segments * window, axis=1), axes=1)) ** 2
    scale = float((window ** 2).sum())
    if typical:
        return np.median(power, axis=0) / np.log(2) / scale     # an exponential's median is ln 2 of its mean
    return power.mean(axis=0) / scale


def noise_floor(samples: np.ndarray) -> float:
    """
    The per-sample noise power, from the spectrum's quiet bins at their typical level, so
    neither a transmitter that never stops nor a busy channel can pass for the floor. The
    edges (where the baseband filter rolls off) and DC are left out.
    """
    psd = welch(samples, typical=True)
    nfft = psd.size
    keep = np.r_[nfft // 10: nfft // 2 - 2, nfft // 2 + 3: nfft - nfft // 10]
    return float(max(np.percentile(psd[keep], 20), 1e-12))


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) of each run of True, end exclusive."""
    if mask.size == 0:
        return []
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()))


def close_gaps(mask: np.ndarray, gap: int) -> np.ndarray:
    """Fills False runs no longer than ``gap`` between two True runs."""
    if gap <= 0:
        return mask
    filled = mask.copy()
    found = runs(~mask)
    for start, end in found:
        if start > 0 and end < mask.size and end - start <= gap:
            filled[start:end] = True
    return filled


@dataclass(slots=True)
class Burst:
    start_us: float
    length_us: float
    above_db: float               # mean power over the floor
    offset_mhz: float | None = None
    width_mhz: float | None = None
    reach_mhz: float | None = None       # how far from the centre its spectrum goes


def occupied(samples: np.ndarray, floor: float, rate: float) -> tuple[float, float, float] | None:
    """
    Centre offset, width and highest edge (all MHz from the capture's centre) of one burst's
    spectrum, or None if it's too short to say. The width is the span holding 90% of the
    power above the floor, the usual occupied-bandwidth measure: a real OFDM frame's spectrum
    is ragged over a few tens of µs, and a "fall 10 dB from the peak" rule stops at its first
    dip and calls it narrow.
    """
    if samples.size < 64:
        return None
    nfft = 256 if samples.size >= 2048 else 64          # short bursts: more segments to average
    psd = welch(samples, nfft)
    psd[nfft // 2] = (psd[nfft // 2 - 1] + psd[nfft // 2 + 1]) / 2       # DC
    above = np.maximum(psd - floor, 0.0)
    total = float(above.sum())
    if total <= floor * nfft * 0.5:                      # not clearly above the floor
        return None
    share = np.cumsum(above) / total
    low = int(np.searchsorted(share, 0.05))
    high = int(np.searchsorted(share, 0.95))
    bin_mhz = rate / nfft / 1e6
    lowest = (low - nfft / 2) * bin_mhz
    highest = (high + 1 - nfft / 2) * bin_mhz
    return (lowest + highest) / 2, highest - lowest, max(abs(lowest), abs(highest))


def find_bursts(samples: np.ndarray, rate: float = SAMPLE_RATE, floor: float | None = None
                ) -> tuple[list[Burst], np.ndarray, float]:
    """Every transmission in the capture, the envelope's on/off mask, and the floor."""
    floor = noise_floor(samples) if floor is None else floor
    env = envelope(samples, rate)
    on = close_gaps(env > floor * 10 ** (THRESHOLD_DB / 10), int(MERGE_GAP_US / STEP_US))
    step = int(round(rate * STEP_US / 1e6))
    found = []
    for start, end in runs(on):
        level = float(env[start:end].mean())
        found.append(Burst(start * STEP_US, (end - start) * STEP_US,
                           10 * np.log10(level / floor)))
    # The spectrum of each burst is the expensive part: only the strongest get one
    ranked = sorted(range(len(found)), key=lambda i: found[i].above_db + 3 * np.log10(found[i].length_us),
                    reverse=True)[:MAX_BURSTS]
    for i in ranked:
        burst = found[i]
        first = int(burst.start_us / STEP_US) * step
        last = first + int(burst.length_us / STEP_US) * step
        middle = samples[first:last]
        if middle.size > 16384:                     # its middle is enough for the shape
            centre = middle.size // 2
            middle = middle[centre - 8192: centre + 8192]
        shape = occupied(middle, floor, rate)
        if shape:
            burst.offset_mhz, burst.width_mhz, burst.reach_mhz = shape
    return found, on, floor


def mains_cycle(on: np.ndarray, step_us: float = STEP_US) -> float | None:
    """
    The mains period in ms (16.7 or 20) if the channel switches on and off with it the way a
    microwave oven's magnetron does, otherwise None.
    """
    per = int(100 / step_us)                       # 0.1 ms resolution is plenty
    usable = on.size // per * per
    if usable < per * 600:                         # needs 60 ms: three cycles at 50 Hz
        return None
    coarse = on[:usable].reshape(-1, per).mean(axis=1)
    duty = float(coarse.mean())
    if not 0.2 <= duty <= 0.8:
        return None
    centred = coarse - duty
    energy = float((centred ** 2).sum())
    if energy <= 0:
        return None
    for period_ms in (16.667, 20.0):
        lag = int(round(period_ms * 10))
        best = max(float((centred[:-l] * centred[l:]).sum()) / energy
                   for l in (lag - 1, lag, lag + 1))
        half = int(round(period_ms * 5))
        anti = float((centred[:-half] * centred[half:]).sum()) / energy
        if best > 0.5 and anti < -0.2:
            return period_ms
    return None


@dataclass(slots=True)
class Pulse:
    time_us: float
    width_us: float
    above_db: float


def pulse_trains(pulses: list[Pulse], min_pulses: int = 5) -> list[dict]:
    """
    Groups of short pulses spaced at one exact interval, the signature of radar. FCC test
    patterns use 150-3066 µs between pulses and ETSI's go to 5000 µs. A pulse or two missed
    still counts, since the interval then shows up doubled.
    """
    found = []
    ordered = sorted(pulses, key=lambda p: p.time_us)
    group: list[Pulse] = []

    def judge(group: list[Pulse]) -> None:
        if len(group) < min_pulses:
            return
        times = np.array([p.time_us for p in group])
        gaps = np.diff(times)
        gaps = gaps[(gaps >= 140) & (gaps <= 5100)]
        if gaps.size < min_pulses - 1:
            return
        pri = float(np.min(gaps[gaps >= np.percentile(gaps, 25)]))
        multiples = gaps / pri
        regular = np.abs(multiples - np.round(multiples)) * pri <= max(2.0, 0.01 * pri)
        if regular.sum() >= min_pulses - 1 and np.round(multiples[regular]).max() <= 4:
            exact = gaps[regular] / np.round(multiples[regular])
            found.append({
                "time_us": float(times[0]), "pulses": len(group),
                "pri_us": float(np.median(exact)),
                "width_us": float(np.median([p.width_us for p in group])),
                "above_db": float(np.median([p.above_db for p in group])),
            })

    for pulse in ordered:
        if group and pulse.time_us - group[-1].time_us > 5100:
            judge(group)
            group = []
        group.append(pulse)
    judge(group)
    return found


class PulseWatch:
    """
    Looks for radar while I/Q streams in, a chunk at a time, keeping only the pulses: a few
    minutes at 20 MS/s is far too much to hold. Transmissions longer than a radar pulse are
    only counted.
    """

    def __init__(self, rate: float = SAMPLE_RATE) -> None:
        self.rate = rate
        self.step = int(round(rate * STEP_US / 1e6))
        self.carry = np.zeros(0, dtype=np.complex64)
        self.position = 0                 # envelope steps seen so far
        self.floor: float | None = None
        self.open_at: int | None = None   # where a pulse still going at the chunk's end began
        self.open_power: list[float] = []
        self.pulses: list[Pulse] = []
        self.longer = 0
        self._last_end = -1e12            # in envelope steps
        self._pending: Pulse | None = None
        self.found = 0                    # pulses so far, including any the list let go of
        self.carry_raw = b""
        self.samples = 0                  # fed through feed_int8, and how many of them clipped
        self.clipped = 0
        self._chunks = 0
        self._picks = np.zeros((2 * self.step, 2), dtype=np.float32)     # I, Q of a step
        self._picks[0::2, 0] = 1
        self._picks[1::2, 1] = 1
        self.behind = False               # set by whoever streams it: samples were lost
        self.recording: str | None = None     # where raw I/Q of what it heard was kept

    def feed(self, samples: np.ndarray) -> None:
        samples = np.concatenate((self.carry, samples)) if self.carry.size else samples
        usable = samples.size // self.step * self.step
        self.carry = samples[usable:]
        if usable == 0:
            return
        self._floor_from(samples[:usable])
        self._feed_envelope(envelope(samples[:usable], self.rate))

    def feed_int8(self, raw: bytes) -> None:
        """
        hackrf_transfer's bytes as they arrive. Faster than ``feed``, which a stream at 20 MS/s
        needs: power comes straight from the 8-bit samples, without complex numbers, and the
        floor is worked out again only now and then. Counts clipped values as it goes.
        """
        raw = self.carry_raw + raw
        usable = len(raw) // (2 * self.step) * (2 * self.step)
        self.carry_raw = raw[usable:]
        if usable == 0:
            return
        data = np.frombuffer(raw[:usable], dtype=np.int8)
        count = usable // 2
        self.samples += count
        self.clipped += _full_scale(data)
        # Per step, the sums of I and of Q (a matrix product is the fast way) and of |x|²
        steps = data.astype(np.float32).reshape(-1, 2 * self.step)
        sums = steps @ self._picks
        dc_i, dc_q = (sums.sum(axis=0) / count).tolist()        # the receiver's DC offset
        if self.floor is None or self._chunks % FLOOR_EVERY == 0:
            part = data[: 2 * min(count, FLOOR_SAMPLES)].astype(np.float32)
            self._floor_from((((part[0::2] - dc_i) + 1j * (part[1::2] - dc_q)) / 128.0)
                             .astype(np.complex64))
        self._chunks += 1
        # Mean power in each step with DC taken out exactly: the HackRF's DC can be bigger than
        # the noise, so leaving out the cross term would turn noise into pulses
        steps *= steps
        squares = steps.sum(axis=1)
        env = (squares - 2 * (sums @ np.array([dc_i, dc_q], dtype=np.float32))
               + self.step * (dc_i * dc_i + dc_q * dc_q)) * (1 / (128.0 * 128.0 * self.step))
        env = np.maximum(env, 1e-12)
        self._feed_envelope(env)

    def _floor_from(self, samples: np.ndarray) -> None:
        quiet = noise_floor(samples)
        self.floor = quiet if self.floor is None else min(self.floor, quiet)

    @property
    def clipped_share(self) -> float:
        return min(1.0, self.clipped / self.samples) if self.samples else 0.0

    def _feed_envelope(self, env: np.ndarray) -> None:
        found = runs(env > self.floor * 10 ** (PULSE_THRESHOLD_DB / 10))
        if self.open_at is not None and not (found and found[0][0] == 0):
            # What was still on at the end of the last chunk stopped right there
            self._close(self.open_at, self.position - self.open_at, float(np.mean(self.open_power)))
            self.open_at = None
        for start, end in found:
            begins, levels = self.position + start, [float(env[start:end].mean())]
            if start == 0 and self.open_at is not None:
                begins, levels = self.open_at, self.open_power + levels    # carries on
                self.open_at = None
            if end == env.size:
                self.open_at, self.open_power = begins, levels
            else:
                self._close(begins, self.position + end - begins, float(np.mean(levels)))
        self.position += env.size

    def _close(self, start: int, length: int, level: float) -> None:
        # A short run only counts once the next one is known to start well clear of it
        isolated = (start - self._last_end) * STEP_US >= ISOLATION_US
        if self._pending is not None and isolated:
            self.pulses.append(self._pending)
            self.found += 1
            del self.pulses[:-20000]
        self._pending = None
        if length * STEP_US <= MAX_PULSE_US:
            if isolated:
                self._pending = Pulse(start * STEP_US, length * STEP_US,
                                      10 * np.log10(level / max(self.floor or 1e-12, 1e-12)))
        else:
            self.longer += 1
        self._last_end = start + length

    def trains(self) -> list[dict]:
        pulses = self.pulses + ([self._pending] if self._pending is not None else [])
        return pulse_trains(pulses)


def _near_zigbee(freq_mhz: float) -> bool:
    return any(abs(freq_mhz - centre) <= 0.6 for centre in ZIGBEE_CENTRES)


def classify(samples: np.ndarray, center_mhz: float, rate: float = SAMPLE_RATE) -> dict:
    """
    What's transmitting in this capture: a list of findings, each with what it probably is,
    where, how wide, and the share of the time it's on the air.
    """
    seconds = samples.size / rate
    bursts, on, floor = find_bursts(samples, rate)
    total_us = max(1.0, on.size * STEP_US)
    findings: list[dict] = []
    used: set[int] = set()

    def add(kind: str, members: list[int], centre: float, width: float, **extra) -> None:
        used.update(members)
        chosen = [bursts[i] for i in members]
        name, description = KINDS[kind]
        findings.append({
            "kind": kind, "name": name, "description": description,
            "center_mhz": round(centre, 2), "width_mhz": round(width, 2),
            "bursts": len(chosen),
            "airtime": min(1.0, sum(b.length_us for b in chosen) / total_us),
            "above_db": round(float(np.median([b.above_db for b in chosen])), 1) if chosen else None,
            **extra,
        })

    def freq(burst: Burst) -> float:
        return center_mhz + (burst.offset_mhz or 0.0)

    # Microwave first: its long bursts would otherwise look like several other things
    in_24 = 2400 <= center_mhz <= 2500
    period = mains_cycle(on) if in_24 else None
    if period:
        long_ones = [i for i, b in enumerate(bursts) if b.length_us >= 2000]
        if long_ones:
            widths = [bursts[i].width_mhz for i in long_ones if bursts[i].width_mhz]
            add("microwave", long_ones, center_mhz, float(np.median(widths)) if widths else 20.0,
                period_ms=round(period, 1))

    # Radar: evenly spaced pulses too short to be anything else
    def isolated(i: int) -> bool:
        before = bursts[i].start_us - (bursts[i - 1].start_us + bursts[i - 1].length_us) if i else 1e9
        after = bursts[i + 1].start_us - (bursts[i].start_us + bursts[i].length_us) \
            if i + 1 < len(bursts) else 1e9
        return before >= ISOLATION_US and after >= ISOLATION_US

    pulses = [Pulse(b.start_us, b.length_us, b.above_db) for i, b in enumerate(bursts)
              if b.length_us <= MAX_PULSE_US and i not in used and isolated(i)]
    for train in pulse_trains(pulses):
        members = [i for i, b in enumerate(bursts) if b.length_us <= MAX_PULSE_US
                   and train["time_us"] <= b.start_us <= train["time_us"] + train["pri_us"] * (train["pulses"] + 4)]
        add("radar", members, center_mhz, 0.0, pri_us=round(train["pri_us"], 1),
            pulse_us=round(train["width_us"], 1))

    shaped = [i for i, b in enumerate(bursts) if i not in used and b.width_mhz is not None]

    # Anything that never stops
    for i in shaped:
        burst = bursts[i]
        if burst.length_us >= max(0.9 * total_us, WIFI_MAX_US * 2):
            add("continuous" if burst.width_mhz < 4 else "video", [i], freq(burst), burst.width_mhz)
    shaped = [i for i in shaped if i not in used]

    # Running off the edge of the capture, well off-centre, a few MHz of it inside: the part of
    # a neighbouring Wi-Fi channel that overlaps this one. Checked first, since it's wide too.
    half = rate / 2e6
    spilling = [i for i in shaped if bursts[i].reach_mhz >= half - 2.5
                and abs(bursts[i].offset_mhz) >= 2.5 and bursts[i].width_mhz >= 2.5
                and bursts[i].length_us <= WIFI_MAX_US + 100]
    if len(spilling) >= 3:
        side = float(np.median([bursts[i].offset_mhz for i in spilling]))
        add("overlap", spilling, center_mhz + side,
            float(np.median([bursts[i].width_mhz for i in spilling])))

    # 9 MHz and up is Wi-Fi: OFDM and 802.11b frames measure 10-20 MHz over a few tens of µs,
    # while Bluetooth and Zigbee are 1-3 MHz
    wide = [i for i in shaped if i not in used and bursts[i].width_mhz >= 9
            and bursts[i].length_us <= WIFI_MAX_US + 100]
    if wide:
        add("wifi", wide, center_mhz, float(np.median([bursts[i].width_mhz for i in wide])))
    # What's left near the edges is too distorted by the filter and folding to name
    shaped = [i for i in shaped if i not in used and abs(bursts[i].offset_mhz) <= EDGE_MHZ]

    narrow = [i for i in shaped if i not in used and bursts[i].width_mhz <= 3.2]
    hopping = [i for i in narrow if bursts[i].width_mhz <= 2.0 and bursts[i].length_us <= BT_MAX_US]
    channels = {round(freq(bursts[i])) for i in hopping}
    if in_24 and len(channels) >= 4:
        add("bluetooth", hopping, center_mhz, 1.0, frequencies=len(channels))
    by_freq: dict[int, list[int]] = {}
    for i in narrow:
        if i not in used:
            by_freq.setdefault(round(freq(bursts[i])), []).append(i)
    for centre, members in sorted(by_freq.items()):
        widths = [bursts[i].width_mhz for i in members]
        exact = float(np.median([freq(bursts[i]) for i in members]))
        if in_24 and _near_zigbee(exact) and all(bursts[i].length_us <= ZIGBEE_MAX_US for i in members) \
                and 1.2 <= float(np.median(widths)) <= 3.2:
            add("zigbee", members, exact, float(np.median(widths)),
                channel=11 + ZIGBEE_CENTRES.index(min(ZIGBEE_CENTRES, key=lambda c: abs(c - exact))))
        elif len(members) >= 3:
            add("bursty", members, exact, float(np.median(widths)))

    rest = [i for i in shaped if i not in used]
    if len(rest) >= 5:
        add("bursty", rest, center_mhz + float(np.median([bursts[i].offset_mhz for i in rest])),
            float(np.median([bursts[i].width_mhz for i in rest])))

    findings = [f for f in findings if f["airtime"] >= 0.002 or f["kind"] in ("radar", "microwave")]
    findings.sort(key=lambda f: f["airtime"], reverse=True)
    return {
        "center_mhz": center_mhz,
        "seconds": round(seconds, 3),
        "airtime": float(on.mean()) if on.size else 0.0,
        "floor_dbfs": round(10 * np.log10(floor), 1),
        "bursts": len(bursts),
        "findings": findings,
    }


def describe(finding: dict) -> str:
    """One line for a finding, for status lines, tooltips and the report."""
    text = f"{finding['name']} at {finding['center_mhz']:.0f} MHz"
    if finding["kind"] == "radar":
        return f"{text}: pulses every {finding['pri_us']:.0f} µs, {finding['pulse_us']:.0f} µs long"
    if finding["kind"] == "microwave":
        return f"{text}: on and off every {finding['period_ms']:.1f} ms, {finding['airtime'] * 100:.0f}% of the time"
    if finding["kind"] == "zigbee":
        text = f"Zigbee/Thread on 802.15.4 channel {finding['channel']} ({finding['center_mhz']:.0f} MHz)"
    elif finding["kind"] == "bluetooth":
        text = f"Bluetooth hopping across {finding['frequencies']} frequencies"
    return f"{text}, on the air {finding['airtime'] * 100:.0f}% of the time"
