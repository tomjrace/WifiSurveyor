"""Wireless mic and IEM frequency coordination from a scan of the room.

Given a fine scan (peak hold) of the equipment's tuning range, it picks frequencies that are
  - inside the range and inside US spectrum mics may use (rf.bands, licensed parts only if
    you hold a Part 74 licence), and not on a TV channel that carries a station;
  - quiet: no more than a few dB over the floor anywhere within the mic's own bandwidth;
  - far enough from each other (transmitter spacing), and from every intermodulation product
    the set makes between itself, including frequencies already in use that are entered as
    fixed: two-transmitter 3rd order (2a-b), two-transmitter 5th order (3a-2b) and
    three-transmitter 3rd order (a+b-c), each with its own guard.
It's greedy, quietest first, the way coordination tools seed a plan; the spacing profiles are
typical starting values, not any manufacturer's figures, so check yours (Shure Wireless
Workbench, Sennheiser WSM) before the show.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import bands as rfbands

# (key, name, low MHz, high MHz). From Shure's published band charts; the J bands' top
# (608-616 MHz) is only usable at 614-616, which the US spectrum check takes care of.
EQUIPMENT = (
    ("uhf_all", "Whole UHF TV band (470-608 + 614-616 MHz)", 470.0, 616.0),
    ("vhf_all", "VHF TV band (174-216 MHz)", 174.0, 216.0),
    ("shure_g50", "Shure ULX-D / QLX-D G50 (470-534 MHz)", 470.0, 534.0),
    ("shure_h50", "Shure ULX-D / QLX-D H50 (534-598 MHz)", 534.0, 598.0),
    ("shure_j50a", "Shure ULX-D / QLX-D J50A (572-616 MHz)", 572.0, 616.0),
    ("shure_g58", "Shure SLX-D G58 (470-514 MHz)", 470.0, 514.0),
    ("shure_h55", "Shure SLX-D H55 (514-558 MHz)", 514.0, 558.0),
    ("shure_j52", "Shure SLX-D J52 (558-616 MHz)", 558.0, 616.0),
    ("shure_g57", "Shure Axient Digital G57 (470-608 MHz)", 470.0, 608.0),
    ("shure_x55", "Shure Axient Digital X55 (941-960 MHz)", 941.0, 960.0),
    ("ism900", "900 MHz systems (902-928 MHz)", 902.0, 928.0),
    ("duplex_gap", "600 MHz duplex gap (653-663 MHz)", 653.0, 663.0),
)
EQUIPMENT_BY_KEY = {key: (name, low, high) for key, name, low, high in EQUIPMENT}


@dataclass(frozen=True, slots=True)
class Profile:
    key: str
    name: str
    spacing_khz: float            # between any two transmitters
    imd3_khz: float               # from two-transmitter 3rd order products (2a-b)
    imd5_khz: float               # from two-transmitter 5th order products (3a-2b)
    imd3x3_khz: float             # from three-transmitter 3rd order products (a+b-c)
    bandwidth_khz: float = 200.0  # what one transmitter occupies, for the quietness check


PROFILES = (
    Profile("analog", "Analog FM mics (e.g. Shure SLX, ULX, BLX, UR)", 400, 150, 50, 50),
    Profile("digital", "Digital mics (e.g. ULX-D, QLX-D, SLX-D, Axient Digital)", 350, 50, 0, 25),
    Profile("dense", "Digital high-density mode (e.g. ULX-D, Axient HD)", 125, 25, 0, 0, 125.0),
    Profile("iem", "In-ear monitors (e.g. Shure PSM 300/900/1000)", 400, 200, 100, 100, 250.0),
)
PROFILE_BY_KEY = {p.key: p for p in PROFILES}
STEP_KHZ = 25.0
CLEAR_DB = 6.0


def legal_ranges(low: float, high: float, licensed: bool = False) -> list[tuple[float, float]]:
    """The parts of low-high where US wireless mics may go (Part 74 parts only if licensed)."""
    found = []
    for band in rfbands.BANDS:
        if not band.mics or band.key in ("dect", "ism24", "ism58"):
            continue                          # those systems pick their own channels
        if band.licence == rfbands.LICENSED and not licensed:
            continue
        a, b = max(low, band.low), min(high, band.high)
        if b - a > 0.05:
            found.append((a, b))
    return _merge(found)


def mic_spectrum_at(freq_mhz: float, licensed: bool) -> bool:
    """Whether US wireless mics may sit on this frequency (with a Part 74 licence or not)."""
    return any(a <= freq_mhz <= b for a, b in legal_ranges(freq_mhz - 1, freq_mhz + 1, licensed))


def _merge(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _cut(ranges, holes) -> list[tuple[float, float]]:
    """Ranges with the holes taken out."""
    result = list(ranges)
    for h0, h1 in holes:
        cut = []
        for a, b in result:
            if h1 <= a or h0 >= b:
                cut.append((a, b))
                continue
            if h0 > a:
                cut.append((a, h0))
            if h1 < b:
                cut.append((h1, b))
        result = cut
    return [(a, b) for a, b in result if b - a > 1e-6]


def window_max(freqs: np.ndarray, levels: np.ndarray, at: np.ndarray, half_mhz: float) -> np.ndarray:
    """The highest level within ±half_mhz of each frequency in ``at`` (or the nearest bin)."""
    left = np.searchsorted(freqs, at - half_mhz, side="left")
    right = np.searchsorted(freqs, at + half_mhz, side="right")
    out = np.empty(at.size)
    for i, (a, b) in enumerate(zip(left, right)):
        if b > a:
            out[i] = levels[a:b].max()
        else:
            nearest = min(max(0, a), freqs.size - 1)
            out[i] = levels[nearest]
    return out


def products(freqs: list[float], profile: Profile) -> list[tuple[float, float, str]]:
    """Every intermod product of a set, as (MHz, guard MHz, kind)."""
    found = []
    f = list(freqs)
    for i, a in enumerate(f):
        for j, b in enumerate(f):
            if i == j:
                continue
            if profile.imd3_khz:
                found.append((2 * a - b, profile.imd3_khz / 1000, "2-transmitter 3rd order"))
            if profile.imd5_khz:
                found.append((3 * a - 2 * b, profile.imd5_khz / 1000, "2-transmitter 5th order"))
    if profile.imd3x3_khz:
        for i in range(len(f)):
            for j in range(i + 1, len(f)):
                for k in range(len(f)):
                    if k != i and k != j:
                        found.append((f[i] + f[j] - f[k], profile.imd3x3_khz / 1000,
                                      "3-transmitter 3rd order"))
    return found


def _fits(candidate: float, chosen: list[float], profile: Profile,
          existing: list[tuple[float, float, str]]) -> bool:
    spacing = profile.spacing_khz / 1000
    if any(abs(candidate - c) < spacing - 1e-9 for c in chosen):
        return False
    if any(abs(candidate - p) < guard - 1e-9 for p, guard, _k in existing):
        return False
    # The products the candidate itself makes with the set, landing on any of them
    everyone = [*chosen, candidate]
    for p, guard, _kind in _new_products(candidate, chosen, profile):
        if any(abs(p - c) < guard - 1e-9 for c in everyone):
            return False
    return True


def _new_products(x: float, chosen: list[float], profile: Profile):
    for a in chosen:
        if profile.imd3_khz:
            yield 2 * x - a, profile.imd3_khz / 1000, "3rd"
            yield 2 * a - x, profile.imd3_khz / 1000, "3rd"
        if profile.imd5_khz:
            yield 3 * x - 2 * a, profile.imd5_khz / 1000, "5th"
            yield 3 * a - 2 * x, profile.imd5_khz / 1000, "5th"
    if profile.imd3x3_khz:
        guard = profile.imd3x3_khz / 1000
        for i, a in enumerate(chosen):
            for j, b in enumerate(chosen):
                if j > i:
                    yield a + b - x, guard, "3x3"
                if j != i:
                    yield x + a - b, guard, "3x3"


@dataclass
class Plan:
    picks: list[dict] = field(default_factory=list)     # {"mhz", "level_dbm", "over_floor_db"}
    wanted: int = 0
    floor_dbm: float | None = None
    usable: list[tuple[float, float]] = field(default_factory=list)
    stations: list[int] = field(default_factory=list)   # TV channels left out
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"{len(self.picks)} of {self.wanted} frequencies found"
        if self.stations:
            text += f", avoiding TV channel{'s' if len(self.stations) > 1 else ''} " \
                    + ", ".join(map(str, self.stations))
        return text + "."


def coordinate(freqs, levels, low: float, high: float, count: int, profile: Profile,
               fixed: list[float] | None = None, licensed: bool = False,
               clear_db: float = CLEAR_DB, step_khz: float = STEP_KHZ) -> Plan:
    """
    Up to ``count`` new frequencies (MHz) in low-high that are quiet and compatible with each
    other and with ``fixed`` (already in use), from a scan's peak-hold ``levels``.
    """
    freqs, levels = np.asarray(freqs, dtype=float), np.asarray(levels, dtype=float)
    order = np.argsort(freqs)
    freqs, levels = freqs[order], levels[order]
    plan = Plan(wanted=count)
    fixed = sorted(float(f) for f in (fixed or []))
    usable = legal_ranges(low, high, licensed)
    if not usable:
        plan.notes.append(f"Nothing in {low:.0f}-{high:.0f} MHz is open to "
                          f"{'' if licensed else 'unlicensed '}wireless mics in the US.")
        return plan
    covered = (freqs.size > 0 and freqs[0] <= low + 0.5 and freqs[-1] >= high - 0.5)
    if not covered:
        plan.notes.append("The scan doesn't cover the whole range; only what was scanned is used.")
        if freqs.size:
            usable = _cut(usable, [(-1e9, float(freqs[0])), (float(freqs[-1]), 1e9)])
    inside = np.zeros(freqs.size, dtype=bool)
    for a, b in usable:
        inside |= (freqs >= a) & (freqs <= b)
    if inside.sum() < 4:
        plan.notes.append("Too little of the range was scanned to coordinate.")
        return plan
    floor = float(np.percentile(levels[inside], 10))
    plan.floor_dbm = round(floor, 1)
    stations = [c for c in rfbands.tv_stations(freqs, levels, floor) if c["state"] == "station"]
    plan.stations = [c["channel"] for c in stations]
    usable = _cut(usable, [(c["low_mhz"], c["high_mhz"]) for c in stations])
    half = profile.bandwidth_khz / 2000
    # Keep the transmitter's whole bandwidth inside each usable stretch
    plan.usable = [(a, b) for a, b in usable if b - a >= 2 * half]
    step = step_khz / 1000
    grid = np.concatenate([np.arange(np.ceil((a + half) / step) * step, b - half + 1e-9, step)
                           for a, b in plan.usable]) if plan.usable else np.zeros(0)
    if grid.size == 0:
        plan.notes.append("No open spectrum left once TV stations are left out.")
        return plan
    noise = window_max(freqs, levels, grid, half)
    quiet = noise <= floor + clear_db
    candidates = sorted(zip(noise[quiet], np.round(grid[quiet], 3)), key=lambda c: (c[0], c[1]))
    existing = products(fixed, profile)
    chosen = list(fixed)
    for level, freq in candidates:
        if len(plan.picks) >= count:
            break
        freq = float(freq)
        if _fits(freq, chosen, profile, existing):
            chosen.append(freq)
            existing = products(chosen, profile)
            plan.picks.append({"mhz": round(freq, 3), "level_dbm": round(float(level), 1),
                               "over_floor_db": round(float(level - floor), 1)})
    plan.picks.sort(key=lambda p: p["mhz"])
    if len(plan.picks) < count:
        plan.notes.append(
            f"Only {len(plan.picks)} fit. For more: use a denser spacing profile if your system "
            "has one, spread across more than one band, or move IEMs to a different range from "
            "the mics.")
    return plan


def check(existing: list[float], freqs, levels, profile: Profile) -> list[dict]:
    """
    Frequencies already set: for each, the level heard around it, and every problem: too
    close to another, sitting on an intermod product of the others, or on a TV station.
    """
    freqs, levels = np.asarray(freqs, dtype=float), np.asarray(levels, dtype=float)
    order = np.argsort(freqs)
    freqs, levels = freqs[order], levels[order]
    floor = float(np.percentile(levels, 10)) if levels.size else None
    stations = rfbands.tv_stations(freqs, levels, floor) if floor is not None else []
    results = []
    for index, freq in enumerate(existing):
        others = [f for j, f in enumerate(existing) if j != index]
        problems = []
        close = [o for o in others if abs(o - freq) < profile.spacing_khz / 1000 - 1e-9]
        if close:
            problems.append("within " + f"{profile.spacing_khz:.0f} kHz of "
                            + ", ".join(f"{o:.3f}" for o in close))
        for p, guard, kind in products(others, profile):
            if abs(p - freq) < guard - 1e-9:
                problems.append(f"on a {kind} product ({p:.3f} MHz)")
                break
        station = next((c for c in stations if c["state"] == "station"
                        and c["low_mhz"] <= freq <= c["high_mhz"]), None)
        if station:
            problems.append(f"on TV channel {station['channel']}, which has a station")
        if not mic_spectrum_at(freq, licensed=True):
            problems.append("outside US wireless mic spectrum")
        elif not mic_spectrum_at(freq, licensed=False):
            problems.append("needs a Part 74 licence")
        level = None
        if freqs.size:
            level = float(window_max(freqs, levels, np.array([freq]), profile.bandwidth_khz / 2000)[0])
            if floor is not None and level > floor + CLEAR_DB:
                problems.append(f"{level - floor:.0f} dB of other signal on it")
        results.append({"mhz": freq, "level_dbm": None if level is None else round(level, 1),
                        "problems": problems})
    return results


def parse_frequencies(text: str) -> list[float]:
    """Frequencies typed in MHz, separated by commas, spaces or new lines."""
    found = []
    for part in text.replace(",", " ").replace(";", " ").split():
        try:
            value = float(part)
        except ValueError:
            raise ValueError(f"'{part}' isn't a frequency in MHz.") from None
        if not 1 <= value <= 8000:
            raise ValueError(f"{value:g} MHz is out of range.")
        found.append(round(value, 3))
    return found
