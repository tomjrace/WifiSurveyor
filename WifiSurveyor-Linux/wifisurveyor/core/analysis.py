"""What the measurements mean: metrics, verdicts, and where signals come from."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from ..net import oui
from ..rf import cellular as cell
from ..rf import iq as iqmod
from ..rf import spectrum as spec

UNEXPLAINED_DB = 6.0


@dataclass(slots=True)
class Targets:
    min_rssi: float = -70
    min_snr: float = 20
    max_airtime: float = 0.45
    min_phy_mbps: float = 100
    max_retry_rate: float = 0.20


METRICS = {
    "signal": ("Signal level", "dBm", -85, -45, True),
    "snr": ("Signal-to-noise ratio", "dB", 5, 40, True),
    "noise": ("Noise floor (per 20 MHz)", "dBm", -95, -75, False),
    "aps": ("Overlapping access points", "APs", 0, 12, False),
    "occupancy": ("Channel occupancy", "%", 0, 100, False),
    "retries": ("Retry rate", "%", 0, 30, False),
    "airtime": ("Measured airtime", "%", 0, 100, False),
    "unexplained": ("Unexplained energy", "dB", 0, 20, False),
    "verdict": ("What's limiting this spot", "", 0, 7, True),
    "requirement": ("Meets the requirement", "", 0, 1, True),
    "secondary": ("Second access point (roaming)", "dBm", -85, -55, True),
    "contention": ("Co-channel contention", "radios", 0, 6, False),
    "latency": ("Latency to the gateway", "ms", 2, 100, False),
    "loss": ("Packet loss to the gateway", "%", 0, 10, False),
    "throughput": ("Download throughput (iperf3)", "Mbps", 0, 200, True),
    "predicted": ("Predicted signal with planned access points", "dBm", -85, -45, True),
    "cellular": ("Cellular downlink (HackRF, per 5 MHz)", "dBm", -100, -40, True),
    "tone": ("Test tone level (HackRF)", "dBm", -100, -30, True),
    "bands": ("Activity in other bands (dB over the floor)", "dB", 0, 40, False),
}
REQUIREMENT_COLOURS = [(0.86, 0.15, 0.15), (0.08, 0.50, 0.24)]      # fails, passes

VERDICTS = [
    ("good", "Meets the targets", (0.08, 0.50, 0.24)),
    ("coverage", "Weak coverage", (0.86, 0.15, 0.15)),
    ("nonwifi", "Non-Wi-Fi interference", (0.58, 0.20, 0.92)),
    ("cochannel", "Neighbouring networks", (0.92, 0.35, 0.05)),
    ("congestion", "Airtime congestion", (0.79, 0.54, 0.02)),
    ("collisions", "Retries and collisions", (0.05, 0.58, 0.53)),
    ("config", "Link configuration", (0.15, 0.39, 0.92)),
    ("nodata", "Not enough data", (0.58, 0.64, 0.70)),
]
VERDICT_INDEX = {key: i for i, (key, _, _) in enumerate(VERDICTS)}


def noise_floor(point, band: str) -> float | None:
    spectrum = point.spectrum or {}
    key = {"2.4": "noise_24", "5": "noise_5", "6": "noise_6"}.get(band, "noise_24")
    return spectrum.get(key)


def wifi_power(point, center_mhz: float) -> float | None:
    """Every scanned network that lands on this channel, added up in linear power."""
    total = 0.0
    for network in point.networks:
        overlap = max(0.0, 20 - abs(network["freq_mhz"] - center_mhz)) / 20
        if overlap > 0:
            total += float(spec.to_mw(network["rssi"])) * overlap
    return float(spec.to_dbm(total)) if total > 0 else None


def measured_power(point, center_mhz: float) -> float | None:
    segment = spec.segment_for(point.spectrum, center_mhz)
    if not segment:
        return None
    return spec.channel_power(segment["max"], segment["start_mhz"], segment["step_mhz"],
                              segment["rbw_khz"], center_mhz)


def point_offset(point) -> float | None:
    """
    Card and analyzer read different absolute levels, so they can't be compared until
    referenced to each other on channels where one strong network dominates.
    """
    differences = []
    for _number, center in spec.all_channels():
        wifi = wifi_power(point, center)
        measured = measured_power(point, center)
        if wifi is None or measured is None or wifi < -70:
            continue
        differences.append(measured - wifi)
    return float(np.median(differences)) if len(differences) >= 2 else None


class Context:
    """Per-project analysis state, so offsets are worked out once."""

    def __init__(self, project, targets: Targets | None = None, profile: str = "data") -> None:
        from . import rfplan

        self.project = project
        rfplan.use_twins(radio_twins(project))
        self.profile = rfplan.PROFILES.get(profile, rfplan.PROFILES["data"])
        # Judged as a phone would hear it: every signal target raised by the allowance
        self.allowance = float(getattr(project, "client_allowance_db", 0) or 0)
        if self.allowance:
            lift = self.allowance
            self.profile = replace(
                self.profile, primary=self.profile.primary + lift,
                secondary=None if self.profile.secondary is None else self.profile.secondary + lift,
                third=None if self.profile.third is None else self.profile.third + lift)
        # The verdicts judge against the same levels as the chosen requirement
        self.targets = targets or Targets(
            min_rssi=self.profile.primary, min_snr=self.profile.snr,
            max_airtime=self.profile.max_airtime or Targets.max_airtime)
        self._offsets: dict[int, float | None] = {}
        found = [o for o in (point_offset(p) for p in project.points) if o is not None]
        self.project_offset = float(np.median(found)) if found else None
        calibration = getattr(project, "rf_calibration", None) or {}
        # Measured on purpose beats worked out from the survey
        self.calibrated = calibration.get("offset_db")
        # Which cellular band the cellular map shows: a key of rf.cellular.BANDS, or None for
        # the strongest cellular band at each point
        self.cellular_band: str | None = None
        # Which band the other-bands map shows: a key of rf.bands.BY_KEY, or None for the
        # busiest band measured at each point
        self.other_band: str | None = None

    def offset(self, point) -> float | None:
        if self.calibrated is not None:
            return float(self.calibrated)
        key = id(point)
        if key not in self._offsets:
            self._offsets[key] = point_offset(point)
        return self._offsets[key] if self._offsets[key] is not None else self.project_offset


def residual(point, center_mhz: float, offset: float) -> float | None:
    """Power beyond what the visible networks and the noise floor account for."""
    measured = measured_power(point, center_mhz)
    if measured is None:
        return None
    floor = noise_floor(point, spec.band_of(center_mhz)) or -95
    wifi = wifi_power(point, center_mhz)
    expected = max(wifi + offset, floor) if wifi is not None else floor
    return measured - expected


def iq_at(point, center_mhz: float) -> dict | None:
    """The HackRF's close look at the channel covering this frequency, if it took one here."""
    for result in point.iq or []:
        if abs(result["center_mhz"] - center_mhz) < 10:
            return result
    return None


def iq_airtime(point, center_mhz: float) -> float | None:
    found = iq_at(point, center_mhz)
    return None if found is None else found["airtime"]


def iq_interferers(point, center_mhz: float | None = None) -> list[dict]:
    """What the HackRF identified that isn't Wi-Fi, on one channel or all it looked at."""
    results = point.iq or []
    if center_mhz is not None:
        results = [r for r in results if abs(r["center_mhz"] - center_mhz) < 10]
    return [f for r in results for f in r["findings"] if f["kind"] != "wifi"]


def worst_unexplained(point, offset: float, band: str = "All") -> tuple[int, float] | None:
    worst = None
    for number, center in spec.all_channels():
        if band != "All" and spec.band_of(center) != band:
            continue
        value = residual(point, center, offset)
        if value is not None and (worst is None or value > worst[1]):
            worst = (number, value)
    return worst


def band_activity(point, key: str | None = None) -> float | None:
    """How far the strongest signal stood over the floor in a band (or the busiest band)."""
    from ..rf import bands as rfbands

    measured = point.bands or {}
    if key is not None:
        return rfbands.activity(measured.get(key))
    values = [v for v in (rfbands.activity(entry) for entry in measured.values()) if v is not None]
    return max(values) if values else None


def metric(point, name: str, ctx: Context, ssid=None, bssid=None, band="All",
           channel: int | None = None) -> float | None:
    best = point.best(ssid, bssid, band)

    if name == "signal":
        if best:
            return best["rssi"]
        # A filtered network that was not heard here is a gap, not a -100 dBm reading.
        if ssid or bssid:
            return None
        return -100 if point.networks else None
    if name == "snr":
        if best is None:
            return None
        floor = noise_floor(point, best["band"])
        return None if floor is None else max(0.0, best["rssi"] - floor)
    if name == "noise":
        return noise_floor(point, band if band in ("5", "6") else "2.4")
    if name == "aps":
        return len({n["bssid"] for n in point.networks
                    if n["rssi"] >= -75 and (band == "All" or n["band"] == band)
                    and (channel is None or n["channel"] == channel)})
    if name == "occupancy":
        # No channel chosen means the channel of the network that matters at this point:
        # the strongest one, or the strongest of the filtered SSID/BSSID.
        if channel is not None:
            number, center = channel, spec.CHANNELS.get(channel, 0)
        elif best is not None:
            number, center = best["channel"], best.get("freq_mhz") or spec.CHANNELS.get(best["channel"], 0)
        else:
            return None
        segment = spec.segment_for(point.spectrum, center)
        occupancy = (segment or {}).get("occupancy", {}).get(str(number))
        if occupancy is None:
            occupancy = (segment or {}).get("occupancy", {}).get(number)
        return None if occupancy is None else occupancy * 100
    if name == "retries":
        capture = point.capture
        return capture["retry_rate"] * 100 if capture and capture.get("retry_eligible", 0) > 20 else None
    if name == "airtime":
        capture = point.capture
        if capture:
            return capture["airtime"] * 100
        # Otherwise the HackRF's: every transmission on the channel, Wi-Fi or not
        center = spec.CHANNELS.get(channel, 0) if channel is not None else \
            (best.get("freq_mhz") or spec.CHANNELS.get(best["channel"], 0)) if best else None
        found = iq_airtime(point, center) if center else None
        return None if found is None else found * 100
    if name == "cellular":
        return cell.strongest(point.cellular, ctx.cellular_band)
    if name == "tone":
        return (point.tone or {}).get("dbm")
    if name == "bands":
        return band_activity(point, ctx.other_band)
    if name == "unexplained":
        offset = ctx.offset(point)
        if offset is None:
            return None
        if channel is not None:
            return residual(point, spec.CHANNELS.get(channel, 0), offset)
        found = worst_unexplained(point, offset, band)
        return found[1] if found else None
    if name == "verdict":
        return float(VERDICT_INDEX[diagnose(point, ctx, ssid, bssid, band)[0]])
    if name == "throughput":
        # A chosen network shows its own speed test; otherwise the main one, or the best
        if ssid:
            for result in point.throughputs:
                if result.get("ssid") == ssid and result.get("down_mbps") is not None:
                    return result["down_mbps"]
            main = point.throughput or {}
            return main.get("down_mbps") if main.get("ssid") in (None, ssid) else None
        speeds = [r["down_mbps"] for r in [point.throughput or {}, *point.throughputs]
                  if r.get("down_mbps") is not None]
        return max(speeds) if speeds else None
    if name == "predicted":
        return metric(point, "signal", ctx, ssid, bssid, band, channel)
    if name in ("latency", "loss"):
        found = point.check_for(ssid) or ({} if ssid else point.connectivity or {})
        return found.get("latency_ms" if name == "latency" else "loss")
    if name == "requirement":
        ok, _why = requirement(point, ctx, ssid, band)
        return None if ok is None else float(ok)
    if name == "secondary":
        from . import rfplan

        return rfplan.nth_strongest(point, 2, ssid, band)
    if name == "contention":
        from . import rfplan

        return rfplan.least_contention(point, ssid, band)
    return None


def requirement(point, ctx: Context, ssid=None, band="All") -> tuple[bool | None, list[str]]:
    """Whether this spot meets the chosen requirement profile, and what fails if not."""
    from . import rfplan

    def busy(each: str) -> float | None:
        value = metric(point, "occupancy", ctx, ssid=ssid, band=each)
        return None if value is None else value / 100

    return rfplan.evaluate(point, ctx.profile, ssid, band,
                           lambda each: noise_floor(point, each), busy)


def diagnose(point, ctx: Context, ssid=None, bssid=None, band="All") -> tuple[str, str, str]:
    """Stops at the first failing check, so the answer names a cause and an action."""
    t = ctx.targets
    if not point.networks:
        return "nodata", "No Wi-Fi scan here.", "Measure this point again."

    best = point.best(ssid, bssid, band)
    if best is None:
        return ("coverage", "The selected network wasn't seen at all here.",
                "Outside coverage: move an access point closer, or add one.")
    if best["rssi"] < t.min_rssi:
        return ("coverage", f"Signal is {best['rssi']} dBm, below the {t.min_rssi:.0f} dBm target.",
                "Coverage problem: move or add an access point.")

    floor = noise_floor(point, best["band"])
    offset = ctx.offset(point)
    center = best.get("freq_mhz") or spec.CHANNELS.get(best["channel"], 0)
    unexplained = residual(point, center, offset) if offset is not None else None

    if floor is not None:
        snr = best["rssi"] - floor
        if snr < t.min_snr:
            named = iq_interferers(point, center)
            if named and (unexplained is None or unexplained >= UNEXPLAINED_DB / 2):
                worst = max(named, key=lambda f: f["airtime"])
                return ("nonwifi",
                        f"SNR is {snr:.0f} dB, and the HackRF heard {iqmod.describe(worst)} "
                        f"on channel {best['channel']}.",
                        f"{worst['description']} Find it with the noise-source markers, or move "
                        "to a cleaner channel.")
            if unexplained is not None and unexplained >= UNEXPLAINED_DB:
                return ("nonwifi",
                        f"SNR is {snr:.0f} dB and {unexplained:.0f} dB of the energy on channel "
                        f"{best['channel']} isn't from any network here.",
                        "Something non-Wi-Fi is transmitting. Follow the unexplained energy map to it, "
                        "or move to a cleaner channel.")
            return ("cochannel", f"SNR is {snr:.0f} dB and other networks explain the energy.",
                    "Neighbouring networks: change channel or reduce overlap.")

    capture = point.capture
    if capture and capture.get("retry_eligible", 0) > 50 and capture["retry_rate"] >= t.max_retry_rate:
        return ("collisions",
                f"{capture['retry_rate'] * 100:.0f}% of frames are retries with {capture['clients']} clients heard.",
                "Frames are colliding: suspect a hidden node or too much overlap.")

    airtime = capture["airtime"] if capture else iq_airtime(point, center)
    if airtime is None:
        segment = spec.segment_for(point.spectrum, center)
        occupancy = (segment or {}).get("occupancy", {})
        airtime = occupancy.get(str(best["channel"]), occupancy.get(best["channel"]))
    if airtime is not None and airtime >= t.max_airtime:
        return ("congestion", f"The channel is busy {airtime * 100:.0f}% of the time.",
                "The signal is strong enough, but the airtime is spoken for: change channel or add "
                "capacity.")

    link = point.link
    if link and 0 < link.get("tx_mbps", 0) < t.min_phy_mbps:
        return ("config", f"The link negotiates only {link['tx_mbps']:.0f} Mbps despite a usable signal.",
                "Check channel width, legacy rates, and the client itself.")

    return ("good", "Signal, noise and airtime meet the targets here.",
            "Nothing is limiting this spot; the improvements listed add margin.")


# ---------- where a transmitter is ----------

def wall_loss(project, x1, y1, x2, y2) -> float:
    total = 0.0
    for wall in project.walls:
        if _crosses(x1, y1, x2, y2, wall.x1, wall.y1, wall.x2, wall.y2):
            total += wall.db
    return total


SEARCH_GRID = 64
REFINE_GRID = 25
EXPONENTS = np.arange(2.0, 4.01, 0.25)
# How far past the plan's edges access points are looked for once the plan has a scale, as a
# share of its longer side: far enough for a neighbour across the street or the car park.
BEYOND_PLAN = 2.0
# What a radio can be received at 1 m from it, by band: 10-30 dBm EIRP less the free-space loss
# at 1 m (40, 47 and 48 dB), with room for the adapter's own offset. A fit that needs more is a
# distant radio's flat readings explained by an impossibly loud one, so it isn't allowed.
LEVEL_AT_1M = {"2.4": (-45.0, -5.0), "5": (-52.0, -12.0), "6": (-53.0, -13.0)}


def search_grid(project, grid: int = SEARCH_GRID, beyond: bool = False):
    """
    The candidate positions locate tries: the plan and a tenth beyond each edge, or with
    ``beyond`` (which needs the scale), BEYOND_PLAN of the plan's longer side past each edge.
    """
    if beyond and project.meters_per_pixel:
        reach = BEYOND_PLAN * max(project.width, project.height)
        gx = np.linspace(-reach, project.width + reach, grid)
        gy = np.linspace(-reach, project.height + reach, grid)
    else:
        gx = np.linspace(-0.1 * project.width, 1.1 * project.width, grid)
        gy = np.linspace(-0.1 * project.height, 1.1 * project.height, grid)
    cx, cy = np.meshgrid(gx, gy)
    return gx, gy, cx, cy


def _fit_grid(levels, xs, ys, cx, cy, walls, mpp, p0_range):
    """The best path-loss fit at each candidate position, over the exponents: rms, exponent, P0."""
    distance = np.sqrt((cx[..., None] - xs) ** 2 + (cy[..., None] - ys) ** 2) * mpp
    log_distance = 10 * np.log10(np.maximum(distance, 0.5))
    best = None
    for exponent in EXPONENTS:
        predicted = exponent * log_distance + walls
        p0 = (levels + predicted).mean(axis=-1)
        if p0_range:
            p0 = np.clip(p0, *p0_range)
        error = levels - (p0[..., None] - predicted)
        rms = np.sqrt((error ** 2).mean(axis=-1))
        index = np.unravel_index(np.argmin(rms), rms.shape)
        if best is None or rms[index] < best["rms"]:
            best = {"x": float(cx[index]), "y": float(cy[index]), "rms": float(rms[index]),
                    "exponent": float(exponent), "p0": float(p0[index]), "grid": rms}
    return best


def locate(project, samples: list[tuple[float, float, float]], grid: int = SEARCH_GRID,
           walls: np.ndarray | None = None, band: str | None = None, beyond: bool = False):
    """
    Fits received = P0 - 10 n log10(distance) - walls over a grid of positions and
    exponents, solving P0 in closed form at each, then again on a finer grid around the best.
    Returns position, spread, and fit. ``walls`` is grid_wall_loss for these samples over
    search_grid, when the caller already has it: it's most of the work, and the same for every
    access point heard at a point.

    With the scale set, ``band`` holds P0 to what a real radio on it can be (LEVEL_AT_1M), and
    ``beyond`` searches well past the plan for transmitters off the property. "edge" says the
    fit ran to the end of the search, so the position means nothing; "outside" that it isn't
    on the plan, or ran to the edge.
    """
    if len(samples) < 4:
        return None
    xs = np.array([s[0] for s in samples])
    ys = np.array([s[1] for s in samples])
    levels = np.array([s[2] for s in samples])
    if np.hypot(xs.max() - xs.min(), ys.max() - ys.min()) < 40:
        return None

    scaled = bool(project.meters_per_pixel)
    mpp = project.meters_per_pixel or 0.05
    p0_range = LEVEL_AT_1M.get(band) if scaled else None
    gx, gy, cx, cy = search_grid(project, grid, beyond)
    if walls is None:
        walls = grid_wall_loss(project.walls, xs, ys, cx, cy)
    best = _fit_grid(levels, xs, ys, cx, cy, walls, mpp, p0_range)

    close = best["grid"] <= best["rms"] + 3
    step_x, step_y = gx[1] - gx[0], gy[1] - gy[0]
    radius = float(np.sqrt(close.sum() * step_x * step_y / np.pi))
    edge = not (gx[1] < best["x"] < gx[-2] and gy[1] < best["y"] < gy[-2])

    # Closer in: the coarse grid only says which cell it's in
    fx = np.linspace(best["x"] - 1.5 * step_x, best["x"] + 1.5 * step_x, REFINE_GRID)
    fy = np.linspace(best["y"] - 1.5 * step_y, best["y"] + 1.5 * step_y, REFINE_GRID)
    fcx, fcy = np.meshgrid(fx, fy)
    fine = _fit_grid(levels, xs, ys, fcx, fcy, grid_wall_loss(project.walls, xs, ys, fcx, fcy),
                     mpp, p0_range)
    if fine["rms"] <= best["rms"]:
        best = fine

    on_plan = 0 <= best["x"] <= project.width and 0 <= best["y"] <= project.height
    return {
        "x": best["x"], "y": best["y"], "rms": best["rms"], "exponent": best["exponent"],
        "p0": best["p0"], "radius": radius, "points": len(samples),
        "rough": edge or best["rms"] > 6 or radius > 0.25 * np.hypot(project.width, project.height),
        "outside": edge or (beyond and not on_plan), "edge": edge,
    }


def grid_wall_loss(walls, xs: np.ndarray, ys: np.ndarray, cx: np.ndarray, cy: np.ndarray) -> np.ndarray:
    """
    Loss from every wall crossed on the path from each sample to each candidate position,
    shape (*grid, samples). The same test as _crosses, done in NumPy one wall at a time, so
    memory stays at one grid-by-samples array however many walls there are.
    """
    total = np.zeros((*cx.shape, len(xs)))
    if not walls:
        return total
    bx, by = cx[..., None], cy[..., None]          # candidate positions, broadcast over samples
    for wall in walls:
        wx, wy = wall.x2 - wall.x1, wall.y2 - wall.y1
        d1 = wx * (ys - wall.y1) - wy * (xs - wall.x1)               # which side each sample is on
        d2 = wx * (cy - wall.y1) - wy * (cx - wall.x1)               # which side each candidate is on
        d3 = (bx - xs) * (wall.y1 - ys) - (by - ys) * (wall.x1 - xs)
        d4 = (bx - xs) * (wall.y2 - ys) - (by - ys) * (wall.x2 - xs)
        crosses = ((d1 > 0) != (d2[..., None] > 0)) & ((d3 > 0) != (d4 > 0))
        total += np.where(crosses, wall.db, 0.0)
    return total


def ap_samples(project, bssid: str) -> list[tuple[float, float, float]]:
    samples = []
    for point in project.points:
        for network in point.networks:
            if network["bssid"].lower() == bssid.lower():
                samples.append((point.x, point.y, float(network["rssi"])))
                break
    return samples


def candidates(project, limit: int = 6) -> list[tuple[str, str, int]]:
    seen: dict[str, list[str | int]] = {}
    for point in project.points:
        for network in point.networks:
            entry = seen.setdefault(network["bssid"], [network["ssid"], 0])
            entry[1] += 1
    ranked = sorted(seen.items(), key=lambda kv: kv[1][1], reverse=True)
    return [(bssid, str(info[0]), int(info[1])) for bssid, info in ranked if int(info[1]) >= 4][:limit]


def _crosses(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    def side(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)

    d1, d2 = side(cx, cy, dx, dy, ax, ay), side(cx, cy, dx, dy, bx, by)
    d3, d4 = side(ax, ay, bx, by, cx, cy), side(ax, ay, bx, by, dx, dy)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


# ---------- where the access points probably are ----------

BAND_ORDER = {"2.4": 0, "5": 1, "6": 2}

# One radio carrying several SSIDs: how far apart its BSSIDs may be, in address bits, and how
# closely their levels must agree on average where both were heard.
SAME_RADIO_BITS = 4
SAME_RADIO_DB = 3.0


def same_radio(a: str, b: str, heard: dict[str, dict[int, float]], channels: dict) -> bool:
    """
    Whether two BSSIDs are one radio sending two SSIDs. Only when everything agrees: the same
    channel, addresses a few bits apart (how vendors number a radio's extra BSSIDs), heard
    together at nearly every point either was, and levels that match point by point while
    varying enough across the plan for that to mean something. Two access points out of one
    box can have neighbouring addresses and share a channel, but not read the same everywhere.
    """
    if channels.get(a) is None or channels.get(a) != channels.get(b):
        return False
    try:
        bits = bin(int(a.replace(":", ""), 16) ^ int(b.replace(":", ""), 16)).count("1")
    except ValueError:
        return False
    if bits > SAME_RADIO_BITS:
        return False
    common = sorted(heard[a].keys() & heard[b].keys())
    if len(common) < 3 or len(common) < 0.75 * len(heard[a].keys() | heard[b].keys()):
        return False
    first = np.array([heard[a][i] for i in common])
    second = np.array([heard[b][i] for i in common])
    return (np.abs(first - second).mean() <= SAME_RADIO_DB
            and first.max() - first.min() >= 6)


def radio_twins(project) -> dict[str, str]:
    """
    Every BSSID that same_radio shows is another's radio, to the most-heard BSSID of its radio,
    from everything heard across the survey.
    """
    heard: dict[str, dict[int, float]] = {}
    channels: dict[str, object] = {}
    for index, point in enumerate(project.points):
        for network in point.networks:
            bssid = network["bssid"]
            channels.setdefault(bssid, network.get("channel"))
            heard.setdefault(bssid, {}).setdefault(index, float(network["rssi"]))
    on_channel: dict[object, list[str]] = {}
    for bssid in sorted(heard, key=lambda b: (-len(heard[b]), b)):
        on_channel.setdefault(channels[bssid], []).append(bssid)
    twins: dict[str, str] = {}
    for together in on_channel.values():
        for index, leader in enumerate(together):
            if leader in twins:
                continue
            for other in together[index + 1:]:
                if other not in twins and same_radio(leader, other, heard, channels):
                    twins[other] = leader
    return twins


def focus_here(project, point) -> str | None:
    """
    The focus network a client here would use, the strongest of them heard, or the first if
    none is: None when the survey has no focus networks.
    """
    focus = list(getattr(project, "focus_networks", None) or [])
    if not focus:
        return None
    heard = [n for n in point.networks if n["ssid"] in focus]
    return max(heard, key=lambda n: n["rssi"])["ssid"] if heard else focus[0]


def off_plan(project, x: float, y: float) -> dict | None:
    """
    How far past the plan's edge a spot is, and which way (by the compass once the plan is on
    a map), or None when it's on the plan.
    """
    from . import geo

    nearest_x = min(max(x, 0.0), float(project.width))
    nearest_y = min(max(y, 0.0), float(project.height))
    dx, dy = x - nearest_x, y - nearest_y
    if not dx and not dy:
        return None
    way = geo.compass(project, dx, dy)
    if way is None:
        sides = [s for s in ("above" if dy < 0 else "below" if dy > 0 else "",
                             "left" if dx < 0 else "right" if dx > 0 else "") if s]
        way = " and ".join(sides) + " the plan" if len(sides) == 1 else \
            f"{sides[0]} and to the {sides[1]} of the plan"
        way = way.replace("left the plan", "to the left of the plan").replace(
            "right the plan", "to the right of the plan")
    else:
        way += " of the plan"
    return {"feet": project.feet(float(np.hypot(dx, dy))), "way": way,
            "edge_x": nearest_x, "edge_y": nearest_y}


def ap_sites(project, ssid: str | None = None, band: str = "All", limit: int | None = None,
             networks: set[str] | None = None) -> list[dict]:
    """
    Locates the radios, most-measured first, then gathers those that land together into one
    site, since one access point usually carries several: a radio per band, and often more
    than one SSID on each. BSSIDs that are surely one radio are located together and count
    once towards ``limit`` (None: every radio heard). ``networks`` keeps to the radios that
    carry one of them. Each site lists the SSIDs heard from it and on which bands.

    Once the plan has a scale, access points are looked for well past its edges too, so a
    neighbour's is placed where it is, not on the plan; such a site says how far off and which
    way ("beyond"), and with the plan on a map, where on the Earth ("lat_lon").
    """
    from . import geo

    seen: dict[str, tuple[str, str]] = {}
    heard: dict[str, dict[int, float]] = {}
    channels: dict[str, object] = {}
    carries: dict[str, set[str]] = {}
    for index, point in enumerate(project.points):
        for network in point.networks:
            if (ssid and network["ssid"] != ssid) or (band != "All" and network.get("band") != band):
                continue
            carries.setdefault(network["bssid"], set()).add(network["ssid"])
            bssid = network["bssid"]
            seen.setdefault(bssid, (network["ssid"], network.get("band", "")))
            channels.setdefault(bssid, network.get("channel"))
            heard.setdefault(bssid, {}).setdefault(index, float(network["rssi"]))
    ranked = sorted(seen, key=lambda bssid: len(heard[bssid]), reverse=True)

    twins = radio_twins(project)
    by_radio: dict[str, list[str]] = {}
    for bssid in ranked:
        by_radio.setdefault(twins.get(bssid, bssid), []).append(bssid)
    radios = [radio for radio in by_radio.values()
              if networks is None or any(carries[b] & networks for b in radio)]
    limit = len(radios) if limit is None else limit

    def levels(radio: list[str]) -> dict[int, float]:
        by_point: dict[int, list[float]] = {}
        for bssid in radio:
            for index, level in heard[bssid].items():
                by_point.setdefault(index, []).append(level)
        return {index: sum(found) / len(found) for index, found in by_point.items()}

    def add(radio: list[str], found: dict) -> None:
        for bssid in radio:
            name, radio_band = seen[bssid]
            located.append((name, radio_band, dict(found, bssid=bssid)))

    mpp = project.meters_per_pixel
    near = 3.0 / mpp if mpp else 0.04 * float(np.hypot(project.width, project.height))

    # The survey's own networks are on the property: only other people's are looked for past it
    focus = set(getattr(project, "focus_networks", None) or [])
    walls_over: dict[bool, np.ndarray] = {}     # wall loss for every point, per search area
    located = []
    placed: set[str] = set()
    count = 0
    for radio in radios:
        heard_at = levels(radio)
        if count >= limit or len(heard_at) < 4:
            continue
        indices = sorted(heard_at)
        beyond = bool(mpp) and not any(carries[b] & focus for b in radio)
        if project.walls and beyond not in walls_over:
            _gx, _gy, cx, cy = search_grid(project, beyond=beyond)
            walls_over[beyond] = grid_wall_loss(
                project.walls, np.array([p.x for p in project.points]),
                np.array([p.y for p in project.points]), cx, cy)
        found = locate(project, [(project.points[i].x, project.points[i].y, heard_at[i])
                                 for i in indices],
                       walls=walls_over[beyond][..., indices] if beyond in walls_over else None,
                       band=seen[radio[0]][1], beyond=beyond)
        if found and (found["edge"] or (not beyond and off_plan(project, found["x"], found["y"]))):
            # The best fit ran to the end of the search, or off a plan without a scale to say
            # how far is plausible: the levels change too little across the points to tell
            # near and loud from far and louder. Show it in the middle of where it was heard,
            # pulled towards the loudest, with a ring as wide as that.
            xs = np.array([project.points[i].x for i in indices])
            ys = np.array([project.points[i].y for i in indices])
            weights = 10 ** (np.array([heard_at[i] for i in indices]) / 10)
            x, y = float(np.average(xs, weights=weights)), float(np.average(ys, weights=weights))
            spread = float(np.sqrt(np.average((xs - x) ** 2 + (ys - y) ** 2, weights=weights)))
            found.update(x=x, y=y, radius=max(spread, near), rough=True, outside=False,
                         rms=99.0, guess=True, unfitted=True)
        if found:
            add(radio, found)
            placed.update(radio)
            count += 1

    # Heard at too few points to locate: a rough guess beside the point where it was strongest,
    # so it can still be named and moved to where it really is
    for radio in radios:
        if radio[0] in placed or count >= limit:
            continue
        heard_at = levels(radio)
        index = max(heard_at, key=heard_at.get)
        level = heard_at[index]
        x, y = project.points[index].x, project.points[index].y
        add(radio, {
            "x": min(project.width, x + near * 0.6), "y": y, "radius": near, "rough": True,
            "outside": False, "rms": 99.0, "p0": level, "exponent": 3.0, "points": len(heard_at),
            "guess": True})
        count += 1
    sites: list[dict] = []
    for name, radio_band, found in sorted(located, key=lambda item: item[2]["rms"]):
        site = next((s for s in sites
                     if np.hypot(s["x"] - found["x"], s["y"] - found["y"]) <= near), None)
        if site is None:
            site = {"x": found["x"], "y": found["y"], "radius": found["radius"],
                    "rough": found["rough"], "outside": found["outside"], "networks": {},
                    "fits": [], "guess": found.get("guess", False),
                    "unfitted": found.get("unfitted", False)}
            sites.append(site)
        else:
            site["rough"] = site["rough"] and found["rough"]
        site["networks"].setdefault(name or "(hidden)", set()).add(radio_band)
        site["fits"].append({"ssid": name, "band": radio_band, "bssid": found["bssid"],
                             "p0": found["p0"], "exponent": found["exponent"],
                             "rms": found["rms"], "rough": found["rough"]})
    for site in sites:
        site["networks"] = [
            (name, sorted(bands, key=lambda b: BAND_ORDER.get(b, 9)))
            for name, bands in sorted(site["networks"].items())
        ]
        site["bssids"] = [fit["bssid"] for fit in site["fits"]]
        site["beyond"] = None if site["guess"] else off_plan(project, site["x"], site["y"])
        site["lat_lon"] = None if site["guess"] else geo.plan_lat_lon(project, site["x"], site["y"])
    return apply_pins(project, sites, ssid, band)


def apply_pins(project, sites: list[dict], ssid=None, band="All") -> list[dict]:
    """
    Puts the access points the surveyor moved where they really are, with their names, and
    re-fits how their signal falls off from that true position. A pin whose access point
    isn't among the estimates any more (too few points under the current filter) still shows.
    """
    pins = getattr(project, "ap_pins", None) or []
    used: set[int] = set()
    for site in sites:
        mine = {b.lower() for b in site.get("bssids", [])}
        for index, pin in enumerate(pins):
            if index not in used and mine & {b.lower() for b in pin.get("bssids", [])}:
                used.add(index)
                _pin_site(project, site, pin, index)
                break
    for index, pin in enumerate(pins):
        if index in used or pin.get("x") is None:
            continue
        networks = [(name, [b for b in bands if band == "All" or b == band])
                    for name, bands in pin.get("networks", [])
                    if not ssid or name == ssid]
        networks = [(name, bands) for name, bands in networks if bands]
        if not networks:
            continue
        site = {"x": pin["x"], "y": pin["y"], "radius": 0.0, "rough": False, "outside": False,
                "networks": networks, "fits": [], "bssids": list(pin.get("bssids", []))}
        _pin_site(project, site, pin, index)
        sites.append(site)
    return sites


def _pin_site(project, site: dict, pin: dict, index: int) -> None:
    from . import planning

    site["pin"] = index
    site["name"] = pin.get("name") or ""
    if pin.get("x") is None:
        return                       # named only: the position is still the estimate
    site.update(x=float(pin["x"]), y=float(pin["y"]), radius=0.0, rough=False, outside=False,
                placed=True)
    fits = []
    for bssid in site["bssids"]:
        samples = ap_samples(project, bssid)
        found = planning.fit_at(project, samples, site["x"], site["y"])
        if found is None:
            continue
        network = next((n for p in project.points for n in p.networks
                        if n["bssid"].lower() == bssid.lower()), {})
        fits.append({"ssid": network.get("ssid", ""), "band": network.get("band", ""),
                     "bssid": bssid, "rough": False, **found})
    if fits:
        site["fits"] = fits


# ---------- where the noise is coming from ----------

EMITTER_KINDS = {
    "microwave": ("Microwave oven",
                  "A wide hump in the top half of 2.4 GHz that comes and goes. Brutal while it "
                  "runs, but only while it runs."),
    "wideband": ("Wideband energy",
                 "Energy across more than 40 MHz. Could be a jammer, a faulty device, or a very "
                 "wide channel from a network too weak to decode."),
    "continuous": ("Continuous emitter",
                   "A carrier that never stops, such as an analog camera, video sender or "
                   "cordless device. The worst kind: it never yields airtime."),
    "bursty": ("Bursty transmitter",
               "Packet-like bursts. Probably a network too weak to decode, or Bluetooth or "
               "Zigbee."),
    # Named by the HackRF's close look rather than guessed from the sweep's shape
    "bluetooth": ("Bluetooth", iqmod.KINDS["bluetooth"][1]),
    "zigbee": ("Zigbee / Thread", iqmod.KINDS["zigbee"][1]),
    "video": ("Video sender or jammer", iqmod.KINDS["video"][1]),
    "radar": ("Radar", iqmod.KINDS["radar"][1]),
    "overlap": ("Wi-Fi on an overlapping channel", iqmod.KINDS["overlap"][1]),
    "weakwifi": ("Wi-Fi too weak to decode",
                 "Wi-Fi frames from networks too far away for the card to list, still taking "
                 "airtime here."),
}
# The HackRF's names for what it identified, as emitter kinds
IQ_KINDS = {"bluetooth": "bluetooth", "zigbee": "zigbee", "microwave": "microwave",
            "continuous": "continuous", "video": "video", "radar": "radar", "wifi": "weakwifi",
            "overlap": "overlap",
            "bursty": "bursty"}


def identified_kind(project, center_mhz: float, width_mhz: float) -> str | None:
    """
    What the HackRF made of transmissions in this slice, wherever it looked: the kind with
    the most airtime across the survey, or None if it never looked here.
    """
    airtime: dict[str, float] = {}
    for point in project.points:
        for result in point.iq or []:
            for finding in result["findings"]:
                reach = max(width_mhz, finding["width_mhz"] or 0) / 2 + 2
                if abs(finding["center_mhz"] - center_mhz) <= reach:
                    kind = IQ_KINDS.get(finding["kind"])
                    if kind:
                        airtime[kind] = airtime.get(kind, 0.0) + finding["airtime"] + 0.01
    if not airtime:
        return None
    # Wi-Fi counts only when nothing else was found there: it's what's left when it's all Wi-Fi
    others = {k: v for k, v in airtime.items() if k != "weakwifi"}
    pool = others or airtime
    return max(pool, key=pool.get)


def overlap(freq_mhz: float, center_mhz: float, width_mhz: float) -> float:
    return max(0.0, width_mhz - abs(freq_mhz - center_mhz)) / width_mhz


def find_emitters(point, offset: float | None) -> list[dict]:
    """
    Runs of raised energy in the max-hold trace that the scanned networks don't explain,
    with a guess at their character from their width and how steady they are.
    """
    found = []
    for segment in (point.spectrum or {}).get("segments", []):
        peak_trace, average, step = segment.get("max") or [], segment.get("avg") or [], segment["step_mhz"]
        if len(peak_trace) < 4 or step <= 0 or not segment.get("min"):
            continue
        floor = float(np.percentile(segment["min"], 10))
        enter, stay = floor + 12, floor + 6
        i = 0
        while i < len(peak_trace):
            if peak_trace[i] < enter:
                i += 1
                continue
            start, peak, peak_index = i, -1e9, i
            while i < len(peak_trace) and peak_trace[i] >= stay:
                if peak_trace[i] > peak:
                    peak, peak_index = peak_trace[i], i
                i += 1
            low = segment["start_mhz"] + start * step
            high = segment["start_mhz"] + (i - 1) * step
            width = max(step, high - low)
            center = (low + high) / 2
            steadiness = peak - (average[peak_index] if peak_index < len(average) else peak)
            power = spec.channel_power(peak_trace, segment["start_mhz"], step,
                                       segment["rbw_khz"], center, max(width, step))
            if width >= 1 and not _explained_by_wifi(point, center, width, power, offset):
                found.append(_classify(center, width, peak, steadiness))
    return sorted(found, key=lambda e: e["peak"], reverse=True)[:5]


def _explained_by_wifi(point, center_mhz: float, width_mhz: float, power: float | None,
                       offset: float | None) -> bool:
    """
    Whether the networks the card heard account for this emission. Compared as power across
    the emission's width, since that's what the offset relates: a single bin of a Wi-Fi
    signal sits well below its channel power, and would otherwise look unexplained.
    """
    span = max(40.0, width_mhz)
    nearby = [n for n in point.networks if overlap(n["freq_mhz"], center_mhz, span) > 0]
    if not nearby:
        return False
    if offset is None or power is None:
        return any(n["rssi"] > -80 for n in nearby)
    expected = sum(float(spec.to_mw(n["rssi"])) * overlap(n["freq_mhz"], center_mhz, span) for n in nearby)
    return power <= float(spec.to_dbm(expected)) + offset + 10


def _classify(center: float, width: float, peak: float, peak_to_average: float) -> dict:
    if spec.band_of(center) == "2.4" and 2430 < center < 2485 and width >= 8 and peak_to_average >= 10:
        kind = "microwave"
    elif width >= 40:
        kind = "wideband"
    elif peak_to_average <= 5:
        kind = "continuous"
    else:
        kind = "bursty"
    return {"center": center, "width": width, "peak": peak, "kind": kind}


def unexplained_at(point, center_mhz: float, width_mhz: float, offset: float) -> float | None:
    """Power in this slice beyond what the visible networks and the floor account for."""
    span = max(2.0, width_mhz)
    segment = None
    for candidate in (point.spectrum or {}).get("segments", []):
        end = candidate["start_mhz"] + candidate["step_mhz"] * (len(candidate["max"]) - 1)
        if center_mhz - span / 2 >= candidate["start_mhz"] - 0.5 and center_mhz + span / 2 <= end + 0.5:
            segment = candidate
            break
    if segment is None:
        return None
    measured = spec.channel_power(segment["max"], segment["start_mhz"], segment["step_mhz"],
                                  segment["rbw_khz"], center_mhz, span)
    if measured is None:
        return None
    wifi_mw = sum(float(spec.to_mw(n["rssi"] + offset)) * overlap(n["freq_mhz"], center_mhz, max(20.0, span))
                  for n in point.networks)
    floor_mw = float(spec.to_mw(noise_floor(point, spec.band_of(center_mhz)) or -95)) * (span / 20)
    left = float(spec.to_mw(measured)) - max(wifi_mw, floor_mw)
    return float(spec.to_dbm(max(left, floor_mw * 0.25)))


def channels_touched(center_mhz: float, width_mhz: float) -> list[int]:
    """The 20 MHz Wi-Fi channels in this emission's band that it lands on."""
    table = {"2.4": spec.CHANNELS_24, "5": spec.CHANNELS_5, "6": spec.CHANNELS_6}[spec.band_of(center_mhz)]
    return [number for number, centre in sorted(table.items())
            if abs(centre - center_mhz) < width_mhz / 2 + 10]


def noise_sources(project, ctx: Context, limit: int = 5) -> list[dict]:
    """
    Searches every captured segment for emissions no visible network explains, groups them by
    frequency across the survey, and locates each from how it falls off from point to point.
    Quiet points count too: they're what tell the fit which way the signal weakens.
    """
    sightings = [(index, emitter) for index, point in enumerate(project.points)
                 for emitter in find_emitters(point, ctx.offset(point))]
    clusters: list[list[tuple[int, dict]]] = []
    for sighting in sorted(sightings, key=lambda s: s[1]["center"]):
        for cluster in clusters:
            center = float(np.mean([e["center"] for _, e in cluster]))
            width = max(12.0, max(e["width"] for _, e in cluster))
            if abs(center - sighting[1]["center"]) <= width:
                cluster.append(sighting)
                break
        else:
            clusters.append([sighting])

    sources = []
    for cluster in sorted(clusters, key=lambda c: max(e["peak"] for _, e in c), reverse=True)[:limit]:
        center = float(np.mean([e["center"] for _, e in cluster]))
        width = max(e["width"] for _, e in cluster)
        kinds = [e["kind"] for _, e in cluster]
        kind = identified_kind(project, center, width) or max(set(kinds), key=kinds.count)
        seen_at = len({index for index, _ in cluster})
        samples = []
        for point in project.points:
            offset = ctx.offset(point)
            level = unexplained_at(point, center, width, offset) if offset is not None else None
            if level is not None:
                samples.append((point.x, point.y, level))
        name, description = EMITTER_KINDS[kind]
        sources.append({
            "center": center, "width": width, "kind": kind, "name": name,
            "identified": kind in IQ_KINDS.values() and kind == identified_kind(project, center, width),
            "description": description, "points": seen_at,
            "peak": max(e["peak"] for _, e in cluster), "band": spec.band_of(center),
            "channels": channels_touched(center, width),
            "located": locate(project, samples) if seen_at >= 3 else None,
        })
    return sources


def access_points(networks) -> list[dict]:
    """
    Scan results, as ``wifi.Reading`` or a survey point's network dicts, reduced to one entry
    per network per channel at its strongest (an AP's several BSSIDs usually share a channel),
    strongest first.
    """
    strongest: dict[tuple[str, float], dict] = {}
    for network in networks:
        if isinstance(network, dict):
            get = network.get
            source = get("rssi_source", "")
        else:
            def get(key, default=None, network=network):
                return getattr(network, key, default)
            source = network.source
        freq = float(get("freq_mhz") or 0)
        if not freq:
            continue
        ap = {
            "ssid": get("ssid") or "", "bssid": get("bssid") or "", "rssi": get("rssi"),
            "freq": freq, "channel": get("channel"), "band": get("band") or "",
            "center": float(get("center_mhz") or freq), "width": int(get("width_mhz") or 20),
            "estimate": source == "nmcli",
            "hidden": bool(get("hidden")),
            # Saved with the scan, or looked up now for points measured before that
            "vendor": get("vendor") or oui.vendor(get("bssid") or "", access_point=True),
        }
        key = (ap["ssid"], freq)
        if key not in strongest or ap["rssi"] > strongest[key]["rssi"]:
            strongest[key] = ap
    return sorted(strongest.values(), key=lambda ap: ap["rssi"], reverse=True)


def bt_devices(project, limit: int = 12) -> list[dict]:
    """
    Every Bluetooth device with a fixed address heard at enough points to place — of any kind,
    since a phone or wearable that shouldn't be there matters as much as a printer. Each is
    located from how its signal fades between points. Devices using a rotating private address,
    or heard at too few points, come back from ``bt_other`` instead.
    """
    seen: dict[str, dict] = {}
    for index, point in enumerate(project.points):
        for device in point.bluetooth or []:
            if not device.get("public"):
                continue                       # a rotating address can't be followed; see bt_other
            entry = seen.setdefault(device["address"], {"device": device, "sightings": {}})
            entry["device"] = {**entry["device"], **device}     # keep the fullest description
            entry["sightings"][index] = device.get("rssi", -100)

    devices = []
    for address, entry in seen.items():
        sightings = entry["sightings"]
        if len(sightings) < 3:
            continue
        samples = [(project.points[i].x, project.points[i].y, rssi)
                   for i, rssi in sightings.items()]
        device = entry["device"]
        devices.append({
            "address": address, "kind": device.get("kind") or "unknown",
            "name": device.get("name") or "", "vendor": device.get("vendor") or "",
            "detail": device.get("detail") or "", "points": len(sightings),
            "best": max(sightings.values()), "located": locate(project, samples),
        })
    devices.sort(key=lambda d: d["best"], reverse=True)
    return devices[:limit]


def wifi_clients(project, limit: int = 60) -> list[dict]:
    """
    Every Wi-Fi client whose signal was captured at a point, placed on the map. Heard at 4 or
    more points spread across the plan, it's located from how its signal fades, like an access
    point; otherwise it's shown at the point where it was strongest, marked rough. Private
    (randomised) addresses are kept: phones keep the same one for each network, so they can be
    followed through a survey, though one that changes address mid-survey counts twice.
    Clients move, so a fix only means where it was while the captures were taken.
    """
    seen: dict[str, dict] = {}
    for index, point in enumerate(project.points):
        for client in (point.capture or {}).get("clients_heard", []):
            entry = seen.setdefault(client["mac"], {"client": client, "sightings": {}})
            if client.get("ssid") and not entry["client"].get("ssid"):
                entry["client"] = {**entry["client"], "ssid": client["ssid"]}
            entry["sightings"][index] = client["rssi"]

    clients = []
    for mac, entry in seen.items():
        sightings = entry["sightings"]
        strongest = max(sightings, key=sightings.get)
        near = project.points[strongest]
        samples = [(project.points[i].x, project.points[i].y, rssi)
                   for i, rssi in sightings.items()]
        located = locate(project, samples)
        if located is None:
            located = {"x": near.x, "y": near.y, "rough": True, "outside": False, "near": True}
        client = entry["client"]
        clients.append({
            "mac": mac, "vendor": client.get("vendor") or "", "ssid": client.get("ssid") or "",
            "points": len(sightings), "best": sightings[strongest], "near_point": strongest + 1,
            "located": located,
        })
    clients.sort(key=lambda c: c["best"], reverse=True)
    return clients[:limit]


def finder_tags(project) -> list[dict]:
    """
    Every item tracker heard at the points, placed on the map: located from how its signal
    fades when heard at 4 or more points spread across the plan, otherwise shown at the point
    where it was strongest. Those near their owner change address every 15 minutes or so, so
    the same tag can appear as two; those away from their owner keep one far longer.
    """
    seen: dict[str, dict] = {}
    for index, point in enumerate(project.points):
        for tag in point.trackers or []:
            entry = seen.setdefault(tag["address"], {"tag": tag, "sightings": {}})
            if tag.get("away"):
                entry["tag"] = {**entry["tag"], "away": True, "state": tag.get("state", "")}
            entry["sightings"][index] = tag["rssi"]
    tags = []
    for address, entry in seen.items():
        sightings = entry["sightings"]
        strongest = max(sightings, key=sightings.get)
        near = project.points[strongest]
        located = locate(project, [(project.points[i].x, project.points[i].y, rssi)
                                   for i, rssi in sightings.items()])
        if located is None:
            located = {"x": near.x, "y": near.y, "rough": True, "outside": False, "near": True}
        tag = entry["tag"]
        tags.append({"address": address, "kind": tag["kind"], "label": tag["label"],
                     "away": tag.get("away"), "state": tag.get("state", ""),
                     "battery": tag.get("battery", ""), "points": len(sightings),
                     "best": sightings[strongest], "near_point": strongest + 1,
                     "located": located})
    # Away from their owner first: the ones nobody nearby is carrying
    tags.sort(key=lambda t: (not t["away"], -t["best"]))
    return tags


def bt_other(project) -> dict:
    """
    Bluetooth devices heard but not placed on the map: fixed-address ones seen at too few points
    (listed individually), and rotating-address ones (grouped by kind, since each advert carries
    a different address). Useful where any phone or unexpected device is worth knowing about.
    """
    placed = {d["address"] for d in bt_devices(project)}
    fixed: dict[str, dict] = {}
    rotating: dict[str, dict] = {}      # kind -> {addresses seen, strongest}
    for point in project.points:
        for device in point.bluetooth or []:
            if device.get("public"):
                if device["address"] in placed:
                    continue
                entry = fixed.setdefault(device["address"],
                                         {**device, "points": 0, "best": -200, "near": None})
                entry["points"] += 1
                if device.get("rssi", -200) > entry["best"]:
                    entry["best"] = device.get("rssi", -200)
                    entry["near"] = (point.x, point.y)     # strongest spot: roughly where it is
                if device.get("name") and not entry.get("name"):
                    entry["name"] = device["name"]
            else:
                kind = device.get("kind") or "unknown"
                bucket = rotating.setdefault(kind, {"addresses": set(), "best": -200})
                bucket["addresses"].add(device["address"])
                bucket["best"] = max(bucket["best"], device.get("rssi", -200))
    return {
        "fixed": sorted(fixed.values(), key=lambda d: d["best"], reverse=True),
        "rotating": sorted(
            ({"kind": k, "count": len(b["addresses"]), "best": b["best"]}
             for k, b in rotating.items()),
            key=lambda r: r["count"], reverse=True),
    }


def network_quality(project, ssid=None, bssid=None, band="All", ctx=None) -> dict:
    """
    How an SSID (or one BSSID) does across the whole survey: where it's heard, how strong, and
    a one-word grade. ``heard`` is the share of points that heard it at all; ``usable`` the
    share at or above -70 dBm.
    """
    ctx = ctx or Context(project)
    rssis, snrs = [], []
    for point in project.points:
        best = point.best(ssid, bssid, band)
        if best is None:
            continue
        rssis.append(best["rssi"])
        floor = noise_floor(point, best["band"])
        if floor is not None:
            snrs.append(best["rssi"] - floor)
    total = len(project.points)
    if not rssis:
        return {"heard": 0.0, "usable": 0.0, "points": 0, "total": total, "grade": "not heard"}
    rssis.sort()
    median = rssis[len(rssis) // 2]
    usable = sum(1 for r in rssis if r >= -70) / total if total else 0.0
    grade = ("excellent" if median >= -60 and usable >= 0.9 else
             "good" if median >= -67 and usable >= 0.75 else
             "fair" if median >= -75 else "poor")
    return {
        "heard": len(rssis) / total if total else 0.0, "usable": usable,
        "points": len(rssis), "total": total, "grade": grade,
        "best": rssis[-1], "worst": rssis[0], "median": median,
        "median_snr": sorted(snrs)[len(snrs) // 2] if snrs else None,
    }


# ---------- walls, measured with the test tone ----------

def tone_samples(project) -> list[tuple[float, float, float]]:
    source = project.tone_source
    if not source:
        return []
    return [(p.x, p.y, float(p.tone["dbm"])) for p in project.points
            if p.tone and p.tone.get("dbm") is not None
            and abs(p.tone.get("freq_mhz", 0) - source["freq_mhz"]) < 1]


def fit_walls(project, prior_weight: float = 0.5) -> dict | None:
    """
    Each wall's loss, from the test tone. The tone comes from a known place at a constant
    level, so received = P0 - 10 n log10(distance) - (the walls crossed) is linear in P0, n
    and each wall's loss, and least squares solves them together. Walls no path crosses keep
    their loss; each one fitted is pulled gently towards what it was set to, so a wall only
    one or two paths cross can't swing wildly. A loss can't be negative: a wall that comes
    out below zero is set to zero and the rest fitted again. An exponent outside what
    buildings show (1.6-5) means too few paths to separate distance from walls, so it's held
    at the nearest sensible value and the walls fitted again.
    """
    source = project.tone_source
    samples = tone_samples(project)
    if not source or len(samples) < 5:
        return None
    walls = project.walls
    mpp = project.meters_per_pixel or 0.05
    xs = np.array([s[0] for s in samples])
    ys = np.array([s[1] for s in samples])
    levels = np.array([s[2] for s in samples])
    log_distance = 10 * np.log10(np.maximum(np.hypot(xs - source["x"], ys - source["y"]) * mpp, 0.5))
    crossed = np.array([[float(_crosses(source["x"], source["y"], x, y, w.x1, w.y1, w.x2, w.y2))
                         for w in walls] for x, y in zip(xs, ys)]).reshape(len(samples), len(walls))
    paths = crossed.sum(axis=0)
    free = [i for i in range(len(walls)) if paths[i] >= 1]

    def solve(fitting: list[int], zeroed: set[int], exponent: float | None):
        known = np.array([0.0 if i in zeroed else walls[i].db if i not in fitting else 0.0
                          for i in range(len(walls))])
        target = levels + crossed @ known if len(walls) else levels.copy()
        columns = [np.ones(len(samples))]
        if exponent is None:
            columns.append(-log_distance)
        else:
            target = target + exponent * log_distance
        columns += [-crossed[:, i] for i in fitting]
        matrix = np.column_stack(columns)
        if fitting:
            prior = np.zeros((len(fitting), matrix.shape[1]))
            prior[:, -len(fitting):] = np.eye(len(fitting)) * prior_weight
            matrix = np.vstack([matrix, prior])
            target = np.concatenate([target, [walls[i].db * prior_weight for i in fitting]])
        solution = np.linalg.lstsq(matrix, target, rcond=None)[0]
        found_exponent = float(solution[1]) if exponent is None else exponent
        return float(solution[0]), found_exponent, dict(zip(fitting, solution[-len(fitting):].tolist()
                                                            if fitting else []))

    exponent = None
    zeroed: set[int] = set()
    for _ in range(len(free) + 2):
        fitting = [i for i in free if i not in zeroed]
        p0, found, losses = solve(fitting, zeroed, exponent)
        if exponent is None and not 1.6 <= found <= 5.0:
            exponent = min(5.0, max(1.6, found))
            continue
        negative = {i for i, value in losses.items() if value < 0}
        if not negative:
            break
        zeroed |= negative
    walls_db = np.array([losses.get(i, 0.0 if i in zeroed else w.db) for i, w in enumerate(walls)])
    predicted = p0 - found * log_distance - (crossed @ walls_db if len(walls) else 0)
    return {
        "p0": p0, "exponent": found, "exponent_held": exponent is not None,
        "rms": float(np.sqrt(np.mean((levels - predicted) ** 2))),
        "points": len(samples),
        "walls": [{"index": i, "before": walls[i].db, "measured": float(walls_db[i]),
                   "paths": int(paths[i])} for i in free],
    }
