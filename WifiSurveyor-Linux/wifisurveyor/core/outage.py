"""
Why a network dropped its clients: the evidence one watched access point leaves behind, and
what it adds up to.

The monitor (net.ssidmonitor) gathers evidence from up to three places, each telling apart
causes the others can't:

- the adapter connected to the network: why the kernel says the link ended (who sent the
  deauthentication and its reason code, or that beacons stopped arriving), and whether the
  gateway, the internet and DNS answered while it was up;
- a second adapter in monitor mode on the access point's channel: every beacon, with the
  access point's own clock (TSF, which starts from zero when its radio starts, so it is the
  radio's uptime), and every deauthentication frame, with the signal level and sequence number
  that show whether the access point really sent it;
- a spectrum analyzer parked on the channel: whether the channel went quiet (the access point
  stopped transmitting) or was flooded (something drowned it).

Scans while the link is down add the unit's other radios: if its 2.4 GHz network stays up
while the 5 GHz one vanishes, the unit didn't go down, one radio did.

Everything here is plain data in, plain data out, so it can be tested without a radio.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------- 802.11 reason codes ----------

REASONS = {
    1: "unspecified reason",
    2: "previous authentication no longer valid",
    3: "the sender is leaving the network (shutting down or restarting)",
    4: "disassociated due to inactivity",
    5: "the access point can't handle any more clients",
    6: "class 2 frame from a client it doesn't know",
    7: "class 3 frame from a client it doesn't know",
    8: "the sender is leaving the network",
    9: "client tried to associate without authenticating",
    10: "power capability unacceptable",
    11: "supported channels unacceptable",
    12: "BSS transition (steered to another access point)",
    13: "invalid element",
    14: "message integrity (MIC) failure",
    15: "4-way handshake timeout",
    16: "group key handshake timeout",
    17: "handshake element mismatch",
    18: "invalid group cipher",
    19: "invalid pairwise cipher",
    20: "invalid key management",
    23: "802.1X authentication failed",
    24: "cipher suite rejected by policy",
    34: "too many unacknowledged frames: poor channel conditions",
}

# What a reason code from the access point says about it
FORGOT_US = {2, 6, 7, 9}         # it no longer knew this client: it had lost its state
LEAVING = {3, 8}                 # it said goodbye: a clean restart or its radio going off
KEY_TROUBLE = {15, 16, 17}
DFS_CHANNELS = set(range(52, 145))


def reason_text(code: int | None) -> str:
    if code is None:
        return ""
    return f"reason {code}: {REASONS.get(code, 'reserved or vendor code')}"


def is_dfs(channel: int | None) -> bool:
    return bool(channel) and channel in DFS_CHANNELS


# ---------- which BSSIDs belong to the same box ----------

def mac_bytes(mac: str) -> list[int] | None:
    try:
        parts = [int(p, 16) for p in mac.split(":")]
    except (ValueError, AttributeError):
        return None
    return parts if len(parts) == 6 else None


def siblings(target: str, bssids: list[str]) -> list[str]:
    """
    The BSSIDs that are probably other networks or radios of the same unit. Vendors derive
    them from one base address: the middle bytes match and the last differs a little, and
    virtual ones often set the locally-administered bit (or change more of the first byte).
    """
    base = mac_bytes(target)
    if base is None:
        return []
    found = []
    for other in bssids:
        mine = mac_bytes(other)
        if mine is None or other.lower() == target.lower():
            continue
        if mine[1:5] == base[1:5] and abs(mine[5] - base[5]) <= 0x40:
            found.append(other)
        elif mine[1:4] == base[1:4] and abs((mine[4] << 8 | mine[5]) - (base[4] << 8 | base[5])) <= 0x40 \
                and (mine[0] | 0x02) == (base[0] | 0x02):
            found.append(other)
    return found


# ---------- what a monitor adapter hears on the channel ----------

@dataclass
class Deauth:
    time: float
    src: str
    dst: str
    bssid: str
    reason: int | None
    seq: int | None
    rssi: float | None
    protected: bool
    kind: str = "deauth"              # or "disassoc"

    @property
    def broadcast(self) -> bool:
        return self.dst.lower() == "ff:ff:ff:ff:ff:ff"


class BeaconTrack:
    """
    One BSSID's beacons: when they stop and start again, its clock, its signal, and the
    sequence numbers its management frames carry. Fed one beacon at a time.
    """

    WINDOW = 120.0                     # seconds of beacons kept for rates and signal

    def __init__(self, bssid: str) -> None:
        self.bssid = bssid.lower()
        self.interval_tu = 100
        self.count = 0
        self.first_time = 0.0
        self.last_time = 0.0
        self.last_tsf: int | None = None
        self.last_seq: int | None = None
        self.channel = 0
        self.pmf = ""
        self.recent: deque = deque()       # (time, tsf, rssi, seq)
        self.resets: list[dict] = []       # {"time", "uptime_before_s"}
        self.gaps: list[dict] = []         # {"start", "end", "seconds"}
        self.csa: list[dict] = []          # {"time", "channel", "count"}
        # Its own word on how loaded it is, from the BSS Load element when its beacons carry
        # one: {"time", "stations", "busy"}, busy being the share of time it found the channel in use
        self.load: dict | None = None

    @property
    def interval_s(self) -> float:
        return self.interval_tu * 1.024e-3

    def gap_threshold(self) -> float:
        """Silence this long is a gap rather than a few missed beacons."""
        return max(1.0, 10 * self.interval_s)

    def add(self, when: float, tsf: int | None, seq: int | None, rssi: float | None,
            interval_tu: int | None = None, channel: int | None = None,
            csa: dict | None = None, pmf: str | None = None,
            load: tuple[int, float] | None = None) -> list[str]:
        """Records a beacon. Returns what it showed: "gap", "reset", "csa"."""
        shown = []
        if load is not None:
            self.load = {"time": when, "stations": load[0], "busy": load[1]}
        if interval_tu:
            self.interval_tu = interval_tu
        if channel:
            self.channel = channel
        if pmf:
            self.pmf = pmf
        if self.last_time and when - self.last_time > self.gap_threshold():
            self.gaps.append({"start": self.last_time, "end": when,
                              "seconds": round(when - self.last_time, 2)})
            shown.append("gap")
        if tsf is not None and self.last_tsf is not None and tsf + 1_000_000 < self.last_tsf:
            # The clock went backwards: the radio started again from zero
            self.resets.append({"time": when, "uptime_before_s": round(self.last_tsf / 1e6, 1),
                                "uptime_after_s": round(tsf / 1e6, 1)})
            shown.append("reset")
            self.recent.clear()
        if csa and (not self.csa or when - self.csa[-1]["time"] > 5):
            self.csa.append({"time": when, **csa})
            shown.append("csa")
        self.count += 1
        self.first_time = self.first_time or when
        self.last_time = when
        if tsf is not None:
            self.last_tsf = tsf
        if seq is not None:
            self.last_seq = seq
        self.recent.append((when, tsf, rssi, seq))
        while self.recent and self.recent[0][0] < when - self.WINDOW:
            self.recent.popleft()
        return shown

    def silent_for(self, now: float) -> float:
        return now - self.last_time if self.last_time else 0.0

    def uptime_s(self, now: float | None = None) -> float | None:
        """The radio's uptime from its clock, carried forward to ``now``."""
        if self.last_tsf is None:
            return None
        return self.last_tsf / 1e6 + max(0.0, (now or self.last_time) - self.last_time)

    def received_ratio(self, since: float, until: float | None = None) -> float | None:
        """
        Beacons heard over beacons sent, between two times, using the access point's own
        clock to count how many it sent. None without enough to go on.
        """
        rows = [r for r in self.recent if r[0] >= since and (until is None or r[0] <= until)
                and r[1] is not None]
        if len(rows) < 3:
            return None
        span_us = rows[-1][1] - rows[0][1]
        if span_us <= 0:
            return None
        sent = span_us / (self.interval_tu * 1024) + 1
        return min(1.0, len(rows) / sent)

    def rssi_median(self, since: float, until: float | None = None) -> float | None:
        values = [r[2] for r in self.recent if r[0] >= since and (until is None or r[0] <= until)
                  and r[2] is not None]
        return statistics.median(values) if values else None

    def rssi_spread(self, since: float) -> float:
        values = [r[2] for r in self.recent if r[0] >= since and r[2] is not None]
        if len(values) < 4:
            return 3.0
        quartiles = statistics.quantiles(values, n=4)
        return max(1.0, quartiles[2] - quartiles[0])

    def seq_near(self, when: float) -> int | None:
        """The sequence number of the beacon closest before ``when``."""
        before = [r for r in self.recent if r[0] <= when and r[3] is not None]
        return before[-1][3] if before else None


def forged(deauths: list[Deauth], track: BeaconTrack | None, pmf: str = "") -> dict:
    """
    How likely deauthentications claiming to be from the access point were forged: 0 to 1,
    with what points that way. Forged frames come from somewhere else (a different signal
    level from the beacons), carry sequence numbers out of step with the access point's own,
    are unprotected where the network requires protection, and come in bursts.
    """
    if not deauths:
        return {"score": 0.0, "why": []}
    why, score = [], 0.0
    pmf = pmf or (track.pmf if track else "")
    unprotected = [d for d in deauths if not d.protected]
    if pmf == "required" and unprotected:
        score += 0.6
        why.append(f"{len(unprotected)} deauthentication frame(s) were unprotected on a network "
                   "that requires protected management frames: the access point can't send those")
    elif pmf and not unprotected and deauths:
        score -= 0.4
        why.append("the deauthentication frames were protected, which only the access point can do")
    if track is not None:
        since = deauths[0].time - 30
        level = track.rssi_median(since, deauths[0].time)
        spread = track.rssi_spread(since)
        levels = [d.rssi for d in deauths if d.rssi is not None]
        if level is not None and levels:
            off = statistics.median(levels) - level
            if abs(off) >= max(8.0, 2.5 * spread):
                score += 0.35
                why.append(f"they arrived {abs(off):.0f} dB {'stronger' if off > 0 else 'weaker'} "
                           f"than the access point's beacons ({statistics.median(levels):.0f} vs "
                           f"{level:.0f} dBm): sent from somewhere else")
            elif abs(off) <= max(4.0, spread):
                score -= 0.2
                why.append(f"their signal matched the access point's beacons ({level:.0f} dBm)")
        jumps = []
        for d in deauths:
            near = track.seq_near(d.time)
            if near is not None and d.seq is not None:
                jumps.append(min((d.seq - near) % 4096, (near - d.seq) % 4096))
        if jumps:
            typical = statistics.median(jumps)
            if typical > 300:
                score += 0.25
                why.append(f"their sequence numbers were about {typical:.0f} away from the access "
                           "point's own")
            elif typical < 40:
                score -= 0.1
                why.append("their sequence numbers followed on from the access point's own")
    if len(deauths) >= 20:
        span = max(1.0, deauths[-1].time - deauths[0].time)
        if len(deauths) / span > 5:
            score += 0.2
            why.append(f"{len(deauths)} of them in {span:.0f} s: a flood, not a goodbye")
    return {"score": round(max(0.0, min(1.0, score)), 2), "why": why}


class ChannelLog:
    """
    Everything a monitor adapter hears on the watched access point's channel, kept as facts:
    beacons per BSSID, deauthentication and disassociation frames, which clients are about,
    and how much it hears at all (so a deaf monitor isn't mistaken for a silent access point).
    """

    def __init__(self, target: str) -> None:
        self.target = target.lower()
        self.tracks: dict[str, BeaconTrack] = {}
        self.deauths: deque[Deauth] = deque(maxlen=5000)
        self.clients: dict[str, float] = {}           # client MAC -> last heard with the target
        self.joins: deque = deque(maxlen=5000)        # (time, client) auth/assoc to the target
        self.heard: deque = deque()                   # (second, frames) of anything at all
        # Data frames to and from the access point, and how many were retransmissions: a
        # crowded or noisy channel makes them send things twice
        self.data_frames = 0
        self.data_retries = 0
        self.networks: dict[str, float] = {}          # other BSSIDs beaconing -> last heard
        self._lock = threading.Lock()

    def beacon(self, bssid: str, when: float, **fields) -> list[str]:
        with self._lock:
            track = self.tracks.get(bssid.lower())
            if track is None:
                track = self.tracks[bssid.lower()] = BeaconTrack(bssid)
            return track.add(when, **fields)

    def frame(self, when: float) -> None:
        """Anything heard, from anyone: the monitor is still listening."""
        with self._lock:
            second = int(when)
            if self.heard and self.heard[-1][0] == second:
                self.heard[-1][1] += 1
            else:
                self.heard.append([second, 1])
            while self.heard and self.heard[0][0] < second - 900:
                self.heard.popleft()

    def deauth(self, item: Deauth) -> None:
        with self._lock:
            self.deauths.append(item)

    def client(self, mac: str, when: float) -> None:
        with self._lock:
            self.clients[mac.lower()] = when

    def data(self, when: float, retry: bool) -> None:
        """A data frame to or from the access point."""
        with self._lock:
            self.data_frames += 1
            self.data_retries += bool(retry)

    def network(self, bssid: str, when: float) -> None:
        """A beacon from anyone: who else shares the channel."""
        if bssid and bssid != self.target:
            with self._lock:
                self.networks[bssid] = when

    def load(self, now: float, own: str = "") -> dict:
        """
        How busy the access point is right now, as far as the monitor can tell: clients heard
        with it in the last five minutes (``own``, the watching adapter, left out), the counts of
        data frames and retries so far (for the caller to take differences), other networks
        heard in the last minute, frames heard a second, and the access point's own report.
        """
        with self._lock:
            heard = sum(1 for mac, last in self.clients.items() if last >= now - 300 and mac != own)
            others = sum(1 for last in self.networks.values() if last >= now - 60)
            recent = [n for second, n in self.heard if now - 11 <= second < now - 1]
            track = self.tracks.get(self.target)
            reported = dict(track.load) if track is not None and track.load else None
            return {"heard": heard, "networks": others, "data": self.data_frames,
                    "retries": self.data_retries,
                    "fps": sum(recent) / 10 if recent else 0.0,
                    "reported": reported if reported and now - reported["time"] < 30 else None}

    def join(self, mac: str, when: float) -> None:
        with self._lock:
            self.joins.append((when, mac.lower()))

    @property
    def track(self) -> BeaconTrack | None:
        return self.tracks.get(self.target)

    def heard_between(self, start: float, end: float) -> int:
        with self._lock:
            return sum(n for second, n in self.heard if start <= second <= end)

    def evidence(self, start: float, end: float | None, now: float) -> dict:
        """What the monitor saw around an incident from ``start`` to ``end`` (None: ongoing)."""
        end_or_now = end or now
        with self._lock:
            track = self.tracks.get(self.target)
            deauths = [d for d in self.deauths
                       if start - 10 <= d.time <= end_or_now + 5
                       and self.target in (d.src.lower(), d.bssid.lower())]
            clients_before = {mac for mac, last in self.clients.items()
                              if start - 300 <= last <= start}
            rejoined = {mac for when, mac in self.joins if start <= when <= end_or_now + 120}
        result: dict = {"deauths": [asdict(d) for d in deauths[:200]],
                        "deauth_count": len(deauths)}
        if track is not None:
            gaps = [g for g in track.gaps if g["end"] >= start - 5 and g["start"] <= end_or_now + 5]
            silent = track.silent_for(now)
            if silent > track.gap_threshold() and track.last_time <= end_or_now + 5:
                gaps.append({"start": track.last_time, "end": None, "seconds": round(silent, 1)})
            resets = [r for r in track.resets if start - 10 <= r["time"] <= end_or_now + 180]
            csa = [c for c in track.csa if start - 60 <= c["time"] <= end_or_now + 5]
            result.update(
                beacons_heard=True,
                gap=gaps[0] if gaps else None,
                tsf_reset=resets[0] if resets else None,
                csa=csa[0] if csa else None,
                pmf=track.pmf,
                received_before=track.received_ratio(start - 60, start - 1),
                received_baseline=track.received_ratio(start - 120, start - 60),
                rssi_before=track.rssi_median(start - 20, start - 1),
                rssi_baseline=track.rssi_median(start - 120, start - 30),
                forged=forged(deauths, track),
            )
            gap = result["gap"]
            if gap:
                # Did the monitor hear anything else while the access point was silent?
                gap_end = gap["end"] or now
                result["others_heard_in_gap"] = self.heard_between(gap["start"] + 1, gap_end - 1)
        else:
            result.update(beacons_heard=False, forged=forged(deauths, None))
        result["clients_before"] = len(clients_before)
        result["clients_deauthed"] = len({d["dst"] for d in result["deauths"]
                                          if d["dst"] != "ff:ff:ff:ff:ff:ff"})
        result["broadcast_deauth"] = any(d["dst"] == "ff:ff:ff:ff:ff:ff" for d in result["deauths"])
        result["clients_rejoined"] = len(rejoined)
        return result


# ---------- incidents ----------

KINDS = {
    "disconnect": "Disconnected",
    "no_internet": "No internet",
    "no_gateway": "Gateway silent",
    "no_address": "No address",
    "dns": "DNS failing",
}


@dataclass
class Incident:
    """One outage and what was known about it. Plain data, so it's logged as JSON as is."""
    id: int
    start: float
    kind: str
    ssid: str
    bssid: str
    channel: int = 0
    freq_mhz: float = 0.0
    end: float | None = None
    # From the connected adapter
    by_ap: bool | None = None
    reason: int | None = None
    locally_generated: bool | None = None
    unprotected_deauths: int = 0
    ch_switch_mhz: float | None = None
    signal_before: float | None = None
    signal_baseline: float | None = None
    beacons_before: float | None = None         # share of beacons the client received
    beacon_losses_before: int = 0
    rejoin_errors: list[str] = field(default_factory=list)
    associated_again: float | None = None
    # Scans while the link was down: is the access point there, its clock, its other radios
    tsf_before: dict = field(default_factory=dict)   # bssid -> [time, tsf_us], last known
    scans: list[dict] = field(default_factory=list)  # {"time", "heard": {bssid: {...}}}
    sibling_bssids: list[str] = field(default_factory=list)
    # From the other sources, None when there wasn't one
    monitor: dict | None = None
    spectrum: dict | None = None
    probes: dict = field(default_factory=dict)       # what answered while it was down
    pcap: str = ""
    diagnosis: dict = field(default_factory=dict)
    # The joining adapter itself dropped off the computer (USB) at the time: not the network
    adapter_lost: bool = False

    @property
    def seconds(self) -> float | None:
        return None if self.end is None else self.end - self.start

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Incident:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _scan_facts(incident: Incident) -> dict:
    """What the scans while the link was down showed about the target and its siblings."""
    target = incident.bssid.lower()
    others = {b.lower() for b in incident.sibling_bssids}
    # "During" is until the access point was heard again: once it's back, so are its siblings
    back = next((s["time"] for s in incident.scans if target in s["heard"]), None)
    until = min(t for t in (back, incident.associated_again, incident.end, math.inf) if t is not None)
    during = [s for s in incident.scans if s["time"] < until]
    facts: dict = {"scanned": bool(incident.scans), "target_heard_during": None,
                   "siblings_heard_during": None, "target_back_channel": None,
                   "target_tsf_reset": None, "siblings_tsf_reset": None, "sibling_bands": set(),
                   "target_back_after": None}
    facts["heard_at_once"] = bool(incident.scans) and target in incident.scans[0]["heard"]
    if during:
        facts["target_heard_during"] = any(target in s["heard"] for s in during)
        if others:
            facts["siblings_heard_during"] = any(o in s["heard"] for s in during for o in others)
    for scan in incident.scans:
        heard = scan["heard"]
        if target in heard and facts["target_back_channel"] is None:
            facts["target_back_channel"] = heard[target].get("channel")
            facts["target_back_after"] = scan["time"] - incident.start
        for bssid, info in heard.items():
            if bssid != target and bssid not in others:
                continue
            reset = _tsf_reset(incident, bssid, scan["time"], info.get("tsf_us"))
            if reset is None:
                continue
            if bssid == target:
                facts["target_tsf_reset"] = bool(facts["target_tsf_reset"]) or reset
            else:
                facts["siblings_tsf_reset"] = bool(facts["siblings_tsf_reset"]) or reset
                facts["sibling_bands"].add(_band(info.get("freq")))
    return facts


def _band(freq: float | None) -> str:
    if not freq:
        return "?"
    return "2.4 GHz" if freq < 3000 else "5 GHz" if freq < 5925 else "6 GHz"


def _tsf_reset(incident: Incident, bssid: str, when: float, tsf_us: int | None) -> bool | None:
    """Whether a clock read after the incident shows the radio restarted during it."""
    if not tsf_us:
        return None
    uptime = tsf_us / 1e6
    known = incident.tsf_before.get(bssid)
    if known:
        expected = known[1] / 1e6 + (when - known[0])
        return uptime < expected - 5
    # Without an earlier reading: a radio up for less time than the outage has lasted (plus
    # a minute's grace) started during it
    if when - incident.start > 0:
        return uptime < (when - incident.start) + 60
    return None


CAUSES = {
    "unit_down": "The access point went down (rebooted or lost power)",
    "radio_restart": "One radio in the access point restarted",
    "radio_failing": "A radio in the access point is failing",
    "channel_change": "The access point changed channel",
    "deauth_forged": "Someone sent forged deauthentication frames",
    "deauth_ap": "The access point disconnected its clients on purpose",
    "key_handshake": "The network's key handshake failed",
    "mic_failure": "TKIP countermeasures: the access point shut out every client",
    "interference": "Interference drowned the channel",
    "adapter_lost": "This computer's adapter dropped off, not the network",
    "client_side": "Only this adapter lost the access point",
    "upstream": "Wi-Fi stayed up; the internet connection behind it failed",
    "lan": "Wi-Fi stayed up; the gateway stopped answering",
    "dhcp": "Joined, but no address was handed out (DHCP)",
    "dns": "Wi-Fi and internet up; DNS failed",
    "unknown": "Not enough evidence to say",
}

ACTIONS = {
    "unit_down": [
        "Check the access point's power: the PoE switch's power budget and port log, the "
        "injector or adapter, and the cable run. A unit that drops every radio at once is "
        "usually losing power or rebooting.",
        "Read the access point's own log for the reboot reason (crash, watchdog, firmware "
        "update, scheduled reboot) and compare its times with these incidents.",
        "Update the firmware. If it reboots at the same time each day, look for a scheduled "
        "reboot or a controller task.",
    ],
    "radio_restart": [
        "Look in the access point's log for radio or driver resets on that band ('radio "
        "reset', 'beacon stuck', 'firmware crash', 'target assert').",
        "Update the access point's firmware. As a test, narrow that radio's channel width.",
    ],
    "radio_failing": [
        "The same radio keeps restarting while the unit's other radios stay up: plan to "
        "replace (RMA) the unit.",
        "Until then, update its firmware and move clients to the healthy band if you can.",
    ],
    "channel_change": [
        "If it left a DFS channel (52-144), radar detection moved it: give this network a "
        "non-DFS channel (36-48 or 149-165) so it never has to.",
        "Otherwise automatic channel selection moved it: set the channel by hand, or let it "
        "change only at night.",
    ],
    "deauth_forged": [
        "Set Protected Management Frames (802.11w) to Required. WPA3 does this. Clients then "
        "ignore forged deauthentication frames.",
        "Find the transmitter: the forged frames' signal level is in the evidence. Walk "
        "with the monitor and follow it up.",
    ],
    "deauth_ap": [
        "The access point disconnected everyone itself. Check the controller and access point "
        "log at these times for configuration pushes, a WLAN schedule, session timeouts, "
        "load balancing or band steering.",
    ],
    "key_handshake": [
        "The group key rekey failed. Lengthen the group rekey interval (or turn periodic "
        "rekeying off) and update the access point's firmware.",
        "If it happens at a regular interval, compare that with the rekey interval: they will "
        "match.",
    ],
    "mic_failure": [
        "Turn TKIP off: use WPA2/WPA3 with AES (CCMP) only. TKIP's countermeasures disconnect "
        "every client for a minute after two integrity failures.",
    ],
    "interference": [
        "Something on the channel drowned the access point. Look at the channel trace for its "
        "shape, and move the network to a cleaner channel.",
        "If it recurs at set times, find what runs then (microwave, camera, another network "
        "turning on).",
    ],
    "adapter_lost": [
        "The adapter vanished from the computer and came back (the kernel logs 'USB "
        "disconnect'): move it to another USB port, straight into the computer rather than a "
        "hub, or try a shorter cable or a powered hub.",
        "If it keeps happening, watch this band with a different adapter. These incidents say "
        "nothing about the network, so they don't count against its availability.",
    ],
    "client_side": [
        "The access point kept beaconing and nobody was disconnected, so other clients probably "
        "stayed connected. Move this monitor closer, or check this adapter's driver.",
    ],
    "upstream": [
        "Wi-Fi was fine: the access point and gateway answered. The internet connection behind "
        "the router failed. Check the modem and the provider.",
    ],
    "lan": [
        "Wi-Fi was fine but the gateway stopped answering. Check the access point's uplink port, "
        "VLAN, the switch and the router.",
    ],
    "dhcp": [
        "The DHCP server didn't hand out an address. Check its pool isn't exhausted and that "
        "it's reachable on this network's VLAN.",
    ],
    "dns": [
        "Check the DNS server this network hands out, or hand out a different one.",
    ],
    "unknown": [],
}


def diagnose(incident: Incident, earlier: list[Incident] | None = None) -> dict:
    """
    The most likely cause, how sure, why, what to do, the alternatives, and what evidence would
    have made it surer. Causes are scored from each piece of evidence for or against them.
    """
    earlier = earlier or []
    score = dict.fromkeys(CAUSES, 0.0)
    score["unknown"] = 0.5
    why: dict[str, list[str]] = {key: [] for key in CAUSES}
    facts = _scan_facts(incident)
    mon = incident.monitor or {}
    spec = incident.spectrum or {}

    def add(cause: str, points: float, reason: str = "") -> None:
        score[cause] += points
        if reason and points > 0:
            why[cause].append(reason)

    # ----- the watching adapter itself went away: nothing was learnt about the network -----
    if incident.adapter_lost:
        add("adapter_lost", 10, "this computer's adapter disappeared and came back (a USB "
                                "disconnect), so the network was never seen to fail")

    # ----- the link never broke: which stage behind it did -----
    if incident.kind != "disconnect":
        cause = {"no_internet": "upstream", "no_gateway": "lan", "no_address": "dhcp",
                 "dns": "dns"}.get(incident.kind, "unknown")
        add(cause, 6, {
            "upstream": "the adapter stayed connected and the gateway answered, but nothing "
                        "beyond it did",
            "lan": "the adapter stayed connected, but the gateway stopped answering",
            "dhcp": "the adapter joined but was given no address",
            "dns": "the gateway and the internet answered, but DNS lookups failed",
        }.get(cause, ""))
        if mon.get("beacons_heard") and not mon.get("gap"):
            add(cause, 1, "the access point kept beaconing throughout")
        return _verdict(score, why, incident, facts)

    reason = incident.reason
    gap = mon.get("gap")
    beacons_continued = bool(mon.get("beacons_heard")) and not gap
    monitor_deaf = gap is not None and mon.get("others_heard_in_gap", 1) == 0
    target_silent = (gap is not None and not monitor_deaf) or facts["target_heard_during"] is False
    reset_target = bool(mon.get("tsf_reset")) or facts["target_tsf_reset"] is True
    siblings_up = facts["siblings_heard_during"] is True or facts["siblings_tsf_reset"] is False
    siblings_down = facts["siblings_heard_during"] is False or facts["siblings_tsf_reset"] is True
    gap_seconds = (gap or {}).get("seconds") or facts["target_back_after"]
    # The radio's uptime when it restarted: a crash after the same uptime each time is a pattern
    uptime_before = (mon.get("tsf_reset") or {}).get("uptime_before_s")

    # ----- the access point went silent, or restarted -----
    if target_silent:
        words = ("the monitor heard no beacons from it for "
                 f"{gap_seconds:.0f} s" if gap and gap_seconds else
                 "scans while disconnected didn't find it")
        add("unit_down", 2, words)
        add("radio_restart", 2, words)
        add("channel_change", 0.5)
    if reset_target:
        text = ("its clock (TSF) started again from zero: the radio restarted"
                + (f" after {_duration(uptime_before)} up" if uptime_before else ""))
        add("unit_down", 2, text)
        add("radio_restart", 2.5, text)
    if (target_silent or reset_target) and siblings_down:
        add("unit_down", 3, "the unit's other radios went down too"
            + (f" ({', '.join(sorted(facts['sibling_bands']))})" if facts["sibling_bands"] else ""))
        score["radio_restart"] -= 2
    if (target_silent or reset_target) and siblings_up:
        add("radio_restart", 3, "the unit's other radios stayed up throughout")
        score["unit_down"] -= 4
    if gap_seconds and (target_silent or reset_target) and not siblings_up and not siblings_down:
        if gap_seconds >= 45:
            add("unit_down", 1, f"it was gone {gap_seconds:.0f} s, about as long as a reboot takes")
        elif gap_seconds < 25:
            add("radio_restart", 1, f"it was back within {gap_seconds:.0f} s, quicker than a reboot")
    if beacons_continued:
        score["unit_down"] -= 4
        score["radio_restart"] -= 3
        add("deauth_ap", 0.5)
    spectrum_quiet = spec.get("quiet_during")
    spectrum_loud = spec.get("loud_during")
    if spectrum_quiet and target_silent:
        add("unit_down", 1, "the analyzer saw the channel go quiet: it stopped transmitting")
        add("radio_restart", 1, "the analyzer saw the channel go quiet: it stopped transmitting")

    # ----- moved channel -----
    csa = mon.get("csa")
    if csa:
        add("channel_change", 5, f"its beacons announced a move to channel {csa.get('channel')}")
    if incident.ch_switch_mhz:
        add("channel_change", 4, f"the adapter was told to follow it to {incident.ch_switch_mhz:.0f} MHz")
    back = facts["target_back_channel"]
    if back and incident.channel and back != incident.channel:
        add("channel_change", 4, f"it came back on channel {back}, not {incident.channel}")
        score["unit_down"] -= 1
        score["radio_restart"] -= 1
    if (csa or back and back != incident.channel) and is_dfs(incident.channel):
        add("channel_change", 1, f"channel {incident.channel} is a DFS channel, which it must "
                                 "leave when it detects radar")

    # ----- deauthentication: real or forged, and what the reason says -----
    forged_score = (mon.get("forged") or {}).get("score", 0.0)
    if incident.unprotected_deauths:
        add("deauth_forged", 3, f"the adapter rejected {incident.unprotected_deauths} unprotected "
                                "deauthentication frame(s) claiming to be the access point")
    if mon.get("deauth_count"):
        add("deauth_forged", 5 * forged_score, "; ".join((mon.get("forged") or {}).get("why", [])))
        if forged_score < 0.4 and not target_silent:
            add("deauth_ap", 2.5, f"the monitor saw {mon['deauth_count']} deauthentication frame(s) "
                                  "from the access point while it kept beaconing")
        if mon.get("broadcast_deauth") or mon.get("clients_deauthed", 0) > 1:
            who = ("to every client at once" if mon.get("broadcast_deauth")
                   else f"to {mon['clients_deauthed']} clients")
            add("deauth_ap", 1, f"they were sent {who}")
            add("deauth_forged", 0.5)
    if incident.by_ap:
        if not target_silent and not reset_target and forged_score < 0.4:
            add("deauth_ap", 2, "the access point told this adapter to leave ("
                                f"{reason_text(reason) or 'no reason given'})")
        if reason in KEY_TROUBLE:
            add("key_handshake", 6, f"the disconnect gave {reason_text(reason)}")
        elif reason == 14:
            add("mic_failure", 6, f"the disconnect gave {reason_text(reason)}")
        elif reason in LEAVING:
            add("unit_down", 0.5)
            add("radio_restart", 1, f"it said it was leaving ({reason_text(reason)}), as an access "
                                    "point does when its radio goes off")
        elif reason in FORGOT_US:
            add("unit_down", 1.5, f"it no longer knew this adapter ({reason_text(reason)}): it had "
                                  "lost its client list, as a restart does")
            add("radio_restart", 1.5)
        elif reason == 34:
            add("interference", 2, f"it gave {reason_text(reason)}")
    if forged_score >= 0.5 and beacons_continued:
        add("deauth_forged", 1, "the real access point kept beaconing the whole time")
        score["deauth_ap"] -= 2

    # ----- the channel itself -----
    if spectrum_loud:
        add("interference", 4, spec.get("loud_words") or "the analyzer saw the channel flooded")
        score["unit_down"] -= 1
    if monitor_deaf and spectrum_loud:
        add("interference", 1, "the monitor couldn't hear anyone at all while it lasted")

    # ----- only this adapter -----
    if incident.locally_generated and not incident.by_ap:
        if beacons_continued and not mon.get("deauth_count"):
            add("client_side", 4, "the monitor heard the access point beacon throughout, and no "
                                  "deauthentication")
        if facts["heard_at_once"] and not target_silent and not reset_target:
            after = incident.scans[0]["time"] - incident.start
            add("client_side", 2, f"the access point was still there {after:.0f} s later: it hadn't "
                                  "gone anywhere")
            add("interference", 1)
        if incident.signal_before is not None and incident.signal_before <= -78:
            add("client_side", 2, f"this adapter's signal was only {incident.signal_before:.0f} dBm")
        if (incident.beacons_before is not None and incident.beacons_before < 0.5
                and not mon and not spec):
            add("interference", 0.5)
            add("unit_down", 0.5)
            add("radio_restart", 0.5)
    if incident.associated_again and not mon and not facts["scanned"]:
        add("unknown", 1)

    # Last, once everything else has had its say: a radio that keeps restarting while the rest
    # of the unit stays up is failing, and one already faltering beforehand looks the same way
    best = max(score, key=score.get)
    if best == "radio_restart" and (target_silent or reset_target):
        before = [i for i in earlier if (i.diagnosis or {}).get("cause") in ("radio_restart", "radio_failing")
                  and i.bssid.lower() == incident.bssid.lower()]
        signs = []
        mon_before, mon_base = mon.get("received_before"), mon.get("received_baseline")
        if mon_before is not None and mon_base is not None and mon_before < mon_base - 0.15:
            signs.append(f"it was already missing beacons beforehand ({mon_before:.0%} received, "
                         f"against {mon_base:.0%} earlier)")
        drop = _drop(mon.get("rssi_baseline"), mon.get("rssi_before"))
        if drop and drop >= 6 and siblings_up:
            signs.append(f"its beacons had weakened by {drop:.0f} dB just before")
        if before or len(signs) >= 1 and siblings_up:
            score["radio_failing"] = score["radio_restart"] + len(before) + len(signs)
            why["radio_failing"] = list(why["radio_restart"]) + signs
            if before:
                why["radio_failing"].insert(0, f"the same radio has restarted {len(before) + 1} "
                                               "times while being watched")

    return _verdict(score, why, incident, facts)


def _drop(baseline: float | None, before: float | None) -> float | None:
    if baseline is None or before is None:
        return None
    return baseline - before


def _verdict(score: dict, why: dict, incident: Incident, facts: dict) -> dict:
    ranked = sorted(((max(0.0, s), c) for c, s in score.items()), reverse=True)
    best_score, best = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score >= 5 and best_score - runner >= 3:
        confidence = "high"
    elif best_score >= 3 and best_score - runner >= 1.5:
        confidence = "medium"
    else:
        confidence = "low"
    if best_score < 2:
        best, confidence = "unknown", "low"
    missing = []
    if incident.kind == "disconnect":
        if incident.monitor is None:
            missing.append(f"A second adapter in monitor mode on channel {incident.channel or '?'} "
                           "would show the beacons and any deauthentication: whether the access "
                           "point stopped, and whether a deauthentication was forged.")
        if incident.spectrum is None:
            missing.append("A spectrum analyzer on the channel would show whether interference "
                           "drowned it, or it went quiet.")
        if not incident.sibling_bssids:
            missing.append("No other radio of this unit was found to compare with, so a unit going "
                           "down can't be told from one radio going down.")
        elif facts["siblings_heard_during"] is None and facts["siblings_tsf_reset"] is None:
            missing.append("The unit's other radios couldn't be checked while it was down.")
    return {
        "cause": best,
        "title": CAUSES[best],
        "confidence": confidence,
        "why": [w for w in why.get(best, []) if w],
        "do": list(ACTIONS.get(best, [])),
        "alternatives": [{"cause": c, "title": CAUSES[c], "score": round(s, 1)}
                         for s, c in ranked[1:4] if s >= 2 and c != best],
        "missing": missing,
        "score": round(best_score, 1),
    }


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


duration = _duration


# ---------- across incidents ----------

def patterns(incidents: list[Incident]) -> list[str]:
    """What the incidents have in common: a regular interval, a time of day, a crash uptime."""
    done = [i for i in incidents if i.end is not None]
    notes = []
    if len(done) < 2:
        return notes
    causes: dict[str, int] = {}
    for incident in done:
        cause = (incident.diagnosis or {}).get("cause", "unknown")
        causes[cause] = causes.get(cause, 0) + 1
    top, count = max(causes.items(), key=lambda item: item[1])
    if count >= 2 and top != "unknown":
        notes.append(f"{count} of {len(done)} incidents look like: {CAUSES[top].lower()}.")
    starts = sorted(i.start for i in done)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    if len(gaps) >= 2:
        middle = statistics.median(gaps)
        spread = statistics.pstdev(gaps)
        if middle > 60 and spread < 0.15 * middle:
            notes.append(f"They come about every {_duration(middle)}, regularly: something on a "
                         "timer (a scheduled reboot, a key rekey interval, a lease, a backup job).")
    hours = [time.localtime(s).tm_hour + time.localtime(s).tm_min / 60 for s in starts]
    if len(hours) >= 3:
        middle = statistics.median(hours)
        if all(min(abs(h - middle), 24 - abs(h - middle)) < 0.5 for h in hours):
            stamp = f"{int(middle):02d}:{int(middle % 1 * 60):02d}"
            notes.append(f"They all start around {stamp}: look for something scheduled then.")
    uptimes = [((i.monitor or {}).get("tsf_reset") or {}).get("uptime_before_s") for i in done]
    uptimes = [u for u in uptimes if u]
    if len(uptimes) >= 2:
        middle = statistics.median(uptimes)
        if statistics.pstdev(uptimes) < 0.15 * middle:
            notes.append(f"The radio restarted after about {_duration(middle)} up each time: a "
                         "fault that builds up (a memory leak, overheating) more than a random one.")
    return notes


def availability(start: float, now: float, incidents: list[Incident]) -> float | None:
    """The share of the watched time the network was usable."""
    span = now - start
    if span <= 0:
        return None
    down = sum(min(i.end or now, now) - max(i.start, start) for i in incidents if not i.adapter_lost)
    return max(0.0, 1 - down / span)


def kind_name(incident: Incident) -> str:
    """What an incident was, in a few words: its kind, or the adapter dropping out."""
    return "Adapter dropped out" if incident.adapter_lost else KINDS.get(incident.kind, incident.kind)


# ---------- the session log ----------

def folder() -> Path:
    from .project import data_dir, make_dir

    return make_dir(data_dir() / "monitor")


class SessionLog:
    """
    One watching session on disk: a folder holding a JSON-lines log (every beat, every
    channel reading, every incident as it ends) and a capture of the frames around each
    incident. Written as it goes, so a crash or a pulled plug loses seconds, not the session.
    """

    def __init__(self, ssid: str, bssid: str, base: Path | None = None, label: str = "") -> None:
        from .project import hand_back, make_dir

        # ``label`` (the band) keeps apart the folders of two radios watched at once
        safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in ssid)[:40] or "hidden"
        if label:
            safe += " " + "".join(c if c.isalnum() or c in " -_." else "_" for c in label)
        self.folder = make_dir((base or folder()) / time.strftime(f"%Y-%m-%d %H.%M.%S {safe}"))
        self.path = self.folder / "session.jsonl"
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        hand_back(self.path)
        self.write({"type": "start", "ssid": ssid, "bssid": bssid})

    def write(self, record: dict) -> None:
        record = {"t": round(time.time(), 2), **record}
        with self._lock:
            if self._file.closed:
                return
            self._file.write(json.dumps(record, separators=(",", ":"), default=_jsonable) + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()


def _jsonable(value):
    if isinstance(value, set):
        return sorted(value)
    return str(value)


# The channel's spectrum goes into the log a row per SPECTRUM_SECONDS (the average and the
# peak of each bin over the sweeps in it), thinned to SPECTRUM_BINS and packed a byte a bin
SPECTRUM_SECONDS = 30.0
SPECTRUM_BINS = 128
LEVEL_FLOOR = -160                     # dBm a packed byte of 0 stands for; 1 dB a step


def thin_bins(start: float, step: float, avg: list[float], peak: list[float],
              limit: int = SPECTRUM_BINS) -> tuple[float, float, list[float], list[float]]:
    """At most ``limit`` bins: neighbours merged, averages averaged and peaks kept."""
    count = len(avg)
    group = max(1, math.ceil(count / limit))
    if group == 1:
        return start, step, list(avg), list(peak)
    merged_avg = [statistics.fmean(avg[i:i + group]) for i in range(0, count, group)]
    merged_peak = [max(peak[i:i + group]) for i in range(0, count, group)]
    return start + step * (group - 1) / 2, step * group, merged_avg, merged_peak


def pack_levels(levels) -> str:
    """dBm levels as text, a byte each to the nearest dB."""
    import base64

    return base64.b64encode(bytes(int(min(255, max(0, round(v - LEVEL_FLOOR)))) for v in levels)).decode()


def unpack_levels(text: str) -> list[float]:
    import base64

    return [float(b + LEVEL_FLOOR) for b in base64.b64decode(text)]


LOG_NAME = "session.jsonl"
COMPACT_NAME = "session.jsonl.gz"
# Beats kept every one, this long either side of an incident, and either side of a beat that
# wasn't healthy: the detail a diagnosis or a report looks at
FULL_AROUND_INCIDENT = 600.0
FULL_AROUND_TROUBLE = 120.0
# A log nothing has written to for this long belongs to a watch that's over (a crash ends one
# without its "stop")
STALE_SECONDS = 3600.0


def session_file(folder: Path) -> Path | None:
    """A session folder's log: the one being written, or once it's over, its compacted copy."""
    folder = Path(folder)
    for name in (LOG_NAME, COMPACT_NAME):
        if (folder / name).is_file():
            return folder / name
    return None


def read_records(path: Path):
    """Every record of a session log, plain or compacted, skipping a line cut short."""
    import gzip

    path = Path(path)
    if path.is_dir():
        path = session_file(path) or path / LOG_NAME
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue                       # a line cut short when the power went


def read_session(path: Path) -> tuple[dict, list[dict], list[Incident]]:
    """A session log back: its start record, its beats, and its incidents."""
    start, beats, incidents = {}, [], {}
    for record in read_records(path):
        kind = record.get("type")
        if kind == "start" and not start:
            start = record
        elif kind == "beat":
            beats.append(record)
        elif kind == "incident":
            incident = Incident.from_json(record["incident"])
            incidents[incident.id] = incident
    return start, beats, sorted(incidents.values(), key=lambda i: i.start)


def _minute(start: float, beats: list[dict]) -> dict:
    """
    A minute of healthy beats as one record: each number's min, median and max (losses added
    up, beacons and booleans as the worst and the share), and how many beats were in each state.
    """
    summary: dict = {"t": start, "type": "minute", "n": len(beats), "until": beats[-1]["t"]}
    keys = {key for beat in beats for key in beat} - {"t", "type"}
    for key in sorted(keys):
        values = [beat[key] for beat in beats if beat.get(key) is not None]
        if not values:
            continue
        if all(isinstance(v, bool) for v in values):
            summary[key] = round(sum(values) / len(values), 3)
        elif all(isinstance(v, (int, float)) for v in values):
            if key == "losses":
                summary[key] = sum(values)
            else:
                summary[key] = [min(values), round(statistics.median(values), 3), max(values)]
        else:
            counts: dict[str, int] = {}
            for value in values:
                counts[str(value)] = counts.get(str(value), 0) + 1
            summary[key] = counts
    return summary


def compact(path: Path) -> Path | None:
    """
    Shrinks a finished session's log to what's worth keeping, gzipped beside it, and removes
    the original. Everything but the beats stays as it was: incidents, events, channel readings.
    Beats are kept every one around incidents and around any that wasn't healthy; the long
    healthy stretches between become a record a minute (see _minute). Returns the new file,
    or None when there was nothing to compact.
    """
    import gzip

    from .project import hand_back

    path = Path(path)
    if path.name != LOG_NAME or not path.is_file():
        return None
    records = list(read_records(path))
    keep: list[tuple[float, float]] = []
    for record in records:
        kind = record.get("type")
        if kind == "open":
            began = record.get("start", record["t"])
            keep.append((began - FULL_AROUND_INCIDENT, began + FULL_AROUND_INCIDENT))
        elif kind == "incident":
            found = record.get("incident") or {}
            began = found.get("start", record["t"])
            ended = found.get("end") or record["t"]
            keep.append((began - FULL_AROUND_INCIDENT, ended + FULL_AROUND_INCIDENT))
        elif kind == "beat" and record.get("state") not in (None, "up"):
            keep.append((record["t"] - FULL_AROUND_TROUBLE, record["t"] + FULL_AROUND_TROUBLE))
    out, minutes = [], {}
    for record in records:
        if record.get("type") != "beat" or any(a <= record["t"] <= b for a, b in keep):
            out.append(record)
        else:
            minutes.setdefault(int(record["t"] // 60) * 60.0, []).append(record)
    out += [_minute(start, beats) for start, beats in minutes.items()]
    out.sort(key=lambda r: r.get("t", 0))      # stable: the start record stays first
    target = path.with_name(COMPACT_NAME)
    temporary = target.with_name(COMPACT_NAME + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        for record in out:
            handle.write(json.dumps(record, separators=(",", ":"), default=_jsonable) + "\n")
    temporary.replace(target)
    hand_back(target)
    path.unlink()
    return target


def finished(path: Path) -> bool:
    """Whether a plain log is of a watch that's over: it stopped, or nothing writes to it."""
    path = Path(path)
    if time.time() - path.stat().st_mtime > STALE_SECONDS:
        return True
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 200))
        return b'"type":"stop"' in handle.read()


def compact_finished(base: Path | None = None) -> int:
    """Compacts every session under ``base`` (the monitor folder) that's over. Returns how many."""
    done = 0
    for log in sorted(Path(base or folder()).glob(f"*/{LOG_NAME}")):
        try:
            if finished(log) and compact(log):
                done += 1
        except (OSError, ValueError):
            continue                           # one unreadable session doesn't stop the rest
    return done


def incidents_csv(incidents: list[Incident]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["start", "end", "seconds", "band", "what", "most likely cause", "confidence",
                     "reason code", "why", "what to do", "also possible", "capture"])
    for incident in incidents:
        found = incident.diagnosis or {}
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(incident.start)),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(incident.end)) if incident.end else "",
            f"{incident.seconds:.0f}" if incident.seconds is not None else "",
            ("2.4 GHz" if incident.freq_mhz < 3000 else "5 GHz" if incident.freq_mhz < 5925
             else "6 GHz") if incident.freq_mhz else "",
            kind_name(incident),
            found.get("title", ""),
            found.get("confidence", ""),
            reason_text(incident.reason),
            " | ".join(found.get("why", [])),
            " | ".join(found.get("do", [])),
            " | ".join(a["title"] for a in found.get("alternatives", [])),
            incident.pcap,
        ])
    return out.getvalue()
