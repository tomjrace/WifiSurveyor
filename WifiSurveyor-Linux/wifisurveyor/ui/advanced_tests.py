"""
The Advanced network table: which adapter tests which network, and against which iperf3
server, so up to six adapters can join different networks and speed-test them at once. It
belongs to the survey (Project.speed_tests), so a new survey starts with nothing to test.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from gi.repository import Adw, Gtk, Pango

from ..net import iperf_servers, wifi
from ..net.throughput import MAX_ADAPTERS
from .common import run_in_thread

NOT_TESTED = "Don't test"
RANDOM_LABEL = "Random public server"


def load(project) -> dict:
    saved = project.speed_tests or {}
    return {"enabled": bool(saved.get("enabled", False)),
            "scan_with": bool(saved.get("scan_with", True)),
            "region": saved.get("region") or "",
            "rows": [r for r in saved.get("rows", []) if r.get("ssid") and r.get("adapter")]}


def active_plan(project) -> dict | None:
    """The survey's table when it's switched on and tests anything, else None."""
    saved = load(project)
    return saved if saved["enabled"] and saved["rows"] else None


def summary(project) -> str:
    saved = active_plan(project)
    if not saved:
        return ""
    adapters = {r["adapter"] for r in saved["rows"]}
    return (f"Advanced: {len(saved['rows'])} network{'s' if len(saved['rows']) != 1 else ''} on "
            f"{len(adapters)} adapter{'s' if len(adapters) != 1 else ''}"
            + (", which scan too" if saved["scan_with"] else ""))


class AdvancedDialog(Adw.Dialog):
    def __init__(self, project, networks: list[str], saved_changed: Callable[[], None]) -> None:
        super().__init__(title="Advanced network tests", content_width=900, content_height=640)
        self._saved_changed = saved_changed
        self.project = project
        self.saved = load(project)
        self.servers: list[iperf_servers.Server] = []
        self.adapters = [iface for iface in wifi.interfaces(refresh=True) if iface.mode != "monitor"]
        self.adapter_labels = [NOT_TESTED] + [wifi.label_for(iface) for iface in self.adapters]
        rows_by_ssid = {r["ssid"]: r for r in self.saved["rows"]}
        # Networks set up earlier stay listed even when this survey hasn't heard them yet
        self.networks = sorted(set(networks) | set(rows_by_ssid), key=str.lower)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])
        save.connect("clicked", self._save)
        header.pack_start(cancel)
        header.pack_end(save)
        page.append(header)

        options = Adw.PreferencesGroup(margin_start=12, margin_end=12)
        self.enabled = Adw.SwitchRow(
            title="Use this table when measuring",
            subtitle=f"Each network with an adapter is joined and speed-tested at every point. "
                     f"Different adapters test at once, up to {MAX_ADAPTERS}.",
            active=self.saved["enabled"])
        self.scan_with = Adw.SwitchRow(
            title="Scan with these adapters too",
            subtitle="Shares the channels out so each adapter scans its own: faster, and every "
                     "access point is still listed once. Calibrate the adapters against each "
                     "other on the Setup page so their readings line up.",
            active=self.saved["scan_with"])
        self.region = Adw.ComboRow(title="Region for public servers",
                                   model=Gtk.StringList.new(["All regions"]))
        self.region.connect("notify::selected", lambda *_: self._fill_server_menus())
        options.add(self.enabled)
        options.add(self.scan_with)
        options.add(self.region)
        page.append(options)

        list_bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12)
        self.list_info = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["dim-label"],
                                   label="Loading the public server list…")
        self.update = Gtk.Button(label="Update list")
        self.update.connect("clicked", lambda *_: self._load_servers(force=True))
        list_bar.append(self.list_info)
        list_bar.append(self.update)
        page.append(list_bar)

        # The table: network, adapter, server. Scrolls up and down; the header stays put.
        grid = Gtk.Grid(column_spacing=12, row_spacing=6, margin_start=12, margin_end=12,
                        margin_top=6, margin_bottom=12)
        for column, title in enumerate(("Network", "Adapter", "iperf3 server")):
            grid.attach(Gtk.Label(label=title, xalign=0, css_classes=["heading"]), column, 0, 1, 1)
        self.rows: list[tuple[str, Gtk.DropDown, Gtk.Entry, Gtk.MenuButton]] = []
        for number, ssid in enumerate(self.networks, start=1):
            saved_row = rows_by_ssid.get(ssid, {})
            name = Gtk.Label(label=ssid, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                             tooltip_text=ssid)
            adapter = Gtk.DropDown(model=Gtk.StringList.new(self.adapter_labels))
            wanted = saved_row.get("adapter")
            for index, iface in enumerate(self.adapters, start=1):
                if iface.name == wanted:
                    adapter.set_selected(index)
            server_box = Gtk.Box(css_classes=["linked"])
            entry = Gtk.Entry(hexpand=True, width_chars=22,
                              placeholder_text="IP or host[:port], blank for the main server")
            server = saved_row.get("server", "")
            entry.set_text(RANDOM_LABEL if server == iperf_servers.RANDOM else server)
            menu = Gtk.MenuButton(icon_name="pan-down-symbolic", tooltip_text="Choose a server")
            server_box.append(entry)
            server_box.append(menu)
            grid.attach(name, 0, number, 1, 1)
            grid.attach(adapter, 1, number, 1, 1)
            grid.attach(server_box, 2, number, 1, 1)
            self.rows.append((ssid, adapter, entry, menu))
        if not self.networks:
            grid.attach(Gtk.Label(label="No networks yet: measure a point or scan first.",
                                  xalign=0, css_classes=["dim-label"]), 0, 1, 3, 1)
        page.append(Gtk.ScrolledWindow(child=grid, vexpand=True,
                                       hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.problem = Gtk.Label(xalign=0, wrap=True, margin_start=12, margin_end=12,
                                 margin_bottom=8, css_classes=["error"], visible=False)
        page.append(self.problem)
        self.set_child(page)
        self._load_servers(force=False)

    # ----- the public list -----

    def _load_servers(self, force: bool) -> None:
        self.update.set_sensitive(False)
        if force:
            self.list_info.set_text("Downloading the public server list…")

        def done(result, error) -> None:
            self.update.set_sensitive(True)
            if error:
                self.list_info.set_text(f"Couldn't load the public server list ({error}). Type a "
                                        "server in, or try Update list again.")
                self._fill_server_menus()
                return
            self.servers, fetched = result
            when = time.strftime("%d %b %H:%M", time.localtime(fetched)) if fetched else "?"
            self.list_info.set_text(f"{len(self.servers)} public servers, list from {when}. "
                                    "Source: iperf3serverlist.net. Public servers test the "
                                    "internet connection too, not just the Wi-Fi.")
            regions = ["All regions", *iperf_servers.continents(self.servers)]
            self.region.set_model(Gtk.StringList.new(regions))
            if self.saved["region"] in regions:
                self.region.set_selected(regions.index(self.saved["region"]))
            self._fill_server_menus()

        run_in_thread(lambda: iperf_servers.get(force), done)

    def _region(self) -> str:
        item = self.region.get_selected_item()
        text = item.get_string() if item else ""
        return "" if text in ("", "All regions") else text

    def _fill_server_menus(self) -> None:
        region = self._region()
        choices = sorted((s for s in self.servers if not region or s.continent == region),
                         key=lambda s: (s.country, s.site))
        for _ssid, _adapter, entry, menu in self.rows:
            menu.set_popover(self._server_popover(entry, choices))

    def _server_popover(self, entry: Gtk.Entry, choices) -> Gtk.Popover:
        popover = Gtk.Popover()
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])

        def add(label: str, value: str) -> None:
            row = Gtk.ListBoxRow(child=Gtk.Label(label=label, xalign=0, margin_top=10,
                                                 margin_bottom=10, margin_start=10, margin_end=10))
            row.value = value
            listing.append(row)

        add(f"{RANDOM_LABEL} ({self._region() or 'any region'})", RANDOM_LABEL)
        add("Main server (from Measuring)", "")
        for server in choices:
            add(server.display, server.address)

        def chosen(_list, row) -> None:
            entry.set_text(row.value)
            popover.popdown()

        listing.connect("row-activated", chosen)
        popover.set_child(Gtk.ScrolledWindow(child=listing, min_content_height=360,
                                             min_content_width=460,
                                             hscrollbar_policy=Gtk.PolicyType.NEVER))
        return popover

    # ----- saving -----

    def _save(self, *_args) -> None:
        rows = []
        for ssid, adapter, entry, _menu in self.rows:
            chosen = adapter.get_selected()
            if chosen <= 0:
                continue
            text = entry.get_text().strip()
            rows.append({"ssid": ssid, "adapter": self.adapters[chosen - 1].name,
                         "server": iperf_servers.RANDOM if text == RANDOM_LABEL else text})
        adapters = {r["adapter"] for r in rows}
        if len(adapters) > MAX_ADAPTERS:
            self.problem.set_text(f"{len(adapters)} adapters chosen: up to {MAX_ADAPTERS} can be "
                                  "used. Give some networks the same adapter (they'll take turns) "
                                  "or set them to Don't test.")
            self.problem.set_visible(True)
            return
        self.project.speed_tests = {"enabled": self.enabled.get_active() and bool(rows),
                                    "scan_with": self.scan_with.get_active(),
                                    "region": self._region(), "rows": rows}
        self._saved_changed()
        self.close()
