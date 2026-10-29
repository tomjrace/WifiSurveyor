"""
The survey as a handful of recommendations: what to change, where, and what users will notice
afterwards. It gathers each point's findings (``advice``), the configuration audit, the located
noise sources and the channel plan into themes (roaming, noise, coverage, capacity, …), each
with its most useful actions first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import advice, analysis, rfplan
from ..rf import spectrum as spec

PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

THEMES = [
    ("roaming", "Roaming", "Moving between access points without drop-outs"),
    ("noise", "Noise and interference", "Cleaner air, so the same signal carries faster rates"),
    ("coverage", "Coverage", "A strong enough signal everywhere people use Wi-Fi"),
    ("capacity", "Channels and capacity", "More airtime for each user when it's busy"),
    ("connection", "Connection quality", "Latency, loss and DNS: what users actually feel"),
    ("security", "Security", "Encryption that protects users without slowing them"),
    ("survey", "Survey completeness", "What would make this survey's conclusions firmer"),
]

# Which theme each of advice's per-point findings belongs to
TITLE_THEMES = {
    "Network not heard": "coverage", "Weak signal": "coverage", "Thin signal margin": "coverage",
    "Only 2.4 GHz reaches": "coverage", "5 GHz too weak": "coverage",
    "Very strong signal": "roaming",
    "No second access point for handover": "roaming", "Single access point coverage": "roaming",
    "Weak second access point": "roaming", "Too much cell overlap": "roaming",
    "Low signal-to-noise": "noise", "Thin SNR margin": "noise", "Raised noise floor": "noise",
    "Non-Wi-Fi energy on the channel": "noise", "Interference source": "noise",
    "Neighbours on overlapping channels": "noise",
    "Co-channel contention": "capacity", "Overlapping 2.4 GHz channel": "capacity",
    "40 MHz on 2.4 GHz": "capacity", "Wide channel in a crowded band": "capacity",
    "Channel too busy": "capacity", "Channel nearly full": "capacity",
    "Beacon overhead": "capacity", "802.11b rates enabled": "capacity",
    "High retry rate": "capacity", "Some retries": "capacity", "Low link rate": "capacity",
    "Open network": "security", "Finder tag away from its owner": "security", "Weak encryption": "security", "No WPA3": "security",
    "Connection failed": "connection", "Packet loss": "connection", "High latency": "connection",
    "Jitter": "connection", "Slow DNS": "connection", "Low throughput": "connection",
    "Throughput test failed": "survey",
    "No scan here": "survey", "Noise not measured": "survey", "No connection check": "survey",
    "Estimated signal levels": "survey", "Gap between measurements": "survey",
}


@dataclass
class Recommendation:
    priority: str                  # High, Medium or Low
    action: str                    # what to do
    why: str                       # what the survey found that calls for it
    benefit: str                   # what users will notice afterwards
    points: list[int] = field(default_factory=list)      # 1-based point numbers it concerns
    steps: list[str] = field(default_factory=list)       # each change to make, one by one


@dataclass
class Theme:
    key: str
    title: str
    aim: str
    status: str                    # "good", "fair" or "poor"
    summary: str
    items: list[Recommendation]


def _plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


def _numbers(points: list[int]) -> str:
    return ", ".join(str(n) for n in points)


def where(project, numbers: list[int]) -> str:
    """Where a group of points is: their middle in feet once the plan has a scale."""
    chosen = [project.points[n - 1] for n in numbers if 0 < n <= len(project.points)]
    if not chosen:
        return ""
    x = sum(p.x for p in chosen) / len(chosen)
    y = sum(p.y for p in chosen) / len(chosen)
    near = sorted(range(len(project.points)),
                  key=lambda i: math.hypot(project.points[i].x - x, project.points[i].y - y))[:2]
    close = " and ".join(str(i + 1) for i in sorted(near))
    across, down = project.feet(x), project.feet(y)
    if across is None:
        return f"near point{'s' if len(near) > 1 else ''} {close}"
    return (f"around {across:.0f} ft across and {down:.0f} ft down the plan "
            f"(near point{'s' if len(near) > 1 else ''} {close})")


def build(project, ctx: analysis.Context, ssid: str | None, band: str,
          todo: list[list[advice.Improvement]], sites: list[dict],
          allow_dfs: bool = False) -> list[Theme]:
    """Every theme, most troubled first, each with its recommendations, most important first."""
    total = len(project.points)
    found: dict[str, dict[str, list[int]]] = {}         # title -> severity -> point numbers
    networks: dict[str, set[str]] = {}                  # title -> the networks it concerns
    for number, items in enumerate(todo, start=1):
        for item in items:
            found.setdefault(item.title, {}).setdefault(item.severity, []).append(number)
            if item.network:
                networks.setdefault(item.title, set()).add(item.network)

    def at(*titles: str, severities=("High", "Medium", "Low")) -> list[int]:
        numbers = {n for title in titles for severity in severities
                   for n in found.get(title, {}).get(severity, [])}
        return sorted(numbers)

    audit = rfplan.audit(project)
    names = sorted({n["ssid"] for p in project.points for n in p.networks if n["ssid"]})
    ours = [ssid] if ssid else main_networks(project)
    builders = {
        "roaming": lambda: _roaming(project, ctx, at, audit, ours, total),
        "noise": lambda: _noise(project, ctx, at, total),
        "coverage": lambda: _coverage(project, ctx, at, sites, total,
                                      sorted(networks.get("Only 2.4 GHz reaches", set())
                                             | networks.get("5 GHz too weak", set()))),
        "capacity": lambda: _capacity(project, ctx, at, audit, ours, allow_dfs, total, sites),
        "connection": lambda: _connection(project, at, ours, total),
        "security": lambda: _security(project, at, names, ours),
        "survey": lambda: _survey(project, at, total),
    }
    themes = []
    for key, title, aim in THEMES:
        items, summary = builders[key]()
        items.sort(key=lambda r: (PRIORITY_ORDER.get(r.priority, 9), -len(r.points)))
        status = ("poor" if any(r.priority == "High" for r in items)
                  else "fair" if any(r.priority == "Medium" for r in items) else "good")
        themes.append(Theme(key, title, aim, status, summary, items))
    order = {"poor": 0, "fair": 1, "good": 2}
    survey = [t for t in themes if t.key == "survey"]
    return sorted((t for t in themes if t.key != "survey"), key=lambda t: order[t.status]) + survey


def top_actions(themes: list[Theme], limit: int = 6) -> list[tuple[Theme, Recommendation]]:
    """The most useful changes across every theme except the survey's own gaps."""
    pairs = [(theme, item) for theme in themes if theme.key != "survey" for item in theme.items
             if item.priority != "Low"]
    pairs.sort(key=lambda pair: (PRIORITY_ORDER.get(pair[1].priority, 9), -len(pair[1].points)))
    return pairs[:limit]


def main_networks(project, limit: int = 3) -> list[str]:
    """
    With no network chosen, the survey's focus networks, or failing those the ones heard at
    the most points: most likely the site's own.
    """
    if getattr(project, "focus_networks", None):
        return list(project.focus_networks)
    counts: dict[str, int] = {}
    for point in project.points:
        for name in {n["ssid"] for n in point.networks if n["ssid"]}:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts, key=lambda name: -counts[name])
    return [name for name in ranked if counts[name] >= max(1, len(project.points) // 2)][:limit]


def _share(points: list[int], total: int) -> str:
    return f"{len(points)} of {total} point{'s' if total != 1 else ''}"


# ---------- the themes ----------

def _roaming(project, ctx, at, audit, ours, total):
    items: list[Recommendation] = []
    profile = ctx.profile
    lonely = at("No second access point for handover", "Single access point coverage")
    if lonely:
        needed = profile.secondary is not None
        items.append(Recommendation(
            "High" if needed or len(lonely) * 3 >= total else "Medium",
            f"Add overlap from a second access point {where(project, lonely)}: aim for a second "
            f"access point at {profile.secondary or -75:.0f} dBm or better wherever people walk "
            "while connected.",
            f"{_share(lonely, total)} are covered by only one access point, so a client there "
            "has nowhere to hand over to, and loses Wi-Fi entirely if that access point is busy "
            "or restarts.",
            "Calls and video keep going while walking between areas, and one access point "
            "failing no longer leaves a dead zone.", lonely))
    weak = at("Weak second access point")
    if weak:
        items.append(Recommendation(
            "Medium" if profile.secondary is not None else "Low",
            "Increase overlap slightly (a second access point at -70 to -67 dBm) by moving an "
            f"access point toward {where(project, weak)} or raising the neighbouring one's power "
            "a step.",
            f"At {_share(weak, total)} the second access point is below -75 dBm, so clients "
            "cling to the first one until the signal is poor.",
            "Clients hand over earlier, before the connection has degraded.", weak))
    crowded = at("Too much cell overlap")
    if crowded:
        items.append(Recommendation(
            "Medium",
            "Turn transmit power down on the access points that overlap most, and switch off "
            "the 2.4 GHz radio on every other access point.",
            f"At {_share(crowded, total)} four or more of the network's own radios are heard at "
            "-67 dBm or better on one band.",
            "Less hopping between access points, and more airtime, because the network's own "
            "radios stop competing with each other.", crowded))
    sticky = at("Very strong signal")
    if sticky:
        items.append(Recommendation(
            "Low",
            "Don't run access points at maximum transmit power; match the power to what the "
            "clients can send back (phones transmit at about 15 dBm).",
            f"Signals above -35 dBm at {_share(sticky, total)} suggest access points near full "
            "power.",
            "Fewer 'sticky' clients that stay connected to a distant access point with a poor "
            "uplink.", sticky))

    # Roaming assistance: what each network's access points advertise in their beacons
    missing: dict[str, list[str]] = {}          # feature -> networks without it anywhere
    partial: dict[str, list[str]] = {}          # feature -> networks with it on some only
    for name in ours:
        networks = [n for p in project.points for n in p.networks if n["ssid"] == name]
        radios = {rfplan.radio_key(n) for n in networks}
        if len(radios) < 2 or not any("roam_k" in n for n in networks):
            continue
        for flag, label in (("roam_k", "802.11k"), ("roam_v", "802.11v"), ("roam_r", "802.11r")):
            have = [bool(n.get(flag)) for n in networks]
            if not any(have):
                missing.setdefault(label, []).append(name)
            elif not all(have):
                partial.setdefault(label, []).append(name)
    if missing.get("802.11k") or missing.get("802.11v"):
        names = sorted(set(missing.get("802.11k", []) + missing.get("802.11v", [])))
        items.append(Recommendation(
            "Medium",
            f"Turn on 802.11k and 802.11v for {', '.join(names)}.",
            "802.11k gives clients a list of neighbouring access points; 802.11v lets the network "
            "suggest a better one. Without them each client guesses when and where to move.",
            "Clients move to a better access point sooner instead of hanging on to a weak one."))
    if missing.get("802.11r"):
        names = missing["802.11r"]
        items.append(Recommendation(
            "Medium" if profile.secondary is not None else "Low",
            f"Turn on 802.11r (fast transition) for {', '.join(names)}, in adaptive or mixed "
            "mode so older clients still connect.",
            f"{', '.join(names)} advertise{'s' if len(names) == 1 else ''} no 802.11r, so every "
            "hand-over repeats the full key exchange.",
            "Hand-overs take tens of milliseconds instead of hundreds: no audible gap on calls."))
    if partial:
        detail = "; ".join(f"{label} on some {', '.join(names)} access points only"
                           for label, names in sorted(partial.items()))
        items.append(Recommendation(
            "Low", "Make the roaming settings (802.11k/v/r) the same on every access point of "
            "each network.", detail[0].upper() + detail[1:] + ".",
            "Roaming behaves the same everywhere instead of depending on which access point a "
            "client is leaving."))
    for finding in audit:
        if finding.title in ("Security differs between access points",
                             "Basic rates differ between access points"):
            items.append(Recommendation(
                "Medium" if finding.severity == "Medium" else "Low",
                "Use one configuration profile for every access point of the network.",
                finding.detail,
                "Clients move between access points without re-authenticating failures or "
                "uneven cell edges."))
    if items:
        summary = f"{len(items)} change{'s' if len(items) != 1 else ''} would make moving around smoother."
    else:
        second = [rfplan.nth_strongest(p, 2, ours[0] if ours else None) for p in project.points]
        heard = [s for s in second if s is not None]
        summary = ("Every point has a second access point"
                   + (f" (weakest {min(heard)} dBm)" if heard else "")
                   + "; keep that overlap when adding or moving access points.")
    return items, summary


def _noise(project, ctx, at, total):
    items: list[Recommendation] = []
    serving = _serving_channels(project)
    fleeting = []
    for source in analysis.noise_sources(project, ctx):
        located = source.get("located")
        clash = [c for c in source["channels"] if (spec.band_of(source["center"]), c) in serving]
        if source["points"] < 2 and not clash:
            fleeting.append(source)       # heard once and off the network's channels: a footnote
            continue
        if located and not located["outside"]:
            nearest = min(range(total), key=lambda i: math.hypot(project.points[i].x - located["x"],
                                                                  project.points[i].y - located["y"]))
            place = (f"near point {nearest + 1}"
                     + (f", about {project.feet(math.hypot(project.points[nearest].x - located['x'], project.points[nearest].y - located['y'])):.0f} ft from it"
                        if project.meters_per_pixel else "")
                     + (" (a rough fix)" if located["rough"] else ""))
            points = [nearest + 1]
        elif located:
            place, points = "probably outside the surveyed area", []
        else:
            place, points = f"heard at {_plural(source['points'], 'point')}, too few to place", []
        channels = source["channels"]
        on = (f"channel {channels[0]}" if len(channels) == 1
              else f"channels {channels[0]}–{channels[-1]}" if channels else "between channels")
        items.append(Recommendation(
            "High" if clash else "Medium" if source["kind"] in ("continuous", "microwave", "wideband")
            else "Low",
            f"Find the {source['name'].lower()} {place}, then move it, shield it or switch it "
            f"off; until then keep Wi-Fi off {BAND(source['band'])} {on}.",
            f"{source['name']} at {source['center']:.0f} MHz, about {source['width']:.0f} MHz "
            f"wide, peak {source['peak']:.0f} dBm. {source['description']}"
            + (f" It sits on channel {', '.join(str(c) for c in clash)}, which the network uses."
               if clash else ""),
            "Fewer retries and more stable speeds on the channels it covers.", points))
    if fleeting:
        items.append(Recommendation(
            "Low", "Nothing to do yet about brief emissions seen at a single point; re-measure "
            "there if users report trouble.",
            "Seen once: " + "; ".join(f"{s['name'].lower()} at {s['center']:.0f} MHz "
                                      f"(peak {s['peak']:.0f} dBm)" for s in fleeting) + ".",
            "Knowing what's normal here makes a new source easy to spot later."))
    raised = at("Raised noise floor")
    if raised:
        items.append(Recommendation(
            "Medium",
            f"Look for electronics {where(project, raised)}: microwave ovens, cordless phones, "
            "wireless cameras and video senders, LED lighting drivers, USB 3 hubs and "
            "unshielded cables all raise the floor.",
            f"The noise floor is well above a quiet -90 dBm at {_share(raised, total)}.",
            "Each dB of noise removed is a dB of signal gained: faster rates at the same "
            "distance.", raised))
    snr = at("Low signal-to-noise", "Thin SNR margin")
    if snr:
        high = at("Low signal-to-noise")
        items.append(Recommendation(
            "High" if high else "Medium",
            "Raise signal-to-noise to 25 dB or more: a closer access point, or a cleaner "
            "channel (see the quietest channels below).",
            f"Signal-to-noise is below or near the target at {_share(snr, total)}.",
            "Higher data rates, fewer retries, better call quality.", snr))
    alien = at("Non-Wi-Fi energy on the channel")
    if alien:
        items.append(Recommendation(
            "Medium", "Change the serving channel at these points to one without unexplained "
            "energy, or track the source with the Unexplained energy map.",
            f"Energy that no network accounts for sits on the serving channel at "
            f"{_share(alien, total)}.",
            "Clients stop deferring to something that never yields the air.", alien))
    neighbours = at("Neighbours on overlapping channels")
    if neighbours:
        items.append(Recommendation(
            "Low", "Keep 2.4 GHz on channels 1, 6 and 11 only, and ask neighbours on other "
            "channels to do the same.",
            f"Neighbouring networks on off-grid 2.4 GHz channels are strong at "
            f"{_share(neighbours, total)}.",
            "Less interference that can't be shared politely.", neighbours))
    quiet = quietest_channels(project)
    if quiet:
        text = "; ".join(f"{BAND(b)}: {', '.join(f'ch {c} ({busy:.0f}% busy)' for c, busy in chans)}"
                         for b, chans in quiet.items())
        items.append(Recommendation(
            "Low", f"When re-planning channels, prefer the quietest measured: {text}.",
            "From the spectrum analyzer's occupancy across every point that swept.",
            "Less time waiting for the air, before any other change."))
    measured = sum(1 for p in project.points if p.spectrum)
    real = [r for r in items if r.priority != "Low" or r.points]
    summary = (f"{_plural(len(real), 'noise problem')} to deal with." if real
               else f"No noise problems found at the {_plural(measured, 'point')} with a spectrum sweep."
               if measured else "No spectrum sweeps, so noise couldn't be judged.")
    return items, summary


def _coverage(project, ctx, at, sites, total, slow_networks=()):
    items: list[Recommendation] = []
    profile = ctx.profile
    weak = at("Network not heard", "Weak signal")
    if weak:
        items.append(Recommendation(
            "High",
            f"Add an access point {where(project, weak)}, or move the nearest one toward it."
            + _reach_hint(project, ctx, sites),
            f"The signal is below the {profile.name} target of {profile.primary:.0f} dBm at "
            f"{_share(weak, total)}.",
            "Pages load and calls connect reliably instead of stalling or dropping.", weak))
    thin = at("Thin signal margin")
    if thin:
        items.append(Recommendation(
            "Medium",
            f"Get 5–10 dB more signal {where(project, thin)}: move an access point closer, "
            "re-aim directional antennas, or clear metal and mirrors from the path.",
            f"The signal clears the target by under 5 dB at {_share(thin, total)}; phones "
            "(weaker antennas than the survey adapter) and people in the way push it below.",
            "The same experience on a phone as on a laptop, and when the room is full.", thin))
    slow_band = at("Only 2.4 GHz reaches", "5 GHz too weak")
    if slow_band:
        items.append(Recommendation(
            "Medium",
            f"Bring 5 GHz coverage{' for ' + ', '.join(slow_networks) if slow_networks else ''} "
            f"{where(project, slow_band)}: an access point nearer, or one "
            "fewer wall in between. Once it reaches, band steering will move dual-band clients "
            "onto it.",
            "Only 2.4 GHz is usable"
            + (f" for {', '.join(slow_networks)}" if slow_networks else "")
            + f" at {_share(slow_band, total)}, though the network has 5 GHz radios elsewhere.",
            "Several times the speed, on a band with far more clean channels.", slow_band))
    summary = (f"{_share(weak, total)} below target, {len(thin)} with a thin margin."
               if weak or thin else
               f"Every point clears the {profile.primary:.0f} dBm target by 5 dB or more.")
    return items, summary


def _reach_hint(project, ctx, sites) -> str:
    reaches = [advice.reach_feet(fit, ctx.profile.primary, project.meters_per_pixel)
               for site in sites for fit in site.get("fits", []) if not fit.get("rough")]
    reaches = [r for r in reaches if r]
    if not reaches:
        return ""
    reaches.sort()
    typical = reaches[len(reaches) // 2]
    return (f" Measured access points here reach about {typical:.0f} ft in the open, less "
            "through walls, so plan spacing around that.")


def radio_whereabouts(project, sites, radio: rfplan.Radio) -> tuple[int | None, int | None, int | None]:
    """
    Where to find one radio: the number of the access point it belongs to in the report's
    Access points table (None if it wasn't located), and the point it's heard strongest at,
    with that level.
    """
    bssids = {b.lower() for b in radio.bssids if b}
    site = next((i + 1 for i, s in enumerate(sites)
                 if bssids & {b.lower() for b in s.get("bssids", [])}), None)
    strongest = None
    for number, point in enumerate(project.points, start=1):
        for other in rfplan.radios(point.networks):
            if other.key == radio.key and (strongest is None or other.rssi > strongest[1]):
                strongest = (number, other.rssi)
    return (site, *(strongest or (None, None)))


def channel_steps(project, sites, names: list[str], plan: list[rfplan.Suggestion]) -> list[str]:
    """Each channel change of a plan as a step someone can find the radio for and make."""
    steps = []
    for item in plan:
        if not item.changed:
            continue
        radio = item.radio
        site, number, rssi = radio_whereabouts(project, sites, radio)
        bssid = sorted(b.upper() for b in radio.bssids if b)[:1]
        which = (f"Access point {site}" if site else "The access point") + (
            f" (BSSID {bssid[0]})" if bssid else "")
        found = f", heard strongest at point {number} ({rssi} dBm)" if number else ""
        others = " and ".join(n for n in names if n in radio.ssids) or names[0]
        heard = (f"Points hear it with another {others} radio on its channel "
                 f"{_plural(item.clashes_before, 'time')} now, {item.clashes_after} after "
                 "the changes")
        if not item.clashes_before and not item.clashes_after:
            why = (f"It doesn't share a channel with another {others} radio either way, but "
                   "fewer other networks are on its new channel where it serves")
        elif item.clashes_after < item.clashes_before:
            why = heard
        else:
            why = heard + ": it moves to make room for radios that clash more"
        steps.append(f"{which}{found}: move its {radio.band} GHz radio from "
                     f"{item.current.replace('ch ', 'channel ')} to "
                     f"{item.suggested.replace('ch ', 'channel ')}. {why}.")
    return steps


def _capacity(project, ctx, at, audit, ours, allow_dfs, total, sites=()):
    items: list[Recommendation] = []
    planned = rfplan.plans(project, ours, allow_dfs)
    for name, also, plan in planned:
        moves = [s for s in plan if s.changed]
        if moves:
            # Each clash is counted from both radios' side
            before = sum(s.clashes_before for s in plan) // 2
            after = sum(s.clashes_after for s in plan) // 2
            names = [name, *also]
            items.append(Recommendation(
                "High" if before - after >= max(4, before / 3) else "Medium",
                f"Apply the suggested channel plan for {' and '.join(names)}: change the "
                f"channel on {_plural(len(moves), 'radio')} of {len(plan)}, as listed. Make "
                "every change together, since some only help once the others are made, and "
                "set fixed channels so automatic channel selection doesn't move them back.",
                f"Two of the network's radios on the same channel, heard together above "
                f"{rfplan.CCA_DBM} dBm, have to take turns to transmit. That happens "
                f"{_plural(before, 'time')} across the survey's points now "
                f"(a point hearing three counts each pair), and {after or 'none'} after the "
                "change. The full report's Suggested channel plan table lists every radio.",
                "Neighbouring access points stop taking turns with each other, so each gets "
                "more airtime.",
                steps=channel_steps(project, sites, names, plan)))
    shared = at("Co-channel contention")
    if shared:
        items.append(Recommendation(
            "Medium" if len(shared) * 3 >= total else "Low",
            "Give neighbouring access points different channels"
            + (" (see the suggested channel plan)" if planned else "")
            + ", and use narrower channels (40 MHz, or 20 MHz on 2.4 GHz) where the band is "
            "crowded.",
            f"The serving channel is shared with other radios at {_share(shared, total)}.",
            "More airtime per user at busy times.", shared))
    busy = at("Channel too busy", "Channel nearly full")
    if busy:
        items.append(Recommendation(
            "High" if at("Channel too busy") else "Medium",
            f"Split the load {where(project, busy)}: another access point, a quieter channel, "
            "or moving heavy users (streaming, backups) to wired or 5/6 GHz.",
            f"The channel is busy most of the time at {_share(busy, total)}.",
            "Less lag at busy times; everyone waits less for the air.", busy))
    actions = {
        "802.11b rates still required": "Disable the 1, 2, 5.5 and 11 Mbps rates (set the minimum "
                                        "basic rate to 12 Mbps if every client is modern).",
        "Many SSIDs on one radio": "Merge SSIDs (e.g. staff and IoT) using VLANs or per-user "
                                   "policies; keep to three or four per radio.",
        "40 MHz on 2.4 GHz": "Set 2.4 GHz channels to 20 MHz.",
        "Overlapping 2.4 GHz channel": "Move 2.4 GHz radios to channels 1, 6 or 11.",
        "Beacon overhead": "Reduce the number of SSIDs and raise the minimum basic rate.",
    }
    merged: dict[str, list] = {}                # one recommendation however many radios
    for finding in audit:
        if finding.title in actions:
            merged.setdefault(finding.title, []).append(finding)
    for title, findings in merged.items():
        first = findings[0]
        radios = [f.detail.split(":", 1)[0] for f in findings]
        detail = (first.detail if len(findings) == 1 else
                  f"{len(findings)} radios ({'; '.join(radios[:4])}{'…' if len(radios) > 4 else ''}): "
                  + first.detail.split(":", 1)[-1].strip())
        items.append(Recommendation(
            "Medium" if any(f.severity in ("High", "Medium") for f in findings) else "Low",
            actions[title], detail, "Airtime returned to real traffic: faster for everyone."))
    retries = at("High retry rate", "Some retries")
    if retries:
        items.append(Recommendation(
            "High" if at("High retry rate") else "Medium",
            "Look for a hidden node or interference at these points: check channel overlap and "
            "the noise findings.",
            f"Many captured frames are retries at {_share(retries, total)}.",
            "Retries eat airtime and add latency; fixing them speeds up everyone nearby.",
            retries))
    slow = at("Low link rate")
    if slow:
        items.append(Recommendation(
            "Medium", "Check channel width, legacy rates and the client driver where the link "
            "rate is low despite the signal.",
            f"The link negotiated a low rate at {_share(slow, total)}.",
            "Downloads and uploads closer to what the signal allows.", slow))
    summary = (f"{_plural(len(items), 'change')} would give users more airtime." if items
               else "No channel sharing, busy channels or airtime waste found.")
    return items, summary


def _connection(project, at, ours, total):
    items: list[Recommendation] = []
    failed = at("Connection failed")
    if failed:
        # Which network each check was on, and what went wrong there
        by_network: dict[tuple[str, str], tuple[set[str], list[int]]] = {}
        for number in failed:
            for result in project.points[number - 1].checks_made():
                if not result.get("error") or "isn't connected" in result["error"]:
                    continue
                bssids, numbers = by_network.setdefault(
                    (result.get("ssid") or "", result["error"]), (set(), []))
                if result.get("bssid"):
                    bssids.add(result["bssid"].upper())
                numbers.append(number)
        found = "; ".join(
            f"{ssid or 'an unnamed network'}"
            + (f" ({', '.join(sorted(bssids))})" if bssids else "")
            + f" at point{'s' if len(numbers) != 1 else ''} {_numbers(numbers)}: {error}"
            for (ssid, error), (bssids, numbers) in by_network.items())
        names = sorted({ssid for ssid, _error in by_network if ssid})
        action = ("Check association, DHCP and the gateway on "
                  f"{', '.join(names) or 'the network'} where the connection check failed.")
        if (names and not getattr(project, "focus_networks", None)
                and not (getattr(project, "survey_setup", None) or {}).get("join")):
            action += (f" The survey didn't join a network itself, so the check ran on whatever "
                       f"this computer had connected to: {', '.join(names)}. If that isn't "
                       "your network, choose the networks to test under Measuring → Networks "
                       "to survey; connect to each once from the system's Wi-Fi settings so its "
                       "password is saved.")
        items.append(Recommendation(
            "High", action,
            f"The adapter couldn't get a working connection at {_share(failed, total)}: {found}.",
            "Devices that connect and stay connected.", failed))
    lossy = at("Packet loss", "High latency", "Jitter")
    if lossy:
        items.append(Recommendation(
            "High" if at("Packet loss", severities=("High",)) or at("High latency", severities=("High",))
            else "Medium",
            "Treat loss, latency and jitter as air problems first: the fixes under Channels and "
            "Noise usually resolve them. If they persist on a clean channel, check the "
            "access point's uplink and switch port.",
            f"Loss, latency above 20 ms or jitter above 10 ms to the gateway at "
            f"{_share(lossy, total)}.",
            "Smooth voice and video calls, and snappier apps.", lossy))
    slow = at("Low throughput")
    if slow:
        items.append(Recommendation(
            "High" if at("Low throughput", severities=("High",)) else "Medium",
            f"Raise throughput {where(project, slow)}: the signal, noise and channel fixes "
            "above come first; then check the access point's wired uplink and PoE budget.",
            f"Measured throughput is low at {_share(slow, total)}.",
            "Faster downloads, uploads and video; fewer complaints of 'the Wi-Fi is slow'.", slow))
    dns = at("Slow DNS")
    if dns:
        items.append(Recommendation(
            "Low", "Point DHCP at a faster DNS resolver, or enable caching on the gateway.",
            f"DNS lookups took 150 ms or more at {_share(dns, total)}.",
            "Every new page and app request starts faster.", dns))
    checked = sum(1 for p in project.points if any("latency_ms" in c for c in p.checks_made()))
    summary = ("Connection problems found." if items else
               f"Latency, loss and DNS were good at all {checked} checked points." if checked
               else "No connection checks were made, so this couldn't be judged.")
    return items, summary


def _security(project, at, names, ours):
    items: list[Recommendation] = []
    opened = sorted({n["ssid"] for p in project.points for n in p.networks
                     if n["ssid"] and (n.get("secured") is False
                                       or (n.get("security") or "").upper() == "OPEN")})
    for name in opened:
        mine = name in ours
        guest = "guest" in name.lower()
        items.append(Recommendation(
            ("Medium" if guest else "High") if mine else "Low",
            f"Protect {name}: WPA2/WPA3 with a password, or Enhanced Open (OWE) if it must stay "
            "open for guests." + ("" if mine else " (If it's not yours, nothing to do.)"),
            f"{name} is an open network: anyone nearby can read unencrypted traffic"
            + (" (common for guest networks, but OWE encrypts it with no password)."
               if guest else "."),
            "Users' traffic can't be read by others nearby."))
    weak = at("Weak encryption")
    if weak:
        items.append(Recommendation(
            "High", "Replace WEP/WPA (TKIP) with WPA2-AES or WPA3.",
            f"Broken encryption on the serving network at {_share(weak, len(project.points))}.",
            "Real protection, and 802.11n/ac/ax speeds, which TKIP blocks.", weak))
    if at("No WPA3") and not weak:
        items.append(Recommendation(
            "Low", "Enable WPA3 (SAE) in transition mode alongside WPA2.",
            "The serving network offers WPA2-Personal only.",
            "Stronger protection against password guessing, with older devices still "
            "connecting."))
    summary = ("Encryption issues found." if any(r.priority != "Low" for r in items)
               else "The site's networks are encrypted.")
    return items, summary


def _survey(project, at, total):
    items: list[Recommendation] = []
    if not project.meters_per_pixel:
        items.append(Recommendation(
            "Medium", "Set the plan's scale (Set scale on the survey page).",
            "Without it there are no distances: no access point reach, no placement "
            "suggestions in feet, and no check on spacing between measurements.",
            "Recommendations that say exactly where to put things."))
    gaps = at("Gap between measurements")
    if gaps:
        items.append(Recommendation(
            "Low", "Measure more points where they're far apart, and at least one in every "
            "room people use.",
            f"{_share(gaps, total)} are more than {advice.GAP_FEET} ft from their nearest "
            "neighbour.",
            "A map you can trust between points.", gaps))
    no_noise = at("Noise not measured")
    if no_noise:
        items.append(Recommendation(
            "Low", "Re-measure with the spectrum analyzer connected.",
            f"No spectrum sweep at {_share(no_noise, total)}, so noise and non-Wi-Fi "
            "interference there are unknown.", "Noise problems can be found, not guessed.",
            no_noise))
    no_link = at("No connection check")
    if no_link:
        items.append(Recommendation(
            "Low", "Connect the adapter to the network before measuring, so each point gets "
            "a latency, loss and DNS check.",
            f"No connection check at {_share(no_link, total)}.",
            "Confirms each spot is usable, not only strong.", no_link))
    estimated = at("Estimated signal levels")
    if estimated:
        items.append(Recommendation(
            "Low", "Run the app as root for exact signal levels.",
            f"Levels at {_share(estimated, total)} are estimated from NetworkManager's 0–100 "
            "quality.", "Exact numbers behind every recommendation.", estimated))
    summary = ("A few additions would make these conclusions firmer." if items
               else "The survey is complete enough to rely on.")
    return items, summary


# ---------- helpers ----------

def BAND(band: str) -> str:
    return {"2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}.get(band, band)


def _serving_channels(project) -> set[tuple[str, int]]:
    """Every (band, channel) some point is served on: the strongest network there."""
    serving = set()
    for point in project.points:
        best = point.best()
        if best and best.get("channel"):
            serving.add((best.get("band"), best["channel"]))
    return serving


def quietest_channels(project, per_band: int = 3) -> dict[str, list[tuple[int, float]]]:
    """
    The least-occupied channels across every point that swept: 1, 6 and 11 on 2.4 GHz, and the
    20 MHz channels of 5 GHz (without DFS when a non-DFS one is as quiet).
    """
    sums: dict[tuple[str, int], list[float]] = {}
    for point in project.points:
        for number, center in spec.all_channels():
            band = spec.band_of(center)
            if band == "2.4" and number not in (1, 6, 11):
                continue
            segment = spec.segment_for(point.spectrum, center)
            occupancy = (segment or {}).get("occupancy") or {}
            value = occupancy.get(str(number), occupancy.get(number))
            if value is not None:
                sums.setdefault((band, number), []).append(value * 100)
    found: dict[str, list[tuple[int, float]]] = {}
    for (band, number), values in sums.items():
        found.setdefault(band, []).append((number, sum(values) / len(values)))
    return {band: sorted(chans, key=lambda c: (round(c[1]), 52 <= c[0] <= 144, c[0]))[:per_band]
            for band, chans in sorted(found.items(), key=lambda kv: analysis.BAND_ORDER.get(kv[0], 9))
            if band != "6" or chans}
