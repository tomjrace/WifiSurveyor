"""The link test: a generator sends at each frequency in turn while an analyzer here measures it.

The generator is a tinySA, either plugged into this tablet or on a satellite (a Pi on the
LAN). The receiver is any other analyzer here that reaches the frequency: a tinySA, an RF
Explorer, a HackRF or an RTL-SDR. For each step the generator is set, given a moment to
settle, and the receiver parks on that frequency (a narrow dwell) and reads the carrier.
What it gives per frequency: the level heard, the floor beside it, the margin between them,
and the path loss (what was sent minus what was heard).

Over the air it's limited to rf.txrules' ISM bands; down a cable (generator into the
receiver through an attenuator) any frequency both reach, which is how a receiver or an
analyzer's calibration is checked across a band.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import spectrum as spec
from . import txrules

SETTLE_S = 0.6
DWELL_S = 1.2
DWELL_WIDTH_MHZ = 1.0


class LocalGenerator:
    """A tinySA plugged into this tablet, as a generator (it can't measure meanwhile)."""

    def __init__(self, tinysa) -> None:
        self.device = tinysa
        self.name = f"{getattr(tinysa, 'name', 'tinySA')} here"

    @property
    def top_mhz(self) -> float:
        return self.device.output_top_mhz

    def start(self, freq_mhz: float, level_dbm: float, cabled: bool) -> None:
        trouble = txrules.check(freq_mhz, level_dbm, cabled)
        if trouble:
            raise PermissionError(trouble)
        self.device.generate(freq_mhz, level_dbm)

    def stop(self) -> None:
        self.device.stop_generating()


class RemoteGenerator:
    """A satellite's tinySA (net.satellite.Satellite)."""

    def __init__(self, satellite, top_mhz: float = 4400.0) -> None:
        self.satellite = satellite
        self.name = satellite.name
        self.top_mhz = top_mhz

    def start(self, freq_mhz: float, level_dbm: float, cabled: bool) -> None:
        self.satellite.tone(freq_mhz, level_dbm, cabled, minutes=5)

    def stop(self) -> None:
        self.satellite.stop()


@dataclass
class Step:
    freq_mhz: float
    sent_dbm: float
    heard_dbm: float | None = None
    floor_dbm: float | None = None
    error: str = ""

    @property
    def margin_db(self) -> float | None:
        if self.heard_dbm is None or self.floor_dbm is None:
            return None
        return self.heard_dbm - self.floor_dbm

    @property
    def loss_db(self) -> float | None:
        return None if self.heard_dbm is None else self.sent_dbm - self.heard_dbm


@dataclass
class Result:
    generator: str
    receiver: str
    cabled: bool
    steps: list[Step] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        heard = [s for s in self.steps if s.heard_dbm is not None]
        if not self.steps:
            return self.error or "Nothing measured."
        text = f"Heard {len(heard)} of {len(self.steps)} frequencies"
        if heard:
            losses = [s.loss_db for s in heard]
            worst = max(heard, key=lambda s: s.loss_db)
            text += (f": path loss {min(losses):.0f}-{max(losses):.0f} dB, worst at "
                     f"{worst.freq_mhz:.3f} MHz")
        missed = [s for s in self.steps if s.heard_dbm is None and not s.error]
        if missed:
            text += (f". Not heard at {', '.join(f'{s.freq_mhz:.3f}' for s in missed[:5])} MHz: "
                     "move closer, raise the level, or check the antennas")
        if self.error:
            text += f". Stopped early: {self.error}"
        return text + "."


def frequencies(low_mhz: float, high_mhz: float, count: int) -> list[float]:
    """``count`` frequencies spread evenly from low to high, 1 kHz steps."""
    if count <= 1:
        return [round((low_mhz + high_mhz) / 2, 3)]
    step = (high_mhz - low_mhz) / (count - 1)
    return [round(low_mhz + i * step, 3) for i in range(count)]


def run(generator, receiver, freqs: list[float], level_dbm: float, cabled: bool,
        stop: threading.Event | None = None, progress=lambda step, index, total: None) -> Result:
    """Each frequency in turn: send, settle, listen. The generator is always stopped after."""
    result = Result(generator.name, getattr(receiver, "model", "") or receiver.name, cabled)
    for freq in freqs:
        trouble = txrules.check(freq, level_dbm, cabled)
        if trouble:
            result.error = trouble
            return result
        if not getattr(receiver, "min_mhz", 0.0) <= freq <= receiver.max_mhz:
            result.error = f"{result.receiver} doesn't reach {freq:.3f} MHz."
            return result
    try:
        for index, freq in enumerate(freqs):
            if stop is not None and stop.is_set():
                break
            step = Step(freq, level_dbm)
            result.steps.append(step)
            try:
                generator.start(freq, level_dbm, cabled)
                time.sleep(SETTLE_S)
                config, sweeps = receiver.dwell(freq, getattr(receiver, "dwell_seconds", DWELL_S),
                                                 width_mhz=DWELL_WIDTH_MHZ)
                found = spec.tone_level(config, sweeps, freq)
                step.heard_dbm, step.floor_dbm = found["dbm"], found["floor_dbm"]
            except PermissionError as error:
                result.error = str(error)
                break
            except Exception as error:
                step.error = str(error)
                if "isn't answering" in step.error or "stopped answering" in step.error:
                    result.error = step.error
                    break
            progress(step, index, len(freqs))
    finally:
        try:
            generator.stop()
        except Exception as error:
            result.error = result.error or f"couldn't stop the generator: {error}"
    return result
