"""Scanning and link state through the tools Linux already provides.

``iw`` gives true dBm but wants root; NetworkManager answers unprivileged and reports a
0-100 quality, which is mapped back onto dBm so everything downstream sees one unit.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import oui

SYS_NET = Path("/sys/class/net")


@dataclass(slots=True)
class Reading:
    ssid: str
    bssid: str
    rssi: int
    freq_mhz: float
    channel: int
    secured: bool
    vendor: str = ""
    security: str = ""
    # "iw" is a real dBm reading; "nmcli" is NetworkManager's 0-100 quality mapped onto dBm,
    # which is only an estimate. Kept with every point so the two are never silently mixed.
    source: str = "iw"
    # The whole channel the AP transmits on, which is wider than its primary 20 MHz on 40,
    # 80, 160 and 320 MHz networks. Only iw reports it; 0 means just the primary channel.
    center_mhz: float = 0.0
    width_mhz: int = 20
    # How long before the scan finished the access point was last heard; -1 when unknown.
    # The kernel keeps results for about 30 s, so an old one belongs somewhere else.
    age_ms: int = -1
    # The adapter's correction, already added to rssi (see adapter_offset)
    offset_db: float = 0.0
    # What the beacon says about how the network is set up, for the configuration audit
    rates: list[float] = field(default_factory=list)
    basic_rates: list[float] = field(default_factory=list)
    beacon_interval: int = 0
    auth: str = ""                    # authentication suites, as iw names them
    roam_k: bool = False              # 802.11k: neighbour reports
    roam_v: bool = False              # 802.11v: BSS transition management
    roam_r: bool = False              # 802.11r: fast transition
    # The access point's clock from its beacon or probe response, which starts from zero when
    # its radio starts: so it's the radio's uptime. 0 when unknown (nmcli doesn't report it).
    tsf_us: int = 0

    def beacon_details(self) -> dict:
        return {"rates": self.rates, "basic_rates": self.basic_rates,
                "beacon_interval": self.beacon_interval, "auth": self.auth,
                "roam_k": self.roam_k, "roam_v": self.roam_v, "roam_r": self.roam_r}

    @property
    def band(self) -> str:
        return "2.4" if self.freq_mhz < 3000 else "5" if self.freq_mhz < 5925 else "6"

    @property
    def span_center_mhz(self) -> float:
        return self.center_mhz or self.freq_mhz


# Alfa sells these chipsets under these names. The USB id is the sure mark;
# the driver is the fallback when sysfs doesn't expose the id.
ALFA_BY_USB = {
    ("0e8d", "7612"): "Alfa AWUS036ACM",
    ("0e8d", "7961"): "Alfa AWUS036AXML",
}
ALFA_BY_DRIVER = {
    "mt76x2u": "Alfa AWUS036ACM",
    "mt7921u": "Alfa AWUS036AXML",
}


@dataclass(slots=True)
class Interface:
    name: str
    driver: str
    mode: str
    monitor_capable: bool
    model: str = ""
    usb_id: str = ""

    def __str__(self) -> str:
        label = self.model or self.driver or "driver unknown"
        return f"{self.name} ({label}), {self.mode}" + (
            "" if self.monitor_capable else ", no monitor mode"
        )


def model_for(vendor: str, product: str, driver: str) -> str:
    found = ALFA_BY_USB.get((vendor.lower(), product.lower()))
    if found:
        return found
    return ALFA_BY_DRIVER.get(driver, "")


def label_for(iface: Interface) -> str:
    detail = iface.model or iface.driver
    return f"{iface.name} — {detail}" if detail else iface.name


def device_from_label(text: str) -> str:
    if text in ("", "none"):
        return ""
    return text.split(" — ", 1)[0].strip()


def choose_roles(ifaces: list[Interface]) -> tuple[str | None, str | None]:
    """Pick a scanning radio and a monitor radio.

    An Alfa is kept for monitor mode when another radio can scan, so capturing
    does not knock the machine off its network. One radio does both jobs.
    """
    if not ifaces:
        return None, None
    pool = [iface for iface in ifaces if iface.monitor_capable] or list(ifaces)
    monitor = min(pool, key=_monitor_rank)
    others = [iface for iface in ifaces if iface.name != monitor.name and iface.mode != "monitor"]
    alfas = [iface for iface in others if iface.model.startswith("Alfa")]
    if alfas:
        # A second Alfa scans. The first one stays free for monitor mode.
        scan = min(alfas, key=_monitor_rank)
    elif others:
        scan = min(others, key=lambda iface: (bool(iface.model), iface.name))
    else:
        scan = monitor
    return scan.name, monitor.name


def _monitor_rank(iface: Interface) -> tuple:
    preference = {"Alfa AWUS036AXML": 0, "Alfa AWUS036ACM": 1}.get(iface.model, 5)
    return (preference, 0 if iface.monitor_capable else 1, iface.name)


def run(*args: str, timeout: float = 20) -> tuple[int, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return done.returncode, (done.stdout + done.stderr).strip()
    except FileNotFoundError:
        return 127, f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} timed out"


def have(program: str) -> bool:
    return any(
        os.access(os.path.join(path, program), os.X_OK)
        for path in os.environ.get("PATH", "").split(":")
        if path
    )


def is_root() -> bool:
    return os.geteuid() == 0


def app_argv() -> list[str]:
    """How this process was started, as an absolute command sudo and pkexec can run."""
    import shutil
    import sys

    argv0 = sys.argv[0]
    candidate = os.path.realpath(argv0) if os.path.sep in argv0 else ""
    if candidate and os.access(candidate, os.X_OK):
        return [candidate, *sys.argv[1:]]
    found = shutil.which("wifisurveyor")
    if found:
        return [os.path.realpath(found), *sys.argv[1:]]
    return [sys.executable, "-m", "wifisurveyor", *sys.argv[1:]]


def _display_env() -> list[str]:
    """
    How the root copy reaches this session's display. On Wayland it connects straight to the
    compositor's socket, named by its full path so root needs nothing else of this user's.
    Going through XWayland instead breaks touch on popovers: a tapped dropdown's list closes
    before the tap can choose from it. Text goes through GNOME's own input method (Wayland
    text-input) so its on-screen keyboard opens on a tapped text box: the IBus module can't
    reach this user's IBus daemon from root, and then no keyboard appears. X11 stays as the
    fallback.
    """
    name = os.environ.get("WAYLAND_DISPLAY")
    if name:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        socket = name if name.startswith("/") else os.path.join(runtime, name)
        if os.path.exists(socket):
            return [f"WAYLAND_DISPLAY={socket}", "GDK_BACKEND=wayland,x11", "GTK_IM_MODULE=wayland"]
    return ["GDK_BACKEND=x11"]


def root_argv(passwordless_sudo: bool) -> list[str]:
    """Command that starts this app as root with the current display."""
    program = app_argv()
    display = _display_env()
    if passwordless_sudo:
        return ["sudo", "-n", "-E", "env", *display, *program]
    kept = []
    for key in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
                "HOME", "DBUS_SESSION_BUS_ADDRESS", "LANG", "PATH"):
        value = os.environ.get(key)
        if value:
            kept.append(f"{key}={value}")
    return ["pkexec", "env", *kept, *display, *program]


def relaunch_as_root() -> None:
    """Replace this process with a root copy. Asks polkit unless sudo needs no password."""
    if is_root():
        raise RuntimeError("Already running as root.")
    if os.environ.get("DISPLAY") and have("xhost"):
        subprocess.run(["xhost", "+si:localuser:root"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    passwordless = subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0
    command = root_argv(passwordless)
    os.execvp(command[0], command)


def channel_for(freq_mhz: float) -> int:
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz <= 2472:
        return int(round((freq_mhz - 2407) / 5))
    if 5000 <= freq_mhz < 5925:
        return int(round((freq_mhz - 5000) / 5))
    if freq_mhz == 5935:
        return 2                                   # the one 6 GHz channel off the 5950 grid
    if 5950 <= freq_mhz <= 7125:
        return int(round((freq_mhz - 5950) / 5))
    return 0


def wireless_names() -> list[str]:
    if not SYS_NET.is_dir():
        return []
    return [
        path.name for path in sorted(SYS_NET.iterdir())
        if (path / "wireless").is_dir() or (path / "phy80211").exists()
    ]


def _usb_identity(iface: Path) -> tuple[str, str]:
    """Vendor and product ids from the USB device behind a wireless interface."""
    current = iface / "device"
    if not current.exists():
        return "", ""
    try:
        current = current.resolve()
    except OSError:
        return "", ""
    for _ in range(8):
        vendor, product = current / "idVendor", current / "idProduct"
        if vendor.is_file() and product.is_file():
            try:
                return vendor.read_text().strip().lower(), product.read_text().strip().lower()
            except OSError:
                return "", ""
        if current.parent == current:
            break
        current = current.parent
    return "", ""


_interface_cache: tuple[float, list[Interface]] | None = None


def interfaces(refresh: bool = False) -> list[Interface]:
    """Wireless interfaces. Cached briefly so opening the window doesn't run `iw` four times."""
    global _interface_cache
    now = time.monotonic()
    if not refresh and _interface_cache and now - _interface_cache[0] < 2:
        return list(_interface_cache[1])
    found: list[Interface] = []
    if not SYS_NET.is_dir():
        return found
    for path in sorted(SYS_NET.iterdir()):
        if not (path / "wireless").is_dir() and not (path / "phy80211").exists():
            continue
        driver = ""
        try:
            driver = (path / "device" / "driver").resolve().name
        except OSError:
            pass
        _, info = run("iw", "dev", path.name, "info")
        mode = _first(r"type (\w+)", info) or "unknown"
        vendor, product = _usb_identity(path)
        found.append(Interface(
            path.name, driver, mode, supports_monitor(path.name),
            model=model_for(vendor, product, driver),
            usb_id=f"{vendor}:{product}" if vendor else "",
        ))
    _interface_cache = (now, found)
    return list(found)


def supports_monitor(device: str) -> bool:
    """The driver lists the interface types it can be; monitor is in that list or it isn't."""
    _, info = run("iw", "dev", device, "info")
    phy = _first(r"wiphy (\d+)", info)
    _, listing = run("iw", f"phy#{phy}", "info") if phy else run("iw", "list")
    start = listing.find("Supported interface modes")
    if start < 0:
        return "* monitor" in listing
    end = listing.find("Band ", start)
    return "* monitor" in (listing[start:end] if end > start else listing[start:])


def can_scan_passively() -> bool:
    """Only iw, as root, can listen for beacons without sending probe requests."""
    return is_root() and have("iw")


def scan(device: str, passive: bool = False, freqs: list[int] | None = None) -> list[Reading]:
    """
    Prefer a real dBm scan, and fall back when it comes back empty or isn't allowed.
    ``passive`` listens for beacons instead of sending probe requests, which matters when the
    spectrum analyzer is sweeping alongside: it would otherwise see this card's own probes.
    ``freqs`` visits only those channels (MHz), which takes about a second instead of ten.
    """
    readings = _scan(device, passive, freqs)
    offset = adapter_offset(device)
    for reading in readings:
        reading.vendor = oui.vendor(reading.bssid, access_point=True)
        if offset:
            reading.rssi = int(round(reading.rssi + offset))
            reading.offset_db = offset
    return readings


def split_channels(devices: list[str], supported: dict[str, list[float]]) -> dict[str, list[int]]:
    """
    Shares the channels out so each is scanned by exactly one adapter: an access point can't be
    listed twice, and the same channel always goes to the same adapter from point to point, so
    each access point is read by one adapter throughout the survey. Channels only some adapters
    can hear are handed out first, then each goes to the adapter with the fewest so far among
    those able, the first adapter on ties, so a 2.4 GHz-only adapter takes its full share.
    """
    shares: dict[str, list[int]] = {device: [] for device in devices}
    hears = {d: {int(f) for f in supported.get(d, [])} for d in devices}
    every = {f for freqs in hears.values() for f in freqs}
    able = {f: [d for d in devices if f in hears[d]] for f in every}
    for freq in sorted(every, key=lambda f: (len(able[f]), f)):
        shares[min(able[freq], key=lambda d: (len(shares[d]), devices.index(d)))].append(freq)
    for freqs in shares.values():
        freqs.sort()
    return shares


def scan_split(devices: list[str], passive: bool = False) -> tuple[list[Reading], list[str]]:
    """
    One scan shared across several adapters at once, each taking its own channels (see
    ``split_channels``), merged into a single list with every BSSID once. An adapter that
    fails hands its channels back to the first. Returns the readings and any notes.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .monitor import hop_list

    devices = list(dict.fromkeys(d for d in devices if d))
    if len(devices) <= 1:
        return scan(devices[0], passive) if devices else [], []
    shares = split_channels(devices, {d: hop_list(d) for d in devices})
    notes: list[str] = []
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        jobs = {d: pool.submit(scan, d, passive, freqs) for d, freqs in shares.items() if freqs}
        results: dict[str, list[Reading]] = {}
        for device, job in jobs.items():
            try:
                results[device] = job.result()
            except Exception as error:
                notes.append(f"{device} couldn't scan its channels ({error}); {devices[0]} took them")
                results[device] = []
                if device != devices[0]:
                    try:
                        results[device] = scan(devices[0], passive, shares[device])
                    except Exception:
                        pass
    return merge_split(devices, shares, results), notes


MAX_SCAN_ADAPTERS = 6


def scan_adapters(main: str, monitor: str | None, every: bool, extra: list[str],
                  ifaces: list[Interface] | None = None) -> list[str]:
    """
    The adapters that share a point's scan: the main one first (it takes the channels no
    other can), then, when ``every`` is on, every other adapter not in monitor mode and not
    the one kept for monitor mode, and whatever the speed-test plan adds. Up to six.
    """
    from .connectivity import held_by

    ifaces = interfaces() if ifaces is None else ifaces
    # An adapter something holds on one network (the SSID monitor's) would be taken off its
    # channel by a scan, which looks like the outage it is watching for
    usable = [i.name for i in ifaces if i.mode != "monitor" and not held_by(i.name)]
    chosen = [main]
    if every:
        chosen += [name for name in usable if name != monitor]
    chosen += [name for name in extra if name in usable]
    return list(dict.fromkeys(d for d in chosen if d))[:MAX_SCAN_ADAPTERS]


def merge_split(devices: list[str], shares: dict[str, list[int]],
                results: dict[str, list[Reading]]) -> list[Reading]:
    """
    Every BSSID once. An adapter can still decode a beacon from a channel next to the ones it
    was given (common on 2.4 GHz), reading it several dB low; the adapter whose channels
    include the access point's own reads it on-channel, so that reading is kept. Otherwise the
    strongest, and the first adapter's on a tie.
    """
    best: dict[str, tuple[tuple, Reading]] = {}
    for rank, device in enumerate(devices):
        mine = {int(f) for f in shares.get(device, [])}
        for reading in results.get(device, []):
            score = (int(reading.freq_mhz) in mine, reading.rssi, -rank)
            key = reading.bssid.lower()
            if key not in best or score > best[key][0]:
                best[key] = (score, reading)
    return sorted((r for _s, r in best.values()), key=lambda r: r.rssi, reverse=True)


def adapter_key(device: str) -> str:
    """What identifies an adapter model: its USB id, else its driver."""
    for iface in interfaces():
        if iface.name == device:
            return f"usb:{iface.usb_id}" if iface.usb_id else f"driver:{iface.driver or device}"
    return f"driver:{device}"


def adapter_offset(device: str) -> float:
    """
    Adapters read the same signal several dB apart. This correction, set once per model on the
    Setup page, is added to every reading so surveys taken with different adapters line up.
    """
    from ..core import settings

    return float((settings.get("adapter_offsets") or {}).get(adapter_key(device), 0.0))


def set_adapter_offset(device: str, offset_db: float) -> None:
    from ..core import settings

    offsets = dict(settings.get("adapter_offsets") or {})
    key = adapter_key(device)
    if abs(offset_db) < 0.05:
        offsets.pop(key, None)
    else:
        offsets[key] = round(offset_db, 1)
    settings.put("adapter_offsets", offsets)


def compare_adapters(reference: str, other: str) -> tuple[float, int]:
    """
    Scans with both adapters at the same moment and returns how many dB ``other`` reads below
    ``reference`` (the median over access points both heard, uncorrected), and how many that was.
    """
    import statistics
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(_scan, device, False) for device in (reference, other)]
        first, second = (job.result() for job in jobs)
    theirs = {r.bssid: r.rssi for r in second if r.source == "iw"}
    differences = [r.rssi - theirs[r.bssid] for r in first if r.source == "iw" and r.bssid in theirs]
    if len(differences) < 3:
        raise RuntimeError(f"Only {len(differences)} access points were heard by both with true "
                           "dBm (needs 3, and root for iw). Try somewhere with more networks.")
    return float(statistics.median(differences)), len(differences)


def _scan(device: str, passive: bool, freqs: list[int] | None = None) -> list[Reading]:
    scanned = False
    if have("iw"):
        began = time.monotonic()
        code, text = run("iw", "dev", device, "scan",
                         *(["freq", *map(str, freqs)] if freqs else []),
                         *(["passive"] if passive else []), timeout=45)
        if code == 0:
            scanned = True
            readings = fresh(_parse_iw(text), (time.monotonic() - began) * 1000)
            if readings:
                return readings
    if have("nmcli"):
        code, text = run(
            "nmcli", "-t", "-f", "BSSID,SSID,FREQ,SIGNAL,SECURITY",
            "device", "wifi", "list", "ifname", device, "--rescan", "yes", timeout=45,
        )
        if code == 0:
            return _parse_nmcli(text)
    if scanned:
        return []
    raise RuntimeError("Nothing to scan with: install iw (needs root) or NetworkManager.")


def link(device: str) -> dict | None:
    code, text = run("iw", "dev", device, "link")
    if code == 0 and "Not connected" not in text and "Connected to" in text:
        # Only the bitrate lines say which PHY is in use. Searching the whole text would
        # read "HE" out of an SSID such as "THE HOUSE".
        rates = " ".join(line for line in text.splitlines() if "bitrate:" in line)
        return {
            "bssid": (_first(r"Connected to ([0-9a-fA-F:]{17})", text) or "").upper(),
            "ssid": _first(r"SSID: (.+)", text) or "",
            "signal": _float(_first(r"signal: (-?\d+)", text)),
            "tx_mbps": _float(_first(r"tx bitrate: ([\d.]+)", text)),
            "rx_mbps": _float(_first(r"rx bitrate: ([\d.]+)", text)),
            "generation": link_generation(rates),
        }
    if not have("nmcli"):
        return None
    code, text = run("nmcli", "-t", "-f", "GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", device)
    if code != 0:
        return None
    name = _first(r"GENERAL.CONNECTION:(.+)", text)
    if not name or name == "--":
        return None
    return {"bssid": "", "ssid": name, "signal": 0.0, "tx_mbps": 0.0, "rx_mbps": 0.0, "generation": ""}


def link_generation(rates: str) -> str:
    """From iw's bitrate lines: "EHT-MCS 11", "HE-MCS 9", "VHT-MCS 7", "MCS 15", or none."""
    words = set(re.findall(r"\b(EHT|HE|VHT)-MCS\b", rates))
    if "EHT" in words:
        return "Wi-Fi 7 (be)"
    if "HE" in words:
        return "Wi-Fi 6 (ax)"
    if "VHT" in words:
        return "Wi-Fi 5 (ac)"
    if re.search(r"\bMCS \d", rates):
        return "Wi-Fi 4 (n)"
    return "legacy"


def _channel_freq(channel: int, primary_mhz: float) -> float:
    """A channel number's centre, in the band of the primary channel it goes with."""
    if primary_mhz < 3000:
        return 2407 + 5 * channel
    if primary_mhz < 5925:
        return 5000 + 5 * channel
    return 5950 + 5 * channel


def channel_span(primary_mhz: float, ops: dict) -> tuple[float, int]:
    """
    The centre and width of the whole channel, from the operation elements iw prints: EHT
    (Wi-Fi 7, up to 320 MHz), HE's 6 GHz information, VHT (80/160 MHz on 5 GHz), then HT's
    secondary channel (40 MHz). Each is the fallback for the one before when absent or odd.
    For 160 and 320 MHz the second centre segment is the whole channel's centre, and the
    first is only its primary 80 MHz. 80+80 networks are shown as their primary 80.
    """

    def centre(segment: int | None) -> float | None:
        return _channel_freq(segment, primary_mhz) if segment else None

    def wide(seg0: int | None, seg1: int | None, width: int) -> tuple[float | None, int]:
        if width in (160, 320) and seg0 and seg1 and abs(seg1 - seg0) in (8, 16):
            return centre(seg1), width
        if width in (160, 320) and seg0 and seg1:
            return centre(seg0), 80                        # 80+80
        return centre(seg0), width

    candidates = []
    if ops.get("eht_width") in (40, 80, 160, 320):
        candidates.append(wide(ops.get("eht_seg0"), ops.get("eht_seg1"), ops["eht_width"]))
    if ops.get("he6_width") in (40, 80, 160):
        candidates.append(wide(ops.get("he6_seg0"), ops.get("he6_seg1"), ops["he6_width"]))
    vht = ops.get("vht_width")
    if vht in (1, 2, 3):
        seg1, seg2 = ops.get("vht_seg1"), ops.get("vht_seg2")
        if vht == 1 and seg1 and seg2 and abs(seg2 - seg1) == 8:
            candidates.append((centre(seg2), 160))
        else:
            candidates.append((centre(seg1), 160 if vht == 2 else 80))
    offset = ops.get("ht_offset")
    if offset in ("above", "below"):
        candidates.append((primary_mhz + (10 if offset == "above" else -10), 40))

    for middle, width in candidates:
        # Trust it only if the primary channel sits inside it
        if middle and middle - width / 2 < primary_mhz < middle + width / 2:
            return middle, width
    return primary_mhz, 20


def fresh(readings: list[Reading], scan_ms: float, slack_ms: float = 1500) -> list[Reading]:
    """Only what this scan actually heard: older results are left over from somewhere else."""
    return [r for r in readings if r.age_ms < 0 or r.age_ms <= scan_ms + slack_ms]


def _parse_iw(text: str) -> list[Reading]:
    readings: list[Reading] = []
    state: dict = {"bssid": None, "ssid": None, "freq": 0.0, "signal": 0.0, "secured": False,
                   "security": "", "ops": {}, "section": "", "age": -1, "rates": [], "basic": [],
                   "interval": 0, "auth": "", "k": False, "v": False, "tsf": 0}

    def flush() -> None:
        if state["bssid"] and state["freq"]:
            middle, width = channel_span(state["freq"], state["ops"])
            readings.append(
                Reading(
                    state["ssid"] or "",
                    state["bssid"].upper(),
                    int(round(state["signal"])),
                    state["freq"],
                    channel_for(state["freq"]),
                    state["secured"],
                    security=state["security"] or ("Open" if state["bssid"] else ""),
                    center_mhz=middle,
                    width_mhz=width,
                    age_ms=state["age"],
                    rates=sorted(set(state["rates"])),
                    basic_rates=sorted(set(state["basic"])),
                    beacon_interval=state["interval"],
                    auth=state["auth"],
                    roam_k=state["k"], roam_v=state["v"], roam_r="FT/" in state["auth"],
                    tsf_us=state["tsf"],
                )
            )
        state.update(bssid=None, ssid=None, freq=0.0, signal=0.0, secured=False, security="",
                     ops={}, section="", age=-1, rates=[], basic=[], interval=0, auth="",
                     k=False, v=False, tsf=0)

    def number(value: str) -> int | None:
        found = re.search(r"\d+", value)
        return int(found.group()) if found else None

    for raw in text.splitlines():
        line = raw.strip().removeprefix("* ")
        key, _, value = line.partition(":")
        ops = state["ops"]
        # The HE and EHT operation elements print the same field names, so note which is open
        if line.startswith("HE Operation"):
            state["section"] = "he6"
        elif line.startswith("EHT Operation"):
            state["section"] = "eht"
        elif key == "secondary channel offset":
            ops["ht_offset"] = value.strip()
        elif key == "channel width":                       # VHT's, lower case
            ops["vht_width"] = number(value)
        elif key.startswith("center freq segment "):
            ops["vht_seg" + key[-1]] = number(value)
        elif key == "Channel Width" and state["section"]:
            # "80 MHz", "320 MHz", or HE's "80+80 or 160 MHz", which is 160 by its segments
            widths = [int(n) for n in re.findall(r"\d+", value)]
            ops[state["section"] + "_width"] = max(widths) if widths else None
        elif key.startswith("Center Frequency Segment ") and state["section"]:
            ops[f"{state['section']}_seg{key[-1]}"] = number(value)
        elif re.match(r"BSS [0-9a-fA-F]{2}:", line):     # not "BSS Transition" and the like
            flush()
            state["bssid"] = _first(r"BSS ([0-9a-fA-F:]{17})", line)
        elif line.startswith("freq:"):
            state["freq"] = _float(line[5:])
        elif line.startswith(("Supported rates:", "Extended supported rates:")):
            for rate in value.split():
                mbps = _float(rate.rstrip("*"))
                if mbps:
                    state["rates"].append(mbps)
                    if rate.endswith("*"):
                        state["basic"].append(mbps)
        elif line.startswith("beacon interval:"):
            state["interval"] = int(_float(_first(r"(\d+)", value)))
        elif key == "Authentication suites" and not state["auth"]:
            state["auth"] = value.strip()
        elif line == "BSS Transition":
            state["v"] = True
        elif line.startswith("RM enabled capabilities"):
            state["k"] = True
        elif line.startswith("TSF:"):
            state["tsf"] = int(_float(_first(r"TSF: (\d+)", line)))
        elif line.startswith("last seen:"):
            state["age"] = int(_float(_first(r"(\d+) ms", line)))
        elif line.startswith("signal:"):
            state["signal"] = _float(_first(r"(-?\d+(?:\.\d+)?)", line))
        elif line.startswith("SSID:"):
            # iw prints a hidden network's name as nothing, or as the NULs it's padded with
            name = line[5:].strip()
            state["ssid"] = "" if not name.replace("\\x00", "").strip() else name
        elif line.startswith(("RSN:", "WPA:")):
            state["secured"] = True
            state["security"] = "WPA" if line.startswith("WPA:") else "RSN"
    flush()
    return readings


def _split_nmcli(line: str) -> list[str]:
    """nmcli -t separates fields with ':' and escapes ':' and '\\' with a backslash."""
    fields: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line):
            current.append(line[index + 1])
            index += 2
            continue
        if char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    fields.append("".join(current))
    return fields


def _parse_nmcli(text: str) -> list[Reading]:
    readings = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = _split_nmcli(line)
        if len(fields) < 4:
            continue
        bssid = fields[0].upper()
        ssid, freq_text, signal_text = fields[1], fields[2], fields[3]
        security = fields[4].strip() if len(fields) > 4 else ""
        if security == "--":
            security = ""
        freq = _float(freq_text.split()[0] if freq_text else "")
        # NetworkManager reports 0-100; map it the way Windows does, onto -100..-50 dBm
        rssi = int(round(_float(signal_text) / 2 - 100))
        readings.append(
            Reading(ssid, bssid, rssi, freq, channel_for(freq), bool(security), security=security or "Open",
                    source="nmcli")
        )
    return readings


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _float(text: str | None) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return 0.0
