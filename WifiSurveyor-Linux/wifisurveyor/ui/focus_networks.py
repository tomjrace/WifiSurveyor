"""
Choosing the networks a survey is about (Project.focus_networks): each is joined and checked
at every point that hears it, and they're the ones the report judges. Every network heard is
still scanned and located; only these are joined.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from ..net import connectivity
from .common import run_in_thread


def summary(project) -> str:
    focus = list(project.focus_networks or [])
    if not focus:
        return ("None chosen: the connection check uses whatever network the adapter is on. "
                "Choose the networks this survey is about to join and check each one.")
    return (f"{', '.join(focus)}: each is joined and checked at every point that hears it. "
            "Every other network is still scanned and located.")


def heard(project, last_scan) -> list[tuple[str, int, int, bool]]:
    """Every network heard, as (name, points heard at, strongest level, encrypted)."""
    found: dict[str, list] = {}
    for point in project.points:
        for name in {n["ssid"] for n in point.networks if n["ssid"]}:
            found.setdefault(name, [0, -200, False])[0] += 1
        for network in point.networks:
            if network["ssid"]:
                entry = found[network["ssid"]]
                entry[1] = max(entry[1], int(network["rssi"]))
                entry[2] = entry[2] or bool(network.get("secured"))
    for reading in last_scan or []:
        if reading.ssid:
            entry = found.setdefault(reading.ssid, [0, -200, False])
            entry[1] = max(entry[1], int(reading.rssi))
            entry[2] = entry[2] or bool(getattr(reading, "secured", False))
    return [(name, count, best, secured) for name, (count, best, secured) in found.items()]


class FocusDialog(Adw.Dialog):
    def __init__(self, project, last_scan, saved: Callable[[], None]) -> None:
        super().__init__(title="Networks to survey", content_width=560, content_height=640)
        self.project = project
        self._saved = saved
        chosen = set(project.focus_networks or [])
        networks = heard(project, last_scan)
        for name in chosen - {n[0] for n in networks}:
            networks.append((name, 0, -200, True))      # chosen earlier, not heard yet
        # The chosen ones first, then the most heard, then the strongest
        networks.sort(key=lambda n: (n[0] not in chosen, -n[1], -n[2], n[0].lower()))

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])
        save.connect("clicked", self._save)
        header.pack_start(cancel)
        header.pack_end(save)
        page.append(header)

        group = Adw.PreferencesGroup(
            title="Join and check",
            description="At every point, each network switched on here that the point hears is "
                        "joined in turn, then the connection is checked and (if Test "
                        "throughput is on) its speed tested. This takes longer per point. "
                        "Networks need their password saved in the system's Wi-Fi settings; "
                        "open ones are joined as they are.")
        self.rows: list[tuple[str, Adw.SwitchRow]] = []
        total = len(project.points)
        for name, count, best, secured in networks:
            where = (f"Heard at {count} of {total} points" if total else "Heard in the last scan")
            if best > -200:
                where += f", strongest {best} dBm"
            row = Adw.SwitchRow(title=GLib.markup_escape_text(name), active=name in chosen,
                                subtitle=where + ("" if secured else " · open"))
            row.base_subtitle = row.get_subtitle()
            group.add(row)
            self.rows.append((name, row))
        if not networks:
            group.add(Adw.ActionRow(title="No networks yet: measure a point or scan first."))
        content = Adw.PreferencesPage()
        content.add(group)
        content.set_vexpand(True)
        page.append(content)
        self.set_child(page)

        # Whether each has its password saved, which joining needs: nmcli, so off the UI thread
        secured_names = [name for name, _c, _b, secured in networks if secured]

        def look():
            return {name: connectivity.saved_profile(name) is not None for name in secured_names}

        def found(saved, error) -> None:
            if error or not saved:
                return
            for name, row in self.rows:
                if name in saved:
                    row.set_subtitle(row.base_subtitle + (
                        " · password saved" if saved[name] else
                        " · no saved password: connect once from the system's Wi-Fi settings"))

        run_in_thread(look, found)

    def _save(self, *_args) -> None:
        self.project.focus_networks = [name for name, row in self.rows if row.get_active()]
        self._saved()
        self.close()
