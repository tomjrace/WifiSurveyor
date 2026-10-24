"""
Where surveys live and how to find them again: one shared folder, a list of every survey
saved or opened, and which surveys are of the same site, for the timeline.

The shared folder is ~/Documents/Wi-Fi Surveys of the person using the computer, whether the
app runs as them or as root (sudo or pkexec): files the root copy writes are handed back to
them, so either can open what the other saved. Surveys found only in root's own folders are
listed too when running as root, and saved to the shared folder from then on.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import pwd
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .project import data_dir, hand_back, home, in_root_home, invoking_user, make_dir

FOLDER = "Wi-Fi Surveys"
ROOT_PLACES = (Path("/root/.local/share/wifisurveyor"), Path("/root/Documents") / FOLDER)
SKIP = {"recovery.wsurvey"}


def documents_dir() -> Path:
    """The person's Documents folder, as their desktop names it (XDG user dirs)."""
    base = home()
    try:
        text = (base / ".config" / "user-dirs.dirs").read_text()
        found = re.search(r'^XDG_DOCUMENTS_DIR="([^"]+)"', text, re.MULTILINE)
        if found:
            path = Path(found.group(1).replace("$HOME", str(base)))
            if path != base:
                return path
    except OSError:
        pass
    return base / "Documents"


def surveys_dir() -> Path:
    return make_dir(documents_dir() / FOLDER)


def who() -> str:
    """Who is saving: the person, and whether it's the root copy acting for them."""
    user = invoking_user()
    if user:
        try:
            return f"{pwd.getpwuid(user[0]).pw_name} (as root)"
        except KeyError:
            return "root"
    if os.geteuid() == 0:
        return "root"
    try:
        return getpass.getuser()
    except Exception:
        return ""


def site_keys(payload: dict) -> list[str]:
    """
    What makes two surveys the same site: the site id a follow-up survey inherits, or an
    identical floor plan (which also covers surveys made before there were site ids).
    """
    keys = []
    if payload.get("site_id"):
        keys.append(f"site:{payload['site_id']}")
    plan = payload.get("plan_png")
    if plan:
        keys.append("plan:" + hashlib.sha1(plan.encode("ascii", "ignore")).hexdigest()[:20])
    return keys


def surveyed_at(payload: dict) -> float:
    """When the survey was done: its first measurement, or failing that when it was saved."""
    times = [p.get("time") for p in payload.get("points", []) if p.get("time")]
    if times:
        return float(min(times))
    return float((payload.get("saved") or {}).get("at") or 0)


@dataclass
class Info:
    path: str
    name: str
    points: int
    surveyed_at: float
    saved_at: float
    saved_by: str
    sites: list[str]
    size: tuple[int, int]
    root_only: bool               # in root's folders, where only the root copy can open it

    @property
    def folder(self) -> str:
        parent = Path(self.path).parent
        try:
            return "~/" + str(parent.relative_to(home()))
        except ValueError:
            return str(parent)


def catalogue(payload: dict) -> dict | None:
    """
    The few fields the survey list needs, from either file format. A Windows survey uses
    PascalCase names and has no ``points`` key, so reading it as a Linux file would drop it
    from Recent and from the timeline even though the app can open it.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("format") == "wifisurveyor-linux" or "points" in payload or "plan_png" in payload:
        return payload
    from . import interop

    if not interop.is_windows(payload):
        return None
    points = []
    for point in payload.get("Points") or []:
        if isinstance(point, dict):
            points.append({"time": interop._parse_time(point.get("Time"))})
    width = payload.get("FloorPlanWidth") or 1600
    height = payload.get("FloorPlanHeight") or 1000
    try:
        size = (int(width), int(height))
    except (TypeError, ValueError):
        size = (1600, 1000)
    return {
        "name": payload.get("Name") or "",
        "points": points,
        "width": size[0],
        "height": size[1],
        "plan_png": payload.get("FloorPlanBase64") or None,
        "saved": {},
    }


def describe(path: Path, payload: dict) -> Info:
    saved = payload.get("saved") or {}
    return Info(
        path=str(path), name=payload.get("name") or path.stem,
        points=len(payload.get("points", [])), surveyed_at=surveyed_at(payload),
        saved_at=float(saved.get("at") or path.stat().st_mtime),
        saved_by=saved.get("by") or "", sites=site_keys(payload),
        size=(int(payload.get("width", 1600)), int(payload.get("height", 1000))),
        root_only=str(path).startswith("/root/"))


# ---------- what's been seen before ----------

def _registry_path() -> Path:
    return data_dir() / "library.json"


def _registry() -> dict:
    try:
        return json.loads(_registry_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_registry(registry: dict) -> None:
    path = _registry_path()
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(registry))
        hand_back(temporary)
        temporary.replace(path)
    except OSError:
        pass


def remember(path: str | Path, payload: dict | None = None) -> None:
    """Notes a survey saved or opened, so it's offered at start and found for the timeline."""
    path = Path(path).resolve()
    registry = _registry()
    entries = registry.setdefault("surveys", {})
    try:
        payload = payload if payload is not None else json.loads(path.read_text(encoding="utf-8-sig"))
        listed = catalogue(payload)
        if listed is None:
            return
        info = describe(path, listed)
    except (OSError, ValueError, TypeError):
        return
    entries[str(path)] = {"mtime": path.stat().st_mtime, "info": asdict(info)}
    _write_registry(registry)


def forget(path: str | Path) -> None:
    registry = _registry()
    if registry.get("surveys", {}).pop(str(Path(path).resolve()), None) is not None:
        _write_registry(registry)


def _candidates() -> list[Path]:
    places = [surveys_dir(), data_dir()]
    if os.geteuid() == 0:
        places += [p for p in ROOT_PLACES if p.is_dir()]
    found = {Path(p) for p in _registry().get("surveys", {})}
    for place in places:
        try:
            found |= {p.resolve() for p in place.glob("*.wsurvey") if p.name not in SKIP}
        except OSError:
            continue
    return sorted(found)


def known() -> list[Info]:
    """Every survey that can be found, most recently saved first. Read once, then cached."""
    registry = _registry()
    entries = registry.setdefault("surveys", {})
    result, changed = [], False
    for path in _candidates():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            if entries.pop(str(path), None) is not None:
                changed = True                    # deleted or moved: drop it
            continue
        cached = entries.get(str(path))
        if cached and cached.get("mtime") == mtime and "sites" in cached.get("info", {}):
            info = Info(**{**cached["info"], "size": tuple(cached["info"]["size"])})
        else:
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                listed = catalogue(payload)
                if listed is None:
                    continue
                info = describe(path, listed)
            except (OSError, ValueError, TypeError):
                continue
            entries[str(path)] = {"mtime": mtime, "info": asdict(info)}
            changed = True
        result.append(info)
    if changed:
        _write_registry(registry)
    return sorted(result, key=lambda info: info.saved_at, reverse=True)


def same_site(project, infos: list[Info] | None = None) -> list[Info]:
    """
    Other surveys of this site whose plan lines up with this one, oldest first. Two surveys
    are the same site when one is a follow-up of the other, or their floor plans are identical.
    """
    keys = set(site_keys({"site_id": project.site_id, "plan_png": project.plan_png}))
    if not keys:
        return []
    mine = str(Path(project.path).resolve()) if project.path else None
    found = []
    for info in infos if infos is not None else known():
        if info.path == mine or info.size != (project.width, project.height):
            continue
        if keys & set(info.sites):
            found.append(info)
    return sorted(found, key=lambda info: info.surveyed_at)


def shared_path_for(path: str | Path) -> Path:
    """Where a survey found in root's folders is saved from now on: the shared folder."""
    path = Path(path)
    return surveys_dir() / path.name if in_root_home(path) or str(path).startswith("/root/") else path


def when(seconds: float, with_time: bool = True) -> str:
    if not seconds:
        return "undated"
    shown = time.localtime(seconds)
    this_year = shown.tm_year == time.localtime().tm_year
    return time.strftime(("%a %d %b" if this_year else "%d %b %Y") + (", %H:%M" if with_time else ""),
                         shown)
