"""Small helpers the views share."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..core.colours import ssid_colour


def run_in_thread(work: Callable[[], object], done: Callable[[object, Exception | None], None]) -> None:
    """Keeps scans, sweeps and captures off the main loop, then answers back on it."""

    def worker() -> None:
        try:
            result, error = work(), None
        except Exception as caught:  # surfaced in the view, never swallowed
            result, error = None, caught
        GLib.idle_add(done, result, error)

    threading.Thread(target=worker, daemon=True).start()


def labelled(text: str, widget: Gtk.Widget, spacing: int = 6) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)
    box.append(Gtk.Label(label=text, css_classes=["dim-label"]))
    box.append(widget)
    return box


def toolbar(*widgets: Gtk.Widget) -> Gtk.Widget:
    """A row of controls that wraps onto a second line rather than widening the window."""
    margins = dict(margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)
    if hasattr(Adw, "WrapBox"):                     # libadwaita 1.7
        box = Adw.WrapBox(child_spacing=8, line_spacing=8, **margins)
    else:
        box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8,
                          row_spacing=8, max_children_per_line=32, **margins)
    for widget in widgets:
        box.append(widget)
    return box


def fit_dropdown(dropdown: Gtk.DropDown, chars: int = 22) -> Gtk.DropDown:
    """
    Shortens the closed dropdown's label with an ellipsis, so a long adapter or port name
    can't push the toolbar wider than the screen. The open list still shows names in full.
    """

    def setup(_factory, item, ellipsize: bool) -> None:
        label = Gtk.Label(xalign=0)
        if ellipsize:
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_max_width_chars(chars)
        item.set_child(label)

    def bind(_factory, item) -> None:
        text = item.get_item()
        item.get_child().set_text(text.get_string() if text is not None else "")

    button = Gtk.SignalListItemFactory()
    button.connect("setup", setup, True)
    button.connect("bind", bind)
    listing = Gtk.SignalListItemFactory()
    listing.connect("setup", setup, False)
    listing.connect("bind", bind)
    dropdown.set_factory(button)
    dropdown.set_list_factory(listing)
    return dropdown


def status_label(text: str = "") -> Gtk.Label:
    """A status line that ellipsizes instead of setting the window's minimum width."""
    return Gtk.Label(label=text, xalign=0, css_classes=["dim-label"],
                     ellipsize=Pango.EllipsizeMode.END, hexpand=True)


def load_style() -> None:
    """Touch sizing for every window, from data/style.css."""
    provider = Gtk.CssProvider()
    provider.load_from_path(str(Path(__file__).resolve().parent.parent / "data" / "style.css"))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    # The app icon (hicolor/…/org.wifisurveyor.App.png) and the Watch tab's heartbeat,
    # found by name like the theme's.
    Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
        str(Path(__file__).resolve().parent.parent / "data"))


def scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    window = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    window.set_child(child)
    return window


def set_strings(dropdown: Gtk.DropDown, values: list[str], placeholder: str = "") -> None:
    """Replaces a dropdown's entries and keeps the current choice when it is still there."""
    current = ""
    model = dropdown.get_model()
    if model is not None and model.get_n_items():
        current = model.get_string(dropdown.get_selected())
    items = list(values) or ([placeholder] if placeholder else [])
    dropdown.set_model(Gtk.StringList.new(items))
    if current in items:
        dropdown.set_selected(items.index(current))


def selected_text(dropdown: Gtk.DropDown) -> str:
    model = dropdown.get_model()
    if model is None or model.get_n_items() == 0:
        return ""
    return model.get_string(dropdown.get_selected())


def selected_device(dropdown: Gtk.DropDown) -> str:
    from ..net import wifi

    return wifi.device_from_label(selected_text(dropdown))


def bind_device_dropdown(dropdown: Gtk.DropDown, session, role: str = "scan") -> None:
    """Scan menus share one radio. The capture menu prefers the Alfa, when one is plugged in."""
    dropdown._syncing = False

    def refill(*_args) -> None:
        from ..net import wifi

        ifaces = wifi.interfaces()
        labels = [wifi.label_for(iface) for iface in ifaces] or ["none"]
        names = [iface.name for iface in ifaces]
        scan, monitor = wifi.choose_roles(ifaces)
        if role == "monitor":
            if not session.monitor_manual or session.monitor_device not in names:
                session.monitor_device = monitor
                session.monitor_manual = False
            preferred = session.monitor_device
        else:
            if not session.scan_manual or session.scan_device not in names:
                session.scan_device = scan
                session.scan_manual = False
            preferred = session.scan_device
        dropdown._syncing = True
        set_strings(dropdown, labels, "none")
        if preferred in names:
            dropdown.set_selected(names.index(preferred))
        dropdown._syncing = False

    def changed(*_args) -> None:
        if dropdown._syncing:
            return
        name = selected_device(dropdown)
        if role == "monitor":
            session.monitor_device = name or None
            session.monitor_manual = bool(name)
            others = session.monitor_dropdowns
        else:
            session.scan_device = name or None
            session.scan_manual = bool(name)
            others = session.scan_dropdowns
        for other in others:
            if other is not dropdown:
                other.refill()

    dropdown.connect("notify::selected", changed)
    dropdown.refill = refill
    if role == "monitor":
        session.monitor_dropdowns.append(dropdown)
    else:
        session.scan_dropdowns.append(dropdown)
    refill()


# A cell's grade (a row's "_grade_<column>"), coloured with libadwaita's own styles so it reads
# in light and dark: good green, fair amber, poor red
GRADE_CLASSES = {"good": "success", "fair": "warning", "poor": "error"}


class Table(Gtk.ColumnView):
    """
    A column view built from plain dictionaries, which is all these tables need. A row's
    "_grade_<column>" of "good", "fair" or "poor" colours that cell green, amber or red.
    """

    def __init__(self, columns: list[tuple[str, str, int]]) -> None:
        self.store = Gtk.StringList()
        self._rows: list[dict] = []
        selection = Gtk.NoSelection(model=self.store)
        super().__init__(model=selection, hexpand=True, vexpand=True)
        self.add_css_class("data-table")

        for title, key, width in columns:
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup)
            factory.connect("bind", self._bind, key)
            column = Gtk.ColumnViewColumn(title=title, factory=factory)
            column.set_fixed_width(width)
            column.set_resizable(True)
            self.append_column(column)
        # The first column (the name) takes up any spare width rather than leaving a gap
        first = self.get_columns().get_item(0)
        if first is not None:
            first.set_expand(True)

    @staticmethod
    def _setup(_factory, item) -> None:
        item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                 margin_start=6, margin_end=6))

    def _bind(self, _factory, item, key: str) -> None:
        holder = item.get_item()
        if holder is None:
            return
        try:
            index = int(holder.get_string())
        except (TypeError, ValueError):
            return
        if 0 <= index < len(self._rows):
            row, label = self._rows[index], item.get_child()
            label.set_text(str(row.get(key, "")))
            # Labels are reused from row to row, so every grade's colour is cleared first
            grade = GRADE_CLASSES.get(row.get(f"_grade_{key}"))
            for name in GRADE_CLASSES.values():
                if name != grade:
                    label.remove_css_class(name)
            if grade:
                label.add_css_class(grade)

    def set_rows(self, rows: list[dict]) -> None:
        # A fresh model forces every cell to rebind. Reusing "0","1",… does not.
        self._rows = list(rows)
        self.store = Gtk.StringList.new([str(i) for i in range(len(self._rows))])
        self.set_model(Gtk.NoSelection(model=self.store))


def ap_colour(ssid: str, _freq_mhz: float | None = None) -> tuple[float, float, float]:
    """
    One colour per network name, whatever its channel, kept for the whole session: the same on
    the spectrum page, on every point of the survey map and in the report.
    """
    return ssid_colour(ssid)
