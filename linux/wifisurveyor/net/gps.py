"""GPS through gpsd when it's running, or straight NMEA from a serial port."""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass


@dataclass(slots=True)
class Fix:
    lat: float
    lon: float
    quality: int
    satellites: int
    hdop: float | None
    accuracy_m: float | None
    when: float

    @property
    def good(self) -> bool:
        if self.quality == 0:
            return False
        if self.hdop is not None and self.hdop <= 2 and self.satellites >= 5:
            return True
        return self.accuracy_m is not None and self.accuracy_m <= 5

    @property
    def fresh(self) -> bool:
        return time.time() - self.when < 5

    def describe(self) -> str:
        parts = [f"quality {self.quality}", f"{self.satellites} sats"]
        if self.hdop is not None:
            parts.append(f"HDOP {self.hdop:.1f}")
        if self.accuracy_m is not None:
            parts.append(f"±{self.accuracy_m:.0f} m")
        text = ", ".join(parts)
        return text + ("" if self.fresh else " (stale)") + ("" if self.good else " (weak)")


class GpsSource:
    """Runs in the background and keeps the most recent fix."""

    def __init__(self) -> None:
        self.fix: Fix | None = None
        self.status = "off"
        self._stop = threading.Event()

    def _spawn(self, target, args: tuple) -> None:
        """Stops the previous reader. The old thread keeps its own event, so it exits."""
        self._stop.set()
        self._stop = threading.Event()
        threading.Thread(target=target, args=(*args, self._stop), daemon=True).start()

    def start_gpsd(self, host: str = "127.0.0.1", port: int = 2947) -> None:
        self._spawn(self._gpsd_loop, (host, port))

    def start_serial(self, port: str, baud: int = 9600) -> None:
        self._spawn(self._serial_loop, (port, baud))

    def stop(self) -> None:
        self._stop.set()
        self.status = "off"

    def _gpsd_loop(self, host: str, port: int, stop: threading.Event) -> None:
        try:
            with socket.create_connection((host, port), timeout=5) as connection:
                connection.settimeout(2.0)
                connection.sendall(b'?WATCH={"enable":true,"json":true}\n')
                self.status = "gpsd"
                stream = connection.makefile("r")
                while not stop.is_set():
                    try:
                        line = stream.readline()
                    except socket.timeout:
                        continue
                    if not line:
                        break
                    try:
                        report = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if report.get("class") != "TPV" or "lat" not in report:
                        continue
                    if stop.is_set():
                        break
                    self.fix = Fix(
                        lat=float(report["lat"]), lon=float(report["lon"]),
                        quality=int(report.get("mode", 0)),
                        satellites=int(report.get("nSat", 0)),
                        hdop=report.get("hdop"),
                        accuracy_m=report.get("eph"),
                        when=time.time(),
                    )
        except Exception as error:
            if not stop.is_set():
                self.status = f"gpsd: {error}"

    def _serial_loop(self, port: str, baud: int, stop: threading.Event) -> None:
        import serial

        try:
            with serial.Serial(port, baud, timeout=2) as link:
                self.status = f"NMEA on {port}"
                while not stop.is_set():
                    line = link.readline().decode("ascii", "replace").strip()
                    fix = parse_gga(line)
                    if fix and not stop.is_set():
                        self.fix = fix
        except Exception as error:
            if not stop.is_set():
                self.status = f"{port}: {error}"


def parse_gga(line: str) -> Fix | None:
    if not line.startswith("$") or "*" not in line or "GGA" not in line[:8]:
        return None
    body, checksum = line[1:].split("*", 1)
    total = 0
    for char in body:
        total ^= ord(char)
    if f"{total:02X}" != checksum[:2].upper():
        return None

    fields = body.split(",")
    if len(fields) < 10 or not fields[2] or not fields[6] or fields[6] == "0":
        return None
    return Fix(
        lat=_degrees(fields[2], fields[3]),
        lon=_degrees(fields[4], fields[5]),
        quality=int(fields[6]),
        satellites=int(fields[7] or 0),
        hdop=float(fields[8]) if fields[8] else None,
        accuracy_m=None,
        when=time.time(),
    )


def _degrees(value: str, hemisphere: str) -> float:
    raw = float(value)                      # NMEA is ddmm.mmmm
    degrees = int(raw // 100) + (raw % 100) / 60
    return -degrees if hemisphere in ("S", "W") else degrees


# USB ids that are GPS receivers rather than generic serial bridges.
GPS_IDS = {
    (0x1546, 0x01A4), (0x1546, 0x01A5), (0x1546, 0x01A6),  # u-blox 4/5/6
    (0x1546, 0x01A7), (0x1546, 0x01A8), (0x1546, 0x01A9),  # u-blox 7/8/9
    (0x1546, 0x01AA),
    (0x091E, 0x0003),  # Garmin GPS
    (0x067B, 0xAAA0),  # Prolific GPS (some Globalsat mice)
}
GPS_VENDORS = {0x091E}  # Garmin
GPS_WORDS = ("gps", "nmea", "u-blox", "ublox", "garmin", "globalsat", "bu-353", "bu353",
             "holux", "qstarz", "skytraq")
RF_WORDS = ("rf explorer", "rfexplorer", "rf-explorer")
TINYSA_WORDS = ("tinysa",)
# The tinySA's USB id. It's the STM32's stock virtual COM port, which NanoVNAs share, so
# a port with it is asked rather than assumed to be a tinySA.
TINYSA_IDS = {(0x0483, 0x5740)}
# Bridges the RF Explorer actually ships with. Probed, not assumed: CP2102 is common.
UART_IDS = {(0x10C4, 0xEA60), (0x0403, 0x6001), (0x1A86, 0x7523)}


# Chip names for the bridges above, so a label says more than "USB serial".
UART_NAMES = {(0x10C4, 0xEA60): "CP2102", (0x0403, 0x6001): "FT232", (0x1A86, 0x7523): "CH340"}


@dataclass(slots=True)
class SerialDevice:
    device: str
    kind: str
    description: str

    @property
    def label(self) -> str:
        text = f"{self.device} — {self.description}" if self.description else self.device
        return text if accessible(self.device) else f"{text} (no permission)"


def accessible(device: str) -> bool:
    """Whether this user can open the port. Serial ports usually belong to the dialout group."""
    import os

    return not os.path.exists(device) or os.access(device, os.R_OK | os.W_OK)


def classify_serial(vendor: int | None, product: int | None, text: str) -> tuple[str, str]:
    """Returns (kind, short description). kind is gps, rf, uart, or other."""
    blob = text.lower()
    if any(word in blob for word in RF_WORDS):
        return "rf", "RF Explorer"
    if any(word in blob for word in TINYSA_WORDS):
        return "rf", "tinySA"
    if (vendor, product) in TINYSA_IDS:
        return "rf", "tinySA or NanoVNA"
    if (vendor, product) in GPS_IDS or vendor in GPS_VENDORS or any(word in blob for word in GPS_WORDS):
        name = "u-blox" if vendor == 0x1546 else "Garmin" if vendor == 0x091E else "GPS"
        if "u-blox" in blob or "ublox" in blob:
            name = "u-blox"
        return "gps", name
    if (vendor, product) in UART_IDS:
        return "uart", f"USB serial ({UART_NAMES[(vendor, product)]})"
    return "other", text.strip() or "serial"


def _placeholder(port) -> bool:
    """
    The kernel creates /dev/ttyS0-31 whether or not a UART sits behind them. pyserial lists
    them all with no hardware id, which buries the real ports, so those are left out.
    """
    return port.vid is None and (port.description in (None, "", "n/a")) and \
        port.device.startswith("/dev/ttyS")


def serial_devices() -> list[SerialDevice]:
    found: dict[str, SerialDevice] = {}
    try:
        from serial.tools import list_ports

        for port in list_ports.comports():
            if not port.device or _placeholder(port):
                continue
            text = " ".join(part for part in (
                port.manufacturer, port.product, port.description, port.hwid, port.serial_number,
            ) if part)
            kind, description = classify_serial(port.vid, port.pid, text)
            found[port.device] = SerialDevice(port.device, kind, description)
    except Exception:
        pass
    from pathlib import Path

    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        for path in by_id.iterdir():
            try:
                device = str(path.resolve())
            except OSError:
                continue
            if device in found:
                continue
            kind, description = classify_serial(None, None, path.name.replace("_", " "))
            found[device] = SerialDevice(device, kind, description)
    if not found:
        for pattern in ("ttyUSB*", "ttyACM*"):
            for path in Path("/dev").glob(pattern):
                found[str(path)] = SerialDevice(str(path), "other", "serial")
    return sorted(found.values(), key=lambda item: item.device)


def serial_ports() -> list[str]:
    return [item.device for item in serial_devices()]


def port_labels(prefer: str | None = None) -> list[str]:
    devices = serial_devices()
    if prefer:
        devices.sort(key=lambda item: (item.kind != prefer, item.kind == "other", item.device))
    return [item.label for item in devices]


def port_device(label: str) -> str:
    if not label or label.startswith("no "):
        return ""
    return label.split(" — ", 1)[0].strip()


def gpsd_alive(host: str = "127.0.0.1", port: int = 2947) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def preferred_gps() -> tuple[str, str] | None:
    """('gpsd', '') when the daemon answers, else ('serial', device) for a GPS receiver."""
    if gpsd_alive():
        return ("gpsd", "")
    for item in serial_devices():
        if item.kind == "gps":
            return ("serial", item.device)
    return None


def rf_probe_order(skip: set[str] | None = None) -> list[str]:
    """Named analyzers (RF Explorer, tinySA) first, then USB-serial bridges that aren't the GPS."""
    skip = skip or set()
    devices = [item for item in serial_devices() if item.device not in skip and item.kind != "gps"]
    devices.sort(key=lambda item: (item.kind != "rf", item.kind != "uart", item.device))
    return [item.device for item in devices if item.kind in ("rf", "uart")]
