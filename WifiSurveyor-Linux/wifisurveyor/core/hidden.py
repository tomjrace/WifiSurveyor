"""Hidden networks, and the names recovered for them by listening.

A hidden access point leaves its name out of its beacons and ignores a scan's wildcard probe
requests, so a scan only ever sees a blank name. It still has to say the name when a device
that knows it connects: the device asks for it by name (probe request, association request)
and the access point answers with it (probe response). Monitor mode hears that exchange
(net.capture), and the name it gives is kept with the survey, by BSSID, in
Project.hidden_names. Every point that heard that BSSID then shows the name, marked as a
hidden network ("hidden": True on the network), so the map, filters and report all use it.

Nothing here makes a device reconnect: a hidden network nobody joins while someone listens
stays unnamed.
"""

from __future__ import annotations


def is_hidden(ssid: str | None) -> bool:
    """A blank name, or one of nothing but NULs (some access points pad the name they hide)."""
    return not (ssid or "").replace("\\x00", "").strip("\x00 ")


def label(name: str | None, hidden: bool = False) -> str:
    """How a network is named on screen: its recovered name marked hidden, or "(hidden)"."""
    if not name:
        return "(hidden)"
    return f"{name} (hidden)" if hidden else name


def apply(networks: list[dict], names: dict[str, str]) -> int:
    """
    Marks a point's hidden networks and gives them the names recovered so far. Returns how
    many it named that had no name before.
    """
    named = 0
    for network in networks:
        if network.get("hidden") or is_hidden(network.get("ssid")):
            network["hidden"] = True
            name = names.get((network.get("bssid") or "").upper())
            if name and network.get("ssid") != name:
                named += not network.get("ssid")
                network["ssid"] = name
            elif is_hidden(network.get("ssid")):
                network["ssid"] = ""
    return named


def apply_all(project) -> int:
    """apply() on every point of the survey."""
    return sum(apply(point.networks, project.hidden_names) for point in project.points)


def unnamed(project) -> list[str]:
    """BSSIDs heard hiding their name with no name recovered yet, upper case."""
    return sorted({(n.get("bssid") or "").upper() for point in project.points
                   for n in point.networks
                   if (n.get("hidden") or is_hidden(n.get("ssid")))
                   and (n.get("bssid") or "").upper() not in project.hidden_names} - {""})


def learn(project, names: dict[str, str]) -> list[str]:
    """
    Keeps names heard for hidden access points (BSSID → name, either case) and names every
    point's networks with them. Returns the BSSIDs whose name is new or changed.
    """
    fresh = []
    for bssid, name in names.items():
        bssid, name = (bssid or "").upper(), (name or "").strip("\x00")
        if bssid and not is_hidden(name) and project.hidden_names.get(bssid) != name:
            project.hidden_names[bssid] = name
            fresh.append(bssid)
    if fresh:
        apply_all(project)
    return fresh


def describe_learned(project, bssids: list[str]) -> str:
    """One line for a status bar: which names were just recovered."""
    names = sorted({project.hidden_names[b] for b in bssids if b in project.hidden_names})
    if not names:
        return ""
    return (f"Recovered the name of {len(bssids)} hidden access point"
            f"{'s' if len(bssids) != 1 else ''}: {', '.join(names)}.")


def report_rows(project) -> list[dict]:
    """
    Every hidden access point heard: {"bssid", "name" (or ""), "points" (how many heard it),
    "best" (strongest dBm), "band", "channel"}, named ones first, strongest first.
    """
    found: dict[str, dict] = {}
    for point in project.points:
        for network in point.networks:
            if not (network.get("hidden") or is_hidden(network.get("ssid"))):
                continue
            bssid = (network.get("bssid") or "").upper()
            if not bssid:
                continue
            row = found.setdefault(bssid, {"bssid": bssid, "name": "", "points": 0,
                                           "best": network.get("rssi"),
                                           "band": network.get("band") or "",
                                           "channel": network.get("channel")})
            row["name"] = project.hidden_names.get(bssid) or row["name"]
            row["points"] += 1
            if network.get("rssi") is not None and (row["best"] is None
                                                    or network["rssi"] > row["best"]):
                row["best"] = network["rssi"]
    return sorted(found.values(), key=lambda r: (not r["name"], -(r["best"] or -200)))
