"""Settings that belong to this machine and its hardware rather than to one survey."""

from __future__ import annotations

import json
from pathlib import Path

# The parsed file, so a lookup during a survey doesn't re-read it. Report branding keeps a
# logo in here, which is far larger than the other keys. A change written by this process
# updates the cache; a change to the file itself (a different mtime) is read again.
_cached: tuple[Path, float, dict] | None = None


def _path() -> Path:
    from .project import data_dir

    return data_dir() / "settings.json"


def _remember(path: Path, parsed: dict) -> dict:
    global _cached
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cached = None
        return parsed
    _cached = (path, mtime, parsed)
    return parsed


def _view(parsed: dict) -> dict:
    """A copy whose nested dicts are also copies, so a caller can't change the cache."""
    return {key: dict(value) if isinstance(value, dict) else value for key, value in parsed.items()}


def load() -> dict:
    """A copy of the settings. Callers can change the copy; only put() saves."""
    global _cached
    path = _path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cached = None
        return {}
    if _cached and _cached[0] == path and _cached[1] == mtime:
        return _view(_cached[2])
    try:
        parsed = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    _remember(path, parsed)
    return _view(parsed)


def get(key: str, default=None):
    return load().get(key, default)


def put(key: str, value) -> None:
    everything = load()
    everything[key] = value
    path = _path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(everything, indent=1))
    from .project import hand_back

    hand_back(temporary)
    temporary.replace(path)
    _remember(path, everything)
