"""
How a site changed between surveys: a few headline figures per survey, and what changed
between them, in words. The timeline on the map and the report's History section use these.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from . import advice, analysis, library
from ..rf import cellular


@dataclass(frozen=True)
class Figure:
    key: str
    label: str
    unit: str
    higher_is_better: bool
    notable: float               # a change smaller than this is "about the same"


FIGURES = (
    Figure("meets", "Points meeting the requirement", "%", True, 5),
    Figure("signal", "Median signal", "dBm", True, 2),
    Figure("snr", "Median signal-to-noise", "dB", True, 2),
    Figure("noise", "Median noise floor", "dBm", False, 2),
    Figure("issues", "High-priority issues", "", False, 1),
    Figure("throughput", "Median download", "Mbps", True, 10),
    Figure("cellular", "Median cellular (best band)", "dBm", True, 3),
)
BY_KEY = {figure.key: figure for figure in FIGURES}


def _median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return float(statistics.median(values)) if values else None


def figures(project, profile: str = "data", ssid: str | None = None, band: str = "All") -> dict:
    """The headline figures of one survey, judged the same way for every survey compared."""
    ctx = analysis.Context(project, profile=profile)
    points = project.points
    signal, snr, noise, judged, high = [], [], [], [], 0
    for point in points:
        best = point.best(ssid, None, band)
        if best is not None:
            signal.append(best["rssi"])
            floor = analysis.noise_floor(point, best.get("band"))
            if floor is not None:
                snr.append(best["rssi"] - floor)
                noise.append(floor)
        ok, _why = analysis.requirement(point, ctx, ssid, band)
        if ok is not None:
            judged.append(ok)
        high += sum(1 for item in advice.improvements(point, ctx, ssid, band) if item.severity == "High")
    speeds = [r.get("down_mbps") for p in points for r in [p.throughput or {}, *p.throughputs]
              if (not ssid or r.get("ssid") in (None, ssid))]
    payload = {"points": [{"time": p.time} for p in points]}
    return {
        "name": project.name,
        "when": library.surveyed_at(payload),
        "points": len(points),
        "meets": 100 * sum(judged) / len(judged) if judged else None,
        "signal": _median(signal),
        "snr": _median(snr),
        "noise": _median(noise),
        "issues": high if points else None,
        "throughput": _median(speeds),
        "cellular": _median([cellular.strongest(p.cellular) for p in points]),
        "requirement": ctx.profile.name,
    }


def change(before: dict, after: dict, key: str) -> tuple[float, str] | None:
    """How a figure moved, and whether that's "better", "worse" or "same"."""
    if before.get(key) is None or after.get(key) is None:
        return None
    figure = BY_KEY[key]
    delta = after[key] - before[key]
    if abs(delta) < figure.notable:
        return delta, "same"
    return delta, "better" if (delta > 0) == figure.higher_is_better else "worse"


def _value(key: str, value: float) -> str:
    figure = BY_KEY[key]
    if key == "meets":
        return f"{value:.0f}%"
    if key == "issues":
        return f"{value:.0f}"
    return f"{value:.0f} {figure.unit}"


def describe_change(before: dict, after: dict) -> str:
    """One sentence: what got better, what got worse, what held, in plain words."""
    better, worse = [], []
    for figure in FIGURES:
        moved = change(before, after, figure.key)
        if moved is None or moved[1] == "same":
            continue
        delta, verdict = moved
        if figure.key == "issues":
            text = (f"{abs(delta):.0f} {'fewer' if delta < 0 else 'more'} high-priority issue"
                    f"{'s' if abs(delta) != 1 else ''}")
        elif figure.key == "meets":
            text = (f"points meeting {after['requirement']}: {_value('meets', before['meets'])} → "
                    f"{_value('meets', after['meets'])}")
        else:
            word = "up" if delta > 0 else "down"
            # A difference between two levels in dBm is in dB
            unit = "dB" if figure.unit == "dBm" else figure.unit
            text = (f"{figure.label[0].lower()}{figure.label[1:]} {word} {abs(delta):.0f} "
                    f"{unit} ({_value(figure.key, before[figure.key])} → "
                    f"{_value(figure.key, after[figure.key])})")
        (better if verdict == "better" else worse).append(text)
    if not better and not worse:
        return "About the same on every measure."
    parts = []
    if better:
        parts.append("Better: " + "; ".join(better) + ".")
    if worse:
        parts.append("Worse: " + "; ".join(worse) + ".")
    return " ".join(parts)


def span(seconds: float) -> str:
    """How far apart two surveys are, in the unit a person would use."""
    days = abs(seconds) / 86400
    if days < 1:
        hours = max(1, round(abs(seconds) / 3600))
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if days < 14:
        return f"{round(days)} day{'s' if round(days) != 1 else ''}"
    if days < 60:
        return f"{round(days / 7)} weeks"
    if days < 730:
        return f"{round(days / 30.4)} months"
    return f"{days / 365.25:.1f} years"


def summary(rows: list[dict]) -> list[str]:
    """
    The story of the site in a few sentences, oldest survey first in ``rows``: the change
    since the previous survey, and for a longer history, since the first.
    """
    if len(rows) < 2:
        return []
    latest, previous, first = rows[-1], rows[-2], rows[0]
    said = [f"Since the previous survey ({library.when(previous['when'], False)}, "
            f"{span(latest['when'] - previous['when'])} earlier): "
            + describe_change(previous, latest)]
    if len(rows) > 2:
        said.append(f"Since the first survey ({library.when(first['when'], False)}, "
                    f"{span(latest['when'] - first['when'])} earlier, {len(rows)} surveys in all): "
                    + describe_change(first, latest))
    return said
