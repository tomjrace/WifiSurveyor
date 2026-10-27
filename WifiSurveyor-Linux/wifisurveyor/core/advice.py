"""
What could be better at each measured spot. Unlike ``analysis.diagnose``, which names the one
thing limiting a spot, this checks everything and keeps going, so a spot that passes still
shows where its margin is thin and what would strengthen it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import analysis, rfplan
from ..rf import iq
from ..rf import spectrum as spec

SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}
GAP_FEET = 35             # measurements further apart than this leave the map guessing between them
QUIET_FLOOR = -90         # a quiet noise floor per 20 MHz; much above it, something is adding noise


@dataclass
class Improvement:
    severity: str          # High, Medium or Low
    title: str             # a short name for the kind of problem, the same wherever it's found
    issue: str             # what was found here
    fix: str               # what to do about it
    network: str = ""      # the network it concerns, when it's about one


def quality(found: list[Improvement]) -> str:
    """"poor", "fair" or "good" for a spot, from its most serious finding."""
    severities = {item.severity for item in found}
    return "poor" if "High" in severities else "fair" if "Medium" in severities else "good"


def _ssids(radios) -> str:
    names = sorted({name for radio in radios for name in radio.ssids if name})
    hidden = any(not radio.ssids - {""} for radio in radios)
    return ", ".join(names + (["a hidden network"] if hidden else [])) or "a hidden network"


def _distance_ft(project, x1, y1, x2, y2) -> float | None:
    return project.feet(math.hypot(x2 - x1, y2 - y1))


def channel_name(network: dict) -> str:
    """"channel 36", or the frequency when the channel number isn't known."""
    if network.get("channel"):
        return f"channel {network['channel']}"
    return f"{float(network.get('freq_mhz') or 0):.0f} MHz"


def nearest_point(project, point) -> tuple[int, float] | None:
    """The closest other measurement point, as (index, plan pixels)."""
    best = None
    for index, other in enumerate(project.points):
        if other is point:
            continue
        distance = math.hypot(other.x - point.x, other.y - point.y)
        if best is None or distance < best[1]:
            best = (index, distance)
    return best


def reach_feet(fit: dict, target: float, mpp: float | None) -> float | None:
    """
    How far an access point's signal stays at or above ``target`` in the open, from the path
    loss fitted to the survey (received = P0 - 10 n log10(metres) - walls). Walls shorten it.
    """
    if not mpp or not fit or fit.get("exponent", 0) <= 0:
        return None
    metres = 10 ** ((fit["p0"] - target) / (10 * fit["exponent"]))
    return metres / 0.3048


def nearest_site(project, point, sites: list[dict] | None, ssid: str | None) -> dict | None:
    """The located access point nearest this spot that carries ``ssid`` (any, if None)."""
    best = None
    for site in sites or []:
        if site.get("outside") or (site.get("guess") and not site.get("placed")):
            continue                  # a guess isn't good enough to quote distances from
        if ssid and not any(name == ssid for name, _bands in site["networks"]):
            continue
        distance = math.hypot(site["x"] - point.x, site["y"] - point.y)
        if best is None or distance < best[0]:
            best = (distance, site)
    if best is None:
        return None
    distance, site = best
    walls = [w for w in project.walls
             if analysis._crosses(point.x, point.y, site["x"], site["y"], w.x1, w.y1, w.x2, w.y2)]
    return {"site": site, "pixels": distance, "feet": project.feet(distance),
            "walls": len(walls), "wall_db": sum(w.db for w in walls)}


def improvements(point, ctx: analysis.Context, ssid: str | None = None, band: str = "All",
                 sites: list[dict] | None = None) -> list[Improvement]:
    """Everything worth improving at one spot, most serious first."""
    project, profile = ctx.project, ctx.profile
    found: list[Improvement] = []

    def add(severity: str, title: str, issue: str, fix: str, network: str = "") -> None:
        found.append(Improvement(severity, title, issue, fix, network))

    if not point.networks:
        add("High", "No scan here", "No Wi-Fi scan was recorded at this point.",
            "Measure here again: without a scan nothing else can be judged.")
        return found

    best = point.best(ssid, None, band)
    name = ssid or (best or {}).get("ssid") or "the strongest network"
    where = nearest_site(project, point, sites, ssid or (best or {}).get("ssid"))
    where_text = ""
    if where and where["feet"] is not None:
        label = where["site"].get("name")
        placed = "" if where["site"].get("placed") else "located "
        where_text = (f" The nearest {placed}{name} access point"
                      + (f" ({label})" if label else "")
                      + f" is about {where['feet']:.0f} ft away" + (f", through {where['walls']} marked wall"
                                 f"{'s' if where['walls'] != 1 else ''} (≈{where['wall_db']:.0f} dB)"
                                 if where["walls"] else "") + ".")

    # ----- coverage -----
    if best is None:
        add("High", "Network not heard", f"{name} wasn't heard here at all"
            + ("" if band == "All" else f" on {band} GHz") + "." + where_text,
            "Add an access point that covers this area, or move the nearest one closer.")
        _general(point, ctx, add)
        return _sorted(found)

    rssi = best["rssi"]
    target = profile.primary
    if rssi < target:
        add("High", "Weak signal",
            f"Signal is {rssi} dBm, {target - rssi:.0f} dB short of the {profile.name} target of "
            f"{target:.0f} dBm. Expect slow rates, retries and drop-outs." + where_text,
            "Move an access point closer, add one to cover this area, or remove what's blocking "
            "it (metal shelving, mirrors, brick or concrete).")
    elif rssi < target + 5:
        add("Medium", "Thin signal margin",
            f"Signal is {rssi} dBm, only {rssi - target:.0f} dB above the {target:.0f} dBm target. "
            "People, closed doors and phones (whose antennas are typically 3–6 dB weaker than a "
            "survey adapter) will often push it below." + where_text,
            "Aim for 5–10 dB more: move the access point nearer this area, point its antennas "
            "this way, or add one.")
    elif rssi > -35:
        add("Low", "Very strong signal",
            f"Signal is {rssi} dBm: very close to an access point.",
            "Strong here, but if that access point runs at full power, clients elsewhere can "
            "cling to it too long. Check it isn't set to maximum transmit power.")

    _bands(point, ctx, best, name, add)
    _noise(point, ctx, best, add)
    _identified(point, best, add)
    _roaming(point, ctx, ssid or best.get("ssid"), band, best, add)
    _channel(point, ctx, ssid or best.get("ssid"), band, best, add)
    _security(best, add)
    _general(point, ctx, add)
    return _sorted(found)


def _sorted(found: list[Improvement]) -> list[Improvement]:
    return sorted(found, key=lambda item: SEVERITY_ORDER.get(item.severity, 9))


def _bands(point, ctx, best, name, add) -> None:
    """A dual-band network that only reaches here on 2.4 GHz leaves clients on the slow band."""
    ssid = best.get("ssid")
    if not ssid:
        return
    here = {n.get("band"): n["rssi"] for n in sorted(point.networks, key=lambda n: n["rssi"])
            if n["ssid"] == ssid}
    elsewhere = {n.get("band") for p in ctx.project.points for n in p.networks if n["ssid"] == ssid}
    faster = [b for b in ("5", "6") if b in elsewhere]
    if faster and not any(b in here for b in faster):
        add("Medium", "Only 2.4 GHz reaches",
            f"{name} reaches here only on 2.4 GHz ({here.get('2.4')} dBm), though it has "
            f"{' and '.join(f'{b} GHz' for b in faster)} radios elsewhere.",
            "2.4 GHz is slower and far more crowded. Bring a 5 GHz radio closer so clients can use "
            "the faster band here.", ssid)
    elif "5" in here and "2.4" in here and here["5"] < ctx.profile.primary <= here["2.4"]:
        add("Medium", "5 GHz too weak",
            f"{name} is {here['2.4']} dBm on 2.4 GHz but only {here['5']} dBm on 5 GHz, so "
            "clients will fall back to 2.4 GHz here.",
            "5 GHz fades faster through walls: an access point nearer this area, or one fewer "
            "wall between, keeps clients on 5 GHz.", ssid)


# What to do about each kind of transmitter the HackRF can name
IQ_FIXES = {
    "bluetooth": "Bluetooth hops over the whole band, so a different Wi-Fi channel won't escape "
                 "it. Where it's heavy (headsets, scanners, beacons), serve clients on 5 GHz and "
                 "keep Bluetooth hubs away from access points.",
    "zigbee": "Move the Zigbee or Thread network to its channel 25 or 26 (above Wi-Fi channel "
              "11), or move the Wi-Fi off the channel it overlaps.",
    "microwave": "Keep 2.4 GHz radios serving this area off channels 9–11, where ovens are "
                 "worst, or serve it on 5 GHz. A strong one may have a worn door seal.",
    "continuous": "Find it (the noise-source markers show where) and switch it off or move it "
                  "to another band: nothing can share airtime with a carrier that never stops.",
    "video": "Find it (the noise-source markers show where) and replace it with a wired or "
             "5.8 GHz model: an analog video sender or camera never yields the channel.",
    "radar": "Keep this network's radios off this DFS channel: they'll be forced off every time "
             "the radar is heard.",
    "bursty": "Follow the noise-source markers to it, or move to a cleaner channel.",
    "overlap": "Partly overlapping channels hurt both networks more than sharing one: on 2.4 GHz "
               "use only channels 1, 6 and 11, and ask neighbours on 2-5 or 7-10 to move too.",
}


def _identified(point, best, add) -> None:
    """What the HackRF named on the channels it looked at here."""
    center = best.get("freq_mhz") or spec.CHANNELS.get(best["channel"], 0)
    for finding in analysis.iq_interferers(point):
        serving = abs(finding["center_mhz"] - center) < 10 + (finding["width_mhz"] or 0) / 2
        busy = finding["airtime"] >= 0.10 or finding["kind"] in ("continuous", "video", "radar")
        severity = "High" if serving and busy else "Medium" if serving else "Low"
        add(severity, f"{finding['name']} on {'the serving' if serving else 'a nearby'} channel",
            f"The HackRF heard {iq.describe(finding)}"
            + (f", on {channel_name(best)}, the channel this spot is served on" if serving else "")
            + ".",
            IQ_FIXES.get(finding["kind"], IQ_FIXES["bursty"]))


def _noise(point, ctx, best, add) -> None:
    t = ctx.targets
    floor = analysis.noise_floor(point, best["band"])
    offset = ctx.offset(point)
    center = best.get("freq_mhz") or spec.CHANNELS.get(best["channel"], 0)
    unexplained = analysis.residual(point, center, offset) if offset is not None else None
    if floor is None:
        add("Low", "Noise not measured",
            "No spectrum sweep here, so the noise floor and signal-to-noise ratio are unknown.",
            "Connect the spectrum analyzer and measure this point again to confirm the SNR and to spot "
            "non-Wi-Fi interference.")
    else:
        snr = best["rssi"] - floor
        if snr < t.min_snr:
            cause = (f"{unexplained:.0f} dB of the energy on {channel_name(best)} isn't from "
                     "any network, so something non-Wi-Fi is transmitting"
                     if unexplained is not None and unexplained >= analysis.UNEXPLAINED_DB
                     else "other networks' energy is raising the floor")
            add("High", "Low signal-to-noise",
                f"Signal-to-noise is {snr:.0f} dB, below the {t.min_snr:.0f} dB target: {cause}.",
                "Raise the signal (closer access point) or lower the noise: find the source with "
                "the unexplained-energy map, or move to a cleaner channel.")
        elif snr < t.min_snr + 5:
            add("Medium", "Thin SNR margin",
                f"Signal-to-noise is {snr:.0f} dB, only {snr - t.min_snr:.0f} dB above the "
                f"{t.min_snr:.0f} dB target. Higher data rates need 25–30 dB or more.",
                "A few dB more signal, or a quieter channel, would allow faster rates.")
        if floor > QUIET_FLOOR + 5:
            add("Medium", "Raised noise floor",
                f"The {best['band']} GHz noise floor is {floor:.0f} dBm per 20 MHz, "
                f"{floor - QUIET_FLOOR:.0f} dB above a quiet {QUIET_FLOOR} dBm.",
                "Look for nearby electronics: microwaves, cordless phones, wireless cameras, "
                "LED drivers, USB 3 hubs and poorly shielded cables raise the floor.")
    if unexplained is not None and unexplained >= analysis.UNEXPLAINED_DB and (
            floor is None or best["rssi"] - floor >= t.min_snr):
        add("Medium", "Non-Wi-Fi energy on the channel",
            f"{unexplained:.0f} dB more energy on {channel_name(best)} than the networks "
            "heard here account for.",
            "Something non-Wi-Fi is sharing the channel. Follow the unexplained-energy map to it, "
            "or move to a cleaner channel.")
    for emitter in analysis.find_emitters(point, offset)[:3]:
        label, description = analysis.EMITTER_KINDS[emitter["kind"]]
        channels = analysis.channels_touched(emitter["center"], emitter["width"])
        on = (f"channel {channels[0]}" if len(channels) == 1
              else f"channels {channels[0]}–{channels[-1]}" if channels else "between channels")
        serving = best.get("channel") in channels and spec.band_of(emitter["center"]) == best["band"]
        add("High" if serving else "Low", "Interference source",
            f"{label} at {emitter['center']:.0f} MHz ({emitter['width']:.0f} MHz wide, peak "
            f"{emitter['peak']:.0f} dBm) on {on}"
            + (", the channel this spot is served on" if serving else "") + ".",
            description + (" Find and move or switch off the source, or change channel."
                           if serving else " Keep your network off these channels."))


def _roaming(point, ctx, ssid, band, best, add) -> None:
    profile = ctx.profile
    second = rfplan.nth_strongest(point, 2, ssid, band)
    if profile.secondary is not None and (second is None or second < profile.secondary):
        add("High", "No second access point for handover",
            f"The second access point is {'not heard' if second is None else f'{second} dBm'}; "
            f"{profile.name} needs one at {profile.secondary:.0f} dBm or better so calls or "
            "streams can hand over without a gap.",
            "Add or move an access point so each area is covered by two, overlapping by about "
            "15–20%.")
    elif second is None:
        add("Medium", "Single access point coverage",
            "Only one access point of the network covers this spot. If it's busy, restarts or "
            "fails, clients here have nowhere to go.",
            "Overlap coverage from a second access point (about -75 dBm or better) for "
            "redundancy and smoother roaming.")
    elif second < -75:
        add("Low", "Weak second access point",
            f"The second access point is only {second} dBm here, so moving clients hold on to "
            "the first one to the edge.",
            "Slightly more overlap (-70 to -67 dBm from a second access point) makes hand-overs "
            "earlier and smoother. 802.11k/v helps clients decide.")
    serving = rfplan.serving(point, ssid, band)
    if serving is None:
        return
    own_strong = [r for r in rfplan.radios(point.networks)
                  if ssid in r.ssids and r.band == serving.band and r.key != serving.key
                  and r.rssi >= -67]
    if len(own_strong) >= 3:
        add("Medium", "Too much cell overlap",
            f"{len(own_strong) + 1} of {ssid}'s {serving.band} GHz radios reach here at -67 dBm "
            "or better. Cells overlap too much: clients hop between them and the radios contend "
            "for the same airtime.",
            "Lower transmit power on some of them, or switch off the 2.4 GHz radio on every "
            "other access point.")


def _channel(point, ctx, ssid, band, best, add) -> None:
    profile = ctx.profile
    serving = rfplan.serving(point, ssid, band)
    if serving is None:
        return
    others = rfplan.contenders(point, ssid, band) or []
    own = [r for r in others if ssid and ssid in r.ssids]
    foreign = [r for r in others if r not in own]
    channel = channel_name(best)
    if others:
        severity = ("High" if profile.max_contenders is not None and len(others) > profile.max_contenders
                    else "Medium" if len(others) >= 2 else "Low")
        parts = []
        if own:
            parts.append(f"{len(own)} of the network's own radio{'s' if len(own) != 1 else ''}")
        if foreign:
            parts.append(f"{len(foreign)} other network{'s' if len(foreign) != 1 else ''} "
                         f"({_ssids(foreign)}, strongest {max(r.rssi for r in foreign)} dBm)")
        add(severity, "Co-channel contention",
            f"{channel.capitalize()} is shared with {' and '.join(parts)}, heard strongly enough that "
            "they take turns with it for airtime.",
            " ".join(
                (["Re-plan channels so neighbouring access points of the network differ (see "
                  "the suggested channel plan)."] if own else [])
                + (["Move to a channel the neighbours don't use, or narrow the channel width."]
                   if foreign else [])))
    if serving.band == "2.4" and round(serving.freq) not in (2412, 2437, 2462):
        add("Medium", "Overlapping 2.4 GHz channel",
            f"Served on 2.4 GHz {channel}, which overlaps two of channels 1, 6 and 11.",
            "Use only channels 1, 6 or 11 on 2.4 GHz.")
    if serving.band == "2.4" and serving.width > 20:
        add("Medium", "40 MHz on 2.4 GHz",
            f"The 2.4 GHz radio uses a {serving.width:.0f} MHz channel, taking two of the band's "
            "three clear channels.",
            "Set 2.4 GHz to 20 MHz.")
    if serving.band in ("5", "6") and serving.width >= 80 and len(others) >= 2:
        add("Low", "Wide channel in a crowded band",
            f"A {serving.width:.0f} MHz channel here overlaps {len(others)} other radios.",
            f"A narrower channel ({serving.width / 2:.0f} MHz) often gives more real throughput "
            "when the band is this crowded.")
    wide = [r for r in rfplan.radios(point.networks)
            if r.band == "2.4" and r.rssi >= -75 and round(r.freq) not in (2412, 2437, 2462)
            and r.key != serving.key]
    if serving.band == "2.4" and wide:
        add("Low", "Neighbours on overlapping channels",
            f"Neighbours on overlapping 2.4 GHz channels ({_ssids(wide)}) reach here at up to "
            f"{max(r.rssi for r in wide)} dBm; they interfere without sharing airtime politely.",
            "Nothing to change on your side beyond staying on 1, 6 or 11; if they're yours or a "
            "neighbour's, ask for them to move too.")

    # How busy the channel is
    airtime = point.capture["airtime"] if point.capture else None
    if airtime is None:
        value = analysis.metric(point, "occupancy", ctx, ssid=ssid, band=band)
        airtime = None if value is None else value / 100
    limit = profile.max_airtime or ctx.targets.max_airtime
    if airtime is not None:
        if airtime >= limit:
            add("High", "Channel too busy",
                f"{channel.capitalize()} is busy {airtime * 100:.0f}% of the time (limit "
                f"{limit * 100:.0f}%). Everyone here waits for the air.",
                "Move to a quieter channel, add an access point to split the clients, or remove "
                "legacy (802.11b) rates that eat airtime.")
        elif airtime >= limit * 0.7:
            add("Medium", "Channel nearly full",
                f"{channel.capitalize()} is busy {airtime * 100:.0f}% of the time, close to the "
                f"{limit * 100:.0f}% limit.",
                "Little room for more clients or traffic: consider a quieter channel.")
    same_channel = [n for n in point.networks
                    if n.get("freq_mhz") and round(n["freq_mhz"]) == round(best.get("freq_mhz") or 0)
                    and (n.get("rssi") or -100) >= rfplan.CCA_DBM]
    beacons = rfplan.beacon_airtime(same_channel)
    if beacons >= 0.10:
        add("Medium" if beacons >= 0.2 else "Low", "Beacon overhead",
            f"{len(same_channel)} BSSIDs on {channel} spend about {beacons * 100:.0f}% of "
            "its airtime on beacons alone.",
            "Fewer SSIDs per radio (merge guest/IoT networks with VLANs), and higher minimum "
            "basic rates, give that airtime back.")
    basics = [min(n["basic_rates"]) for n in point.networks
              if n["bssid"] == best["bssid"] and n.get("basic_rates")]
    if best["band"] == "2.4" and basics and min(basics) < 6:
        add("Low", "802.11b rates enabled",
            f"The serving radio still requires 802.11b rates (basic rate {min(basics):g} Mbps).",
            "Disable 1, 2, 5.5 and 11 Mbps so beacons and broadcasts go out faster.")
    capture = point.capture
    if capture and capture.get("retry_eligible", 0) > 50:
        rate = capture["retry_rate"]
        if rate >= ctx.targets.max_retry_rate:
            add("High", "High retry rate",
                f"{rate * 100:.0f}% of captured frames are retries ({capture.get('clients', 0)} "
                "clients heard).",
                "Frames are colliding or failing: suspect a hidden node, interference or too "
                "much overlap. Check the channel and the neighbours on it.")
        elif rate >= ctx.targets.max_retry_rate / 2:
            add("Medium", "Some retries", f"{rate * 100:.0f}% of captured frames are retries.",
                "Some collisions or interference: worth watching.")
    link = point.link or {}
    if 0 < link.get("tx_mbps", 0) < ctx.targets.min_phy_mbps:
        add("Medium", "Low link rate",
            f"The survey adapter's link negotiated only {link['tx_mbps']:.0f} Mbps.",
            "Check the channel width, legacy rates, and the client itself; a good signal should "
            "give far more.")


def _security(best, add) -> None:
    security = (best.get("security") or "").upper()
    auth = (best.get("auth") or "").upper()
    if best.get("secured") is False or security == "OPEN":
        add("High", "Open network", f"{best.get('ssid') or 'The network'} is open: traffic isn't "
            "encrypted.", "Use WPA2 or WPA3, or Enhanced Open (OWE) for a guest network.")
    elif "WEP" in security or security in ("WPA", "WPA1"):
        add("High", "Weak encryption", f"{best.get('ssid') or 'The network'} uses {security}, which is "
            "broken and also limits speed to 54 Mbps.", "Switch to WPA2-AES or WPA3.")
    elif "PSK" in auth and "SAE" not in auth:
        add("Low", "No WPA3", "WPA2-Personal only.",
            "WPA3 (SAE) in transition mode is stronger against password guessing and keeps older "
            "clients working.")


def _general(point, ctx, add) -> None:
    """What isn't about the serving network: the connection, the data, and the survey itself."""
    # The adapter not being joined is a gap in the data, not a fault
    checks = [c for c in point.checks_made() if "isn't connected" not in (c.get("error") or "")]
    for result in checks:
        ssid = result.get("ssid") or ""
        on = f" on {ssid}" if ssid else ""
        if result.get("error"):
            named = ssid + (f" ({result['bssid'].upper()})" if ssid and result.get("bssid") else "")
            add("High", "Connection failed",
                f"Connection check{' on ' + named if named else ''}: {result['error']}.",
                "Check the adapter can associate here and that DHCP and the gateway answer.",
                network=ssid)
            continue
        loss = result.get("loss")
        if loss is not None and loss >= 1:
            add("High" if loss >= 5 else "Medium", "Packet loss",
                f"{loss:.0f}% of pings to the gateway{on} were lost.",
                "Loss this close to the gateway means retries or interference on the air: "
                "voice and video will stutter.", network=ssid)
        latency = result.get("latency_ms")
        if latency is not None and latency >= 20:
            add("High" if latency >= 50 else "Medium", "High latency",
                f"Gateway latency{on} averages {latency:.0f} ms.",
                "The gateway is a hop away, so this should be under 5–10 ms: the channel is busy "
                "or frames are being retried.", network=ssid)
        jitter = result.get("jitter_ms")
        if jitter is not None and jitter >= 10:
            add("Medium", "Jitter", f"Latency{on} varies by ±{jitter:.0f} ms.",
                "Jitter above 10 ms hurts calls: usually a busy channel or power-saving clients.",
                network=ssid)
        dns = result.get("dns_ms")
        if dns is not None and dns >= 150:
            add("Low", "Slow DNS", f"A DNS lookup{on} took {dns:.0f} ms.",
                "Slow DNS makes everything feel slow: check the resolver the network hands out.",
                network=ssid)
    for speed in [point.throughput or {}, *point.throughputs]:
        down = speed.get("down_mbps")
        on = f" on {speed['ssid']}" if speed.get("ssid") else ""
        if down is not None:
            up = speed.get("up_mbps")
            if down < 10 or (up is not None and up < 5):
                add("High", "Low throughput",
                    f"Only {down:.0f} Mbps down" + (f" and {up:.0f} Mbps up" if up is not None else "")
                    + f" measured to the test server{on}.",
                    "Users will find this slow for anything beyond browsing. Work through the "
                    "signal, noise and channel findings here; if they're all good, check the access "
                    "point's uplink and the server.", network=speed.get("ssid", ""))
            elif down < 50:
                add("Medium", "Low throughput",
                    f"{down:.0f} Mbps down measured to the test server{on}.",
                    "Enough for everyday use, not for large transfers or several video calls. A "
                    "stronger signal or a less busy channel usually raises it.",
                    network=speed.get("ssid", ""))
        elif speed.get("error") and "isn't installed" not in speed["error"]:
            add("Low", "Throughput test failed", f"The throughput test{on} failed: {speed['error']}.",
                "Check the iperf3 server is running (iperf3 -s) and reachable from this network; "
                "a public server may simply be busy.", network=speed.get("ssid", ""))
    if not checks:
        add("Low", "No connection check", "No connection check (latency, loss, DNS) at this point.",
            "Connect the adapter to the network and measure again to confirm it's usable, "
            "not just strong.")
    away = [t for t in point.trackers if t.get("away") and t.get("rssi", -100) >= -75]
    if away:
        kinds = ", ".join(sorted({t["label"] for t in away}))
        add("Medium", "Finder tag away from its owner",
            f"{len(away)} item tracker{'s' if len(away) != 1 else ''} ({kinds}) heard close by "
            "with nobody it belongs to nearby.",
            "Find and check it: it may be lost, left, or planted. A phone can play its sound "
            "(iPhone: Find My → Items → Identify Found Item; Android: Unknown tracker alerts).")
    if any(n.get("rssi_source") == "nmcli" for n in point.networks):
        add("Low", "Estimated signal levels", "Signal levels here are estimated from NetworkManager's 0–100 quality, "
            "not true dBm.", "Run the app as root for exact levels.")
    project = ctx.project
    near = nearest_point(project, point)
    if near is not None and project.meters_per_pixel:
        index, pixels = near
        feet = project.feet(pixels)
        if feet is not None and feet > GAP_FEET:
            add("Low", "Gap between measurements",
                f"The nearest other measurement (point {index + 1}) is {feet:.0f} ft away, so the "
                f"map between them is a guess. Another point about {feet / 2:.0f} ft toward it "
                "would fill the gap.",
                f"Measure every {GAP_FEET // 2}–{GAP_FEET - 5} ft, and in every room, for a map "
                "you can trust.")
