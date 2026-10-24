"""Live spectrum from the spectrum analyzer, drawn with Cairo."""

from __future__ import annotations

import io
import math
import threading
import time
from collections import deque
from types import SimpleNamespace

import cairo
import numpy as np
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from ..core import rangechoice, recording
from ..core.rangechoice import (  # noqa: F401  (the page's names for them)
    CELLULAR_SET, MIN_SPAN_MHZ, OVERVIEWS, RANGES, WIFI_SET, band_sets, range_key,
)
from ..core.analysis import access_points
from ..net import gps, wifi
from ..rf import bands as rfbands
from ..rf import spectrum as spec
from ..rf import detect
from ..rf.analyzer import WIFI_RANGES, SpanError, wifi_ranges
from .choosers import CheckList, CheckMenu
from .common import (
    ap_colour, fit_dropdown, labelled, run_in_thread, selected_text, set_strings,
    status_label, toolbar,
)

# One panel per band in the combined view: (title, which WIFI_RANGES it draws, relative size
# side by side and stacked, the span it shows or None for just what's swept). 6 GHz shows the
# whole US band, 5925-7125 MHz, with the part above what the analyzer reaches greyed out, so
# it's wide side by side but no taller than 2.4 GHz when stacked.
BAND_PANELS = [("2.4 GHz", [0], (1.0, 1.0), None), ("5 GHz", [1, 2], (2.0, 2.0), None),
               ("6 GHz", [3], (2.0, 1.0), (5925.0, 7125.0))]
TOP_DBM, BOTTOM_DBM = -20.0, -110.0
LEFT, RIGHT, TOP, BOTTOM = 50, 10, 46, 30
AXIS_LABEL_GAP = 14        # pixels kept clear between frequency labels across the top
WATERFALL_ROWS = 150       # how far back the waterfall goes, in updates of each range
WATERFALL_LOW, WATERFALL_HIGH = -110.0, -40.0
# Quiet to strong: dark blue, blue, cyan, yellow, red
WATERFALL_STOPS = np.array([0.0, 0.3, 0.55, 0.78, 1.0])
WATERFALL_COLOURS = np.array([[12, 16, 40], [25, 70, 170], [30, 190, 210], [250, 215, 45],
                              [235, 55, 40]], dtype=float)
FIRST_SWEEP_WAIT = 10      # seconds a single range waits for its first sweep before going to pieces
# More ranges than this and the page lists them, quiet ones folded away, rather than drawing a
# panel each too small to read; a tapped range is drawn full size
SUMMARY_ABOVE = 3
GRADE_COLOURS = {"good": (0.20, 0.70, 0.35), "fair": (0.95, 0.65, 0.15),
                 "poor": (0.90, 0.25, 0.20), "none": (0.55, 0.58, 0.62)}
def merge_ranges(chosen: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """
    Ranges to sweep as panels: overlapping ones, and ones close enough that the gap between
    them is under a quarter of the panel they'd share, become one. Lowest first.
    """
    merged: list[list] = []
    for name, start, end in sorted(chosen, key=lambda r: (r[1], r[2])):
        if merged:
            last = merged[-1]
            span = max(last[2], end) - last[1]
            if start <= last[2] or start - last[2] <= 0.25 * span:
                last[2] = max(last[2], end)
                last[0].append(name)
                continue
        merged.append([[name], start, end])
    titles = []
    for names, start, end in merged:
        title = names[0] if len(names) == 1 else f"{start:.0f}-{end:.0f} MHz: " + ", ".join(
            n.split(" (")[0] for n in names)
        titles.append((title if len(title) <= 70 else title[:68] + "…", start, end))
    return titles


def nice_step(span: float, most: int) -> float:
    """A round step (1, 2, 2.5 or 5 times a power of ten) giving at most ``most`` labels."""
    if span <= 0 or most < 1:
        return max(span, 1e-3)
    raw = span / most
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if factor * power >= raw:
            return factor * power
    return 10 * power


def freq_label(freq: float, step: float) -> str:
    """A tick's frequency with as many decimals as the step needs (0.25 MHz steps need two)."""
    decimals = next((d for d in range(5) if abs(step * 10 ** d - round(step * 10 ** d)) < 1e-6), 4)
    return f"{freq:.{decimals}f}"


# Band overlays on a single range, by what the licence means for planning equipment
LICENCE_COLOURS = {rfbands.UNLICENSED: (0.20, 0.75, 0.45), rfbands.LICENSED: (0.95, 0.65, 0.15),
                   rfbands.AVOID: (0.92, 0.30, 0.30), rfbands.LISTEN: (0.55, 0.62, 0.72)}
STACK_BELOW = 900          # narrower than this, the band panels stack instead of sitting side by side
AP_SCAN_EVERY = 10         # seconds between Wi-Fi scans for the AP markers, while the view is shown


def _waterfall_image(levels: np.ndarray) -> tuple[cairo.ImageSurface, np.ndarray]:
    """
    dBm rows to a picture: one pixel per column and per update, blank where nothing was swept.
    The surface reads straight from the returned buffer, which has to outlive its use.
    """
    rows, columns = levels.shape
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, columns)
    pixels = np.zeros((rows, stride // 4, 4), dtype=np.uint8)
    t = np.clip((levels - WATERFALL_LOW) / (WATERFALL_HIGH - WATERFALL_LOW), 0, 1)
    for channel, bgr in enumerate((2, 1, 0)):
        values = np.interp(np.nan_to_num(t, nan=0.0), WATERFALL_STOPS, WATERFALL_COLOURS[:, bgr])
        values[np.isnan(levels)] = 8
        pixels[:, :columns, channel] = values.astype(np.uint8)
    surface = cairo.ImageSurface.create_for_data(memoryview(pixels), cairo.FORMAT_RGB24,
                                                 columns, rows, stride)
    return surface, pixels


class SpectrumPlot:
    """
    Draws the spectrum panels: the live page draws itself with it, and a survey measurement
    uses it to keep a picture of the spectrum at that point. Needs ``ranges_data``,
    ``combined``, ``view``, ``active`` and ``cursor`` set, and says which access points to mark.
    """

    _panels: list[dict]
    # The Wi-Fi ranges as the analyzer sweeps them: the same four, 6 GHz as far as it reaches
    wifi_ranges = WIFI_RANGES
    # What the combined view sweeps and how it lays it out: the Wi-Fi ranges in BAND_PANELS,
    # or (for any other choice of ranges) ``swept``, one panel each
    swept: list | None = None
    panel_specs: list = BAND_PANELS
    history: dict = {}
    recorder = None
    _recorded_at: dict = {}
    # The one range drawn full size, tapped in the summary list; None draws them all
    focus: int | None = None

    def _ranges(self) -> tuple[tuple[str, float, float], ...]:
        return tuple(self.swept) if self.swept is not None else self.wifi_ranges

    def _wifi_panels(self) -> bool:
        """The familiar Wi-Fi layout: 2.4, 5 and 6 GHz side by side."""
        return self.combined and self.panel_specs is BAND_PANELS

    def _aps_shown(self) -> bool:
        return False

    def _waterfall_shown(self) -> bool:
        return False

    def _remember(self, index: int, parts: list[dict]) -> None:
        """Keeps each range's recent traces for the waterfall."""
        rows = self.history.get(index)
        if rows is None:
            rows = self.history[index] = deque(maxlen=WATERFALL_ROWS)
        rows.append([(part["config"], list(part["trace"])) for part in parts])
        recorder = self.recorder
        # A single range updates many times a second; once a second is plenty on disk
        if recorder is not None and time.time() - self._recorded_at.get(index, 0) >= 1.0:
            self._recorded_at[index] = time.time()
            recorder.sweep(index, [(part["config"].start_mhz, part["config"].step_mhz,
                                    getattr(part["config"], "rbw_khz", 600), part["trace"])
                                   for part in parts])

    def _ap_networks(self) -> list:
        return []

    def _full_span(self) -> tuple[float, float] | None:
        """The single range as swept, or None before its first sweep."""
        data = self.ranges_data.get(0)
        if self.combined or not data or not data.get("parts"):
            return None
        parts = data["parts"]
        first, last = parts[0], parts[-1]
        return (first["config"].start_mhz,
                last["config"].start_mhz + last["config"].step_mhz * (len(last["trace"]) - 1))


    def _layout(self, width: int, height: int) -> list[dict]:
        """Where each panel goes: side by side when there's room, stacked when there isn't."""
        if not self.combined:
            full_span = self._full_span()
            if full_span is None:
                return []
            low, high = self.view or full_span
            low, high = max(full_span[0], low), min(full_span[1], high)
            return [{"x": 0, "y": 0, "w": width, "h": height, "lo": low, "hi": high,
                     "title": "", "ranges": [0]}]

        if not self._wifi_panels():
            return self._grid(width, height)
        panels = []
        stacked = width < STACK_BELOW
        total = sum(weights[stacked] for _, _, weights, _ in BAND_PANELS)
        offset = 0.0
        for title, members, weights, span in BAND_PANELS:
            low, high = span or (min(self._ranges()[i][1] for i in members),
                                 max(self._ranges()[i][2] for i in members))
            share = weights[stacked] / total
            if not stacked:
                panel = {"x": offset * width, "y": 0, "w": share * width, "h": height}
            else:
                panel = {"x": 0, "y": offset * height, "w": width, "h": share * height}
            panel.update({"lo": low, "hi": high, "title": title, "ranges": members})
            panels.append(panel)
            offset += share
        return panels

    def _grid(self, width: int, height: int) -> list[dict]:
        """Any other set of ranges: one panel each, in rows, as many across as fit."""
        specs = self.panel_specs
        if self.focus is not None and self.focus < len(specs):
            specs = [specs[self.focus]]
        count = len(specs)
        if not count:
            return []
        if width < STACK_BELOW:
            columns = 1 if count <= 3 else 2
        else:
            columns = count if count <= 3 else 3 if count <= 9 else 4
        rows = math.ceil(count / columns)
        panels = []
        for number, (title, members, _weights, span) in enumerate(specs):
            row, column = divmod(number, columns)
            across = min(columns, count - row * columns)       # the last row shares its width
            low, high = span or (min(self._ranges()[i][1] for i in members),
                                 max(self._ranges()[i][2] for i in members))
            panels.append({"x": column * width / across, "y": row * height / rows,
                           "w": width / across, "h": height / rows, "lo": low, "hi": high,
                           "title": title, "ranges": members})
        return panels

    def draw(self, _area, cr: cairo.Context, width: int, height: int) -> None:
        cr.set_source_rgb(0.055, 0.09, 0.15)
        cr.paint()
        self._panels = self._layout(width, height)
        cr.select_font_face("Cantarell")
        for index, panel in enumerate(self._panels):
            self._draw_panel(cr, index, panel)

    def _draw_panel(self, cr: cairo.Context, index: int, panel: dict) -> None:
        px, py, pw, ph = panel["x"], panel["y"], panel["w"], panel["h"]
        low, high = panel["lo"], panel["hi"]
        left, right = px + LEFT, px + pw - RIGHT
        top, bottom = py + TOP, py + ph - BOTTOM
        # With the waterfall, the trace keeps the upper part and time runs down the rest
        fall = self._waterfall_shown() and ph >= 220
        if fall:
            bottom = py + ph * 0.58 - BOTTOM + 10
        plot_width, plot_height = max(1.0, right - left), max(1.0, bottom - top)

        def x_at(freq: float) -> float:
            return left + (freq - low) / max(1e-9, high - low) * plot_width

        def y_at(dbm: float) -> float:
            clamped = max(BOTTOM_DBM, min(TOP_DBM, dbm))
            return top + (TOP_DBM - clamped) / (TOP_DBM - BOTTOM_DBM) * plot_height

        if index:                              # a divider between band panels
            cr.set_source_rgba(1, 1, 1, 0.18)
            cr.set_line_width(1)
            if panel["x"] > 0:
                cr.move_to(px + 0.5, py)
                cr.line_to(px + 0.5, py + ph)
            else:
                cr.move_to(px, py + 0.5)
                cr.line_to(px + pw, py + 0.5)
            cr.stroke()

        cr.set_line_width(1)
        cr.set_font_size(12)
        spacing = 10 if plot_height >= 180 else 20          # short stacked panels get fewer labels
        for level in range(int(BOTTOM_DBM) + (10 if spacing == 20 else 0), int(TOP_DBM) + 1, spacing):
            y = y_at(level)
            cr.set_source_rgba(1, 1, 1, 0.09)
            cr.move_to(left, y)
            cr.line_to(right, y)
            cr.stroke()
            cr.set_source_rgba(0.66, 0.71, 0.78, 1)
            cr.move_to(px + 8, y + 4)
            cr.show_text(f"{level}")

        # Channel lines, with numbers only where they won't run into each other
        last_label = -1e9
        for band_table in (spec.CHANNELS_24, spec.CHANNELS_5, spec.CHANNELS_6):
            for number, centre in sorted(band_table.items(), key=lambda item: item[1]):
                if not (low <= centre <= high):
                    continue
                x = x_at(centre)
                cr.set_source_rgba(1, 1, 1, 0.08)
                cr.move_to(x, top)
                cr.line_to(x, bottom)
                cr.stroke()
                text = str(number)
                label_width = cr.text_extents(text).x_advance
                if x - label_width / 2 > last_label + 4:
                    cr.set_source_rgba(0.66, 0.71, 0.78, 1)
                    cr.move_to(x - label_width / 2, bottom + 14)
                    cr.show_text(text)
                    last_label = x + label_width / 2

        if not self._wifi_panels():
            self._draw_bands(cr, low, high, x_at, top, bottom, left, right)
        self._draw_freq_axis(cr, low, high, x_at, left, right, top, bottom, px)

        if panel["title"]:
            cr.set_source_rgba(0.94, 0.95, 0.97, 1)
            cr.set_font_size(13)
            cr.save()
            cr.rectangle(px, py, pw, TOP - 16)
            cr.clip()
            cr.move_to(left, py + 16)
            cr.show_text(panel["title"])
            cr.restore()

        cr.save()
        cr.rectangle(left, py, plot_width, ph)
        cr.clip()
        cr.set_font_size(12)
        members = panel["ranges"]
        if self._wifi_panels() and len(members) > 1:
            # 5350-5470 MHz isn't swept: it isn't Wi-Fi in the US
            gap_start, gap_end = self._ranges()[members[0]][2], self._ranges()[members[1]][1]
            self._shade(cr, x_at(gap_start), x_at(gap_end), top, plot_height,
                        ("not US", "Wi-Fi"), 0.04)
        if self._wifi_panels():
            # The band carries on past what the analyzer can sweep (the 6G Combo stops at
            # 6100 MHz): grey that out rather than leave it looking quiet
            reach_low = min(self._ranges()[i][1] for i in members)
            reach_high = max(self._ranges()[i][2] for i in members)
            for start, end in ((low, reach_low), (reach_high, high)):
                if end - start > 0.5:
                    self._shade(cr, x_at(start), x_at(end), top, plot_height,
                                (f"{start:.0f}-{end:.0f} MHz", "beyond the analyzer's range"), 0.09)
        for member in members:
            data = self.ranges_data.get(member)
            if self.combined and self.active == member:
                # The range being swept right now: a bar under its stretch of the plot
                start, end = self._ranges()[member][1], self._ranges()[member][2]
                cr.set_source_rgba(0.98, 0.80, 0.13, 0.9)
                cr.rectangle(x_at(start), bottom + 18, x_at(end) - x_at(start), 3)
                cr.fill()
            if not data:
                continue
            if "error" in data:
                start, end = self._ranges()[member][1], self._ranges()[member][2]
                self._draw_error(cr, f"Couldn't sweep {start:.0f}-{end:.0f} MHz: {data['error']}",
                                 x_at(start) + 6, top + 20, x_at(end) - x_at(start) - 12)
                continue
            for part in data["parts"]:
                self._stroke(cr, part["peak"], part["config"], x_at, y_at, (0.97, 0.44, 0.44, 0.8), 1)
                self._stroke(cr, part["trace"], part["config"], x_at, y_at, (0.98, 0.80, 0.13, 1), 1.5)

        if self._aps_shown():
            self._draw_aps(cr, low, high, x_at, y_at, left, right, top)

        if self.cursor is not None and self.cursor[0] == index:
            self._draw_readout(cr, panel, self.cursor[1], x_at, y_at, top, bottom, left, right)
        cr.restore()

        if fall:
            self._draw_waterfall(cr, members, x_at, left, right, bottom + 26, py + ph - 6)

        if self.view is not None and not self.combined:
            cr.set_source_rgba(0.66, 0.71, 0.78, 1)
            cr.set_font_size(12)
            label = f"{low:.0f}-{high:.0f} MHz · double-tap for all"
            cr.move_to(right - cr.text_extents(label).width - 4, py + 16)
            cr.show_text(label)

    def _draw_freq_axis(self, cr, low, high, x_at, left, right, top, bottom, px) -> None:
        """
        Frequencies across the top of the plot, at round steps, with a tick into it and a faint
        line down it, so it's plain what frequency anything sits at. "MHz" goes in the margin.
        """
        if high - low <= 0:
            return
        cr.set_font_size(11)
        sample = cr.text_extents(freq_label(high, nice_step(high - low, 8))).x_advance
        most = max(2, int((right - left) / (sample + AXIS_LABEL_GAP)))
        step = nice_step(high - low, most)
        first = math.ceil(low / step - 1e-9) * step
        count = int((high - first) / step + 1e-9) + 1
        last_end = -1e9
        for index in range(count):
            freq = first + index * step
            x = x_at(freq)
            if x < left - 0.5 or x > right + 0.5:
                continue
            text = freq_label(freq, step)
            width = cr.text_extents(text).x_advance
            tx = max(left, min(right - width, x - width / 2))
            cr.set_source_rgba(1, 1, 1, 0.05)
            cr.set_line_width(1)
            cr.move_to(x, top)
            cr.line_to(x, bottom)
            cr.stroke()
            cr.set_source_rgba(0.66, 0.71, 0.78, 0.9)
            cr.move_to(x, top - 4)
            cr.line_to(x, top)
            cr.stroke()
            if tx > last_end + 4:
                cr.move_to(tx, top - 7)
                cr.show_text(text)
                last_end = tx + width
        cr.set_source_rgba(0.66, 0.71, 0.78, 0.9)
        cr.move_to(px + 8, top - 7)
        cr.show_text("MHz")
        cr.set_font_size(12)

    def _draw_bands(self, cr, low, high, x_at, top, bottom, left, right) -> None:
        """
        The bands of rf.bands in view as strips along the top, coloured by licence (green
        unlicensed, amber licensed, red keep out, grey listen only), and TV channel numbers
        along the bottom where TV channels are in view. Wider views get fewer labels.
        """
        shown = rfbands.within(low, high)
        rows: list[float] = []                       # where each strip row's last label ends
        cr.set_font_size(11)
        for band in sorted(shown, key=lambda b: (b.low, -b.width)):
            x0, x1 = max(left, x_at(band.low)), min(right, x_at(band.high))
            if x1 - x0 < 1:
                continue
            row = next((i for i, end in enumerate(rows) if end < x0 - 2), None)
            if row is None:
                if len(rows) >= 3:
                    continue
                rows.append(0.0)
                row = len(rows) - 1
            y = top + 2 + row * 15
            red, green, blue = LICENCE_COLOURS.get(band.licence, (0.6, 0.6, 0.6))
            cr.set_source_rgba(red, green, blue, 0.10)
            cr.rectangle(x0, top, x1 - x0, bottom - top)
            cr.fill()
            cr.set_source_rgba(red, green, blue, 0.85)
            cr.rectangle(x0, y, x1 - x0, 3)
            cr.fill()
            label = band.name
            width = cr.text_extents(label).x_advance
            if width > x1 - x0 - 4:
                label = band.name.split(" (")[0]
                width = cr.text_extents(label).x_advance
            if width <= max(x1 - x0, 60) - 4:
                cr.move_to(x0 + 2, y + 13)
                cr.show_text(label)
                rows[row] = x0 + width + 2
            else:
                rows[row] = x1
        channels = rfbands.tv_channels(low, high)
        if channels and len(channels) <= 40:
            last = -1e9
            for number, edge, upper in channels:
                xa, xb = x_at(edge), x_at(upper)
                cr.set_source_rgba(1, 1, 1, 0.10)
                cr.move_to(xa, top)
                cr.line_to(xa, bottom)
                cr.stroke()
                text = str(number)
                width = cr.text_extents(text).x_advance
                middle = (xa + xb) / 2
                if middle - width / 2 > last + 4 and left <= middle <= right:
                    cr.set_source_rgba(0.66, 0.71, 0.78, 1)
                    cr.move_to(middle - width / 2, bottom + 14)
                    cr.show_text(text)
                    last = middle + width / 2
        cr.set_font_size(12)

    def _draw_waterfall(self, cr, members, x_at, left, right, top, bottom) -> None:
        """Each range's recent traces, newest at the top, coloured by level."""
        height = bottom - top
        if height < 20:
            return
        cr.set_source_rgb(0.03, 0.05, 0.10)
        cr.rectangle(left, top, right - left, height)
        cr.fill()
        for member in members:
            try:
                rows = list(self.history.get(member) or [])[::-1]
            except RuntimeError:           # added to while copying: next draw catches up
                continue
            if not rows:
                continue
            start, end = self._ranges()[member][1], self._ranges()[member][2]
            if not self.combined:
                start, end = (self._full_span() or (start, end))
            x0, x1 = max(left, x_at(start)), min(right, x_at(end))
            columns = int(x1 - x0)
            if columns < 2:
                continue
            freqs = np.linspace(x0, x1, columns)
            freqs = start + (freqs - x_at(start)) / max(1e-9, x_at(end) - x_at(start)) * (end - start)
            levels = np.full((len(rows), columns), np.nan)
            for number, parts in enumerate(rows):
                points_f, points_v = [], []
                for config, trace in parts:
                    points_f.extend(config.start_mhz + i * config.step_mhz for i in range(len(trace)))
                    points_v.extend(trace)
                if points_f:
                    levels[number] = np.interp(freqs, points_f, points_v, left=np.nan, right=np.nan)
            surface, _pixels = _waterfall_image(levels)
            cr.save()
            cr.rectangle(x0, top, columns, height)
            cr.clip()
            cr.translate(x0, top)
            cr.scale(1, height / WATERFALL_ROWS)
            cr.set_source_surface(surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            cr.restore()
        cr.set_source_rgba(0.66, 0.71, 0.78, 1)
        cr.set_font_size(11)
        cr.move_to(left - 44, top + 10)
        cr.show_text("now")
        cr.move_to(left - 44, bottom - 2)
        cr.show_text("earlier")

    def _draw_aps(self, cr, low, high, x_at, y_at, left, right, top) -> None:
        """
        Each access point from the last Wi-Fi scan as a bar across its whole channel, at the
        level the adapter reported, with a dot on its primary 20 MHz, in its own colour.
        Hollow when the level is NetworkManager's estimate.
        """
        placed: list[tuple[float, float, float, float]] = []
        cr.set_font_size(11)
        for ap in access_points(self._ap_networks()):
            if not (low - ap["width"] / 2 <= ap["center"] <= high + ap["width"] / 2):
                continue
            colour = ap_colour(ap["ssid"], ap["freq"])
            x, y = x_at(ap["freq"]), y_at(ap["rssi"])
            half = ap["width"] / 2
            cr.set_source_rgba(*colour, 0.9)
            cr.set_line_width(2)
            cr.move_to(x_at(ap["center"] - half), y - 4)
            cr.line_to(x_at(ap["center"] - half), y)
            cr.line_to(x_at(ap["center"] + half), y)
            cr.line_to(x_at(ap["center"] + half), y - 4)
            cr.stroke()
            cr.arc(x, y, 3.5, 0, 6.2832)
            if ap["estimate"]:
                cr.set_source_rgb(0.055, 0.09, 0.15)
                cr.fill_preserve()
                cr.set_source_rgba(*colour, 1)
                cr.set_line_width(1.5)
                cr.stroke()
            else:
                cr.fill()

            # Its name and level above it, nudged up past labels already drawn when there's
            # room, and overlapping them when there isn't
            text = f"{ap['ssid'] or '(hidden)'} {'~' if ap['estimate'] else ''}{ap['rssi']}"
            width = cr.text_extents(text).x_advance
            tx = max(left + 2, min(right - width - 2, x - width / 2))
            spots = [(tx - 2, ty - 11, tx + width + 2, ty + 3) for ty in (y - 8, y - 21, y - 34)]
            box = next((b for b in spots if b[1] >= top and not any(
                b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3] for o in placed)),
                spots[0])
            placed.append(box)
            cr.set_source_rgba(*colour, 1)
            cr.move_to(tx, box[3] - 3)
            cr.show_text(text)
        cr.set_font_size(12)

    @staticmethod
    def _shade(cr, x0: float, x1: float, top: float, height: float, words, alpha: float) -> None:
        """Greys out a stretch of the plot that isn't swept, saying why in the middle of it."""
        cr.set_source_rgba(1, 1, 1, alpha)
        cr.rectangle(x0, top, x1 - x0, height)
        cr.fill()
        cr.set_source_rgba(0.66, 0.71, 0.78, 0.8)
        cr.set_font_size(11)
        for row, word in enumerate(words):
            extents = cr.text_extents(word)
            if extents.x_advance > x1 - x0 - 4:
                continue                   # too narrow to say it
            cr.move_to((x0 + x1 - extents.x_advance) / 2, top + height / 2 + row * 14)
            cr.show_text(word)
        cr.set_font_size(12)

    @staticmethod
    def _stroke(cr, values, config, x_at, y_at, colour, line_width) -> None:
        cr.set_source_rgba(*colour)
        cr.set_line_width(line_width)
        for index, value in enumerate(values):
            point = (x_at(config.start_mhz + index * config.step_mhz), y_at(value))
            cr.line_to(*point) if index else cr.move_to(*point)
        cr.stroke()

    @staticmethod
    def _draw_error(cr, text: str, x: float, y: float, width: float) -> None:
        """Wraps the analyzer's own complaint into the space its range would have filled."""
        cr.set_source_rgba(0.97, 0.44, 0.44, 0.9)
        cr.set_font_size(12)
        line = ""
        for word in text.split():
            trial = f"{line} {word}".strip()
            if line and cr.text_extents(trial).x_advance > max(80.0, width):
                cr.move_to(x, y)
                cr.show_text(line)
                y += 16
                line = word
            else:
                line = trial
        cr.move_to(x, y)
        cr.show_text(line)

    def _draw_readout(self, cr, panel, freq, x_at, y_at, top, bottom, left, right) -> None:
        """The frequency and level under the finger, from whichever range covers it."""
        parts = [part for member in panel["ranges"]
                 for part in self.ranges_data.get(member, {}).get("parts", [])]
        for part in parts:
            config, trace, peak = part["config"], part["trace"], part["peak"]
            end = config.start_mhz + config.step_mhz * (len(trace) - 1)
            if not (config.start_mhz - config.step_mhz <= freq <= end + config.step_mhz):
                continue
            index = max(0, min(len(trace) - 1, round((freq - config.start_mhz) / config.step_mhz)))
            at = config.start_mhz + index * config.step_mhz
            x = x_at(at)
            cr.set_source_rgba(1, 1, 1, 0.55)
            cr.set_line_width(1)
            cr.move_to(x, top)
            cr.line_to(x, bottom)
            cr.stroke()
            cr.set_source_rgb(0.98, 0.80, 0.13)
            cr.arc(x, y_at(trace[index]), 5, 0, 6.2832)
            cr.fill()
            text = f"{at:.1f} MHz  {trace[index]:.0f} dBm"
            if index < len(peak):
                text += f"  peak {peak[index]:.0f}"
            cr.set_font_size(12)
            extents = cr.text_extents(text)
            tx = max(left + 4, min(right - extents.width - 4, x - extents.width / 2))
            cr.set_source_rgba(0.055, 0.09, 0.15, 0.85)
            cr.rectangle(tx - 4, top + 50, extents.width + 8, 18)
            cr.fill()
            cr.set_source_rgba(0.94, 0.95, 0.97, 1)
            cr.move_to(tx, top + 63)
            cr.show_text(text)
            return


class _Snapshot(SpectrumPlot):
    """The combined view of a survey point's sweeps, with the access points heard there."""

    def __init__(self, segments: list[dict], networks: list) -> None:
        self.combined, self.view, self.active, self.cursor = True, None, None, None
        self._panels = []
        self.networks = networks
        self.ranges_data = {}
        self.history = {}
        # As far up as this point was swept, so a tinySA's 6 GHz isn't drawn greyed out
        reach = max((s["start_mhz"] + s["step_mhz"] * (len(s["avg"]) - 1)
                     for s in segments if s.get("avg")), default=0.0)
        self.wifi_ranges = wifi_ranges(reach)
        for segment in segments:
            if not segment.get("avg"):
                continue
            start = segment["start_mhz"]
            index = next((i for i, (_name, low, high) in enumerate(self.wifi_ranges)
                          if low - 1 <= start < high), None)
            if index is None:
                continue
            config = SimpleNamespace(start_mhz=start, step_mhz=segment["step_mhz"])
            self.ranges_data.setdefault(index, {"parts": []})["parts"].append(
                {"config": config, "trace": segment["avg"], "peak": segment.get("max") or []})

    def _aps_shown(self) -> bool:
        return True

    def _ap_networks(self) -> list:
        return self.networks


def snapshot_png(segments: list[dict], networks: list, width: int = 1400, height: int = 420) -> bytes:
    """A picture of the whole spectrum as the Spectrum page would draw it, for keeping."""
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    _Snapshot(segments, networks).draw(None, cairo.Context(surface), width, height)
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


def band_png(segments: list[dict], networks: list, band: str, width: int = 1100,
             height: int = 340) -> bytes | None:
    """
    One Wi-Fi band of a survey point's sweeps ("2.4", "5" or "6"), drawn on its own with the
    access points heard in it, for the report. None when the point didn't sweep that band.
    """
    spec = next((s for s in BAND_PANELS if s[0].split()[0] == band), None)
    if spec is None:
        return None
    snapshot = _Snapshot(segments, networks)
    if not any(i in snapshot.ranges_data for i in spec[1]):
        return None
    snapshot.panel_specs = [spec]           # a list of its own: one panel, full width
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    snapshot.draw(None, cairo.Context(surface), width, height)
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


class _RangeSnapshot(SpectrumPlot):
    """One range a survey point swept beyond Wi-Fi (Point.ranges), drawn as its own panel."""

    def __init__(self, record: dict) -> None:
        self.combined, self.view, self.active, self.cursor = True, None, None, None
        self._panels = []
        self.history = {}
        avg, peak = record.get("avg") or [], record.get("max") or []
        start, stop = record["start_mhz"], record["stop_mhz"]
        self.swept = [(record.get("name", ""), start, stop)]
        self.panel_specs = [(record.get("name", ""), [0], (1.0, 1.0), None)]
        config = SimpleNamespace(start_mhz=start, step_mhz=(stop - start) / max(1, len(avg) - 1))
        self.ranges_data = {0: {"parts": [{"config": config, "trace": avg, "peak": peak}]}}


def range_png(record: dict, width: int = 1100, height: int = 300) -> bytes | None:
    """A picture of one range a survey point swept (Point.ranges), for the report."""
    if len(record.get("avg") or []) < 2:
        return None
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    _RangeSnapshot(record).draw(None, cairo.Context(surface), width, height)
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


class SpectrumView(Gtk.Box, SpectrumPlot):
    def __init__(self, session) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session = session
        # What's been swept, by range: {"parts": [{"config", "trace", "peak"}, …]}, one part
        # per piece the analyzer swept it in, or {"error"} when it couldn't. The single-range
        # view keeps its one range at key 0.
        self.ranges_data: dict[int, dict] = {}
        self.combined = True
        self.active: int | None = None          # the range being swept now, in the combined view
        self.running = False
        self._stop = threading.Event()
        self._sweep_count = 3
        self.view: tuple[float, float] | None = None     # zoomed-in span, single range only
        self.cursor: tuple[int, float] | None = None     # (panel, frequency) under the finger
        self._panels: list[dict] = []
        self._pinch: dict | None = None
        self._pinching = False

        self.ports = fit_dropdown(Gtk.DropDown.new_from_strings(gps.serial_ports() or ["no serial ports"]))
        # Bands (sets of ranges) and ranges, each a checkbox list with All at the top. Ticking
        # a set ticks its ranges; what's ticked under Ranges is what's swept.
        self._sets = band_sets()
        self._set_lists = CheckList(self._sets_changed)
        self._set_lists.set_items([(None, [(name, name) for name, _r in self._sets])],
                                  self._saved_sets())
        self.band_set = CheckMenu(self._set_lists, width=380)
        self.band_set.set_tooltip_text("Which sets of bands to choose ranges from: Wi-Fi, wireless "
                                       "mics and IEMs, production, sub-GHz and industrial, "
                                       "business radio, the venue's other services, cellular.")
        self._range_lists = CheckList(self._ranges_changed)
        self.ranges = CheckMenu(self._range_lists, width=380)
        self.ranges.set_tooltip_text("The ranges to sweep. One is shown on its own and can be "
                                     "zoomed; several are shown side by side, nearby ones together.")
        self._fill_ranges(self._saved_ranges())
        self.sweeps = Gtk.SpinButton.new_with_range(1, 30, 1)
        self.sweeps.set_value(self._sweep_count)
        self.sweeps.set_tooltip_text(
            "How many sweeps to average on each range before moving to the next. More is "
            "steadier; fewer goes round the bands faster."
        )
        self.sweeps.connect("value-changed",
                            lambda spin: setattr(self, "_sweep_count", int(spin.get_value())))
        self.sweeps_box = labelled("Sweeps per range", self.sweeps)
        self.status = status_label("Connect an RF Explorer, tinySA, HackRF or RTL-SDR, then start.")
        self.button = Gtk.Button(label="Start", css_classes=["suggested-action"])
        self.button.connect("clicked", self.toggle)
        clear = Gtk.Button(label="Clear peak")
        clear.connect("clicked", self.clear_peak)
        self.record = Gtk.ToggleButton(child=Adw.ButtonContent(icon_name="media-record-symbolic",
                                                               label="Record"))
        self.record.set_tooltip_text(
            "Saves every sweep, and the access points scanned every 10 s, to a file for as "
            "long as it runs: leave it overnight to catch interference that comes and goes.")
        self.record.connect("toggled", self._record_toggled)
        recordings = Gtk.Button(label="Recordings…")
        recordings.connect("clicked", self._open_recording)
        self.recorder = None
        self._recorded_at = {}
        self.show_aps = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.show_aps.set_tooltip_text(
            "Mark the access points a Wi-Fi scan finds on their channel, at the signal the "
            "adapter reports. Scanned every 10 s while this page is showing."
        )
        self.show_aps.connect("notify::active", lambda *_: self._aps_toggled())
        self.show_fall = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.show_fall.set_tooltip_text(
            "Recent sweeps under each panel, newest at the top: something that comes and goes "
            "shows up as a streak or a dotted line.")
        self.show_fall.connect("notify::active", lambda *_: self.area.queue_draw())
        self.history = {}
        self._ap_scanning = False

        self.append(toolbar(labelled("Port", self.ports), labelled("Bands", self.band_set),
                            labelled("Ranges", self.ranges),
                            self.sweeps_box, self.button, clear, labelled("APs", self.show_aps), labelled("Waterfall", self.show_fall),
                            self.record, recordings,
                            self.status))
        self.connect("map", lambda *_: (self.refresh_ports(), self._scan_aps()))
        session.on("explorer", self._explorer_changed)
        session.on("scan", self._scanned)
        session.on("spectrum-choice", self._choice_from_elsewhere)
        GLib.timeout_add_seconds(AP_SCAN_EVERY, self._ap_tick)

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self.draw)
        self._add_gestures()
        self.stack = Gtk.Stack(hexpand=True, vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._graph_page(), "graph")
        self.stack.add_named(self._summary_page(), "summary")
        self.append(self.stack)
        self._rows: list[Adw.ActionRow] = []
        self._prepare_view()

    # ---------- many ranges: the summary list ----------

    def _graph_page(self) -> Gtk.Widget:
        """The panels, with a way back to the list when one range is shown from it."""
        back = Gtk.Button(child=Adw.ButtonContent(icon_name="go-previous-symbolic",
                                                  label="All ranges"))
        back.connect("clicked", lambda *_: self._show_summary())
        self.focus_title = Gtk.Label(xalign=0, css_classes=["heading"],
                                     ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        self.focus_bar = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_bottom=6,
                                 visible=False)
        self.focus_bar.append(back)
        self.focus_bar.append(self.focus_title)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.focus_bar)
        box.append(self.area)
        return box

    def _summary_page(self) -> Gtk.Widget:
        """
        One row per range: how loud its strongest signal is over the range's own floor, in
        green, amber or red as the report grades the other bands, where that is, and a small
        trace. Ranges that need a look come first; the quiet ones are folded away below.
        """
        self.summary_intro = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        self.busy_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.busy_list.set_placeholder(Gtk.Label(
            label="Nothing stands out: every range swept so far is quiet.", margin_top=18,
            margin_bottom=18, css_classes=["dim-label"]))
        self.quiet_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for listing in (self.busy_list, self.quiet_list):
            listing.set_sort_func(lambda a, b: a.index - b.index)        # frequency order
            listing.connect("row-activated", lambda _l, row: self._show_range(row.index))
        self.quiet = Gtk.Expander(child=self.quiet_list, margin_top=6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=6,
                      margin_bottom=18, margin_start=12, margin_end=12)
        for widget in (self.summary_intro, self.busy_list, self.quiet):
            box.append(widget)
        window = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        window.set_child(Adw.Clamp(maximum_size=1100, child=box))
        return window

    def _summary_wanted(self) -> bool:
        return self.combined and self.swept is not None and len(self.swept) > SUMMARY_ABOVE

    def _prepare_view(self) -> None:
        """The list for many ranges, the panels otherwise; rows for the ranges to be swept."""
        if not self.running:
            if self._mode() == "multi":
                self.combined, self.swept = True, merge_ranges(self._chosen())
                self.panel_specs = [(t, [i], (1.0, 1.0), None) for i, (t, _s, _e) in enumerate(self.swept)]
            else:
                self.swept, self.panel_specs = None, BAND_PANELS
        self.focus = None
        for row in self._rows:
            row.get_parent().remove(row)
        self._rows = []
        if not self._summary_wanted():
            self.focus_bar.set_visible(False)
            self.stack.set_visible_child_name("graph")
            return
        for index, (name, _start, _end) in enumerate(self.swept):
            row = Adw.ActionRow(title=GLib.markup_escape_text(name), activatable=True)
            row.index = index
            dot = Gtk.DrawingArea(content_width=14, content_height=14, valign=Gtk.Align.CENTER)
            dot.set_draw_func(self._draw_dot, index)
            trace = Gtk.DrawingArea(content_width=200, content_height=40, valign=Gtk.Align.CENTER)
            trace.set_draw_func(self._draw_mini, index)
            row.add_prefix(dot)
            row.add_suffix(trace)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.drawings = (dot, trace)
            self._rows.append(row)
            self.busy_list.append(row)
        self.summary_intro.set_label(
            f"{len(self.swept)} ranges. Tap one to see it full size. Each is graded by how far "
            f"its strongest signal, averaged, stands over the range's own floor: green under "
            f"{rfbands.QUIET_DB:.0f} dB, amber to {rfbands.NOISY_DB:.0f} dB, red above.")
        self._refresh_summary()
        self._show_summary()

    def _show_summary(self) -> None:
        self.focus = None
        self.focus_bar.set_visible(False)
        self.stack.set_visible_child_name("summary")

    def _show_range(self, index: int) -> None:
        self.focus = index
        self.cursor = None
        self.focus_title.set_label(self.swept[index][0])
        self.focus_bar.set_visible(True)
        self.stack.set_visible_child_name("graph")
        self.area.queue_draw()

    def _redraw(self) -> bool:
        """After new sweeps: the panels, and the summary list when it's showing."""
        self.area.queue_draw()
        self._refresh_summary()
        return False

    def _range_summary(self, index: int) -> dict:
        """What a range's row says: its grade, and the strongest signal over its floor."""
        data = self.ranges_data.get(index) or {}
        if data.get("error"):
            return {"grade": "none", "text": f"Couldn't sweep it: {data['error']}"}
        parts = data.get("parts") or []
        if not parts:
            return {"grade": "none",
                    "text": "Sweeping now…" if self.active == index and self.running else "Not swept yet"}
        freqs = np.concatenate([p["config"].start_mhz + p["config"].step_mhz * np.arange(len(p["trace"]))
                                for p in parts])
        avg = np.concatenate([np.asarray(p["trace"], dtype=float) for p in parts])
        peak = np.concatenate([np.asarray(p["peak"] or p["trace"], dtype=float)[:len(p["trace"])]
                               for p in parts])
        # Graded on the average against its median: the peak hold only ever climbs while the
        # page runs, and the noise's own spikes would make every range look busy
        floor = float(np.median(avg))
        over = float(avg.max()) - floor
        grade = "good" if over < rfbands.QUIET_DB else "fair" if over < rfbands.NOISY_DB else "poor"
        busy = float(np.mean(avg > floor + rfbands.BUSY_ABOVE_DB)) * 100
        top = int(np.argmax(peak))
        _name, start, end = self.swept[index]
        text = (f"{start:g}-{end:g} MHz · {over:.0f} dB over the floor ({floor:.0f} dBm) · "
                f"strongest {peak[top]:.0f} dBm at {freqs[top]:.3f} MHz"
                + (f" · {busy:.0f}% of it busy" if busy >= 1 else ""))
        if self.active == index and self.running:
            text += " · sweeping now"
        return {"grade": grade, "text": text}

    def _refresh_summary(self) -> None:
        if not self._rows:
            return
        quiet = 0
        for row in self._rows:
            found = self._range_summary(row.index)
            row.set_subtitle(GLib.markup_escape_text(found["text"]))
            row.grade = found["grade"]
            # Quiet ranges are folded away; the rest, and any not swept yet, stay in view
            wanted = self.quiet_list if found["grade"] == "good" else self.busy_list
            if row.get_parent() is not wanted:
                row.get_parent().remove(row)
                wanted.append(row)
            quiet += found["grade"] == "good"
            for drawing in row.drawings:
                drawing.queue_draw()
        self.quiet.set_visible(quiet > 0)
        self.quiet.set_label(f"{quiet} quiet range{'s' if quiet != 1 else ''}")

    def _draw_dot(self, _area, cr, width: int, height: int, index: int) -> None:
        row = self._rows[index] if index < len(self._rows) else None
        cr.arc(width / 2, height / 2, min(width, height) / 2 - 1, 0, 2 * math.pi)
        cr.set_source_rgb(*GRADE_COLOURS[getattr(row, "grade", "none")])
        cr.fill()

    def _draw_mini(self, _area, cr, width: int, height: int, index: int) -> None:
        """The range's average (yellow) and peak (red), as on its panel, without the axes."""
        cr.set_source_rgb(0.055, 0.09, 0.15)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        parts = (self.ranges_data.get(index) or {}).get("parts") or []
        avg = [v for p in parts for v in p["trace"]]
        peak = [v for p in parts for v in (p["peak"] or p["trace"])[:len(p["trace"])]]
        if len(avg) < 2:
            return
        for values, colour in ((peak, (0.9, 0.3, 0.3)), (avg, (1.0, 0.8, 0.2))):
            cr.set_source_rgb(*colour)
            cr.set_line_width(1)
            for number, value in enumerate(values):
                x = number / (len(values) - 1) * width
                y = (TOP_DBM - value) / (TOP_DBM - BOTTOM_DBM) * height
                cr.line_to(x, max(0.0, min(height, y))) if number else cr.move_to(x, y)
            cr.stroke()

    # ---------- gestures ----------

    def _add_gestures(self) -> None:
        """Drag for a readout, pinch to zoom the frequency axis, double-tap to see it all."""
        area = self.area

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda _g, x, y: self._point_at(x, y))
        drag.connect("drag-update", lambda g, dx, dy: self._point_at(
            g.get_start_point()[1] + dx, g.get_start_point()[2] + dy))
        area.add_controller(drag)

        tap = Gtk.GestureClick()
        tap.connect("pressed", self._tapped)
        area.add_controller(tap)

        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._pinch_begin)
        zoom.connect("scale-changed", self._pinched)
        zoom.connect("end", self._pinch_end)
        zoom.connect("cancel", self._pinch_end)
        area.add_controller(zoom)

        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        wheel.connect("scroll", self._scrolled)
        area.add_controller(wheel)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self._point_at(x, y))
        motion.connect("leave", lambda *_: self._point_at(None, None))
        area.add_controller(motion)

    def _panel_at(self, x: float, y: float) -> int | None:
        for index, panel in enumerate(self._panels):
            if panel["x"] <= x <= panel["x"] + panel["w"] and panel["y"] <= y <= panel["y"] + panel["h"]:
                return index
        return None

    def _freq_at(self, x: float, panel: dict | None = None) -> float | None:
        panel = panel or (self._panels[0] if self._panels else None)
        if panel is None:
            return None
        left, width = panel["x"] + LEFT, max(1, panel["w"] - LEFT - RIGHT)
        return panel["lo"] + (x - left) / width * (panel["hi"] - panel["lo"])

    def _point_at(self, x: float | None, y: float | None) -> None:
        if self._pinching:
            return
        index = None if x is None else self._panel_at(x, y)
        self.cursor = None if index is None else (index, self._freq_at(x, self._panels[index]))
        self.area.queue_draw()

    def _tapped(self, _gesture, n_press: int, _x, _y) -> None:
        if n_press == 2:
            self.view = None
            self.area.queue_draw()

    def _zoom(self, span_mhz: float, anchor_freq: float, anchor_x: float) -> None:
        """Shows ``span_mhz`` with ``anchor_freq`` at ``anchor_x``, kept inside what was swept."""
        full_span = self._full_span()
        if full_span is None or not self._panels:
            return
        start, end = full_span
        full = end - start
        span_mhz = max(min(MIN_SPAN_MHZ, full), min(full, span_mhz))
        if span_mhz >= full * 0.999:
            self.view = None
        else:
            panel = self._panels[0]
            width = max(1, panel["w"] - LEFT - RIGHT)
            low = anchor_freq - (anchor_x - panel["x"] - LEFT) / width * span_mhz
            low = max(start, min(end - span_mhz, low))
            self.view = (low, low + span_mhz)
        self.area.queue_draw()

    def _pinch_begin(self, gesture, _sequence) -> None:
        found, x, _y = gesture.get_bounding_box_center()
        if self._full_span() is None or not self._panels or not found:
            return                       # the combined view doesn't zoom
        self._pinching = True
        panel = self._panels[0]
        self._pinch = {"span": panel["hi"] - panel["lo"], "anchor": self._freq_at(x)}

    def _pinched(self, gesture, factor: float) -> None:
        found, x, _y = gesture.get_bounding_box_center()
        if self._pinch is None or not found or factor <= 0:
            return
        # The frequency that started between the fingers stays between them as they move
        self._zoom(self._pinch["span"] / factor, self._pinch["anchor"], x)

    def _pinch_end(self, *_args) -> None:
        self._pinch = None
        self._pinching = False

    def _scrolled(self, controller, dx: float, dy: float) -> bool:
        if self._full_span() is None or not self._panels:
            return False
        panel = self._panels[0]
        low, high = panel["lo"], panel["hi"]
        width = max(1, panel["w"] - LEFT - RIGHT)
        if controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            anchor = self.cursor[1] if self.cursor is not None else (low + high) / 2
            x = panel["x"] + LEFT + (anchor - low) / (high - low) * width
            self._zoom((high - low) * (1.1 if dy > 0 else 0.9), anchor, x)
            return True
        if self.view is None:
            return False
        unit = 0.1 if controller.get_unit() == Gdk.ScrollUnit.WHEEL else 0.004
        shift = (dx or dy) * unit * (high - low)
        self._zoom(high - low, (low + high) / 2 + shift, panel["x"] + LEFT + width / 2)
        return True

    # ---------- access points ----------

    def _aps_toggled(self) -> None:
        if self.show_aps.get_active():
            self._scan_aps()
        self.area.queue_draw()

    def _ap_tick(self) -> bool:
        self._scan_aps()
        return True

    def _scan_aps(self) -> None:
        """Refreshes the session's Wi-Fi scan, which the Discover page shares, for the markers."""
        recording_now = self.recorder is not None
        if self._ap_scanning or not (self.show_aps.get_active() or recording_now) \
                or not (self.get_mapped() or recording_now):
            return
        device = self.session.scan_device or wifi.choose_roles(wifi.interfaces())[0]
        if not device:
            return
        self._ap_scanning = True
        # Listen for beacons when we can: the analyzer would otherwise see the card's own probes
        passive = wifi.can_scan_passively()

        def done(readings, error) -> None:
            self._ap_scanning = False
            if error:
                if not self.running:
                    self.status.set_text(f"Wi-Fi scan for the AP markers failed: {error}")
                return
            self.session.last_scan = readings
            self.session.emit("scan")

        run_in_thread(lambda: wifi.scan(device, passive=passive), done)

    # ---------- recording ----------

    def _scanned(self) -> None:
        if self.recorder is not None:
            self.recorder.scan(self.session.last_scan)
        self.area.queue_draw()

    def _record_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            try:
                self.recorder = recording.Recorder()
            except OSError as error:
                self.status.set_text(f"Couldn't start recording: {error}")
                button.set_active(False)
                return
            self._recorded_at = {}
            self.session.spectrum_recording = True     # the Discover page leaves the analyzer be
            if not self.running:
                self.toggle()                    # recording means sweeping
            self._scan_aps()
            GLib.timeout_add_seconds(5, self._recording_tick)
            self.status.set_text(f"Recording to {self.recorder.path.name}")
            return
        recorder, self.recorder = self.recorder, None
        self.session.spectrum_recording = False
        if recorder is not None:
            recorder.close()
            minutes = (time.time() - recorder.started) / 60
            self.status.set_text(f"Recorded {minutes:.0f} min ({recorder.size / 1e6:.1f} MB) to "
                                 f"{recorder.path.name}. Recordings… shows what it caught.")

    def _recording_tick(self) -> bool:
        recorder = self.recorder
        if recorder is None:
            return False
        minutes = (time.time() - recorder.started) / 60
        self.record.set_tooltip_text(f"Recording for {minutes:.0f} min, {recorder.size / 1e6:.1f} MB. "
                                     "Press to stop.")
        return True

    def _open_recording(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Open a recording",
                                initial_folder=Gio.File.new_for_path(str(recording.folder())))
        dialog.open(self.get_root(), None, self._recording_chosen)

    def _recording_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        self.status.set_text("Reading the recording…")

        def done(summary, error) -> None:
            if error:
                self.status.set_text(f"Couldn't read it: {error}")
                return
            from .recording_view import show_recording     # it draws with this module's pieces

            self.status.set_text("")
            show_recording(self, path, summary)

        run_in_thread(lambda: recording.summarize(path), done)

    # ---------- running ----------

    # ---------- what to sweep ----------

    def _all_ranges(self) -> dict[tuple[float, float], tuple[str, float, float]]:
        return rangechoice.all_ranges()

    def _saved_sets(self) -> list[str]:
        return rangechoice.chosen_sets()

    def _saved_ranges(self) -> list[tuple[float, float]]:
        return rangechoice.chosen_keys()

    def _choice_from_elsewhere(self, source=None) -> None:
        """The survey setup changed the ranges: tick them here too."""
        if source is self:
            return
        self._set_lists.set_selected(self._saved_sets())
        self._fill_ranges(self._saved_ranges())
        self._range_changed()

    def _fill_ranges(self, chosen) -> None:
        """The Ranges list: every range of the ticked sets, under each set's name."""
        sets = set(self._set_lists.selected())
        sections = [(name if len(sets) > 1 else None,
                     [(range_key(r), r[0]) for r in ranges])
                    for name, ranges in self._sets if name in sets]
        self._range_lists.set_items(sections, chosen)
        self._label_choices()

    def _sets_changed(self, sets: list[str]) -> None:
        """A set ticked brings its ranges in, ticked; one unticked takes its own away."""
        before = set(self._range_lists.selected())
        listed = {name: [range_key(r) for r in ranges] for name, ranges in self._sets}
        kept = {k for name in sets for k in listed[name]}
        added = {k for name in sets for k in listed[name]} - set(self._range_lists.keys())
        self._fill_ranges((before & kept) | added)
        self._choice_changed()

    def _ranges_changed(self, _ranges) -> None:
        self._label_choices()
        self._choice_changed()

    def _chosen(self) -> list[tuple[str, float, float]]:
        known = self._all_ranges()
        return [known[k] for k in self._range_lists.selected()]

    def _mode(self) -> str:
        """"wifi" (the four Wi-Fi ranges, as before), "single", "multi", or "none"."""
        chosen = {range_key(r) for r in self._chosen()}
        if not chosen:
            return "none"
        if chosen == {range_key(r) for r in RANGES}:
            return "wifi"
        return "single" if len(chosen) == 1 else "multi"

    def _label_choices(self) -> None:
        sets = self._set_lists.selected()
        self.band_set.set_label("All" if len(sets) == len(self._sets) else
                                sets[0] if len(sets) == 1 else f"{len(sets)} sets" if sets else "None")
        chosen = self._chosen()
        mode = self._mode()
        text = ("All Wi-Fi" if mode == "wifi" else "None" if mode == "none" else
                chosen[0][0] if mode == "single" else
                "All" if len(chosen) == len(self._range_lists.keys()) else f"{len(chosen)} ranges")
        self.ranges.set_label(text if len(text) <= 26 else text[:24] + "…")

    def _is_combined(self) -> bool:
        return self._mode() in ("wifi", "multi")

    def _choice_changed(self) -> None:
        rangechoice.choose(self._set_lists.selected(), self._range_lists.selected())
        # The survey measures the same ranges
        self.session.emit("spectrum-choice", self)
        self._range_changed()

    def _range_changed(self, *_args) -> None:
        self.combined = self._is_combined()
        self.sweeps_box.set_sensitive(self.combined)
        if self.running:              # switch straight over rather than asking for a restart
            self.toggle()
            self.toggle()
        else:
            self.ranges_data = {}
            self.history = {}
            self.view = None
            self._prepare_view()
            self.area.queue_draw()

    def toggle(self, *_args) -> None:
        if self.running:
            self._stop.set()
            self.running = False
            self.active = None
            self.button.set_label("Start")
            self.area.queue_draw()
            return

        port = gps.port_device(selected_text(self.ports))
        if not port.startswith("/dev"):
            self.status.set_text("No serial port to open.")
            return
        if self._mode() == "none":
            self.status.set_text("Tick at least one range under Ranges.")
            return

        # A fresh event per run, so a loop that's still winding down can't be revived
        self._stop = threading.Event()
        self.running = True
        self.button.set_label("Stop")
        self.ranges_data = {}
        self.history = {}
        self.view = None
        mode = self._mode()
        self.combined = mode in ("wifi", "multi")
        chosen = self._chosen()
        if mode == "multi":
            # One panel per range, nearby ones together; cut to the analyzer's reach once open
            self.swept = merge_ranges(chosen)
            self.panel_specs = [(title, [i], (1.0, 1.0), None) for i, (title, _s, _e) in enumerate(self.swept)]
        else:
            self.swept, self.panel_specs = None, BAND_PANELS
        self._prepare_view()
        target = self._loop_all if self.combined else self._loop
        threading.Thread(target=target, args=(port, chosen[0] if mode == "single" else None, self._stop),
                         daemon=True).start()
        self.area.queue_draw()

    def refresh_ports(self, *_args) -> None:
        labels = gps.port_labels(prefer="rf")
        for device in (*self.session.hackrfs, *self.session.rtlsdrs):
            # Not a serial port, but it's chosen and started the same way
            labels.insert(0 if self.session.explorer is device else len(labels),
                          f"{device.device} — {device.model}")
        set_strings(self.ports, labels, "no serial ports")
        explorer = self.session.explorer
        if explorer is not None:
            _select_port(self.ports, explorer.device)

    def _explorer_changed(self) -> None:
        self.refresh_ports()
        if self.session.explorer is None and self.running:
            self._stop.set()
            self._stopped("The analyzer went away. Plug it back in and press Start.", self._stop)
        elif self.session.explorer is not None and not self.running:
            explorer = self.session.explorer
            self.status.set_text(f"{explorer.name} ready on {explorer.device}. Press Start.")

    def clear_peak(self, *_args) -> None:
        for data in list(self.ranges_data.values()):
            for part in data.get("parts", []):
                part["peak"] = list(part["trace"])
        self.area.queue_draw()

    def _stopped(self, message: str, stop: threading.Event) -> bool:
        if stop is self._stop:        # not a message from a run that's already been replaced
            self.running = False
            self.active = None
            self.button.set_label("Start")
            self.status.set_text(message)
            self._redraw()
        return False

    def _say(self, text: str, stop: threading.Event) -> None:
        GLib.idle_add(lambda: (stop is self._stop and self.status.set_text(text), False)[1])

    def _open(self, port: str):
        """The shared analyzer if it's on this port, otherwise this port opened and shared."""
        explorer = next((each for each in (self.session.explorer, *self.session.hackrfs,
                                           *self.session.rtlsdrs, *self.session.serial_analyzers)
                         if each is not None and each.alive and each.device == port),
                        next((a for a in self.session.serial_analyzers if a.device == port), None))
        if explorer is None or not explorer.alive or explorer.device != port:
            if explorer is not None and explorer.device == port:
                explorer.close()             # a dead one still holding the port
            explorer = detect.identify(port)
            if explorer is None:
                raise RuntimeError(f"Nothing on {port} answers as an RF Explorer or a tinySA.")
            GLib.idle_add(self.session.set_explorer, explorer)
        self.wifi_ranges = explorer.wifi_ranges
        return explorer

    def _loop_all(self, port: str, _choice, stop: threading.Event) -> None:
        """
        Goes round the US Wi-Fi ranges one at a time, since the analyzer sweeps one range at
        once, averaging a few sweeps of each. The lock is let go between ranges, so a survey
        measurement can take the analyzer without waiting for a whole cycle.
        """
        try:
            explorer = self._open(port)
        except Exception as error:
            GLib.idle_add(self._stopped, f"Couldn't open {port}: {error}", stop)
            return

        if self.swept is not None:
            # Cut each range to what the analyzer reaches; one wholly out of reach stays, to
            # say so on its panel
            low, high = getattr(explorer, "min_mhz", 0.0), explorer.max_mhz
            self.swept = [(name, max(start, low), min(end, high)) if start < high and end > low
                          else (name, start, end) for name, start, end in self.swept]
        elif not self.wifi_ranges:
            GLib.idle_add(self._stopped, f"The {explorer.name} doesn't reach Wi-Fi. Tick other "
                          "ranges, or choose another analyzer.", stop)
            return
        cycle = 0
        while not stop.is_set():
            cycle += 1
            for index, (name, start, end) in enumerate(self._ranges()):
                if not explorer.alive:
                    GLib.idle_add(self._stopped,
                                  f"The {explorer.name} on {explorer.device} stopped answering.", stop)
                    return
                paused = False
                while not explorer.lock.acquire(timeout=0.3):
                    if stop.is_set():
                        return
                    if not paused:
                        paused = True
                        self._say("Paused while a survey measurement uses the analyzer…", stop)
                try:
                    if stop.is_set():
                        return
                    self.active = index
                    GLib.idle_add(self._redraw)
                    self._say(f"Pass {cycle}: sweeping {name}, {self._sweep_count} "
                              f"sweep{'s' if self._sweep_count != 1 else ''}…", stop)
                    pieces = explorer.sweep_range(start, end, count=self._sweep_count, timeout=20)
                except ConnectionError as error:
                    GLib.idle_add(self._stopped, str(error), stop)
                    return
                except Exception as error:
                    # Say what went wrong on its panel, in the analyzer's words, and carry
                    # on with the other ranges
                    self.ranges_data[index] = {"error": str(error)}
                    GLib.idle_add(self._redraw)
                    continue
                finally:
                    explorer.lock.release()
                if stop.is_set():
                    return
                previous = self.ranges_data.get(index, {}).get("parts", [])
                parts = []
                for number, (config, sweeps) in enumerate(pieces):
                    data = np.array([s[: min(len(x) for x in sweeps)] for s in sweeps], dtype=float)
                    trace = spec.to_dbm(spec.to_mw(data).mean(axis=0)).tolist()
                    highest = data.max(axis=0).tolist()
                    old = previous[number]["peak"] if number < len(previous) else None
                    peak = ([max(a, b) for a, b in zip(old, highest)]
                            if old and len(old) == len(highest) else highest)
                    parts.append({"config": config, "trace": trace, "peak": peak})
                self.ranges_data[index] = {"parts": parts}
                self._remember(index, parts)
                GLib.idle_add(self._redraw)

    def _loop(self, port: str, choice: tuple[str, float, float], stop: threading.Event) -> None:
        try:
            explorer = self._open(port)
        except Exception as error:
            GLib.idle_add(self._stopped, f"Couldn't open {port}: {error}", stop)
            return

        _name, start, end = choice
        if start < explorer.max_mhz < end:            # 6 GHz, say, as far as it reaches
            end = explorer.max_mhz
        mine = -1                      # the analyzer generation this view tuned
        too_wide = False
        paused = False
        config = None
        tuned_at = 0.0
        while not stop.wait(0.2):
            if too_wide:
                self._loop_pieces(explorer, start, end, stop, too_wide=True)
                return
            # Some analyzers never deliver a sweep this wide in one go: after a while without
            # one, sweep it in pieces the way the combined view does
            if config is not None and 0 < tuned_at < time.monotonic() - FIRST_SWEEP_WAIT \
                    and not self.ranges_data.get(0):
                self._loop_pieces(explorer, start, end, stop)
                return
            if not explorer.alive:
                GLib.idle_add(self._stopped, f"The {explorer.name} on {explorer.device} stopped answering.", stop)
                return
            # A survey measurement holds the lock while it sweeps. Wait for it rather than
            # retuning the analyzer in the middle of someone else's capture.
            if not explorer.lock.acquire(timeout=0.2):
                if not paused:
                    paused = True
                    self._say("Paused while a survey measurement uses the analyzer…", stop)
                continue
            try:
                if explorer.generation != mine:
                    try:
                        config = explorer.configure(start, end)
                    except SpanError:
                        too_wide = True      # wider than it sweeps at once: in pieces, below
                        continue
                    except Exception as error:
                        GLib.idle_add(self._stopped, str(error), stop)
                        return
                    explorer.clear_sweeps()
                    mine = explorer.generation
                    tuned_at = time.monotonic()
                    self.ranges_data = {}
                    self.history = {}
                    self.view = None
                    paused = False
                    self._say(f"{config.start_mhz:.0f}-{config.end_mhz:.0f} MHz, "
                              f"{config.points} points, RBW {config.rbw_khz:.0f} kHz", stop)
                trace = explorer.latest()
            finally:
                explorer.lock.release()
            if stop.is_set():
                return
            if not trace:
                continue
            previous = (self.ranges_data.get(0, {}).get("parts") or [{}])[0].get("peak")
            peak = ([max(a, b) for a, b in zip(previous, trace)]
                    if previous and len(previous) == len(trace) else list(trace))
            self.ranges_data[0] = {"parts": [{"config": config, "trace": trace, "peak": peak}]}
            self._remember(0, self.ranges_data[0]["parts"])
            GLib.idle_add(self.area.queue_draw)

    def _loop_pieces(self, explorer, start: float, end: float, stop: threading.Event,
                     too_wide: bool = False) -> None:
        """
        The single range swept again and again in pieces, when it won't come in one: because
        no sweep that wide ever arrived, or because it's wider than the analyzer sweeps at
        once, in which case sweep_range works out the pieces itself.
        """
        if too_wide:
            self._say(f"{end - start:.0f} MHz is wider than the {explorer.name} sweeps at once, "
                      "so it's swept in pieces (updates are slower).", stop)
        else:
            explorer.remember_pieces(start, end, max(2, math.ceil((end - start) / 150)))
            self._say(f"No sweep arrived {end - start:.0f} MHz wide, so it's swept in pieces "
                      "(updates are slower).", stop)
        while not stop.is_set():
            if not explorer.alive:
                GLib.idle_add(self._stopped, f"The {explorer.name} on {explorer.device} stopped answering.", stop)
                return
            if not explorer.lock.acquire(timeout=0.3):
                continue
            try:
                pieces = explorer.sweep_range(start, end, count=1, timeout=20)
            except ConnectionError as error:
                GLib.idle_add(self._stopped, str(error), stop)
                return
            except Exception as error:
                GLib.idle_add(self._stopped, f"Couldn't sweep {start:.0f}-{end:.0f} MHz: {error}", stop)
                return
            finally:
                explorer.lock.release()
            if stop.is_set():
                return
            previous = (self.ranges_data.get(0) or {}).get("parts") or []
            parts = []
            for number, (config, sweeps) in enumerate(pieces):
                trace = list(sweeps[-1])
                old = previous[number]["peak"] if number < len(previous) else None
                peak = ([max(a, b) for a, b in zip(old, trace)]
                        if old and len(old) == len(trace) else list(trace))
                parts.append({"config": config, "trace": trace, "peak": peak})
            self.ranges_data[0] = {"parts": parts}
            self._remember(0, parts)
            GLib.idle_add(self.area.queue_draw)

    def _aps_shown(self) -> bool:
        return self.show_aps.get_active()

    def _ap_networks(self) -> list:
        return self.session.last_scan

    def _waterfall_shown(self) -> bool:
        return self.show_fall.get_active()


def _select_port(dropdown: Gtk.DropDown, device: str) -> None:
    model = dropdown.get_model()
    for index in range(model.get_n_items() if model else 0):
        if gps.port_device(model.get_string(index)) == device:
            dropdown.set_selected(index)
            return
