"""
The facility-level questions: which radios are really there, who they contend with, whether
each spot meets a requirement, how the networks are set up, and a better channel plan.

A radio is one transmitter on one channel. An access point usually broadcasts several SSIDs
from each radio, each with its own BSSID, and those BSSIDs share the last five octets or
differ only in the low bits, so they're grouped back into the radio they came from.
"""

from __future__ import annotations

from dataclasses import dataclass

CCA_DBM = -82              # a radio heard this strongly makes others on its channel wait
TU_US = 1024
MOVE_COST = 15             # what changing one radio's channel has to be worth (see channel_plan)

# ---------- radios ----------

# BSSIDs the survey shows are one radio although their addresses don't say so (some vendors
# number a radio's extra BSSIDs by flipping a bit in the middle), each to the BSSID it's grouped
# under. Set from the survey being analysed (analysis.Context): a BSSID is the same radio
# wherever it's heard.
_twins: dict[str, str] = {}


def use_twins(twins: dict[str, str]) -> None:
    global _twins
    _twins = {bssid.upper(): radio for bssid, radio in twins.items()}


def _octets(bssid: str) -> list[int]:
    try:
        return [int(part, 16) for part in bssid.split(":")]
    except ValueError:
        return []


def radio_key(network: dict) -> tuple:
    bssid = network.get("bssid") or ""
    octets = _octets(_twins.get(bssid.upper(), bssid))
    freq = round(float(network.get("freq_mhz") or 0))
    if len(octets) != 6:
        return (freq, network.get("bssid"))
    return (freq, tuple(octets[1:5]), octets[5] >> 4)


def band_of(freq: float) -> str:
    return "2.4" if freq < 3000 else "5" if freq < 5925 else "6"


def span(network: dict) -> tuple[float, float]:
    center = float(network.get("center_mhz") or network.get("freq_mhz") or 0)
    width = float(network.get("width_mhz") or 20)
    return center - width / 2, center + width / 2


def overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] - 0.5 and b[0] < a[1] - 0.5


@dataclass
class Radio:
    key: tuple
    freq: float
    center: float
    width: float
    rssi: int
    ssids: set
    bssids: set
    networks: list

    @property
    def band(self) -> str:
        return band_of(self.freq)

    @property
    def span(self) -> tuple[float, float]:
        return self.center - self.width / 2, self.center + self.width / 2


def radios(networks: list[dict]) -> list[Radio]:
    """One point's scan, as radios, strongest first."""
    grouped: dict[tuple, Radio] = {}
    for network in networks:
        if network.get("rssi") is None or not network.get("freq_mhz"):
            continue
        key = radio_key(network)
        radio = grouped.get(key)
        low, high = span(network)
        if radio is None:
            radio = grouped[key] = Radio(key, float(network["freq_mhz"]), (low + high) / 2,
                                         high - low, int(network["rssi"]), set(), set(), [])
        radio.rssi = max(radio.rssi, int(network["rssi"]))
        if high - low > radio.width:
            radio.center, radio.width = (low + high) / 2, high - low
        if network.get("ssid"):
            radio.ssids.add(network["ssid"])
        radio.bssids.add(network.get("bssid") or "")
        radio.networks.append(network)
    return sorted(grouped.values(), key=lambda r: r.rssi, reverse=True)


def _carrying(point_radios: list[Radio], ssid: str | None, band: str) -> list[Radio]:
    return [r for r in point_radios
            if (ssid is None or ssid in r.ssids) and (band == "All" or r.band == band)]


def serving(point, ssid: str | None = None, band: str = "All") -> Radio | None:
    """The radio a client here would most likely use: the strongest carrying the network."""
    found = _carrying(radios(point.networks), ssid, band)
    return found[0] if found else None


def bands(point, ssid: str | None, band: str) -> list[str]:
    """The bands to judge separately: the chosen one, or every band the network is heard on."""
    if band != "All":
        return [band]
    return sorted({r.band for r in _carrying(radios(point.networks), ssid, "All")})


def nth_strongest(point, n: int, ssid: str | None = None, band: str = "All") -> int | None:
    """
    The n-th strongest radio of the network (1 = serving) within one band, since a client roams,
    and a location engine triangulates, within a band. With every band, the best band's.
    """
    levels = []
    for each in bands(point, ssid, band):
        found = _carrying(radios(point.networks), ssid, each)
        if len(found) >= n:
            levels.append(found[n - 1].rssi)
    return max(levels) if levels else None


def least_contention(point, ssid: str | None = None, band: str = "All") -> int | None:
    """How many radios share the serving channel, in the band where that's fewest."""
    counts = [len(found) for each in bands(point, ssid, band)
              if (found := contenders(point, ssid, each)) is not None]
    return min(counts) if counts else None


def contenders(point, ssid: str | None = None, band: str = "All") -> list[Radio] | None:
    """Other radios heard above the defer threshold on any part of the serving radio's channel."""
    first = serving(point, ssid, band)
    if first is None:
        return None
    return [r for r in radios(point.networks)
            if r.key != first.key and r.rssi >= CCA_DBM and overlaps(r.span, first.span)]


# ---------- requirements ----------

@dataclass(frozen=True)
class Profile:
    name: str
    primary: float                  # serving radio at least this strong
    secondary: float | None         # a second radio this strong, so clients can roam
    third: float | None             # a third, for location
    snr: float
    max_contenders: int | None
    max_airtime: float | None


PROFILES = {
    "data": Profile("Data", -70, None, None, 20, 3, 0.5),
    "voice": Profile("Voice", -67, -67, None, 25, 2, 0.4),
    "video": Profile("Video", -65, -70, None, 25, 2, 0.4),
    "location": Profile("Location (RTLS)", -70, -75, -75, 20, None, None),
}


def evaluate(point, profile: Profile, ssid: str | None, band: str, noise, airtime
             ) -> tuple[bool | None, list[str]]:
    """
    Whether this spot meets the profile, and what fails. None when there's nothing to judge.
    ``noise`` and ``airtime`` are a band's figures, or functions of the band. With every band,
    the spot passes when any band does, since a client uses whichever band serves it best.
    """
    judged = bands(point, ssid, band)
    if not judged:
        return (False, ["the network isn't heard here"]) if point.networks else (None, [])
    results = []
    for each in judged:
        ok, failed = _evaluate_band(point, profile, ssid, each,
                                    noise(each) if callable(noise) else noise,
                                    airtime(each) if callable(airtime) else airtime)
        if ok:
            return True, []
        results.append((len(failed), each, failed))
    _count, worst_band, failed = min(results)
    prefix = f"{worst_band} GHz: " if len(judged) > 1 else ""
    return False, [prefix + failed[0]] + failed[1:] if failed else []


def _evaluate_band(point, profile: Profile, ssid: str | None, band: str, noise: float | None,
                   airtime: float | None) -> tuple[bool | None, list[str]]:
    first = serving(point, ssid, band)
    if first is None:
        return (False, ["the network isn't heard here"]) if point.networks else (None, [])
    failed = []
    if first.rssi < profile.primary:
        failed.append(f"signal {first.rssi} dBm, needs {profile.primary:.0f}")
    for n, needed, what in ((2, profile.secondary, "second"), (3, profile.third, "third")):
        if needed is None:
            continue
        level = nth_strongest(point, n, ssid, band)
        if level is None or level < needed:
            failed.append(f"{what} access point {'not heard' if level is None else f'{level} dBm'}, "
                          f"needs {needed:.0f}")
    if noise is not None and first.rssi - noise < profile.snr:
        failed.append(f"SNR {first.rssi - noise:.0f} dB, needs {profile.snr:.0f}")
    others = contenders(point, ssid, band) or []
    if profile.max_contenders is not None and len(others) > profile.max_contenders:
        failed.append(f"{len(others)} radios share the channel, at most {profile.max_contenders}")
    if profile.max_airtime is not None and airtime is not None and airtime > profile.max_airtime:
        failed.append(f"channel busy {airtime * 100:.0f}%, at most {profile.max_airtime * 100:.0f}%")
    return not failed, failed


# ---------- configuration audit ----------

@dataclass
class Finding:
    severity: str
    title: str
    detail: str


SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2, "Info": 3}


def _survey_radios(project) -> dict[tuple, dict]:
    """Every radio heard anywhere in the survey, with its networks' settings and best signal."""
    found: dict[tuple, dict] = {}
    for point in project.points:
        for radio in radios(point.networks):
            entry = found.setdefault(radio.key, {"radio": radio, "rssi": radio.rssi, "networks": {}})
            entry["rssi"] = max(entry["rssi"], radio.rssi)
            for network in radio.networks:
                entry["networks"].setdefault(network.get("bssid"), network)
    return found


def _label(radio: Radio) -> str:
    names = ", ".join(sorted(radio.ssids)) or "(hidden)"
    return f"{names} on {radio.freq:.0f} MHz"


def beacon_airtime(networks: list[dict]) -> float:
    """
    The share of a channel's time spent on beacons: each BSSID beacons every interval at its
    lowest basic rate, whether or not anyone is connected.
    """
    share = 0.0
    for network in networks:
        rates = network.get("basic_rates") or [1.0 if network.get("freq_mhz", 0) < 3000 else 6.0]
        rate = min(rates)
        preamble = 192 if rate <= 11 else 20
        frame_us = preamble + 350 * 8 / rate                 # a typical beacon, ~350 bytes
        interval = (network.get("beacon_interval") or 100) * TU_US
        share += frame_us / interval
    return share


def radar_spans(project) -> list[tuple[float, float]]:
    """
    Where the HackRF's radar watch heard radar. It watches 20 MHz at a time and can't tell
    where in that the radar sits, so the whole slice counts.
    """
    events = (getattr(project, "radar", None) or {}).get("events", [])
    return [(e["center_mhz"] - 10, e["center_mhz"] + 10) for e in events]


def audit(project) -> list[Finding]:
    found: list[Finding] = []
    heard = _survey_radios(project)
    if not heard:
        return found

    radar = radar_spans(project)
    for entry in heard.values():
        radio = entry["radio"]
        hits = [s for s in radar if overlaps(radio.span, s)]
        if hits:
            found.append(Finding(
                "High", "Radar on this radio's channel",
                f"{_label(radio)}: the radar watch heard radar pulses at "
                f"{', '.join(f'{(a + b) / 2:.0f} MHz' for a, b in hits)}. An access point on a DFS "
                "channel must leave it for 30 minutes when it hears radar, dropping its clients. "
                "Move this radio to a channel without radar (the channel plan avoids them)."))

    # Per radio
    for entry in heard.values():
        radio, networks = entry["radio"], list(entry["networks"].values())
        label = _label(radio)
        basics = [min(n["basic_rates"]) for n in networks if n.get("basic_rates")]
        if radio.band == "2.4" and basics and min(basics) < 6:
            found.append(Finding("Medium", "802.11b rates still required", f"{label}: basic rates "
                                 "from 1 Mbps. Every beacon and broadcast goes out at 1 Mbps; "
                                 "disabling 1–11 Mbps frees a lot of airtime."))
        if len(networks) > 4:
            found.append(Finding("Low", "Many SSIDs on one radio",
                                 f"{label}: {len(networks)} BSSIDs, each beaconing ten times a second."))
        if radio.band == "2.4" and radio.width > 20:
            found.append(Finding("Medium", "40 MHz on 2.4 GHz",
                                 f"{label}: uses two of the band's three clear channels."))
        if radio.band == "2.4" and round(radio.freq) not in (2412, 2437, 2462):
            found.append(Finding("Medium", "Overlapping 2.4 GHz channel",
                                 f"{label}: channels other than 1, 6 and 11 overlap two of them, so "
                                 "interference comes from both neighbours instead of sharing one."))

    # Beacon overhead on each channel, from the most BSSIDs heard above the defer level at once
    worst: dict[int, tuple[float, int]] = {}
    for point in project.points:
        by_channel: dict[int, list[dict]] = {}
        for network in point.networks:
            if (network.get("rssi") or -100) >= CCA_DBM and network.get("freq_mhz"):
                by_channel.setdefault(round(network["freq_mhz"]), []).append(network)
        for freq, networks in by_channel.items():
            share = beacon_airtime(networks)
            if share > worst.get(freq, (0, 0))[0]:
                worst[freq] = (share, len(networks))
    for freq, (share, count) in sorted(worst.items()):
        if share >= 0.10:
            severity = "Medium" if share >= 0.20 else "Low"
            found.append(Finding(severity, "Beacon overhead",
                                 f"{freq} MHz: {count} BSSIDs heard at once spend about "
                                 f"{share * 100:.0f}% of the channel's time on beacons alone. "
                                 "Fewer SSIDs, or higher basic rates, give that back."))

    # Consistency within each network, and roaming support
    by_ssid: dict[str, list[dict]] = {}
    for entry in heard.values():
        for network in entry["networks"].values():
            if network.get("ssid"):
                by_ssid.setdefault(network["ssid"], []).append(network)
    for ssid, networks in sorted(by_ssid.items()):
        radios_here = {radio_key(n) for n in networks}
        auths = {n.get("auth") for n in networks if n.get("auth")}
        if len(auths) > 1:
            found.append(Finding("Medium", "Security differs between access points",
                                 f"{ssid}: {' / '.join(sorted(auths))}. Clients may fail to roam "
                                 "between them; check for a misconfigured or rogue access point."))
        for band in ("2.4", "5", "6"):
            same_band = [n for n in networks if band_of(n.get("freq_mhz") or 0) == band]
            if len({radio_key(n) for n in same_band}) < 2:
                continue
            basics = {tuple(n.get("basic_rates") or ()) for n in same_band if n.get("basic_rates")}
            if len(basics) > 1:
                found.append(Finding("Low", "Basic rates differ between access points",
                                     f"{ssid} on {band} GHz: coverage cells end at different "
                                     "distances, which makes roaming uneven."))
            for flag, name in (("roam_k", "802.11k"), ("roam_v", "802.11v"), ("roam_r", "802.11r")):
                values = {bool(n.get(flag)) for n in same_band if flag in n}
                if len(values) > 1:
                    found.append(Finding("Low", f"{name} on some access points only",
                                         f"{ssid} on {band} GHz: roaming behaves differently "
                                         "from one access point to the next."))
        if len(radios_here) >= 2 and all(flag in n for n in networks for flag in ("roam_k",)):
            if not any(n.get("roam_k") or n.get("roam_v") for n in networks):
                found.append(Finding("Low", "No roaming assistance",
                                     f"{ssid}: {len(radios_here)} radios but neither 802.11k nor "
                                     "802.11v, so clients choose when to move on their own."))
        intervals = {n.get("beacon_interval") for n in networks if n.get("beacon_interval")}
        if intervals - {100}:
            found.append(Finding("Info", "Unusual beacon interval",
                                 f"{ssid}: {', '.join(f'{i} TU' for i in sorted(intervals))}."))
    return sorted(found, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))


# ---------- channel plan ----------

def _freq(channel: int, band: str) -> float:
    return {"2.4": 2407, "5": 5000, "6": 5950}[band] + 5 * channel


def candidates(band: str, width: float, allow_dfs: bool) -> list[tuple[float, float, str]]:
    """Channel blocks a radio could use: (centre, width, name)."""
    width = int(width)
    if band == "2.4":
        return [(_freq(n, band), 20, f"ch {n}") for n in (1, 6, 11)]
    if band == "5":
        blocks = {
            20: [36, 40, 44, 48, 149, 153, 157, 161, 165],
            40: [38, 46, 151, 159],
            80: [42, 155],
            160: [50],
        }
        dfs = {
            20: [52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144],
            40: [54, 62, 102, 110, 118, 126, 134, 142],
            80: [58, 106, 122, 138],
            160: [114],
        }
        centres = blocks.get(width, blocks[20]) + (dfs.get(width, []) if allow_dfs else [])
        if width == 160 and not allow_dfs:
            centres = []                      # every 160 MHz block in 5 GHz includes DFS
        if width not in blocks:
            width = 20
    else:
        first = {20: 1, 40: 3, 80: 7, 160: 15}.get(width, 1)
        step = {20: 4, 40: 8, 80: 16, 160: 32}.get(width, 4)
        if width not in (20, 40, 80, 160):
            width = 20
        centres = list(range(first, 234, step))
    result = []
    for centre in centres:
        low_channel = centre - (width // 10) + 2 if width > 20 else centre
        high_channel = centre + (width // 10) - 2 if width > 20 else centre
        name = f"ch {centre}" if width == 20 else f"ch {low_channel}–{high_channel} ({width} MHz)"
        result.append((_freq(centre, band), width, name))
    return result


@dataclass
class Suggestion:
    radio: Radio
    current: str
    suggested: str
    changed: bool
    shared_before: int          # of this network's other radios it contends with, before
    shared_after: int
    clashes_before: int = 0     # points hearing it and one of those on its channel, before
    clashes_after: int = 0


def _block_name(radio: Radio) -> str:
    for centre, width, name in candidates(radio.band, radio.width, True):
        if abs(centre - radio.center) < 1 and width == radio.width:
            return name
    return f"{radio.center:.0f} MHz ({radio.width:.0f} MHz)"


def channel_plan(project, ssid: str, allow_dfs: bool = False) -> list[Suggestion]:
    """
    Channels for the network's own radios that keep them apart from each other first, and from
    everyone else's second, changing as few as possible. Two of its radios conflict when a point
    hears both above the defer level; other networks count against a channel where the radio's
    own coverage overlaps them.
    """
    own: dict[tuple, Radio] = {}
    heard_at: dict[tuple, set[int]] = {}
    foreign_at: list[list[Radio]] = []
    for index, point in enumerate(project.points):
        point_radios = radios(point.networks)
        foreign_at.append([r for r in point_radios if ssid not in r.ssids and r.rssi >= CCA_DBM])
        for radio in point_radios:
            if ssid in radio.ssids:
                own.setdefault(radio.key, radio)
                if radio.rssi >= CCA_DBM:
                    heard_at.setdefault(radio.key, set()).add(index)
    keys = list(own)
    conflict = {(a, b): len(heard_at.get(a, set()) & heard_at.get(b, set()))
                for a in keys for b in keys if a != b and own[a].band == own[b].band}

    def foreign_cost(key, block: tuple[float, float]) -> int:
        return sum(1 for index in heard_at.get(key, ()) for other in foreign_at[index]
                   if overlaps(other.span, block))

    radar = radar_spans(project)
    options = {key: candidates(own[key].band, own[key].width, allow_dfs)
               or [(own[key].center, own[key].width, _block_name(own[key]))] for key in keys}

    def span_of(option) -> tuple[float, float]:
        return option[0] - option[1] / 2, option[0] + option[1] / 2

    def unmoved(key, option) -> bool:
        return abs(option[0] - own[key].center) < 1 and option[1] == own[key].width

    def cost(key, option, chosen) -> float:
        """What one radio on one channel costs, given everyone else's, plus the move itself."""
        centre, width, _name = option
        block = (centre - width / 2, centre + width / 2)
        total = 10 * sum(conflict.get((key, other), 0) for other, (c, w, _n) in chosen.items()
                         if other != key and overlaps(block, (c - w / 2, c + w / 2)))
        total += foreign_cost(key, block)
        # Radar heard there: the radio would be forced off it, so it's a last resort
        total += 1000 if any(overlaps(block, heard) for heard in radar) else 0
        # Every change is work for someone and a moment off the air for its clients, so a
        # move has to clear about one and a half points of clash with the network's own radios
        return total + (0 if unmoved(key, option) else MOVE_COST)

    def settle(chosen) -> dict:
        """The single change that helps most, again and again, until none helps."""
        while True:
            best = None
            for key in keys:
                here = cost(key, chosen[key], chosen)
                for option in options[key]:
                    gain = here - cost(key, option, chosen)
                    if gain > 0.5 and (best is None or gain > best[0]):
                        best = (gain, key, option)
            if best is None:
                return chosen
            chosen[best[1]] = best[2]

    def total(chosen) -> float:
        # Each radio's cost counts its clashes with the network's own radios; halve those,
        # since each is counted from both sides
        clashes = sum(10 * sum(conflict.get((key, other), 0) for other in keys
                               if other != key and overlaps(span_of(chosen[key]),
                                                            span_of(chosen[other])))
                      for key in keys)
        return sum(cost(key, chosen[key], chosen) for key in keys) - clashes / 2

    # From the channels as they are, and from a fresh plan built radio by radio (which finds
    # swaps no single change can), each settled; the cheaper wins, fewer moves on a tie
    current = {key: next((o for o in options[key] if unmoved(key, o)),
                         (own[key].center, own[key].width, _block_name(own[key])))
               for key in keys}
    fresh: dict[tuple, tuple[float, float, str]] = {}
    for key in sorted(keys, key=lambda k: -sum(v for (a, _b), v in conflict.items() if a == k)):
        fresh[key] = min(options[key], key=lambda o: cost(key, o, fresh))
    tried = [settle(dict(current)), settle(fresh)]
    chosen = min(tried, key=lambda c: (total(c), sum(not unmoved(k, c[k]) for k in keys)))

    def clashing(key, block_of) -> list[int]:
        """For each of the network's other radios on its channel, how many points hear both."""
        centre, width = block_of(key)
        return [conflict[(key, other)] for other in keys if other != key
                and conflict.get((key, other), 0)
                and overlaps((centre - width / 2, centre + width / 2),
                             (lambda c, w: (c - w / 2, c + w / 2))(*block_of(other)))]

    def now(k):
        return own[k].center, own[k].width

    def after(k):
        return chosen[k][0], chosen[k][1]

    suggestions = []
    for key in keys:
        radio = own[key]
        centre, width, name = chosen[key]
        suggestions.append(Suggestion(
            radio, _block_name(radio), name, abs(centre - radio.center) >= 1 or width != radio.width,
            len(clashing(key, now)), len(clashing(key, after)),
            sum(clashing(key, now)), sum(clashing(key, after))))
    return sorted(suggestions, key=lambda s: (s.radio.band, s.radio.freq))


def plans(project, names: list[str], allow_dfs: bool = False) -> list[tuple[str, list[str], list[Suggestion]]]:
    """
    A channel plan for each of ``names``, as (network, the others it shares its radios with,
    plan). A network whose radios are mostly another's already planned isn't planned again: one
    radio can only be on one channel.
    """
    found: list[tuple[str, list[str], list[Suggestion]]] = []
    planned: list[set] = []
    for name in names:
        plan = channel_plan(project, name, allow_dfs)
        keys = {item.radio.key for item in plan}
        if not keys:
            continue
        index = next((i for i, seen in enumerate(planned)
                      if len(keys & seen) >= 0.75 * min(len(keys), len(seen))), None)
        if index is None:
            found.append((name, [], plan))
            planned.append(keys)
        else:
            found[index][1].append(name)
    return found
