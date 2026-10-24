"""The survey document: points, plan, walls, and where it lives on disk."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

METRES_PER_FOOT = 0.3048


def _safe_name(name: str) -> str:
    cleaned = "".join(ch if ch not in '\\/:' else "-" for ch in name).strip()
    return cleaned or "survey"


def _from_dict(kind, payload: dict):
    """Ignores keys this version doesn't know, so an older app can still open a newer file."""
    fields = getattr(kind, "__dataclass_fields__")
    return kind(**{key: value for key, value in payload.items() if key in fields})


def invoking_user() -> tuple[int, int, Path] | None:
    """
    (uid, gid, home) of the person behind a root copy started with sudo or pkexec, or None when
    this process isn't root on someone's behalf. sudo sets HOME to /root, so without this every
    survey, report and setting would land in root's folders instead of theirs.
    """
    if os.geteuid() != 0:
        return None
    import pwd

    for key in ("SUDO_UID", "PKEXEC_UID"):
        try:
            entry = pwd.getpwuid(int(os.environ.get(key, "")))
        except (KeyError, ValueError):
            continue
        if entry.pw_uid != 0:
            return entry.pw_uid, entry.pw_gid, Path(entry.pw_dir)
    return None


def home() -> Path:
    """The person's home folder, even from the root copy."""
    user = invoking_user()
    return user[2] if user else Path.home()


def hand_back(path: str | Path) -> None:
    """Gives a file the root copy wrote to the person who started it, so they can open and delete it."""
    user = invoking_user()
    if user:
        try:
            os.chown(path, user[0], user[1])
        except OSError:
            pass


def in_root_home(path: str | Path) -> bool:
    """Whether the root copy would be writing into root's own folders rather than the person's."""
    if not invoking_user():
        return False
    root = Path("/root").resolve()
    target = Path(path).resolve()
    return target == root or root in target.parents


def make_dir(path: Path) -> Path:
    """mkdir -p, handing every folder it makes to the person, not root."""
    missing = []
    step = path
    while not step.exists() and step != step.parent:
        missing.append(step)
        step = step.parent
    path.mkdir(parents=True, exist_ok=True)
    for made in missing:
        hand_back(made)
    return path


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    if not base or in_root_home(base):
        base = home() / ".local" / "share"
    return make_dir(Path(base) / "wifisurveyor")


@dataclass
class Point:
    x: float
    y: float
    time: float = field(default_factory=time.time)
    networks: list[dict] = field(default_factory=list)
    link: dict | None = None
    spectrum: dict | None = None
    # The whole spectrum as the Spectrum page drew it at the time, a base64 PNG, for the report
    spectrum_png: str | None = None
    capture: dict | None = None
    # Latency, jitter and loss to the gateway and DNS time, when the adapter was connected
    connectivity: dict | None = None
    # Bluetooth devices heard here (address, name, rssi, kind, vendor), when that scan was on
    bluetooth: list[dict] = field(default_factory=list)
    # Item trackers heard here (AirTags, Find Hub tags, SmartTags…): address, rssi, kind,
    # label, away (from its owner), state, battery
    trackers: list[dict] = field(default_factory=list)
    gps: dict | None = None
    notes: list[str] = field(default_factory=list)
    # What the surveyor wrote about this spot ("behind the freezer", "metal racking")
    note: str = ""
    # iperf3 to a server on the network: {"down_mbps", "up_mbps", "server"} or {"error"}
    throughput: dict | None = None
    # One per network tested from the Advanced table: {"ssid", "adapter", "server",
    # "down_mbps", "up_mbps"} or {"ssid", "adapter", "error"}
    throughputs: list[dict] = field(default_factory=list)
    # How it was measured: adapter, analyzer, sweeps, scans, for the report's method section
    method: dict | None = None
    # The HackRF's close look at channels here: one rf.iq.classify result per channel
    iq: list[dict] = field(default_factory=list)
    # Downlink power per cellular (and other) band, from the HackRF: rf.cellular.summarize
    cellular: dict | None = None
    # The test tone heard here: {"freq_mhz", "dbm"}
    tone: dict | None = None
    # The bands beyond Wi-Fi the survey was asked to measure (production, industrial,
    # business): {band key: rf.bands.summarize entry, plus "analyzer"}
    bands: dict | None = None
    # Every range beyond Wi-Fi swept here, for the report's pictures: {"name", "start_mhz",
    # "stop_mhz", "rbw_khz", "analyzer", "avg", "max"}, bins evenly spaced (ui.survey_view)
    ranges: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)        # Windows-only details, carried through

    def best(self, ssid: str | None = None, bssid: str | None = None, band: str = "All") -> dict | None:
        candidates = [
            n for n in self.networks
            if (ssid is None or n["ssid"] == ssid)
            and (bssid is None or n["bssid"] == bssid)
            and (band == "All" or n.get("band") == band)
        ]
        return max(candidates, key=lambda n: n["rssi"], default=None)


@dataclass
class Wall:
    x1: float
    y1: float
    x2: float
    y2: float
    db: float = 3.0
    material: str = "Drywall"
    inferred: bool = False


@dataclass
class Project:
    name: str = "Untitled survey"
    # The site this is a survey of. A follow-up survey keeps it, which is what puts surveys
    # of one site on the same timeline. None for files made before it existed.
    site_id: str | None = field(default_factory=lambda: uuid.uuid4().hex)
    plan_png: str | None = None          # base64, so a project is one portable file
    width: int = 1600
    height: int = 1000
    meters_per_pixel: float | None = None
    points: list[Point] = field(default_factory=list)
    walls: list[Wall] = field(default_factory=list)
    path: Path | None = None
    # The analyzer checked against the Wi-Fi card on a busy channel: how far apart their
    # readings are ("offset_db", analyzer minus card), and on what, and when
    rf_calibration: dict | None = None
    # Access points the surveyor moved to where they really are, or named: each
    # {"bssids": [...], "networks": [[ssid, [bands]], ...], "name", "x", "y"}; x and y are None
    # when only the name was given and the position is still the estimate
    ap_pins: list[dict] = field(default_factory=list)
    # Access points that don't exist yet, to see what adding them would do:
    # {"x", "y", "name", "bands": ["2.4", "5"]}
    virtual_aps: list[dict] = field(default_factory=list)
    # dB taken off every signal before it's judged, so "meets" means on a phone, not the
    # survey adapter (phones typically hear 3-6 dB less)
    client_allowance_db: float = 0.0
    # For the report's cover: client, site
    report_info: dict = field(default_factory=dict)
    # The HackRF's radar watch on the DFS channels: {"watches": [{"time", "seconds",
    # "channels": [...]}], "events": [{"time", "center_mhz", "pri_us", "width_us", "pulses"}]}
    radar: dict = field(default_factory=dict)
    # Where the test tone is sent from, to measure walls: {"x", "y", "freq_mhz"}
    tone_source: dict | None = None
    # The Advanced network tests table for this survey: {"enabled", "scan_with", "region",
    # "rows": [{"ssid", "adapter", "server"}]}. Per survey, so one site's networks are never
    # tried at another.
    speed_tests: dict = field(default_factory=dict)
    # What each point measures, chosen in the survey setup: core.surveysetup.Setup.to_dict()
    survey_setup: dict = field(default_factory=dict)
    # Names heard for hidden access points while listening in monitor mode, BSSID (upper
    # case) → name (core.hidden). The points' networks carry the names too.
    hidden_names: dict = field(default_factory=dict)
    # Where the plan sits on the Earth, when it was placed on a map (ui.site_map): {"lat",
    # "lon" (the plan's centre), "rotation_deg" (clockwise from north-up), "meters_per_pixel",
    # "zoom", "source", "credit", "located_by", "plan": "blueprint" or "map"}
    geo: dict = field(default_factory=dict)
    # What only the Windows app uses (GPS pins, walk tracks, walk issues), carried through
    # untouched so a survey that visits Linux goes back with nothing missing.
    extra: dict = field(default_factory=dict)
    modified: bool = field(default=False, compare=False)

    # ---------- storage ----------

    def to_dict(self) -> dict:
        from .library import who

        payload = {
            "format": "wifisurveyor-linux",
            "name": self.name,
            "site_id": self.site_id,
            "saved": {"by": who(), "at": time.time()},
            "plan_png": self.plan_png,
            "width": self.width,
            "height": self.height,
            "meters_per_pixel": self.meters_per_pixel,
            "points": [asdict(p) for p in self.points],
            "walls": [asdict(w) for w in self.walls],
        }
        if self.rf_calibration:
            payload["rf_calibration"] = self.rf_calibration
        for key in ("ap_pins", "virtual_aps", "client_allowance_db", "report_info", "radar",
                    "tone_source", "speed_tests", "survey_setup", "hidden_names", "geo"):
            if getattr(self, key):
                payload[key] = getattr(self, key)
        if self.extra:
            payload["extra"] = self.extra
        return payload

    def write(self, target: Path, payload: dict | None = None) -> Path:
        """Writes the file without changing where this project thinks it lives."""
        target = Path(target)
        if in_root_home(target):
            raise ValueError(f"{target.parent} is root's folder. Save it in your own, such as "
                             f"{data_dir()}.")
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload if payload is not None else self.to_dict()))
        hand_back(temporary)
        temporary.replace(target)                       # never leave a half-written survey
        return target

    def save(self, path: str | Path | None = None) -> Path:
        from . import library

        target = Path(path or self.path or library.surveys_dir() / f"{_safe_name(self.name)}.wsurvey")
        payload = self.to_dict()
        self.write(target, payload)
        self.path = target
        self.modified = False
        library.remember(target, payload)
        return target

    def follow_up(self) -> "Project":
        """
        A new survey of the same site: the plan, its scale, the walls, the access points
        placed and planned, the tone source, the speed-test table and the survey setup carry
        over; the measurements start empty. It shares the site id, so the two sit on one timeline.
        """
        fresh = Project(
            # The site's name with today's date, replacing the date an earlier follow-up added
            name=f"{re.sub(r' \d{4}-\d{2}-\d{2}$', '', self.name)} {time.strftime('%Y-%m-%d')}",
            plan_png=self.plan_png,
            width=self.width, height=self.height, meters_per_pixel=self.meters_per_pixel,
            walls=copy.deepcopy(self.walls), ap_pins=copy.deepcopy(self.ap_pins),
            virtual_aps=copy.deepcopy(self.virtual_aps),
            client_allowance_db=self.client_allowance_db,
            report_info=copy.deepcopy(self.report_info), tone_source=copy.deepcopy(self.tone_source),
            speed_tests=copy.deepcopy(self.speed_tests),
            survey_setup=copy.deepcopy(self.survey_setup), geo=copy.deepcopy(self.geo),
            # The same site's access points: a name heard once names them in the follow-up too
            hidden_names=dict(self.hidden_names))
        # A survey from before site ids is still tied to the follow-up by their identical plan
        if self.site_id is not None:
            fresh.site_id = self.site_id
        fresh.modified = True
        return fresh

    def save_for_windows(self, path: str | Path) -> Path:
        from .interop import to_windows

        return self.write(Path(path), to_windows(self))

    @classmethod
    def from_dict(cls, payload: dict) -> "Project":
        if not isinstance(payload, dict):
            raise ValueError("This file isn't a Wi-Fi survey.")
        from .interop import from_windows, is_windows

        if is_windows(payload):
            return from_windows(payload)
        if "points" not in payload and "plan_png" not in payload:
            raise ValueError("This file isn't a Wi-Fi survey: it has no points and no floor plan.")
        return cls(
            name=payload.get("name", "Untitled survey"),
            site_id=payload.get("site_id"),
            plan_png=payload.get("plan_png"),
            width=int(payload.get("width", 1600)),
            height=int(payload.get("height", 1000)),
            meters_per_pixel=payload.get("meters_per_pixel"),
            points=[_from_dict(Point, p) for p in payload.get("points", [])],
            walls=[_from_dict(Wall, w) for w in payload.get("walls", [])],
            rf_calibration=payload.get("rf_calibration"),
            ap_pins=list(payload.get("ap_pins") or []),
            virtual_aps=list(payload.get("virtual_aps") or []),
            client_allowance_db=float(payload.get("client_allowance_db") or 0),
            report_info=dict(payload.get("report_info") or {}),
            radar=dict(payload.get("radar") or {}),
            tone_source=payload.get("tone_source"),
            speed_tests=dict(payload.get("speed_tests") or {}),
            survey_setup=dict(payload.get("survey_setup") or {}),
            hidden_names={str(k).upper(): str(v)
                          for k, v in (payload.get("hidden_names") or {}).items()},
            geo=dict(payload.get("geo") or {}),
            extra=payload.get("extra") or {},
        )

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(f"This file isn't a Wi-Fi survey ({error.msg}).") from error
        project = cls.from_dict(payload)
        project.path = Path(path)
        return project

    def set_plan(self, image_bytes: bytes, width: int, height: int) -> None:
        self.plan_png = base64.b64encode(image_bytes).decode("ascii")
        self.width, self.height = width, height

    def plan_bytes(self) -> bytes | None:
        return base64.b64decode(self.plan_png) if self.plan_png else None

    # ---------- scale ----------

    def set_scale(self, pixels: float, feet: float) -> None:
        """Two spots ``pixels`` apart on the plan are ``feet`` apart in the building."""
        if pixels <= 0 or feet <= 0:
            raise ValueError("Both the distance on the plan and in feet must be more than zero.")
        self.meters_per_pixel = feet * METRES_PER_FOOT / pixels

    def feet(self, pixels: float) -> float | None:
        """A plan distance in feet, or None before the scale is set."""
        return pixels * self.meters_per_pixel / METRES_PER_FOOT if self.meters_per_pixel else None

    def pixels(self, feet: float) -> float | None:
        """A distance in feet as plan pixels, or None before the scale is set."""
        return feet * METRES_PER_FOOT / self.meters_per_pixel if self.meters_per_pixel else None


# ---------- crash recovery ----------

def recovery_path() -> Path:
    return data_dir() / "recovery.wsurvey"


def write_recovery(project: Project) -> Path:
    """A copy of unsaved work, naming the file it belongs to, so a crash costs minutes at most."""
    payload = project.to_dict()
    payload["recovered_from"] = str(project.path) if project.path else None
    payload["recovered_at"] = time.time()
    return project.write(recovery_path(), payload)


def read_recovery() -> tuple[Project, str | None, float] | None:
    path = recovery_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
        project = Project.from_dict(payload)
    except Exception:
        return None
    origin = payload.get("recovered_from")
    project.path = Path(origin) if origin else None
    project.modified = True
    return project, origin, float(payload.get("recovered_at") or path.stat().st_mtime)


def clear_recovery() -> None:
    try:
        recovery_path().unlink()
    except FileNotFoundError:
        pass
