"""Satellites: a tinySA on a Raspberry Pi elsewhere on the LAN that sends test signals for us.

The Pi runs satellite/wifisurveyor_satellite.py. It answers a UDP broadcast on port 8737 with
its name and what's attached, and takes commands over HTTP on the same port, each signed with
an HMAC of the pairing code it printed (with a timestamp and a nonce, so none can be replayed).
The pairing code is kept with the satellite's address in this machine's settings.

While it's sending, the app checks in every few seconds (status); the satellite turns its
signal off when it hasn't heard from the app for a while, so a tablet that walks out of Wi-Fi
range doesn't leave it transmitting. The rules for what may be sent are rf.txrules, checked
here first for a clear message and again on the satellite, which has the final say.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import time
from dataclasses import asdict, dataclass

from ..core import settings
from ..rf import txrules
from . import outbound

PORT = 8737
DISCOVER = b"WIFISURVEYOR-SATELLITE?"


@dataclass(slots=True)
class Paired:
    name: str
    host: str
    port: int = PORT
    code: str = ""


def saved() -> list[Paired]:
    found = []
    for entry in settings.get("satellites") or []:
        try:
            found.append(Paired(str(entry["name"]), str(entry["host"]), int(entry.get("port", PORT)),
                                str(entry.get("code", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    return found


def remember(paired: Paired) -> None:
    others = [p for p in saved() if (p.host, p.port) != (paired.host, paired.port)]
    settings.put("satellites", [asdict(p) for p in [*others, paired]])


def forget(host: str, port: int = PORT) -> None:
    settings.put("satellites", [asdict(p) for p in saved() if (p.host, p.port) != (host, port)])


def discover(timeout: float = 1.5, port: int = PORT) -> list[dict]:
    """Every satellite that answers a broadcast on this LAN: its info, with "host" added."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.2)
    found: dict[tuple[str, int], dict] = {}
    try:
        sock.sendto(DISCOVER, ("255.255.255.255", port))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (host, _sender_port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                info = json.loads(data)
            except ValueError:
                continue
            if info.get("service") == "wifisurveyor-satellite":
                info["host"] = host
                found[(host, int(info.get("port", port)))] = info
    except OSError:
        pass
    finally:
        sock.close()
    return list(found.values())


def _normalise(code: str) -> bytes:
    return code.strip().upper().replace("-", "").replace(" ", "").encode()


def signature(code: str, stamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    message = f"{stamp}:{nonce}:{method}:{path}:".encode() + body
    return hmac.new(_normalise(code), message, hashlib.sha256).hexdigest()


class SatelliteError(RuntimeError):
    pass


class Satellite:
    """One paired satellite, and the commands it takes."""

    def __init__(self, paired: Paired, timeout: float = 6.0) -> None:
        self.paired = paired
        self.timeout = timeout

    @property
    def name(self) -> str:
        return self.paired.name

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""
        stamp, nonce = f"{time.time():.3f}", secrets.token_hex(8)
        headers = {"Content-Type": "application/json",
                   "X-Satellite-Auth": f"{stamp}:{nonce}:"
                                       + signature(self.paired.code, stamp, nonce, method, path, body)}
        try:
            # Through net.outbound, which only lets it reach this network's own addresses
            with outbound.open_url(f"http://{self.paired.host}:{self.paired.port}{path}",
                                   data=body or None, headers=headers,
                                   timeout=timeout or self.timeout, method=method) as answer:
                return json.loads(answer.read() or b"{}")
        except outbound.HTTPError as error:
            try:
                message = json.loads(error.read()).get("error") or str(error)
            except ValueError:
                message = str(error)
            raise SatelliteError(f"{self.name}: {message}") from error
        except (outbound.URLError, OSError, ValueError) as error:
            reason = getattr(error, "reason", error)
            raise SatelliteError(f"{self.name} at {self.paired.host} isn't answering ({reason}).") from error

    def info(self) -> dict:
        """What it says about itself, with no pairing code needed."""
        return self._request("GET", "/info")

    def status(self) -> dict:
        """What it's sending, if anything. Also keeps it from timing out the signal."""
        return self._request("GET", "/status")

    def tone(self, freq_mhz: float, level_dbm: float, cabled: bool, modulation: str = "off",
             minutes: float = txrules.TX_MINUTES) -> dict:
        trouble = txrules.check(freq_mhz, level_dbm, cabled)
        if trouble:
            raise SatelliteError(trouble)
        return self._request("POST", "/tone", {"freq_mhz": freq_mhz, "level_dbm": level_dbm,
                                               "modulation": modulation, "minutes": minutes,
                                               "cabled": cabled})

    def plan(self, steps: list[tuple[float, float, float]], cabled: bool, repeat: int = 1,
             minutes: float = txrules.TX_MINUTES) -> dict:
        """Steps through (MHz, dBm, seconds), ``repeat`` times."""
        for freq, level, _seconds in steps:
            trouble = txrules.check(freq, level, cabled)
            if trouble:
                raise SatelliteError(trouble)
        return self._request("POST", "/plan", {
            "steps": [{"freq_mhz": f, "level_dbm": l, "seconds": s} for f, l, s in steps],
            "repeat": repeat, "minutes": minutes, "cabled": cabled})

    def stop(self) -> dict:
        return self._request("POST", "/stop", {})

    def scan(self, start_mhz: float, stop_mhz: float, points: int = 450) -> tuple[list[float], list[float]]:
        """The satellite's tinySA sweeping a range: (MHz, dBm) per point."""
        answer = self._request("POST", "/scan", {"start_mhz": start_mhz, "stop_mhz": stop_mhz,
                                                 "points": points}, timeout=40)
        levels = answer.get("levels") or []
        if len(levels) < 2:
            raise SatelliteError(f"{self.name} returned no sweep.")
        step = (stop_mhz - start_mhz) / (len(levels) - 1)
        return [start_mhz + i * step for i in range(len(levels))], [float(v) for v in levels]


def all_paired() -> list[Satellite]:
    return [Satellite(p) for p in saved()]
