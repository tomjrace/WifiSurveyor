"""The Windows app's survey format, read and written, so one survey can move between the two.

The Windows side serialises its C# models with System.Text.Json: PascalCase names, enums as
strings, times as ISO 8601. What only it uses (GPS pins, walk tracks, per-adapter links,
iperf3 results) is kept in ``extra`` on the way in and put back on the way out.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from .project import Point, Project, Wall

FIX_QUALITY = {0: "None", 1: "Gps", 2: "Dgps", 3: "Pps", 4: "RtkFixed", 5: "RtkFloat",
               6: "DeadReckoning", 98: "Network", 99: "Manual"}
FIX_NUMBER = {name: number for number, name in FIX_QUALITY.items()}

# Top-level Windows fields this app has no use for but must not drop.
CARRIED = ("GeoRef", "Track", "Issues")


def is_windows(payload: dict) -> bool:
    return "Points" in payload or "FloorPlanBase64" in payload or "FormatVersion" in payload


# ---------- times ----------

def _parse_time(text) -> float | None:
    """.NET writes up to seven fractional digits, which Python's parser won't take."""
    if not isinstance(text, str) or not text:
        return None
    cleaned = re.sub(r"(\.\d{6})\d+", r"\1", text.strip()).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()          # DateTime.Now: local time, no offset
    return moment.timestamp()


def _local_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def _utc_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _band(freq: float) -> str:
    return "2.4" if freq < 3000 else "5" if freq < 5925 else "6"


# ---------- Windows → this app ----------

def from_windows(payload: dict) -> Project:
    walls, suggested = [], []
    for barrier in payload.get("Barriers") or []:
        if barrier.get("Suggested"):
            suggested.append(barrier)       # unconfirmed on Windows, so kept out of the maths
            continue
        walls.append(Wall(
            x1=float(barrier.get("X1", 0)), y1=float(barrier.get("Y1", 0)),
            x2=float(barrier.get("X2", 0)), y2=float(barrier.get("Y2", 0)),
            db=float(barrier.get("Db", 3)), material=barrier.get("Material") or "Drywall",
            inferred=bool(barrier.get("Inferred")),
        ))

    extra = {key: payload[key] for key in CARRIED if payload.get(key)}
    if suggested:
        extra["SuggestedBarriers"] = suggested

    return Project(
        name=payload.get("Name") or "Untitled survey",
        plan_png=payload.get("FloorPlanBase64"),
        width=int(payload.get("FloorPlanWidth") or 1600),
        height=int(payload.get("FloorPlanHeight") or 1000),
        meters_per_pixel=payload.get("MetersPerPixel"),
        points=[_point_from_windows(p) for p in payload.get("Points") or []],
        walls=walls,
        extra=extra,
    )


def _point_from_windows(p: dict) -> Point:
    point = Point(x=float(p.get("X", 0)), y=float(p.get("Y", 0)))
    point.time = _parse_time(p.get("Time")) or point.time
    point.notes = list(p.get("Notes") or [])
    point.networks = [
        {
            "ssid": n.get("Ssid") or "",
            "bssid": (n.get("Bssid") or "").upper(),
            "rssi": int(n.get("Rssi", -100)),
            "freq_mhz": float(n.get("FrequencyMHz") or 0),
            "channel": int(n.get("Channel") or 0),
            "band": _band(float(n.get("FrequencyMHz") or 0)),
            "secured": bool(n.get("Secured")),
            "security": "" if n.get("Secured") is None else ("Secured" if n.get("Secured") else "Open"),
            "vendor": n.get("Vendor") or "",
            "rssi_source": "windows",
        }
        for n in p.get("Networks") or []
    ]

    connection = p.get("Connection")
    if connection:
        quality = connection.get("SignalQuality")
        point.link = {
            "bssid": (connection.get("Bssid") or "").upper(),
            "ssid": connection.get("Ssid") or "",
            "signal": float(quality / 2 - 100) if isinstance(quality, (int, float)) else 0.0,
            "tx_mbps": float(connection.get("TxRateMbps") or 0),
            "rx_mbps": float(connection.get("RxRateMbps") or 0),
            "generation": connection.get("PhyType") or "",
        }

    spectrum = p.get("Spectrum")
    if spectrum:
        point.spectrum = {
            "segments": [_segment_from_windows(s) for s in spectrum.get("Segments") or []],
            "noise_24": spectrum.get("NoiseFloor24"),
            "noise_5": spectrum.get("NoiseFloor5"),
            "noise_6": None,
        }
        if spectrum.get("Dwell"):
            point.spectrum["dwell"] = _segment_from_windows(spectrum["Dwell"])
            point.spectrum["dwell_center_mhz"] = spectrum.get("DwellCenterMHz")

    fix = p.get("Gps")
    if fix:
        quality = fix.get("Quality")
        point.gps = {
            "lat": fix.get("Lat"), "lon": fix.get("Lon"),
            "quality": FIX_NUMBER.get(quality, quality if isinstance(quality, int) else 0),
            "satellites": fix.get("Satellites", 0), "hdop": fix.get("Hdop"),
        }

    capture = p.get("Capture")
    if capture:
        point.capture = {
            "retry_rate": float(capture.get("RetryRate") or 0),
            "retry_eligible": int(capture.get("RetryEligible") or 0),
            "airtime": float(capture.get("AirtimeFraction") or 0),
            "clients": int(capture.get("Clients") or 0),
            "channel": int(capture.get("Channel") or 0),
        }

    if p.get("ThroughputDownMbps") is not None or p.get("ThroughputUpMbps") is not None:
        point.throughput = {"down_mbps": p.get("ThroughputDownMbps"),
                            "up_mbps": p.get("ThroughputUpMbps")}
    # Windows-only details, so they survive the round trip
    point.extra = {key: p[key] for key in
                   ("Id", "Links", "ThroughputDownMbps", "ThroughputUpMbps", "Connection", "Gps", "Capture")
                   if p.get(key) is not None}
    return point


def _segment_from_windows(s: dict) -> dict:
    return {
        "start_mhz": float(s.get("StartMHz") or 0),
        "step_mhz": float(s.get("StepMHz") or 0),
        "rbw_khz": float(s.get("RbwKHz") or 600),
        "sweeps": int(s.get("SweepCount") or 0),
        "avg": list(s.get("Avg") or []),
        "max": list(s.get("Max") or []),
        "min": list(s.get("Min") or []),
        "occupancy": {str(k): v for k, v in (s.get("ChannelOccupancy") or {}).items()},
    }


# ---------- this app → Windows ----------

def to_windows(project: Project) -> dict:
    extra = project.extra or {}
    barriers = [
        {"X1": w.x1, "Y1": w.y1, "X2": w.x2, "Y2": w.y2, "Material": w.material,
         "Db": w.db, "Suggested": False, "Inferred": bool(w.inferred)}
        for w in project.walls
    ] + list(extra.get("SuggestedBarriers") or [])

    return {
        "FormatVersion": 1,
        "Name": project.name,
        "FloorPlanBase64": project.plan_png,
        "FloorPlanWidth": project.width,
        "FloorPlanHeight": project.height,
        "MetersPerPixel": project.meters_per_pixel,
        "GeoRef": extra.get("GeoRef") or {"Anchors": []},
        "Barriers": barriers,
        "Track": extra.get("Track") or [],
        "Issues": extra.get("Issues") or [],
        "Points": [_point_to_windows(p) for p in project.points],
    }


def _point_to_windows(point: Point) -> dict:
    kept = point.extra or {}
    result = {
        "Id": kept.get("Id") or str(uuid.uuid4()),
        "X": point.x,
        "Y": point.y,
        "Time": _local_time(point.time),
        "Networks": [
            {
                "Ssid": n.get("ssid") or "",
                "Bssid": n.get("bssid") or "",
                "Rssi": int(n.get("rssi", -100)),
                "LinkQuality": max(0, min(100, (int(n.get("rssi", -100)) + 100) * 2)),
                "Channel": int(n.get("channel") or 0),
                "FrequencyMHz": float(n.get("freq_mhz") or 0),
                "BandGHz": {"2.4": 2.4, "5": 5.0, "6": 6.0}.get(n.get("band") or "", 0.0),
                "Secured": n.get("secured"),
                "Vendor": n.get("vendor") or "",
            }
            for n in point.networks
        ],
        "Connection": _connection_to_windows(point.link, kept.get("Connection")),
        "Links": kept.get("Links") or [],
        "Spectrum": _spectrum_to_windows(point.spectrum),
        "Gps": _fix_to_windows(point.gps, point.time, kept.get("Gps")),
        "ThroughputDownMbps": (point.throughput or {}).get("down_mbps", kept.get("ThroughputDownMbps")),
        "ThroughputUpMbps": (point.throughput or {}).get("up_mbps", kept.get("ThroughputUpMbps")),
        "Capture": _capture_to_windows(point.capture, point.time, kept.get("Capture")),
        "Notes": list(point.notes) + ([f"Note: {point.note}"] if point.note else []),
    }
    return result


def _connection_to_windows(link: dict | None, kept: dict | None) -> dict | None:
    if not link:
        return None
    signal = link.get("signal") or 0
    base = dict(kept or {})
    base.update({
        "Ssid": link.get("ssid") or "",
        "Bssid": link.get("bssid") or "",
        "SignalQuality": max(0, min(100, int((signal + 100) * 2))) if signal else base.get("SignalQuality", 0),
        "RxRateMbps": float(link.get("rx_mbps") or 0),
        "TxRateMbps": float(link.get("tx_mbps") or 0),
        "PhyType": link.get("generation") or base.get("PhyType", ""),
        "Auth": base.get("Auth", ""),
    })
    return base


def _segment_to_windows(s: dict) -> dict:
    return {
        "StartMHz": s.get("start_mhz", 0),
        "StepMHz": s.get("step_mhz", 0),
        "RbwKHz": s.get("rbw_khz", 600),
        "SweepCount": s.get("sweeps", 0),
        "Avg": list(s.get("avg") or []),
        "Max": list(s.get("max") or []),
        "Min": list(s.get("min") or []),
        "ChannelOccupancy": {str(k): v for k, v in (s.get("occupancy") or {}).items()},
    }


def _spectrum_to_windows(spectrum: dict | None) -> dict | None:
    if not spectrum:
        return None
    result = {
        "Segments": [_segment_to_windows(s) for s in spectrum.get("segments") or []],
        "NoiseFloor24": spectrum.get("noise_24"),
        "NoiseFloor5": spectrum.get("noise_5"),
        "Dwell": None,
        "DwellCenterMHz": None,
    }
    if spectrum.get("dwell"):
        result["Dwell"] = _segment_to_windows(spectrum["dwell"])
        result["DwellCenterMHz"] = spectrum.get("dwell_center_mhz")
    return result


def _fix_to_windows(fix: dict | None, when: float, kept: dict | None) -> dict | None:
    if not fix:
        return None
    base = dict(kept or {})
    base.update({
        "Lat": fix.get("lat"), "Lon": fix.get("lon"),
        "AltM": base.get("AltM"),
        "Quality": FIX_QUALITY.get(int(fix.get("quality") or 0), "Gps"),
        "Satellites": int(fix.get("satellites") or 0),
        "Hdop": fix.get("hdop"),
        "AccuracyM": base.get("AccuracyM"),
        "UtcTime": base.get("UtcTime") or _utc_time(when),
        "Source": base.get("Source") or "Linux",
    })
    return base


def _capture_to_windows(capture: dict | None, when: float, kept: dict | None) -> dict | None:
    if not capture:
        return None
    base = dict(kept or {"Start": _local_time(when), "Bss": []})
    base.update({
        "Channel": int(capture.get("channel") or 0),
        "RetryRate": float(capture.get("retry_rate") or 0),
        "RetryEligible": int(capture.get("retry_eligible") or 0),
        "AirtimeFraction": float(capture.get("airtime") or 0),
        "Clients": int(capture.get("clients") or 0),
    })
    return base
