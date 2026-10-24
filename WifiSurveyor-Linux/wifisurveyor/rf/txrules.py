"""Where a signal generator may send, here or on a satellite: the same rules on both ends.

Over the air, a test signal stays in the US ISM bands, where low-power unlicensed operation is
allowed, at no more than -20 dBm into the antenna (a microwatt or so radiated, far below what
Part 15 allows there). Everywhere else, including the TV band mics use, a test signal may only
go down a cable: into an attenuator, a receiver's antenna input, or a shielded box. The app
can't see the cable, so whoever starts it says it's there. Every signal stops by itself after
TX_MINUTES, and a satellite also stops one when the app stops checking in (LEASE_SECONDS).

satellite/wifisurveyor_satellite.py carries a copy of these numbers (the Pi runs it without
the rest of the app); a test keeps the two the same.
"""

from __future__ import annotations

OVER_THE_AIR = ((902.0, 928.0, "900 MHz ISM"), (2400.0, 2483.5, "2.4 GHz ISM"),
                (5725.0, 5850.0, "5.8 GHz ISM"))
EDGE_MHZ = 0.5                 # kept clear of each band edge
MAX_OTA_DBM = -20.0
MAX_CABLED_DBM = 0.0
TX_MINUTES = 30
LEASE_SECONDS = 90


def over_the_air_ok(freq_mhz: float) -> bool:
    return any(low + EDGE_MHZ <= freq_mhz <= high - EDGE_MHZ for low, high, _name in OVER_THE_AIR)


def check(freq_mhz: float, level_dbm: float, cabled: bool) -> str:
    """Why this signal may not be sent, or "" if it may."""
    if cabled:
        if level_dbm > MAX_CABLED_DBM:
            return f"Down a cable it's capped at {MAX_CABLED_DBM:.0f} dBm."
        return ""
    if not over_the_air_ok(freq_mhz):
        names = ", ".join(f"{name} ({low:g}-{high:g} MHz)" for low, high, name in OVER_THE_AIR)
        return (f"{freq_mhz:.3f} MHz can't be sent over the air: only {names}. For other "
                "bands, connect the generator to the receiver by cable (with an attenuator) and "
                "choose Cabled.")
    if level_dbm > MAX_OTA_DBM:
        return f"Over the air it's capped at {MAX_OTA_DBM:.0f} dBm into the antenna."
    return ""


def describe() -> str:
    names = ", ".join(name for _low, _high, name in OVER_THE_AIR)
    return (f"Over the air: {names} only, at up to {MAX_OTA_DBM:.0f} dBm. Any other frequency "
            f"only by cable, up to {MAX_CABLED_DBM:.0f} dBm. Stops by itself after "
            f"{TX_MINUTES} minutes.")
