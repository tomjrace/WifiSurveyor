"""802.11 capture and what can be read out of the frames.

Scapy does the Radiotap and 802.11 parsing, including beacon information elements, so this
module is about what the frames mean: retries, airtime, who is talking, what the access
points advertise about themselves, and what in all that a network's owner should worry about.

Long captures go through tcpdump when it's installed: it writes the frames down a pipe, this
module saves every one of them to the capture file and analyses as many as it keeps up with,
so a busy channel never costs frames on disk and hours of capture never cost memory.
Everything here only listens; nothing is transmitted.
"""

from __future__ import annotations

import ctypes
import queue
import shutil
import signal
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.project import hand_back, make_dir
from . import oui

ACK_OVERHEAD_US = 44       # SIFS plus an acknowledgement at a basic rate
LOW_DISK_BYTES = 1 << 30   # a capture stops itself rather than fill the disk

SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2, "Info": 3}


@dataclass(slots=True)
class BssStat:
    bssid: str
    ssid: str = ""
    channel: int = 0
    freq_mhz: float = 0.0
    width_mhz: int = 0
    generation: str = ""
    security: str = ""
    ciphers: set[str] = field(default_factory=set)
    akms: set[str] = field(default_factory=set)
    pmf: str = ""                     # "required", "capable" or "off", for RSN networks
    wps: bool = False
    hidden: bool = False
    beacon_interval: int = 0
    reported_utilization: float | None = None
    reported_stations: int | None = None
    frames: int = 0
    beacons: int = 0
    retries: int = 0
    retry_eligible: int = 0
    airtime_us: float = 0.0
    clients: set[str] = field(default_factory=set)
    signal_sum: int = 0
    signal_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    described: bool = False

    @property
    def retry_rate(self) -> float:
        return self.retries / self.retry_eligible if self.retry_eligible else 0.0

    @property
    def avg_rssi(self) -> float | None:
        return self.signal_sum / self.signal_count if self.signal_count else None


@dataclass(slots=True)
class DeviceStat:
    """Anything that isn't an access point: phones, laptops, cameras, printers…"""
    mac: str
    frames: int = 0
    signal_sum: int = 0
    signal_count: int = 0
    # Its own transmissions: how much of the channel it takes, and how often it had to resend
    airtime_us: float = 0.0
    retries: int = 0
    bssids: set[str] = field(default_factory=set)
    probes: set[str] = field(default_factory=set)
    first_seen: float = 0.0
    last_seen: float = 0.0

    @property
    def avg_rssi(self) -> float | None:
        return self.signal_sum / self.signal_count if self.signal_count else None


@dataclass(slots=True)
class CaptureSummary:
    source: str = ""
    seconds: float = 0.0
    channel: int = 0
    frames: int = 0
    management: int = 0
    control: int = 0
    data: int = 0
    bad_fcs: int = 0
    retries: int = 0
    retry_eligible: int = 0
    deauths: int = 0
    disassocs: int = 0
    airtime_us: float = 0.0
    bss: list[BssStat] = field(default_factory=list)
    devices: list[DeviceStat] = field(default_factory=list)
    # (transmitter, receiver) → how many deauthentication or disassociation frames
    deauth_pairs: dict[tuple[str, str], int] = field(default_factory=dict)
    channels: dict[int, int] = field(default_factory=dict)

    @property
    def retry_rate(self) -> float:
        return self.retries / self.retry_eligible if self.retry_eligible else 0.0

    @property
    def airtime(self) -> float:
        return min(1.0, self.airtime_us / (self.seconds * 1e6)) if self.seconds else 0.0

    @property
    def clients(self) -> int:
        return len({mac for stat in self.bss for mac in stat.clients})


def available() -> bool:
    try:
        import scapy.all  # noqa: F401
        return True
    except Exception:
        return False


def have_tcpdump() -> bool:
    return shutil.which("tcpdump") is not None


# ---------- analysis ----------

class Analyzer:
    """Takes frames one at a time, so a capture can be read while it's still going."""

    def __init__(self, source: str = "") -> None:
        from scapy.layers.dot11 import (
            Dot11, Dot11AssoReq, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt,
            Dot11ProbeReq, Dot11ProbeResp, Dot11ReassoReq,
        )

        self._layers = (Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt, Dot11ProbeReq,
                        Dot11ProbeResp, (Dot11AssoReq, Dot11ReassoReq))
        self._lock = threading.Lock()
        self.source = source
        self.total = CaptureSummary(source=source)
        self.stats: dict[str, BssStat] = {}
        self.devices: dict[str, DeviceStat] = {}
        # Names heard for access points in probe responses and (re)association requests, by
        # BSSID: what names a hidden one (core.hidden)
        self.names: dict[str, str] = {}
        self.first = self.last = 0.0

    def add(self, packet) -> None:
        with self._lock:
            self._add(packet)

    def _add(self, packet) -> None:
        (Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt, Dot11ProbeReq,
         Dot11ProbeResp, joining) = self._layers
        summary = self.total
        summary.frames += 1
        when = float(getattr(packet, "time", 0) or 0)
        if when:
            self.first = min(self.first, when) if self.first else when
            self.last = max(self.last, when)
        if not packet.haslayer(Dot11):
            return
        dot11 = packet.getlayer(Dot11)
        radio = packet.getlayer("RadioTap")

        rate = _rate_mbps(radio)
        signal_ = getattr(radio, "dBm_AntSignal", None) if radio else None
        freq = getattr(radio, "ChannelFrequency", None) if radio else None
        if freq:
            from .wifi import channel_for
            channel = channel_for(float(freq))
            summary.channels[channel] = summary.channels.get(channel, 0) + 1

        flags = getattr(radio, "Flags", None) if radio else None
        if flags is not None and "badFCS" in str(flags):
            summary.bad_fcs += 1
            return

        length = len(packet)
        kind = dot11.type
        if kind == 0:
            summary.management += 1
        elif kind == 1:
            summary.control += 1
        else:
            summary.data += 1

        unicast = bool(dot11.addr1) and not _is_group(dot11.addr1)
        expects_ack = unicast and kind != 1
        airtime = _airtime_us(length, rate, expects_ack)
        summary.airtime_us += airtime
        retry = kind != 1 and bool(dot11.FCfield & 0x08)
        if kind != 1:
            summary.retry_eligible += 1
            summary.retries += retry

        transmitter = (dot11.addr2 or "").lower()
        receiver = (dot11.addr1 or "").lower()
        if packet.haslayer(Dot11Deauth) or packet.haslayer(Dot11Disas):
            if packet.haslayer(Dot11Deauth):
                summary.deauths += 1
            else:
                summary.disassocs += 1
            key = (transmitter, receiver)
            summary.deauth_pairs[key] = summary.deauth_pairs.get(key, 0) + 1

        if packet.haslayer(Dot11ProbeReq) and transmitter and not _is_group(transmitter):
            device = self._device(transmitter, when, signal_)
            device.airtime_us += airtime
            element = packet.getlayer(Dot11Elt)
            if element is not None and element.ID == 0 and element.info:
                device.probes.add(bytes(element.info).decode("utf-8", "replace"))
            return

        bssid = (_bssid(dot11) or "").lower()
        if not bssid or _is_group(bssid):
            return
        stat = self.stats.get(bssid)
        if stat is None:
            stat = self.stats[bssid] = BssStat(bssid=bssid, first_seen=when)
        stat.frames += 1
        stat.last_seen = when or stat.last_seen
        stat.airtime_us += airtime
        if kind != 1:
            stat.retry_eligible += 1
            stat.retries += retry
        if transmitter == bssid:
            if signal_ is not None:
                stat.signal_sum += int(signal_)
                stat.signal_count += 1
            if freq and kind == 0:
                stat.freq_mhz = float(freq)
        for address in (receiver, transmitter):
            if address and not _is_group(address) and address != bssid:
                stat.clients.add(address)
        if transmitter and transmitter != bssid and not _is_group(transmitter):
            device = self._device(transmitter, when, signal_)
            device.bssids.add(bssid)
            device.airtime_us += airtime
            device.retries += retry

        beacon = packet.haslayer(Dot11Beacon)
        if beacon:
            stat.beacons += 1
            stat.beacon_interval = int(packet.getlayer(Dot11Beacon).beacon_interval or 0)
            # Its beacons are what say it hides, even when a probe response named it first
            if not stat.hidden and not _ssid_element(packet, Dot11Elt):
                stat.hidden = True
        if (beacon or packet.haslayer(Dot11ProbeResp)) and not stat.described:
            _read_beacon(packet, stat, Dot11Elt, Dot11Beacon if beacon else Dot11ProbeResp)
        if packet.haslayer(Dot11ProbeResp) and transmitter == bssid:
            # A hidden network names itself when it answers a client that asked for it
            name = _ssid_element(packet, Dot11Elt)
            if name:
                self.names[bssid] = name
        elif any(packet.haslayer(layer) for layer in joining) and receiver == bssid:
            # A device joining asks for the network by name
            name = _ssid_element(packet, Dot11Elt)
            if name:
                self.names[bssid] = name
        if stat.hidden and bssid in self.names:
            stat.ssid = self.names[bssid]


    def _device(self, mac: str, when: float, signal_) -> DeviceStat:
        device = self.devices.get(mac)
        if device is None:
            device = self.devices[mac] = DeviceStat(mac=mac, first_seen=when)
        device.frames += 1
        device.last_seen = when or device.last_seen
        if signal_ is not None:
            device.signal_sum += int(signal_)
            device.signal_count += 1
        return device

    def summary(self, seconds: float | None = None) -> CaptureSummary:
        """A snapshot that's safe to hand to the window while frames keep arriving."""
        import copy

        with self._lock:
            result = copy.copy(self.total)
            result.seconds = seconds if seconds else max(0.05, self.last - self.first)
            known = set(self.stats)
            result.bss = sorted((copy.copy(s) for s in self.stats.values()),
                                key=lambda s: s.airtime_us, reverse=True)
            for stat in result.bss:
                stat.clients = set(stat.clients) - known
                stat.ciphers, stat.akms = set(stat.ciphers), set(stat.akms)
            result.devices = [copy.copy(d) for mac, d in self.devices.items() if mac not in known]
            for device in result.devices:
                device.bssids, device.probes = set(device.bssids), set(device.probes)
            result.deauth_pairs = dict(self.total.deauth_pairs)
            result.channels = dict(self.total.channels)
        if result.channels:
            result.channel = max(result.channels, key=result.channels.get)
        return result


def analyze(packets: list, source: str) -> CaptureSummary:
    analyzer = Analyzer(source)
    for packet in packets:
        analyzer.add(packet)
    return analyzer.summary()


def read_pcap(path: str) -> list:
    from scapy.all import rdpcap

    return list(rdpcap(path))


def analyze_file(path: str, progress: Callable[[int], None] | None = None) -> CaptureSummary:
    """Reads a capture a frame at a time, so a file from a long capture fits in memory."""
    from scapy.utils import PcapReader

    analyzer = Analyzer(Path(path).name)
    with PcapReader(path) as reader:
        for count, packet in enumerate(reader, start=1):
            analyzer.add(packet)
            if progress and count % 5000 == 0:
                progress(count)
    return analyzer.summary()


# ---------- what's worth worrying about ----------

@dataclass(slots=True)
class Finding:
    severity: str          # High, Medium, Low, Info
    title: str
    detail: str


def findings(summary: CaptureSummary) -> list[Finding]:
    found: list[Finding] = []
    names = {stat.bssid: stat.ssid or "(hidden)" for stat in summary.bss}
    minutes = max(summary.seconds / 60, 1 / 60)

    # Deauthentication and disassociation: a handful is normal, a stream is an attack
    total = summary.deauths + summary.disassocs
    if total:
        rate = total / minutes
        top = sorted(summary.deauth_pairs.items(), key=lambda item: item[1], reverse=True)[:3]
        who = "; ".join(f"{_mac_name(tx, names)} → {'everyone' if _is_group(rx) else _mac_name(rx, names)}"
                        f" ×{count}" for (tx, rx), count in top)
        if total >= 20 and rate >= 10:
            found.append(Finding("High", "Possible deauthentication attack",
                                 f"{total} deauth/disassoc frames, {rate:.0f} a minute. Busiest: {who}. "
                                 "A flood like this knocks clients off their network. Protected management "
                                 "frames (802.11w) stop it."))
        elif any(_is_group(rx) for (_tx, rx) in summary.deauth_pairs):
            found.append(Finding("Medium", "Broadcast deauthentication",
                                 f"Deauth sent to every client: {who}. Legitimate access points "
                                 "rarely do this outside a reboot."))
        else:
            found.append(Finding("Info", "Deauthentications",
                                 f"{total} deauth/disassoc frames ({rate:.1f} a minute): {who}."))

    by_ssid: dict[str, list[BssStat]] = {}
    for stat in summary.bss:
        if not stat.described:
            continue
        label = f"{stat.ssid or '(hidden)'} ({stat.bssid.upper()})"
        if stat.ssid:
            by_ssid.setdefault(stat.ssid, []).append(stat)
        if stat.security == "WEP":
            found.append(Finding("High", "WEP encryption", f"{label}: WEP has been broken for years; replace it with WPA2 or WPA3."))
        elif stat.security == "Open":
            found.append(Finding("Medium", "Open network",
                                 f"{label}: no encryption, so anyone nearby can read its traffic."))
        if "TKIP" in stat.ciphers:
            found.append(Finding("High" if stat.ciphers == {"TKIP"} else "Medium", "TKIP allowed",
                                 f"{label}: TKIP is deprecated and weak; use CCMP (AES) only."))
        if stat.wps:
            found.append(Finding("Medium", "WPS enabled",
                                 f"{label}: WPS PIN setup has known weaknesses. Turn WPS off "
                                 "unless it's needed."))
        personal = stat.akms & {"PSK", "PSK-SHA256"}
        if "SAE" in stat.akms and personal:
            found.append(Finding("Low", "WPA3 transition mode",
                                 f"{label}: offers WPA2 as well as WPA3, so a client can be pushed "
                                 "down to WPA2. Move to WPA3-only once every client supports it."))
        if stat.pmf == "off" and stat.akms:
            found.append(Finding("Low", "Management frames unprotected",
                                 f"{label}: no 802.11w, so forged deauths can disconnect its clients."))
        if stat.hidden:
            found.append(Finding("Info", "Hidden SSID",
                                 f"{stat.bssid.upper()}: hides its name, which adds no security; "
                                 "clients reveal it when they connect."))
        if stat.retry_eligible > 200 and stat.retry_rate > 0.25:
            found.append(Finding("Low", "High retry rate",
                                 f"{label}: {stat.retry_rate * 100:.0f}% of frames retried, "
                                 "a sign of interference or weak links."))
        if stat.reported_utilization is not None and stat.reported_utilization > 0.7:
            found.append(Finding("Low", "Busy channel",
                                 f"{label} reports its channel busy "
                                 f"{stat.reported_utilization * 100:.0f}% of the time."))

    # One name, advertised differently: a misconfiguration or an evil twin
    for ssid, stats in by_ssid.items():
        securities = {s.security for s in stats}
        makers = {oui.vendor(s.bssid, access_point=True).replace(" (virtual BSSID)", "")
                  for s in stats} - {"", "Virtual BSSID"}
        where = ", ".join(f"{s.bssid.upper()} {s.security}" for s in stats[:6])
        if len(securities) > 1:
            found.append(Finding("High", "Same SSID, different security",
                                 f"{ssid}: {where}. One of these may be an impostor (evil twin)."))
        elif len(makers) > 1:
            found.append(Finding("Medium", "Same SSID from different manufacturers",
                                 f"{ssid}: {', '.join(sorted(makers))}. Fine for a mixed deployment; "
                                 "otherwise check for a rogue access point."))

    probing = [d for d in summary.devices if d.probes]
    if probing:
        found.append(Finding("Info", "Devices naming networks they know",
                             f"{len(probing)} devices sent probe requests naming saved networks, "
                             "which reveals where they've been and invites evil-twin attacks. "
                             "See Probes."))
    if summary.frames > 500 and summary.bad_fcs / summary.frames > 0.15:
        found.append(Finding("Low", "Many damaged frames",
                             f"{summary.bad_fcs / summary.frames * 100:.0f}% failed their checksum: "
                             "interference, or the capture radio is too far away."))
    return sorted(found, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))


def _mac_name(mac: str, names: dict[str, str]) -> str:
    return f"{names[mac]} ({mac.upper()})" if mac in names else mac.upper()


# ---------- live capture ----------

def _die_with_parent() -> None:
    """Runs in the child: if the app goes away, so does tcpdump."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except OSError:
        pass


class LiveCapture:
    """
    Captures to ``path`` until ``seconds`` pass (None: until stopped), analysing as it goes.
    ``hop`` is a list of (frequency, None, None) to go round, or ``tune`` is called once by the
    caller before starting; ``bpf`` narrows what's kept.
    """

    def __init__(self, device: str, path: Path, seconds: float | None, bpf: str = "",
                 hop: list[float] | None = None, dwell: float = 0.5) -> None:
        self.device, self.path, self.seconds, self.bpf = device, Path(path), seconds, bpf
        self.hop, self.dwell = hop or [], dwell
        self.analyzer = Analyzer(self.path.name)
        self.stop_event = threading.Event()
        self.started = 0.0
        self.written = 0            # frames saved to the file
        self.skipped = 0            # frames saved but not analysed, when analysis fell behind
        self.error = ""
        self.ended_because = ""
        self._process: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=20000)
        self._threads: list[threading.Thread] = []

    # --- control ---

    def start(self) -> None:
        make_dir(self.path.parent)
        self.started = time.monotonic()
        if have_tcpdump():
            command = ["tcpdump", "-i", self.device, "-Z", "root", "-U", "-s", "0", "-w", "-"]
            if self.bpf:
                command.append(self.bpf)
            self._process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, preexec_fn=_die_with_parent)
            self._spawn(self._read_pipe)
        else:
            self._spawn(self._sniff_with_scapy)
        self._spawn(self._analyse)
        self._spawn(self._watch)
        if len(self.hop) > 1:
            self._spawn(self._hopper)

    def stop(self, because: str = "") -> None:
        if not self.ended_because:
            self.ended_because = because
        self.stop_event.set()
        process = self._process
        if process and process.poll() is None:
            process.terminate()

    def wait(self, timeout: float = 10) -> None:
        for thread in self._threads:
            thread.join(timeout)
        hand_back(self.path)

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.started else 0.0

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def summary(self) -> CaptureSummary:
        return self.analyzer.summary()

    # --- workers ---

    def _spawn(self, target) -> None:
        thread = threading.Thread(target=target, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _read_pipe(self) -> None:
        """Saves every frame tcpdump sends, and queues it for analysis if there's room."""
        process = self._process
        pipe = process.stdout
        try:
            with self.path.open("wb") as out:
                header = _read_exactly(pipe, 24)
                if len(header) < 24:
                    raise RuntimeError(_tcpdump_error(process))
                out.write(header)
                endian = "<" if header[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
                nanos = header[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
                link = struct.unpack(endian + "I", header[20:24])[0]
                while True:
                    record = _read_exactly(pipe, 16)
                    if len(record) < 16:
                        break
                    seconds, fraction, length, _original = struct.unpack(endian + "IIII", record)
                    data = _read_exactly(pipe, length)
                    if len(data) < length:
                        break
                    out.write(record)
                    out.write(data)
                    self.written += 1
                    when = seconds + fraction / (1e9 if nanos else 1e6)
                    try:
                        self._queue.put_nowait((link, when, data))
                    except queue.Full:
                        self.skipped += 1
                    if self.written % 200 == 0:
                        out.flush()
        except Exception as error:
            self.error = str(error)
        finally:
            self.stop_event.set()
            self._queue.put(None)

    def _sniff_with_scapy(self) -> None:
        """Without tcpdump: Scapy captures and writes the file itself."""
        try:
            from scapy.all import AsyncSniffer
            from scapy.utils import PcapWriter

            writer = PcapWriter(str(self.path), linktype=127, sync=False)

            def keep(packet) -> None:
                writer.write(packet)
                self.written += 1
                try:
                    self._queue.put_nowait(packet)
                except queue.Full:
                    self.skipped += 1

            sniffer = AsyncSniffer(iface=self.device, prn=keep, store=False, monitor=True,
                                   filter=self.bpf or None)
            sniffer.start()
            self.stop_event.wait()
            try:
                sniffer.stop()
            except Exception:
                pass
            writer.close()
        except Exception as error:
            self.error = str(error)
        finally:
            self.stop_event.set()
            self._queue.put(None)

    def _analyse(self) -> None:
        from scapy.layers.dot11 import Dot11, RadioTap

        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                if isinstance(item, tuple):
                    link, when, data = item
                    packet = RadioTap(data) if link == 127 else Dot11(data)
                    packet.time = when
                else:
                    packet = item
                self.analyzer.add(packet)
            except Exception:
                self.skipped += 1       # a frame Scapy can't make sense of

    def _watch(self) -> None:
        """Ends the capture on time, or early when the disk is nearly full."""
        while not self.stop_event.wait(1.0):
            if self.seconds and self.elapsed >= self.seconds:
                self.stop("finished")
                return
            try:
                if shutil.disk_usage(self.path.parent).free < LOW_DISK_BYTES:
                    self.stop("stopped: less than 1 GB of disk left")
                    return
            except OSError:
                pass

    def _hopper(self) -> None:
        from .monitor import tune

        index = 0
        while not self.stop_event.is_set():
            tune(self.device, self.hop[index % len(self.hop)])
            index += 1
            self.stop_event.wait(self.dwell)


def _read_exactly(pipe, count: int) -> bytes:
    chunks, needed = [], count
    while needed:
        chunk = pipe.read(needed)
        if not chunk:
            break
        chunks.append(chunk)
        needed -= len(chunk)
    return b"".join(chunks)


def _tcpdump_error(process: subprocess.Popen) -> str:
    try:
        process.wait(3)
        text = process.stderr.read().decode("utf-8", "replace").strip()
    except Exception:
        text = ""
    lines = [line for line in text.splitlines() if "listening on" not in line]
    return lines[-1] if lines else "tcpdump stopped before capturing anything."


def survey(device: str, freqs: list[float], dwell: float = 0.3,
           progress: Callable[[int, int], None] | None = None,
           stop: threading.Event | None = None) -> list[BssStat]:
    """
    Goes round the channels on a monitor interface listening for beacons, so the access points
    around can be listed without a second radio. Nothing is sent.
    """
    from scapy.all import AsyncSniffer

    from .monitor import tune

    analyzer = Analyzer(device)
    sniffer = AsyncSniffer(iface=device, prn=analyzer.add, store=False, monitor=True,
                           filter="type mgt subtype beacon or type mgt subtype probe-resp")
    sniffer.start()
    waiter = stop or threading.Event()
    try:
        for number, freq in enumerate(freqs, start=1):
            if waiter.is_set():
                break
            tune(device, freq)
            if progress:
                progress(number, len(freqs))
            waiter.wait(dwell)
    finally:
        try:
            sniffer.stop()
        except Exception:
            pass
    return [stat for stat in analyzer.summary().bss if stat.described and stat.freq_mhz]


def hidden_names(summary: CaptureSummary) -> dict[str, str]:
    """The hidden access points a capture heard named: BSSID (upper case) → name."""
    return {stat.bssid.upper(): stat.ssid for stat in summary.bss if stat.hidden and stat.ssid}


def listen_for_names(device: str, channels: list[tuple[float, int, float]], seconds: float,
                     say: Callable[[str], None] | None = None,
                     stop: threading.Event | None = None) -> dict[str, str]:
    """
    Parks on each of ``channels`` (frequency, width, centre) for ``seconds`` on a monitor
    interface, listening for a hidden access point to say its name: a device joining it asks
    for it by name, and the access point answers with it. Returns BSSID (upper case) → name
    for the hidden ones heard named. Only management frames are kept. Nothing is sent.
    """
    from scapy.all import AsyncSniffer

    from .monitor import tune

    analyzer = Analyzer(device)
    sniffer = AsyncSniffer(iface=device, prn=analyzer.add, store=False, monitor=True,
                           filter="type mgt")
    sniffer.start()
    waiter = stop or threading.Event()
    try:
        for number, (freq, width, centre) in enumerate(channels, start=1):
            if waiter.is_set():
                break
            tune(device, freq, width, centre)
            if say:
                say(f"Listening for hidden network names on {freq:.0f} MHz "
                    f"({number} of {len(channels)})…")
            waiter.wait(seconds)
    finally:
        try:
            sniffer.stop()
        except Exception:
            pass
    return hidden_names(analyzer.summary())


class Listener:
    """
    Listens on a monitor interface without saving anything, for the Discover page: goes round
    ``channels()`` (each (frequency, width, centre), asked afresh every lap, so it follows the
    latest scan), ``dwell`` seconds on each, and keeps what the frames say about who is
    there. Control frames (acknowledgements and the like) are left out: they name nobody new
    and are most of a busy channel. Nothing is sent.
    """

    def __init__(self, device: str, channels: Callable[[], list[tuple[float, int, float]]],
                 dwell: float = 0.5) -> None:
        self.device, self.channels, self.dwell = device, channels, dwell
        self.analyzer = Analyzer(device)
        self.stop_event = threading.Event()
        self.error = ""
        self._sniffer = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from scapy.all import AsyncSniffer

        self._sniffer = AsyncSniffer(iface=self.device, prn=self.analyzer.add, store=False,
                                     monitor=True, filter="type mgt or type data")
        self._sniffer.start()
        self._thread = threading.Thread(target=self._hop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        sniffer = self._sniffer
        if sniffer is not None:
            try:
                sniffer.stop(join=False)
            except Exception:
                pass

    @property
    def running(self) -> bool:
        sniffer = self._sniffer
        if sniffer is not None and not sniffer.running and not self.stop_event.is_set():
            self.error = str(getattr(sniffer, "exception", "") or "the capture stopped")
        return not self.stop_event.is_set() and sniffer is not None and sniffer.running

    def summary(self) -> CaptureSummary:
        return self.analyzer.summary()

    def _hop(self) -> None:
        from .monitor import tune

        while not self.stop_event.is_set():
            lap = self.channels() or []
            if not lap:
                self.stop_event.wait(1.0)
            for freq, width, centre in lap:
                if self.stop_event.is_set():
                    return
                tune(self.device, freq, width, centre)
                self.stop_event.wait(self.dwell)


# ---------- reading frames ----------

def _ssid_element(packet, Dot11Elt) -> str:
    """The network name a frame carries, or "" when it has none or hides it."""
    element = packet.getlayer(Dot11Elt)
    while isinstance(element, Dot11Elt):
        if element.ID == 0:
            info = bytes(element.info or b"")
            return info.decode("utf-8", "replace") if info.strip(b"\x00 ") else ""
        element = element.payload.getlayer(Dot11Elt)
    return ""


def _read_beacon(packet, stat: BssStat, Dot11Elt, frame_layer) -> None:
    """Pulls what the access point advertises about itself out of a beacon or probe response."""
    stat.described = True
    ht = vht = he = eht = False
    rsn = wpa = False
    element = packet.getlayer(Dot11Elt)
    while isinstance(element, Dot11Elt):
        number, info = element.ID, bytes(element.info)
        if number == 0:
            stat.ssid = info.decode("utf-8", "replace").rstrip("\x00")
            stat.hidden = not stat.ssid.strip("\x00 ")
            if stat.hidden:
                stat.ssid = ""
        elif number == 3 and info:
            stat.channel = info[0]
        elif number == 11 and len(info) >= 5:
            # BSS Load: the access point's own view of how busy the channel is
            stat.reported_stations = int.from_bytes(info[0:2], "little")
            stat.reported_utilization = info[2] / 255
        elif number == 45 and len(info) >= 2:
            ht = True
            stat.width_mhz = 40 if info[0] & 0x02 else 20
        elif number == 48:
            rsn = True
            _read_rsn(info, stat)
        elif number == 191:
            vht = True
            stat.width_mhz = max(stat.width_mhz, 80)
        elif number == 192 and info:
            stat.width_mhz = {1: 80, 2: 160, 3: 160}.get(info[0], stat.width_mhz or 40)
        elif number == 221 and info[:4] == b"\x00\x50\xf2\x01":
            wpa = True
            _read_rsn(info[4:], stat, legacy=True)
        elif number == 221 and info[:4] == b"\x00\x50\xf2\x04":
            stat.wps = True
        elif number == 255 and info:
            if info[0] in (35, 36):
                he = True
            if info[0] in (106, 108):
                eht = True
        element = element.payload.getlayer(Dot11Elt)

    frame = packet.getlayer(frame_layer)
    privacy = "privacy" in str(getattr(frame, "cap", ""))
    stat.width_mhz = stat.width_mhz or 20
    stat.generation = (
        "Wi-Fi 7 (be)" if eht else "Wi-Fi 6/6E (ax)" if he
        else "Wi-Fi 5 (ac)" if vht else "Wi-Fi 4 (n)" if ht else "legacy"
    )
    if rsn or wpa:
        stat.security = _security_name(stat.akms, wpa and not rsn)
    else:
        stat.security = "WEP" if privacy else "Open"


AKM_NAMES = {1: "802.1X", 2: "PSK", 3: "FT-802.1X", 4: "FT-PSK", 5: "802.1X-SHA256",
             6: "PSK-SHA256", 8: "SAE", 9: "FT-SAE", 11: "802.1X-SuiteB", 12: "802.1X-SuiteB-192",
             18: "OWE", 24: "SAE-EXT"}
CIPHER_NAMES = {1: "WEP-40", 2: "TKIP", 4: "CCMP", 5: "WEP-104", 8: "GCMP", 9: "GCMP-256",
                10: "CCMP-256"}


def _read_rsn(info: bytes, stat: BssStat, legacy: bool = False) -> None:
    """Ciphers, key management and 802.11w from an RSN (or old WPA) element."""
    try:
        pos = 2                                            # version
        stat.ciphers.add(CIPHER_NAMES.get(info[pos + 3], f"cipher {info[pos + 3]}"))
        pos += 4
        count = int.from_bytes(info[pos:pos + 2], "little")
        pos += 2
        for _ in range(count):
            stat.ciphers.add(CIPHER_NAMES.get(info[pos + 3], f"cipher {info[pos + 3]}"))
            pos += 4
        count = int.from_bytes(info[pos:pos + 2], "little")
        pos += 2
        for _ in range(count):
            stat.akms.add(AKM_NAMES.get(info[pos + 3], f"AKM {info[pos + 3]}"))
            pos += 4
        if not legacy:
            capabilities = int.from_bytes(info[pos:pos + 2], "little") if pos + 2 <= len(info) else 0
            stat.pmf = ("required" if capabilities & 0x40 else
                        "capable" if capabilities & 0x80 else "off")
    except IndexError:
        pass


def _security_name(akms: set[str], legacy_wpa: bool) -> str:
    if legacy_wpa:
        return "WPA"
    enterprise = any("802.1X" in akm for akm in akms)
    sae = any(akm.startswith(("SAE", "FT-SAE")) for akm in akms)
    psk = any(akm in ("PSK", "FT-PSK", "PSK-SHA256") for akm in akms)
    if "OWE" in akms:
        return "OWE"
    if sae and psk:
        return "WPA2/WPA3-Personal"
    if sae:
        return "WPA3-Personal (SAE)"
    if enterprise:
        return "WPA3-Enterprise" if any("SuiteB" in akm for akm in akms) else "WPA2-Enterprise"
    return "WPA2-Personal" if psk else "RSN"


def _rate_mbps(radio) -> float:
    if radio is None:
        return 6.0
    rate = getattr(radio, "Rate", None)
    if rate:
        return float(rate) / 2 if float(rate) > 3 else float(rate)
    mcs = getattr(radio, "MCS_index", None)
    if mcs is not None:
        base = [6.5, 13, 19.5, 26, 39, 52, 58.5, 65]
        index = int(mcs) % 8
        streams = 1 + int(mcs) // 8
        width = 2.077 if getattr(radio, "MCS_bandwidth", 0) == 1 else 1
        return base[index] * streams * width
    return 6.0


def _airtime_us(length: int, rate_mbps: float, expects_ack: bool) -> float:
    rate = rate_mbps if rate_mbps > 0 else 6.0
    preamble = 192.0 if rate <= 11 else 20.0
    return preamble + length * 8 / rate + (ACK_OVERHEAD_US if expects_ack else 0)


def _bssid(dot11) -> str | None:
    to_ds, from_ds = dot11.FCfield & 0x01, dot11.FCfield & 0x02
    if to_ds and not from_ds:
        return dot11.addr1
    if from_ds and not to_ds:
        return dot11.addr2
    return dot11.addr3


def _is_group(mac: str) -> bool:
    try:
        return int(mac.split(":")[0], 16) % 2 == 1
    except (ValueError, AttributeError):
        return False
