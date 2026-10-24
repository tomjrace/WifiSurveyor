"""What a long recording caught: the whole span as a waterfall, and the channels and access
points that changed over it."""

from __future__ import annotations

import time
from pathlib import Path

import cairo
from gi.repository import Adw, GLib, Gtk

from ..rf.analyzer import WIFI_RANGES
from .spectrum_view import BAND_PANELS, _waterfall_image

BAND_NAMES = {"2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}


def show_recording(parent: Gtk.Widget, path: str, summary: dict) -> None:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12,
                  margin_end=12, margin_bottom=12)
    if summary.get("start") is None:
        box.append(Gtk.Label(label="This recording is empty.", xalign=0))
    else:
        start, end = summary["start"], summary["end"]
        hours = (end - start) / 3600
        box.append(Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"],
            label=f"{time.strftime('%d %b %H:%M', time.localtime(start))} to "
                  f"{time.strftime('%d %b %H:%M', time.localtime(end))} ({hours:.1f} h): "
                  f"{summary['sweeps']:,} sweeps, {summary['scans']:,} access point scans. "
                  "Time runs down the waterfall; each row is the strongest level in its slice, "
                  "so short bursts still show."))
        area = Gtk.DrawingArea(content_height=340, hexpand=True)
        area.set_draw_func(lambda _a, cr, w, h: _draw_overview(cr, w, h, summary))
        box.append(area)

        channels = sorted(summary["channels"], key=lambda c: c["busy"], reverse=True)
        busy = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for channel in channels[:15]:
            hour = channel["busiest_hour"]
            busy.append(Adw.ActionRow(
                title=f"{BAND_NAMES.get(channel['band'], '')} channel {channel['channel']}: "
                      f"busy {channel['busy'] * 100:.0f}% of the time",
                subtitle=f"{channel['bursts']} separate bursts, peak {channel['peak_dbm']:.0f} dBm "
                         f"over a floor of {channel['floor_dbm']:.0f} dBm"
                         + (f", mostly around {hour:02d}:00" if hour is not None else "")))
        if channels:
            box.append(Gtk.Label(label="Busiest channels", xalign=0, css_classes=["heading"]))
            box.append(busy)

        if summary["aps"]:
            box.append(Gtk.Label(label="Access points: how reliably each was heard", xalign=0,
                                 css_classes=["heading"]))
            aps = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
            for ap in summary["aps"][:40]:
                aps.append(Adw.ActionRow(
                    title=GLib.markup_escape_text(f"{ap['ssid'] or '(hidden)'} · {ap['bssid']}"
                                                  + (f" · {ap['vendor']}" if ap.get("vendor") else "")),
                    subtitle=f"heard in {ap['heard'] * 100:.0f}% of scans, signal {ap['min']} to "
                             f"{ap['max']} dBm (average {ap['avg']:.0f}), {ap['freq']:.0f} MHz"
                             + ("" if ap["heard"] > 0.9 else
                                f", last heard {time.strftime('%H:%M', time.localtime(ap['last']))}")))
            box.append(aps)

    body = Gtk.ScrolledWindow(child=box, propagate_natural_height=True, max_content_height=700,
                              hscrollbar_policy=Gtk.PolicyType.NEVER)
    view = Adw.ToolbarView(content=body)
    view.add_top_bar(Adw.HeaderBar())
    Adw.Dialog(title=Path(path).stem, child=view, content_width=900).present(parent)


def _draw_overview(cr: cairo.Context, width: int, height: int, summary: dict) -> None:
    """The combined view's panels, each range's time-squeezed waterfall laid out by frequency."""
    cr.set_source_rgb(0.055, 0.09, 0.15)
    cr.paint()
    left, top, bottom = 56, 22, height - 6
    usable = width - left - 6
    entries = summary["ranges"]
    # Recorded with the Wi-Fi view: its three panels. With any other ranges: one each.
    wifi = all(int(key) <= 3 and entry["start_mhz"] >= 2300 for key, entry in entries.items())
    panels = BAND_PANELS if wifi else [
        (f"{entry['start_mhz']:.0f}-{entry['end_mhz']:.0f} MHz", [key], (1.0, 1.0),
         (entry["start_mhz"], entry["end_mhz"]))
        for key, entry in sorted(entries.items(), key=lambda item: item[1]["start_mhz"])]
    total = sum(weights[0] for _t, _m, weights, _s in panels) or 1
    x = left
    cr.select_font_face("Cantarell")
    for title, members, weights, span in panels:
        panel_width = usable * weights[0] / total
        low, high = span or (min(WIFI_RANGES[i][1] for i in members),
                             max(WIFI_RANGES[i][2] for i in members))

        def x_at(freq: float, x=x, panel_width=panel_width, low=low, high=high) -> float:
            return x + 4 + (freq - low) / (high - low) * (panel_width - 8)

        cr.set_source_rgb(0.03, 0.05, 0.10)
        cr.rectangle(x + 4, top, panel_width - 8, bottom - top)
        cr.fill()
        for member in members:
            entry = summary["ranges"].get(member) or summary["ranges"].get(str(member))
            if not entry:
                continue
            image = entry["image"]
            surface, _pixels = _waterfall_image(image)
            x0, x1 = x_at(entry["start_mhz"]), x_at(entry["end_mhz"])
            cr.save()
            cr.rectangle(x0, top, x1 - x0, bottom - top)
            cr.clip()
            cr.translate(x0, top)
            cr.scale((x1 - x0) / image.shape[1], (bottom - top) / image.shape[0])
            cr.set_source_surface(surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            cr.restore()
        cr.set_source_rgba(0.94, 0.95, 0.97, 1)
        cr.set_font_size(12)
        cr.move_to(x + 6, 15)
        cr.show_text(title)
        x += panel_width
    cr.set_source_rgba(0.66, 0.71, 0.78, 1)
    cr.set_font_size(11)
    for fraction in (0, 0.25, 0.5, 0.75, 1):
        stamp = summary["start"] + (summary["end"] - summary["start"]) * fraction
        y = top + (bottom - top) * fraction
        cr.move_to(6, min(bottom - 2, y + 10 if fraction == 0 else y + 4))
        cr.show_text(time.strftime("%H:%M", time.localtime(stamp)))
