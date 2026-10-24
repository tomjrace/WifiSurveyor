"""The spectrum ranges to measure: one choice, made on the Spectrum page or in the survey setup.

The Spectrum page offers ranges in sets (Wi-Fi, each group of rf.bands, cellular), and what's
ticked there is what each survey point sweeps. The survey setup edits the same choice in its
own terms: Wi-Fi ranges by number, bands by key, band groups, and any other ranges (the wide
overviews, cellular downlinks) as they are. Kept on this computer (settings), and with each
survey in its setup, so opening a survey ticks its ranges again.
"""

from __future__ import annotations

from . import settings
from ..rf import bands as rfbands
from ..rf import cellular
from ..rf.analyzer import WIFI_RANGES

# The Wi-Fi ranges to choose from: the four the analyzers sweep, 6 GHz as far as the US band
# goes (cut to what the analyzer reaches when it's swept)
RANGES = [*WIFI_RANGES[:3], ("6 GHz", WIFI_RANGES[3][1], 7125.0)]
MIN_SPAN_MHZ = 5.0
# The band sets the Spectrum page offers beyond Wi-Fi: a wide look first where one helps, then
# every band in the set (rf.bands), each a range of its own
OVERVIEWS = {
    "mics": [("UHF mics overview, 470-663 MHz", 470.0, 663.0)],
    "subghz": [("Remotes and sensors, 300-440 MHz", 300.0, 440.0)],
    "business": [("VHF two-way, 136-174 MHz", 136.0, 174.0), ("UHF two-way, 450-470 MHz", 450.0, 470.0)],
    "venue": [],
    "production": [],
}
WIFI_SET = "Wi-Fi"
CELLULAR_SET = "Cellular (US downlinks)"


def band_sets() -> list[tuple[str, list[tuple[str, float, float]]]]:
    """(set name, its ranges): Wi-Fi, then each group of rf.bands, then cellular."""
    sets = [(WIFI_SET, list(RANGES))]
    for key, name, _about in rfbands.GROUPS:
        ranges = list(OVERVIEWS.get(key, []))
        ranges += [band_range(b) for b in rfbands.in_group(key)]
        sets.append((name, ranges))
    sets.append((CELLULAR_SET, [(name, low, high) for _k, name, low, high in cellular.BANDS]))
    return sets


def band_range(band) -> tuple[str, float, float]:
    """A band as the Spectrum page lists it: at least MIN_SPAN_MHZ wide, so it can be seen."""
    return band.name, band.low, max(band.high, band.low + MIN_SPAN_MHZ)


def range_key(entry: tuple[str, float, float]) -> tuple[float, float]:
    """A range's identity, whichever set it's listed in."""
    return round(entry[1], 3), round(entry[2], 3)


def all_ranges() -> dict[tuple[float, float], tuple[str, float, float]]:
    found = {}
    for _set, ranges in band_sets():
        for entry in ranges:
            found.setdefault(range_key(entry), entry)
    return found


# ---------- what's ticked ----------

def chosen_sets() -> list[str]:
    names = [name for name, _r in band_sets()]
    saved = [n for n in (settings.get("spectrum_sets") or [WIFI_SET]) if n in names]
    return saved or [WIFI_SET]


def chosen_keys() -> list[tuple[float, float]]:
    """The ticked ranges' keys. The four Wi-Fi ranges until something else is chosen."""
    saved = settings.get("spectrum_ranges")
    if saved is None:
        return [range_key(r) for r in RANGES]
    known = all_ranges()
    return [tuple(k) for k in saved if tuple(k) in known]


def chosen() -> list[tuple[str, float, float]]:
    known = all_ranges()
    return [known[k] for k in chosen_keys()]


def choose(sets: list[str], keys) -> None:
    settings.put("spectrum_sets", list(sets))
    settings.put("spectrum_ranges", [list(k) for k in keys])


# ---------- in the survey setup's terms ----------

def _band_keys() -> dict[tuple[float, float], str]:
    return {range_key(band_range(b)): b.key for b in rfbands.BANDS}


def split(entries) -> tuple[list[int], list[str], list[list]]:
    """(Wi-Fi range numbers, rf.bands keys, any other ranges as [name, low, high])."""
    wifi_keys = [range_key(r) for r in RANGES]
    bands = _band_keys()
    wifi, keys, extras = [], [], []
    for entry in entries:
        key = range_key(entry)
        if key in wifi_keys:
            wifi.append(wifi_keys.index(key))
        elif key in bands:
            keys.append(bands[key])
        else:
            extras.append([entry[0], entry[1], entry[2]])
    return sorted(set(wifi)), keys, extras


def setup_choice() -> tuple[list[int], list[str], list[list], list[str]]:
    """What's ticked as the survey setup holds it: Wi-Fi, bands, other ranges, band groups."""
    wifi, keys, extras = split(chosen())
    ticked = set(chosen_sets())
    groups = [key for key, name, _a in rfbands.GROUPS if name in ticked]
    return wifi, keys, extras, groups


def choose_for_setup(wifi: list[int], bands: list[str], extras: list, groups: list[str]) -> None:
    """Ticks what a survey setup measures, with the sets that list it."""
    keys = [range_key(RANGES[i]) for i in sorted(set(wifi)) if 0 <= i < len(RANGES)]
    keys += [range_key(band_range(rfbands.BY_KEY[k])) for k in bands if k in rfbands.BY_KEY]
    keys += [range_key(e) for e in extras]
    names = {key: name for key, name, _a in rfbands.GROUPS}
    sets = [WIFI_SET] if wifi else []
    sets += [names[g] for g in groups if g in names]
    # A range no ticked set lists brings in the first set that does. Only then: a band listed
    # in two groups mustn't tick the other group as well
    listed = {name: {range_key(r) for r in ranges} for name, ranges in band_sets()}
    for key in keys:
        if not any(key in listed[name] for name in sets):
            sets.append(next(name for name, members in listed.items() if key in members))
    choose(sets, keys)
