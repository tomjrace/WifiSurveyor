"""
The Monitor page: one network on one access point, watched for as long as it takes, drawn
like a patient monitor.

Three traces sweep across: the connection (a heartbeat per check, as tall as the internet's
reply was quick, flat and red when it's down), the beacons arriving from the access point (a
pulse per check, as tall as the share that arrived), and how busy the channel is (from the
analyzer). They start as a sweep of ten beats, then compress as the session grows until they
show the last half hour, and scroll back through two days. Every amber or red stretch says
why. Beside them the vital signs.
Below, the channel itself as the analyzer sees it, the whole session as a strip, and every
incident with its most likely cause, why, and what to do about it.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import cairo
import numpy as np
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from ..core import library, outage, watchreport
from ..core.colours import ssid_colour
from ..net import connectivity, monitor as monitor_mode, wifi
from ..net.power import KeepAwake
from ..net.ssidmonitor import (HOLDER, WIFI_STAGES, SsidMonitor, Target, band_name,
                               bands_compared, describe_siblings, partner, share_adapters,
                               trouble_words)
from .common import fit_dropdown, labelled, run_in_thread, scrolled, selected_text, set_strings, \
    status_label, toolbar
from .spectrum_view import _waterfall_image
from .watch_to_survey import WatchToSurvey

INTERVALS = (("Check every 1 s", 1.0), ("Check every 2 s", 2.0), ("Check every 5 s", 5.0))
SWEEP_BEATS = 10              # beats across the traces when the session starts
SHOW_SECONDS = 30 * 60        # then they compress until they show this much, then scroll
KEEP_SECONDS = 48 * 3600      # how far back they scroll
AXIS = 18                     # the clock times under the traces
ZOOM_STEPS = (10, 20, 40, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 4 * 3600, 8 * 3600,
              12 * 3600, 24 * 3600, 48 * 3600)
TICK_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600,
              43200, 86400)
PAGE = "Watch"                # this page, as other pages name it ("Watch is using …")
NO_ANALYZER = "No analyzer"
NO_MONITOR = "No monitor adapter"

BACKGROUND = (0.02, 0.03, 0.04)
GRID = (0.08, 0.16, 0.12)
TEXT = (0.75, 0.80, 0.85)
DIM = (0.42, 0.47, 0.52)
GREEN = (0.25, 0.95, 0.45)
AMBER = (1.00, 0.76, 0.15)
RED = (1.00, 0.28, 0.28)
CYAN = (0.35, 0.85, 1.00)
VIOLET = (0.72, 0.60, 1.00)
STATE_COLOURS = {"up": GREEN, "degraded": AMBER, "down": RED, "joining": DIM}
STATE_CODE = {"joining": 0, "up": 1, "degraded": 2, "down": 3}
CODE_COLOURS = (DIM, GREEN, AMBER, RED)
BEACON_COLOURS = (DIM, CYAN, AMBER, RED)


def _rgb(colour, alpha=1.0):
    return (*colour, alpha)


def _bump(dt: float, centre: float, width: float, height: float) -> float:
    return height * np.exp(-((dt - centre) / width) ** 2)


def ecg(dt: float, k: float) -> float:
    """A heartbeat ``dt`` seconds after the check that drew it, ``k`` scaling it to the beat."""
    return (_bump(dt, 0.08 * k, 0.030 * k, 0.12) + _bump(dt, 0.19 * k, 0.014 * k, -0.14)
            + _bump(dt, 0.225 * k, 0.018 * k, 1.0) + _bump(dt, 0.26 * k, 0.016 * k, -0.30)
            + _bump(dt, 0.45 * k, 0.055 * k, 0.26))


def pleth(dt: float, k: float, height: float) -> float:
    return _bump(dt, 0.20 * k, 0.09 * k, height) + _bump(dt, 0.50 * k, 0.11 * k, 0.35 * height)


def reply_height(net_ms: float | None) -> float:
    """A heartbeat's height: full for a quick internet reply, shorter as it slows."""
    if net_ms is None:
        return 0.2
    return float(np.clip(1.0 - 0.7 * math.log(max(net_ms, 30.0) / 30.0) / math.log(1000 / 30),
                         0.3, 1.0))


def beacon_words(beats: list[dict]) -> list[str]:
    """Why the beacon trace went red over these beats: only when the connection failed too."""
    if any(not b.get("assoc") for b in beats):
        return ["not connected, so no beacons counted"]
    shares = [b["beacons"] for b in beats if b.get("beacons") is not None
              and b.get("stage") in WIFI_STAGES]
    if not shares or min(shares) >= 0.9:
        return []
    return [f"only {min(shares):.0%} of beacons arriving while the Wi-Fi link failed",
            "interference, a busy channel or the AP pausing"]


def load_words(beat: dict) -> list[str]:
    """How loaded the access point and this link were at one beat, for the tapped readout."""
    words = []
    if beat.get("ap_clients") is not None:
        words.append(f"AP has {beat['ap_clients']} clients")
    if beat.get("heard") is not None:
        words.append(f"{beat['heard']} heard")
    if beat.get("ap_busy") is not None:
        words.append(f"AP finds the channel {beat['ap_busy']:.0%} busy")
    if beat.get("air_retry") is not None:
        words.append(f"{beat['air_retry']:.0%} of the AP's frames resent")
    if beat.get("tx_rate") is not None:
        words.append(f"link {beat['tx_rate']:.0f}/{beat.get('rx_rate') or 0:.0f} Mbit/s")
    if beat.get("retries") is not None:
        words.append(f"{beat['retries']:.0%} of ours resent")
    if beat.get("tx_failed"):
        words.append(f"{beat['tx_failed']} sends failed")
    return words


def beacon_code(share: float, away: bool, stage: str | None) -> int:
    """
    A beat's colour on the beacon trace. Missed beacons on their own are normal enough (a
    client saving power skips some) that nobody using the network notices, so they show only
    as a lower trace. Red is for beacons missing while the Wi-Fi link itself failed.
    """
    return 3 if away or (stage in WIFI_STAGES and share < 0.9) else 1


class History:
    """
    One session's beats and channel readings as arrays, copied a few at a time as they come,
    so two days of them draw in milliseconds.
    """

    def __init__(self, watcher) -> None:
        self.watcher = watcher
        self.seen = (0, 0)
        self.beats: list[dict] = []
        self.t = np.empty(0)
        self.code = np.empty(0, dtype=np.int8)
        self.height = np.empty(0)
        self.flat = np.empty(0, dtype=bool)
        self.share = np.empty(0)
        self.share_code = np.empty(0, dtype=np.int8)
        self.away = np.empty(0, dtype=bool)
        self.clients = np.empty(0)
        self.ap_busy = np.empty(0)
        self.reported = False               # the access point has said how many clients it has
        self.ct = np.empty(0)
        self.air = np.empty(0)

    def update(self) -> None:
        beats, readings, *seen = self.watcher.new_beats(self.seen)
        self.seen = tuple(seen)
        if beats:
            self.beats.extend(beats)
            shares = [1.0 if b.get("beacons") is None else b["beacons"] for b in beats]
            away = [not b.get("assoc") for b in beats]
            self.t = np.concatenate((self.t, [b["t"] for b in beats]))
            self.code = np.concatenate((self.code, np.array(
                [STATE_CODE.get(b["state"], 0) for b in beats], dtype=np.int8)))
            self.height = np.concatenate((self.height, [reply_height(b.get("net")) for b in beats]))
            self.flat = np.concatenate((self.flat, np.array(
                [b["state"] in ("down", "joining") for b in beats], dtype=bool)))
            self.share = np.concatenate((self.share, [max(0.08, s) for s in shares]))
            self.share_code = np.concatenate((self.share_code, np.array(
                [beacon_code(s, gone, b.get("stage")) for s, gone, b in zip(shares, away, beats)],
                dtype=np.int8)))
            # The access point's load: its own count when it reports one, else clients heard
            self.clients = np.concatenate((self.clients, [
                b["ap_clients"] if b.get("ap_clients") is not None else
                b["heard"] if b.get("heard") is not None else np.nan for b in beats]))
            self.ap_busy = np.concatenate((self.ap_busy, [
                np.nan if b.get("ap_busy") is None else b["ap_busy"] for b in beats]))
            self.reported = self.reported or any(b.get("ap_clients") is not None for b in beats)
            self.away = np.concatenate((self.away, np.array(away, dtype=bool)))
            old = int(np.searchsorted(self.t, self.t[-1] - KEEP_SECONDS - 3600))
            if old > 1000:
                del self.beats[:old]
                for name in ("t", "code", "height", "flat", "share", "share_code", "away",
                             "clients", "ap_busy"):
                    setattr(self, name, getattr(self, name)[old:])
        if readings:
            self.ct = np.concatenate((self.ct, [r["t"] for r in readings]))
            self.air = np.concatenate((self.air, [np.nan if r.get("airtime") is None
                                                  else r["airtime"] for r in readings]))
            old = int(np.searchsorted(self.ct, self.ct[-1] - KEEP_SECONDS - 3600))
            if old > 1000:
                self.ct, self.air = self.ct[old:], self.air[old:]


def beat_columns(t, height, code, flat, a: float, spp: float, width: int, shape, peak_at: float,
                 dip: float, stale: float, gap_code: int, end: float, mode: str):
    """
    A beat trace across ``width`` columns from time ``a``, ``spp`` seconds each, as arrays of
    (top, bottom, colour code) per column, NaN where there's nothing to draw. "fine": each
    beat's shape is drawn. "spikes": each beat is a spike in the column its peak falls in
    (``dip`` of it below the line), the way a monitor's compressed trace looks. "envelope",
    with beats closer than the pixels: each column is as tall as its beats on average. Either
    way a column is coloured the worst of its beats.
    """
    centre = a + (np.arange(width) + 0.5) * spp
    if not len(t):
        blank = np.full(width, np.nan)
        return blank, blank, np.zeros(width, dtype=np.int8)
    index = np.searchsorted(t, centre, "right") - 1
    covering = np.clip(index, 0, None)
    dt = centre - t[covering]
    stale_ = (index < 0) | (dt > stale)
    codes = np.where(stale_, gap_code, code[covering]).astype(np.int8)
    if mode == "fine":
        top = np.where(stale_ | flat[covering], 0.0, shape(dt) * height[covering])
        bottom = top
    else:
        top = np.zeros(width)
        column = np.floor((t + peak_at - a) / spp).astype(np.int64)
        inside = (column >= 0) & (column < width)
        heights = np.where(flat[inside], 0.0, height[inside])
        np.maximum.at(codes, column[inside], code[inside])
        if mode == "spikes":
            np.maximum.at(top, column[inside], heights)
            bottom = -dip * top
        else:
            counts = np.zeros(width)
            np.add.at(top, column[inside], heights)
            np.add.at(counts, column[inside], 1)
            top = np.divide(top, counts, out=np.zeros(width), where=counts > 0)
            bottom = top
    hidden = (index < 0) | (centre - spp / 2 > end)
    return np.where(hidden, np.nan, top), np.where(hidden, np.nan, bottom), codes


def busy_columns(t, airtime, a: float, spp: float, width: int, end: float):
    """The channel's busy share per column: the busiest reading in it, NaN where none is near."""
    centre = a + (np.arange(width) + 0.5) * spp
    if not len(t):
        return np.full(width, np.nan)
    value = np.interp(centre, t, airtime)
    column = np.floor((t - a) / spp).astype(np.int64)
    inside = (column >= 0) & (column < width) & ~np.isnan(airtime)
    np.fmax.at(value, column[inside], airtime[inside])
    index = np.searchsorted(t, centre, "right") - 1
    stale = (index < 0) | (centre - t[np.clip(index, 0, None)] > max(10.0, 2 * spp))
    value[stale | (centre - spp / 2 > end)] = np.nan
    return value


class MonitorView(Gtk.Box):
    def __init__(self, session) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._spectra: dict[int, tuple] = {}     # watcher id -> (sweeps seen, spectrum_rows)
        self.session = session
        self.monitor: SsidMonitor | None = None       # the radio shown, while watching
        self.finished: SsidMonitor | None = None      # the last session, still shown after Stop
        self.watchers: list[SsidMonitor] = []         # every radio of this session, one per band
        self._partner = None                          # the network's radio on its other band
        self.all_readings: list = []
        self.readings: list = []            # the networks in the dropdown, in order
        self.analyzers: list = []
        self.starting = False
        self._shown_incidents: tuple = ()
        self._provisional: dict[int, dict] = {}      # id(watcher) -> its ongoing outage, weighed so far
        self._provisional_at: dict[int, float] = {}
        self._provisional_busy: set[int] = set()
        self._tick_id = 0
        self._awake: KeepAwake | None = None   # holds the computer awake while watching
        self._histories: dict[int, History] = {}     # id(watcher) -> its beats as arrays
        self._span: float | None = None     # seconds across the traces; None: fits the session
        self._end: float | None = None      # the time at their right edge; None: the latest
        self._probe: float | None = None    # a moment tapped on the traces, explained
        self._waves: dict = {}              # panel -> (left, top, width, height, a, b) as last drawn
        self._dragging: dict | None = None
        self._pinching: dict | None = None
        self._every = 0.05
        self._drawn_at = 0.0

        self.networks = fit_dropdown(Gtk.DropDown(model=Gtk.StringList.new(["Scanning…"])), 34)
        self.networks.connect("notify::selected", lambda *_: self._network_changed())
        rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Look for networks again")
        rescan.connect("clicked", lambda *_: self._scan())
        self.client = fit_dropdown(Gtk.DropDown(), 18)
        self.client.connect("notify::selected", lambda *_: self._adapters_changed())
        self.listener = fit_dropdown(Gtk.DropDown(), 18)
        self.listener.connect("notify::selected", lambda *_: self._describe_evidence())
        self.analyzer_menu = fit_dropdown(Gtk.DropDown(), 18)
        self.analyzer_menu.connect("notify::selected", lambda *_: self._describe_evidence())
        self.every = Gtk.DropDown(model=Gtk.StringList.new([label for label, _s in INTERVALS]),
                                  selected=1)
        self.button = Gtk.Button(label="Start watching", css_classes=["suggested-action"])
        self.button.connect("clicked", self._toggle)
        self.controls = [self.networks, rescan, self.client, self.listener, self.analyzer_menu,
                         self.every]
        self.append(toolbar(
            labelled("Network", self.networks), rescan, labelled("Join with", self.client),
            labelled("Listen with", self.listener), labelled("Analyzer", self.analyzer_menu),
            self.every, self.button))

        # The same network on its other band, watched alongside with adapters of its own
        self.pair_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.pair_switch.connect("notify::active", lambda *_: self._pair_toggled())
        self.pair_label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.pair_client = fit_dropdown(Gtk.DropDown(), 18)
        self.pair_client.connect("notify::selected", lambda *_: self._describe_evidence())
        self.pair_listener = fit_dropdown(Gtk.DropDown(), 18)
        self.pair_listener.connect("notify::selected", lambda *_: self._describe_evidence())
        self.pair_row = toolbar(self.pair_switch, self.pair_label,
                                labelled("Join with", self.pair_client),
                                labelled("Listen with", self.pair_listener))
        self.pair_row.set_visible(False)
        self.controls += [self.pair_switch, self.pair_client, self.pair_listener]
        self.append(self.pair_row)

        self.status = status_label("Choose the network and access point to watch.")
        self.status.set_margin_start(12)
        self.status.set_margin_end(12)
        self.append(self.status)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12,
                       margin_end=12, margin_top=8, margin_bottom=12)
        self.evidence = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                  css_classes=["dim-label"], use_markup=True)
        body.append(self.evidence)

        # A panel of traces per band, stacked on one time axis so the bands line up: the
        # second shows only while two are watched
        self.panels = [self._panel(), self._panel()]
        self.vitals = self.panels[0]
        self.panels[1].set_visible(False)
        for panel in self.panels:
            body.append(panel)

        steering = Gtk.Box(spacing=6)
        for icon, tip, action in (
                ("go-previous-symbolic", "Earlier", lambda *_: self._scroll_by(-0.5)),
                ("go-next-symbolic", "Later", lambda *_: self._scroll_by(0.5)),
                ("zoom-out-symbolic", "Show a longer stretch", lambda *_: self._zoom(2.0)),
                ("zoom-in-symbolic", "Show a shorter stretch", lambda *_: self._zoom(0.5))):
            button = Gtk.Button(icon_name=icon, tooltip_text=tip)
            button.connect("clicked", action)
            steering.append(button)
        self.live_button = Gtk.Button(child=Adw.ButtonContent(icon_name="go-last-symbolic",
                                                              label="Latest"),
                                      tooltip_text="Back to the latest, filling as it goes",
                                      sensitive=False)
        self.live_button.connect("clicked", lambda *_: self._to_live())
        steering.append(self.live_button)
        self.view_label = Gtk.Label(xalign=0, hexpand=True, wrap=True,
                                    wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=["dim-label"])
        steering.append(self.view_label)
        body.append(steering)

        self.channel_area = Gtk.DrawingArea(content_height=300, hexpand=True)
        self.channel_area.set_draw_func(self._draw_channel)
        body.append(self.channel_area)

        body.append(Gtk.Label(label="The whole session", xalign=0, css_classes=["heading"]))
        self.strip = Gtk.DrawingArea(content_height=64, hexpand=True)
        self.strip.set_draw_func(self._draw_strip)
        tap = Gtk.GestureClick()
        tap.connect("released", self._strip_tap)
        self.strip.add_controller(tap)
        body.append(self.strip)

        self.findings_title = Gtk.Label(label="Worth fixing, outage or not", xalign=0,
                                        css_classes=["heading"], visible=False)
        body.append(self.findings_title)
        self.findings = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    css_classes=["boxed-list"], visible=False)
        body.append(self.findings)

        heading = Gtk.Box(spacing=8)
        heading.append(Gtk.Label(label="What happened", xalign=0, hexpand=True,
                                 css_classes=["heading"]))
        self.export = Gtk.Button(label="Export incidents…", sensitive=False)
        self.export.connect("clicked", self._export)
        self.open_folder = Gtk.Button(label="Open session folder", sensitive=False)
        self.open_folder.connect("clicked", self._open_folder)
        self.report = Gtk.Button(label="Save report…", sensitive=False,
                                 tooltip_text="This watch as a report of its own")
        self.report.connect("clicked", self._save_report)
        self.to_survey = Gtk.Button(label="Add to survey", sensitive=False,
                                    tooltip_text="Put this watch on the open survey's plan, "
                                                 "where you watched from, and in its report")
        self.to_survey.connect("clicked", self._add_to_survey)
        heading.append(self.report)
        heading.append(self.to_survey)
        heading.append(self.export)
        heading.append(self.open_folder)
        body.append(heading)
        self.patterns = Gtk.Label(xalign=0, wrap=True, visible=False)
        body.append(self.patterns)
        self.incidents = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                     css_classes=["boxed-list"])
        self.incidents.set_placeholder(Gtk.Label(
            label="Nothing yet. Leave it running: every disconnect and outage is listed here "
                  "with its most likely cause.", wrap=True, margin_top=18, margin_bottom=18,
            margin_start=12, margin_end=12, css_classes=["dim-label"]))
        body.append(self.incidents)
        self.earlier_title = Gtk.Label(label="Earlier watches", xalign=0, css_classes=["heading"],
                                       margin_top=12, visible=False)
        body.append(self.earlier_title)
        self.earlier = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                   css_classes=["boxed-list"], visible=False)
        body.append(self.earlier)
        self.append(scrolled(body))

        session.on("explorer", lambda *_: self._fill_analyzers())
        session.on("scan", self._fill_networks)
        self.connect("map", self._mapped)
        self._fill_adapters()
        self._fill_analyzers()
        self._fill_networks()
        self._fill_earlier()
        GLib.timeout_add(1000, self._refresh)

    def _panel(self) -> Gtk.DrawingArea:
        """One band's traces, which drag, pinch and tap move the time axis of every band's."""
        panel = Gtk.DrawingArea(content_height=400, hexpand=True)
        panel.set_draw_func(self._draw_vitals)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        pinch = Gtk.GestureZoom()
        pinch.connect("begin", self._pinch_begin)
        pinch.connect("scale-changed", self._pinch_scale)
        pinch.connect("end", self._pinch_end)
        pinch.connect("cancel", self._pinch_end)
        drag.group(pinch)
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        wheel.connect("scroll", self._wheel)
        for controller in (drag, pinch, wheel):
            panel.add_controller(controller)
        return panel

    def _shown(self) -> list:
        """The radios on screen: this session's, watching or stopped."""
        return list(self.watchers) if self.watchers else \
            [w for w in (self.monitor or self.finished,) if w is not None]

    def _lead(self):
        """The radio the shared time axis follows: the band chosen first."""
        shown = self._shown()
        return shown[0] if shown else None

    # ---------- choosing what to watch ----------

    def _mapped(self, *_args) -> None:
        self._fill_adapters()
        if not self.readings:
            self._scan()

    def _fill_adapters(self) -> None:
        if self.monitor is not None:
            return
        ifaces = wifi.interfaces()
        labels = [wifi.label_for(i) for i in ifaces]
        names = [i.name for i in ifaces]
        scan, monitor_role = wifi.choose_roles(ifaces)
        current = wifi.device_from_label(selected_text(self.client))
        set_strings(self.client, labels or ["none"], "none")
        # The scanning role joins, leaving the best monitor-mode adapter free to listen
        want = current if current in names else scan
        if want in names:
            self.client.set_selected(names.index(want))
        self._fill_listeners(monitor_role)
        self._fill_pair()

    def _fill_listeners(self, preferred: str | None = None) -> None:
        client = wifi.device_from_label(selected_text(self.client))
        ifaces = [i for i in wifi.interfaces() if i.name != client and i.monitor_capable]
        labels = [NO_MONITOR] + [wifi.label_for(i) for i in ifaces]
        current = wifi.device_from_label(selected_text(self.listener))
        set_strings(self.listener, labels)
        names = [None] + [i.name for i in ifaces]
        want = current if current in names else preferred if preferred in names else None
        if want is None and len(names) > 1 and selected_text(self.listener) != NO_MONITOR:
            want = names[1]
        self.listener.set_selected(names.index(want) if want in names else 0)
        self._describe_evidence()

    def _adapters(self) -> list[dict]:
        """Every adapter with what share_adapters needs to know: its channels, monitor mode."""
        return [{"name": i.name, "label": wifi.label_for(i), "monitor": i.monitor_capable,
                 "freqs": monitor_mode.supported_freqs(i.name), "rank": wifi._monitor_rank(i)}
                for i in wifi.interfaces()]

    def _fill_pair(self) -> None:
        """The second band's row: shown when the network has one, with adapters that reach it."""
        reading = self._partner
        self.pair_row.set_visible(reading is not None)
        if reading is None:
            return
        self.pair_label.set_markup(
            f"Also watch <b>{band_name(reading.freq_mhz)}</b> · ch {reading.channel} · "
            f"{reading.bssid.upper()} · {reading.rssi} dBm")
        adapters = self._adapters()
        able = [a for a in adapters if any(abs(f - reading.freq_mhz) < 1 for f in a["freqs"])]
        set_strings(self.pair_client, [a["label"] for a in able] or ["none"], "none")
        set_strings(self.pair_listener, [NO_MONITOR] + [a["label"] for a in able if a["monitor"]])
        on = self.pair_switch.get_active()
        for widget in (self.pair_client, self.pair_listener):
            widget.set_sensitive(on and self.monitor is None)

    def _network_changed(self) -> None:
        """A new network: find its other band, and share the adapters out between them."""
        if self.monitor is not None:
            return
        reading = self._chosen()
        everything = self.all_readings or self.readings
        self._partner = partner(reading, everything) if reading is not None else None
        if self._partner is not None:
            # Offered on when there are adapters enough for each band to have its own
            plan = share_adapters([reading.freq_mhz, self._partner.freq_mhz], self._adapters())
            self.pair_switch.set_active(all(join for join, _listen in plan))
        self._fill_pair()
        self._apply_plan()
        self._describe_evidence()

    def _pair_toggled(self) -> None:
        if self.monitor is not None:
            return
        self._fill_pair()
        self._apply_plan()
        self._describe_evidence()

    def _apply_plan(self) -> None:
        """Picks each band's adapters (share_adapters); any of them can still be changed."""
        reading = self._chosen()
        if reading is None:
            return
        freqs = [reading.freq_mhz]
        if self._partner is not None and self.pair_switch.get_active():
            freqs.append(self._partner.freq_mhz)
        adapters = self._adapters()
        labels = {a["name"]: a["label"] for a in adapters}
        plan = share_adapters(freqs, adapters)
        (join, listen), rest = plan[0], plan[1:]
        if join:
            _choose(self.client, labels[join])
        _choose(self.listener, labels[listen] if listen else NO_MONITOR)
        if rest:
            join, listen = rest[0]
            if join:
                _choose(self.pair_client, labels[join])
            _choose(self.pair_listener, labels[listen] if listen else NO_MONITOR)

    def _adapters_changed(self) -> None:
        if self.monitor is None:
            self._fill_listeners()

    def _fill_analyzers(self) -> None:
        if self.monitor is not None:
            return
        pool = [a for a in self.session.all_analyzers() if a.max_mhz >= 2500]
        explorer = self.session.explorer
        pool.sort(key=lambda a: a is not explorer)
        self.analyzers = pool
        set_strings(self.analyzer_menu, [NO_ANALYZER] + [f"{a.model or a.name} ({a.device})"
                                                         for a in pool])
        self.analyzer_menu.set_selected(1 if pool else 0)
        self._describe_evidence()

    def _scan(self) -> None:
        device = wifi.device_from_label(selected_text(self.client)) or self.session.scan_device
        if not device or self.monitor is not None:
            return
        self.status.set_text(f"Looking for networks with {device}…")

        def done(readings, error) -> None:
            if error:
                self.status.set_text(f"Couldn't scan: {error}")
                return
            self._fill_networks(readings)
            self.status.set_text("Choose the network and access point to watch.")

        run_in_thread(lambda: wifi.scan(device), done)

    def _fill_networks(self, readings=None) -> None:
        if self.monitor is not None:
            return
        readings = readings if readings is not None else list(self.session.last_scan or [])
        if not readings:
            return
        current = self._chosen()
        named = [r for r in readings if r.ssid]
        named.sort(key=lambda r: (-r.rssi, r.ssid.lower()))     # the strongest first
        self.readings = named
        self.all_readings = list(readings)
        labels = [f"{r.ssid} · {r.bssid.upper()} · ch {r.channel} ({r.band} GHz) · {r.rssi} dBm"
                  for r in named]
        set_strings(self.networks, labels or ["No networks found"])
        if current is not None:
            for index, r in enumerate(named):
                if r.bssid.lower() == current.bssid.lower():
                    self.networks.set_selected(index)
        self._network_changed()

    def _chosen(self):
        index = self.networks.get_selected()
        if 0 <= index < len(self.readings) and selected_text(self.networks).startswith(self.readings[index].ssid):
            return self.readings[index]
        return None

    def _target(self, reading=None) -> Target | None:
        reading = reading or self._chosen()
        if reading is None:
            return None
        everything = self.all_readings or self.readings
        found = outage.siblings(reading.bssid, [r.bssid for r in everything])
        freqs = {r.bssid.lower(): r.freq_mhz for r in everything}
        return Target(ssid=reading.ssid, bssid=reading.bssid.upper(), freq=reading.freq_mhz,
                      center=reading.span_center_mhz, width=reading.width_mhz or 20,
                      siblings={b.lower(): freqs[b.lower()] for b in found})

    def _analyzer(self):
        index = self.analyzer_menu.get_selected() - 1
        return self.analyzers[index] if 0 <= index < len(self.analyzers) else None

    def _describe_evidence(self) -> None:
        """What can be told apart with what's plugged in, and what's missing for the rest."""
        if self.monitor is not None:
            return
        target = self._target()
        client = wifi.device_from_label(selected_text(self.client))
        listener = wifi.device_from_label(selected_text(self.listener)) \
            if selected_text(self.listener) != NO_MONITOR else None
        analyzer = self._analyzer()
        lines = []
        if client:
            lines.append(f"<b>✓ {client} joins</b> and checks the link, gateway, internet and DNS, "
                         "and reads the reason for every disconnect.")
            if client == self.session.scan_device:
                lines.append("   It's also the Discover page's scanning adapter, and scans take it "
                             "off the channel for a moment. Leave Discover's listening off, or "
                             "plug in another adapter for it.")
        if listener:
            root = "" if wifi.is_root() else " <b>Needs root: use Relaunch as root in the menu.</b>"
            lines.append(f"<b>✓ {listener} listens</b> in monitor mode on the channel: beacons and "
                         "the access point's clock, so a reboot or a dead radio shows, and every "
                         f"deauthentication, so a forged one shows.{root}")
        else:
            lines.append("<b>✗ No monitor adapter.</b> Without one, a forged deauthentication "
                         "can't be told from a real one, and a silent access point from a lost "
                         "client. Plug in a second adapter that does monitor mode (an Alfa).")
        if analyzer is not None and target is not None and analyzer.max_mhz < target.span[1]:
            lines.append(f"<b>✗ The {analyzer.model or analyzer.name} doesn't reach channel "
                         f"{target.channel}.</b>")
        elif analyzer is not None:
            lines.append(f"<b>✓ The {analyzer.model or analyzer.name} watches the channel</b> "
                         "only, so interference and a channel gone quiet show.")
        else:
            lines.append("<b>✗ No analyzer.</b> With a tinySA or RF Explorer the channel itself "
                         "is watched, which tells interference from an access point gone quiet.")
        pair = self._pair_plan()
        if pair is not None:
            reading, join, listen = pair
            band = band_name(reading.freq_mhz)
            if join:
                lines.append(f"<b>✓ {band} too</b>, at the same time: {join} joins channel "
                             f"{reading.channel}" + (f" and {listen} listens" if listen else
                                                     ", with no monitor adapter") +
                             ". When one band drops and the other doesn't, it's that radio; "
                             "both at once, the unit or what's behind it.")
                if analyzer is not None:
                    lines.append(f"   The {analyzer.model or analyzer.name} takes turns between "
                                 "the two channels, so each is swept half as often.")
            else:
                lines.append(f"<b>✗ No adapter left to join {band}.</b> Plug in another, or watch "
                             "one band.")
        if target is not None:
            lines.append(f"Other radios of this unit: {describe_siblings(target)}. Scanned for "
                         "while it's down, they tell one radio failing from the whole unit.")
        self.evidence.set_markup("\n".join(lines))

    def _pair_plan(self):
        """(reading, join, listen) for the second band when it's to be watched too, or None."""
        if self._partner is None or not self.pair_switch.get_active():
            return None
        join = wifi.device_from_label(selected_text(self.pair_client))
        listen = wifi.device_from_label(selected_text(self.pair_listener)) \
            if selected_text(self.pair_listener) != NO_MONITOR else None
        return self._partner, join or None, listen

    # ---------- starting and stopping ----------

    def _toggle(self, *_args) -> None:
        if self.monitor is not None:
            self._confirm_stop()
            return
        if self.starting:
            return
        target = self._target()
        client = wifi.device_from_label(selected_text(self.client))
        if target is None or not client:
            self.status.set_text("Choose a network to watch, and an adapter to join it with.")
            return
        listener = wifi.device_from_label(selected_text(self.listener)) \
            if selected_text(self.listener) != NO_MONITOR else None
        # One plan per band: (target, joins, listens, label)
        plans = [(target, client, listener, "")]
        pair = self._pair_plan()
        if pair is not None:
            reading, join, listen = pair
            if not join:
                self.status.set_text(f"Choose an adapter to join {band_name(reading.freq_mhz)} "
                                     "with, or switch the second band off.")
                return
            plans = [(target, client, listener, band_name(target.freq)),
                     (self._target(reading), join, listen, band_name(reading.freq_mhz))]
        devices = [d for _t, c, l, _b in plans for d in (c, l) if d]
        if len(devices) != len(set(devices)):
            self.status.set_text("Each band needs adapters of its own: one adapter can't join or "
                                 "listen on two channels at once.")
            return
        listeners = [l for _t, _c, l, _b in plans if l]
        if listeners and not wifi.is_root():
            self.status.set_text("Listening in monitor mode needs root: use Relaunch as root in "
                                 "the menu, or choose No monitor adapter.")
            return
        if listeners and self.session.monitor_users:
            users = " and ".join(sorted(self.session.monitor_users))
            self.status.set_text(f"{users} is using the monitor adapter: stop it there first.")
            return
        moving = [(c, on) for t, c, _l, _b in plans
                  if (on := (wifi.link(c) or {}).get("ssid")) and on != t.ssid]
        if moving:
            which = ", ".join(f"{c} (on {on} now)" for c, on in moving)
            dialog = Adw.AlertDialog(
                heading=f"Move {' and '.join(c for c, _on in moving)} to {target.ssid}?",
                body=f"{which} will join {target.ssid} and stay there until you stop watching, "
                     "so if one is this computer's own connection, the computer moves too.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("go", "Move")
            dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", lambda _d, answer: answer == "go" and self._start(plans))
            dialog.present(self)
            return
        self._start(plans)

    def _start(self, plans: list[tuple]) -> None:
        interval = INTERVALS[self.every.get_selected()][1]
        chosen = self._chosen()
        secured = bool(chosen and chosen.secured)
        analyzer = self._analyzer()
        ssid = plans[0][0].ssid
        self.starting = True
        self.button.set_sensitive(False)
        self.status.set_text(f"Checking {ssid} can be joined…")

        def work():
            if secured and not connectivity.saved_profile(ssid):
                raise RuntimeError(f"There's no saved password for {ssid} on this computer: "
                                   "connect to it once from the system's Wi-Fi settings, then "
                                   "start again.")
            started, trouble = [], []
            for target, client, listener, label in plans:
                # The analyzer (one, shared) takes turns between the channels it reaches
                reach = analyzer if analyzer is not None and analyzer.max_mhz >= target.span[1] else None
                watcher = SsidMonitor(target, client, listener, reach, interval, label=label)
                try:
                    watcher.start()
                except Exception as error:
                    watcher.stop()              # puts back whatever it had changed
                    if not started:
                        raise
                    trouble.append(f"{label} couldn't start: {error}")
                    continue
                started.append(watcher)
            return started, trouble

        def done(result, error) -> None:
            self.starting = False
            self.button.set_sensitive(True)
            if error:
                self.status.set_text(f"Couldn't start: {error}")
                return
            watchers, trouble = result
            self.watchers = watchers
            self.monitor = watchers[0]
            self.finished = None
            for watcher in watchers:
                self.session.radio_holds[watcher.device] = HOLDER
                if watcher.monitor_device:
                    self.session.radio_holds[watcher.monitor_device] = HOLDER
                    self.session.monitor_users.add(PAGE)
            self.button.set_label("Stop watching")
            self.button.remove_css_class("suggested-action")
            self.button.add_css_class("destructive-action")
            for control in self.controls:
                control.set_sensitive(False)
            self.export.set_sensitive(True)
            self.open_folder.set_sensitive(True)
            self.report.set_sensitive(True)
            self.to_survey.set_sensitive(True)
            self._awake = KeepAwake(f"Watching {watchers[0].target.ssid}",
                                    on_sleep=lambda: [w.pause() for w in watchers],
                                    on_wake=lambda: [w.resume() for w in watchers],
                                    on_change=self._describe_running)
            self._awake.start()
            self._begin_view()
            self._tick_id = self.vitals.add_tick_callback(self._tick)
            if trouble:
                self.status.set_text(" · ".join(trouble))

        run_in_thread(work, done)

    def _begin_view(self) -> None:
        """A new session on screen: a panel per band, the time axis back to live."""
        for panel, watcher in zip(self.panels, [None, *self.watchers[1:]]):
            if panel is not self.vitals:
                panel.set_visible(watcher is not None)
        self.pair_row.set_visible(False)            # its adapters are in the description now
        self.strip.set_content_height(40 + 30 * max(1, len(self.watchers)))
        self._histories = {}
        self._provisional = {}
        self._provisional_at = {}
        self._shown_incidents = ()
        self._findings_key = None
        self._span = self._end = self._probe = None
        self._describe_running()
        self._describe_view()
        self._refresh()
        self._redraw()

    def _confirm_stop(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Stop watching?",
            body="The session so far is kept, with every incident, in its folder"
                 + ("s (one per band)." if len(self.watchers) > 1 else "."))
        dialog.add_response("cancel", "Keep watching")
        dialog.add_response("stop", "Stop")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, answer: answer == "stop" and self.stop())
        dialog.present(self)

    def _release(self, watchers) -> None:
        if self._awake is not None:
            self._awake.stop()
            self._awake = None
        for watcher in watchers:
            for device in (watcher.device, watcher.monitor_device):
                self.session.radio_holds.pop(device, None)
        self.session.monitor_users.discard(PAGE)

    def stop(self) -> None:
        shown, self.monitor = self.monitor, None
        watchers = list(self.watchers)
        if shown is None:
            return
        self._release(watchers)
        self.button.set_label("Start watching")
        self.button.remove_css_class("destructive-action")
        self.button.add_css_class("suggested-action")
        self.button.set_sensitive(False)
        self.status.set_text("Stopping: the adapters are leaving the network…" if len(watchers) > 1
                             else "Stopping: the adapter is leaving the network…")
        if self._tick_id:
            self.vitals.remove_tick_callback(self._tick_id)
            self._tick_id = 0
        self.finished = shown

        def work():
            for watcher in watchers:
                watcher.stop()
            # Over: its log shrinks to what's worth keeping (outage.compact)
            for watcher in watchers:
                if watcher.log is not None:
                    try:
                        outage.compact(watcher.log.path)
                    except (OSError, ValueError):
                        pass                  # left as it is; the next start tries again

        def done(*_args) -> None:
            self.button.set_sensitive(True)
            self._fill_earlier()
            for control in self.controls:
                control.set_sensitive(True)
            folders = [str(w.log.folder) for w in watchers if w.log]
            self.status.set_text(f"Stopped. The session is in {' and '.join(folders)}" if folders
                                 else "Stopped.")
            self._fill_adapters()
            self._fill_pair()
            self._describe_evidence()
            self._describe_view()
            self._redraw()

        run_in_thread(work, done)

    def stop_now(self) -> None:
        """Stops before returning, for when the app is about to go: the adapters are put back."""
        watchers, self.monitor = (list(self.watchers) if self.monitor is not None else []), None
        self._release(watchers)
        for watcher in watchers:
            watcher.stop()

    @property
    def running(self) -> bool:
        return self.monitor is not None

    def _describe_running(self) -> None:
        if self.monitor is None:
            return
        lines = []
        for watcher in self.watchers:
            t = watcher.target
            parts = [f"Watching <b>{GLib.markup_escape_text(t.ssid)}</b>"
                     + (f" on <b>{watcher.label}</b>" if watcher.label else "")
                     + f" on {t.bssid}, channel {t.channel} ({t.width} MHz), joined with {watcher.device}"]
            parts.append(f"listening with {watcher.monitor_device}" if watcher.watch is not None
                         else "no monitor adapter")
            if watcher.analyzer is not None:
                parts.append(f"channel watched by the {watcher.analyzer.model or watcher.analyzer.name}")
            lines.append(", ".join(parts) + ".")
            if watcher.log:
                lines.append(f"   Saved as it goes to {GLib.markup_escape_text(str(watcher.log.folder))}")
            for source, note in sorted(watcher.notes.items()):
                lines.append(f"   ⚠ {GLib.markup_escape_text(note)}")
        if self._awake is not None and self._awake.note:
            lines.append(GLib.markup_escape_text(self._awake.note) + ".")
        self.evidence.set_markup("\n".join(lines))

    # ---------- keeping the page up to date ----------

    def _tick(self, _widget, clock) -> bool:
        now = clock.get_frame_time() / 1e6
        if self.get_mapped() and now - self._drawn_at >= self._every:
            self._drawn_at = now
            for panel in self.panels:
                if panel.get_visible():
                    panel.queue_draw()
        return True

    def _redraw(self) -> None:
        for area in (*self.panels, self.channel_area, self.strip):
            area.queue_draw()

    def _refresh(self) -> bool:
        shown = self._shown()
        if not shown:
            return True
        if self.monitor is not None:
            self.status.set_text(shown[0].status if len(shown) == 1 else
                                 " · ".join(f"{w.label}: {w.status}" for w in shown))
            self._describe_running()
            self._describe_view()
        if self.get_mapped():
            self.channel_area.queue_draw()
            self.strip.queue_draw()
        now = time.time()
        groups, signature = [], []
        for watcher in shown:
            with watcher.lock:
                incidents = list(watcher.incidents)
                current = watcher.current
            key = id(watcher)
            if current is not None and current.end is None and key not in self._provisional_busy \
                    and now - self._provisional_at.get(key, 0.0) > 3:
                # Weighing the evidence so far takes the watch's lock: never on the main loop
                self._provisional_at[key] = now
                self._provisional_busy.add(key)

                def weighed(found, _error, key=key) -> None:
                    self._provisional_busy.discard(key)
                    if found is not None:
                        self._provisional[key] = found

                run_in_thread(lambda w=watcher, c=current: w.provisional(c), weighed)
            groups.append((watcher, incidents, current))
            signature.append(tuple((i.id, i.end, bool(i.diagnosis), len(i.scans)) for i in incidents)
                             + ((self._provisional.get(key, {}).get("cause"),) if current is not None else ()))
        if tuple(signature) != self._shown_incidents:
            self._shown_incidents = tuple(signature)
            self._fill_incidents(groups)
        self._fill_findings(shown)
        return True

    def _fill_findings(self, watchers) -> None:
        """Problems seen that didn't (yet) cause an outage, each with what to do, every band's."""
        rows = []
        for watcher in watchers:
            band = f"{watcher.label}: " if len(watchers) > 1 else ""
            for server, failed, asked in watcher.unreliable_dns():
                rows.append((f"{band}DNS server {server} failed {failed} of {asked} lookups",
                             "A client that asks it waits seconds before trying the next server, which "
                             "feels like the network hanging. Fix it, or stop handing it out in DHCP."))
            if watcher.forged_blocked:
                rows.append((f"{band}{len(watcher.forged_blocked)} forged deauthentication frame(s) rejected",
                             "Something is sending deauthentications in the access point's name. "
                             "Protected management frames stopped them for this adapter; clients "
                             "without them would have dropped. Make PMF required."))
            if watcher.adapter_resets:
                rows.append((f"{band}{watcher.device} dropped off the computer "
                             f"{len(watcher.adapter_resets)} time(s)",
                             "The adapter itself vanished and came back, a USB disconnect: its "
                             "port, cable, power or the adapter, not the network. Those moments "
                             "aren't counted against the network. Try another port or adapter."))
            losses = watcher.losses_total
            if losses:
                rows.append((f"{band}The adapter reported beacon loss {losses} time(s) while connected",
                             "Beacons stopped arriving for a moment without a disconnect: interference, "
                             "a busy channel or the access point pausing. Watch the channel trace at "
                             "those times."))
        key = tuple(title for title, _ in rows)
        if key == getattr(self, "_findings_key", None):
            return
        self._findings_key = key
        child = self.findings.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.findings.remove(child)
            child = following
        for title, subtitle in rows:
            row = Adw.ActionRow(title=GLib.markup_escape_text(title),
                                subtitle=GLib.markup_escape_text(subtitle), subtitle_lines=0)
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
            self.findings.append(row)
        self.findings.set_visible(bool(rows))
        self.findings_title.set_visible(bool(rows))

    def _fill_incidents(self, groups: list[tuple]) -> None:
        """Every band's incidents in one list, newest first, each saying which band it was."""
        child = self.incidents.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            if isinstance(child, Gtk.ListBoxRow):
                self.incidents.remove(child)
            child = following
        several = len(groups) > 1
        rows = []
        for watcher, incidents, current in groups:
            for incident in incidents:
                found = incident.diagnosis if incident.diagnosis else (
                    self._provisional.get(id(watcher), {}) if incident is current else {})
                rows.append((incident.start, watcher, incident, found))
        for _start, watcher, incident, found in sorted(rows, key=lambda r: r[0], reverse=True):
            self.incidents.append(self._incident_row(incident, found, ongoing=not incident.diagnosis,
                                                     band=watcher.label if several else ""))
        notes = outage.patterns([i for _w, incidents, _c in groups for i in incidents])
        if several:
            # An adapter dropping out says nothing about the network, so it's left out
            notes += bands_compared([(w.label, [i for i in incidents if not i.adapter_lost])
                                     for w, incidents, _c in groups])
        self.patterns.set_visible(bool(notes))
        self.patterns.set_text("\n".join(f"• {note}" for note in notes))

    def _incident_row(self, incident, found: dict, ongoing: bool, band: str = "") -> Gtk.Widget:
        when = time.strftime("%a %d %b %H:%M:%S", time.localtime(incident.start))
        if incident.end is None:
            how_long = f"for {outage.duration(time.time() - incident.start)} so far"
        else:
            how_long = f"for {outage.duration(incident.end - incident.start)}"
        title = (f"{band} " if band else "") + \
            f"#{incident.id} · {when} · {outage.kind_name(incident)} {how_long}"
        confidence = found.get("confidence", "")
        lead = "So far" if ongoing else "Most likely"
        subtitle = (f"{lead}: {found.get('title', 'gathering evidence…')}"
                    + (f" ({confidence} confidence)" if confidence and not ongoing else ""))
        row = Adw.ExpanderRow(title=GLib.markup_escape_text(title),
                              subtitle=GLib.markup_escape_text(subtitle), subtitle_lines=0)
        grade = {"high": "success", "medium": "warning", "low": "error"}.get(confidence, "dim-label")
        row.add_prefix(Gtk.Image(icon_name="emblem-important-symbolic" if not ongoing
                                 else "content-loading-symbolic", css_classes=[grade]))

        def lines(heading: str, items: list[str]) -> None:
            if items:
                row.add_row(Adw.ActionRow(
                    title=heading, subtitle=GLib.markup_escape_text("\n".join(f"• {i}" for i in items)),
                    subtitle_lines=0, css_classes=["property"]))

        lines("Why", found.get("why", []))
        lines("What to do", found.get("do", []))
        lines("Also possible", [a["title"] for a in found.get("alternatives", [])])
        lines("To be surer", found.get("missing", []))
        facts = []
        if incident.reason is not None:
            who = ("sent by the access point" if incident.by_ap else "decided by this adapter"
                   if incident.locally_generated else "")
            facts.append(f"{outage.reason_text(incident.reason)}" + (f", {who}" if who else ""))
        if incident.signal_before is not None:
            facts.append(f"signal just before: {incident.signal_before:.0f} dBm"
                         + (f" (usually {incident.signal_baseline:.0f})"
                            if incident.signal_baseline is not None else ""))
        if incident.beacons_before is not None:
            facts.append(f"beacons arriving just before: {incident.beacons_before:.0%}")
        mon = incident.monitor or {}
        if mon.get("deauth_count"):
            facts.append(f"deauthentication frames heard: {mon['deauth_count']}"
                         + (", to every client" if mon.get("broadcast_deauth") else ""))
        if mon.get("clients_rejoined"):
            facts.append(f"clients that rejoined afterwards: {mon['clients_rejoined']}"
                         f" (of {mon.get('clients_before', 0)} heard before)")
        if incident.rejoin_errors:
            facts.append(f"rejoin attempts that failed: {len(incident.rejoin_errors)} "
                         f"(last: {incident.rejoin_errors[-1]})")
        if incident.pcap:
            facts.append(f"frames saved: {incident.pcap}")
        lines("Evidence", facts)
        return row

    # ---------- exporting ----------

    def _export(self, *_args) -> None:
        shown = self._shown()
        if not shown:
            return
        watcher = shown[0]
        incidents = []
        for each in shown:
            with each.lock:
                incidents += [i for i in each.incidents if i.diagnosis]
        incidents.sort(key=lambda i: i.start)
        dialog = Gtk.FileDialog(title="Export incidents",
                                initial_name=f"{watcher.target.ssid} incidents.csv")
        try:
            folder = library.documents_dir()
            if folder.is_dir():
                dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))
        except Exception:
            pass

        def chosen(dialog, result) -> None:
            try:
                path = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            try:
                Path(path).write_text(outage.incidents_csv(incidents), encoding="utf-8")
                from ..core.project import hand_back

                hand_back(path)
                self.status.set_text(f"Saved {len(incidents)} incident(s) to {path}")
            except OSError as error:
                self.status.set_text(f"Couldn't save: {error}")

        dialog.save(self.get_root(), None, chosen)

    def _watch_summary(self) -> dict | None:
        """This session, as core.watchreport summarises it from its logs (one per band)."""
        logs = [outage.session_file(w.log.folder) for w in self._shown() if w.log is not None]
        logs = [log for log in logs if log is not None]
        return watchreport.summary(logs) if logs else None

    def _save_report(self, *_args) -> None:
        watch = self._watch_summary()
        if watch is not None:
            self.save_report(watch)

    def save_report(self, watch: dict) -> None:
        """A watch as a report of its own: saved where you choose, then opened."""
        dialog = Gtk.FileDialog(
            title="Save the watch report",
            initial_name=f"{watch['ssid'] or 'hidden'} watch "
                         f"{time.strftime('%Y-%m-%d %H.%M', time.localtime(watch['start']))}.html")
        try:
            folder = library.documents_dir()
            if folder.is_dir():
                dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))
        except Exception:
            pass

        def chosen(dialog, result) -> None:
            try:
                path = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            try:
                from .. import __version__
                from ..core.project import hand_back

                Path(path).write_text(watchreport.build_html(watch, __version__), encoding="utf-8")
                hand_back(path)
            except OSError as error:
                self.status.set_text(f"Couldn't save: {error}")
                return
            self.status.set_text(f"Saved the watch report to {path}")
            Gtk.FileLauncher(file=Gio.File.new_for_path(path)).launch(self.get_root(), None, None)

        dialog.save(self.get_root(), None, chosen)

    def _add_to_survey(self, *_args) -> None:
        watch = self._watch_summary()
        if watch is not None:
            self.add_to_survey(watch)

    def add_to_survey(self, watch: dict) -> None:
        """Puts a watch on a survey: the open one, a recent one, or one you pick."""
        watch = {k: v for k, v in watch.items() if k != "logs"}    # this computer's paths only
        WatchToSurvey(self.session, self.get_root(), watch).present(self.get_root())

    def _fill_earlier(self) -> None:
        """The watches saved before, newest first: each can be reported, added to a survey or deleted."""
        running = {str(w.log.folder) for w in self.watchers if w.log is not None} \
            if self.monitor is not None else set()

        def done(watches, error) -> None:
            while (row := self.earlier.get_row_at_index(0)) is not None:
                self.earlier.remove(row)
            for watch in watches or []:
                self.earlier.append(self._earlier_row(watch))
            self.earlier_title.set_visible(bool(watches))
            self.earlier.set_visible(bool(watches))

        run_in_thread(lambda: watchreport.earlier(skip=running), done)

    def _earlier_row(self, watch: dict) -> Gtk.Widget:
        bands = ", ".join(b["band"] for b in watch["bands"] if b["band"])
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(
                f"{watch['ssid'] or '(hidden)'} · "
                + time.strftime("%d %b %H:%M", time.localtime(watch["start"]))
                + (f" · {bands}" if bands else "")),
            subtitle=GLib.markup_escape_text(watchreport.headline(watch)), subtitle_lines=0)
        report = Gtk.Button(label="Report…", valign=Gtk.Align.CENTER)
        report.connect("clicked", lambda *_: self.save_report(watch))
        to_survey = Gtk.Button(label="Add to survey", valign=Gtk.Align.CENTER)
        to_survey.connect("clicked", lambda *_: self.add_to_survey(watch))
        delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Delete this watch's logs and captures",
                            css_classes=["flat"])
        delete.connect("clicked", lambda *_: self._delete_earlier(watch))
        for button in (report, to_survey, delete):
            row.add_suffix(button)
        return row

    def _delete_earlier(self, watch: dict) -> None:
        folders = sorted({str(Path(log).parent) for log in watch.get("logs", [])})
        size = sum(f.stat().st_size for folder in folders for f in Path(folder).iterdir()
                   if f.is_file())
        dialog = Adw.AlertDialog(
            heading=f"Delete the watch of {watch['ssid'] or 'the hidden network'}?",
            body=f"{watchreport.headline(watch)}. Its logs and frame captures "
                 f"({size / 1e6:.1f} MB) are deleted. A survey it was added to keeps its summary.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(_dialog, response: str) -> None:
            if response != "delete":
                return
            import shutil

            for folder in folders:
                shutil.rmtree(folder, ignore_errors=True)
            self._fill_earlier()

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    def _open_folder(self, *_args) -> None:
        logs = [w.log for w in self._shown() if w.log is not None]
        if not logs:
            return
        watcher = self._lead()
        # Two bands: their folders sit side by side, so the folder holding both
        folder = logs[0].folder if len(logs) == 1 else logs[0].folder.parent
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(str(folder)))

        def done(launcher, result) -> None:
            try:
                launcher.launch_finish(result)
            except GLib.Error:
                self.status.set_text(f"The session is in {watcher.log.folder}")

        launcher.launch(self.get_root(), None, done)

    # ---------- the traces' time axis: fills, compresses, then scrolls ----------

    def _history_of(self, watcher) -> History:
        history = self._histories.get(id(watcher))
        if history is None or history.watcher is not watcher:
            history = self._histories[id(watcher)] = History(watcher)
        history.update()
        return history

    def _live_end(self, watcher, now: float) -> float:
        """The newest moment there is: now while watching, the last beat once stopped."""
        if watcher.running:
            return now
        history = self._history_of(watcher)
        return float(history.t[-1]) if len(history.t) else watcher.started + 1

    def _window(self, watcher, now: float) -> tuple[float, float]:
        """
        The times at the traces' left and right edges. It starts as a sweep of ten beats, then
        compresses as the session grows until it shows half an hour, then scrolls. Zoomed or
        scrolled by hand, it keeps the span chosen (and, scrolled back, the moment chosen).
        """
        live = self._live_end(watcher, now)
        fill = max(20.0, watcher.interval * SWEEP_BEATS)
        span = self._span if self._span is not None else min(max(fill, live - watcher.started),
                                                             SHOW_SECONDS)
        if self._end is None:
            if self._span is None and live - watcher.started < fill:
                return watcher.started, watcher.started + fill
            return live - span, live
        return self._end - span, self._end

    def _move_view(self, end: float | None, span: float) -> None:
        """Shows ``span`` seconds up to ``end`` (None: up to now), kept within the last 48 hours."""
        watcher = self.monitor or self.finished
        if watcher is None:
            return
        live = self._live_end(watcher, time.time())
        self._span = max(min(span, KEEP_SECONDS), watcher.interval * 5)
        if end is not None:
            # No further back than the session's start (or two days) at the left edge
            first = max(watcher.started, live - KEEP_SECONDS)
            end = max(end, min(live, first + self._span))
            if end >= live - self._span * 0.002:
                end = None
        self._end = end
        self._describe_view()
        self._redraw()

    def _zoom(self, factor: float) -> None:
        watcher = self.monitor or self.finished
        if watcher is None:
            return
        a, b = self._window(watcher, time.time())
        span = b - a
        steps = [s for s in ZOOM_STEPS if (s > span * 1.01 if factor > 1 else s < span * 0.99)]
        new = (min(steps) if factor > 1 else max(steps)) if steps else span
        self._move_view(None if self._end is None else (a + b) / 2 + new / 2, new)

    def _scroll_by(self, share: float) -> None:
        watcher = self.monitor or self.finished
        if watcher is None:
            return
        a, b = self._window(watcher, time.time())
        self._move_view(b + share * (b - a), b - a)

    def _to_live(self) -> None:
        self._span = self._end = None
        self._describe_view()
        self._redraw()

    def _describe_view(self) -> None:
        watcher = self.monitor or self.finished
        if watcher is None:
            self.view_label.set_text("")
            self.live_button.set_sensitive(False)
            return
        now = time.time()
        a, b = self._window(watcher, now)
        live = self._live_end(watcher, now)
        span = outage.duration(b - a)
        if self._end is None:
            words = f"Showing the last {span}" if b - a < live - watcher.started else \
                f"Showing all {outage.duration(live - watcher.started)} so far"
        else:
            words = (f"Showing {span} from {time.strftime('%H:%M', time.localtime(a))}, "
                     f"{outage.duration(live - b)} before the latest")
        self.view_label.set_text(f"{words} · drag sideways to look back (up to 48 h), pinch to "
                                 "zoom, tap a trace for why it's that colour")
        self.live_button.set_sensitive(self._end is not None or self._span is not None)

    def _waves_at(self, panel, x: float, y: float) -> float | None:
        """The time under a point on a panel's traces, or None off them."""
        frame = self._waves.get(panel)
        if frame is None:
            return None
        left, top, width, height, a, b = frame
        if not (left <= x < left + width and top <= y < top + height):
            return None
        return a + (x - left) / width * (b - a)

    def _drag_begin(self, gesture, x: float, y: float) -> None:
        watcher = self._lead()
        panel = gesture.get_widget()
        if watcher is None or self._waves_at(panel, x, y) is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        a, b = self._window(watcher, time.time())
        self._dragging = {"a": a, "b": b, "x": x, "y": y, "panning": False, "moved": 0.0,
                          "panel": panel}

    def _drag_update(self, gesture, dx: float, dy: float) -> None:
        drag = self._dragging
        if drag is None or self._pinching is not None:
            return
        drag["moved"] = max(drag["moved"], math.hypot(dx, dy))
        if not drag["panning"]:
            if abs(dx) > 12 and abs(dx) > abs(dy) * 1.5:
                drag["panning"] = True
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            elif abs(dy) > 12:
                self._dragging = None              # a scroll of the page: let it have it
                gesture.set_state(Gtk.EventSequenceState.DENIED)
                return
            else:
                return
        width = self._waves[drag["panel"]][2]
        span = drag["b"] - drag["a"]
        self._move_view(drag["b"] - dx / width * span, span)

    def _drag_end(self, _gesture, _dx: float, _dy: float) -> None:
        drag, self._dragging = self._dragging, None
        if drag is None or drag["panning"] or drag["moved"] > 10 or self._pinching is not None:
            return
        # A tap: say what the traces show at that moment, or put the readout away
        when = self._waves_at(drag["panel"], drag["x"], drag["y"])
        if when is None:
            return
        frame = self._waves[drag["panel"]]
        near = self._probe is not None and abs(when - self._probe) / (frame[5] - frame[4]) \
            * frame[2] < 16
        self._probe = None if near else when
        self._redraw()

    def _pinch_begin(self, gesture, _sequence) -> None:
        watcher = self._lead()
        panel = gesture.get_widget()
        found, x, y = gesture.get_bounding_box_center()
        if watcher is None or not found or self._waves_at(panel, x, y) is None:
            return
        a, b = self._window(watcher, time.time())
        self._pinching = {"a": a, "b": b, "x": x, "at": self._waves_at(panel, x, y), "panel": panel}

    def _pinch_scale(self, _gesture, scale: float) -> None:
        pinch = self._pinching
        if pinch is None or scale <= 0:
            return
        left, _top, width = self._waves[pinch["panel"]][:3]
        span = (pinch["b"] - pinch["a"]) / scale
        share = (pinch["x"] - left) / width
        self._move_view(pinch["at"] + (1 - share) * span, span)

    def _pinch_end(self, *_args) -> None:
        self._pinching = None

    def _wheel(self, controller, dx: float, dy: float) -> bool:
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK and dy:
            self._zoom(2.0 if dy > 0 else 0.5)
            return True
        if dx:
            self._scroll_by(dx * 0.1)
            return True
        return False                                # up and down scroll the page

    def _strip_tap(self, _gesture, _n, x: float, _y: float) -> None:
        """A tap on the session strip brings that moment onto the traces."""
        shown = [w for w in self._shown() if w.started]
        width = self.strip.get_width()
        if not shown or width <= 0:
            return
        watcher = shown[0]
        label_w = 52.0 if len(shown) > 1 else 0.0
        now = time.time()
        start = min(w.started for w in shown)
        end = max(max(self._live_end(w, now) for w in shown), start + 1)
        when = start + max(0.0, x - label_w) / max(1.0, width - label_w) * (end - start)
        a, b = self._window(watcher, now)
        self._move_view(when + (b - a) / 2, b - a)

    # ---------- drawing: the monitor ----------

    def _draw_vitals(self, area, cr: cairo.Context, width: int, height: int) -> None:
        cr.set_source_rgb(*BACKGROUND)
        cr.paint()
        cr.select_font_face("Cantarell")
        shown = self._shown()
        index = self.panels.index(area) if area in self.panels else 0
        watcher = shown[index] if index < len(shown) else None
        now = time.time()
        wide = width >= 720
        tiles_w = 270 if wide else width
        tile_rows = 0 if wide else 2
        banner = 34
        waves_w = width - tiles_w if wide else width
        waves_h = height - banner - AXIS - (tile_rows * 64 if not wide else 0)

        self._draw_banner(cr, width, banner, watcher, now)
        if watcher is None:
            self._waves.pop(area, None)
            self._flat_waves(cr, 0, banner, waves_w, waves_h + AXIS, "Choose a network and press "
                                                                     "Start watching")
            self._draw_tiles(cr, waves_w if wide else 0, banner if wide else banner + waves_h + AXIS,
                             tiles_w, height - banner if wide else tile_rows * 64, None, now)
            return

        history = self._history_of(watcher)
        interval = watcher.interval
        # Every band on the first one's time axis, so the panels line up
        a, b = self._window(self._lead(), now)
        end = self._live_end(watcher, now)
        columns = max(1, int(waves_w))
        spp = (b - a) / columns
        self._waves[area] = (0, banner, waves_w, waves_h, a, b)
        # Redrawn as often as the trace moves a pixel, 20 times a second at most
        self._every = max(0.05, min(1.0, spp))
        # With an analyzer, a fifth lane: the channel's spectrum on the same time axis
        spectrum = self._spectrum_of(watcher) if watcher.analyzer is not None or watcher.traces \
            else None
        lane_count = 5 if watcher.analyzer is not None or spectrum else 4
        lane = waves_h / lane_count
        busy_colour = ssid_colour(watcher.target.ssid)
        load_scale, load_hint = self._load_scale(watcher, history, a, b)
        names = (("CONNECTION", GREEN, "taller = quicker internet reply"),
                 ("BEACONS", CYAN, "height = share of beacons arriving"),
                 ("CHANNEL BUSY", busy_colour,
                  "line = the AP's own figure" if np.isfinite(history.ap_busy).any() else ""),
                 ("AP LOAD", VIOLET, load_hint))
        for number in range(lane_count):
            self._lane_frame(cr, 0, banner + number * lane, waves_w, lane)
        self._draw_axis(cr, 0, banner, waves_w, waves_h, a, b)

        # The beat takes half the time between checks, as a resting heart's does
        k = interval
        mode = "fine" if spp <= 0.02 * k else "spikes" if spp < interval * 0.5 else "envelope"
        envelope = mode == "envelope"
        stale = interval * 2.5
        lanes = []
        connection = beat_columns(history.t, history.height, history.code, history.flat, a, spp,
                                  columns, lambda dt: ecg(dt, k), 0.225 * k, 0.3, stale, 3,
                                  end, mode)
        lanes.append((connection, CODE_COLOURS, 0.62, 0.70, envelope))
        beacons = beat_columns(history.t, history.share, history.share_code, history.away, a,
                               spp, columns, lambda dt: pleth(dt, k, 1.0), 0.2 * k, 0.0, stale,
                               3, end, mode)
        lanes.append((beacons, BEACON_COLOURS, 0.55, 0.80, envelope))
        ones = np.ones(columns, dtype=np.int8)
        if watcher.analyzer is not None:
            busy = busy_columns(history.ct, history.air, a, spp, columns, end)
            lanes.append(((busy, busy, ones), (DIM, busy_colour), 0.75, 0.88, True))
        else:
            lanes.append(None)
        if watcher.analyzer is None and not np.isfinite(history.ap_busy).any():
            self._lane_note(cr, 12, banner + 2 * lane + lane / 2, "no analyzer chosen")
        if watcher.watch is not None or np.isfinite(history.clients).any():
            clients = busy_columns(history.t, history.clients / load_scale, a, spp, columns, end)
            lanes.append(((clients, clients, ones), (DIM, VIOLET), 0.75, 0.88, True))
        else:
            lanes.append(None)
            self._lane_note(cr, 12, banner + 3 * lane + lane / 2,
                            "needs a monitor adapter, or an AP that reports its load")
        for number, item in enumerate(lanes):
            if item is None:
                continue
            shape, palette, scale, baseline, fill = item
            top = banner + number * lane
            self._trace(cr, shape, palette, top + lane * baseline, lane * scale, fill,
                        1.6 if mode == "spikes" else 2.2)
        if np.isfinite(history.ap_busy).any():
            # The access point's own measure of how busy the channel is, over the analyzer's
            reported = busy_columns(history.t, history.ap_busy, a, spp, columns, end)
            self._trace(cr, (reported, reported, ones), (DIM, TEXT),
                        banner + 2 * lane + lane * 0.88, lane * 0.75, False, 1.4)
        for number, (label, colour, hint) in enumerate(names):
            self._lane_heading(cr, 0, banner + number * lane, label, colour,
                               hint if number < 2 and mode != "envelope" else
                               "average of each moment's checks" if number == 0 else
                               hint if number >= 2 else "")
        if lane_count == 5:
            self._draw_spectrum_lane(cr, spectrum, banner + 4 * lane, waves_w, lane, a, spp,
                                     columns)
        self._label_runs(cr, connection, CODE_COLOURS, banner, waves_w, a, spp, history,
                         trouble_words)
        self._label_runs(cr, beacons, BEACON_COLOURS, banner + lane, waves_w, a, spp, history,
                         beacon_words)
        if end < b:
            # Still filling the first sweep: the wiper, where the newest beat is drawn
            cr.set_source_rgba(1, 1, 1, 0.25)
            cr.rectangle((end - a) / spp, banner, 2, waves_h)
            cr.fill()
        if self._probe is not None and a <= self._probe <= b:
            self._draw_probe(cr, (self._probe - a) / spp, banner, waves_w, waves_h, history,
                             watcher)
        self._draw_tiles(cr, waves_w if wide else 0, banner if wide else banner + waves_h + AXIS,
                         tiles_w, height - banner if wide else tile_rows * 64, watcher, now)

    def _spectrum_of(self, watcher):
        """
        The channel's sweeps so far, on one frequency grid (watchreport.spectrum_rows): the
        minute rows of the long view, then every recent sweep. Worked out again only when a
        sweep has arrived since.
        """
        key = (watcher.channel_count, len(watcher.minutes))
        cached = self._spectra.get(id(watcher))
        if cached is not None and cached[0] == key:
            return cached[1]
        with watcher.lock:
            traces = list(watcher.traces)
            minutes = [m for m in watcher.minutes if m.get("avg")]
        first = traces[0]["t"] if traces else float("inf")
        rows = [m for m in minutes if m["t"] < first] + traces       # minutes until sweeps take over
        found = watchreport.spectrum_rows(rows) if rows else None
        self._spectra[id(watcher)] = (key, found)
        return found

    def _draw_spectrum_lane(self, cr, spectrum, top, width, height, a, spp, columns) -> None:
        """The channel's spectrum as a waterfall lying on its side: time across, frequency up."""
        if spectrum is None:
            self._lane_note(cr, 12, top + height / 2, "waiting for the analyzer's first sweep")
        else:
            times, matrix, (low, high) = spectrum
            levels = watchreport.grid_columns(times, matrix, a, spp, columns, stale=75.0)
            surface, _pixels = _waterfall_image(levels)
            cr.save()
            cr.rectangle(0, top, width, height)
            cr.clip()
            cr.translate(0, top)
            cr.scale(width / max(1, columns), height / max(1, levels.shape[0]))
            cr.set_source_surface(surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            cr.restore()
            text = f"{low:.0f}–{high:.0f} MHz"
            cr.set_font_size(10)
            extent = cr.text_extents(text).x_advance
            cr.set_source_rgba(*BACKGROUND, 0.75)
            cr.rectangle(width - extent - 14, top + 2, extent + 10, 14)
            cr.fill()
            cr.set_source_rgb(*TEXT)
            cr.move_to(width - extent - 9, top + 13)
            cr.show_text(text)
        self._lane_heading(cr, 0, top, "SPECTRUM", TEXT, "quiet blue to strong red")

    def _load_scale(self, watcher, history, a: float, b: float) -> tuple[float, str]:
        """The AP LOAD lane's top, in clients (5 at least), and what its trace counts."""
        lo, hi = np.searchsorted(history.t, [a, b])
        shown = history.clients[lo:hi]
        most = float(np.nanmax(shown)) if np.isfinite(shown).any() else 0.0
        scale = max(5.0, math.ceil(most / 5) * 5)
        if history.reported:
            what = "clients the AP says it has"
        elif watcher.watch is not None:
            what = "clients heard in the last 5 min (the AP doesn't report a count)"
        else:
            return scale, ""
        return scale, f"{what} · top = {scale:.0f}"

    def _trace(self, cr, shape, palette, base_y, amplitude, fill, line_width) -> None:
        """One lane's trace from beat_columns' (top, bottom, colour) per column."""
        tops, bottoms, codes = (s.tolist() for s in shape)
        cr.set_line_width(line_width)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        points: list[tuple[float, float]] = []
        colour = None

        def finish() -> None:
            if colour is None or len(points) < 2:
                return
            cr.move_to(*points[0])
            for point in points[1:]:
                cr.line_to(*point)
            rgb = palette[colour]
            if fill:
                cr.set_source_rgba(*rgb, 0.9)
                cr.stroke_preserve()
                cr.line_to(points[-1][0], base_y)
                cr.line_to(points[0][0], base_y)
                cr.close_path()
                cr.set_source_rgba(*rgb, 0.18)
                cr.fill()
            else:
                cr.set_source_rgb(*rgb)
                cr.stroke()

        for x, (high, low, code) in enumerate(zip(tops, bottoms, codes)):
            if high != high:                       # NaN: nothing here
                finish()
                points, colour = [], None
                continue
            here = [(x, base_y - high * amplitude)]
            if low != high:
                here.append((x + 0.5, base_y - low * amplitude))
            if code != colour:
                joined = points[-1:]
                finish()
                points, colour = joined, code
            points.extend(here)
        finish()

    def _label_runs(self, cr, shape, palette, top, width, a, spp, history, words) -> None:
        """
        Over each amber or red stretch of a trace, why it's that colour, in the trace's own
        words (the same as the status line's): written where the stretch starts, while there's
        room before the next.
        """
        tops, _bottoms, codes = shape
        bad = (codes >= 2) & ~np.isnan(tops)
        if not bad.any():
            return
        edges = np.flatnonzero(np.diff(np.concatenate(([0], bad.astype(np.int8), [0]))))
        runs = []
        for start, stop in zip(edges[::2], edges[1::2]):
            if runs and start - runs[-1][1] < 8:   # a flicker of green doesn't split a stretch
                runs[-1][1] = stop
            else:
                runs.append([start, stop])
        cr.set_font_size(11)
        free_from = 0.0
        for start, stop in runs:
            if start < free_from:
                continue
            first = np.searchsorted(history.t, a + start * spp - spp, "left")
            last = np.searchsorted(history.t, a + stop * spp, "right")
            said = words(history.beats[first:last])
            if not said:
                continue
            text = " · ".join(said[:3])
            colour = palette[int(codes[start:stop].max())]
            # Near the right edge the words move left to fit, without moving the stretch
            x, room = float(start), min(520.0, width - start - 8)
            if room < 60:
                x, room = max(0.0, width - 528.0), 520.0
            advance = min(room, cr.text_extents(text).x_advance)
            cr.set_source_rgba(*BACKGROUND, 0.75)
            cr.rectangle(x + 2, top + 21, advance + 8, 16)
            cr.fill()
            cr.set_source_rgb(*colour)
            _fit_text(cr, text, x + 6, top + 33, room)
            free_from = x + advance + 20

    def _draw_probe(self, cr, x, top, width, height, history, watcher) -> None:
        """The readout for a tapped moment: the checks' numbers and why they were that colour."""
        when = self._probe
        cr.set_source_rgba(1, 1, 1, 0.6)
        cr.set_line_width(1)
        cr.set_dash([4, 3])
        cr.move_to(x + 0.5, top)
        cr.line_to(x + 0.5, top + height)
        cr.stroke()
        cr.set_dash([])
        index = int(np.searchsorted(history.t, when, "right")) - 1
        beat = history.beats[index] if index >= 0 and \
            when - history.t[index] <= watcher.interval * 2.5 else None
        stamp = time.strftime("%H:%M:%S", time.localtime(when))
        third = ""
        if beat is None:
            first = f"{stamp} · no check was made at this moment"
            second = ("the watch was busy rejoining or scanning" if index >= 0 and
                      when < self._live_end(watcher, time.time()) else "")
            colour = RED if second else DIM
        else:
            state = beat["state"]
            colour = STATE_COLOURS.get(state, DIM)
            why = trouble_words([beat])
            verdict = {"up": "green: every check passed", "joining": "grey: joining",
                       "degraded": "amber: " + ", ".join(why),
                       "down": "red: " + ", ".join(why)}.get(state, state)
            first = f"{stamp} · {verdict}"
            parts = []
            for key, label in (("net", "internet"), ("gw", "gateway"), ("dns", "DNS")):
                if beat.get(key) is not None:
                    parts.append(f"{label} {beat[key]:.0f} ms")
                elif key != "dns" and beat.get("assoc"):
                    parts.append(f"{label} no reply")
            if beat.get("beacons") is not None:
                parts.append(f"beacons {beat['beacons']:.0%}")
            if beat.get("signal") is not None:
                parts.append(f"signal {beat['signal']:.0f} dBm")
            near = int(np.searchsorted(history.ct, when, "right")) - 1
            if near >= 0 and when - history.ct[near] < 10 and not math.isnan(history.air[near]):
                parts.append(f"channel {history.air[near] * 100:.0f}% busy")
            second = " · ".join(parts)
            third = " · ".join(load_words(beat))
        box_h = 58 if third else 40 if second else 22
        box_top = top + height - box_h - 4
        cr.set_source_rgba(0.06, 0.07, 0.09, 0.92)
        cr.rectangle(4, box_top, width - 8, box_h)
        cr.fill()
        cr.set_source_rgb(*colour)
        cr.rectangle(4, box_top, 3, box_h)
        cr.fill()
        cr.set_font_size(12)
        cr.set_source_rgb(*colour)
        _fit_text(cr, first, 14, box_top + 15, width - 26)
        if second:
            cr.set_source_rgb(*TEXT)
            _fit_text(cr, second, 14, box_top + 33, width - 26)
        if third:
            cr.set_source_rgb(*VIOLET)
            _fit_text(cr, third, 14, box_top + 51, width - 26)

    def _draw_axis(self, cr, x, top, width, height, a, b) -> None:
        """Clock times under the traces, and faint lines up through them."""
        span = b - a
        step = next((s for s in TICK_STEPS if span / s <= width / 90), TICK_STEPS[-1])
        form = "%H:%M:%S" if step < 60 else "%H:%M"
        offset = -time.localtime(a).tm_gmtoff         # ticks on the hour in local time
        first = math.ceil((a - offset) / step) * step + offset
        cr.set_font_size(10)
        tick = first
        while tick <= b:
            px = x + (tick - a) / span * width
            cr.set_source_rgba(1, 1, 1, 0.07)
            cr.set_line_width(1)
            cr.move_to(int(px) + 0.5, top)
            cr.line_to(int(px) + 0.5, top + height + 4)
            cr.stroke()
            stamp = time.localtime(tick)
            text = time.strftime("%a %H:%M" if span > 12 * 3600 and stamp.tm_hour == 0
                                 and stamp.tm_min == 0 else form, stamp)
            extent = cr.text_extents(text)
            if extent.width / 2 < px < x + width - extent.width / 2:
                cr.set_source_rgb(*DIM)
                cr.move_to(px - extent.width / 2, top + height + 13)
                cr.show_text(text)
            tick += step

    def _lane_frame(self, cr, x, top, width, height, label=None, colour=None) -> None:
        cr.set_source_rgb(*GRID)
        cr.set_line_width(1)
        for gx in range(int(x), int(x + width), 40):
            cr.move_to(gx + 0.5, top)
            cr.line_to(gx + 0.5, top + height)
        for gy in range(int(top), int(top + height), 20):
            cr.move_to(x, gy + 0.5)
            cr.line_to(x + width, gy + 0.5)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.move_to(x, top + height - 0.5)
        cr.line_to(x + width, top + height - 0.5)
        cr.stroke()
        if label:
            self._lane_heading(cr, x, top, label, colour)

    def _lane_heading(self, cr, x, top, label, colour, hint="") -> None:
        """A lane's name, over its trace so a tall one can't hide it."""
        cr.set_font_size(12)
        width = cr.text_extents(label).x_advance
        cr.set_font_size(11)
        if hint:
            width += cr.text_extents("   " + hint).x_advance
        cr.set_source_rgba(*BACKGROUND, 0.75)
        cr.rectangle(x + 4, top + 3, width + 8, 17)
        cr.fill()
        cr.set_source_rgb(*colour)
        cr.set_font_size(12)
        cr.move_to(x + 8, top + 16)
        cr.show_text(label)
        if hint:
            cr.set_source_rgb(*DIM)
            cr.set_font_size(11)
            cr.show_text("   " + hint)

    def _lane_note(self, cr, x, y, text) -> None:
        cr.set_source_rgb(*DIM)
        cr.set_font_size(13)
        cr.move_to(x, y)
        cr.show_text(text)

    def _flat_waves(self, cr, x, top, width, height, words) -> None:
        lane = height / 4
        for number, (label, colour) in enumerate((("CONNECTION", GREEN), ("BEACONS", CYAN),
                                                  ("CHANNEL BUSY", AMBER), ("AP LOAD", VIOLET))):
            self._lane_frame(cr, x, top + number * lane, width, lane, label, colour)
            cr.set_source_rgba(*DIM, 0.8)
            cr.set_line_width(2)
            cr.move_to(x, top + number * lane + lane * 0.7)
            cr.line_to(x + width, top + number * lane + lane * 0.7)
            cr.stroke()
        cr.set_source_rgb(*TEXT)
        cr.set_font_size(15)
        extent = cr.text_extents(words)
        cr.move_to(x + max(8, (width - extent.width) / 2), top + height / 2)
        cr.show_text(words)

    def _draw_banner(self, cr, width, height, watcher, now) -> None:
        """The alarm line: what state it's in, for how long, and the cause so far."""
        if watcher is None or not watcher.running:
            colour, text = (0.10, 0.12, 0.15), ("Not watching" if watcher is None
                                                 else f"Stopped · {watcher.target.ssid}")
        else:
            state = watcher.state
            current = watcher.current
            if state == "down" and current is not None and current.end is None:
                flash = int(now * 2) % 2 == 0
                colour = (0.55, 0.05, 0.05) if flash else (0.35, 0.03, 0.03)
                found = self._provisional.get(id(watcher), {})
                so_far = found.get("title")
                text = (f"{outage.kind_name(current).upper()} "
                        f"{outage.duration(now - current.start)}"
                        + (f" — so far: {so_far}" if so_far and found.get("cause") != "unknown"
                           else ""))
            elif state == "degraded":
                colour, text = (0.35, 0.25, 0.02), watcher.status
            elif state in ("joining", "starting", "asleep"):
                colour, text = (0.12, 0.14, 0.18), watcher.status
            else:
                colour = (0.03, 0.22, 0.10)
                text = (f"{watcher.target.ssid} · {watcher.target.bssid} · channel "
                        f"{watcher.target.channel} · watching for "
                        f"{outage.duration(now - watcher.started)}")
        cr.set_source_rgb(*colour)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.arc(16, height / 2, 5, 0, 2 * math.pi)
        cr.fill()
        cr.set_font_size(14)
        _fit_text(cr, (f"{watcher.label} · " if watcher is not None and watcher.label else "") + text,
                  28, height / 2 + 5, width - 38)

    def _draw_tiles(self, cr, x, top, width, height, watcher, now) -> None:
        tiles = self._tiles(watcher, now)
        columns = 2 if width < 400 else 3
        rows = math.ceil(len(tiles) / columns)
        cell_w, cell_h = width / columns, height / max(1, rows)
        for index, (label, value, unit, sub, colour) in enumerate(tiles):
            cx = x + (index % columns) * cell_w
            cy = top + (index // columns) * cell_h
            cr.set_source_rgba(1, 1, 1, 0.06)
            cr.rectangle(cx + 0.5, cy + 0.5, cell_w - 1, cell_h - 1)
            cr.stroke()
            cr.set_source_rgb(*colour)
            cr.set_font_size(11)
            cr.move_to(cx + 8, cy + 15)
            cr.show_text(label)
            big = min(40.0, cell_h * 0.42, cell_w / max(3, len(value)) * 1.5)
            cr.set_font_size(big)
            cr.move_to(cx + 8, cy + 15 + big)
            cr.show_text(value)
            extent = cr.text_extents(value)
            cr.set_font_size(11)
            cr.move_to(cx + 12 + extent.x_advance, cy + 15 + big)
            cr.show_text(unit)
            if sub and cell_h > 15 + big + 14:
                cr.set_source_rgb(*DIM)
                cr.set_font_size(11)
                _fit_text(cr, sub, cx + 8, cy + 15 + big + 16, cell_w - 14)

    def _tiles(self, watcher, now) -> list[tuple]:
        if watcher is None:
            blank = "--"
            return [("INTERNET", blank, "ms", "", GREEN), ("SIGNAL", blank, "dBm", "", CYAN),
                    ("CHANNEL", blank, "% busy", "", AMBER), ("CLIENTS", blank, "", "", VIOLET),
                    ("LINK", blank, "Mbit/s", "", TEXT), ("AP UPTIME", blank, "", "", TEXT),
                    ("AVAILABLE", blank, "%", "", GREEN), ("INCIDENTS", blank, "", "", RED)]
        beats = watcher.recent_beats(30)
        with watcher.lock:
            channel = watcher.channel[-1] if watcher.channel else None
            incidents = list(watcher.incidents)
        last = beats[-1] if beats else {}
        state_colour = STATE_COLOURS.get(last.get("state"), DIM)
        net = last.get("net")
        recent_loss = [b for b in beats[-15:] if b.get("assoc")]
        lost = sum(1 for b in recent_loss if b.get("net") is None)
        internet = ("INTERNET", f"{net:.0f}" if net is not None else "lost", "ms" if net is not None else "",
                    f"gateway {last['gw']:.0f} ms" if last.get("gw") is not None else
                    ("gateway silent" if last.get("assoc") else "not connected")
                    + (f" · {lost}/{len(recent_loss)} lost" if lost else ""), state_colour)
        signal = last.get("signal")
        share = last.get("beacons")
        signal_tile = ("SIGNAL", f"{signal:.0f}" if signal is not None else "--", "dBm",
                       f"beacons {share:.0%}" if share is not None else "", CYAN)
        # Whatever of the access point's load the last few beats saw
        seen = {}
        for b in beats[-5:]:
            seen.update({k: v for k, v in b.items() if v is not None})
        crowd = []
        if seen.get("ap_busy") is not None:
            crowd.append(f"AP says {seen['ap_busy']:.0%}")
        if seen.get("air_retry") is not None:
            crowd.append(f"{seen['air_retry']:.0%} resent")
        if seen.get("networks"):
            crowd.append(f"{seen['networks']} other network{'s' if seen['networks'] != 1 else ''}")
        if channel is not None and channel.get("airtime") is not None:
            busy = ("CHANNEL", f"{channel['airtime'] * 100:.0f}", "% busy",
                    " · ".join(crowd) or (f"noise {channel['noise']:.0f} dBm"
                                          if channel.get("noise") is not None else ""),
                    ssid_colour(watcher.target.ssid))
        elif seen.get("ap_busy") is not None:
            busy = ("CHANNEL", f"{seen['ap_busy'] * 100:.0f}", "% busy",
                    "the AP's own figure" + "".join(f" · {c}" for c in crowd[1:]),
                    ssid_colour(watcher.target.ssid))
        else:
            busy = ("CHANNEL", "--", "", " · ".join(crowd) or
                    ("no analyzer" if watcher.analyzer is None else "starting"), DIM)
        if seen.get("ap_clients") is not None:
            clients = ("CLIENTS", str(seen["ap_clients"]), "", "the AP's count"
                       + (f" · {seen['heard']} heard" if seen.get("heard") is not None else ""), VIOLET)
        elif seen.get("heard") is not None:
            clients = ("CLIENTS", str(seen["heard"]), "heard", "in 5 min · the AP doesn't say", VIOLET)
        else:
            clients = ("CLIENTS", "--", "", "needs a monitor adapter", DIM)
        if last.get("tx_rate") is not None:
            notes = [f"down {last['rx_rate']:.0f}" if last.get("rx_rate") is not None else "",
                     f"{seen['retries']:.0%} resent" if seen.get("retries") is not None
                     else "adapter doesn't count resends",
                     f"{seen['tx_failed']} failed" if seen.get("tx_failed") else ""]
            link = ("LINK", f"{last['tx_rate']:.0f}", "Mbit/s", " · ".join(n for n in notes if n), TEXT)
        else:
            link = ("LINK", "--", "", "not connected" if not last.get("assoc") else "", DIM)
        uptime = watcher.ap_uptime()
        if uptime is None:
            known = watcher.tsf_known.get(watcher.target.bssid.lower())
            if known:
                uptime = known[1] / 1e6 + (now - known[0])
        up_tile = ("AP UPTIME", _short_duration(uptime) if uptime is not None else "--", "",
                   "from its radio's clock" if uptime is not None else
                   "needs a monitor adapter or a scan", TEXT)
        share_up = outage.availability(watcher.started, now, incidents)
        avail = ("AVAILABLE", f"{share_up * 100:.2f}" if share_up is not None else "--", "%",
                 f"watched {outage.duration(now - watcher.started)}",
                 GREEN if share_up is None or share_up > 0.999 else AMBER if share_up > 0.99 else RED)
        finished = [i for i in incidents if i.end]
        since = (f"last {outage.duration(now - (finished[-1].end or now))} ago" if finished
                 else "none yet")
        count = ("INCIDENTS", str(len(incidents)), "", since, RED if incidents else GREEN)
        return [internet, signal_tile, busy, clients, link, up_tile, avail, count]

    # ---------- drawing: the channel ----------

    def _draw_channel(self, _area, cr: cairo.Context, width: int, height: int) -> None:
        cr.set_source_rgb(*BACKGROUND)
        cr.paint()
        cr.select_font_face("Cantarell")
        shown = self._shown() or [None]
        # Two bands side by side, 2.4 GHz on the left, each its own spectrum and waterfall
        part = width / len(shown)
        for number, watcher in enumerate(shown):
            cr.save()
            cr.translate(number * part, 0)
            cr.rectangle(0, 0, part, height)
            cr.clip()
            self._draw_one_channel(cr, part, height, watcher)
            cr.restore()
            if number:
                cr.set_source_rgba(1, 1, 1, 0.12)
                cr.set_line_width(1)
                cr.move_to(int(number * part) + 0.5, 0)
                cr.line_to(int(number * part) + 0.5, height)
                cr.stroke()

    def _draw_one_channel(self, cr, width: float, height: float, watcher) -> None:
        target = watcher.target if watcher else self._target()
        if target is None:
            self._lane_note(cr, 12, height / 2, "The channel appears here once a network is chosen.")
            return
        low, high = target.span
        colour = ssid_colour(target.ssid)
        cr.set_source_rgb(*TEXT)
        cr.set_font_size(13)
        _fit_text(cr, (f"{watcher.label} · " if watcher is not None and watcher.label else "")
                  + f"Channel {target.channel} · {low:.0f}–{high:.0f} MHz ({target.width} MHz)"
                  + (f" · {watcher.analyzer.model or watcher.analyzer.name}"
                     if watcher and watcher.analyzer and not watcher.label else ""), 10, 18, width - 20)
        if watcher is None or watcher.analyzer is None:
            self._lane_note(cr, 12, height / 2, "No analyzer watching the channel." if watcher
                            else "Press Start watching to sweep just this channel.")
            return
        with watcher.lock:
            traces = list(watcher.traces)
            incidents = list(watcher.incidents)
        if not traces:
            trouble = watcher.notes.get("analyzer")
            self._lane_note(cr, 12, height / 2, f"No sweep yet: {trouble}" if trouble
                            else "Waiting for the first sweep…")
            return
        left, right, top = 46, width - 10, 28
        plot_bottom = top + (height - top) * 0.48
        fall_top, fall_bottom = plot_bottom + 18, height - 6
        latest = traces[-1]
        span = (latest["start"], latest["start"] + latest["step"] * (len(latest["avg"]) - 1))

        def x_at(freq):
            return left + (freq - span[0]) / max(0.001, span[1] - span[0]) * (right - left)

        floor, ceiling = -110.0, -20.0

        def y_at(dbm):
            return plot_bottom - (min(ceiling, max(floor, dbm)) - floor) / (ceiling - floor) * (plot_bottom - top)

        cr.set_source_rgb(*GRID)
        cr.set_line_width(1)
        for dbm in range(-100, -19, 20):
            cr.move_to(left, y_at(dbm) + 0.5)
            cr.line_to(right, y_at(dbm) + 0.5)
        cr.stroke()
        cr.set_source_rgb(*DIM)
        cr.set_font_size(10)
        for dbm in range(-100, -19, 20):
            cr.move_to(4, y_at(dbm) + 4)
            cr.show_text(str(dbm))
        # The channel's own edges
        cr.set_source_rgba(*colour, 0.12)
        cr.rectangle(x_at(low), top, x_at(high) - x_at(low), plot_bottom - top)
        cr.fill()
        freqs = [latest["start"] + i * latest["step"] for i in range(len(latest["avg"]))]
        for values, alpha, line in ((latest["max"], 0.35, 1.0), (latest["avg"], 1.0, 1.8)):
            cr.set_source_rgba(*colour, alpha)
            cr.set_line_width(line)
            for index, (freq, dbm) in enumerate(zip(freqs, values)):
                (cr.move_to if index == 0 else cr.line_to)(x_at(freq), y_at(dbm))
            cr.stroke()
        cr.set_source_rgb(*DIM)
        cr.set_font_size(10)
        for freq in (low, (low + high) / 2, high):
            text = f"{freq:.0f}"
            cr.move_to(x_at(freq) - cr.text_extents(text).width / 2, plot_bottom + 12)
            cr.show_text(text)

        # The waterfall: newest at the top, one row per sweep
        rows = [t for t in traces if len(t["max"]) == len(latest["max"])][::-1]
        levels = np.array([t["max"] for t in rows], dtype=float)
        surface, _pixels = _waterfall_image(levels)
        cr.save()
        cr.rectangle(left, fall_top, right - left, fall_bottom - fall_top)
        cr.clip()
        cr.translate(left, fall_top)
        rows_shown = max(len(rows), 60)
        cr.scale((right - left) / levels.shape[1], (fall_bottom - fall_top) / rows_shown)
        cr.set_source_surface(surface, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_NEAREST)
        cr.paint()
        cr.restore()
        row_h = (fall_bottom - fall_top) / rows_shown
        newest = rows[0]["t"]
        times = [t["t"] for t in rows]
        for incident in incidents:
            ends = incident.end or time.time()
            inside = [i for i, t in enumerate(times) if incident.start - 1 <= t <= ends + 1]
            if inside:
                cr.set_source_rgba(*RED, 0.9)
                cr.rectangle(left - 6, fall_top + min(inside) * row_h, 4,
                             max(2, (max(inside) - min(inside) + 1) * row_h))
                cr.fill()
        cr.set_source_rgb(*DIM)
        cr.set_font_size(10)
        cr.move_to(4, fall_top + 10)
        cr.show_text(time.strftime("%H:%M", time.localtime(newest)))
        cr.move_to(4, fall_bottom)
        cr.show_text(time.strftime("%H:%M", time.localtime(rows[-1]["t"])))

    # ---------- drawing: the session strip ----------

    def _draw_strip(self, _area, cr: cairo.Context, width: int, height: int) -> None:
        cr.set_source_rgb(*BACKGROUND)
        cr.paint()
        cr.select_font_face("Cantarell")
        shown = [w for w in self._shown() if w.started]
        if not shown:
            self._lane_note(cr, 10, height / 2 + 4, "Green while it's fine, amber when it struggles, "
                                                    "red when it's down.")
            return
        lead = shown[0]
        now = time.time()
        # One timeline for every band: from the first start to the latest beat
        start = min(w.started for w in shown)
        end = max(max(self._live_end(w, now) for w in shown), start + 1)
        label_w = 52.0 if len(shown) > 1 else 0.0
        bar_w = width - label_w
        top, row_h = 20, (height - 16 - 20) / len(shown)
        a, b = self._window(lead, now)
        for number, watcher in enumerate(shown):
            row_top = top + number * row_h
            self._draw_strip_row(cr, watcher, label_w, row_top, bar_w, row_h - (4 if len(shown) > 1 else 0),
                                 start, end)
            if label_w:
                cr.set_source_rgb(*DIM)
                cr.set_font_size(11)
                cr.move_to(2, row_top + row_h / 2 + 2)
                cr.show_text(watcher.label)
        # What the traces above are showing
        x0 = label_w + max(0.0, (a - start) / (end - start) * bar_w)
        x1 = label_w + min(bar_w - 1.0, (b - start) / (end - start) * bar_w)
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.set_line_width(1.5)
        cr.rectangle(int(x0) + 0.5, top - 1.5, max(2, int(x1 - x0)), height - 16 - top + 3)
        cr.stroke()
        cr.set_source_rgb(*DIM)
        cr.set_font_size(10)
        cr.move_to(label_w + 2, height - 3)
        cr.show_text(time.strftime("%d %b %H:%M", time.localtime(start)))
        stamp = time.strftime("%d %b %H:%M", time.localtime(end))
        cr.move_to(width - cr.text_extents(stamp).width - 4, height - 3)
        cr.show_text(stamp)

    def _draw_strip_row(self, cr, watcher, x, top, width, height, start, end) -> None:
        """One band's whole session as a coloured bar, its incidents marked above it."""
        history = self._history_of(watcher)
        with watcher.lock:
            incidents = list(watcher.incidents)
        columns = max(1, int(width))
        worst = np.full(columns, -1, dtype=np.int8)
        if len(history.t):
            column = np.clip(((history.t - start) / (end - start) * columns).astype(np.int64),
                             0, columns - 1)
            np.maximum.at(worst, column, history.code)
            # Columns narrower than the time between beats take the beat covering them, so a
            # short session is a solid bar rather than a line per beat
            centre = start + (np.arange(columns) + 0.5) * (end - start) / columns
            index = np.searchsorted(history.t, centre, "right") - 1
            covered = (index >= 0) & (centre - history.t[np.clip(index, 0, None)] <= watcher.interval * 2.5)
            worst = np.where(covered, np.maximum(worst, history.code[np.clip(index, 0, None)]), worst)
        cr.set_antialias(cairo.ANTIALIAS_NONE)
        changes = np.flatnonzero(np.diff(worst)) + 1
        for left, right in zip(np.concatenate(([0], changes)), np.concatenate((changes, [columns]))):
            code = int(worst[left])
            if code < 0:
                continue
            cr.set_source_rgb(*CODE_COLOURS[code])
            cr.rectangle(x + int(left), top, int(right - left), height)
            cr.fill()
        cr.set_antialias(cairo.ANTIALIAS_DEFAULT)
        cr.set_font_size(11)
        for incident in incidents:
            ix = x + min(width - 6, (incident.start - start) / (end - start) * width)
            cr.set_source_rgb(*RED)
            cr.move_to(ix, top - 2)
            cr.line_to(ix - 5, top - 10)
            cr.line_to(ix + 5, top - 10)
            cr.close_path()
            cr.fill()
            label = f"#{incident.id}"
            room = cr.text_extents(label).x_advance
            cr.move_to(ix + 7 if ix + 7 + room < x + width else ix - 7 - room, top - 4)
            cr.show_text(label)


def _choose(dropdown: Gtk.DropDown, label: str) -> None:
    """Selects the entry with this label, if the dropdown has it."""
    model = dropdown.get_model()
    for index in range(model.get_n_items() if model is not None else 0):
        if model.get_string(index) == label:
            dropdown.set_selected(index)
            return


def _fit_text(cr, text: str, x: float, y: float, width: float) -> None:
    """Draws ``text``, cut short with an ellipsis if it's wider than ``width``."""
    if cr.text_extents(text).x_advance > width:
        while text and cr.text_extents(text + "…").x_advance > width:
            text = text[:-1]
        text += "…"
    cr.move_to(x, y)
    cr.show_text(text)


def _short_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours:02d}h"
    return f"{hours}h {minutes:02d}m"
