"""Monitor mode, which on Linux is a property of the interface rather than a favour.

The driver either offers the monitor interface type or it doesn't, and ``iw`` switches to
it. The real work is keeping NetworkManager and wpa_supplicant from scanning on the
interface and pulling it back out.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .wifi import have, is_root, run, supports_monitor

Log = Callable[[str], None]

INTERFERING = ("NetworkManager", "wpa_supplicant", "iwd")


def interfering() -> list[str]:
    return [name for name in INTERFERING if run("pgrep", "-x", name)[0] == 0]


def enable(device: str, channel: int | None, release_from_nm: bool, log: Log) -> bool:
    """Returns whether the interface ended up in monitor mode. Brings it back up on failure."""
    if not device or device == "none":
        log("No wireless interface to switch.")
        return False
    if not is_root():
        log("Monitor mode needs root. Start the app with pkexec or sudo.")
        return False
    if not supports_monitor(device):
        log(f"{device}'s driver doesn't list monitor among its interface modes, so it "
            "can't be switched. 'iw list' shows which device can.")
        return False

    if release_from_nm and have("nmcli"):
        code, text = run("nmcli", "device", "set", device, "managed", "no")
        log(f"NetworkManager: {'told to leave it alone' if code == 0 else text}")

    brought_down = False
    for args, what in (
        (("ip", "link", "set", device, "down"), "Bringing the interface down"),
        (("iw", "dev", device, "set", "type", "monitor"), "Switching to monitor"),
        (("ip", "link", "set", device, "up"), "Bringing it back up"),
    ):
        code, text = run(*args)
        log(f"{what}: {'done' if code == 0 else text}")
        if what == "Bringing the interface down" and code == 0:
            brought_down = True
        if code != 0:
            if brought_down:
                run("ip", "link", "set", device, "up")
            return False

    if channel and not set_channel(device, channel, log):
        return False

    _, info = run("iw", "dev", device, "info")
    ok = "type monitor" in info
    log(f"{device} is in monitor mode." if ok else f"{device} didn't take: {info}")
    return ok


def fresh_interface(device: str, log: Log) -> str | None:
    """
    A new monitor-mode interface on ``device``'s radio, ``device`` itself taken down, for
    a driver that won't hear in monitor mode however it's tuned. The Alfa AWUS036ACM's MT7612U,
    switched to monitor mode after working on 5 GHz, hears nothing on 2.4 GHz whatever channel
    iw reports it on; a new interface on the same radio hears fine on either band. Its name, or
    None. Undone by remove_interface, then restore.
    """
    phy = re.search(r"wiphy (\d+)", run("iw", "dev", device, "info")[1])
    if not phy:
        return None
    name = f"mon{phy.group(1)}"
    run("iw", "dev", name, "del")                  # left over from a session that crashed
    run("ip", "link", "set", device, "down")
    code, text = run("iw", "phy", f"phy{phy.group(1)}", "interface", "add", name, "type", "monitor")
    if code != 0:
        log(f"Couldn't add a monitor interface to {device}'s radio: {text}")
        run("ip", "link", "set", device, "up")
        return None
    run("ip", "link", "set", name, "up")
    return name


def remove_interface(name: str) -> None:
    run("iw", "dev", name, "del")


def restore(device: str, log: Log) -> None:
    if not device or device == "none":
        log("No wireless interface to restore.")
        return
    for args in (
        ("ip", "link", "set", device, "down"),
        ("iw", "dev", device, "set", "type", "managed"),
        ("ip", "link", "set", device, "up"),
    ):
        run(*args)
    if have("nmcli"):
        run("nmcli", "device", "set", device, "managed", "yes")
    log(f"{device} is back to normal and NetworkManager is managing it again.")


def set_channel(device: str, channel: int, log: Log) -> bool:
    code, text = run("iw", "dev", device, "set", "channel", str(channel))
    log(f"Channel {channel}: {'set' if code == 0 else text}")
    return code == 0


def is_monitor(device: str) -> bool:
    return "type monitor" in run("iw", "dev", device, "info")[1]


def supported_freqs(device: str) -> list[float]:
    """Every channel centre the radio can listen on, lowest first. Disabled ones are left out."""
    phy = re.search(r"wiphy (\d+)", run("iw", "dev", device, "info")[1])
    listing = run("iw", f"phy#{phy.group(1)}", "info")[1] if phy else run("iw", "list")[1]
    freqs = set()
    for match in re.finditer(r"\* (\d+(?:\.\d+)?) MHz \[\d+\](.*)", listing):
        if "disabled" not in match.group(2):
            freqs.add(float(match.group(1)))
    return sorted(freqs)


def hop_list(device: str) -> list[float]:
    """
    The channels worth going round to find access points: all of 2.4 and 5 GHz, and the 6 GHz
    preferred scanning channels (5, 21, 37, …), where every 6 GHz access point can be found.
    """
    chosen = []
    for freq in supported_freqs(device):
        if freq < 5925:
            chosen.append(freq)
        elif freq >= 5950 and (round((freq - 5950) / 5) - 5) % 16 == 0:
            chosen.append(freq)
    return chosen


def tune(device: str, freq: float, width: int = 20, center: float | None = None,
         log: Log | None = None) -> bool:
    """
    Listens on ``freq``, across the whole of a wider channel when its width and centre are
    given, so data frames sent at 40, 80 or 160 MHz are heard and not just beacons. Falls back
    to the primary 20 MHz when the driver won't take the wider setting.
    """
    base = ["iw", "dev", device, "set", "freq", str(int(freq))]
    if width > 20 and center:
        code, text = run(*base, str(width), str(int(center)))
        if code == 0:
            if log:
                log(f"Listening on {freq:.0f} MHz, {width} MHz wide")
            return True
        if log:
            log(f"The driver wouldn't listen {width} MHz wide ({text.strip()}); using 20 MHz")
    code, text = run(*base)
    if log:
        log(f"Listening on {freq:.0f} MHz" if code == 0 else f"Couldn't tune to {freq:.0f} MHz: {text}")
    return code == 0

