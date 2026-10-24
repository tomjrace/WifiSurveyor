"""
A monitor-mode adapter parked on the watched access point's channel, listening only.

It hears what the connected adapter can't: every beacon with the access point's clock, so a
radio that restarts or goes quiet shows; every deauthentication and disassociation frame on the
channel, from anyone to anyone, with its signal level and sequence number, so a forged one
shows; and the clients coming back afterwards, so "all clients" can be counted rather than
assumed. Facts go to a core.outage.ChannelLog.

The last 30 seconds of frames are kept in memory, so when something happens the capture saved
for it starts before it did.

A busy channel brings thousands of frames a second, and Scapy takes about a millisecond to
dissect each, all while holding the interpreter lock the window needs to draw. So every frame's
header is read straight from its bytes first, and only the few that say something (the access
point's beacons, deauthentications, joins) are dissected.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

from ..core.outage import ChannelLog, Deauth
from . import monitor

LOOKBACK = 30.0


def _elements(packet, Dot11Elt) -> dict[int, bytes]:
    found: dict[int, bytes] = {}
    element = packet.getlayer(Dot11Elt)
    while isinstance(element, Dot11Elt):
        found.setdefault(int(element.ID), bytes(element.info or b""))
        element = element.payload.getlayer(Dot11Elt)
    return found


def _pmf(rsn: bytes | None) -> str:
    if not rsn:
        return ""
    from .capture import BssStat, _read_rsn

    stat = BssStat(bssid="")
    _read_rsn(rsn, stat)
    return stat.pmf


def bss_load(element: bytes | None) -> tuple[int, float] | None:
    """
    The BSS Load element (11) an access point may put in its beacons: how many clients it has
    and the share of time it found the channel busy (0-255 on air), or None without one.
    """
    if not element or len(element) < 3:
        return None
    return int.from_bytes(element[0:2], "little"), round(element[2] / 255, 3)


def frame_header(raw: bytes) -> tuple[int, int, int, str, str, str] | None:
    """
    (type, subtype, flags, addr1, addr2, addr3) of a radiotap-framed 802.11 frame, read from
    its bytes in microseconds, or None for one that's damaged (a bad FCS) or too short.
    """
    if len(raw) < 8:
        return None
    length = int.from_bytes(raw[2:4], "little")
    present = int.from_bytes(raw[4:8], "little")
    offset, word = 8, present
    while word & 0x80000000 and offset + 4 <= length:      # more presence words follow
        word = int.from_bytes(raw[offset:offset + 4], "little")
        offset += 4
    if present & 0x02:                       # the flags byte, after the TSF if there is one
        if present & 0x01:
            offset = ((offset + 7) & ~7) + 8
        if offset < length and raw[offset] & 0x40:
            return None
    body = raw[length:]
    if len(body) < 10:
        return None
    return ((body[0] >> 2) & 3, body[0] >> 4, body[1], body[4:10].hex(":"),
            body[10:16].hex(":") if len(body) >= 16 else "",
            body[16:22].hex(":") if len(body) >= 22 else "")


def worth_dissecting(header, target: str, beacons_of: set[str]) -> bool:
    """Whether read_frame needs the whole frame: everything else is read from the header."""
    kind, subtype, _flags, addr1, _addr2, addr3 = header
    if kind != 0:
        return False
    if subtype == 8:
        return addr3 in beacons_of
    return subtype in (10, 12) or (subtype in (0, 2, 11) and addr1 == target)


def read_header(header, when: float, log: ChannelLog) -> None:
    """
    What a frame that wasn't dissected says, as read_frame finds it: a data frame's client and
    whether it was sent again, or which network a beacon came from.
    """
    log.frame(when)
    if header is not None and header[0] == 0 and header[1] == 8:
        log.network(header[5], when)
    if header is None or header[0] != 2:
        return
    _kind, _subtype, flags, dst, src, _addr3 = header
    if log.target in (dst, src):
        log.data(when, bool(flags & 0x08))
    to_ds, from_ds = flags & 0x01, flags & 0x02
    if to_ds and not from_ds and dst == log.target and src:
        log.client(src, when)
    elif from_ds and not to_ds and src == log.target and not int(dst.split(":")[0], 16) & 1:
        log.client(dst, when)


def _listener(wanted):
    """Scapy's listening socket, dissecting only the frames ``wanted(header)`` picks."""
    from scapy.config import conf
    from scapy.data import MTU

    class Listener(conf.L2listen):
        def recv(self, x=MTU, **kwargs):
            cls, raw, ts = self.recv_raw(x)
            if not raw or not cls:
                return None
            header = frame_header(raw)
            packet = None
            if header is not None and wanted(header):
                try:
                    packet = cls(raw, **kwargs)
                except Exception:
                    packet = None
            if packet is None:
                packet = conf.raw_layer(raw)
            if ts:
                packet.time = ts
            return packet

    return Listener


def read_frame(packet, log: ChannelLog, layers) -> None:
    """What one frame says, into ``log``. Split out so recorded frames can be replayed in tests."""
    Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt = layers
    when = float(getattr(packet, "time", 0) or time.time())
    log.frame(when)
    if not packet.haslayer(Dot11):
        return
    radio = packet.getlayer("RadioTap")
    flags = getattr(radio, "Flags", None) if radio else None
    if flags is not None and "badFCS" in str(flags):
        return
    rssi = getattr(radio, "dBm_AntSignal", None) if radio else None
    rssi = float(rssi) if rssi is not None else None
    dot11 = packet.getlayer(Dot11)
    seq = (int(dot11.SC) >> 4) if dot11.SC is not None else None
    src, dst = (dot11.addr2 or "").lower(), (dot11.addr1 or "").lower()
    target = log.target

    if dot11.type == 0 and dot11.subtype == 8 and packet.haslayer(Dot11Beacon):
        beacon = packet.getlayer(Dot11Beacon)
        elements = _elements(packet, Dot11Elt)
        csa = None
        if 37 in elements and len(elements[37]) >= 3:
            csa = {"channel": elements[37][1], "count": elements[37][2]}
        elif 60 in elements and len(elements[60]) >= 4:
            csa = {"channel": elements[60][2], "count": elements[60][3]}
        channel = elements.get(3, b"\x00")[:1]
        log.network((dot11.addr3 or src).lower(), when)
        log.beacon((dot11.addr3 or src), when, tsf=int(beacon.timestamp), seq=seq, rssi=rssi,
                   interval_tu=int(beacon.beacon_interval or 0) or None,
                   channel=channel[0] if channel else None, csa=csa,
                   pmf=_pmf(elements.get(48)) or None, load=bss_load(elements.get(11)))
        return
    if dot11.type == 0 and dot11.subtype in (10, 12):
        protected = bool(int(dot11.FCfield) & 0x40)
        reason = None
        if not protected:
            frame = packet.getlayer(Dot11Deauth if dot11.subtype == 12 else Dot11Disas)
            reason = int(frame.reason) if frame is not None else None
        log.deauth(Deauth(time=when, src=src, dst=dst, bssid=(dot11.addr3 or "").lower(),
                          reason=reason, seq=seq, rssi=rssi, protected=protected,
                          kind="deauth" if dot11.subtype == 12 else "disassoc"))
        return
    if dot11.type == 0 and dot11.subtype in (0, 2, 11) and dst == target and src:
        log.join(src, when)                  # association, reassociation, authentication
        return
    if dot11.type == 2:
        to_ds, from_ds = int(dot11.FCfield) & 0x01, int(dot11.FCfield) & 0x02
        if target in (dst, src):
            log.data(when, bool(int(dot11.FCfield) & 0x08))
        if to_ds and not from_ds and dst == target:
            log.client(src, when)
        elif from_ds and not to_ds and src == target and not int(dst.split(":")[0], 16) & 1:
            log.client(dst, when)


class BeaconWatch:
    """Listens on ``device`` (already in monitor mode) on one channel, feeding a ChannelLog."""

    def __init__(self, device: str, target: str, freq: float, width: int = 20,
                 center: float | None = None, also: list[str] = ()) -> None:
        self.device, self.freq, self.width, self.center = device, freq, width, center
        self.log = ChannelLog(target)
        # Beacons worth dissecting: the access point's and its unit's other radios' (clocks)
        self.beacons_of = {target.lower(), *(b.lower() for b in also)}
        self.error = ""
        self.started = 0.0
        self._sniffer = None
        self._recent: deque = deque()
        self._writer = None
        self._writer_lock = threading.Lock()
        self._layers = None

    def start(self) -> None:
        from scapy.all import AsyncSniffer
        from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt

        self._layers = (Dot11, Dot11Beacon, Dot11Deauth, Dot11Disas, Dot11Elt)
        notes: list[str] = []
        if not monitor.tune(self.device, self.freq, self.width, self.center, log=notes.append):
            raise RuntimeError(notes[-1] if notes else f"couldn't tune {self.device}")
        target = self.log.target
        # Management frames from everyone (a forger can claim any address), data only with the
        # access point, which is enough to count its clients
        bpf = f"type mgt or (type data and (wlan addr1 {target} or wlan addr2 {target}))"
        self._sniffer = AsyncSniffer(
            iface=self.device, prn=self._frame, store=False, monitor=True, filter=bpf,
            L2socket=_listener(lambda header: worth_dissecting(header, target, self.beacons_of)))
        self._sniffer.start()
        self.started = time.time()

    def stop(self) -> None:
        sniffer = self._sniffer
        if sniffer is not None:
            try:
                sniffer.stop(join=False)
            except Exception:
                pass
        self.end_capture()

    @property
    def running(self) -> bool:
        sniffer = self._sniffer
        if sniffer is not None and not sniffer.running:
            self.error = str(getattr(sniffer, "exception", "") or "the capture stopped")
        return sniffer is not None and sniffer.running

    def retune(self, freq: float, width: int = 20, center: float | None = None) -> bool:
        """Follows the access point to a new channel."""
        self.freq, self.width, self.center = freq, width, center
        return monitor.tune(self.device, freq, width, center)

    # ---------- the frames around an incident ----------

    def begin_capture(self, path: Path) -> None:
        """Saves frames to ``path`` from LOOKBACK seconds ago until end_capture."""
        from scapy.utils import PcapWriter

        from ..core.project import make_dir

        with self._writer_lock:
            if self._writer is not None:
                return
            make_dir(Path(path).parent)
            self._writer = PcapWriter(str(path), linktype=127, sync=True)
            self._writer_path = Path(path)
            for packet in list(self._recent):
                self._writer.write(packet)

    def end_capture(self) -> None:
        from ..core.project import hand_back

        with self._writer_lock:
            if self._writer is None:
                return
            try:
                self._writer.close()
            finally:
                self._writer = None
                hand_back(self._writer_path)

    def _frame(self, packet) -> None:
        try:
            if packet.haslayer(self._layers[0]):
                read_frame(packet, self.log, self._layers)
            else:
                read_header(frame_header(bytes(packet)),
                            float(getattr(packet, "time", 0) or time.time()), self.log)
        except Exception:
            pass                            # a frame Scapy can't make sense of
        now = float(getattr(packet, "time", 0) or time.time())
        self._recent.append(packet)
        while self._recent and float(getattr(self._recent[0], "time", now)) < now - LOOKBACK:
            self._recent.popleft()
        with self._writer_lock:
            if self._writer is not None:
                try:
                    self._writer.write(packet)
                except Exception:
                    pass
