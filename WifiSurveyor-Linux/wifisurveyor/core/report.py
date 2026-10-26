"""
The survey as one HTML file: the map and each point's findings and spectrum are embedded, so
the report can be emailed or archived with nothing else alongside it.
"""

from __future__ import annotations

import base64
import re
import statistics
import time
from collections import Counter
from collections.abc import Callable
from html import escape
from pathlib import Path

from . import advice, analysis, hidden, history as site_history, library, overview, planning, rfplan
from .colours import hex_colour, quality_colour, signal_quality, ssid_colour
from ..rf import bands as rfbands
from ..rf import cellular, iq

BAND_NAMES = {"2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}
QUALITY_WORDS = {"good": "Good", "fair": "Could be better", "poor": "Needs work"}

STYLE = """
:root { color-scheme: light; --good: #16A34A; --fair: #EAB308; --poor: #DC2626; }
body { font: 15px/1.55 Cantarell, "Segoe UI", system-ui, sans-serif; color: #1E293B;
       margin: 0 auto; padding: 32px 28px 60px; max-width: 1100px; background: #FFFFFF; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 36px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #D9DEE5; }
h3 { font-size: 17px; margin: 0; display: flex; align-items: center; gap: 10px; }
h4 { font-size: 14px; margin: 14px 0 4px; text-transform: uppercase; letter-spacing: .04em;
     color: #5B6778; }
a { color: #1D4ED8; }
.muted { color: #5B6778; }
img { max-width: 100%; border: 1px solid #D9DEE5; border-radius: 6px; display: block; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 14px; }
th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid #E5E9EF; vertical-align: top; }
th { background: #F3F5F8; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.swatch { display: inline-block; width: 11px; height: 11px; border-radius: 2px;
          margin-right: 7px; vertical-align: middle; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 6px;
       vertical-align: -1px; box-shadow: inset 0 0 0 1px rgba(0,0,0,.35); }
.ssid { white-space: nowrap; font-weight: 600; }
.q { display: inline-block; padding: 0 7px; border-radius: 4px; font-weight: 600;
     font-variant-numeric: tabular-nums; }
.q-good { background: #DCFCE7; color: #14532D; }
.q-fair { background: #FEF3C7; color: #713F12; }
.q-poor { background: #FEE2E2; color: #7F1D1D; }
.sev { display: inline-block; min-width: 58px; text-align: center; padding: 0 6px; border-radius: 4px;
       font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
.sev-High { background: var(--poor); color: #fff; }
.sev-Medium { background: var(--fair); color: #422006; }
.sev-Low { background: #CBD5E1; color: #1E293B; }
.trends { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px;
          margin: 12px 0; }
.trend { border: 1px solid #D9DEE5; border-radius: 8px; padding: 8px 10px 4px; }
.trend b { font-size: 13px; display: block; }
.trend svg { width: 100%; height: auto; display: block; }
.then { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.then figure { margin: 0; }
.then figcaption { font-size: 13px; color: #5B6778; margin-top: 4px; }
tr.now td { font-weight: 600; background: #F8FAFC; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 14px 0; }
.card { flex: 1 1 150px; border: 1px solid #D9DEE5; border-radius: 8px; padding: 10px 14px;
        border-top: 5px solid var(--c, #CBD5E1); }
.card b { font-size: 24px; display: block; }
.mapbox { position: relative; display: inline-block; max-width: 100%; }
.mapbox img { width: 100%; }
.pin { position: absolute; transform: translate(-50%, -50%); min-width: 22px; height: 22px;
       padding: 0 4px; box-sizing: border-box; border-radius: 11px; border: 2px solid #fff;
       box-shadow: 0 1px 3px rgba(0,0,0,.5); color: #fff; font: 700 12px/18px system-ui, sans-serif;
       text-align: center; text-decoration: none; }
.pin.fair { color: #422006; }
.pin:hover { transform: translate(-50%, -50%) scale(1.25); z-index: 2; }
.scalebar { position: absolute; left: 12px; bottom: 12px; background: rgba(255,255,255,.9);
            padding: 2px 6px 4px; border-radius: 4px; font-size: 12px; }
.scalebar div { height: 6px; border: 2px solid #1E293B; border-top: 0; }
.point { border: 1px solid #D9DEE5; border-radius: 8px; padding: 16px 18px; margin: 18px 0;
         border-left: 6px solid var(--c, #CBD5E1); }
.point-head { display: flex; gap: 18px; flex-wrap: wrap; align-items: flex-start; }
.point-head .text { flex: 1 1 380px; }
.badge { display: inline-flex; align-items: center; justify-content: center; min-width: 30px;
         height: 30px; border-radius: 15px; color: #fff; font-weight: 700; padding: 0 6px;
         box-sizing: border-box; }
.badge.fair { color: #422006; }
.locator { position: relative; width: 240px; flex: 0 0 240px; border: 1px solid #D9DEE5;
           border-radius: 6px; background-size: 100% 100%; background-repeat: no-repeat; }
.locator span { position: absolute; width: 18px; height: 18px; border-radius: 50%;
                transform: translate(-50%, -50%); border: 3px solid #fff;
                box-shadow: 0 0 0 3px #1D4ED8, 0 1px 4px rgba(0,0,0,.6); }
.facts { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 8px 0; font-size: 14px; }
.facts span b { font-weight: 600; }
ul.todo { list-style: none; padding: 0; margin: 6px 0; }
ul.todo li { padding: 6px 0; border-bottom: 1px solid #EEF1F5; }
ul.todo .fix { color: #334155; }
ul.todo .fix::before { content: "→ "; color: #1D4ED8; font-weight: 700; }
.chip { display: inline-block; margin: 1px 2px; padding: 0 6px; border-radius: 9px; font-size: 12px;
        font-weight: 700; text-decoration: none; color: #fff; }
.chip.fair { color: #422006; }
.verdict { margin: 6px 0 10px; }
.note { background: #F3F5F8; border-left: 3px solid #1D4ED8; padding: 8px 12px; margin: 10px 0;
        font-size: 14px; }
.top { font-size: 13px; float: right; }
h3.sub { margin: 14px 0 6px; }
ol.top-actions { padding-left: 22px; }
ol.top-actions li { margin: 8px 0; }
.benefit { color: #14532D; font-size: 14px; }
.benefit::before { content: "Users will notice: "; font-weight: 600; }
.why { color: #475569; font-size: 14px; }
.where { margin-top: 2px; }
.theme { border: 1px solid #D9DEE5; border-left: 6px solid var(--c, #CBD5E1); border-radius: 8px;
         padding: 12px 16px; margin: 14px 0; break-inside: avoid; }
.theme h3 .q { font-size: 12px; }
ul.recs { list-style: none; padding: 0; margin: 4px 0; }
ul.recs li { padding: 8px 0; border-top: 1px solid #EEF1F5; }
header.cover { display: flex; justify-content: space-between; align-items: center; gap: 20px;
               margin-bottom: 18px; font-size: 14px; }
header.cover img.logo { max-height: 70px; max-width: 260px; border: 0; }
header.cover div { text-align: right; margin-left: auto; }
table.whom { width: auto; }
table.whom th { background: none; color: #5B6778; font-weight: 600; padding-right: 24px; }
ul.checklist { list-style: none; padding: 0; }
ul.checklist li { padding: 5px 0; border-bottom: 1px solid #EEF1F5; }
ul.checklist input { width: 16px; height: 16px; vertical-align: -2px; }
details.sweeps { border: 1px solid #D9DEE5; border-radius: 8px; margin: 14px 0; }
details.sweeps > summary { cursor: pointer; padding: 10px 14px; font-weight: 600; }
details.sweeps > summary .muted { font-weight: 400; }
details.sweeps[open] > summary { border-bottom: 1px solid #EEF1F5; }
details.sweeps figure { margin: 10px 14px 16px; }
details.sweeps figcaption { font-size: 13px; color: #475569; margin-bottom: 4px; }
details.sweeps .inner { padding: 0 12px 4px; }
details.fold > summary { list-style: none; cursor: pointer; }
details.fold > summary::-webkit-details-marker { display: none; }
details.fold > summary h2::before, details.fold > summary h3::before {
  content: ""; width: 7px; height: 7px; flex: 0 0 auto; border: solid #5B6778;
  border-width: 0 2px 2px 0; transform: rotate(-45deg); transition: transform .15s;
  margin-right: 4px; }
details.fold[open] > summary h2::before, details.fold[open] > summary h3::before {
  transform: rotate(45deg); }
details.section > summary h2 { display: flex; align-items: center; gap: 8px; }
details.section:not([open]) > summary h2 { margin-bottom: 0; }
details.point-fold:not([open]) > summary h3 { margin-bottom: -4px; }
details.point-fold[open] > summary { margin-bottom: 6px; }
.fold-bar { position: sticky; top: 0; z-index: 5; display: flex; gap: 8px; justify-content: flex-end;
            padding: 8px 0; background: #FFFFFF; border-bottom: 1px solid #EEF1F5; }
.fold-bar button { font: inherit; font-size: 14px; padding: 6px 14px; border-radius: 6px;
                   border: 1px solid #CBD5E1; background: #F3F5F8; color: #1E293B; cursor: pointer;
                   min-height: 36px; }
.fold-bar button:hover { background: #E5E9EF; }
@media print { body { padding: 0; } .point { break-inside: avoid; } a { color: inherit; }
               details.sweeps > summary { list-style: none; } .fold-bar { display: none; }
               details.fold > summary h2::before, details.fold > summary h3::before { display: none; } }
"""
FOLD_BAR = ('<div class="fold-bar"><button type="button" data-fold="open">Expand all</button>'
            '<button type="button" data-fold="close">Collapse all</button></div>')
# Marks where the folded sections end (see _fold_sections)
SECTIONS_END = "<!-- end of sections -->"
# Expand all and Collapse all; and a link to something folded away (a pin to its point, a
# chip to a point) opens the folds it's in on the way
FOLDING = """<script>
(() => {
  const fold = open => document.querySelectorAll("details").forEach(d => d.open = open);
  document.querySelectorAll("[data-fold]").forEach(button =>
    button.addEventListener("click", () => fold(button.dataset.fold === "open")));
  const reveal = () => {
    const id = decodeURIComponent(location.hash.slice(1));
    const target = id && document.getElementById(id);
    if (!target) return;
    for (let d = target.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
    const own = target.querySelector(":scope > details");
    if (own) own.open = true;
    target.scrollIntoView();
  };
  addEventListener("hashchange", reveal);
  document.addEventListener("click", event => {
    const link = event.target.closest('a[href^="#"]');
    if (link && link.getAttribute("href") === location.hash) setTimeout(reveal);
  });
  reveal();
})();
</script>"""
# Printing shows every collapsed section's pictures, then puts them back as they were
PRINT_OPENS = """<script>
(() => {
  let closed = [];
  addEventListener("beforeprint", () => {
    closed = [...document.querySelectorAll("details:not([open])")];
    closed.forEach(d => d.open = true);
  });
  addEventListener("afterprint", () => closed.forEach(d => d.open = false));
})();
</script>"""


def _png(data: bytes, alt: str) -> str:
    return f'<img alt="{escape(alt)}" src="data:image/png;base64,{base64.b64encode(data).decode()}">'


def _colour(rgb) -> str:
    return hex_colour(rgb)


def _ssid(name: str | None) -> str:
    """A network's name with its colour dot, the same colour everywhere in the report."""
    return (f'<span class="ssid"><span class="dot" style="background:{hex_colour(ssid_colour(name))}">'
            f'</span>{escape(name or "(hidden)")}</span>')


def _signal(rssi, target: float, estimate: bool = False, unit: str = " dBm") -> str:
    if rssi is None:
        return '<span class="q q-poor">not heard</span>'
    return (f'<span class="q q-{signal_quality(rssi, target)}">'
            f'{"~" if estimate else ""}{rssi:.0f}{unit}</span>')


def _graded(value, good: float, fair: float, higher_is_better: bool, text: str) -> str:
    """A value in red, amber or green against two thresholds."""
    if higher_is_better:
        grade = "good" if value >= good else "fair" if value >= fair else "poor"
    else:
        grade = "good" if value <= good else "fair" if value <= fair else "poor"
    return f'<span class="q q-{grade}">{escape(text)}</span>'


def _chip(number: int, quality: str, link: bool = True) -> str:
    """A point's number in its quality colour, linking to its section when the report has one."""
    style = f'class="chip {quality}" style="background:{_colour(quality_colour(quality))}"'
    if not link:
        return f'<span {style}>{number}</span>'
    return f'<a {style} href="#point-{number}">{number}</a>'


def _overview(themes: list[overview.Theme], chip: Callable[[int], str]) -> str:
    """The recommendations, the most useful first, then theme by theme."""
    html = ["<h2 id=\"overview\">Overview and recommendations</h2>"]
    top = overview.top_actions(themes)
    if top:
        html.append("<h3 class=\"sub\">Most useful changes</h3><ol class=\"top-actions\">")
        for theme, item in top:
            where = ("".join(chip(n) for n in item.points)) if item.points else ""
            html.append(f"<li><span class=\"sev sev-{item.priority}\">{item.priority}</span> "
                        f"<span class=\"muted\">{escape(theme.title)}:</span> "
                        f"<b>{escape(item.action)}</b> {where}"
                        f"<div class=\"benefit\">{escape(item.benefit)}</div></li>")
        html.append("</ol>")
    else:
        html.append("<p>No change stands out; the themes below list what would add margin.</p>")
    for theme in themes:
        colour = _colour(quality_colour(theme.status))
        html.append(f"<section class=\"theme\" style=\"--c:{colour}\">"
                    f"<h3><span class=\"q q-{theme.status}\">{THEME_WORDS[theme.status]}</span>"
                    f"{escape(theme.title)}</h3>"
                    f"<p class=\"muted\">{escape(theme.aim)}. {escape(theme.summary)}</p>")
        if theme.items:
            html.append("<ul class=\"recs\">")
            for item in theme.items:
                where = ("<div class=\"where\">Points: " + "".join(chip(n) for n in item.points)
                         + "</div>") if item.points else ""
                html.append(f"<li><span class=\"sev sev-{item.priority}\">{item.priority}</span> "
                            f"<b>{escape(item.action)}</b>"
                            f"<div class=\"why\">{escape(item.why)}</div>"
                            f"<div class=\"benefit\">{escape(item.benefit)}</div>{where}</li>")
            html.append("</ul>")
        html.append("</section>")
    return "\n".join(html)


def _networks_table(point, target: float) -> str:
    aps = analysis.access_points(point.networks)
    if not aps:
        return '<p class="muted">No access points heard.</p>'
    rows = []
    for ap in aps:
        width = f"{ap['width']} MHz" if ap["width"] else ""
        rows.append(
            f"<tr><td>{_ssid(ap['ssid'])}</td>"
            f"<td>{escape(BAND_NAMES.get(ap['band'], ap['band']))}</td>"
            f"<td class=\"num\">{escape(str(ap['channel'] or ''))}</td>"
            f"<td class=\"num\">{width}</td>"
            f"<td class=\"num\">{_signal(ap['rssi'], target, ap['estimate'])}</td>"
            f"<td>{escape(ap['vendor'])}</td><td>{escape(ap['bssid'])}</td></tr>")
    return ("<table><tr><th>Network</th><th>Band</th><th class=\"num\">Channel</th>"
            "<th class=\"num\">Width</th><th class=\"num\">Signal</th><th>Manufacturer</th>"
            "<th>BSSID</th></tr>" + "".join(rows) + "</table>")


def _dwell_table(point) -> str:
    dwells = (point.spectrum or {}).get("dwells") or []
    if not dwells:
        return ""

    def dbm(value) -> str:
        return "" if value is None else f"{value:.0f} dBm"

    def busy(value) -> str:
        return "" if value is None else _graded(value * 100, 30, 50, False, f"{value * 100:.0f}%")

    several = len({d.get("analyzer") for d in dwells}) > 1
    rows = "".join(
        f"<tr><td class=\"num\">{d['center_mhz']:.0f} MHz</td>"
        f"<td class=\"num\">{d['width_mhz']:.0f} MHz</td>"
        f"<td class=\"num\">{dbm(d.get('active_dbm'))}</td>"
        f"<td class=\"num\">{busy(d.get('airtime'))}</td>"
        f"<td class=\"num\">{dbm(d.get('noise_20mhz'))}</td>"
        f"<td class=\"num\">{d.get('sweeps', '')}</td>"
        + (f"<td>{escape(d.get('analyzer') or '')}</td>" if several else "") + "</tr>"
        for d in dwells)
    return ("<h4>Channels watched closely</h4>"
            "<table><tr><th class=\"num\">Centre</th><th class=\"num\">Width</th>"
            "<th class=\"num\">While transmitting</th><th class=\"num\">Busy</th>"
            "<th class=\"num\">Noise per 20 MHz</th><th class=\"num\">Sweeps</th>"
            + ("<th>Analyzer</th>" if several else "") + "</tr>"
            + rows + "</table>")


def _facts(point, ctx, ssid, band) -> str:
    """The key numbers for one spot, each coloured by how good it is."""
    profile = ctx.profile
    best = point.best(ssid, None, band)
    parts = []
    if best is not None:
        parts.append(f"<span>Serving: {_ssid(best['ssid'])} "
                     f"{escape(BAND_NAMES.get(best.get('band'), ''))} {escape(advice.channel_name(best))}</span>")
        parts.append(f"<span><b>Signal</b> {_signal(best['rssi'], profile.primary)}</span>")
        floor = analysis.noise_floor(point, best.get("band"))
        if floor is not None:
            snr = best["rssi"] - floor
            parts.append(f"<span><b>SNR</b> {_graded(snr, profile.snr + 5, profile.snr, True, f'{snr:.0f} dB')}</span>")
            parts.append(f"<span><b>Noise</b> {_graded(floor, -90, -85, False, f'{floor:.0f} dBm')}</span>")
    second = rfplan.nth_strongest(point, 2, ssid or (best or {}).get("ssid"), band)
    parts.append(f"<span><b>2nd AP</b> {_signal(second, profile.secondary or -75)}</span>")
    others = rfplan.contenders(point, ssid or (best or {}).get("ssid"), band)
    if others is not None:
        parts.append(f"<span><b>Sharing the channel</b> "
                     f"{_graded(len(others), 0, 2, False, str(len(others)))}</span>")
    result = point.connectivity or {}
    latency, loss = result.get("latency_ms"), result.get("loss")
    if latency is not None:
        parts.append(f"<span><b>Latency</b> {_graded(latency, 10, 30, False, f'{latency:.0f} ms')}</span>")
    if loss is not None:
        parts.append(f"<span><b>Loss</b> {_graded(loss, 0, 2, False, f'{loss:.0f}%')}</span>")
    for speed in [point.throughput or {}, *point.throughputs]:
        if speed.get("down_mbps") is None:
            continue
        down = speed["down_mbps"]
        text = f"{down:.0f}↓" + (f" {speed['up_mbps']:.0f}↑" if speed.get("up_mbps") is not None else "") + " Mbps"
        label = f"Throughput, {escape(speed['ssid'])}" if speed.get("ssid") else "Throughput"
        parts.append(f"<span><b>{label}</b> {_graded(down, 100, 25, True, text)}</span>")
    parts.append(f"<span><b>Access points heard</b> {len(analysis.access_points(point.networks))}</span>")
    busiest = analysis.band_activity(point)
    if busiest is not None:
        key = max(point.bands, key=lambda k: rfbands.activity(point.bands[k]) or 0.0)
        name = rfbands.BY_KEY[key].name if key in rfbands.BY_KEY else key
        parts.append(f"<span><b>Busiest other band</b> {_graded(busiest, rfbands.QUIET_DB, rfbands.NOISY_DB, False, f'{busiest:.0f} dB over floor')} "
                     f"<span class=\"muted\">{escape(name)}</span></span>")
    phone = cellular.strongest(point.cellular)
    if phone is not None:
        parts.append(f"<span><b>Cellular</b> {_graded(phone, cellular.GOOD_DBM, cellular.FAIR_DBM, True, f'{phone:.0f} dBm/5 MHz')}</span>")
    return f'<div class="facts">{"".join(parts)}</div>'


def _todo(found: list[advice.Improvement]) -> str:
    items = "".join(
        f'<li><span class="sev sev-{f.severity}">{f.severity}</span> <b>{escape(f.title)}.</b> '
        f'{escape(f.issue)}<br><span class="fix">{escape(f.fix)}</span></li>' for f in found)
    return f'<ul class="todo">{items}</ul>'


def _margins(point, ctx, ssid, band) -> str:
    """For a spot with nothing to improve: by how much it clears each target."""
    best = point.best(ssid, None, band)
    if best is None:
        return ""
    parts = [f"signal {best['rssi'] - ctx.profile.primary:+.0f} dB over the target"]
    floor = analysis.noise_floor(point, best.get("band"))
    if floor is not None:
        parts.append(f"SNR {best['rssi'] - floor - ctx.profile.snr:+.0f} dB over")
    return ("<p>Nothing found to improve for the " + escape(ctx.profile.name)
            + " requirement. Margins: " + escape(", ".join(parts)) + ".</p>")


def build_html(project, map_png: bytes | None,
               spectrum_png: Callable[[object], bytes | None], profile: str = "data",
               ssid: str | None = None, band: str = "All", allow_dfs: bool = False,
               overview_only: bool = False, extra_maps: dict | None = None,
               details: dict | None = None, version: str = "",
               history: dict | None = None,
               range_png: Callable[[dict], bytes | None] | None = None,
               band_png: Callable[[object, str], bytes | None] | None = None) -> str:
    """
    ``extra_maps`` holds the other maps drawn by the survey page and any comparison with an
    earlier survey (see ``SurveyView.report_maps``); ``details`` who the report is from and for.

    ``overview_only`` gives the short version: the recommendations, the map and the networks,
    without each point's section.

    ``spectrum_png`` gives a point's spectrum picture: the one kept when it was measured, or
    one drawn now from its sweeps for points measured before pictures were kept.
    ``range_png`` draws one of the other ranges a point swept (Point.ranges);
    ``band_png`` one Wi-Fi band ("2.4", "5", "6") of a point's sweeps, each folded on its
    own, falling back to the one ``spectrum_png`` picture when it can't. The
    requirement, network and band are the ones chosen on the survey page. ``map_png`` is the
    whole plan, drawn without point numbers: the report pins its own, which link to each point.
    """
    ctx = analysis.Context(project, profile=profile)
    target = ctx.profile.primary
    points = project.points
    verdicts = [analysis.diagnose(point, ctx, ssid, band=band) for point in points]
    judged = [analysis.requirement(point, ctx, ssid, band) for point in points]
    sites = analysis.ap_sites(project, ssid, band)
    todo = [advice.improvements(point, ctx, ssid, band, sites) for point in points]
    grades = [advice.quality(found) for found in todo]
    themes = overview.build(project, ctx, ssid, band, todo, sites, allow_dfs)
    links = not overview_only

    def chip(number: int) -> str:
        return _chip(number, grades[number - 1], links)
    labels = {key: (label, colour) for key, label, colour in analysis.VERDICTS}
    with_spectrum = sum(1 for point in points if point.spectrum)
    width, height = max(1, project.width), max(1, project.height)

    def pin_style(point) -> str:
        return f"left:{point.x / width * 100:.2f}%;top:{point.y / height * 100:.2f}%"

    def map_block(png: bytes, alt: str) -> str:
        """A map picture with a numbered, coloured pin on each point."""
        parts = ["<div class=\"mapbox\">" + _png(png, alt)]
        for number, (point, grade) in enumerate(zip(points, grades), start=1):
            tag, target_attr = ("a", f" href=\"#point-{number}\"") if links else ("span", "")
            parts.append(f"<{tag} class=\"pin {grade}\"{target_attr} "
                         f"title=\"Point {number}: {QUALITY_WORDS[grade]}\" "
                         f"style=\"{pin_style(point)};background:{_colour(quality_colour(grade))}\">"
                         f"{number}</{tag}>")
        parts.append(_scale_bar(project))
        parts.append("</div>")
        return "".join(parts)

    map_css = ""
    if map_png:
        map_css = (".locator { background-image: url(data:image/png;base64,"
                   + base64.b64encode(map_png).decode() + "); }")

    scale = ""
    if project.meters_per_pixel:
        scale = (f" · plan {project.feet(project.width):.0f} × {project.feet(project.height):.0f} ft")
    html = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{escape(project.name)}: Wi-Fi survey{' overview' if overview_only else ''}</title><style>{STYLE}{map_css}</style></head>"
        "<body id=\"top\">",
        _cover(details or {}),
        f"<h1>{escape(project.name)}</h1>",
        f"<p class=\"muted\">{len(points)} measurement points · {with_spectrum} with "
        f"spectrum · judged for {escape(ssid or 'the strongest network at each point')}"
        f"{'' if band == 'All' else f' on {band} GHz'} against the {escape(ctx.profile.name)} "
        f"requirement"
        + (f", allowing {ctx.allowance:g} dB for phones" if ctx.allowance else "")
        + f"{scale} · {time.strftime('%d %B %Y, %H:%M')}</p>",
    ]
    html.append(FOLD_BAR)
    html.append(_for_whom(details or {}))

    # ----- at a glance -----
    counts = Counter(grades)
    counted = [ok for ok, _why in judged if ok is not None]
    html.append("<div class=\"cards\">")
    for grade in ("poor", "fair", "good"):
        html.append(f"<div class=\"card\" style=\"--c:{_colour(quality_colour(grade))}\">"
                    f"<b>{counts.get(grade, 0)}</b>{QUALITY_WORDS[grade]}"
                    f"<div class=\"muted\">{_grade_meaning(grade)}</div></div>")
    if counted:
        passing = sum(counted)
        html.append(f"<div class=\"card\"><b>{passing / len(counted) * 100:.0f}%</b>meet "
                    f"{escape(ctx.profile.name)}<div class=\"muted\">{passing} of {len(counted)} "
                    "points</div></div>")
    html.append("</div>")
    html.append(_area_cards(project, ctx, ssid, band))
    if not project.meters_per_pixel:
        html.append("<p class=\"note\">The floor plan has no scale, so distances, access point "
                    "reach and gaps between measurements aren't in this report. On the survey "
                    "page, use <b>Set scale</b>: tap two spots a known distance apart and enter "
                    "the distance in feet.</p>")

    html.append(_overview(themes, chip))
    html.append(_checklist(themes))

    # ----- the map, with a numbered pin per point -----
    if map_png:
        html.append("<h2 id=\"map\">Map</h2>")
        html.append("<p class=\"muted\">Each numbered pin is a measurement point, coloured by how "
                    "much needs improving there (green good, amber could be better, red needs "
                    "work)." + (" Select a pin to jump to that point's details." if links else "")
                    + "</p>")
        shown = (extra_maps or {}).get("shown")
        if shown:
            html.append(f"<h3 class=\"sub\">{escape(shown)}</h3>")
        html.append(map_block(map_png, "Survey map"))
        html.append("<p class=\"muted\">Rings show each point's spectrum: frequency clockwise "
                    "from the top through 2.4, 5 and 6 GHz, level from the centre out, one dot "
                    "per access point in its network's colour (listed under Networks). Squares "
                    "are access points placed where they really are, circles estimated ones, "
                    "dashed purple diamonds planned ones.</p>")
        for key, title, png in (extra_maps or {}).get("maps", []):
            html.append(f"<h3 class=\"sub\">{escape(title)}</h3>")
            html.append(map_block(png, title))
            html.append(f"<p class=\"muted\">{escape(MAP_CAPTIONS.get(key, ''))}</p>")
    html.append(_planned(project, ctx, ssid, band, sites))
    html.append(_history(history))
    html.append(_comparison(extra_maps or {}, map_block))

    # ----- what to improve, across the survey -----
    grouped: dict[str, dict] = {}
    for number, found in enumerate(todo, start=1):
        for item in found:
            entry = grouped.setdefault(item.title, {"item": item, "points": []})
            if advice.SEVERITY_ORDER[item.severity] < advice.SEVERITY_ORDER[entry["item"].severity]:
                entry["item"] = item
            if number not in entry["points"]:
                entry["points"].append(number)
    if grouped and links:
        html.append("<h2>What to improve, point by point</h2>")
        html.append("<p class=\"muted\">Every issue found, most serious first, with the points "
                    "where it was found. Each point's own section below explains it there.</p>")
        html.append("<table><tr><th>Severity</th><th>Issue</th><th>Points</th>"
                    "<th>What to do</th></tr>")
        ranked = sorted(grouped.values(), key=lambda e: (advice.SEVERITY_ORDER[e["item"].severity],
                                                         -len(e["points"])))
        for entry in ranked:
            item = entry["item"]
            chips = "".join(chip(n) for n in entry["points"])
            html.append(f"<tr><td><span class=\"sev sev-{item.severity}\">{item.severity}</span></td>"
                        f"<td><b>{escape(item.title)}</b></td><td>{chips}</td>"
                        f"<td>{escape(item.fix)}</td></tr>")
        html.append("</table>")

    # ----- the networks, each in its colour -----
    names = sorted({n["ssid"] for p in points for n in p.networks},
                   key=lambda name: (name != ssid, -sum(1 for p in points
                                                        if any(n["ssid"] == name for n in p.networks)),
                                     name))
    if names:
        html.append("<h2>Networks</h2>")
        html.append(f"<p class=\"muted\">Each network keeps its colour throughout: on the map's "
                    f"rings and in every table. Signals are green at {target + 3:.0f} dBm or "
                    f"better, amber down to {target - 5:.0f} dBm, red below.</p>")
        html.append("<table><tr><th>Network</th><th>Bands</th><th class=\"num\">Heard at</th>"
                    "<th class=\"num\">Best</th><th class=\"num\">Median</th>"
                    "<th class=\"num\">Worst</th><th>Security</th></tr>")
        for name in names:
            levels = [p.best(name)["rssi"] for p in points if p.best(name)]
            bands = sorted({n.get("band") for p in points for n in p.networks if n["ssid"] == name},
                           key=lambda b: analysis.BAND_ORDER.get(b, 9))
            security = sorted({n.get("security") or "" for p in points for n in p.networks
                               if n["ssid"] == name} - {""})
            html.append(f"<tr><td>{_ssid(name)}</td>"
                        f"<td>{escape(', '.join(BAND_NAMES.get(b, b) for b in bands))}</td>"
                        f"<td class=\"num\">{len(levels)} of {len(points)}</td>"
                        f"<td class=\"num\">{_signal(max(levels), target)}</td>"
                        f"<td class=\"num\">{_signal(statistics.median_low(levels), target)}</td>"
                        f"<td class=\"num\">{_signal(min(levels), target)}</td>"
                        f"<td>{escape(', '.join(security))}</td></tr>")
        html.append("</table>")

    if overview_only:
        html.append("<p class=\"muted\">The full report adds each point's findings, spectrum "
                    "and access points, the configuration audit and the channel plan.</p>")
        html.append(_method(project, ctx, version))
        html.append("</body></html>")
        return "\n".join(html)

    # ----- every access point: where, what, how far, and whom it serves -----
    html.append(_inventory(project, ctx, sites, chip, ssid, band))

    if verdicts:
        html.append("<h2>What's limiting each spot most</h2><table><tr><th>Main limit</th>"
                    "<th>Points</th></tr>")
        by_key: dict[str, list[int]] = {}
        for number, (key, _why, _fix) in enumerate(verdicts, start=1):
            by_key.setdefault(key, []).append(number)
        for key, numbers in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
            label, colour = labels.get(key, (key, (0.5, 0.5, 0.5)))
            chips = "".join(chip(n) for n in numbers)
            html.append(f"<tr><td><span class=\"swatch\" style=\"background:{_colour(colour)}\">"
                        f"</span>{escape(label)}</td><td>{chips}</td></tr>")
        html.append("</table>")

    if counted:
        passing = sum(counted)
        html.append(f"<h2>{escape(ctx.profile.name)} requirement</h2>")
        html.append(f"<p><b>{passing} of {len(counted)} points ({passing / len(counted) * 100:.0f}%) "
                    f"meet it</b> for {escape(ssid or 'the strongest network at each point')}"
                    f"{'' if band == 'All' else f' on {band} GHz'}. It asks for a signal of "
                    f"{ctx.profile.primary:.0f} dBm or better"
                    + (f", a second access point at {ctx.profile.secondary:.0f} dBm or better"
                       if ctx.profile.secondary is not None else "")
                    + (f", a third at {ctx.profile.third:.0f} dBm" if ctx.profile.third is not None else "")
                    + f", {ctx.profile.snr:.0f} dB signal-to-noise"
                    + (f", at most {ctx.profile.max_contenders} other radios on the channel"
                       if ctx.profile.max_contenders is not None else "")
                    + (f" and the channel busy at most {ctx.profile.max_airtime * 100:.0f}% of the time"
                       if ctx.profile.max_airtime is not None else "") + ".</p>")
        failing = [n for n, (ok, _why) in enumerate(judged, start=1) if ok is False]
        if failing:
            html.append("<p>Failing: " + "".join(chip(n) for n in failing) + "</p>")

    devices = analysis.bt_devices(project)
    other = analysis.bt_other(project)
    if devices or other["fixed"] or other["rotating"]:
        html.append("<h2>Bluetooth devices</h2>")
        html.append("<p class=\"muted\">Bluetooth devices sharing 2.4 GHz. Fixed-address ones "
                    "heard at three or more points are placed on the map; the rest are listed "
                    "below. Anything not expected here is worth a look.</p>")
        html.append("<table><tr><th>Device</th><th>Type</th><th>Manufacturer</th>"
                    "<th>Address</th><th class=\"num\">Strongest</th><th class=\"num\">Points</th></tr>")
        kinds = {"printer": "Printer", "imaging": "Scanner/imaging", "audio": "Audio",
                 "peripheral": "Peripheral", "computer": "Computer", "network": "Network",
                 "phone": "Phone", "wearable": "Wearable", "health": "Health",
                 "unknown": "Bluetooth"}
        for d in devices:
            html.append(f"<tr><td>{escape(d['name'] or '(unnamed)')}</td>"
                        f"<td>{escape(kinds.get(d['kind'], 'Bluetooth'))}</td>"
                        f"<td>{escape(d['vendor'])}</td><td>{escape(d['address'])}</td>"
                        f"<td class=\"num\">{d['best']} dBm</td>"
                        f"<td class=\"num\">{d['points']}</td></tr>")
        for d in other["fixed"]:
            html.append(f"<tr><td>{escape(d.get('name') or '(unnamed)')}</td>"
                        f"<td>{escape(kinds.get(d.get('kind'), 'Bluetooth'))}</td>"
                        f"<td>{escape(d.get('vendor') or '')}</td><td>{escape(d['address'])}</td>"
                        f"<td class=\"num\">{d['best']} dBm</td>"
                        f"<td class=\"num\">{d['points']} (not placed)</td></tr>")
        html.append("</table>")
        if other["rotating"]:
            words = {**kinds, "unknown": "device"}
            summary = ", ".join(f"{r['count']} {words.get(r['kind'], 'device').lower()}"
                                f"{'s' if r['count'] != 1 else ''} (strongest {r['best']} dBm)"
                                for r in other["rotating"])
            html.append(f"<p class=\"muted\">Also heard, using rotating private addresses so they "
                        f"can't be placed or counted exactly: {summary}. Phones and wearables "
                        "usually advertise this way.</p>")

    tags = analysis.finder_tags(project)
    if tags:
        away = [t for t in tags if t["away"]]
        html.append("<h2>Finder tags</h2>")
        html.append("<p class=\"muted\">AirTags and other item trackers heard during the survey"
                    + (f". {len(away)} {'was' if len(away) == 1 else 'were'} away from their owner, "
                       "with nobody they belong to nearby: lost, left, or planted. Find and check "
                       "each one." if away else
                       "; all were with their owner, so most likely people's keys and bags.")
                    + " Tags with their owner change address every 15 minutes or so, so one tag "
                    "can be listed twice.</p>")
        html.append("<table><tr><th>Tag</th><th>State</th><th>Address</th>"
                    "<th class=\"num\">Strongest</th><th class=\"num\">Nearest point</th></tr>")
        for t in tags:
            state = escape(t["state"] or "not reported")
            if t["away"]:
                state = f'<span class="q q-poor">{state}</span>'
            html.append(f"<tr><td>{escape(t['label'])}</td><td>{state}</td>"
                        f"<td>{escape(t['address'])}</td><td class=\"num\">{t['best']} dBm</td>"
                        f"<td class=\"num\">{chip(t['near_point'])}</td></tr>")
        html.append("</table>")

    html.append(_hackrf(project, chip))
    html.append(_other_bands(project, chip))

    findings = rfplan.audit(project)
    if findings:
        html.append("<h2>Configuration audit</h2><table><tr><th>Severity</th><th>Finding</th>"
                    "<th>Detail</th></tr>")
        for finding in findings:
            severity = finding.severity if finding.severity in advice.SEVERITY_ORDER else "Low"
            html.append(f"<tr><td><span class=\"sev sev-{severity}\">{escape(finding.severity)}"
                        f"</span></td><td>{escape(finding.title)}</td>"
                        f"<td>{escape(finding.detail)}</td></tr>")
        html.append("</table>")

    html.append(_hidden_networks(project))

    if ssid:
        plan = rfplan.channel_plan(project, ssid, allow_dfs)
        if plan:
            html.append(f"<h2>Suggested channel plan for {escape(ssid)}</h2>")
            html.append("<p class=\"muted\">Keeps the network's own radios off each other's "
                        f"channels first, then away from other networks{'' if allow_dfs else ', without DFS channels'}.</p>")
            html.append("<table><tr><th>Radio</th><th>Now</th><th>Suggested</th>"
                        "<th class=\"num\">Shares a channel with, now → after</th></tr>")
            for item in plan:
                macs = ", ".join(sorted(b.upper() for b in item.radio.bssids)[:2])
                html.append(f"<tr><td>{escape(macs)}</td><td>{escape(item.current)}</td>"
                            f"<td>{escape(item.suggested if item.changed else 'keep')}</td>"
                            f"<td class=\"num\">{item.shared_before} → {item.shared_after}</td></tr>")
            html.append("</table>")

    # ----- each point -----
    html.append("<h2>Points</h2>")
    if any(point.spectrum for point in points):
        html.append("<p class=\"muted\">In each spectrum: yellow is the average of the sweeps, "
                    "red the peak, and each access point is a bar across its channel at the "
                    "level it was heard. Grey stretches weren't swept.</p>")
    for number, (point, (key, why, _fix)) in enumerate(zip(points, verdicts), start=1):
        grade = grades[number - 1]
        colour = _colour(quality_colour(grade))
        label, verdict_colour = labels.get(key, (key, (0.5, 0.5, 0.5)))
        html.append(f"<section class=\"point\" id=\"point-{number}\" style=\"--c:{colour}\">")
        html.append("<details class=\"fold point-fold\" open><summary>")
        html.append(f"<a class=\"top\" href=\"#map\">↑ map</a><h3><span class=\"badge {grade}\" "
                    f"style=\"background:{colour}\">{number}</span>Point {number} · "
                    f"{QUALITY_WORDS[grade]}</h3></summary>")
        html.append("<div class=\"point-head\"><div class=\"text\">")
        html.append(f"<p class=\"muted\">{time.strftime('%d %B %Y, %H:%M', time.localtime(point.time))}"
                    + _where(project, point) + "</p>")
        html.append(f"<p class=\"verdict\"><span class=\"swatch\" style=\"background:"
                    f"{_colour(verdict_colour)}\"></span><b>{escape(label)}.</b> {escape(why)}</p>")
        if point.note:
            html.append(f"<p class=\"note\"><b>Note:</b> {escape(point.note)}</p>")
        ok, failed = judged[number - 1]
        if ok is not None:
            html.append(f"<p>{'Meets' if ok else 'Fails'} {escape(ctx.profile.name)}"
                        + ("" if ok else ": " + escape("; ".join(failed))) + ".</p>")
        html.append(_facts(point, ctx, ssid, band))
        html.append("</div>")
        if map_png:
            html.append(f"<div class=\"locator\" style=\"aspect-ratio:{width}/{height}\" "
                        f"role=\"img\" aria-label=\"Where point {number} is on the plan\">"
                        f"<span style=\"{pin_style(point)};background:{colour}\"></span></div>")
        html.append("</div>")
        found = todo[number - 1]
        html.append("<h4>What could be improved here</h4>")
        html.append(_todo(found) if found else _margins(point, ctx, ssid, band))
        html.append(_sweeps(point, number, spectrum_png, range_png, band_png))
        html.append("<h4>Access points heard</h4>")
        html.append(_networks_table(point, target))
        html.append(_dwell_table(point))
        html.append(_iq_table(point))
        if point.connectivity:
            from ..net import connectivity

            html.append(f"<p>{escape(connectivity.describe(point.connectivity))}</p>")
        for note in point.notes:
            html.append(f"<p class=\"note\">{escape(note)}</p>")
        html.append("</details></section>")

    html.append(_method(project, ctx, version))
    html.append(SECTIONS_END)
    html.append(FOLDING)
    html.append(PRINT_OPENS)
    html.append("</body></html>")
    return _fold_sections("\n".join(html))


def _fold_sections(text: str) -> str:
    """
    Each section, from its heading to the next, folded under that heading, open to begin
    with. Every <h2> in the report is at the top level of the body, so this can work on text.
    """
    start, end = text.find("<h2"), text.find(SECTIONS_END)
    if start < 0 or end < start:
        return text
    folded = []
    for part in re.split(r"(?=<h2[ >])", text[start:end]):
        match = re.match(r"(<h2[^>]*>.*?</h2>)(.*)", part, re.S)
        folded.append(f"<details class=\"fold section\" open><summary>{match.group(1)}</summary>"
                      f"{match.group(2)}</details>\n" if match else part)
    return text[:start] + "".join(folded) + text[end:]


def _sweeps(point, number: int, spectrum_png, range_png, band_png=None) -> str:
    """
    A point's spectrum pictures: each Wi-Fi band it swept, then every other range, each
    folded away on its own so the report stays short until someone wants one.
    """
    bands = []
    if point.spectrum and band_png:
        heard = analysis.access_points(point.networks)
        for key, noise in (("2.4", "noise_24"), ("5", "noise_5"), ("6", "noise_6")):
            drawn = band_png(point, key)
            if not drawn:
                continue
            floor = point.spectrum.get(noise)
            count = sum(1 for ap in heard if ap["band"] == key)
            facts = " · ".join(part for part in (
                f"noise floor {floor:.0f} dBm" if floor is not None else "",
                f"{count} access point{'s' if count != 1 else ''}") if part)
            bands.append((f"{key} GHz", facts,
                          _png(drawn, f"{key} GHz spectrum at point {number}")))
    if point.spectrum and not bands:
        # Swept before each band could be drawn on its own (or on Windows): the one picture
        picture = spectrum_png(point)
        if picture:
            bands.append(("Wi-Fi", "", _png(picture, f"Wi-Fi spectrum at point {number}")))
    others = []
    for record in point.ranges if range_png else []:
        drawn = range_png(record)
        if drawn:
            facts = (f"{record['start_mhz']:g}-{record['stop_mhz']:g} MHz"
                     + (f" · {record['analyzer']}" if record.get("analyzer") else ""))
            others.append((record.get("name", ""), facts,
                           _png(drawn, f"{record.get('name', '')} at point {number}")))
    if not bands and not others:
        return ""

    def fold(title: str, facts: str, image: str) -> str:
        return (f"<details class=\"sweeps\"><summary>{escape(title)}"
                + (f" <span class=\"muted\">{escape(facts)}</span>" if facts else "")
                + f"</summary><figure>{image}</figure></details>")

    html = ["<h4>Spectrum</h4>"] + [fold(*band) for band in bands]
    if len(others) > 3:
        # A site sweeping many bands would bury the Wi-Fi ones: the rest go one fold down
        html.append(f"<details class=\"sweeps\"><summary>{len(others)} other ranges</summary>"
                    "<div class=\"inner\">" + "".join(fold(*other) for other in others)
                    + "</div></details>")
    else:
        html += [fold(*other) for other in others]
    return "".join(html)


THEME_WORDS = {"good": "Good", "fair": "Room to improve", "poor": "Needs attention"}


def _grade_meaning(grade: str) -> str:
    return {"good": "only minor suggestions", "fair": "thin margins or moderate issues",
            "poor": "serious problems to fix"}[grade]


def _where(project, point) -> str:
    """Where the point is, in feet from the plan's top-left corner, once the plan has a scale."""
    across, down = project.feet(point.x), project.feet(point.y)
    if across is None:
        return ""
    return f" · {across:.0f} ft across, {down:.0f} ft down from the plan's top-left"


def _scale_bar(project) -> str:
    """A bar of a round number of feet, about a sixth of the plan's width."""
    if not project.meters_per_pixel:
        return ""
    wide = project.feet(project.width) or 0
    feet = next((f for f in (100, 50, 25, 20, 10, 5) if f <= wide / 5), None)
    if feet is None:
        return ""
    share = project.pixels(feet) / project.width * 100
    return (f"<div class=\"scalebar\" style=\"width:{share:.2f}%\">{feet} ft"
            f"<div></div></div>")


MAP_CAPTIONS = {
    "signal": "The strongest signal of the network at each spot. Green is strong, red weak.",
    "snr": "How far the signal stands above the noise. Below 20 dB rates drop; 25 dB or more "
           "is comfortable.",
    "secondary": "The second-strongest access point: what a moving client can hand over to. "
                 "Red means no fallback there.",
    "contention": "How many other radios share the serving channel. Fewer is better.",
    "throughput": "Download speed measured with iperf3.",
    "predicted": "The signal if the planned access points (purple diamonds) were added, from the path "
                 "loss measured in this building. Only a prediction: survey again after.",
}


def _cover(details: dict) -> str:
    """Who the report is from: logo and company, top right."""
    logo = details.get("logo") or {}
    parts = []
    if logo.get("data"):
        parts.append(f'<img class="logo" alt="Logo" src="data:{escape(logo.get("mime", "image/png"))};'
                     f'base64,{logo["data"]}">')
    lines = [escape(details[key]) for key in ("company", "surveyor", "contact") if details.get(key)]
    if lines:
        parts.append("<div>" + "<br>".join(f"<b>{line}</b>" if i == 0 else line
                                            for i, line in enumerate(lines)) + "</div>")
    return f'<header class="cover">{"".join(parts)}</header>' if parts else ""


def _for_whom(details: dict) -> str:
    rows = [(title, details.get(key)) for key, title in
            (("client", "Client"), ("site", "Site"), ("purpose", "Purpose"),
             ("surveyor", "Surveyed by")) if details.get(key)]
    if not rows:
        return ""
    return ('<table class="whom">' + "".join(f"<tr><th>{title}</th><td>{escape(value)}</td></tr>"
                                            for title, value in rows) + "</table>")


def _area_cards(project, ctx, ssid, band) -> str:
    """How much of the floor, not how many points, meets each target."""
    checks = [
        ("signal", ctx.profile.primary, f"at {ctx.profile.primary:.0f} dBm or better"),
        ("snr", ctx.profile.snr, f"with {ctx.profile.snr:.0f} dB signal-to-noise"),
        ("secondary", ctx.profile.secondary or -75,
         f"with a second access point at {ctx.profile.secondary or -75:.0f} dBm"),
    ]
    cards = []
    for key, threshold, words in checks:
        found = planning.area_coverage(project, ctx, key, threshold, ssid, band)
        if found is None:
            continue
        share = found["share"] * 100
        grade = "good" if share >= 95 else "fair" if share >= 80 else "poor"
        area = (f"{found['meeting_sqft']:,.0f} of {found['surveyed_sqft']:,.0f} sq ft"
                if found["surveyed_sqft"] else "of the surveyed area")
        cards.append(f'<div class="card" style="--c:{_colour(quality_colour(grade))}">'
                     f"<b>{share:.0f}%</b>of the floor {escape(words)}"
                     f'<div class="muted">{area}</div></div>')
    if not cards:
        return ""
    return ('<h3 class="sub">Floor area</h3><div class="cards">' + "".join(cards) + "</div>"
            '<p class="muted">Worked out across the surveyed floor from the same interpolation '
            "the heatmap draws, so a corner with one weak point counts by its size, not as one "
            "point.</p>")


def _checklist(themes) -> str:
    """Every recommendation that matters, as boxes to tick off on a printout."""
    items = [(theme, item) for theme in themes if theme.key != "survey" for item in theme.items
             if item.priority != "Low"]
    if not items:
        return ""
    rows = "".join(
        f'<li><label><input type="checkbox"> <span class="sev sev-{item.priority}">'
        f"{item.priority}</span> <span class=\"muted\">{escape(theme.title)}:</span> "
        f"{escape(item.action)}"
        + (f' <span class="muted">(points {", ".join(str(n) for n in item.points)})</span>'
           if item.points else "")
        + "</label></li>" for theme, item in items)
    return ('<h3 class="sub" id="checklist">Checklist</h3><p class="muted">For whoever makes the '
            "changes: tick each off, then survey again to confirm.</p>"
            f'<ul class="checklist">{rows}</ul>')


def _planned(project, ctx, ssid, band, sites) -> str:
    if not project.virtual_aps:
        return ""
    gain = planning.planned_gain(project, ctx, ssid, band, sites)
    html = ["<h2>Planned access points</h2>"]
    if gain:
        beyond = (f" They would also cover about {gain['beyond_sqft']:,.0f} sq ft that wasn't "
                  "surveyed." if gain.get("beyond_sqft") else "")
        html.append(f"<p><b>{gain['now'] * 100:.0f}% → {gain['after'] * 100:.0f}%</b> of the "
                    f"surveyed floor at {gain['target']:.0f} dBm or better with these added."
                    f"{beyond} Predicted from {escape(gain['model'])}.</p>")
    html.append("<table><tr><th>Planned</th><th>Name</th><th>Bands</th><th>Where</th></tr>")
    for index, ap in enumerate(project.virtual_aps, start=1):
        across, down = project.feet(ap["x"]), project.feet(ap["y"])
        spot = (f"{across:.0f} ft across, {down:.0f} ft down" if across is not None
                else f"{ap['x'] / max(1, project.width) * 100:.0f}% across, "
                     f"{ap['y'] / max(1, project.height) * 100:.0f}% down")
        html.append(f"<tr><td>P{index}</td><td>{escape(ap.get('name') or '')}</td>"
                    f"<td>{escape(', '.join(BAND_NAMES.get(b, b) for b in ap.get('bands', [])))}</td>"
                    f"<td>{spot}</td></tr>")
    html.append("</table>")
    return "\n".join(html)


def _comparison(extra: dict, map_block) -> str:
    compared = extra.get("compared") or []
    if not compared:
        return ""
    html = [f"<h2>Compared with {escape(extra.get('baseline') or 'the earlier survey')}</h2>",
            "<p class=\"muted\">Where both surveys measured: green got better, red worse.</p>",
            "<table><tr><th>Measure</th><th class=\"num\">Better</th><th class=\"num\">Worse</th>"
            "<th class=\"num\">Average change</th></tr>"]
    for key, title, stats, _png_bytes in compared:
        if not stats.get("cells"):
            html.append(f"<tr><td>{escape(title)}</td><td colspan=\"3\" class=\"muted\">not "
                        "measured in the same places</td></tr>")
            continue
        unit = analysis.METRICS[key][1]
        better, worse = stats["better"] * 100, stats["worse"] * 100
        html.append(f"<tr><td>{escape(title)}</td>"
                    f"<td class=\"num\">{_graded(better, 30, 1, True, f'{better:.0f}% of the area')}</td>"
                    f"<td class=\"num\">{_graded(worse, 1, 10, False, f'{worse:.0f}%')}</td>"
                    f"<td class=\"num\">{stats['average']:+.1f} {escape(unit)}</td></tr>")
    html.append("</table>")
    for key, title, stats, png in compared:
        if png and stats.get("cells"):
            html.append(f"<h3 class=\"sub\">{escape(title)}: change</h3>"
                        + map_block(png, f"Change in {title}"))
    return "\n".join(html)


def _inventory(project, ctx, sites, chip, ssid, band) -> str:
    """Every access point found: where it is, what it runs, how far it reaches, whom it serves."""
    if not sites:
        return ""
    points = project.points
    by_bssid = {}
    for point in points:
        for network in point.networks:
            by_bssid.setdefault(network["bssid"].lower(), network)
    served: dict[int, list[int]] = {}
    for number, point in enumerate(points, start=1):
        best = point.best(ssid, None, band)
        if best is None:
            continue
        for index, site in enumerate(sites):
            if best["bssid"].lower() in {b.lower() for b in site.get("bssids", [])}:
                served.setdefault(index, []).append(number)
    html = ["<h2 id=\"aps\">Access points</h2>",
            "<p class=\"muted\">Every access point heard at enough points to locate, or placed on "
            "the plan by the surveyor. Reach is how far each radio stays at "
            f"{ctx.profile.primary:.0f} dBm or better in the open; walls shorten it. "
            "Serves lists the points where it's the strongest.</p>",
            "<table><tr><th>#</th><th>Access point</th><th>Radios</th>"
            "<th>Manufacturer</th><th>Where</th><th>Serves</th></tr>"]
    for index, site in enumerate(sites):
        radios = []
        vendor = ""
        for fit in sorted(site.get("fits", []),
                          key=lambda f: (analysis.BAND_ORDER.get(f.get("band"), 9), f.get("ssid", ""))):
            network = by_bssid.get(fit["bssid"].lower(), {})
            vendor = vendor or network.get("vendor") or ""
            reach = (None if fit.get("rough") else
                     advice.reach_feet(fit, ctx.profile.primary, project.meters_per_pixel))
            radios.append(f"{_ssid(fit.get('ssid'))} {escape(BAND_NAMES.get(fit.get('band'), ''))} "
                          f"{escape(advice.channel_name(network)) if network else ''}"
                          + (f", {network.get('width_mhz')} MHz" if (network.get('width_mhz') or 20) > 20 else "")
                          + (f", reach ≈{reach:.0f} ft" if reach else ""))
        status = ("placed" if site.get("placed") else
                  "middle of where it was heard; its signal changes too little to place it"
                  if site.get("unfitted") else
                  "near the point where it was strongest" if site.get("guess") else
                  "outside the plan" if site["outside"]
                  else "estimated, rough" if site["rough"] else "estimated")
        across, down = project.feet(site["x"]), project.feet(site["y"])
        where = (f"{across:.0f} ft across, {down:.0f} ft down" if across is not None
                 else f"{site['x'] / max(1, project.width) * 100:.0f}% across, "
                      f"{site['y'] / max(1, project.height) * 100:.0f}% down")
        html.append(f"<tr><td>{index + 1}</td><td><b>{escape(site.get('name') or 'Unnamed')}</b>"
                    f"<div class=\"muted\">{status}</div></td>"
                    f"<td>{'<br>'.join(radios) or escape(', '.join(n for n, _b in site['networks']))}</td>"
                    f"<td>{escape(vendor)}</td><td>{where}</td>"
                    f"<td>{''.join(chip(n) for n in served.get(index, [])) or '<span class=muted>none</span>'}</td></tr>")
    html.append("</table>")
    return "\n".join(html)


def _hidden_networks(project) -> str:
    """Every hidden access point heard, with the name recovered for it by listening, if any."""
    rows = hidden.report_rows(project)
    if not rows:
        return ""
    named = sum(1 for r in rows if r["name"])
    html = ["<h2>Hidden networks</h2>",
            f"<p>{len(rows)} access point{'s' if len(rows) != 1 else ''} hid "
            f"{'their' if len(rows) != 1 else 'its'} network name. "
            + (f"The survey heard {named} of them say it anyway, when a device joined. "
               if named else "")
            + "<b>To improve:</b> broadcast the name and rely on WPA2 or WPA3 for security. "
            "Hiding it adds none, since every device joining says the name out loud; it makes "
            "devices slower to join, and makes phones call out for it by name wherever they "
            "go.</p>",
            "<table><tr><th>Access point</th><th>Name</th><th>Band</th>"
            "<th class=\"num\">Heard at</th><th class=\"num\">Strongest</th></tr>"]
    for row in rows:
        name = (_ssid(row["name"]) if row["name"] else
                '<span class="muted">Not heard: no device joined it while the survey listened'
                "</span>")
        where = f"{row['band']} GHz" + (f", ch {row['channel']}" if row.get("channel") else "")
        best = f"{row['best']} dBm" if row["best"] is not None else ""
        html.append(f"<tr><td>{escape(row['bssid'])}</td><td>{name}</td><td>{escape(where)}</td>"
                    f"<td class=\"num\">{row['points']} point{'s' if row['points'] != 1 else ''}"
                    f"</td><td class=\"num\">{best}</td></tr>")
    html.append("</table>")
    return "".join(html)


def _method(project, ctx, version: str) -> str:
    """How the survey was made, so the reader can judge how far to trust it."""
    points = project.points
    if not points:
        return ""
    times = sorted(p.time for p in points)
    methods = [p.method for p in points if p.method]
    adapters = Counter(m.get("adapter_name") or m.get("adapter") for m in methods)
    scans = Counter(m.get("scan") for m in methods)
    analyzers = {m.get("analyzer") for m in methods if m.get("analyzer")}
    sweeps = sorted({m.get("sweeps_per_band") for m in methods if m.get("sweeps_per_band")})
    sources = Counter(n.get("rssi_source") for p in points for n in p.networks)
    rows = [
        ("Measured", f"{time.strftime('%d %b %Y %H:%M', time.localtime(times[0]))} to "
                     f"{time.strftime('%d %b %Y %H:%M', time.localtime(times[-1]))}, "
                     f"{len(points)} points"),
        ("Signal levels", ", ".join(f"{'true dBm (iw)' if k == 'iw' else 'estimated (NetworkManager)' if k == 'nmcli' else k}: "
                                    f"{v} readings" for k, v in sources.items() if k)),
    ]
    if project.survey_setup:
        from .surveysetup import Setup, describe

        rows.insert(1, ("Survey type", describe(Setup.from_dict(project.survey_setup))))
    if adapters:
        rows.append(("Wi-Fi adapter", ", ".join(f"{name} ({count} points)" for name, count in adapters.items())))
    shared = Counter(", ".join(m["scan_adapters"]) for m in methods if m.get("scan_adapters"))
    if shared:
        rows.append(("Shared scans", "; ".join(
            f"{names} ({count} points)" for names, count in shared.items())
            + ". Each adapter scanned its own channels, so each access point was read by one "
              "adapter, on its own channel; adapters' corrections line their levels up"))
    if scans:
        rows.append(("Scanning", ", ".join(f"{name} ({count})" for name, count in scans.items())))
    merged = sum(((m.get("wifi_readings") or {}).get("merged") or 0) for m in methods)
    if merged:
        rows.append(("Repeat readings", f"{merged} access point readings were heard by both of a "
                                        "point's scans and averaged, rather than the stronger kept"))
    with_spectrum = sum(1 for p in points if p.spectrum)
    by_analyzer = Counter(seg.get("analyzer") for p in points
                          for seg in (p.spectrum or {}).get("segments") or [] if seg.get("analyzer"))
    if by_analyzer:
        ranges: dict[str, set] = {}
        for p in points:
            for seg in (p.spectrum or {}).get("segments") or []:
                if seg.get("analyzer"):
                    ranges.setdefault(seg["analyzer"], set()).add(BAND_NAMES.get(
                        "2.4" if seg["start_mhz"] < 3000 else "5" if seg["start_mhz"] < 5925 else "6"))
        spectrum_text = "; ".join(
            f"{name}: {', '.join(sorted(ranges[name]))}" for name in sorted(by_analyzer)) \
            + f", at {with_spectrum} points"
    else:
        spectrum_text = f"{', '.join(sorted(analyzers)) or 'RF Explorer'} at {with_spectrum} points"
    spectrum_text += f", {'/'.join(str(s) for s in sweeps)} sweeps per band" if sweeps else ""
    if len(by_analyzer) > 1:
        spectrum_text += (". The analyzers swept at once, each range always by the same one, so "
                          "every map compares like with like")
    rows.append(("Spectrum", spectrum_text if with_spectrum else "none"))
    hackrf_uses = [label for label, count in (
        ("channels identified", sum(1 for p in points if p.iq)),
        ("cellular bands", sum(1 for p in points if p.cellular)),
        ("test tone", sum(1 for p in points if p.tone)),
        ("radar watch", len((project.radar or {}).get("watches") or []))) if count]
    if hackrf_uses:
        uncalibrated = any("uncalibrated" in (m.get("hackrf") or "") for m in methods)
        rows.append(("HackRF", ", ".join(hackrf_uses)
                     + (". Its levels weren't calibrated, so they're estimates" if uncalibrated else "")))
    # Each HackRF's calibration as it stood at its last point, since Setup's may have changed
    latest: dict[str, dict] = {}
    for m in methods:
        latest.update(m.get("hackrf_cal") or {})
    for name, cal in latest.items():
        rows.append((name, _calibration_words(cal)))
    looks = [r for p in points for r in p.iq or []]
    overloaded = [n for n, p in enumerate(points, start=1)
                  if any((r.get("clipped") or 0) >= iq.OVERLOAD_SHARE for r in p.iq or [])]
    if overloaded:
        rows.append(("HackRF overload", f"at {len(overloaded)} points ({', '.join(map(str, overloaded[:12]))}"
                                        f"{'…' if len(overloaded) > 12 else ''}): too strong a signal "
                                        "nearby for its gain, so what it named there is less "
                                        "certain. Lower the LNA and VGA gain for the next survey"))
    deaf = sorted({band for cal in latest.values() for band in cal.get("deaf") or []})
    if deaf:
        rows.append(("HackRF antenna", f"heard nothing of the strong signals on "
                                       f"{' and '.join(b + ' GHz' for b in deaf)}, so those bands "
                                       "were left out of its calibration and its spectrum there "
                                       "understates what's on the air"))
    kept = sum(1 for r in looks if r.get("recording")) + sum(
        1 for e in (project.radar or {}).get("events") or [] if e.get("recording"))
    if kept:
        rows.append(("Raw I/Q kept", f"{kept} SigMF recordings of what couldn't be named, or of radar, "
                                     "in the surveying computer's wifisurveyor/iq folder"))
    bt_adapters = Counter(", ".join(m["bt_adapters"]) for m in methods if m.get("bt_adapters"))
    if bt_adapters:
        both = sum(1 for p in points for d in p.bluetooth or [] if (d.get("adapters") or 1) > 1)
        rows.append(("Bluetooth adapters", "; ".join(f"{names} ({count} points)"
                                                     for names, count in bt_adapters.items())
                     + (f". {both} devices were heard by more than one at a point and listed "
                        "once, at their strongest" if both else "")))
    listened = [m["hidden_listen"] for m in methods
                if m.get("hidden_listen") and not m["hidden_listen"].get("skipped")]
    if listened or project.hidden_names:
        rows.append(("Hidden network names", (
            f"listened for in monitor mode at {len(listened)} points "
            f"({', '.join(sorted({m['adapter'] for m in listened}))}, "
            f"{listened[0]['seconds']} s a channel); " if listened else "")
            + f"{len(project.hidden_names)} recovered, only by listening: nothing was sent "
              "to make devices reconnect"))
    if project.rf_calibration and project.rf_calibration.get("offset_db") is not None:
        rows.append(("Analyzer calibration", f"{project.rf_calibration['offset_db']:+.1f} dB against the adapter"))
    rows.append(("Connection checks", f"{sum(1 for p in points if p.connectivity and 'latency_ms' in p.connectivity)} points"))
    rows.append(("Throughput tests", f"{sum(1 for p in points if any(r.get('down_mbps') is not None for r in [p.throughput or {}, *p.throughputs]))} points"))
    rows.append(("Requirement", f"{ctx.profile.name}"
                 + (f", with {ctx.allowance:g} dB allowed for phones" if ctx.allowance else "")))
    rows.append(("Scale", f"{project.feet(project.width):.0f} × {project.feet(project.height):.0f} ft"
                 if project.meters_per_pixel else "not set"))
    rows.append(("Software", f"Wi-Fi Surveyor {version}".strip()))
    return ("<h2 id=\"method\">How this survey was made</h2><table class=\"whom\">"
            + "".join(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in rows)
            + "</table>")


def _calibration_words(cal: dict) -> str:
    """One HackRF's calibration, as the report's method table says it."""
    if not cal.get("calibrated"):
        return ("not calibrated, so its levels are estimates (it reads relative to its own full "
                "scale, roughly placed in dBm)")
    words = f"matched to {cal['calibrated_with']}" if cal.get("calibrated_with") else "offset set by hand"
    words += f", {cal['offset_db']:+.1f} dB"
    bands = cal.get("band_offsets") or {}
    if bands:
        words += " (" + ", ".join(f"{band} GHz {value:+.1f}" for band, value in bands.items()) + ")"
    gains = cal.get("gains") or []
    if len(gains) == 3:
        words += f"; LNA {gains[0]} dB, VGA {gains[1]} dB, amplifier {'on' if gains[2] else 'off'}"
    if abs(cal.get("gain_shift") or 0) >= 0.5:
        words += (f"; adjusted {-cal['gain_shift']:+.0f} dB for gains changed since it was "
                  "calibrated")
    if cal.get("bias_tee"):
        words += "; antenna power on"
    return words


def _iq_table(point) -> str:
    """What the HackRF identified on each channel it looked at here, and how far to trust it."""
    if not point.iq:
        return ""
    several = len({r.get("hackrf") for r in point.iq if r.get("hackrf")}) > 1
    rows = []
    for result in point.iq:
        found = result["findings"] or []
        what = "; ".join(iq.describe(f) for f in found) or "nothing transmitting"
        about = []
        clipped = result.get("clipped") or 0.0
        if clipped >= iq.OVERLOAD_SHARE:
            about.append(f'<span class="q q-fair">overloaded</span> {clipped * 100:.1f}% of samples '
                         "at full scale: some of what's named may not be there")
        if result.get("recording"):
            about.append(f"raw I/Q kept: {escape(Path(result['recording']).name)}")
        if several and result.get("hackrf"):
            about.append(escape(result["hackrf"]))
        rows.append(f"<tr><td class=\"num\">{result['center_mhz']:.0f} MHz</td>"
                    f"<td class=\"num\">{_graded(result['airtime'] * 100, 30, 60, False, f'{result['airtime'] * 100:.0f}%')}</td>"
                    f"<td>{escape(what)}</td><td>{'; '.join(about)}</td></tr>")
    return ("<h4>Identified by the HackRF</h4><table><tr><th class=\"num\">Channel</th>"
            "<th class=\"num\">On the air</th><th>What was transmitting</th><th>Notes</th></tr>"
            + "".join(rows) + "</table>")


def _other_bands(project, chip) -> str:
    """
    The bands beyond Wi-Fi the survey measured (mics, production, industrial, business, venue):
    per band, how busy it was across the site and where, with what to do about it.
    """
    points = project.points
    measured = [(n, p.bands) for n, p in enumerate(points, start=1) if p.bands]
    if not measured:
        return ""
    keys = {key for _n, found in measured for key in found}
    html = ["<h2>Other bands</h2>",
            "<p class=\"muted\">How far the strongest signal in each band stood over that "
            "band's own floor at each point: green under 10 dB (quiet), amber 10-25 dB, red over "
            "25 dB (busy). Energy, not decoded signals: a red band has something transmitting "
            "strongly near those points. For the TV bands, the channels carrying stations and "
            "the open ones are listed, which is where mics and IEMs can go.</p>",
            "<table><tr><th>Band</th><th>Licence</th><th class=\"num\">Quietest</th>"
            "<th class=\"num\">Busiest</th><th>Busy at</th><th>What to improve</th></tr>"]
    words = {rfbands.UNLICENSED: "unlicensed", rfbands.LICENSED: "licensed",
             rfbands.AVOID: "keep out", rfbands.LISTEN: "listen only"}
    for band in sorted((rfbands.BY_KEY[k] for k in keys if k in rfbands.BY_KEY), key=lambda b: b.low):
        values = [(n, found[band.key]) for n, found in measured if band.key in found]
        levels = [(n, rfbands.activity(e)) for n, e in values if rfbands.activity(e) is not None]
        if not levels:
            continue

        def cell(value: float) -> str:
            return _graded(value, rfbands.QUIET_DB, rfbands.NOISY_DB, False, f"{value:.0f} dB")

        busy = [n for n, v in sorted(levels, key=lambda item: -item[1]) if v >= rfbands.QUIET_DB]
        worst_n, worst_entry = max(values, key=lambda item: rfbands.activity(item[1]) or 0.0)
        todo = rfbands.advice(band, worst_entry)
        if band.tv:
            stations = sorted({c["channel"] for _n, e in values for c in (e.get("tv") or [])
                               if c["state"] == "station"})
            busy_tv = sorted({c["channel"] for _n, e in values for c in (e.get("tv") or [])
                              if c["state"] == "busy"})
            every = [c for c, _a, _b in rfbands.tv_channels(band.low, band.high)]
            open_ = [c for c in every if c not in stations and c not in busy_tv]
            todo = ((f"Stations on {', '.join(map(str, stations))}. " if stations else "No TV stations. ")
                    + (f"Other signals on {', '.join(map(str, busy_tv))}. " if busy_tv else "")
                    + (f"Open everywhere measured: {', '.join(map(str, open_))}; put mics and "
                       "IEMs there, and coordinate them (Frequencies → Mic frequencies)."
                       if open_ else "No channel was open at every point: coordinate around the "
                                     "quietest spots at the stage."))
        elif busy:
            todo += f" Loudest at point {worst_n}."
        html.append(f"<tr><td>{escape(band.name)}</td><td>{escape(words.get(band.licence, ''))}</td>"
                    f"<td class=\"num\">{cell(min(v for _n, v in levels))}</td>"
                    f"<td class=\"num\">{cell(max(v for _n, v in levels))}</td>"
                    f"<td>{''.join(chip(n) for n in busy[:12])}</td>"
                    f"<td>{escape(todo)}</td></tr>")
    html.append("</table>")
    rough = sorted({e.get("analyzer", "") for _n, found in measured for e in found.values()
                    if any(word in e.get("analyzer", "") for word in ("RTL-SDR", "HackRF"))})
    if rough:
        html.append(f"<p class=\"muted\">Measured partly with {escape(', '.join(rough))}: the "
                    "figures above are differences from each band's own floor, so they hold even "
                    "where those radios weren't calibrated to true dBm.</p>")
    return "\n".join(html)


def _hackrf(project, chip) -> str:
    """The survey-wide results only the HackRF provides: interferers, radar, cellular, walls."""
    html = []
    points = project.points

    # ----- interferers it named -----
    named: dict[str, dict] = {}
    for number, point in enumerate(points, start=1):
        for finding in analysis.iq_interferers(point):
            entry = named.setdefault(finding["kind"], {"finding": finding, "points": [], "airtime": 0.0,
                                                       "freqs": set()})
            if number not in entry["points"]:
                entry["points"].append(number)
            entry["airtime"] = max(entry["airtime"], finding["airtime"])
            entry["freqs"].add(round(finding["center_mhz"]))
    if named:
        html.append("<h2>Non-Wi-Fi transmitters identified</h2>")
        html.append("<p class=\"muted\">From the HackRF's close look at the busiest channels at "
                    "each point: every transmission seen whole, named by its width and timing. "
                    "Busiest is the largest share of time it was on the air at any one point.</p>")
        html.append("<table><tr><th>What</th><th class=\"num\">Busiest</th><th>Points</th>"
                    "<th>What to do</th></tr>")
        for kind, entry in sorted(named.items(), key=lambda kv: -kv[1]["airtime"]):
            finding = entry["finding"]
            busy = entry["airtime"] * 100
            grade = _graded(busy, 5, 20, False, f"{busy:.0f}%")
            where = ", ".join(f"{f} MHz" for f in sorted(entry["freqs"])[:4])
            html.append(f"<tr><td><b>{escape(finding['name'])}</b><br><span class=\"muted\">"
                        f"{escape(where)}</span></td><td class=\"num\">{grade}</td>"
                        f"<td>{''.join(chip(n) for n in entry['points'])}</td>"
                        f"<td>{escape(advice.IQ_FIXES.get(kind, advice.IQ_FIXES['bursty']))}</td></tr>")
        html.append("</table>")

    # ----- radar -----
    radar = project.radar or {}
    watches = radar.get("watches") or []
    if watches:
        events = radar.get("events") or []
        seconds = sum(w.get("seconds", 0) for w in watches)
        html.append("<h2>Radar on the DFS channels</h2>")
        if events:
            html.append(f"<p><span class=\"q q-poor\">Radar heard</span> in {seconds / 60:.0f} "
                        "minutes of listening. Access points must leave a channel for 30 minutes "
                        "when they hear radar on it, dropping their clients, so keep radios off "
                        "these channels.</p>")
            html.append("<table><tr><th class=\"num\">Around</th><th>Channels</th>"
                        "<th class=\"num\">Pulse interval</th><th class=\"num\">Pulse width</th>"
                        "<th>When</th></tr>")
            for event in events:
                channels = analysis.channels_touched(event["center_mhz"], 20)
                html.append(f"<tr><td class=\"num\">{event['center_mhz']:.0f} MHz</td>"
                            f"<td>{escape(', '.join(str(c) for c in channels))}</td>"
                            f"<td class=\"num\">{event['pri_us']:.0f} µs</td>"
                            f"<td class=\"num\">{event['width_us']:.0f} µs</td>"
                            f"<td>{time.strftime('%d %b %H:%M', time.localtime(event['time']))}</td></tr>")
            html.append("</table>")
        else:
            html.append(f"<p><span class=\"q q-good\">No radar heard</span> in {seconds / 60:.0f} "
                        "minutes of listening across the DFS channels. Radar that sweeps past "
                        "rarely, or is far off, can still be missed: a longer watch at a different "
                        "time of day makes this firmer before relying on DFS channels.</p>")
        radios = sorted({name for w in watches for name in w.get("hackrfs") or []})
        if len(radios) > 1:
            html.append(f"<p class=\"muted\">Watched with {len(radios)} HackRFs at once "
                        f"({escape(', '.join(radios))}), sharing the channels, so each was "
                        "listened to for longer.</p>")
        overloaded = sorted({c for w in watches for c in w.get("overloaded") or []})
        behind = sorted({c for w in watches for c in w.get("behind") or []})
        if overloaded:
            html.append(f"<p><span class=\"q q-fair\">Less certain</span> on channel"
                        f"{'s' if len(overloaded) > 1 else ''} {', '.join(map(str, overloaded))}: "
                        "the HackRF was overloaded there, which hides weak pulses. Watch again "
                        "with its gain lowered.</p>")
        if behind:
            html.append(f"<p><span class=\"q q-fair\">Less certain</span> on channel"
                        f"{'s' if len(behind) > 1 else ''} {', '.join(map(str, behind))}: the "
                        "computer fell behind the HackRF's samples there, so short pulses may "
                        "have been missed. Watch again with less else running.</p>")
        recorded = [e for e in events if e.get("recording")]
        if recorded:
            html.append(f"<p class=\"muted\">Raw I/Q of {len(recorded)} of these was kept as SigMF "
                        f"recordings, for a closer look: "
                        f"{escape(', '.join(Path(e['recording']).name for e in recorded[:6]))}.</p>")

    # ----- cellular -----
    measured = [(n, p.cellular) for n, p in enumerate(points, start=1) if p.cellular]
    if measured:
        html.append("<h2>Cellular coverage</h2>")
        html.append("<p class=\"muted\">Downlink power in the strongest 5 MHz of each band, from "
                    "the HackRF. Roughly, −65 dBm per 5 MHz is what a phone shows as RSRP −90 "
                    "(good) and −80 is RSRP −105 (weak). It can't say which carrier it is.</p>")
        rough = [n for n, p in enumerate(points, start=1) if p.cellular
                 and any(not cal.get("calibrated") for cal in ((p.method or {}).get("hackrf_cal") or {}).values())]
        if rough:
            html.append(f"<p><span class=\"q q-fair\">Estimates</span> at {len(rough)} of "
                        f"{len(measured)} points, where the HackRF wasn't calibrated: their levels "
                        "may be off by 10 dB or more. Calibrate it (Setup → HackRF) and measure "
                        "again before relying on them.</p>")
        html.append("<table><tr><th>Band</th><th class=\"num\">Best</th><th class=\"num\">"
                    "Typical</th><th class=\"num\">Worst</th><th>Weak at</th></tr>")
        weak_bands = []
        for key, name, _low, _high in cellular.BANDS:
            values = [(n, c[key]["peak_dbm"]) for n, c in measured
                      if key in c and c[key].get("peak_dbm") is not None]
            if not values:
                continue
            levels = [v for _n, v in values]

            def cell(value: float) -> str:
                return _graded(value, cellular.GOOD_DBM, cellular.FAIR_DBM, True, f"{value:.0f} dBm")

            weak = [n for n, v in values if v < cellular.FAIR_DBM]
            if key in cellular.CELLULAR and len(weak) > len(values) / 2:
                weak_bands.append(name)
            html.append(f"<tr><td>{escape(name)}</td><td class=\"num\">{cell(max(levels))}</td>"
                        f"<td class=\"num\">{cell(sorted(levels)[len(levels) // 2])}</td>"
                        f"<td class=\"num\">{cell(min(levels))}</td>"
                        f"<td>{''.join(chip(n) for n in weak[:12])}</td></tr>")
        html.append("</table>")
        best = [cellular.strongest(c) for _n, c in measured]
        poor = [n for (n, _c), level in zip(measured, best) if level is not None and level < cellular.FAIR_DBM]
        if poor:
            html.append(f"<p><b>Weak cellular at {len(poor)} of {len(measured)} points</b> "
                        f"(no band reaches −80 dBm per 5 MHz): {''.join(chip(n) for n in poor[:20])}. "
                        "Phones there will struggle with calls; Wi-Fi calling on this network, or a "
                        "carrier-approved booster or small cell, would cover them.</p>")
        elif weak_bands:
            html.append(f"<p>Some bands are weak across most of the site ({escape(', '.join(weak_bands))}), "
                        "though another band reaches every point. Phones that rely on those bands "
                        "(some carriers' 5G) will drop back to the stronger one.</p>")

    # ----- walls from the test tone -----
    fit = analysis.fit_walls(project)
    if fit and fit["walls"]:
        html.append("<h2>Walls measured with the test tone</h2>")
        html.append(f"<p class=\"muted\">The HackRF sent a steady tone at "
                    f"{project.tone_source['freq_mhz']:.0f} MHz from a fixed spot, heard at "
                    f"{fit['points']} points. Fitted path-loss exponent {fit['exponent']:.1f}"
                    + (" (held: too few paths to measure it)" if fit["exponent_held"] else "")
                    + f", leaving {fit['rms']:.1f} dB unexplained on average.</p>")
        html.append("<table><tr><th>Wall</th><th class=\"num\">Set to</th><th class=\"num\">"
                    "Measured</th><th class=\"num\">Paths through it</th></tr>")
        for wall in fit["walls"]:
            item = project.walls[wall["index"]]
            html.append(f"<tr><td>{escape(item.material)} #{wall['index'] + 1}</td>"
                        f"<td class=\"num\">{wall['before']:.0f} dB</td>"
                        f"<td class=\"num\">{wall['measured']:.1f} dB</td>"
                        f"<td class=\"num\">{wall['paths']}</td></tr>")
        html.append("</table>")
    return "\n".join(html)


def _trend(figure, rows: list[dict]) -> str:
    """One measure across the surveys: a thin line, a dot per survey, the latest labelled."""
    points = [(i, row[figure.key]) for i, row in enumerate(rows) if row.get(figure.key) is not None]
    if len(points) < 2:
        return ""
    # Drawn about as wide as it's shown, so the text stays readable; room on the right for
    # the latest value's label
    width, height, left, right, top, bottom = 250, 110, 8, 64, 12, 20
    values = [v for _i, v in points]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.15, figure.notable)
    low, high = low - pad, high + pad
    last = len(rows) - 1

    def x(i: int) -> float:
        return left + (width - left - right) * (i / last if last else 0.5)

    def y(v: float) -> float:
        return top + (height - top - bottom) * (1 - (v - low) / (high - low))

    unit = "%" if figure.key == "meets" else (f" {figure.unit}" if figure.unit else "")
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(figure.label)} '
             'across the surveys">']
    for fraction in (0.0, 1.0):                      # recessive guides, top and bottom
        level = top + (height - top - bottom) * fraction
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{level:.1f}" y2="{level:.1f}" '
                     'stroke="#E5E9EF" stroke-width="1"/>')
    path = " ".join(f"{'M' if n == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for n, (i, v) in enumerate(points))
    parts.append(f'<path d="{path}" fill="none" stroke="#1D4ED8" stroke-width="2" '
                 'stroke-linejoin="round" stroke-linecap="round"/>')
    for i, v in points:
        latest = i == points[-1][0]
        when = library.when(rows[i]["when"], False)
        parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="{5 if latest else 4}" fill="#1D4ED8" '
                     f'stroke="#FFFFFF" stroke-width="2"><title>{escape(when)}: {v:.0f}{escape(unit)}'
                     '</title></circle>')
    i, v = points[-1]
    parts.append(f'<text x="{x(i) + 8:.1f}" y="{y(v) + 4:.1f}" font-size="12" font-weight="600" '
                 f'fill="#1E293B">{v:.0f}{escape(unit)}</text>')
    for i, anchor in ((points[0][0], "start"), (points[-1][0], "end")):
        parts.append(f'<text x="{x(i):.1f}" y="{height - 5}" font-size="11" fill="#5B6778" '
                     f'text-anchor="{anchor}">{escape(library.when(rows[i]["when"], False))}</text>')
    parts.append("</svg>")
    better = "higher is better" if figure.higher_is_better else "lower is better"
    return (f'<div class="trend"><b>{escape(figure.label)}</b><span class="muted" '
            f'style="font-size:12px">{better}</span>{"".join(parts)}</div>')


def _history(history: dict | None) -> str:
    """
    The site over time: what changed in words, a row per survey with arrows against the one
    before, a small chart per measure, and the signal map as it was at each survey.
    """
    rows = (history or {}).get("rows") or []
    if len(rows) < 2:
        return ""
    html = ["<h2>History of this site</h2>"]
    html += [f"<p>{escape(sentence)}</p>" for sentence in site_history.summary(rows)]
    shown = [f for f in site_history.FIGURES if any(r.get(f.key) is not None for r in rows)]
    html.append("<table><tr><th>Survey</th><th class=\"num\">Points</th>"
                + "".join(f"<th class=\"num\">{escape(f.label)}</th>" for f in shown) + "</tr>")
    for index, row in enumerate(rows):
        cells = []
        for figure in shown:
            value = row.get(figure.key)
            if value is None:
                cells.append("<td class=\"num muted\">–</td>")
                continue
            text = (f"{value:.0f}%" if figure.key == "meets" else f"{value:.0f}"
                    + (f" {figure.unit}" if figure.unit and figure.key != "issues" else ""))
            moved = site_history.change(rows[index - 1], row, figure.key) if index else None
            if moved and moved[1] != "same":
                arrow = "▲" if moved[0] > 0 else "▼"
                css = "q-good" if moved[1] == "better" else "q-poor"
                text = (f'<span class="q {css}" title="{moved[1]} than the survey before">'
                        f'{arrow} {text}</span>')
            cells.append(f"<td class=\"num\">{text}</td>")
        now = " (this report)" if row.get("this") else ""
        html.append(f"<tr{' class=\"now\"' if row.get('this') else ''}><td>"
                    f"{escape(library.when(row['when'], False))}{now}<br><span class=\"muted\">"
                    f"{escape(row['name'])}</span></td><td class=\"num\">{row['points']}</td>"
                    + "".join(cells) + "</tr>")
    html.append("</table>")
    html.append("<p class=\"muted\">▲▼ compare each survey with the one before it: green is better, "
                f"red worse. Judged against {escape(rows[-1]['requirement'])} for every survey.</p>")
    trends = "".join(_trend(f, rows) for f in shown)
    if trends:
        html.append(f'<div class="trends">{trends}</div>')
    maps = (history or {}).get("maps") or []
    if len(maps) >= 2:
        html.append("<h3 class=\"sub\">Signal, survey by survey</h3><div class=\"then\">")
        for when, png in maps:
            html.append(f"<figure>{_png(png, 'Signal map ' + library.when(when, False))}"
                        f"<figcaption>{escape(library.when(when, False))}</figcaption></figure>")
        html.append("</div>")
    return "\n".join(html)
