"""
Finding Bluetooth devices that might be sharing 2.4 GHz with the Wi-Fi.

This listens for the advertisements and inquiry responses that Bluetooth devices send anyway,
through BlueZ's ``btmgmt``. It's for spotting fixed equipment that adds to the interference —
a printer, a barcode scanner, an audio bridge, or a phone or wearable that shouldn't be there
(in a no-phone facility, or one charging and pulling updates) — so it can be found. Nothing is
paired with or connected to; it only listens.

The class each device announces says what kind of thing it is, and the address's maker comes
from the same IEEE register the Wi-Fi side uses. A device with a fixed (public) address can be
placed on the map from how its signal fades between points; one using a rotating private
address changes identity every few minutes, so it can only be reported as heard, not placed.
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import oui
from .wifi import is_root, run

# Bluetooth "Class of Device", major device class → what it is. Imaging (printers, scanners)
# and peripherals and audio are the fixed things worth finding; phones and wearables are not.
MAJOR_CLASSES = {
    1: ("computer", "Computer"),
    2: ("phone", "Phone"),
    3: ("network", "Network access point"),
    4: ("audio", "Audio / video"),
    5: ("peripheral", "Keyboard / mouse / peripheral"),
    6: ("imaging", "Printer / scanner / display"),
    7: ("wearable", "Wearable"),
    8: ("toy", "Toy"),
    9: ("health", "Health device"),
}
class BtDevice:
    __slots__ = ("address", "name", "rssi", "public", "cod", "kind", "detail", "vendor", "adapters")

    def __init__(self, address: str, name: str, rssi: int, public: bool, cod: int) -> None:
        self.address = address.upper()
        self.name = name
        self.rssi = rssi
        self.public = public                 # a fixed (public) address, not a rotating private one
        self.cod = cod
        self.kind, self.detail = classify(cod)
        self.vendor = oui.vendor(address) if public else ""
        self.adapters = 1                    # how many Bluetooth adapters heard it here

    @property
    def stable(self) -> bool:
        """A fixed (public) address, so this device can be followed from point to point."""
        return self.public

    @property
    def label(self) -> str:
        return self.name or self.vendor or self.detail or "Bluetooth device"

    def as_dict(self) -> dict:
        return {"address": self.address, "name": self.name, "rssi": self.rssi,
                "public": self.public, "cod": self.cod, "kind": self.kind,
                "detail": self.detail, "vendor": self.vendor, "adapters": self.adapters}


def classify(cod: int) -> tuple[str, str]:
    """Class of Device → a kind and a description. 0 means the device didn't say."""
    if not cod:
        return "unknown", ""
    major = (cod >> 8) & 0x1f
    kind, detail = MAJOR_CLASSES.get(major, ("unknown", "Bluetooth device"))
    if kind == "imaging":
        minor = (cod >> 2) & 0x3f
        if minor & 0x20:
            return "printer", "Printer"
        if minor & 0x10:
            return "imaging", "Scanner"
        if minor & 0x08:
            return "imaging", "Camera"
        return "imaging", "Display"
    return kind, detail


def available() -> bool:
    return shutil.which("btmgmt") is not None and is_root()


def why_unavailable() -> str:
    if shutil.which("btmgmt") is None:
        return "btmgmt isn't installed (the bluez package)."
    if not is_root():
        return "Bluetooth discovery needs root: relaunch as root."
    return ""


def controllers(root: Path = Path("/sys/class/bluetooth")) -> list[int]:
    """The Bluetooth adapters (hci0, hci1, …) by index, from sysfs."""
    try:
        names = [entry.name for entry in root.iterdir()]
    except OSError:
        return []
    return sorted(int(name[3:]) for name in names if re.fullmatch(r"hci\d+", name))


def controller_names(root: Path = Path("/sys/class/bluetooth")) -> list[str]:
    """Each adapter as "hci0 (btusb)", for the report's account of how the survey was made."""
    found = []
    for index in controllers(root):
        try:
            driver = (root / f"hci{index}" / "device" / "driver").resolve(strict=True).name
        except OSError:
            driver = ""
        found.append(f"hci{index}" + (f" ({driver})" if driver else ""))
    return found


def _btmgmt(*args: str, timeout: float = 20, index: int | None = None) -> tuple[int, str]:
    """
    Runs btmgmt with a pseudo-terminal for its input. BlueZ 5.8x's btmgmt never exits when its
    input isn't a terminal, and the app's never is, so without this every call timed out.
    """
    master, slave = pty.openpty()
    try:
        where = ["--index", str(index)] if index is not None else []
        done = subprocess.run(["btmgmt", *where, *args], stdin=slave, capture_output=True,
                              text=True, timeout=timeout)
        return done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        return 124, "btmgmt timed out"
    except OSError as error:
        return 127, str(error)
    finally:
        os.close(slave)
        os.close(master)


def powered_on(log=None, index: int | None = None) -> bool:
    """Makes sure the adapter is on (it's often rfkill-blocked)."""
    if shutil.which("rfkill"):
        run("rfkill", "unblock", "bluetooth")
    # Just unblocked, the adapter takes a moment to come back before it can be powered
    for _attempt in range(6):
        code, text = _btmgmt("power", "on", timeout=8, index=index)
        if code == 0 and "complete" in text.lower():
            return True
        time.sleep(0.5)
    if log:
        name = "the Bluetooth adapter" if index is None else f"Bluetooth adapter hci{index}"
        log(f"Couldn't power {name}: {text.strip()}")
    return False


def scan(seconds: float = 6.0, log=None) -> list[BtDevice]:
    """
    One discovery pass on every Bluetooth adapter at once, merged (see ``merge``). Returns the
    devices heard, strongest first. Needs root; returns an empty list rather than raising when
    there's no adapter, so a survey point still stands.
    """
    if not available():
        return []
    indexes = controllers() or [None]
    if len(indexes) == 1:
        return _scan_one(seconds, log, indexes[0])
    with ThreadPoolExecutor(max_workers=len(indexes)) as pool:
        found = list(pool.map(lambda index: _scan_one(seconds, log, index), indexes))
    return merge(found)


def _scan_one(seconds: float, log, index: int | None) -> list[BtDevice]:
    if not powered_on(log, index):
        return []
    code, text = _btmgmt("find", timeout=seconds + 12, index=index)
    if code != 0 and "dev_found" not in text:
        if log:
            name = "Bluetooth" if index is None else f"Bluetooth on hci{index}"
            log(f"{name} scan failed: {text.strip() or f'btmgmt exited {code}'}")
        return []
    return parse_find(text)


def merge(found: list[list[BtDevice]]) -> list[BtDevice]:
    """
    What several adapters heard at one spot, each device once: at the strongest of its
    readings (Bluetooth levels swing with which of its three advertising channels was caught,
    so the strongest is the steadiest), with whatever name and class any adapter caught, and
    how many adapters heard it.
    """
    merged: dict[str, BtDevice] = {}
    for devices in found:
        for device in devices:
            kept = merged.get(device.address)
            if kept is None:
                merged[device.address] = device
                continue
            kept.adapters += 1
            if device.rssi > kept.rssi:
                kept.rssi = device.rssi
            if device.name and not kept.name:
                kept.name = device.name
            if device.cod and not kept.cod:
                kept.cod = device.cod
                kept.kind, kept.detail = classify(device.cod)
    return sorted(merged.values(), key=lambda d: d.rssi, reverse=True)


def parse_find(text: str) -> list[BtDevice]:
    """Reads ``btmgmt find`` output into devices (strongest first, each address once)."""
    seen: dict[str, dict] = {}
    current: dict | None = None

    def flush() -> None:
        if not (current and current.get("address")):
            return
        # A device is found afresh on each advertisement; merge its sightings, keeping the
        # strongest signal and whatever name or class any one of them carried.
        kept = seen.setdefault(current["address"], {})
        kept["rssi"] = max(current.get("rssi", -100), kept.get("rssi", -200))
        for field in ("name", "public"):
            if field in current:
                kept[field] = current[field]
        if current.get("cod"):
            kept["cod"] = current["cod"]

    for line in text.splitlines():
        line = line.strip()
        found = re.search(r"dev_found:\s*([0-9A-Fa-f:]{17})\s+type\s+(.+?)\s+rssi\s+(-?\d+)", line)
        if found:
            flush()
            # BR/EDR (classic) and "LE Public" addresses are fixed; only "LE Random" rotates
            current = {"address": found.group(1),
                       "public": "Random" not in found.group(2),
                       "rssi": int(found.group(3))}
            continue
        if current is None:
            continue
        name = re.match(r"name\s+(.+)", line)
        if name:
            current["name"] = name.group(1).strip().strip("'\"")
        cls = re.search(r"(?:dev_)?class(?:_of_device)?[\s:]+((?:0x)?[0-9A-Fa-f]+)", line)
        if cls:
            current["cod"] = int(cls.group(1), 16)
    flush()
    devices = [BtDevice(addr, d.get("name", ""), d.get("rssi", -100),
                        d.get("public", True), d.get("cod", 0)) for addr, d in seen.items()]
    return sorted(devices, key=lambda d: d.rssi, reverse=True)


_lock = threading.Lock()


def scan_serialised(seconds: float = 6.0, log=None) -> list[BtDevice]:
    """As ``scan``, but only one at a time: two survey jobs can't drive the adapters at once."""
    with _lock:
        return scan(seconds, log)
