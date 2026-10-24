"""Finding the spectrum analyzer by asking each likely serial port whether it is one."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field

from ..net import gps
from . import explorer, tinysa
from .analyzer import Analyzer, PortPermissionError

RULE_PATH = "/etc/udev/rules.d/70-wifisurveyor-serial.rules"


@dataclass
class Detection:
    explorer: Analyzer | None = None
    denied: list[str] = field(default_factory=list)   # ports we weren't allowed to open
    tried: list[str] = field(default_factory=list)
    error: str = ""
    # Every analyzer that answered, when looking for all of them (find_all); explorer is the first
    found: list[Analyzer] = field(default_factory=list)

    @property
    def message(self) -> str:
        if len(self.found) > 1:
            return ", ".join(f"{a.name} on {a.device}" for a in self.found)
        if self.explorer is not None:
            return f"{self.explorer.name} found on {self.explorer.device}"
        if self.denied:
            return (f"Can't open {', '.join(self.denied)}: no permission. "
                    "Press Grant serial access, then it connects by itself.")
        if self.error:
            return self.error
        if self.tried:
            return f"No RF Explorer or tinySA answered on {', '.join(self.tried)}."
        return "No RF Explorer or tinySA plugged in."


def _tinysa_first(port: str) -> bool:
    """A tinySA is a USB CDC device (ttyACM); the RF Explorer sits behind a UART bridge."""
    return usb_ids(port) == tinysa.USB_ID or os.path.basename(port).startswith("ttyACM")


def identify(port: str):
    """
    Opens the port as whichever analyzer answers on it, or returns None. The likelier one is
    asked first, since asking the wrong one costs a timeout. Raises PortPermissionError when
    the port can't be opened for lack of rights.
    """
    def as_tinysa():
        # One that's still finishing a scan answers once it's done, so it's given longer
        # when the port's USB ID says it's a tinySA
        wait = 3.0 if usb_ids(port) == tinysa.USB_ID else 1.0
        return tinysa.TinySa(port) if tinysa.probe(port, timeout=wait) else None

    def as_explorer():
        baud = explorer.probe(port)
        return explorer.RfExplorer(port, baud) if baud is not None else None

    order = (as_tinysa, as_explorer) if _tinysa_first(port) else (as_explorer, as_tinysa)
    for attempt in order:
        found = attempt()
        if found is not None:
            return found
    return None


def find(skip: set[str] | None = None) -> Detection:
    """
    Probes USB serial ports that aren't the GPS, named analyzers first, and opens the first
    that answers. Ports this user can't open are reported rather than passed over.
    """
    result = Detection()
    for port in gps.rf_probe_order(skip):
        result.tried.append(port)
        try:
            result.explorer = identify(port)
        except PortPermissionError:
            result.denied.append(port)
            continue
        except Exception as error:
            result.error = f"{port} answered but couldn't be opened: {error}"
            continue
        if result.explorer is not None:
            return result
    return result


def find_all(skip: set[str] | None = None) -> Detection:
    """
    As ``find``, but opens every analyzer that answers, not just the first: an RF Explorer and
    a tinySA, or two of either, all sweep. ``skip`` holds the GPS and ports already open.
    """
    result = Detection()
    for port in gps.rf_probe_order(skip):
        result.tried.append(port)
        try:
            found = identify(port)
        except PortPermissionError:
            result.denied.append(port)
            continue
        except Exception as error:
            result.error = f"{port} answered but couldn't be opened: {error}"
            continue
        if found is not None:
            result.found.append(found)
    result.explorer = result.found[0] if result.found else None
    return result


def usb_ids(device: str) -> tuple[str, str] | None:
    """Vendor and product id of the USB device behind a tty, from sysfs."""
    from pathlib import Path

    node = Path("/sys/class/tty") / os.path.basename(device) / "device"
    try:
        current = node.resolve()
    except OSError:
        return None
    for _ in range(6):
        vendor, product = current / "idVendor", current / "idProduct"
        if vendor.is_file() and product.is_file():
            return vendor.read_text().strip().lower(), product.read_text().strip().lower()
        current = current.parent
    return None


def udev_rule(ids: list[tuple[str, str]]) -> str:
    """
    uaccess hands the device to whoever is logged in at the seat, straight away and on every
    replug, which is what a desk instrument wants. No group change, no logging out.
    """
    lines = ["# Written by Wi-Fi Surveyor: lets the logged-in user open these USB serial bridges"]
    for vendor, product in sorted(set(ids)):
        lines.append(f'SUBSYSTEM=="tty", ATTRS{{idVendor}}=="{vendor}", '
                     f'ATTRS{{idProduct}}=="{product}", TAG+="uaccess"')
    return "\n".join(lines) + "\n"


def grant_command(devices: list[str]) -> list[str] | None:
    """The pkexec command that installs the rule and applies it to what's plugged in now."""
    ids = [found for found in (usb_ids(device) for device in devices) if found]
    if not ids:
        return None
    rule = udev_rule(ids)
    script = (
        f"printf %s {shlex.quote(rule)} > {RULE_PATH} && "
        "udevadm control --reload-rules && "
        "udevadm trigger --subsystem-match=tty --action=change && "
        "udevadm settle"
    )
    return ["pkexec", "sh", "-c", script]


def grant_access(devices: list[str]) -> str:
    """Asks polkit for the password, once. Returns what happened, in words."""
    command = grant_command(devices)
    if command is None:
        return "Couldn't work out which USB device those ports belong to."
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode == 0:
        return "Serial access granted. It applies now and whenever the device is plugged in."
    if done.returncode in (126, 127):
        return "Cancelled, or polkit isn't available. Run: sudo usermod -aG dialout $USER"
    return (done.stderr or done.stdout).strip() or f"pkexec exited with {done.returncode}"
