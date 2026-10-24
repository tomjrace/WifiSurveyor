"""The survey setup dialog: what each point measures, chosen by the kind of survey."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ..core import surveysetup as ss
from ..rf import bands as rfbands
from .choosers import CheckList


class SurveySetupDialog(Adw.Dialog):
    """
    A survey type up top fills in everything below; changing anything below makes it Custom.
    Apply hands the result to ``on_apply``. ``analyzers`` and ``hackrf`` are what's connected,
    for the time a point will take.
    """

    def __init__(self, setup: ss.Setup, on_apply: Callable[[ss.Setup], None],
                 analyzers: int = 0, hackrf: bool = False) -> None:
        super().__init__(title="Survey setup", content_width=760, content_height=760)
        self.on_apply = on_apply
        self.analyzers, self.hackrf = analyzers, hackrf
        self._syncing = False

        page = Adw.PreferencesPage()
        kinds = Adw.PreferencesGroup(
            title="What kind of survey",
            description="Each type ticks what's worth measuring for it. Change anything below "
                        "to suit the site; that makes it Custom.")
        self.kind = Adw.ComboRow(title="Survey type", model=Gtk.StringList.new(
            [name for _k, name, _a, _s in ss.TYPES]))
        self.kind.connect("notify::selected", self._kind_chosen)
        kinds.add(self.kind)
        self.time_row = Adw.ActionRow(title="Each point takes about", use_markup=False)
        kinds.add(self.time_row)
        page.add(kinds)

        wifi = Adw.PreferencesGroup(title="Wi-Fi",
                                    description="The Wi-Fi scan always runs. These are the "
                                                "spectrum ranges swept with the analyzers, and "
                                                "the tests on the connection. The spectrum "
                                                "ranges here and below are the ones ticked on "
                                                "the Spectrum page: change them in either place.")
        self.wifi_ranges = CheckList(lambda _k: self._changed())
        self.wifi_ranges.set_items([(None, list(enumerate(ss.WIFI_RANGE_NAMES)))], setup.wifi_ranges)
        wifi.add(_card("Spectrum ranges", self.wifi_ranges))
        self.sweeps = _spin("Sweeps per range", 1, 30, "More is steadier but slower.")
        self.dwell_count = _spin("Channels to watch closely", 0, 8,
                                 "Parks on the busiest channels to measure airtime and power. "
                                 "0 turns it off.")
        self.dwell_seconds = _spin("Seconds per channel", 1, 15)
        self.check_link = _switch("Check the connection", "Latency, jitter, loss and DNS time.")
        self.join = _switch("Join the network first",
                            "Connects to the network chosen on the map, so the connection and "
                            "throughput can be tested.")
        self.speed = _switch("Test throughput", "iperf3 to the server set on the survey page.")
        self.hidden_names = _switch(
            "Recover hidden network names",
            f"Listens up to {ss.HIDDEN_SECONDS} s on each hidden network's channel for its "
            "name, which it says when a device joins it. Only listens. Needs root and a "
            "second adapter that does monitor mode.")
        for row in (self.sweeps, self.dwell_count, self.dwell_seconds, self.check_link,
                    self.join, self.speed, self.hidden_names):
            wifi.add(row)
        page.add(wifi)

        radios = Adw.PreferencesGroup(title="Other radios")
        self.bluetooth = _switch("Bluetooth devices", "Needs root; a few seconds a point.")
        self.tags = _switch("AirTags and finder tags", "Needs root; about 8 s a point.")
        self.identify = _switch("Name what's on the busy channels (HackRF)",
                                "Wi-Fi, Bluetooth, Zigbee, microwave ovens, carriers, radar.")
        self.cellular = _switch("Cellular bands (HackRF)", "Downlink power in each US band.")
        for row in (self.bluetooth, self.tags, self.identify, self.cellular):
            radios.add(row)
        page.add(radios)

        other = Adw.PreferencesGroup(
            title="Other bands",
            description="Swept with whichever analyzers reach each band (RTL-SDR, HackRF, "
                        "tinySA, RF Explorer). Tick the groups this site needs, then untick any "
                        "range in them it doesn't.")
        self.groups = CheckList(self._groups_changed)
        self.groups.set_items([(None, [(k, n) for k, n, _a in rfbands.GROUPS])], setup.band_groups)
        other.add(_card("Band groups", self.groups))
        self.bands = CheckList(lambda _k: self._changed())
        other.add(_card("Ranges", self.bands))
        # Ranges ticked on the Spectrum page that aren't bands listed above: kept as they are
        self._extras: list[list] = []
        self.extras_row = Adw.ActionRow(title="Also from the Spectrum page", use_markup=False)
        other.add(self.extras_row)
        page.add(other)

        for row in (self.sweeps, self.dwell_count, self.dwell_seconds):
            row.connect("notify::value", lambda *_: self._changed())
        for row in (self.check_link, self.join, self.speed, self.hidden_names, self.bluetooth,
                    self.tags, self.identify, self.cellular):
            row.connect("notify::active", lambda *_: self._changed())

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        apply = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        apply.connect("clicked", self._apply)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(apply)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        self.set_child(view)
        self._show(setup)

    # ---------- between the setup and the widgets ----------

    def _show(self, setup: ss.Setup) -> None:
        self._syncing = True
        keys = [k for k, _n, _a, _s in ss.TYPES]
        self.kind.set_selected(keys.index(setup.kind) if setup.kind in keys else keys.index("custom"))
        self.wifi_ranges.set_selected(setup.wifi_ranges)
        self.sweeps.set_value(setup.sweeps)
        self.dwell_count.set_value(setup.dwell_count)
        self.dwell_seconds.set_value(setup.dwell_seconds)
        for name in ("check_link", "join", "speed", "hidden_names", "bluetooth", "tags",
                     "identify", "cellular"):
            getattr(self, name).set_active(getattr(setup, name))
        self.groups.set_selected(setup.band_groups)
        self._fill_bands(setup.bands)
        self._extras = [list(e) for e in setup.extra_ranges]
        self.extras_row.set_visible(bool(self._extras))
        self.extras_row.set_subtitle(", ".join(e[0] for e in self._extras))
        self._syncing = False
        self._describe()

    def current(self) -> ss.Setup:
        keys = [k for k, _n, _a, _s in ss.TYPES]
        setup = ss.Setup(
            kind=keys[self.kind.get_selected()], wifi_ranges=self.wifi_ranges.selected(),
            sweeps=int(self.sweeps.get_value()), dwell_count=int(self.dwell_count.get_value()),
            dwell_seconds=int(self.dwell_seconds.get_value()),
            band_groups=self.groups.selected(), bands=self.bands.selected(),
            extra_ranges=[list(e) for e in self._extras],
            **{name: getattr(self, name).get_active() for name in
               ("check_link", "join", "speed", "hidden_names", "bluetooth", "tags", "identify",
                "cellular")})
        return setup

    def _fill_bands(self, chosen) -> None:
        groups = set(self.groups.selected())
        self.bands.set_items([(name if len(groups) > 1 else None,
                               [(b.key, b.name) for b in rfbands.in_group(key)])
                              for key, name, _a in rfbands.GROUPS if key in groups], chosen)

    def _groups_changed(self, groups: list[str]) -> None:
        """A group ticked brings its ranges in, ticked; one unticked takes its own away."""
        before = set(self.bands.selected())
        kept = {b.key for key in groups for b in rfbands.in_group(key)}
        added = kept - set(self.bands.keys())
        self._fill_bands((before & kept) | added)
        self._changed()

    def _kind_chosen(self, *_args) -> None:
        if self._syncing:
            return
        key = ss.TYPES[self.kind.get_selected()][0]
        if key != "custom":
            self._show(ss.preset(key))
        else:
            self._describe()

    def _changed(self) -> None:
        if self._syncing:
            return
        setup = self.current()
        kind = ss.matching_kind(setup)
        if kind != setup.kind:
            self._syncing = True
            self.kind.set_selected([k for k, _n, _a, _s in ss.TYPES].index(kind))
            self._syncing = False
        self._describe()

    def _describe(self) -> None:
        setup = self.current()
        self.kind.set_subtitle(ss.TYPE_BY_KEY[setup.kind][1])
        seconds = ss.seconds_per_point(setup, self.analyzers, self.hackrf)
        self.time_row.set_subtitle(
            f"{seconds:.0f} seconds with what's connected now"
            + ("" if self.analyzers else " (no analyzer connected, so no spectrum)")
            + f": about {3600 / seconds:.0f} points an hour, before walking between them.")

    def _apply(self, *_args) -> None:
        self.on_apply(self.current())
        self.close()


def _card(title: str, child: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["card"],
                  margin_top=6, margin_bottom=6)
    box.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"], margin_top=8,
                         margin_start=10))
    child.set_margin_start(6)
    child.set_margin_end(6)
    child.set_margin_bottom(6)
    box.append(child)
    return box


def _spin(title: str, low: int, high: int, subtitle: str = "") -> Adw.SpinRow:
    row = Adw.SpinRow.new_with_range(low, high, 1)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


def _switch(title: str, subtitle: str = "") -> Adw.SwitchRow:
    return Adw.SwitchRow(title=title, subtitle=subtitle)
