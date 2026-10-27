"""
What a Watch-tab session found, as a report of its own or as a section of a survey's report.

A session is read back from its logs (outage.SessionLog), so a watch still running, one just
stopped and one from last week are summarised the same way. Both bands of an access point
watched at once are two logs side by side; they make one summary with a part per band.
The summary is plain data, so a survey can keep it (Project.watches) with where on the plan
the watching laptop stood.
"""

from __future__ import annotations

import math
import statistics
import time
from html import escape
from pathlib import Path

from . import outage

# Two logs started this close together, of the same network, are one watch of both its bands
PARTNER_SECONDS = 120


def _band_of(freq: float | None) -> str:
    if not freq:
        return ""
    return "2.4 GHz" if freq < 3000 else "5 GHz" if freq < 5925 else "6 GHz"


def _median(pairs: list[tuple[float, int]]) -> float | None:
    """The median of values each standing for ``weight`` beats: a beat is 1, a minute its count."""
    pairs = sorted(pairs)
    total = sum(weight for _value, weight in pairs)
    if not total:
        return None
    running = 0
    for value, weight in pairs:
        running += weight
        if running * 2 >= total:
            return value
    return pairs[-1][0]


def band_summary(path: Path) -> dict:
    """
    One log's watch of one radio: how long, how available, how it behaved, what went wrong.
    Reads a compacted log the same as a plain one: its minute records count for their beats.
    """
    start, target, end = {}, {}, None
    beats, minutes, channel, spectrum, incidents = [], [], [], [], {}
    for record in outage.read_records(path):
        kind = record.get("type")
        if kind == "start" and not start:
            start = record
        elif kind == "target":
            target = record
        elif kind == "stop":
            end = record.get("t")
        elif kind == "beat":
            beats.append(record)
        elif kind == "minute":
            minutes.append(record)
        elif kind == "channel":
            channel.append(record)
        elif kind == "spectrum":
            spectrum.append(record)
        elif kind == "incident":
            incident = outage.Incident.from_json(record["incident"])
            incidents[incident.id] = incident
    incidents = sorted(incidents.values(), key=lambda i: i.start)
    times = [b["t"] for b in beats] + [m.get("until", m["t"]) for m in minutes]
    began = start.get("t") or (min(times) if times else time.time())
    end = end or (max(times) if times else began)

    def figure(key: str) -> float | None:
        pairs = [(b[key], 1) for b in beats if b.get(key) is not None]
        pairs += [(m[key][1], m["n"]) for m in minutes if isinstance(m.get(key), list)]
        return _median(pairs)

    states = [b.get("state") for b in beats]
    counted = len(states) + sum(m["n"] for m in minutes)
    up = sum(1 for s in states if s == "up") + sum((m.get("state") or {}).get("up", 0) for m in minutes)
    return {
        "ssid": start.get("ssid") or target.get("ssid") or "",
        "bssid": (start.get("bssid") or target.get("bssid") or "").upper(),
        "band": _band_of(target.get("freq")),
        "freq_mhz": target.get("freq"),
        "start": began, "end": end,
        "availability": outage.availability(began, end, incidents),
        "healthy_share": up / counted if counted else None,
        "signal": figure("signal"),
        "gateway_ms": figure("gw"),
        "retries": figure("air_retry"),
        "clients": figure("ap_clients"),
        "monitor": bool(target.get("monitor")),
        "patterns": outage.patterns(incidents),
        "incidents": [_incident(i) for i in incidents],
        "folder": str(Path(path).parent),
        # Read by summary(), which lays every band on one time axis, and not kept
        "_raw": {"beats": beats, "minutes": minutes, "channel": channel, "spectrum": spectrum},
    }


# ---------- the timeline: everything on one time axis ----------

# A column of the timeline per this many seconds, as long as the watch fits MAX_COLUMNS of
# them; a longer watch gets wider columns
TIMELINE_SECONDS = 30.0
MAX_COLUMNS = 4000
TIMELINE_BINS = 64                     # the spectrum's rows in a kept timeline: plenty at report size
STATES = {"up": "u", "degraded": "g", "joining": "j", "down": "d"}
WORST = "djgu "                        # the worst state in a column wins it
NONE = 255                             # a packed byte with nothing in it


def _pack(values: list[float | None], encode) -> str:
    import base64

    return base64.b64encode(bytes(NONE if v is None else max(0, min(254, encode(v)))
                                  for v in values)).decode()


def _unpack(text: str, decode) -> list[float | None]:
    import base64

    return [None if b == NONE else decode(b) for b in base64.b64decode(text)]


def pack_dbm(values):
    return _pack(values, lambda v: round(v - outage.LEVEL_FLOOR))


def unpack_dbm(text):
    return _unpack(text, lambda b: float(b + outage.LEVEL_FLOOR))


def pack_ms(values):
    """Milliseconds on a log scale: 1 ms to about 100 s in 254 steps."""
    return _pack(values, lambda v: round(math.log10(max(1.0, v)) * 50))


def unpack_ms(text):
    return _unpack(text, lambda b: 10 ** (b / 50))


def _timeline(raw: dict, t0: float, spp: float, columns: int) -> dict:
    """
    One band's watch as columns of ``spp`` seconds from ``t0``: the worst state, the median
    signal, the slowest gateway reply and the channel's average power in each, and the
    spectrum's average per bin (a minute record counts towards every column it covers).
    """
    def column(t: float) -> int:
        return min(columns - 1, max(0, int((t - t0) // spp)))

    state = [" "] * columns
    signal: list[list[float]] = [[] for _ in range(columns)]
    gateway: list[list[float]] = [[] for _ in range(columns)]
    power: list[list[float]] = [[] for _ in range(columns)]

    def worse(index: int, code: str) -> None:
        if WORST.index(code) < WORST.index(state[index]):
            state[index] = code

    for beat in raw["beats"]:
        index = column(beat["t"])
        worse(index, STATES.get(beat.get("state"), "u"))
        if beat.get("signal") is not None:
            signal[index].append(beat["signal"])
        if beat.get("gw") is not None:
            gateway[index].append(beat["gw"])
    for minute in raw["minutes"]:
        for index in range(column(minute["t"]), column(minute.get("until", minute["t"])) + 1):
            for name, count in (minute.get("state") or {}).items():
                if count:
                    worse(index, STATES.get(name, "u"))
            if isinstance(minute.get("signal"), list):
                signal[index].append(minute["signal"][1])
            if isinstance(minute.get("gw"), list):
                gateway[index].append(minute["gw"][2])
    for reading in raw["channel"]:
        if reading.get("power") is not None:
            power[column(reading["t"])].append(reading["power"])
    found = {
        "state": "".join(state),
        "signal": pack_dbm([statistics.median(v) if v else None for v in signal]),
        "gateway": pack_ms([max(v) if v else None for v in gateway]),
        "power": pack_dbm([statistics.fmean(v) if v else None for v in power]),
    }
    rows = [{"t": r.get("at", r["t"]), "start": r["start"], "step": r["step"],
             "avg": outage.unpack_levels(r["avg"])} for r in raw["spectrum"]]
    if rows:
        import numpy as np

        levels, (low, high) = spectrum_grid(rows, t0, spp, columns, stale=spp * 1.5,
                                            bins=TIMELINE_BINS)
        flat = levels.T.reshape(-1)                 # column after column, low to high in each
        found["spectrum"] = {"low": low, "high": high, "bins": levels.shape[0],
                             "avg": pack_dbm([None if np.isnan(v) else float(v) for v in flat])}
    return found


def spectrum_rows(rows: list[dict], bins: int = outage.SPECTRUM_BINS):
    """
    Spectrum rows ({"t", "start", "step", "avg"}, oldest first) on the newest row's
    frequencies, at most ``bins`` of them: (times, levels a row each, (low MHz, high MHz)).
    The slow half of spectrum_grid, worth keeping while no new row arrives.
    """
    import numpy as np

    latest = rows[-1]
    span = len(latest["avg"])
    low, high = latest["start"], latest["start"] + latest["step"] * (span - 1)
    grid = np.linspace(low, high, min(bins, span))
    matrix = np.empty((len(rows), len(grid)))
    for index, row in enumerate(rows):
        freqs = row["start"] + row["step"] * np.arange(len(row["avg"]))
        matrix[index] = np.interp(grid, freqs, row["avg"], left=np.nan, right=np.nan)
    return np.array([row["t"] for row in rows]), matrix, (float(low), float(high))


def grid_columns(times, matrix, a: float, spp: float, columns: int, stale: float):
    """
    spectrum_rows' levels as a picture: a column per ``spp`` seconds from ``a``, a row per bin
    with the highest frequency on top. Each column shows the latest row up to its end, for as
    long as ``stale``; NaN where there's none.
    """
    import numpy as np

    ends = a + spp * (np.arange(columns) + 1)
    latest = np.searchsorted(times, ends, side="right") - 1
    levels = np.full((matrix.shape[1], columns), np.nan)
    valid = latest >= 0
    valid[valid] &= ends[valid] - times[latest[valid]] <= stale + spp
    levels[:, valid] = matrix[latest[valid]].T
    return levels[::-1]


def spectrum_grid(rows: list[dict], a: float, spp: float, columns: int, stale: float,
                  bins: int = outage.SPECTRUM_BINS):
    """Spectrum rows as a picture of levels on a time axis: (levels, (low MHz, high MHz))."""
    times, matrix, span = spectrum_rows(rows, bins)
    return grid_columns(times, matrix, a, spp, columns, stale), span


BAND_COLOURS = {"2.4 GHz": (0.10, 0.50, 0.92), "5 GHz": (0.55, 0.30, 0.85),
                "6 GHz": (0.05, 0.62, 0.55)}
STATE_COLOURS = {"u": (0.13, 0.64, 0.29), "g": (0.92, 0.70, 0.03), "j": (0.92, 0.70, 0.03),
                 "d": (0.86, 0.15, 0.15), " ": (0.90, 0.91, 0.93)}
TICKS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]


def timeline_png(watch: dict) -> bytes | None:
    """
    The watch on one time axis, for a report: each band's state, the signal, the gateway's
    reply time, the channel's power and each band's spectrum as a waterfall (frequency up the
    side), dropouts shaded through all of them. None without pycairo or a timeline.
    """
    try:
        import io

        import cairo
        import numpy as np

        from .colours import WATERFALL_COLOURS, WATERFALL_HIGH, WATERFALL_LOW, WATERFALL_STOPS
    except ImportError:
        return None
    axis = watch.get("timeline")
    bands = [b for b in watch.get("bands", []) if b.get("timeline")]
    if not axis or not bands:
        return None
    t0, spp, columns = axis["t0"], axis["spp"], axis["n"]
    col_w = max(1, min(8, 960 // columns))
    left, right = 118, 12
    plot_w = columns * col_w
    with_spectrum = [b for b in bands if b["timeline"].get("spectrum")]
    lanes = ([("state", b, 16) for b in bands] + [("signal", None, 70), ("gateway", None, 70),
             ("power", None, 56)] + [("spectrum", b, min(96, b["timeline"]["spectrum"]["bins"]))
                                     for b in with_spectrum])
    gap, axis_h, legend_h = 8, 22, 18
    height = legend_h + sum(h + gap for _k, _b, h in lanes) + axis_h
    width = left + plot_w + right
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    cr = cairo.Context(surface)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.select_font_face("Sans")

    def x_of(index: float) -> float:
        return left + index * col_w

    def label(text: str, y: float, colour=(0.28, 0.33, 0.41), size=11, x=6.0) -> None:
        cr.set_source_rgb(*colour)
        cr.set_font_size(size)
        cr.move_to(x, y)
        cr.show_text(text)

    # Which band is which colour
    x = left
    for band in bands:
        colour = BAND_COLOURS.get(band["band"], (0.3, 0.3, 0.3))
        cr.set_source_rgb(*colour)
        cr.rectangle(x, 5, 14, 3)
        cr.fill()
        label(band["band"] or "?", 11, colour, 11, x + 18)
        x += 80

    top = legend_h
    frames = []
    for kind, band, h in lanes:
        frames.append((kind, band, top, h))
        top += h + gap

    def line(values, low, high, y, h, colour, log=False) -> None:
        cr.set_source_rgb(*colour)
        cr.set_line_width(1.5)
        drawing = False
        for index, value in enumerate(values):
            if value is None:
                drawing = False
                continue
            share = ((math.log10(value) - math.log10(low)) / (math.log10(high) - math.log10(low))
                     if log else (value - low) / (high - low))
            px, py = x_of(index + 0.5), y + h - max(0.0, min(1.0, share)) * h
            (cr.line_to if drawing else cr.move_to)(px, py)
            drawing = True
        cr.stroke()

    def grid(y, h, marks, low, high, unit, log=False) -> None:
        cr.set_line_width(1)
        for mark in marks:
            share = ((math.log10(mark) - math.log10(low)) / (math.log10(high) - math.log10(low))
                     if log else (mark - low) / (high - low))
            py = round(y + h - share * h) + 0.5
            cr.set_source_rgb(0.90, 0.91, 0.93)
            cr.move_to(left, py)
            cr.line_to(left + plot_w, py)
            cr.stroke()
            cr.set_font_size(9)
            text = f"{mark:g}{unit}"
            label(text, py + 4, size=9, x=left - 6 - cr.text_extents(text).x_advance)

    for kind, band, y, h in frames:
        if kind == "state":
            label(f"Status {band['band']}", y + 12, size=10)
            for index, code in enumerate(band["timeline"]["state"]):
                cr.set_source_rgb(*STATE_COLOURS.get(code, STATE_COLOURS[" "]))
                cr.rectangle(x_of(index), y, col_w, h)
                cr.fill()
        elif kind in ("signal", "gateway", "power"):
            title, units, low, high, marks, log, read = {
                "signal": ("Signal", "dBm", -90, -30, [-80, -60, -40], False, unpack_dbm),
                "gateway": ("Gateway", "ms, log", 1, 1000, [1, 10, 100, 1000], True, unpack_ms),
                "power": ("Channel", "power, dBm", -100, -40, [-90, -70, -50], False, unpack_dbm),
            }[kind]
            label(title, y + 12)
            label(units, y + 25, size=9)
            grid(y, h, marks, low, high, "", log)
            for each in bands:
                values = read(each["timeline"][kind])
                if any(v is not None for v in values):
                    line(values, low, high, y, h, BAND_COLOURS.get(each["band"], (0.3, 0.3, 0.3)), log)
        else:
            found = band["timeline"]["spectrum"]
            label("Spectrum", y + 12)
            label(band["band"], y + 25, size=9)
            for text, ty in ((f"{found['high']:.0f}", y + 9), (f"{found['low']:.0f} MHz", y + h - 1)):
                cr.set_font_size(9)
                label(text, ty, size=9, x=left - 6 - cr.text_extents(text).x_advance)
            values = unpack_dbm(found["avg"])
            levels = np.array([np.nan if v is None else v for v in values], dtype=float)
            levels = levels.reshape(columns, found["bins"]).T[::-1]     # high frequencies on top
            share = np.clip((levels - WATERFALL_LOW) / (WATERFALL_HIGH - WATERFALL_LOW), 0, 1)
            stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, columns)
            pixels = np.zeros((found["bins"], stride // 4, 4), dtype=np.uint8)
            for channel, bgr in enumerate((2, 1, 0)):
                plane = np.interp(np.nan_to_num(share, nan=0.0), WATERFALL_STOPS,
                                  WATERFALL_COLOURS[:, bgr])
                plane[np.isnan(levels)] = 235          # nothing swept: the page's light grey
                pixels[:, :columns, channel] = plane.astype(np.uint8)
            picture = cairo.ImageSurface.create_for_data(memoryview(pixels), cairo.FORMAT_RGB24,
                                                         columns, found["bins"], stride)
            cr.save()
            cr.translate(left, y)
            cr.scale(col_w, h / found["bins"])
            cr.set_source_surface(picture, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            cr.restore()

    # The dropouts: shaded through the traces, and edged on the waterfalls so their colours
    # (the quieter spectrum while the access point is off) stay true
    plot_top, plot_bottom = legend_h, top - gap
    traces_bottom = next((y - gap for kind, _b, y, _h in frames if kind == "spectrum"), plot_bottom)
    for each in bands:
        for incident in each["incidents"]:
            a = x_of((incident["start"] - t0) / spp)
            b = x_of(((incident["end"] or watch["end"]) - t0) / spp)
            cr.set_source_rgba(0.86, 0.15, 0.15, 0.16)
            cr.rectangle(a, plot_top, max(2.0, b - a), traces_bottom - plot_top)
            cr.fill()
            cr.set_source_rgba(0.86, 0.15, 0.15, 0.9)
            cr.set_line_width(1.5)
            for edge in (a, max(a + 2, b)):
                cr.move_to(edge, traces_bottom)
                cr.line_to(edge, plot_bottom)
            cr.stroke()

    # Clock times under it all, a tick every few minutes or hours as the width allows
    every = next((t for t in TICKS if t / spp * col_w >= 80), TICKS[-1])
    first = math.ceil(t0 / every) * every
    for tick in np.arange(first, t0 + columns * spp, every):
        px = round(x_of((tick - t0) / spp)) + 0.5
        cr.set_source_rgb(0.80, 0.82, 0.85)
        cr.move_to(px, plot_top)
        cr.line_to(px, plot_bottom + 4)
        cr.stroke()
        stamp = time.strftime("%H:%M" if every < 86400 else "%d %b", time.localtime(tick))
        label(stamp, plot_bottom + 16, size=10, x=px - 14)
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


def _incident(incident: outage.Incident) -> dict:
    found = incident.diagnosis or {}
    return {"start": incident.start, "end": incident.end, "seconds": incident.seconds,
            "what": outage.kind_name(incident), "cause": found.get("title", ""),
            "confidence": found.get("confidence", ""), "why": list(found.get("why", [])),
            "do": list(found.get("do", [])), "reason": outage.reason_text(incident.reason),
            "adapter_lost": incident.adapter_lost}


def _opening(path: Path) -> tuple[dict, str]:
    """A log's start record and the band it watched, from its first few records."""
    start, band = {}, ""
    for _number, record in zip(range(5), outage.read_records(path)):
        if record.get("type") == "start":
            start = record
        elif record.get("type") == "target":
            band = _band_of(record.get("freq"))
    return start, band


def partners(path: Path) -> list[Path]:
    """
    The logs of one watch: this one, and for each other band, the log of the same network
    started closest to it (within PARTNER_SECONDS) in a folder beside it, since both bands of
    one access point are watched at once.
    """
    path = Path(path)
    first, mine = _opening(path)
    closest: dict[str, tuple[float, Path]] = {}
    for each in sorted(path.parent.parent.iterdir()):
        other = outage.session_file(each) if each.is_dir() else None
        if other is None or other.parent == path.parent:
            continue
        try:
            start, band = _opening(other)
        except OSError:
            continue
        if (band and band != mine and start.get("ssid") == first.get("ssid")
                and start.get("t") and first.get("t")):
            gap = abs(start["t"] - first["t"])
            if gap <= PARTNER_SECONDS and gap < closest.get(band, (PARTNER_SECONDS + 1, None))[0]:
                closest[band] = (gap, other)
    return [path] + [found for _gap, found in closest.values()]


def summary(paths: list[Path]) -> dict:
    """
    A watch of one network, from its logs (one per band watched), with each band's timeline
    on one shared axis (see _timeline): what the report draws, small enough to keep in a survey.
    """
    bands = sorted((band_summary(p) for p in paths), key=lambda b: b["band"])
    start = min((b["start"] for b in bands), default=time.time())
    end = max((b["end"] for b in bands), default=time.time())
    spp = max(TIMELINE_SECONDS, (end - start) / MAX_COLUMNS)
    columns = max(1, math.ceil((end - start) / spp))
    for band in bands:
        band["timeline"] = _timeline(band.pop("_raw"), start, spp, columns)
    return {"ssid": bands[0]["ssid"] if bands else "", "start": start, "end": end,
            "timeline": {"t0": start, "spp": spp, "n": columns}, "bands": bands}


def earlier(base: Path | None = None, skip: set[str] | None = None, limit: int = 40) -> list[dict]:
    """
    The watches saved under ``base`` (the monitor folder), newest first, both bands of one
    watch together: each a summary with its "logs". ``skip`` holds folders still being written.
    """
    base = Path(base or outage.folder())
    skip = skip or set()
    seen: set[str] = set()
    found = []
    for folder in sorted((f for f in base.iterdir() if f.is_dir()), reverse=True):
        if str(folder) in seen or str(folder) in skip:
            continue
        log = outage.session_file(folder)
        if log is None:
            continue
        try:
            logs = partners(log)
            seen.update(str(p.parent) for p in logs)
            if any(str(p.parent) in skip for p in logs):
                continue
            watch = summary(logs)
        except (OSError, ValueError, KeyError):
            continue
        watch["logs"] = [str(p) for p in logs]
        found.append(watch)
        if len(found) >= limit:
            break
    return found


# ---------- in words ----------

def _when(t: float | None) -> str:
    return time.strftime("%d %b %Y %H:%M:%S", time.localtime(t)) if t else ""


def _figure(value, form: str) -> str:
    return "—" if value is None else form.format(value)


def availability_class(share: float | None) -> str:
    """Red or amber only for what users would notice: dropouts adding up to minutes a day."""
    if share is None:
        return "fair"
    return "good" if share >= 0.999 else "fair" if share >= 0.99 else "poor"


def headline(watch: dict) -> str:
    """One line for a map label or a list: the network, how long, how it went."""
    count = sum(len(b["incidents"]) for b in watch["bands"])
    span = outage.duration(watch["end"] - watch["start"])
    worst = min((b["availability"] for b in watch["bands"] if b["availability"] is not None),
                default=None)
    return (f"{watch['ssid'] or '(hidden)'}: watched {span}, "
            + (f"{count} dropout{'s' if count != 1 else ''}" if count else "no dropouts")
            + (f", up {worst * 100:.2f}%" if worst is not None else ""))


def section(watch: dict, number: int | None = None, where: str = "") -> str:
    """The watch as HTML, for its own report or the survey's (where it was watched from)."""
    title = f"Watch {number}: " if number else ""
    html = [f"<h3 id=\"watch-{number or 0}\">{title}{escape(watch['ssid'] or '(hidden)')}</h3>",
            f"<p class=\"muted\">{_when(watch['start'])} to {_when(watch['end'])} "
            f"({outage.duration(watch['end'] - watch['start'])})"
            + (f" · {escape(where)}" if where else "") + "</p>",
            "<table><tr><th>Radio</th><th class=\"num\">Up</th><th class=\"num\">Dropouts</th>"
            "<th class=\"num\">Signal</th><th class=\"num\">Gateway</th>"
            "<th class=\"num\">Retries</th><th class=\"num\">Clients on the AP</th></tr>"]
    for band in watch["bands"]:
        share = band["availability"]
        html.append(
            f"<tr><td>{escape(band['band'] or '?')} · {escape(band['bssid'])}"
            + ("" if band["monitor"] else " <span class=\"muted\">(no monitor adapter)</span>")
            + "</td>"
            f"<td class=\"num\"><span class=\"q q-{availability_class(share)}\">"
            f"{_figure(share, '{:.2%}')}</span></td>"
            f"<td class=\"num\">{len(band['incidents'])}</td>"
            f"<td class=\"num\">{_figure(band['signal'], '{:.0f} dBm')}</td>"
            f"<td class=\"num\">{_figure(band['gateway_ms'], '{:.0f} ms')}</td>"
            f"<td class=\"num\">{_figure(band['retries'], '{:.0%}')}</td>"
            f"<td class=\"num\">{_figure(band['clients'], '{:.0f}')}</td></tr>")
    html.append("</table>")
    picture = timeline_png(watch)
    if picture:
        import base64

        spp = watch["timeline"]["spp"]
        html.append(f"<p class=\"muted\">Everything on one time axis, {spp:.0f} s a column: "
                    "status (green up, amber struggling, red down), the signal, the gateway's "
                    "reply time, the channel's power and its spectrum from the analyzer (quiet "
                    "blue to strong red). Dropouts are shaded red through all of them, so a "
                    "quieter spectrum while one lasts shows the access point stopped "
                    "transmitting.</p>")
        html.append("<div class=\"timeline\"><img alt=\"The watch's timeline\" "
                    f"src=\"data:image/png;base64,{base64.b64encode(picture).decode()}\"></div>")
    notes = [note for band in watch["bands"] for note in band["patterns"]]
    if notes:
        html.append("<p><b>Pattern:</b> " + escape(" ".join(notes)) + "</p>")
    incidents = sorted(((band["band"], i) for band in watch["bands"] for i in band["incidents"]),
                       key=lambda item: item[1]["start"])
    if incidents:
        html.append("<table><tr><th>When</th><th class=\"num\">For</th><th>Radio</th><th>What</th>"
                    "<th>Most likely cause</th><th>What to do</th></tr>")
        for band, incident in incidents:
            cause = escape(incident["cause"] or "Not clear")
            if incident["confidence"]:
                cause += f" <span class=\"muted\">({escape(str(incident['confidence']))})</span>"
            if incident["why"]:
                cause += "<div class=\"muted\">" + escape("; ".join(incident["why"][:3])) + "</div>"
            html.append(
                f"<tr><td>{_when(incident['start'])}</td>"
                f"<td class=\"num\">{escape(outage.duration(incident['seconds']) or 'ongoing')}</td>"
                f"<td>{escape(band)}</td><td>{escape(incident['what'])}"
                + (f"<div class=\"muted\">{escape(incident['reason'])}</div>" if incident["reason"] else "")
                + f"</td><td>{cause}</td><td>{escape(' '.join(incident['do'][:2]))}</td></tr>")
        html.append("</table>")
    else:
        html.append("<p>No dropouts while it was watched.</p>")
    return "\n".join(html)


STYLE = """
body { font: 15px/1.55 Cantarell, "Segoe UI", system-ui, sans-serif; color: #1E293B;
       margin: 0 auto; padding: 32px 28px 60px; max-width: 1100px; background: #FFFFFF; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #E2E8F0; vertical-align: top; }
th { font-size: 13px; color: #475569; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: #64748B; font-size: 13px; }
.q { display: inline-block; padding: 0 7px; border-radius: 4px; font-weight: 600; }
.q-good { background: #DCFCE7; color: #14532D; }
.q-fair { background: #FEF3C7; color: #713F12; }
.q-poor { background: #FEE2E2; color: #7F1D1D; }
.timeline { overflow-x: auto; margin: 8px 0 16px; }
.timeline img { display: block; max-width: none; }
"""


def build_html(watch: dict, version: str = "") -> str:
    """A watch as a report of its own."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Watch: {escape(watch['ssid'] or '(hidden)')}</title><style>{STYLE}</style>"
            f"</head><body><h1>Watching {escape(watch['ssid'] or '(hidden)')}</h1>"
            f"<p>{escape(headline(watch))}.</p>"
            + section(watch)
            + "<p class=\"muted\">Up is the share of the watched time the network was usable. "
              "Each dropout's cause is the most likely of those the evidence fits; its capture "
              f"and the full log are in {escape(', '.join(b['folder'] for b in watch['bands']))}."
              + (f" Wi-Fi Surveyor {escape(version)}." if version else "") + "</p>"
            + "</body></html>")
