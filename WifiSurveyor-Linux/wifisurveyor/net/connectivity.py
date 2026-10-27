"""
Whether a good signal is also a usable connection: latency, jitter and loss to the gateway
over the Wi-Fi adapter, and how long a DNS lookup takes. A few small packets, nothing more.
"""

from __future__ import annotations

import re
import socket
import time

from .wifi import have, link, run

DNS_NAME = "example.com"


def gateway(device: str) -> str | None:
    """The default gateway reached through this adapter, if it has one."""
    code, text = run("ip", "-4", "route", "show", "default", "dev", device)
    found = re.search(r"default via (\S+)", text) if code == 0 else None
    return found.group(1) if found else None


def parse_ping(text: str) -> dict:
    result: dict = {}
    loss = re.search(r"(\d+(?:\.\d+)?)% packet loss", text)
    if loss:
        result["loss"] = float(loss.group(1))
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", text)
    if rtt:
        result["latency_ms"] = float(rtt.group(2))
        result["jitter_ms"] = float(rtt.group(4))
    return result


def check(device: str, count: int = 10) -> dict:
    """Latency, jitter and loss to the gateway, and DNS lookup time. Problems go in "error"."""
    result: dict = {"time": time.time()}
    joined = link(device) or {}
    if not joined.get("ssid"):
        result["error"] = f"{device} isn't connected to a network"
        return result
    result.update(ssid=joined.get("ssid"), bssid=joined.get("bssid"), signal=joined.get("signal"))
    target = gateway(device)
    if target is None:
        result["error"] = "connected, but no gateway: DHCP may have failed"
        return result
    result["gateway"] = target
    if not have("ping"):
        result["error"] = "ping isn't installed (iputils-ping)"
        return result
    _code, text = run("ping", "-c", str(count), "-i", "0.2", "-W", "1", "-I", device, target,
                      timeout=count + 5)
    result.update(parse_ping(text))
    if result.get("loss") == 100:
        result["error"] = f"the gateway {target} didn't answer"
    started = time.monotonic()
    try:
        socket.getaddrinfo(DNS_NAME, 443)
        result["dns_ms"] = (time.monotonic() - started) * 1000
    except OSError as error:
        result.setdefault("error", f"DNS lookup failed: {error}")
    return result


def problem(result: dict | None) -> str | None:
    """What's wrong enough to mark on the map, or None."""
    if not result:
        return None
    if result.get("error"):
        return result["error"]
    if (result.get("loss") or 0) >= 10:
        return f"{result['loss']:.0f}% packet loss to the gateway"
    return None


def describe(result: dict | None) -> str:
    if not result:
        return ""
    if result.get("error") and "latency_ms" not in result:
        return f"Connection: {result['error']}"
    parts = []
    if "latency_ms" in result:
        parts.append(f"gateway {result['latency_ms']:.0f} ms (±{result.get('jitter_ms', 0):.0f}), "
                     f"{result.get('loss', 0):.0f}% loss")
    if "dns_ms" in result:
        parts.append(f"DNS {result['dns_ms']:.0f} ms")
    if result.get("error"):
        parts.append(result["error"])
    return "Connection: " + ", ".join(parts)


def saved_profile(ssid: str) -> str | None:
    """The name of a NetworkManager Wi-Fi connection saved for this SSID, if there is one."""
    code, text = run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
    if code != 0:
        return None
    for line in text.splitlines():
        name, _, kind = line.rpartition(":")
        if kind != "802-11-wireless" or not name:
            continue
        name = name.replace("\\:", ":")
        if re.search(r" \(survey [^)]+\)$", name):
            continue                      # a temporary copy made by join, not the saved one
        code, found = run("nmcli", "-g", "802-11-wireless.ssid", "connection", "show", name)
        if code == 0 and found.strip() == ssid:
            return name
    return None


# Key management that means the link is encrypted: WPA/WPA2/WPA3 personal and enterprise,
# and Enhanced Open (OWE), which encrypts without a password
ENCRYPTED_KEY_MGMT = {"wpa-psk", "sae", "wpa-eap", "wpa-eap-suite-b-192", "owe", "ieee8021x"}


def why_not_test(ssid: str, heard: list[dict], allow_open: bool = False) -> str | None:
    """
    Why a network shouldn't be joined and speed-tested from here, or None if it can be: it
    has to be heard at this point, encrypted, and have its password saved on this computer,
    so nothing ever tries an open network or asks for a password mid-survey. ``allow_open``
    lets an open network through, for one the surveyor chose to test by name.
    """
    here = [n for n in heard if n.get("ssid") == ssid]
    if not here:
        return "not heard here"
    if not any(n.get("secured") for n in here):
        if allow_open:
            return None if have("nmcli") else "NetworkManager (nmcli) isn't available to join it"
        return "an open network: only encrypted networks are joined"
    if not have("nmcli"):
        return "NetworkManager (nmcli) isn't available to join it"
    profile = saved_profile(ssid)
    if not profile:
        return ("no saved password on this computer: connect to it once from the system's "
                "Wi-Fi settings")
    code, key_mgmt = run("nmcli", "-g", "802-11-wireless-security.key-mgmt", "connection",
                         "show", profile)
    if code == 0 and key_mgmt.strip().lower() not in ENCRYPTED_KEY_MGMT:
        return "its saved connection isn't encrypted"
    return None


# Adapters something is keeping on one network for a while (the SSID monitor), and who: a
# survey's speed test mustn't move them to another network meanwhile
_held: dict[str, str] = {}


def hold(device: str, who: str) -> None:
    _held[device] = who


def release(device: str) -> None:
    _held.pop(device, None)


def held_by(device: str) -> str | None:
    return _held.get(device)


def copy_name(profile: str, device: str) -> str:
    return f"{profile} (survey {device})"


def join(device: str, ssid: str, bssid: str | None = None, holder: str | None = None) -> str | None:
    """
    Connects ``device`` to ``ssid`` so the connection check can run, and returns why it
    couldn't, or None. A saved connection is used through a temporary copy bound to this
    adapter, so the computer's own Wi-Fi keeps its connection; an open network is joined
    directly. Nothing is saved. With ``bssid``, only that access point is joined: the
    adapter won't roam to another one of the network. ``holder`` is whoever holds the
    adapter (see hold), who may move it; anyone else is refused.
    """
    owner = held_by(device)
    if owner and owner != holder:
        return f"{device} is in use by {owner}"
    joined = link(device) or {}
    if joined.get("ssid") == ssid and (not bssid or joined.get("bssid", "").upper() == bssid.upper()):
        return None
    if not have("nmcli"):
        return "NetworkManager (nmcli) isn't available to join the network"
    profile = saved_profile(ssid)
    if profile:
        copy = copy_name(profile, device)
        run("nmcli", "connection", "delete", copy)
        code, text = run("nmcli", "connection", "clone", "--temporary", profile, copy)
        if code == 0:
            run("nmcli", "connection", "modify", "--temporary", copy,
                "connection.interface-name", device, "connection.autoconnect", "no",
                "802-11-wireless.bssid", bssid.upper() if bssid else "")
            code, text = run("nmcli", "--wait", "25", "connection", "up", copy,
                             "ifname", device, timeout=35)
    else:
        code, text = run("nmcli", "--wait", "25", "device", "wifi", "connect", ssid,
                         "ifname", device, *(["bssid", bssid.upper()] if bssid else []),
                         timeout=35)
        if code != 0 and ("Secrets were required" in text or "password" in text.lower()):
            return (f"{ssid} needs a password: connect to it once from the system's Wi-Fi "
                    "settings so it's saved, then measure again")
    if code != 0:
        return f"couldn't join {ssid}: {text.splitlines()[-1] if text else 'no reason given'}"
    return None


def leave(device: str, ssid: str) -> None:
    """
    Undoes join: takes ``device`` off the temporary copy join made and deletes it, so
    NetworkManager puts it back on whatever it would normally use. A connection join didn't
    make (the computer's own, which it was already on) is left alone.
    """
    profile = saved_profile(ssid)
    if not profile:
        return
    copy = copy_name(profile, device)
    code, active = run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", device)
    if code == 0 and active.strip() == copy:
        run("nmcli", "connection", "down", copy)
    run("nmcli", "connection", "delete", copy)
