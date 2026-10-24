"""Checkbox lists with "All" at the top: for choosing bands and ranges, on the Spectrum page
and in the survey setup."""

from __future__ import annotations

from collections.abc import Callable, Hashable

from gi.repository import Gtk, Pango


class CheckList(Gtk.Box):
    """
    "All", then a checkbox per item, optionally under section headings. An item listed in two
    sections appears once, under the first. ``on_change`` gets the chosen keys, in the order
    shown, whenever a person changes them (never when the code does).
    """

    def __init__(self, on_change: Callable[[list], None] | None = None, all_label: str = "All") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("check-list")
        self.on_change = on_change
        self._syncing = False
        self._order: list[Hashable] = []
        self._checks: dict[Hashable, Gtk.CheckButton] = {}
        self.all = Gtk.CheckButton(label=all_label)
        self.all.add_css_class("check-all")
        self.all.connect("toggled", self._all_toggled)
        self.append(self.all)
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.append(self._body)

    def set_items(self, sections: list[tuple[str | None, list[tuple[Hashable, str]]]],
                  selected) -> None:
        """Replaces the items: (heading or None, [(key, label)]) per section."""
        chosen = set(selected)
        child = self._body.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._body.remove(child)
            child = following
        self._order, self._checks = [], {}
        self._syncing = True
        for heading, items in sections:
            fresh = [(key, label) for key, label in items if key not in self._checks]
            if not fresh:
                continue
            if heading:
                self._body.append(Gtk.Label(label=heading, xalign=0, css_classes=["heading"],
                                            margin_top=8, margin_start=4))
            for key, label in fresh:
                check = Gtk.CheckButton(label=label, active=key in chosen)
                text = check.get_last_child()
                if isinstance(text, Gtk.Label):
                    text.set_wrap(True)
                    text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                check.connect("toggled", self._item_toggled)
                self._body.append(check)
                self._order.append(key)
                self._checks[key] = check
        self._syncing = False
        self._show_all_state()

    def selected(self) -> list:
        return [key for key in self._order if self._checks[key].get_active()]

    def set_selected(self, keys) -> None:
        chosen = set(keys)
        self._syncing = True
        for key, check in self._checks.items():
            check.set_active(key in chosen)
        self._syncing = False
        self._show_all_state()

    def keys(self) -> list:
        return list(self._order)

    def _show_all_state(self) -> None:
        """All ticked when every item is, partly ticked when some are."""
        count = len(self.selected())
        self._syncing, before = True, self._syncing
        self.all.set_active(bool(self._order) and count == len(self._order))
        self.all.set_inconsistent(0 < count < len(self._order))
        self.all.set_sensitive(bool(self._order))
        self._syncing = before

    def _all_toggled(self, check: Gtk.CheckButton) -> None:
        if self._syncing:
            return
        self.set_selected(self._order if check.get_active() else [])
        self._changed()

    def _item_toggled(self, _check: Gtk.CheckButton) -> None:
        if self._syncing:
            return
        self._show_all_state()
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self.selected())


class CheckMenu(Gtk.MenuButton):
    """A button that opens a CheckList, its label saying what's chosen."""

    def __init__(self, list_: CheckList, max_height: int = 560, width: int = 360) -> None:
        super().__init__(always_show_arrow=True)
        self.list = list_
        # The width is requested outright: with no horizontal scrolling, GTK ignores
        # min_content_width, and wrapping labels would let the list shrink to a word a line
        window = Gtk.ScrolledWindow(child=list_, propagate_natural_height=True,
                                    max_content_height=max_height, width_request=width,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        list_.set_margin_top(6)
        list_.set_margin_bottom(6)
        list_.set_margin_start(6)
        list_.set_margin_end(6)
        self.set_popover(Gtk.Popover(child=window))
