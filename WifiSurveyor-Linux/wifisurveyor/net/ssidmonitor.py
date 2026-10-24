"""
Watching one network on one access point for as long as it takes to catch it misbehaving.

An adapter stays joined to the access point (only that one: it isn't allowed to roam) and
beats every couple of seconds: is it still associated, how many beacons arrived, does the
gateway answer, does the internet, does DNS. When something fails it opens an incident and
gathers what will explain it: the kernel's reason for the disconnect, scans for the access
point and the unit's other radios while it's gone (with their clocks, which show a restart),
what a monitor-mode adapter heard on the channel, and what a spectrum analyzer parked on the
channel saw. When it's over the evidence is weighed (core.outage.diagnose) and written down.

The UI reads the state; everything here runs on its own threads.
"""

from __future__ import annotations

import itertools
import statistics
import threading
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field

from ..core import outage
from ..core.outage import Incident, SessionLog
from . import connectivity, linkwatch, monitor, wifi

HOLDER = "the Watch page"
SCAN_EVERY = 8.0              # seconds between scans while the link is down
JOIN_BACKOFF = (5, 10, 20, 30, 60)
SETTLE = 30.0                 # seconds of evidence gathered after an incident ends
DHCP_GRACE = 20.0             # seconds after joining before no address counts as a failure
FAILS_TO_OPEN = 2             # failed beats in a row before a non-disconnect incident opens
SLOW_MS = 250.0
WIFI_STAGES = ("disconnect", "no_gateway", "no_address")   # failures of the Wi-Fi link itself
BEAT_KEEP = 200_000           # about four days at a beat every two seconds
WATERFALL_RECENT = 300        # channel traces kept at full rate
WATERFALL_MINUTES = 60 * 24 * 7


@dataclass
class Target:
    ssid: str
    bssid: str
    freq: float
    center: float = 0.0
    width: int = 20
    siblings: dict[str, float] = field(default_factory=dict)    # bssid -> frequency

    @property
    def channel(self) -> int:
        return wifi.channel_for(self.freq)

    @property
    def span(self) -> tuple[float, float]:
        middle = self.center or self.freq
        return middle - self.width / 2, middle + self.width / 2


STAGE_WORDS = {"disconnect": "disconnected", "no_internet": "internet not answering",
               "no_gateway": "gateway not answering", "no_address": "no address from DHCP",
               "dns": "DNS not answering"}


def trouble_words(beats: list[dict]) -> list[str]:
    """
    Why these beats weren't green, worst first: for the status line (one beat) and for the
    labels on the traces (a run of them).
    """
    words = [STAGE_WORDS.get(stage, stage) for stage in
             dict.fromkeys(b["stage"] for b in beats if b.get("stage"))]
    several = len(beats) > 1
    # Missed beacons only matter when the Wi-Fi link failed with them: on their own they're
    # common (a client saving power skips some) and nobody using the network notices, and with
    # the internet down beyond a gateway that still answers they're beside the point
    failed = [b for b in beats if b.get("stage") in WIFI_STAGES]
    shares = [b["beacons"] for b in failed if b.get("beacons") is not None and b["beacons"] < 0.6]
    if shares:
        words.append(f"only {min(shares):.0%} of beacons arriving")
    losses = sum(b.get("losses") or 0 for b in failed)
    if losses:
        words.append(f"beacon loss reported{f' ×{losses}' if several and losses > 1 else ''}")
    for key, label in (("gw", "gateway"), ("net", "internet")):
        slow = [b[key] for b in beats if (b.get(key) or 0) > SLOW_MS]
        if slow:
            words.append(f"{label} slow ({'up to ' if several and len(slow) > 1 else ''}"
                         f"{max(slow):.0f} ms)")
    return words


def band_name(freq_mhz: float) -> str:
    return "2.4 GHz" if freq_mhz < 3000 else "5 GHz" if freq_mhz < 5925 else "6 GHz"


def partner(reading, readings: list):
    """
    The same network's radio on another band, if it has one: a reading with the same SSID in
    another band, an access point of the same unit (a neighbouring BSSID) first, the
    strongest otherwise. None for a network on one band only.
    """
    band = band_name(reading.freq_mhz)
    others = [r for r in readings if r.ssid == reading.ssid and band_name(r.freq_mhz) != band]
    if not others:
        return None
    unit = {b.lower() for b in outage.siblings(reading.bssid, [r.bssid for r in others])}
    return max(others, key=lambda r: (r.bssid.lower() in unit, r.rssi))


def bands_compared(groups: list[tuple[str, list]], within: float = 60.0) -> list[str]:
    """
    What two radios of one network watched side by side say together. Dropping at the same
    moment points past the radios, to the unit, its power or what's behind it; one dropping
    alone points at that radio, its channel or its clients. ``groups`` is (band, incidents).
    """
    if len(groups) < 2:
        return []
    (first, ones), (second, others) = groups[0], groups[1]
    if not ones and not others:
        return []
    together = sum(1 for i in ones if any(abs(i.start - j.start) <= within for j in others))
    notes = []
    if together:
        notes.append(f"{together} time{'s' if together != 1 else ''} both bands dropped within a "
                     "minute of each other: look past the radios, at the access point itself, its "
                     "power, or the connection behind it.")
    for band, mine, theirs, other in ((first, ones, others, second), (second, others, ones, first)):
        alone = [i for i in mine if not any(abs(i.start - j.start) <= within for j in theirs)]
        if alone:
            notes.append(f"{len(alone)} time{'s' if len(alone) != 1 else ''} only {band} dropped "
                         f"while {other} stayed up: that radio, its channel, or the clients on it.")
    return notes


def share_adapters(freqs: list[float], adapters: list[dict]) -> list[tuple[str | None, str | None]]:
    """
    Which adapter joins, and which listens, on each of the radios at ``freqs`` (the first is
    the one chosen; the rest its other bands). ``adapters`` are dicts of "name", "freqs" (the
    channels it can tune), "monitor" (it does monitor mode) and "rank" (lower listens better).

    Joining needs no special adapter, so each band takes the one that can do least else: an
    adapter that only reaches 2.4 GHz joins there, leaving one that reaches both free for the
    other band, and the best listener stays free to listen. Listeners go out after, best
    first, the chosen radio first. A band nothing free can reach gets None.
    """
    def reaches(adapter, freq):
        return any(abs(f - freq) < 1 for f in adapter["freqs"])

    used: set[str] = set()
    joins = []
    for freq in freqs:
        able = [a for a in adapters if a["name"] not in used and reaches(a, freq)]
        best_listener = min((a["rank"] for a in able if a["monitor"]), default=None)
        able.sort(key=lambda a: (sum(reaches(a, f) for f in freqs), a["monitor"] and a["rank"] == best_listener,
                                 a["name"]))
        joins.append(able[0]["name"] if able else None)
        used |= {able[0]["name"]} if able else set()
    listens = []
    for freq in freqs:
        able = sorted((a for a in adapters if a["name"] not in used and a["monitor"] and reaches(a, freq)),
                      key=lambda a: (a["rank"], a["name"]))
        listens.append(able[0]["name"] if able else None)
        used |= {able[0]["name"]} if able else set()
    return list(zip(joins, listens))


def describe_siblings(target: Target) -> str:
    if not target.siblings:
        return "none found among the networks heard"
    bands = sorted({outage._band(f) for f in target.siblings.values()})
    return f"{len(target.siblings)} ({', '.join(bands)})"


def joined_channel(device: str) -> tuple[float, float, int] | None:
    """
    (primary MHz, centre MHz, width MHz) of the channel ``device`` is joined on, from ``iw dev
    info``, which needs no root and knows the width even when the scan didn't.
    """
    import re

    code, text = wifi.run("iw", "dev", device, "info", timeout=5)
    found = re.search(r"channel \d+ \((\d+) MHz\), width: (\d+) MHz(?:, center1: (\d+) MHz)?", text)
    if code != 0 or not found:
        return None
    primary, width = float(found.group(1)), int(found.group(2))
    return primary, float(found.group(3) or primary), width


class SsidMonitor:
    """
    Runs the watch. ``device`` joins; ``monitor_device`` (optional, put into monitor mode
    here) listens on the channel; ``analyzer`` (optional) dwells on it. ``changed`` is called
    from the worker threads whenever there's something new to show.
    """

    def __init__(self, target: Target, device: str, monitor_device: str | None = None,
                 analyzer=None, interval: float = 2.0, changed=None, label: str = "") -> None:
        self.target, self.device, self.interval = target, device, interval
        self.label = label                      # "2.4 GHz" when another band is watched too
        self.monitor_device = monitor_device
        self.analyzer = analyzer
        self.changed = changed or (lambda: None)
        self.started = 0.0
        self.log: SessionLog | None = None
        self.watch = None                       # beaconwatch.BeaconWatch
        self.events: linkwatch.IwEvents | None = None
        self.notes: dict[str, str] = {}         # source -> what's wrong with it
        self.state = "starting"                 # up, degraded, down, joining, starting, stopped
        self.status = "Starting…"
        self.lock = threading.RLock()
        self.beats: deque[dict] = deque(maxlen=BEAT_KEEP)
        self.beat_count = 0                     # ever taken, so the view copies only new ones
        self.channel_count = 0
        self.losses_total = 0
        self.channel: deque[dict] = deque(maxlen=BEAT_KEEP)
        self.traces: deque[dict] = deque(maxlen=WATERFALL_RECENT)
        self.minutes: deque[dict] = deque(maxlen=WATERFALL_MINUTES)
        self.incidents: list[Incident] = []
        self.current: Incident | None = None
        self.dns_stats: dict[str, list[int]] = {}   # server -> [asked, failed]
        self.tsf_known: dict[str, list] = {}    # bssid -> [time, tsf_us]
        self._stop = threading.Event()
        self._fails = 0
        self._first_fail = 0.0
        self._last_station: dict = {}
        self._last_station_time = 0.0
        self._last_load: dict = {}
        self._retries_counted = False           # some adapters leave "tx retries" at 0
        self._arp_held = False                  # counted in _own_arp until stopped
        self._fresh_interface: str | None = None  # monitor.fresh_interface's, while listening
        self._ifindex: int | None = None        # the joining adapter's, which changes if it drops out
        self._adapter_lost_at = 0.0
        self.adapter_resets: list[float] = []   # times it fell off the computer and came back
        self._own_mac = ""
        self._joined_at = 0.0
        self._pending: dict = {}                # the kernel's last word on a disconnect
        self._gateway: str | None = None
        self._dns: list[str] = []
        self._gateway_answers = False
        self._gw_misses = 0
        self._number = 0
        self._switched_monitor = False
        self._autoconnect_was: str | None = None
        self._minute: dict | None = None
        self._join_tries = 0
        self._next_join = 0.0
        self._next_scan = 0.0
        self._dns_job = None
        self._dns_last: dict = {}
        self._dns_all_at = 0.0
        # Forged deauthentications the adapter rejected (protected management frames at work)
        self.forged_blocked: list[float] = []

    # ---------- start and stop ----------

    def start(self) -> None:
        """Called on a worker thread: it joins the network, which can take half a minute."""
        self.started = time.time()
        connectivity.hold(self.device, HOLDER)
        self._release_from_autoconnect()
        self.log = SessionLog(self.target.ssid, self.target.bssid, label=self.label)
        self.log.write({"type": "target", "ssid": self.target.ssid, "bssid": self.target.bssid,
                        "freq": self.target.freq, "width": self.target.width,
                        "siblings": self.target.siblings, "device": self.device,
                        "monitor": self.monitor_device,
                        "analyzer": getattr(self.analyzer, "model", "") or getattr(self.analyzer, "name", None)})
        self._say("joining", f"Looking for {self.target.ssid}…")
        self._ifindex = _ifindex(self.device)
        _own_arp(True)
        self._arp_held = True
        try:
            self._own_mac = Path(f"/sys/class/net/{self.device}/address").read_text().strip().lower()
        except OSError:
            self._own_mac = ""
        self._scan_for_clocks(record=None)
        self._join()
        joined = joined_channel(self.device)
        if joined and abs(joined[0] - self.target.freq) < 1 and joined[2] >= 20:
            self.target.center, self.target.width = joined[1], joined[2]
        self.events = linkwatch.IwEvents(self.device, self._event)
        self.events.start()
        if self.events.error:
            self.notes["events"] = self.events.error
        if self.monitor_device:
            self._start_monitor()
        threading.Thread(target=self._beat_loop, daemon=True).start()
        if self.analyzer is not None:
            _share_analyzer(self)

    def stop(self) -> None:
        """Lets everything go: the adapter leaves the network, the monitor adapter goes back."""
        self._stop.set()
        with self.lock:
            if self.current is not None:
                self.current.end = self.current.end or time.time()
                self._finish(self.current)
        if self.events:
            self.events.stop()
        if self.watch:
            self.watch.stop()
        if self._fresh_interface:
            monitor.remove_interface(self._fresh_interface)
            self._fresh_interface = None
            if not self._switched_monitor and self.monitor_device:
                wifi.run("ip", "link", "set", self.monitor_device, "up")
        if self._switched_monitor and self.monitor_device:
            monitor.restore(self.monitor_device, lambda _text: None)
        connectivity.release(self.device)
        connectivity.leave(self.device, self.target.ssid)
        if self._arp_held:
            self._arp_held = False
            _own_arp(False)
        if self._autoconnect_was is not None:
            wifi.run("nmcli", "device", "set", self.device, "autoconnect", self._autoconnect_was)
        if self.log:
            self.log.write({"type": "stop"})
            self.log.close()
        self.state, self.status = "stopped", "Stopped."
        self.changed()

    @property
    def running(self) -> bool:
        return self.started > 0 and not self._stop.is_set()

    def _release_from_autoconnect(self) -> None:
        """
        Keeps NetworkManager from putting the adapter on another network while this one is
        down, which would hide the outage and spoil the evidence. Put back on stop.
        """
        code, text = wifi.run("nmcli", "-g", "GENERAL.AUTOCONNECT", "device", "show", self.device)
        if code == 0 and text.strip() in ("yes", "no"):
            self._autoconnect_was = text.strip()
            wifi.run("nmcli", "device", "set", self.device, "autoconnect", "no")

    def _start_monitor(self) -> None:
        from .beaconwatch import BeaconWatch

        device = self.monitor_device
        lines: list[str] = []
        try:
            if not monitor.is_monitor(device):
                if not monitor.enable(device, None, True, lines.append):
                    raise RuntimeError(lines[-1] if lines else "it wouldn't switch to monitor mode")
                self._switched_monitor = True
            middle = self.target.center if self.target.width > 20 else None

            def listen(on: str):
                watch = BeaconWatch(on, self.target.bssid, self.target.freq, self.target.width,
                                    middle, also=list(self.target.siblings))
                watch.start()
                return watch

            self.watch = listen(device)
            if not self._hears(self.watch):
                # Deaf where it's tuned (see monitor.fresh_interface): a new interface hears
                self.watch.stop()
                self.watch = None
                fresh = monitor.fresh_interface(device, lines.append)
                if fresh is None:
                    raise RuntimeError(f"it heard nothing on channel {self.target.channel}"
                                       + (f" ({lines[-1]})" if lines else ""))
                self._fresh_interface = fresh
                self.watch = listen(fresh)
        except Exception as error:
            self.watch = None
            self.notes["monitor"] = f"{device}: {error}"

    def _hears(self, watch, seconds: float = 4.0) -> bool:
        """Whether a monitor adapter hears anything at all: on a working channel it's dozens a second."""
        deadline = time.time() + seconds
        while time.time() < deadline and not self._stop.is_set():
            if watch.log.heard:
                return True
            time.sleep(0.25)
        return bool(watch.log.heard)

    # ---------- the kernel's events ----------

    def _event(self, event: dict) -> None:
        kind = event["kind"]
        with self.lock:
            if kind == "disconnected":
                self._pending.update(time=event["time"], by_ap=event["by_ap"],
                                     reason=event.get("reason"),
                                     locally=not event["by_ap"])
            elif kind in ("deauth", "disassoc"):
                ours = event["src"] == self.target.bssid.lower()
                self._pending.setdefault("frames", []).append(event)
                if ours:
                    self._pending.setdefault("time", event["time"])
            elif kind == "unprotected":
                # A forged frame the adapter ignored: worth a note even when nothing dropped
                self.forged_blocked.append(event["time"])
                del self.forged_blocked[:-1000]
                if self.current is not None and self.current.end is None:
                    self.current.unprotected_deauths += 1
            elif kind == "ch_switch":
                self._pending["ch_switch"] = event["freq"]
                if self.current is not None:
                    self.current.ch_switch_mhz = event["freq"]
            elif kind == "beacon_loss":
                self._pending["beacon_loss"] = event["time"]
        if self.log:
            self.log.write({"type": "event", "event": event})

    # ---------- the beat ----------

    def _beat_loop(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                self._beat()
            except Exception as error:      # one bad beat mustn't end a week's watch
                self.notes["beat"] = f"a beat failed: {error}"
            self.changed()
            next_at += self.interval
            self._stop.wait(max(0.05, next_at - time.monotonic()))
            next_at = max(next_at, time.monotonic() - self.interval)

    def _beat(self) -> None:
        now = time.time()
        self._number += 1
        self._check_adapter(now)
        st = linkwatch.station(self.device)
        associated = st.get("bssid") == self.target.bssid.lower()
        beat: dict = {"t": round(now, 2), "assoc": associated, "signal": st.get("signal"),
                      "beacon_loss": st.get("beacon_loss")}
        if associated and self._last_station.get("bssid") == st.get("bssid") and "beacon_rx" in st:
            elapsed = now - self._last_station_time
            interval_s = (st.get("beacon_interval") or 100) * 1.024e-3
            got = st["beacon_rx"] - self._last_station.get("beacon_rx", st["beacon_rx"])
            if elapsed > 0 and got >= 0:
                beat["beacons"] = round(min(1.0, got / (elapsed / interval_s)), 2)
            beat["losses"] = max(0, st.get("beacon_loss", 0) - self._last_station.get("beacon_loss", 0))
        if associated and self._last_station.get("bssid") == st.get("bssid"):
            # Not every adapter counts beacons (the RTL8188CUS doesn't), but they all give rates
            beat.update(self._link_quality(st))
        self._last_station, self._last_station_time = st, now
        beat.update(self._ap_load(now))
        if associated and not self._joined_at:
            self._joined_at = now

        stage, waiting = None, False
        if not associated:
            stage = "disconnect"
            self._joined_at = 0.0
            self._gateway = None
        else:
            if self._gateway is None or self._number % 30 == 0:
                self._gateway = connectivity.gateway(self.device)
                self._dns = linkwatch.dns_servers(self.device) or ([self._gateway] if self._gateway else [])
            if linkwatch.address(self.device) is None:
                if now - self._joined_at > DHCP_GRACE:
                    stage = "no_address"
                else:
                    waiting = True                  # just joined: DHCP gets its moment
            else:
                probe = linkwatch.beat(self.device, self._gateway, [], self._number)
                beat.update(gw=probe.get("gw_ms"), net=probe.get("net_ms"))
                if probe.get("gw_ms") is not None:
                    self._gateway_answers = True
                # One lost ping isn't a silent gateway: it takes two in a row
                self._gw_misses = 0 if probe.get("gw_ms") is not None else self._gw_misses + 1
                dns, fresh = self._check_dns()
                if fresh and "dns_ms" in dns:
                    beat["dns"] = dns["dns_ms"]
                if probe.get("net_ms") is None:
                    stage = ("no_gateway" if self._gateway is None or
                             (self._gw_misses >= 2 and self._gateway_answers) else "no_internet")
                elif dns.get("dns_error") and "dns_ms" not in dns:
                    stage = "dns"

        # Amber is for what someone using the network would notice: slow replies. Missed
        # beacons that don't stop the connection working are drawn, but not as a fault.
        slow = any((beat.get(k) or 0) > SLOW_MS for k in ("gw", "net"))
        beat["state"] = "joining" if waiting else "down" if stage else "degraded" if slow else "up"
        beat["stage"] = stage
        with self.lock:
            self.beats.append(beat)
            self.beat_count += 1
            self.losses_total += beat.get("losses") or 0
            down = None if waiting else self._track(stage, beat, now)
            current = self.current
            if current is not None and self._adapter_lost_at and \
                    abs(current.start - self._adapter_lost_at) < 30:
                current.adapter_lost = True
        if self.log:
            self.log.write({"type": "beat", **{k: v for k, v in beat.items() if v is not None}})
        if down is not None:
            # Scans and joins take seconds: never while holding the lock the view reads under
            self._while_down(down, now)

    def _check_adapter(self, now: float) -> None:
        """
        Notices the joining adapter dropping off the computer and coming back (a flaky USB port,
        cable or adapter): it comes back as a new interface, so its index changes. A disconnect
        then is the adapter's, not the network's.
        """
        index = _ifindex(self.device)
        if index is None or index == self._ifindex:
            return
        if self._ifindex is not None:
            self._adapter_lost_at = now
            self.adapter_resets.append(now)
            self.notes["adapter"] = (f"{self.device} dropped off the computer and came back "
                                     f"{len(self.adapter_resets)} time(s): a USB port, cable or "
                                     "adapter fault, not the network")
            if self.log:
                self.log.write({"type": "adapter_reset", "device": self.device})
            self._release_from_autoconnect()    # it came back as a new device, autoconnecting
        self._ifindex = index

    def _link_quality(self, st: dict) -> dict:
        """
        This adapter's link to the access point since the last beat: the rates it's using, the
        share of its transmissions that needed sending again, and how many it gave up on.
        """
        last = self._last_station
        found = {key: st[name] for key, name in (("tx_rate", "tx_bitrate"), ("rx_rate", "rx_bitrate"))
                 if st.get(name) is not None}
        if st.get("tx_retries"):
            self._retries_counted = True
        sent = st.get("tx_packets", 0) - last.get("tx_packets", st.get("tx_packets", 0))
        again = st.get("tx_retries", 0) - last.get("tx_retries", st.get("tx_retries", 0))
        if self._retries_counted and sent >= 10 and again >= 0:
            found["retries"] = round(again / (sent + again), 3)
        failed = st.get("tx_failed", 0) - last.get("tx_failed", st.get("tx_failed", 0))
        if failed > 0:
            found["tx_failed"] = failed
        return found

    def _ap_load(self, now: float) -> dict:
        """How loaded the access point is, from the monitor adapter: see ChannelLog.load."""
        if self.watch is None:
            return {}
        load = self.watch.log.load(now, self._own_mac)
        last, self._last_load = self._last_load, load
        found = {"heard": load["heard"], "networks": load["networks"], "fps": round(load["fps"], 1)}
        frames = load["data"] - last.get("data", load["data"])
        if frames >= 20:
            found["air_retry"] = round((load["retries"] - last.get("retries", 0)) / frames, 3)
        if load["reported"]:
            found["ap_clients"] = load["reported"]["stations"]
            found["ap_busy"] = load["reported"]["busy"]
        return found

    def _check_dns(self) -> tuple[dict, bool]:
        """
        DNS is looked up every few beats in the background, since dead servers each take
        seconds to give up on; the beat uses the latest answer (and whether it's new). About
        once a minute every server is asked, to find one that's dead behind one that answers.
        """
        job, fresh = self._dns_job, False
        if job is not None and job.done():
            fresh = True
            self._dns_job = None
            try:
                self._dns_last = {**job.result(), "time": time.time()}
            except Exception as error:
                self._dns_last = {"dns_error": str(error), "time": time.time()}
            for server, answered in self._dns_last.get("dns_tried", {}).items():
                asked_failed = self.dns_stats.setdefault(server, [0, 0])
                asked_failed[0] += 1
                asked_failed[1] += not answered
            if self._dns_last.get("dns_dead") and self.log:
                self.log.write({"type": "dns_failed", "servers": self._dns_last["dns_dead"]})
        if self._dns_job is None and self._dns and self._number % 5 == 0:
            every = time.time() - self._dns_all_at > 60
            if every:
                self._dns_all_at = time.time()
            self._dns_job = linkwatch._pool.submit(linkwatch.lookup_any, self.device,
                                                   list(self._dns), every)
        last = self._dns_last
        return (last if last and time.time() - last["time"] < 30 else {}), fresh

    def _track(self, stage: str | None, beat: dict, now: float) -> Incident | None:
        """
        The incident state machine, one beat at a time (holding the lock). Returns the
        incident when the link is down, for the caller to scan and rejoin for.
        """
        current = self.current
        if stage == "disconnect":
            self._fails = FAILS_TO_OPEN
            when = self._pending.get("time") or now - self.interval / 2
            if current is None or current.end is not None:
                if current is not None:
                    self._finish(current)
                current = self._open("disconnect", min(when, now))
            elif current.kind != "disconnect":
                current.kind = "disconnect"             # it got worse
            self._absorb_pending(current)
            self._say("down", f"Disconnected from {self.target.ssid} for "
                              f"{outage.duration(now - current.start)}")
            return current
        if stage:
            self._fails += 1
            if self._fails == 1:
                self._first_fail = beat["t"]
            if self._fails >= FAILS_TO_OPEN and (current is None or current.end is not None):
                if current is not None:
                    self._finish(current)
                current = self._open(stage, self._first_fail)
            words = {"no_internet": "Connected, but the internet isn't answering",
                     "no_gateway": "Connected, but the gateway isn't answering",
                     "no_address": "Connected, but no address from DHCP",
                     "dns": "Connected, but DNS isn't answering"}[stage]
            self._say("down", words + (f" for {outage.duration(now - current.start)}"
                                       if current is not None and current.end is None else ""))
            return None
        # This beat was fine
        self._fails = 0
        if current is not None and current.end is None:
            current.end = beat["t"]
            if current.kind == "disconnect" and current.associated_again is None:
                current.associated_again = beat["t"]
            self._say(beat["state"], f"Back after {outage.duration(current.end - current.start)}: "
                                     "gathering what happened…")
            self._pending = {}
        elif current is not None and now - current.end >= SETTLE:
            self._finish(current)
        elif current is None or current.end is not None:
            self._say(beat["state"], self._healthy_words(beat))
        self._remember_clock()
        return None

    def _healthy_words(self, beat: dict) -> str:
        if beat["state"] == "up":
            return f"Connected to {self.target.ssid} on channel {self.target.channel}"
        return "Connected, but " + ", ".join(trouble_words([beat]))

    def _open(self, kind: str, start: float) -> Incident:
        before = [b for b in self.beats if start - 20 <= b["t"] < start and b.get("signal")]
        baseline = [b for b in self.beats if start - 180 <= b["t"] < start - 30 and b.get("signal")]
        received = [b["beacons"] for b in self.beats
                    if start - 20 <= b["t"] < start and b.get("beacons") is not None]
        incident = Incident(
            id=len(self.incidents) + 1, start=start, kind=kind, ssid=self.target.ssid,
            bssid=self.target.bssid, channel=self.target.channel, freq_mhz=self.target.freq,
            signal_before=statistics.median(b["signal"] for b in before) if before else None,
            signal_baseline=statistics.median(b["signal"] for b in baseline) if baseline else None,
            beacons_before=statistics.mean(received) if received else None,
            beacon_losses_before=sum(b.get("losses") or 0 for b in self.beats
                                     if start - 30 <= b["t"] <= start + self.interval),
            tsf_before={k: list(v) for k, v in self.tsf_known.items()},
            sibling_bssids=list(self.target.siblings),
        )
        self.incidents.append(incident)
        self.current = incident
        self._join_tries = 0
        self._next_join = 0.0
        self._next_scan = 0.0
        if self.watch is not None and self.log is not None:
            path = self.log.folder / time.strftime(f"incident {incident.id} %H.%M.%S.pcap",
                                                   time.localtime(start))
            incident.pcap = str(path)
            # Thirty seconds of frames go to disk first: never while holding the lock the view
            # draws under, or the window stops answering
            threading.Thread(target=self._begin_capture, args=(incident, path), daemon=True).start()
        if self.log:
            self.log.write({"type": "open", "id": incident.id, "kind": kind, "start": start})
        return incident

    def _begin_capture(self, incident: Incident, path) -> None:
        watch = self.watch
        if watch is None or incident.diagnosis is not None:
            return
        try:
            watch.begin_capture(path)
        except Exception as error:
            incident.pcap = None
            self.notes["capture"] = f"couldn't save the frames: {error}"

    def _absorb_pending(self, incident: Incident) -> None:
        pending = self._pending
        if "by_ap" in pending:
            incident.by_ap = pending["by_ap"]
            incident.locally_generated = pending.get("locally")
            incident.reason = pending.get("reason")
        frames = [f for f in pending.get("frames", []) if f["src"] == self.target.bssid.lower()
                  and f["time"] >= incident.start - 30]
        if frames and incident.reason is None:
            incident.reason = frames[-1].get("reason")
            incident.by_ap = True if incident.by_ap is None else incident.by_ap
        recent = sum(1 for t in self.forged_blocked if incident.start - 30 <= t <= incident.start + 5)
        incident.unprotected_deauths = max(incident.unprotected_deauths, recent)
        if pending.get("ch_switch"):
            incident.ch_switch_mhz = pending["ch_switch"]

    def _while_down(self, incident: Incident, now: float) -> None:
        """Scans for the access point and its siblings, and tries to rejoin, in turn."""
        if now >= self._next_scan:
            self._next_scan = now + SCAN_EVERY
            self._scan_for_clocks(record=incident)
            self.changed()
        if now >= self._next_join:
            wait = JOIN_BACKOFF[min(self._join_tries, len(JOIN_BACKOFF) - 1)]
            self._join_tries += 1
            self._next_join = now + wait
            heard = (incident.scans[-1]["heard"] if incident.scans else {})
            if incident.scans and self.target.bssid.lower() not in heard:
                return                               # not there to join yet
            trouble = self._join(quiet=True)
            if trouble:
                incident.rejoin_errors.append(f"{time.strftime('%H:%M:%S')}: {trouble}")
                del incident.rejoin_errors[:-20]

    def _scan_for_clocks(self, record: Incident | None) -> None:
        """
        A quick scan of the access point's channel and its siblings': who's there, and their
        clocks. Only when the adapter isn't carrying the link (it would leave the channel).
        """
        freqs = sorted({int(self.target.freq), *(int(f) for f in self.target.siblings.values())})
        try:
            readings = wifi.scan(self.device, freqs=freqs)
        except Exception as error:
            self.notes["scan"] = f"couldn't scan while it was down: {error}"
            return
        wanted = {self.target.bssid.lower(), *(b.lower() for b in self.target.siblings)}
        heard = {r.bssid.lower(): {"freq": r.freq_mhz, "channel": r.channel, "rssi": r.rssi,
                                   "tsf_us": r.tsf_us or None}
                 for r in readings if r.bssid.lower() in wanted}
        now = time.time()
        found = next((r for r in readings if r.bssid.lower() == self.target.bssid.lower()), None)
        if found is not None and abs(found.freq_mhz - self.target.freq) > 1:
            self._follow(found)
        if record is not None:
            with self.lock:
                record.scans.append({"time": round(now, 2), "heard": heard})
                del record.scans[:-60]
        else:
            for bssid, info in heard.items():
                if info["tsf_us"]:
                    self.tsf_known[bssid] = [now, info["tsf_us"]]

    def _follow(self, reading) -> None:
        """The access point moved channel: the monitor adapter and the analyzer go with it."""
        old = self.target.channel
        self.target.freq = reading.freq_mhz
        self.target.center, self.target.width = reading.span_center_mhz, reading.width_mhz
        if self.log:
            self.log.write({"type": "moved", "from": old, "to": self.target.channel,
                            "freq": reading.freq_mhz, "width": reading.width_mhz})
        if self.watch is not None:
            middle = self.target.center if self.target.width > 20 else None
            self.watch.retune(self.target.freq, self.target.width, middle)

    def _remember_clock(self) -> None:
        """The monitor's live view of each radio's clock, for the next incident to compare with."""
        if self.watch is None:
            return
        for bssid, track in list(self.watch.log.tracks.items()):
            if track.last_tsf is not None and (bssid == self.target.bssid.lower()
                                               or bssid in {b.lower() for b in self.target.siblings}):
                self.tsf_known[bssid] = [track.last_time, track.last_tsf]

    def _join(self, quiet: bool = False) -> str | None:
        if not quiet:
            self._say("joining", f"Joining {self.target.ssid} on {self.target.bssid}…")
        trouble = connectivity.join(self.device, self.target.ssid, self.target.bssid, holder=HOLDER)
        if trouble and not quiet:
            self.notes["join"] = trouble
        elif not trouble:
            self.notes.pop("join", None)
        return trouble

    def _finish(self, incident: Incident) -> None:
        """Weighs the evidence and writes the incident down. Holding the lock."""
        now = time.time()
        if self.watch is not None:
            incident.monitor = self.watch.log.evidence(incident.start, incident.end, now)
            self.watch.end_capture()
        if self.analyzer is not None:
            incident.spectrum = self.channel_evidence(incident.start, incident.end or now)
        incident.diagnosis = outage.diagnose(incident, [i for i in self.incidents if i is not incident])
        if self.current is incident:
            self.current = None
        if self.log:
            self.log.write({"type": "incident", "incident": incident.to_json()})

    def provisional(self, incident: Incident) -> dict:
        """The diagnosis so far, for an incident still going on (no writing down)."""
        now = time.time()
        with self.lock:
            draft = Incident.from_json(incident.to_json())
            if self.watch is not None:
                draft.monitor = self.watch.log.evidence(incident.start, incident.end, now)
            if self.analyzer is not None:
                draft.spectrum = self.channel_evidence(incident.start, incident.end or now)
            return outage.diagnose(draft, [i for i in self.incidents if i is not incident])

    # ---------- the channel, through the analyzer ----------

    def _dwell_once(self) -> bool:
        """
        One dwell on this radio's channel, recorded. False once the analyzer can't serve this
        channel at all, so it isn't asked again.
        """
        from ..rf import spectrum as spec

        low, high = self.target.span               # read each time: the access point may move
        middle, width = (low + high) / 2, high - low
        analyzer = self.analyzer
        if analyzer is None or not analyzer.alive:
            self.notes["analyzer"] = "the analyzer stopped answering"
            self._stop.wait(5)
            return True
        if analyzer.max_mhz < high - 0.5:
            self.notes["analyzer"] = (f"the {outage_name(analyzer)} reaches {analyzer.max_mhz:.0f} MHz, "
                                      f"short of this channel ({high:.0f} MHz)")
            return False
        try:
            config, sweeps = analyzer.dwell(middle, 2.0, width)
            summary = spec.summarize_dwell(config, sweeps, middle, width)
        except Exception as error:
            self.notes["analyzer"] = str(error)
            self._stop.wait(2)
            return True
        self.notes.pop("analyzer", None)
        now = time.time()
        reading = {"t": round(now, 2), "airtime": summary.get("airtime"),
                   "power": summary.get("average_dbm"), "active": summary.get("active_dbm"),
                   "noise": summary.get("noise_20mhz")}
        trace = {"t": now, "start": config.start_mhz, "step": config.step_mhz,
                 "avg": summary["avg"], "max": summary["max"]}
        with self.lock:
            self.channel.append(reading)
            self.channel_count += 1
            self.traces.append(trace)
            self._fold_minute(trace)
        if self.log:
            self.log.write({"type": "channel", **{k: (round(v, 3) if isinstance(v, float) else v)
                                                  for k, v in reading.items() if v is not None}})
        self.changed()
        return True

    def _fold_minute(self, trace: dict) -> None:
        """One waterfall row a minute for the long view: the strongest level in each bin."""
        minute = int(trace["t"] // 60)
        row = self._minute
        if row is not None and row["minute"] == minute and len(row["max"]) == len(trace["max"]):
            row["max"] = [max(a, b) for a, b in zip(row["max"], trace["max"])]
            return
        self._minute = {"minute": minute, "t": minute * 60.0, "start": trace["start"],
                        "step": trace["step"], "max": list(trace["max"])}
        self.minutes.append(self._minute)

    def channel_evidence(self, start: float, end: float) -> dict | None:
        """The channel during an incident against the five minutes before it."""
        with self.lock:
            readings = list(self.channel)
        base = [r for r in readings if start - 300 <= r["t"] < start - 5]
        during = [r for r in readings if start <= r["t"] <= end]
        if not base or not during:
            return {"readings_before": len(base), "readings_during": len(during)}

        def mean(rows, key):
            values = [r[key] for r in rows if r.get(key) is not None]
            return statistics.mean(values) if values else None

        result = {"readings_before": len(base), "readings_during": len(during)}
        for key in ("airtime", "power", "noise"):
            result[f"{key}_before"] = mean(base, key)
            result[f"{key}_during"] = mean(during, key)
        air_b, air_d = result["airtime_before"] or 0, result["airtime_during"] or 0
        pow_b, pow_d = result["power_before"], result["power_during"]
        noise_b, noise_d = result["noise_before"], result["noise_during"]
        loud = []
        if air_d >= 0.6 and air_d >= air_b + 0.25:
            loud.append(f"the channel was busy {air_d:.0%} of the time, against {air_b:.0%} before")
        if noise_b is not None and noise_d is not None and noise_d >= noise_b + 8:
            loud.append(f"its noise floor rose {noise_d - noise_b:.0f} dB")
        if pow_b is not None and pow_d is not None and pow_d >= pow_b + 10 and air_d >= 0.3:
            loud.append(f"its power rose {pow_d - pow_b:.0f} dB")
        result["loud_during"] = bool(loud)
        result["loud_words"] = "the analyzer saw " + " and ".join(loud) if loud else ""
        result["quiet_during"] = (pow_b is not None and pow_d is not None and pow_d <= pow_b - 6
                                  and air_d <= max(0.05, air_b * 0.4))
        return result

    # ---------- for the view ----------

    def _say(self, state: str, text: str) -> None:
        self.state, self.status = state, text

    def snapshot(self, since: float) -> dict:
        """What the view draws, copied under the lock."""
        with self.lock:
            return {
                "beats": [b for b in self.beats if b["t"] >= since],
                "channel": [c for c in self.channel if c["t"] >= since],
                "trace": self.traces[-1] if self.traces else None,
                "incidents": list(self.incidents),
                "current": self.current,
            }

    def new_beats(self, seen: int) -> tuple[list[dict], list[dict], int, int]:
        """
        The beats and channel readings since the view last asked, ``seen`` being the counts it
        was given then, and the counts to pass next time. Copies only what's new.
        """
        with self.lock:
            beats = _tail(self.beats, self.beat_count - seen[0])
            channel = _tail(self.channel, self.channel_count - seen[1])
            return beats, channel, self.beat_count, self.channel_count

    def recent_beats(self, count: int) -> list[dict]:
        with self.lock:
            return _tail(self.beats, count)

    def unreliable_dns(self) -> list[tuple[str, int, int]]:
        """Servers that failed at least one lookup in five: (server, failed, asked)."""
        return [(server, failed, asked) for server, (asked, failed) in sorted(self.dns_stats.items())
                if asked >= 3 and failed / asked >= 0.2]

    def ap_uptime(self) -> float | None:
        """The access point radio's uptime, from its clock, when the monitor hears it."""
        if self.watch is None:
            return None
        track = self.watch.log.track
        return track.uptime_s(time.time()) if track and track.silent_for(time.time()) < 5 else None


# Several adapters on one network (both bands watched, or this computer's own connection on
# it too) each have an address on the same subnet. Linux lets any adapter answer ARP for any
# of the computer's addresses, so the gateway can learn the wrong adapter's MAC for a watch's
# address: its replies then go to the other adapter, every check fails, and NetworkManager,
# seeing the "conflict", leaves out the watch's routes. While watching, each adapter answers
# only for its own address (arp_ignore 1) and speaks from it (arp_announce 2); put back after.
ARP_SETTINGS = {"arp_ignore": "1", "arp_announce": "2"}
_arp_users = 0
_arp_before: dict[str, str] = {}
_arp_lock = threading.Lock()


def _own_arp(on: bool, root: Path = Path("/proc/sys/net/ipv4/conf/all")) -> None:
    """Counts the watches running, setting ARP_SETTINGS for the first and putting back after the last."""
    global _arp_users
    with _arp_lock:
        _arp_users = max(0, _arp_users + (1 if on else -1))
        if on and _arp_users == 1:
            for name, value in ARP_SETTINGS.items():
                try:
                    _arp_before[name] = (root / name).read_text().strip()
                    (root / name).write_text(value)
                except OSError:
                    pass                    # not root: checks through a second adapter may fail
        elif not on and _arp_users == 0:
            for name, value in _arp_before.items():
                try:
                    (root / name).write_text(value)
                except OSError:
                    pass
            _arp_before.clear()


# Watches sharing one analyzer (both bands of a network) take turns on it from one thread,
# which holds it for them all: otherwise the Spectrum page takes it between dwells, retunes it
# to a wide range, and every dwell waits out a slow sweep for nothing. Keyed by id(analyzer).
_shares: dict[int, list] = {}
_shares_lock = threading.Lock()


def _share_analyzer(watcher: SsidMonitor) -> None:
    """Adds a watch to its analyzer's turns, starting them if it's the first."""
    key = id(watcher.analyzer)
    with _shares_lock:
        members = _shares.get(key)
        if members is not None:
            members.append(watcher)
            return
        members = _shares[key] = [watcher]
    threading.Thread(target=_take_turns, args=(watcher.analyzer, key, members), daemon=True).start()


def _take_turns(analyzer, key: int, members: list) -> None:
    """A dwell for each watch in turn, for as long as any is still watching."""
    with analyzer.lock:
        while True:
            with _shares_lock:
                members[:] = [w for w in members if not w._stop.is_set()]
                if not members:
                    del _shares[key]
                    return
                turn = list(members)
            for watcher in turn:
                if not watcher._stop.is_set() and not watcher._dwell_once():
                    with _shares_lock:
                        if watcher in members:
                            members.remove(watcher)


def _ifindex(device: str) -> int | None:
    try:
        return int(Path(f"/sys/class/net/{device}/ifindex").read_text())
    except (OSError, ValueError):
        return None


def _tail(queue: deque, count: int) -> list:
    """The last ``count`` items of ``queue``, oldest first, without copying the rest."""
    count = min(count, len(queue))
    return list(itertools.islice(reversed(queue), count))[::-1] if count > 0 else []


def outage_name(analyzer) -> str:
    return getattr(analyzer, "model", "") or getattr(analyzer, "name", "analyzer")
