"""
What the adapter connected to a watched network can say about it, second by second.

- ``iw event`` reports the moment the link ends, and why: who sent the deauthentication and
  its reason code, or that the kernel gave up because beacons stopped arriving. It also reports
  forged deauthentications the adapter rejected (on a network with protected management
  frames) and the access point announcing a channel switch.
- ``iw station dump`` counts the beacons received and lost, and the signal, without leaving
  the channel.
- One ping to the gateway and one to the internet (public DNS resolvers, by address) per beat,
  and a DNS lookup now and then, all bound to this adapter so another adapter's connection
  can't stand in for it.
"""

from __future__ import annotations

import random
import re
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .capture import _die_with_parent
from .wifi import have, run

# Pinged by address, so a DNS failure can't look like the internet being down. Several, so
# one resolver having a bad minute isn't an outage.
INTERNET_TARGETS = ("1.1.1.1", "9.9.9.9", "8.8.8.8")
DNS_NAME = "example.com"

_PREFIX = re.compile(r"^(?:(\d+\.\d+):\s+)?(\S+) \(phy #\d+\): (.*)$")
_MAC = r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})"


def parse_event(line: str) -> dict | None:
    """One ``iw event -t`` line as {"time", "device", "kind", ...}, or None if it's not one we use."""
    found = _PREFIX.match(line.strip())
    if not found:
        return None
    stamp, device, body = found.groups()
    event: dict = {"time": float(stamp) if stamp else time.time(), "device": device}
    if match := re.match(r"disconnected \((by AP|local request)\)(?: reason: (\d+))?", body):
        event.update(kind="disconnected", by_ap=match.group(1) == "by AP",
                     reason=int(match.group(2)) if match.group(2) else None)
    elif match := re.match(r"unprotected (deauth|disassoc)\S*:? " + _MAC + r" -> " + _MAC, body):
        event.update(kind="unprotected", frame=match.group(1), src=match.group(2).lower(),
                     dst=match.group(3).lower())
    elif match := re.match(r"(deauth|disassoc)\S*:? " + _MAC + r" -> " + _MAC
                           + r"(?: reason (\d+))?", body):
        event.update(kind=match.group(1), src=match.group(2).lower(), dst=match.group(3).lower(),
                     reason=int(match.group(4)) if match.group(4) else None)
    elif match := re.match(r"connected to " + _MAC, body):
        event.update(kind="connected", bssid=match.group(1).lower())
    elif body.startswith(("failed to connect", "timed out")):
        status = re.search(r"status: (\d+)", body)
        event.update(kind="connect_failed", status=int(status.group(1)) if status else None)
    elif match := re.match(r"ch(?:annel)?[ _]switch(?:[ _]started)?.*?freq[= ](\d+)", body):
        event.update(kind="ch_switch", freq=float(match.group(1)), started="started" in body)
    elif "beacon loss" in body:
        event.update(kind="beacon_loss")
    elif body.startswith("roamed"):
        event.update(kind="roamed", bssid=(re.search(_MAC, body) or [None, ""])[1].lower())
    else:
        return None
    return event


class IwEvents:
    """Runs ``iw event -t`` for as long as it's wanted, handing ``device``'s events to ``handle``."""

    def __init__(self, device: str, handle: Callable[[dict], None]) -> None:
        self.device, self.handle = device, handle
        self.error = ""
        self._process: subprocess.Popen | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not have("iw"):
            self.error = "iw isn't installed, so the reason for each disconnect can't be read"
            return
        threading.Thread(target=self._read, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process and process.poll() is None:
            process.terminate()

    def _read(self) -> None:
        # iw can exit (a driver reload takes the netlink family with it): start it again
        while not self._stop.is_set():
            try:
                self._process = subprocess.Popen(
                    ["iw", "event", "-t"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, preexec_fn=_die_with_parent)
                for line in self._process.stdout:
                    event = parse_event(line)
                    if event and event["device"] == self.device:
                        self.handle(event)
                    if self._stop.is_set():
                        break
            except OSError as error:
                self.error = f"couldn't follow iw event: {error}"
                return
            self._stop.wait(2)


# ---------- station counters ----------

def parse_station(text: str) -> dict:
    """``iw dev X station dump`` for the access point: the counters this monitor uses."""
    result: dict = {}
    for raw in text.splitlines():
        key, _, value = raw.strip().partition(":")
        value = value.strip()
        number = re.match(r"-?\d+(?:\.\d+)?", value)
        if raw.startswith("Station "):
            found = re.search(_MAC, raw)
            result["bssid"] = found.group(1).lower() if found else ""
        elif key in ("beacon loss", "beacon rx", "tx failed", "tx retries", "rx drop misc",
                     "inactive time", "connected time", "tx packets", "rx packets") and number:
            result[key.replace(" ", "_")] = int(float(number.group()))
        elif key in ("signal", "signal avg", "beacon signal avg", "tx bitrate", "rx bitrate") and number:
            result[key.replace(" ", "_")] = float(number.group())
        elif key == "MFP":
            result["mfp"] = value == "yes"
        elif key == "beacon interval" and number:
            result["beacon_interval"] = int(number.group())
    return result


def station(device: str) -> dict:
    code, text = run("iw", "dev", device, "station", "dump", timeout=5)
    return parse_station(text) if code == 0 else {}


# ---------- addresses ----------

def address(device: str) -> str | None:
    code, text = run("ip", "-4", "-o", "addr", "show", "dev", device, timeout=5)
    found = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", text) if code == 0 else None
    return found.group(1) if found else None


def dns_servers(device: str) -> list[str]:
    """The IPv4 DNS servers this adapter's connection was given."""
    if not have("nmcli"):
        return []
    code, text = run("nmcli", "-g", "IP4.DNS", "device", "show", device, timeout=5)
    if code != 0:
        return []
    return re.findall(r"\d+\.\d+\.\d+\.\d+", text)


# ---------- probes ----------

def ping(device: str, target: str, wait: float = 1.0) -> float | None:
    """One echo through ``device``: the round trip in ms, or None if it didn't come back."""
    code, text = run("ping", "-n", "-c", "1", "-W", str(max(1, round(wait))), "-I", device,
                     target, timeout=wait + 3)
    found = re.search(r"time[=<]([\d.]+) ?ms", text)
    return float(found.group(1)) if code == 0 and found else None


RCODES = {1: "a format error", 2: "a server failure", 3: "no such name", 4: "not implemented",
          5: "refused"}


class DnsError(OSError):
    pass


def dns_query(name: str, ident: int) -> bytes:
    header = struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0)       # recursion desired
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split("."))
    return header + labels + b"\x00" + struct.pack(">HH", 1, 1)       # A, IN


def dns_answer(data: bytes, ident: int) -> int:
    """How many answers a reply carries; raises DnsError when the server refused or failed."""
    if len(data) < 12:
        raise DnsError("a reply too short to read")
    got, flags, _questions, answers = struct.unpack(">HHHH", data[:8])
    if got != ident:
        raise DnsError("a reply to another question")
    rcode = flags & 0x0F
    if rcode:
        raise DnsError(f"the server answered with {RCODES.get(rcode, f'error {rcode}')}")
    return answers


def dns_lookup(device: str, server: str, name: str = DNS_NAME, wait: float = 3.0) -> float:
    """
    Asks ``server`` for ``name`` through ``device`` and returns how long it took, in ms.
    Bound to the adapter as root; otherwise the routing table decides, which is the same
    adapter when it carries the default route.
    """
    ident = random.randrange(1, 0xFFFF)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25),
                            device.encode())
        except OSError:
            pass
        sock.settimeout(wait)
        started = time.monotonic()
        sock.sendto(dns_query(name, ident), (server, 53))
        while True:
            try:
                data, _sender = sock.recvfrom(1500)
            except TimeoutError as error:
                raise DnsError(f"{server} didn't answer") from error
            try:
                dns_answer(data, ident)
                return (time.monotonic() - started) * 1000
            except DnsError as error:
                if "another question" not in str(error):
                    raise


_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="beat")


def lookup_any(device: str, servers: list[str], every: bool = False) -> dict:
    """
    DNS through each of the network's servers in turn until one answers, as a client does:
    {"dns_ms", "dns_server"} from the one that did, and "dns_dead" naming those that didn't.
    "dns_error" only when none answered. With ``every``, all of them are asked, so a dead
    second server is found even while the first answers.
    """
    result: dict = {"dns_dead": [], "dns_tried": {}}
    for server in servers:
        try:
            took = dns_lookup(device, server)
            result["dns_tried"][server] = True
        except OSError as error:
            result["dns_tried"][server] = False
            result["dns_dead"].append(server)
            if "dns_ms" not in result:
                result["dns_error"] = str(error)
            continue
        if "dns_ms" not in result:
            result.update(dns_ms=took, dns_server=server)
            result.pop("dns_error", None)
        if not every:
            break
    return result


def beat(device: str, gateway: str | None, dns_servers_: list[str], number: int,
         dns_every: int = 5) -> dict:
    """
    One heartbeat: the gateway and the internet pinged at once, and every ``dns_every`` beats a
    DNS lookup. The internet counts as up when any of the targets answers; they're tried in
    turn so no one resolver is asked every time.
    """
    result: dict = {"time": time.time()}
    targets = INTERNET_TARGETS[number % len(INTERNET_TARGETS):] + INTERNET_TARGETS[:number % len(INTERNET_TARGETS)]

    def internet() -> tuple[float | None, str]:
        for target in targets[:2]:
            answer = ping(device, target)
            if answer is not None:
                return answer, target
        return None, targets[0]

    gw = _pool.submit(ping, device, gateway) if gateway else None
    net = _pool.submit(internet)
    lookup = None
    if dns_servers_ and number % dns_every == 0:
        lookup = _pool.submit(lookup_any, device, dns_servers_)
    result["gw_ms"] = gw.result() if gw else None
    result["net_ms"], result["net_target"] = net.result()
    if lookup is not None:
        result.update(lookup.result())
    return result
