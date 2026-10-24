"""
Finding item trackers: AirTags and other Find My accessories, Google Find Hub (Android) tags,
Samsung SmartTags, Tile, Chipolo and Pebblebee, so they can be pinned on the plan. It's the
same check phones make to warn about a tracker travelling with someone, done room by room.

Only advertisements are read, through BlueZ over D-Bus, since recognising a tracker needs the
manufacturer and service data that ``btmgmt find`` doesn't show. Nothing is connected to.
The rules follow AirGuard (seemoo-lab, TU Darmstadt), the open-source tracker detector:

- Apple Find My: manufacturer 0x004C, message type 0x12. Bits 4-5 of the status byte say what
  it is: 0 an iPhone, iPad or Mac, 1 an AirTag, 2 a licensed Find My accessory, 3 AirPods.
  Only 1 and 2 are tags. A 25-byte payload (length 0x19) means it has been away from its
  owner for a while, and its address then holds for a day, so it can be followed.
- Google Find Hub: service data 0xFEAA, frame 0x40 or 0x41; 0x41 is set when it hasn't been
  near its owner for hours. Advertising flags 0x06 mark a tag, 0x02 a phone.
- Samsung SmartTag: service data 0xFD5A with (first byte & 0xF8) == 0x10; the low three bits
  say how long it has been offline from its owner.
- Tile: service data 0xFEED starting 02 00. Chipolo (0xFE33) and Pebblebee (0xFA25) by
  service UUID.

Most of these rotate their address while near their owner (every 15 minutes or so), so one
heard at a single point may be a different address at the next; those are shown where they
were strongest. Ones away from their owner keep an address far longer and can be located.
"""

from __future__ import annotations

import time

from . import bluetooth

APPLE = 0x004C
BATTERY = ("full", "medium", "low", "very low")
LABELS = {
    "airtag": "AirTag", "findmy": "Find My accessory", "google": "Find Hub tag",
    "samsung": "Samsung SmartTag", "tile": "Tile", "chipolo": "Chipolo", "pebblebee": "Pebblebee",
}


def _uuid16(uuid: str) -> str:
    """'0000feaa-0000-1000-8000-00805f9b34fb' → 'feaa'."""
    return uuid.lower()[4:8] if len(uuid) >= 8 else uuid.lower()


def classify(manufacturer: dict[int, bytes], service_data: dict[str, bytes],
             service_uuids: list[str] | None = None, flags: bytes | None = None) -> dict | None:
    """
    What tracker an advertisement comes from: {"kind", "label", "away", "state", "battery"},
    or None when it isn't one (phones, laptops and AirPods included).
    ``away`` is True when it has been apart from its owner, False when it's with them, and
    None when the tag doesn't say.
    """
    service = {_uuid16(k): bytes(v) for k, v in service_data.items()}
    uuids = {_uuid16(u) for u in service_uuids or []}

    apple = bytes(manufacturer.get(APPLE, b""))
    if len(apple) >= 3 and apple[0] == 0x12:
        kind = {1: "airtag", 2: "findmy"}.get((apple[2] & 0x30) >> 4)
        if kind:
            away = apple[1] == 0x19
            return _found(kind, away, "away from its owner" if away else "with its owner nearby",
                          BATTERY[(apple[2] >> 6) & 0x03])

    google = service.get("feaa", b"")
    if google and google[0] in (0x40, 0x41):
        flag = flags[0] if flags else None
        if flag == 0x02:
            return None                       # a phone on Find Hub, not a tag
        away = bool(google[0] & 0x01)
        return _found("google", away, "hasn't been near its owner for hours" if away
                      else "near its owner recently")

    samsung = service.get("fd5a", b"")
    if samsung and samsung[0] & 0xF8 == 0x10:
        bits = samsung[0] & 0x07
        state = {0b011: (True, "away from its owner for a long time"),
                 0b010: (True, "away from its owner"),
                 0b001: (False, "only just apart from its owner")}.get(bits, (False, "with its owner"))
        return _found("samsung", *state)

    tile = service.get("feed", b"")
    if tile[:2] == b"\x02\x00":
        return _found("tile", None, "")
    if "fe33" in uuids or "fe33" in service:
        return _found("chipolo", None, "")
    if "fa25" in uuids or "fa25" in service:
        return _found("pebblebee", None, "")
    return None


def _found(kind: str, away: bool | None, state: str, battery: str = "") -> dict:
    return {"kind": kind, "label": LABELS[kind], "away": away, "state": state, "battery": battery}


# ---------- scanning ----------

def available() -> bool:
    return bluetooth.available()


def scan(seconds: float = 8.0, log=None) -> list[dict]:
    """
    Listens for Bluetooth LE advertisements on every Bluetooth adapter at once and returns the
    trackers heard, strongest first: {"address", "rssi", "kind", "label", "away", "state",
    "battery", "adapters"}. Empty when there's no adapter or no BlueZ, so a survey point still
    stands.
    """
    with bluetooth._lock:                   # the adapters, shared with the device scan
        return _scan(seconds, log)


def _scan(seconds: float, log) -> list[dict]:
    from gi.repository import Gio, GLib

    if not available() or not bluetooth.powered_on(log):
        return []
    adapters: list[str] = []
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)

        def call(path, interface, method, args=None):
            return bus.call_sync("org.bluez", path, interface, method, args, None,
                                 Gio.DBusCallFlags.NONE, 10000, None)

        objects = call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects").unpack()[0]
        found = sorted(p for p, i in objects.items() if "org.bluez.Adapter1" in i)
        if not found:
            if log:
                log("No Bluetooth adapter for BlueZ to scan with.")
            return []
        for adapter in found:
            try:
                call(adapter, "org.freedesktop.DBus.Properties", "Set",
                     GLib.Variant("(ssv)", ("org.bluez.Adapter1", "Powered", GLib.Variant("b", True))))
                call(adapter, "org.bluez.Adapter1", "SetDiscoveryFilter", GLib.Variant("(a{sv})", ({
                    "Transport": GLib.Variant("s", "le"), "DuplicateData": GLib.Variant("b", True)},)))
                call(adapter, "org.bluez.Adapter1", "StartDiscovery")
                adapters.append(adapter)
            except Exception as error:
                if log:
                    log(f"Finder tags: {adapter.rsplit('/', 1)[-1]} couldn't scan ({error})")
        if not adapters:
            return []
        try:
            time.sleep(seconds)
            # Read while still discovering: BlueZ forgets each device's signal once it stops,
            # which is also how devices remembered from earlier scans are told apart
            objects = call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects").unpack()[0]
        finally:
            for adapter in adapters:
                try:
                    call(adapter, "org.bluez.Adapter1", "StopDiscovery")
                except Exception:
                    pass
    except Exception as error:
        if log:
            log(f"Finder tag scan failed: {error}")
        return []
    return merge([from_objects(objects, adapter) for adapter in adapters])


def merge(found: list[list[dict]]) -> list[dict]:
    """
    Trackers heard by several adapters, each once: at its strongest reading, away from its
    owner if any adapter caught it saying so, with how many adapters heard it.
    """
    merged: dict[str, dict] = {}
    for tags in found:
        for tag in tags:
            kept = merged.get(tag["address"])
            if kept is None:
                merged[tag["address"]] = {**tag, "adapters": 1}
                continue
            kept["adapters"] += 1
            if tag["rssi"] > kept["rssi"]:
                kept.update({k: v for k, v in tag.items() if k not in ("away", "state") or v},
                            adapters=kept["adapters"])
            if tag.get("away") and not kept.get("away"):
                kept["away"], kept["state"] = True, tag.get("state", kept.get("state", ""))
    return sorted(merged.values(), key=lambda t: t["rssi"], reverse=True)


def from_objects(objects: dict, adapter: str) -> list[dict]:
    """The trackers among BlueZ's devices heard in this scan (they carry a signal strength)."""
    found = []
    for path, interfaces in objects.items():
        device = interfaces.get("org.bluez.Device1")
        if not device or "RSSI" not in device or not path.startswith(adapter + "/"):
            continue
        tag = classify({int(k): bytes(v) for k, v in device.get("ManufacturerData", {}).items()},
                       {k: bytes(v) for k, v in device.get("ServiceData", {}).items()},
                       list(device.get("UUIDs", [])),
                       bytes(device["AdvertisingFlags"]) if "AdvertisingFlags" in device else None)
        if tag:
            found.append({"address": device["Address"].upper(), "rssi": int(device["RSSI"]), **tag})
    return sorted(found, key=lambda t: t["rssi"], reverse=True)
