"""
Manufacturer names for MAC addresses, from the IEEE registries.

A MAC's first 24 bits (MA-L), 28 bits (MA-M) or 36 bits (MA-S, and the older IAB blocks) name
who it was assigned to; the longest match wins, since the smaller blocks are carved out of
large ones the IEEE keeps for itself. The lists come from a download into the app's data
folder, or failing that the copy Debian and Ubuntu ship in the ieee-data package.
"""

from __future__ import annotations

import csv
import os
import threading
from . import outbound
from pathlib import Path

REGISTRIES = {             # file name: where the IEEE publishes it
    "oui.csv": "https://standards-oui.ieee.org/oui/oui.csv",
    "mam.csv": "https://standards-oui.ieee.org/oui28/mam.csv",
    "oui36.csv": "https://standards-oui.ieee.org/oui36/oui36.csv",
    "iab.csv": "https://standards-oui.ieee.org/iab/iab.csv",
}
SYSTEM_DIRS = (Path("/usr/share/ieee-data"), Path("/var/lib/ieee-data"))
PREFIX_LENGTHS = (9, 7, 6)          # hex digits: MA-S and IAB, MA-M, MA-L

_lock = threading.Lock()
_table: dict[str, str] | None = None
_source = ""


def download_dir() -> Path:
    from ..core.project import data_dir

    return data_dir()


def _folder() -> Path | None:
    """Where the lists are read from: a download when there is one, else the system's copy."""
    for folder in (download_dir(), *SYSTEM_DIRS):
        if (folder / "oui.csv").is_file():
            return folder
    return None


def _load() -> dict[str, str]:
    global _table, _source
    with _lock:
        if _table is not None:
            return _table
        table: dict[str, str] = {}
        folder = _folder()
        if folder is not None:
            for name in REGISTRIES:
                path = folder / name
                if not path.is_file():
                    continue
                try:
                    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
                        # Registry, Assignment, Organization Name, Organization Address
                        for row in csv.reader(handle):
                            if len(row) >= 3 and row[0] != "Registry":
                                assignment, name_ = row[1].strip().upper(), row[2].strip()
                                if len(assignment) in PREFIX_LENGTHS and name_:
                                    table[assignment] = name_
                except OSError:
                    continue
            _source = str(folder)
        _table = table
        return table


def reload() -> None:
    """Forget what was read, so a fresh download is used from the next lookup."""
    global _table
    with _lock:
        _table = None


def warm() -> None:
    """Reads the lists on a worker thread, so the first lookup on the main loop is instant."""
    threading.Thread(target=_load, daemon=True).start()


def randomised(mac: str) -> bool:
    """The locally administered bit: a phone's private address, or a virtual BSSID."""
    try:
        return bool(int(mac.replace("-", ":").split(":")[0], 16) & 0x02)
    except ValueError:
        return False


def vendor(mac: str, access_point: bool = False) -> str:
    """
    Who made the device, or "" when nobody's registered for it. A locally administered
    address isn't in the registry: for an access point that's usually a second SSID on the same
    radio, made by flipping that bit on its real address, so that's tried; for anything else
    it's a private (randomised) address.
    """
    digits = "".join(ch for ch in mac.upper() if ch in "0123456789ABCDEF")
    if len(digits) < 6:
        return ""
    table = _load()
    if randomised(mac):
        if not access_point:
            return "Private address"
        real = f"{int(digits[:2], 16) & ~0x02:02X}{digits[2:]}"
        found = _match(table, real)
        return f"{found} (virtual BSSID)" if found else "Virtual BSSID"
    return _match(table, digits)


def _match(table: dict[str, str], digits: str) -> str:
    for length in PREFIX_LENGTHS:
        found = table.get(digits[:length])
        if found and found != "Private":
            return found
    return ""


def describe() -> str:
    table = _load()
    if not table:
        return "not found — download it to see who made each device"
    where = "downloaded" if _source == str(download_dir()) else f"from {_source}"
    stamp = ""
    try:
        path = Path(_source) / "oui.csv"
        import time

        stamp = time.strftime(", %d %B %Y", time.localtime(path.stat().st_mtime))
    except OSError:
        pass
    return f"{len(table):,} manufacturers, {where}{stamp}"


def download(progress=None) -> str:
    """Fetches every registry into the data folder, each replacing the old one only when whole."""
    from ..core.project import hand_back

    target = download_dir()
    for number, (name, url) in enumerate(REGISTRIES.items(), start=1):
        if progress:
            progress(f"Downloading {name} ({number} of {len(REGISTRIES)})…")
        # Through net.outbound, which gives the IEEE's server the browser-like identity it wants
        partial = target / (name + ".part")
        with outbound.open_url(url, timeout=60) as response, partial.open("wb") as out:
            while chunk := response.read(1 << 16):
                out.write(chunk)
        if partial.stat().st_size < 1000 or not partial.read_bytes()[:40].startswith(b"Registry"):
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"{url} didn't send the registry.")
        hand_back(partial)
        os.replace(partial, target / name)
    reload()
    return describe()
