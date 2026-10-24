"""The radio bands beyond Wi-Fi that a site might care about: production, industrial, business.

Each band is a US allocation with what uses it there, grouped the way a survey would ask for
them: wireless mics and in-ear monitors, the rest of a production (intercom, DMX, video,
cue), sub-GHz industrial and IoT, business radio, and what a venue shares the air with. A band
that serves several groups (900 MHz ISM, DECT, 2.4 GHz) is listed once and belongs to each.

What's measured in a band is energy, not decoded signals: the strongest bin, the floor, and how
much of the band stands well above that floor. For the UHF and VHF TV bands, it's also which
6 MHz TV channels carry a station, since that decides where mics can go.

Wireless mic rules are the FCC's since the 600 MHz repack (July 2020): mics may use vacant TV
channels 470-608 MHz (50 mW unlicensed), the 614-616 MHz guard band and 657-663 MHz of the
duplex gap (20 mW unlicensed), and 653-657 MHz only with a Part 74 licence. Channel 37
(608-614 MHz) is radio astronomy and medical telemetry, and 616-653 / 663-698 MHz now belong to
T-Mobile's 600 MHz network: mics there are no longer legal.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


GROUPS = (
    ("mics", "Wireless mics & IEMs",
     "UHF and VHF TV bands, the 600 MHz guard band and duplex gap, 900 MHz, 941-960 MHz, "
     "1.4 GHz, DECT, 2.4 and 5.8 GHz: everywhere US wireless mics and in-ear monitors work."),
    ("production", "Production: intercom, DMX, video, cue",
     "Wireless intercom (DECT, 900 MHz, 2.4 GHz), wireless DMX, video links, IFB and "
     "production two-way radios."),
    ("subghz", "Sub-GHz & industrial",
     "Car and garage remotes, security sensors, 433 MHz devices, 900 MHz ISM (LoRaWAN, Z-Wave, "
     "RFID, SCADA radios, meters), licensed SCADA and the industrial 2.4 GHz protocols."),
    ("business", "Business radio & paging",
     "VHF and UHF two-way radio, MURS, FRS/GMRS, 800 MHz public safety and SMR, 900 MHz "
     "private LTE, paging."),
    ("venue", "Venue & public services",
     "Assistive listening (ADA), FM and TV broadcast, aviation, NOAA weather, hospital "
     "medical telemetry."),
)
GROUP_NAMES = {key: name for key, name, _about in GROUPS}

# What the licence situation means for someone planning equipment in the band
UNLICENSED, LICENSED, AVOID, LISTEN = "unlicensed", "licensed", "avoid", "listen"


@dataclass(frozen=True, slots=True)
class Band:
    key: str
    name: str
    low: float                    # MHz
    high: float
    groups: tuple[str, ...]
    uses: str = ""                # what transmits there
    licence: str = UNLICENSED     # UNLICENSED, LICENSED, AVOID, or LISTEN (receive-only interest)
    rule: str = ""                # the rule, in a few words
    tv: bool = False              # split into 6 MHz TV channels and look for stations
    mics: bool = False            # wireless mics may be placed here (coordination uses it)

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def centre(self) -> float:
        return (self.low + self.high) / 2


BANDS = (
    # ----- venue and broadcast, lowest first -----
    Band("als72", "Assistive listening 72-76 MHz", 72.0, 76.0, ("venue",),
         "ADA assistive listening receivers (Listen, Williams AV)", UNLICENSED,
         "Part 15.237"),
    Band("fm", "FM broadcast 88-108 MHz", 88.0, 108.0, ("venue",),
         "FM radio stations: strong ones can overload receivers nearby", LISTEN),
    Band("air", "Aviation voice 118-137 MHz", 118.0, 137.0, ("venue",),
         "Airport tower and air traffic control", AVOID, "Restricted: never transmit here"),
    # ----- business and two-way radio -----
    Band("vhf_biz", "VHF business radio 150-174 MHz", 150.0, 174.0, ("business", "production"),
         "Two-way radios, marine, railroads, public works", LICENSED, "Part 90"),
    Band("murs", "MURS 151.8-154.6 MHz", 151.8, 154.65, ("business",),
         "Licence-free VHF radios, wireless call boxes, drive-thru headsets", UNLICENSED,
         "Part 95J, 2 W"),
    Band("noaa", "NOAA weather 162.40-162.55 MHz", 162.39, 162.56, ("venue",),
         "NOAA Weather Radio (evacuation alerts)", LISTEN),
    Band("vhf169", "169-172 MHz licensed mics", 169.0, 172.0, ("mics",),
         "Part 74 wireless mics (shared with business radio)", LICENSED, "Part 74",
         mics=True),
    Band("tv_vhf", "VHF TV channels 7-13 (174-216 MHz)", 174.0, 216.0, ("mics", "venue"),
         "VHF TV stations; VHF mics on the vacant channels", UNLICENSED,
         "Vacant TV channels, 50 mW", tv=True, mics=True),
    Band("als216", "Assistive listening 216-217 MHz", 216.0, 217.0, ("venue",),
         "ADA assistive listening and hearing loops' radio links", UNLICENSED,
         "Part 15.237 / Part 95"),
    Band("tel217", "217-220 MHz telemetry", 217.0, 220.0, ("subghz",),
         "Industrial telemetry and AMTS", LICENSED, "Part 80/90"),
    Band("tpms315", "315 MHz remotes and TPMS", 314.0, 316.0, ("subghz",),
         "Car key fobs, tyre pressure sensors, garage doors", UNLICENSED, "Part 15.231"),
    Band("sec319", "319.5 MHz security sensors", 319.0, 320.0, ("subghz",),
         "Interlogix / GE wireless alarm sensors", UNLICENSED, "Part 15.231"),
    Band("sec345", "345 MHz security sensors", 344.5, 345.5, ("subghz",),
         "Honeywell / 2GIG wireless alarm sensors", UNLICENSED, "Part 15.231"),
    Band("garage390", "390 MHz garage remotes", 389.5, 390.5, ("subghz",),
         "Garage and gate openers", UNLICENSED, "Part 15.231"),
    Band("ism433", "433.92 MHz devices", 433.05, 434.79, ("subghz",),
         "Remotes, weather stations, sensors, cheap telemetry", UNLICENSED, "Part 15.231"),
    Band("rpu450", "450-451 MHz IFB and cue", 450.0, 451.0, ("production",),
         "Broadcast remote pickup: IFB, cue and talent earpieces", LICENSED, "Part 74 subpart D"),
    Band("uhf_biz", "UHF business radio 451-470 MHz", 451.0, 470.0, ("business", "production"),
         "Two-way radios (Motorola, Kenwood), production walkie-talkies, SCADA", LICENSED,
         "Part 90"),
    Band("rpu455", "455-456 MHz IFB and cue", 455.0, 456.0, ("production",),
         "Broadcast remote pickup: IFB and cue", LICENSED, "Part 74 subpart D"),
    Band("frs462", "FRS/GMRS 462 MHz", 462.54, 462.73, ("business",),
         "Licence-free walkie-talkies (FRS) and GMRS", UNLICENSED, "Part 95"),
    Band("frs467", "FRS/GMRS 467 MHz", 467.54, 467.73, ("business",),
         "FRS walkie-talkies and GMRS repeater inputs", UNLICENSED, "Part 95"),
    # ----- the UHF TV band and the 600 MHz leftovers: where most mics and IEMs live -----
    Band("tv_uhf", "UHF TV channels 14-36 (470-608 MHz)", 470.0, 608.0, ("mics", "venue"),
         "TV stations; UHF wireless mics and in-ear monitors on the vacant channels "
         "(Shure G50/H50/J50A, G58/H55/J52, G57, Sennheiser, Audio-Technica)", UNLICENSED,
         "Vacant TV channels, 50 mW (licensed users have priority)", tv=True, mics=True),
    Band("ch37", "TV channel 37 (608-614 MHz)", 608.0, 614.0, ("mics", "venue"),
         "Radio astronomy and hospital medical telemetry (WMTS)", AVOID,
         "No mics, ever"),
    Band("guard600", "600 MHz guard band (614-616 MHz)", 614.0, 616.0, ("mics",),
         "Unlicensed wireless mics", UNLICENSED, "20 mW", mics=True),
    Band("dl600", "600 MHz downlink (617-652 MHz)", 617.0, 652.0, ("mics",),
         "T-Mobile band 71: mics here have been illegal since July 2020", AVOID,
         "No mics since 2020"),
    Band("gap_lic", "Duplex gap 653-657 MHz (licensed)", 653.0, 657.0, ("mics",),
         "Part 74 licensed wireless mics only", LICENSED, "Part 74, 20 mW", mics=True),
    Band("gap_unlic", "Duplex gap 657-663 MHz", 657.0, 663.0, ("mics",),
         "Unlicensed wireless mics", UNLICENSED, "20 mW", mics=True),
    # ----- 800 and 900 MHz -----
    Band("ps800", "800 MHz public safety and SMR (851-869 MHz)", 851.0, 869.0, ("business",),
         "Police, fire and EMS trunked radio; building DAS / BDA systems", LICENSED,
         "Part 90"),
    Band("pltel_ul", "900 MHz private LTE uplink (896-901 MHz)", 896.0, 901.0, ("business",),
         "Anterix band 106 for utilities, and narrowband business radio", LICENSED, "Part 27/90"),
    Band("ism900", "900 MHz ISM (902-928 MHz)", 902.0, 928.0,
         ("mics", "production", "subghz"),
         "LoRaWAN US915, Z-Wave, Zigbee 915, Wi-Fi HaLow, UHF RFID (RAIN), SCADA radios, "
         "smart meters; 900 MHz mics (Shure X52), IEMs, intercom and wireless DMX",
         UNLICENSED, "Part 15.247 / 15.249", mics=True),
    Band("zwave", "Z-Wave 908.4 and 916 MHz", 908.0, 916.4, ("subghz",),
         "Z-Wave home and building automation (908.42 and 916 MHz)", UNLICENSED, "Part 15.249"),
    Band("mas928", "928-929 MHz licensed SCADA", 928.0, 929.0, ("subghz",),
         "Multiple Address Systems: utility and pipeline SCADA", LICENSED, "Part 101"),
    Band("paging", "929-932 MHz paging", 929.0, 932.0, ("business",),
         "Pagers (hospitals, restaurants, on-call staff)", LICENSED, "Part 22/90"),
    Band("pltel_dl", "900 MHz private LTE downlink (935-940 MHz)", 935.0, 940.0, ("business",),
         "Anterix band 106 for utilities, and narrowband business radio", LICENSED, "Part 27/90"),
    Band("mic941", "941-960 MHz (licensed mics, STL)", 941.0, 960.0, ("mics", "subghz"),
         "Part 74 mics and IEMs (Shure X55), studio-transmitter links, licensed SCADA",
         LICENSED, "Part 74 / 101, coordinate first", mics=True),
    # ----- above 1 GHz -----
    Band("wmts1395", "Medical telemetry 1395-1400 MHz", 1395.0, 1400.0, ("venue",),
         "Hospital patient monitors (WMTS)", AVOID, "Protected: medical"),
    Band("wmts1427", "Medical telemetry 1427-1432 MHz", 1427.0, 1432.0, ("venue",),
         "Hospital patient monitors (WMTS)", AVOID, "Protected: medical"),
    Band("mic1435", "1435-1525 MHz licensed mics", 1435.0, 1525.0, ("mics",),
         "Part 74 wireless mics, shared with aeronautical flight-test telemetry", LICENSED,
         "Part 74, coordinate with AFTRCC", mics=True),
    Band("dect", "DECT 6.0 (1920-1930 MHz)", 1920.0, 1930.0,
         ("mics", "production", "business"),
         "Cordless phones, Shure Microflex Wireless, Sennheiser SpeechLine, Clear-Com "
         "FreeSpeak II and Riedel Bolero intercom", UNLICENSED, "Part 15 subpart D",
         mics=True),
    Band("ism24", "2.4 GHz ISM (2400-2483.5 MHz)", 2400.0, 2483.5,
         ("mics", "production", "subghz"),
         "Wi-Fi and Bluetooth, plus Shure GLX-D, Line 6 Relay, Sennheiser XSW-D, Rode and DJI "
         "mics, LumenRadio CRMX and W-DMX, HME intercom, WirelessHART, ISA100.11a, Zigbee",
         UNLICENSED, "Part 15.247", mics=True),
    Band("video5", "5 GHz video links (5150-5850 MHz)", 5150.0, 5850.0, ("production",),
         "Teradek, Hollyland and Accsoon video, Clear-Com FreeSpeak Edge, sharing with "
         "Wi-Fi", UNLICENSED, "Part 15.407"),
    Band("ism58", "5.8 GHz ISM (5725-5850 MHz)", 5725.0, 5850.0, ("mics", "production"),
         "Shure GLX-D+ (with 2.4 GHz), wireless DMX, analog video and FPV, Wi-Fi channels "
         "149-165", UNLICENSED, "Part 15.247", mics=True),
    Band("mic7g", "6875-7125 MHz licensed mics", 6875.0, 7125.0, ("mics",),
         "Part 74 wireless mics and IEMs (shares with broadcast links and 6 GHz Wi-Fi)",
         LICENSED, "Part 74", mics=True),
)
BY_KEY = {band.key: band for band in BANDS}


def in_group(group: str) -> list[Band]:
    return [band for band in BANDS if group in band.groups]


def for_groups(groups) -> list[Band]:
    """Every band in any of these groups, once each, lowest first."""
    chosen = set(groups)
    return sorted((b for b in BANDS if chosen & set(b.groups)), key=lambda b: (b.low, b.high))


def within(low: float, high: float) -> list[Band]:
    """Bands that overlap low-high, to label on a spectrum plot."""
    return [b for b in BANDS if b.high > low and b.low < high]


def reachable(bands, low_mhz: float, high_mhz: float) -> list[Band]:
    return [b for b in bands if b.low >= low_mhz - 0.5 and b.high <= high_mhz + 0.5]


# ---------- TV channels ----------

def tv_channels(low: float, high: float) -> list[tuple[int, float, float]]:
    """US TV channels inside low-high: (number, lower edge, upper edge)."""
    found = []
    for number in range(7, 14):                      # VHF high, 174-216 MHz
        edge = 174.0 + 6 * (number - 7)
        found.append((number, edge, edge + 6))
    for number in range(14, 37):                     # UHF since the repack, 470-608 MHz
        edge = 470.0 + 6 * (number - 14)
        found.append((number, edge, edge + 6))
    found.append((37, 608.0, 614.0))
    return [c for c in found if c[1] >= low - 0.01 and c[2] <= high + 0.01]


# A DTV station fills its channel: this far above the floor across most of it
TV_ABOVE_DB = 8.0
TV_FILLED = 0.7
# ATSC 1.0 puts a pilot 309 kHz above the channel's lower edge
PILOT_OFFSET_MHZ = 0.309


def tv_stations(freqs, levels, floor: float) -> list[dict]:
    """
    Which TV channels carry a station: most of the channel well above the floor. ATSC 1.0's
    pilot stands out at the bottom of the channel; ATSC 3.0 has none, so a filled channel
    without a pilot is called ATSC 3.0. A channel with something narrower is "busy" (mics,
    or another carrier); one with nothing is "open".
    """
    freqs, levels = np.asarray(freqs, dtype=float), np.asarray(levels, dtype=float)
    if freqs.size == 0:
        return []
    result = []
    for number, low, high in tv_channels(float(freqs[0]), float(freqs[-1])):
        inside = (freqs >= low + 0.25) & (freqs <= high - 0.25)
        if inside.sum() < 4:
            continue
        values = levels[inside]
        filled = float(np.mean(values > floor + TV_ABOVE_DB))
        peak = float(values.max())
        state = "open"
        if filled >= TV_FILLED:
            state = "station"
        elif peak > floor + 10:
            state = "busy"
        entry = {"channel": number, "low_mhz": low, "high_mhz": high, "state": state,
                 "level_dbm": round(float(np.median(values)), 1), "peak_dbm": round(peak, 1)}
        if state == "station":
            pilot = np.abs(freqs - (low + PILOT_OFFSET_MHZ)) <= max(0.06, _step(freqs))
            body = np.median(values)
            entry["standard"] = ("ATSC 1.0" if pilot.any() and levels[pilot].max() > body + 6
                                 else "ATSC 3.0 or wideband")
        result.append(entry)
    return result


def _step(freqs: np.ndarray) -> float:
    return float(np.median(np.diff(freqs))) if freqs.size > 1 else 0.0


# ---------- what a survey point records for each band ----------

BUSY_ABOVE_DB = 10.0                 # a bin this far over the band's floor is something on air
QUIET_DB, NOISY_DB = 10.0, 25.0      # the strongest signal over the floor: green, amber, red


def summarize(freqs, levels, bands, rbw_khz: float | None = None) -> dict:
    """
    Per band: the strongest bin and where, the floor (10th percentile), the share of the band
    well above it, the few strongest separate signals, and for TV bands which channels carry
    stations. ``levels`` are dBm per bin (an average or a peak hold).
    """
    freqs, levels = np.asarray(freqs, dtype=float), np.asarray(levels, dtype=float)
    result = {}
    for band in bands:
        inside = (freqs >= band.low) & (freqs <= band.high)
        if inside.sum() < 4:
            continue
        f, v = freqs[inside], levels[inside]
        floor = float(np.percentile(v, 10))
        top = int(np.argmax(v))
        entry = {
            "peak_dbm": round(float(v[top]), 1), "peak_mhz": round(float(f[top]), 3),
            "floor_dbm": round(floor, 1),
            "busy": round(float(np.mean(v > floor + BUSY_ABOVE_DB)), 3),
            "signals": strongest_signals(f, v, floor),
        }
        if rbw_khz:
            entry["rbw_khz"] = round(float(rbw_khz), 1)
        if band.tv:
            entry["tv"] = tv_stations(f, v, floor)
        result[band.key] = entry
    return result


def strongest_signals(freqs, levels, floor: float, count: int = 5,
                      above_db: float = BUSY_ABOVE_DB) -> list[dict]:
    """
    The strongest separate signals: runs of bins over the floor, each reported by its peak and
    width, strongest first.
    """
    freqs, levels = np.asarray(freqs, dtype=float), np.asarray(levels, dtype=float)
    over = levels > floor + above_db
    found = []
    start = None
    for index, high in enumerate([*over, False]):
        if high and start is None:
            start = index
        elif not high and start is not None:
            run = slice(start, index)
            peak = start + int(np.argmax(levels[run]))
            step = _step(freqs)
            found.append({"mhz": round(float(freqs[peak]), 3), "dbm": round(float(levels[peak]), 1),
                          "width_khz": round(max(step, float(freqs[index - 1] - freqs[start]) + step)
                                             * 1000)})
            start = None
    return sorted(found, key=lambda s: -s["dbm"])[:count]


def activity(entry: dict | None) -> float | None:
    """How far the strongest signal stands over the band's floor: the survey map's figure."""
    if not entry or entry.get("peak_dbm") is None or entry.get("floor_dbm") is None:
        return None
    return float(entry["peak_dbm"]) - float(entry["floor_dbm"])


def grade(entry: dict | None) -> str:
    level = activity(entry)
    if level is None:
        return "none"
    return "good" if level < QUIET_DB else "fair" if level < NOISY_DB else "poor"


def resolution_khz(band: Band) -> float:
    """How finely a survey point sweeps a band: about 450 bins across, 10-250 kHz."""
    return float(min(250.0, max(10.0, band.width * 1000 / 450)))


def scan_bands(analyzer, bands, sweeps: int = 3, step_khz: float | None = None):
    """
    Sweeps each band with one analyzer and returns ({band key: summary}, notes). Bands it
    can't reach are left out with a note.
    """
    results, notes = {}, []
    for band in bands:
        if band.low < getattr(analyzer, "min_mhz", 0.0) - 0.5 or band.high > analyzer.max_mhz + 0.5:
            notes.append(f"{band.name}: beyond the {analyzer.name}'s range.")
            continue
        try:
            trace = analyzer.scan(band.low, band.high, step_khz or resolution_khz(band), sweeps)
        except Exception as error:
            notes.append(f"{band.name}: {error}")
            continue
        summary = summarize(trace.freqs, trace.peak, [band], trace.rbw_khz)
        if band.key in summary:
            summary[band.key]["analyzer"] = getattr(analyzer, "model", "") or analyzer.name
            results.update(summary)
    return results, notes


def share_bands(bands, pool: list) -> list[tuple[object, list[Band]]]:
    """
    Which analyzer sweeps which bands, so several sweep at once: each band goes to an analyzer
    that reaches it, the one with the least bandwidth so far, the first in ``pool`` on ties. The
    same pool always shares the same way.
    """
    load = {id(a): 0.0 for a in pool}
    jobs: dict[int, list[Band]] = {id(a): [] for a in pool}
    for band in sorted(bands, key=lambda b: -b.width):
        able = [a for a in pool
                if band.low >= getattr(a, "min_mhz", 0.0) - 0.5 and band.high <= a.max_mhz + 0.5]
        if not able:
            continue
        chosen = min(able, key=lambda a: (load[id(a)], pool.index(a)))
        load[id(chosen)] += band.width
        jobs[id(chosen)].append(band)
    return [(a, sorted(jobs[id(a)], key=lambda b: b.low)) for a in pool if jobs[id(a)]]


def out_of_reach(bands, pool: list) -> list[Band]:
    return [b for b in bands if not any(
        b.low >= getattr(a, "min_mhz", 0.0) - 0.5 and b.high <= a.max_mhz + 0.5 for a in pool)]


def advice(band: Band, entry: dict) -> str:
    """One line on what the reading means for equipment in this band, improvements first."""
    level = activity(entry) or 0.0
    signals = entry.get("signals") or []
    strongest = (f"{signals[0]['mhz']:.3f} MHz at {signals[0]['dbm']:.0f} dBm"
                 if signals else f"{entry['peak_mhz']:.3f} MHz")
    if band.tv and entry.get("tv"):
        stations = [c["channel"] for c in entry["tv"] if c["state"] == "station"]
        open_ = [c["channel"] for c in entry["tv"] if c["state"] == "open"]
        text = (f"TV stations on channel{'s' if len(stations) != 1 else ''} "
                f"{', '.join(map(str, stations))}. " if stations else "No TV stations heard. ")
        if band.mics:
            text += (f"Put mics and IEMs on open channels {', '.join(map(str, open_[:8]))}"
                     f"{'…' if len(open_) > 8 else ''}." if open_ else
                     "No channel is fully open: coordinate around the weakest signals.")
        return text
    if band.licence == AVOID:
        return (f"Keep all transmitters out of this band. Strongest here {strongest}."
                if level >= QUIET_DB else "Keep all transmitters out of this band.")
    if level < QUIET_DB:
        return (f"Quiet (nothing over {QUIET_DB:.0f} dB above the floor). Good for new equipment; "
                "recheck during the event, when everything is on.")
    if level < NOISY_DB:
        return (f"Some activity: strongest {strongest}, {level:.0f} dB over the floor. Keep new "
                "equipment off that frequency and its neighbours.")
    return (f"Busy: {strongest}, {level:.0f} dB over the floor. Find the source, or plan "
            "equipment in this band away from it with margin.")
