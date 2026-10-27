"""The survey map: measure by tapping, pinch to zoom, drag to pan."""

from __future__ import annotations

import atexit
import base64
import copy
import io
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cairo
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, GObject, Gtk, Pango

from ..core import (advice, analysis, heatmap, hidden, history, library, planning, watchreport,
                    positioning, rangechoice, rfplan, settings, surveysetup, tonetest)
from ..core.colours import signal_quality, ssid_colour
from ..core.project import Point, Project, Wall
from ..net import bluetooth as bt
from ..net import trackers
from ..net import capture, connectivity, iperf_servers, monitor, throughput, wifi
from ..rf import bands as rfbands
from ..rf import cellular as cell
from ..rf import hackrf as hrf
from ..rf.analyzer import label as analyzer_label
from ..rf.analyzer import ranges_for, share_channels, share_ranges
from ..rf import spectrum as spec
from . import advanced_tests, focus_networks
from .common import bind_device_dropdown, run_in_thread, scrolled, selected_device, selected_text, toolbar
from .spectrum_ring import MIN_RADIUS, RING_SHARE, describe, draw_ring
from .spectrum_view import snapshot_png
from .survey_setup import SurveySetupDialog

RING_DIAMETER_FEET = 10.0     # each spectrum ring, once the plan has a scale
BANDS = [("All bands", "All"), ("2.4 GHz", "2.4"), ("5 GHz", "5"), ("6 GHz", "6")]
AUTO_CHANNEL = "Auto (serving network)"
ALL_BSSIDS = "All access points"
# How big a change counts as a full-strength colour on the comparison map, per metric
CHANGE_SCALE = {"signal": 10, "snr": 10, "noise": 10, "secondary": 10, "aps": 4, "contention": 3,
                "occupancy": 30, "retries": 15, "airtime": 30, "unexplained": 10,
                "requirement": 1, "latency": 30, "loss": 10}


class SurveyView(Gtk.Box):
    def __init__(self, session) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session = session
        self.scale = 1.0
        self.offset = [0.0, 0.0]
        self.plan: cairo.ImageSurface | None = None
        self.overlay: cairo.ImageSurface | None = None
        self.busy = False
        self._needs_fit = True
        self._dragged = False
        self._suppress_tap = False
        self._pending_tap: int | None = None
        self._press = (0.0, 0.0)
        self._drag_last = (0.0, 0.0)
        self._rubber: tuple[tuple[float, float], tuple[float, float]] | None = None
        self._pinching = False
        self._pinched_during_drag = False
        self._pinch: dict | None = None
        self._velocity = [0.0, 0.0]
        self._fling_id: int | None = None
        self._fling_last: int | None = None
        # What Undo steps back through: ("added" | "removed", "point" | "wall", index, item),
        # ("replaced", "point", index, (old, new)) and ("moved", "point", index, (point, x, y))
        self._history: list[tuple[str, str, int, object]] = []
        self.markers: list[tuple[float, float, tuple[float, float, float]]] = []
        self.sites: list[dict] = []           # where the access points probably are
        self.noises: list[dict] = []          # where the noise is probably coming from
        self.bt: list[dict] = []              # located Bluetooth devices
        self.bt_other: dict = {"fixed": [], "rotating": []}   # heard but not placed
        self.clients: list[dict] = []         # Wi-Fi clients from captures attached to points
        self.tags: list[dict] = []            # AirTags and other finder tags heard at points
        self.locator = positioning.Locator()  # where you are, from the access points heard
        self.me: positioning.Fix | None = None
        self._locate_model: positioning.Model | None = None
        self._locating = False
        self._progress: dict[str, str] = {}
        self._refreshing_ssids = False
        self._render_generation = 0
        self._map_message = ""                # what the map last said on the status line
        self._pointer = (0.0, 0.0)
        self.pending: tuple[float, float] | None = None
        self._ruler: list[tuple[float, float]] = []      # the ends of the scale line, on the plan
        self._for_report = False              # drawing the report's map: no numbers or scale bar
        self._rings_off = False               # drawing a report's extra map: plain markers
        # What the next tap on the plan does instead of measuring:
        # ("move_site", site) | ("move_virtual", index) | ("add_virtual", None) | ("move_point", point)
        self._placing: tuple[str, object] | None = None
        self.baseline = None                  # an earlier survey of the same plan, to compare with
        # The timeline: other surveys of this site and this one, oldest first, as
        # (when, project or None for this survey, figures); and the one shown "as it was"
        self._timeline: list[tuple[float, Project | None, dict]] = []
        self.viewing: Project | None = None

        # ----- the side panel: what the map shows and how it measures -----
        self.metric = Adw.ComboRow(title="Map", model=Gtk.StringList.new(
            [analysis.METRICS[key][0] for key in analysis.METRICS]))
        self.metric.connect("notify::selected", lambda *_: self.redraw_heatmap())
        self.ssid = Adw.ComboRow(title="Network", model=Gtk.StringList.new(["Any network"]))
        self.ssid.connect("notify::selected", self._ssid_changed)
        self._ssid_before_advanced = 0
        self.bssid = Adw.ComboRow(title="Access point", subtitle="A single radio of the network",
                                  model=Gtk.StringList.new([ALL_BSSIDS]))
        self.bssid.connect("notify::selected", self._ssid_changed)
        self.requirement = Adw.ComboRow(
            title="Requirement", subtitle="What the pass/fail map and the verdicts judge against",
            model=Gtk.StringList.new([p.name for p in rfplan.PROFILES.values()]))
        self.requirement.connect("notify::selected", self._requirement_changed)
        self.band = Adw.ComboRow(title="Band", model=Gtk.StringList.new([label for label, _ in BANDS]))
        self.band.connect("notify::selected", lambda *_: self.redraw_heatmap())
        self.channel = Adw.ComboRow(
            title="Channel", subtitle="For the occupancy, unexplained-energy and overlap maps",
            model=Gtk.StringList.new([AUTO_CHANNEL]),
        )
        self.channel.connect("notify::selected", self._ssid_changed)
        self.devices = Adw.ComboRow(title="Adapter", use_subtitle=True)
        bind_device_dropdown(self.devices, session)
        self.sweeps = Adw.SpinRow.new_with_range(1, 30, 1)
        self.sweeps.set_title("Spectrum sweeps per band")
        self.sweeps.set_subtitle("At each point, when a spectrum analyzer is connected. More is "
                                 "steadier but slower.")
        self.sweeps.set_value(8)
        self.dwell_count = Adw.SpinRow.new_with_range(0, 8, 1)
        self.dwell_count.set_title("Channels to watch closely")
        self.dwell_count.set_subtitle("After the sweep, the analyzer parks on the busiest channels "
                                      "(the chosen network's first) to measure power while "
                                      "transmitting, airtime and noise. 0 turns it off.")
        self.dwell_count.set_value(3)
        self.dwell_seconds = Adw.SpinRow.new_with_range(1, 15, 1)
        self.dwell_seconds.set_title("Seconds per channel")
        self.dwell_seconds.set_value(4)
        self.check_link = Adw.SwitchRow(
            title="Check the connection", active=True,
            subtitle="When the adapter is connected: latency, jitter and loss to the gateway, "
                     "and DNS time. A few small packets, after the spectrum is measured.")
        self.show_aps = Adw.SwitchRow(title="Where the access points are",
                                      subtitle="Needs each network heard at 4 or more points",
                                      active=True)
        self.show_aps.connect("notify::active", lambda *_: self.redraw_heatmap())
        self.ap_scope = Adw.ComboRow(
            title="Which access points",
            subtitle="Every network heard is located, off the property too once the plan has a "
                     "scale",
            model=Gtk.StringList.new(["Every network heard", "Only the networks to survey"]),
            selected=1 if settings.get("survey_ap_scope") == "focus" else 0)
        self.ap_scope.connect("notify::selected", lambda row, _p: (
            settings.put("survey_ap_scope", "focus" if row.get_selected() == 1 else "all"),
            self.redraw_heatmap()))
        self.show_noise = Adw.SwitchRow(title="Where the noise is coming from",
                                        subtitle="Needs spectrum captured at 3 or more points",
                                        active=True)
        self.show_noise.connect("notify::active", lambda *_: self.redraw_heatmap())
        self.show_rings = Adw.SwitchRow(
            title="Spectrum at each point",
            subtitle="Where the analyzer swept: frequency round the ring, level out from the "
                     "centre, and a dot for each access point. Tap or point at a dot for details.",
            active=True)
        self.show_rings.connect("notify::active", lambda *_: self.area.queue_draw())
        self.show_bt = Adw.SwitchRow(title="Where Bluetooth devices are",
                                     subtitle="Devices found by the Bluetooth scan. Fixed-address "
                                              "ones are placed from 3+ points; the rest show where "
                                              "they were strongest.",
                                     active=True)
        self.show_bt.connect("notify::active", lambda *_: self.redraw_heatmap())
        self.show_clients = Adw.SwitchRow(
            title="Where Wi-Fi clients are",
            subtitle="Phones, laptops and other devices from captures attached to points "
                     "(Capture tab). Placed from 4+ points on the same channel; the rest show "
                     "beside the point where they were strongest.",
            active=True)
        self.show_clients.connect("notify::active", lambda *_: self.redraw_heatmap())
        self.show_tags = Adw.SwitchRow(
            title="Where finder tags are",
            subtitle="AirTags, Find My accessories, Find Hub (Android) tags, SmartTags and Tile "
                     "heard at the points. Red ones are away from their owner.",
            active=True)
        self.show_tags.connect("notify::active", lambda *_: self.redraw_heatmap())
        self.show_me = Adw.SwitchRow(
            title="Where I am",
            subtitle=ME_HELP,
            active=bool(settings.get("survey_locate", False)))
        self.show_me.connect("notify::active", self._show_me_toggled)
        self.focus_row = Adw.ActionRow(title="Networks to survey", subtitle_lines=0)
        choose_focus = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose_focus.connect("clicked", lambda *_: self.choose_focus())
        self.focus_row.add_suffix(choose_focus)
        self.join = Adw.SwitchRow(
            title="Join the network first",
            subtitle="With no networks to survey chosen: connects the adapter to the network "
                     "chosen under Heatmap (using its saved password) so the connection check "
                     "and throughput test can run",
            active=bool(settings.get("survey_join", False)))
        self.join.connect("notify::active",
                          lambda row, _p: settings.put("survey_join", row.get_active()))
        self.speed = Adw.SwitchRow(
            title="Test throughput",
            subtitle="iperf3 to the server below, 4 s each way. Run 'iperf3 -s' on a wired "
                     "computer on the network.",
            active=bool(settings.get("survey_iperf", False)))
        self.speed.connect("notify::active",
                           lambda row, _p: settings.put("survey_iperf", row.get_active()))
        self.speed_server = Adw.EntryRow(title="iperf3 server (host or host:port)",
                                         text=settings.get("iperf_server", "") or "")
        self.speed_server.connect("changed",
                                  lambda row: settings.put("iperf_server", row.get_text().strip()))
        self.advanced_row = Adw.ActionRow(title="Advanced network tests")
        edit = Gtk.Button(label="Edit…", valign=Gtk.Align.CENTER)
        edit.connect("clicked", lambda *_: self.open_advanced())
        self.advanced_row.add_suffix(edit)
        self._describe_advanced()
        self.allowance = Adw.SpinRow.new_with_range(0, 10, 1)
        self.allowance.set_title("Phone allowance (dB)")
        self.allowance.set_subtitle("Judge as a phone hears it: phones typically hear 3–6 dB "
                                    "less than the survey adapter. Raises every signal target.")
        self.allowance.connect("notify::value", self._allowance_changed)
        self.scan_bt = Adw.SwitchRow(
            title="Scan Bluetooth at each point",
            subtitle="Lists every Bluetooth device sharing 2.4 GHz — printers, scanners, phones, "
                     "wearables. Needs root; adds a few seconds a point.",
            active=False)
        self.scan_tags = Adw.SwitchRow(
            title="Find AirTags and finder tags at each point",
            subtitle="AirTags and other Find My tags, Google Find Hub tags, Samsung SmartTags, "
                     "Tile, Chipolo, Pebblebee. Phones and AirPods are left out. Needs root; "
                     "adds about 8 s a point.",
            active=bool(settings.get("survey_tags", False)))
        self.scan_tags.connect("notify::active",
                               lambda row, _p: settings.put("survey_tags", row.get_active()))
        self.iq_identify = Adw.SwitchRow(
            title="Name what's on the busy channels (HackRF)",
            subtitle="When a HackRF is connected: listens to the busiest channels and any "
                     "unexplained energy, and names what's transmitting (Wi-Fi, Bluetooth, "
                     "Zigbee, microwave ovens, carriers, radar). About 2 s a channel.",
            active=bool(settings.get("survey_iq", True)))
        self.iq_identify.connect("notify::active",
                                 lambda row, _p: settings.put("survey_iq", row.get_active()))
        self.measure_cellular = Adw.SwitchRow(
            title="Measure cellular bands (HackRF)",
            subtitle="Downlink power in each US cellular band, plus 900 MHz ISM and DECT, for "
                     "the cellular map and the report. About 3 s a point.",
            active=bool(settings.get("survey_cellular", False)))
        self.measure_cellular.connect("notify::active",
                                      lambda row, _p: settings.put("survey_cellular", row.get_active()))
        self.hidden_row = Adw.SwitchRow(
            title="Recover hidden network names",
            subtitle="At points that hear a hidden network still unnamed, listens on its "
                     f"channel in monitor mode (up to {surveysetup.HIDDEN_SECONDS} s a channel) "
                     "for the name it says when a device joins it. Only listens. Needs root and "
                     "a second adapter that does monitor mode (Capture → monitor adapter).")
        self.cellular_band = Adw.ComboRow(
            title="Cellular band", visible=False,
            model=Gtk.StringList.new(["Strongest cellular band"] + [name for _k, name, _l, _h in cell.BANDS]))
        self.cellular_band.connect("notify::selected", lambda *_: self.redraw_heatmap())
        # What each point measures, chosen for the kind of survey (the setup dialog). The rows
        # below it still change one thing at a time; the summary follows them.
        self.setup_row = Adw.ActionRow(title="Survey setup", use_markup=False)
        self.setup_row.set_subtitle_lines(4)
        setup_button = Gtk.Button(label="Edit…", valign=Gtk.Align.CENTER,
                                  css_classes=["suggested-action"])
        setup_button.connect("clicked", lambda *_: self.open_setup())
        self.setup_row.add_suffix(setup_button)
        self.other_band = Adw.ComboRow(
            title="Band", visible=False,
            model=Gtk.StringList.new(["Busiest band measured"] + [b.name for b in rfbands.BANDS]))
        self.other_band.connect("notify::selected", lambda *_: self.redraw_heatmap())
        self._ap_hits: list[tuple[float, float, dict]] = []
        self.wall_db = Adw.SpinRow.new_with_range(0, 40, 1)
        self.wall_db.set_title("Wall loss (dB)")
        self.wall_db.set_value(3)

        shown = Adw.PreferencesGroup(title="Heatmap")
        for row in (self.metric, self.cellular_band, self.other_band, self.requirement, self.allowance, self.ssid,
                    self.bssid, self.band, self.channel):
            shown.add(row)
        self.quality_group = Adw.PreferencesGroup(title="Signal quality")
        self.quality_row = Adw.ActionRow(title="Choose a network above", subtitle="")
        self.quality_group.add(self.quality_row)
        layers = Adw.PreferencesGroup(title="On the map")
        layers.add(self.show_aps)
        layers.add(self.ap_scope)
        layers.add(self.show_noise)
        layers.add(self.show_bt)
        layers.add(self.show_clients)
        layers.add(self.show_tags)
        layers.add(self.show_me)
        layers.add(self.show_rings)
        measuring = Adw.PreferencesGroup(title="Measuring")
        measuring.add(self.setup_row)
        measuring.add(self.devices)
        measuring.add(self.sweeps)
        measuring.add(self.dwell_count)
        measuring.add(self.dwell_seconds)
        measuring.add(self.focus_row)
        measuring.add(self.check_link)
        measuring.add(self.join)
        measuring.add(self.speed)
        measuring.add(self.speed_server)
        measuring.add(self.advanced_row)
        measuring.add(self.hidden_row)
        measuring.add(self.scan_bt)
        measuring.add(self.scan_tags)
        measuring.add(self.iq_identify)
        measuring.add(self.measure_cellular)
        review = Adw.PreferencesGroup(
            title="Review", description="From everything measured so far. The channel plan is "
                                        "for the network chosen under Heatmap.")
        audit_row = Adw.ActionRow(title="Configuration audit",
                                  subtitle="Basic rates, beacon overhead, roaming, consistency")
        audit_button = Gtk.Button(label="Show", valign=Gtk.Align.CENTER)
        audit_button.connect("clicked", lambda *_: self.show_audit())
        audit_row.add_suffix(audit_button)
        plan_row = Adw.ActionRow(title="Channel plan",
                                 subtitle="Keeps the network's radios off each other's channels")
        plan_button = Gtk.Button(label="Suggest", valign=Gtk.Align.CENTER)
        plan_button.connect("clicked", lambda *_: self.show_plan())
        plan_row.add_suffix(plan_button)
        self.allow_dfs = Adw.SwitchRow(title="Include DFS channels",
                                       subtitle="5 GHz channels 52–144: more room, but radar can "
                                                "force a sudden channel change", active=False)
        self.radar_row = Adw.SpinRow.new_with_range(2, 60, 1)
        self.radar_row.set_title("Radar watch (HackRF), minutes")
        self.radar_row.set_value(8)
        self._radar_stop: threading.Event | None = None
        self.radar_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.radar_button.connect("clicked", lambda *_: self.toggle_radar())
        self.radar_row.add_suffix(self.radar_button)
        for row in (audit_row, plan_row, self.allow_dfs, self.radar_row):
            review.add(row)
        planning_group = Adw.PreferencesGroup(
            title="Planning",
            description="Try access points that don't exist yet. Choose 'Predicted signal with "
                        "planned access points' under Map to see the result. Tap an access "
                        "point's icon to name it or move it to where it really is.")
        add_ap = Adw.ActionRow(title="Planned access point", subtitle="Then tap where it would go")
        add_button = Gtk.Button(label="Add", valign=Gtk.Align.CENTER)
        add_button.connect("clicked", lambda *_: self._start_placing("add_virtual", None))
        add_ap.add_suffix(add_button)
        planning_group.add(add_ap)
        self.tone_row = Adw.ActionRow(title="Test tone (HackRF)")
        self.tone_choice = Gtk.DropDown.new_from_strings([f"{freq:.0f} MHz" for freq, _t in hrf.TONES.values()])
        self.tone_choice.set_valign(Gtk.Align.CENTER)
        self.tone_row.add_suffix(self.tone_choice)
        place_tone = Gtk.Button(label="Place", valign=Gtk.Align.CENTER)
        place_tone.connect("clicked", lambda *_: self._start_placing("place_tone", None))
        self.tone_row.add_suffix(place_tone)
        tone_help = Gtk.Button(label="How?", valign=Gtk.Align.CENTER,
                               tooltip_text="How to set up the tone with what's connected")
        tone_help.connect("clicked", lambda *_: self.show_tone_setup())
        self.tone_row.add_suffix(tone_help)
        self.tone_remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                      tooltip_text="Remove the test tone source")
        self.tone_remove.connect("clicked", lambda *_: self._remove_tone())
        self.tone_row.add_suffix(self.tone_remove)
        planning_group.add(self.tone_row)
        fit_row = Adw.ActionRow(title="Wall losses from the tone",
                                subtitle="Works out each wall's loss from how the tone fades, "
                                         "once it's been measured at 5 or more points")
        fit_button = Gtk.Button(label="Fit", valign=Gtk.Align.CENTER)
        fit_button.connect("clicked", lambda *_: self.show_wall_fit())
        fit_row.add_suffix(fit_button)
        planning_group.add(fit_row)
        walls = Adw.PreferencesGroup(title="Walls",
                                     description="Turn on Draw wall, then drag across the plan.")
        walls.add(self.wall_db)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                        margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        for group in (shown, self.quality_group, layers, measuring, review, planning_group, walls):
            panel.append(group)
        sidebar = scrolled(panel)
        sidebar.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # ----- the toolbar: only what you reach for while walking -----
        plan_button = Gtk.Button(child=Adw.ButtonContent(icon_name="image-x-generic-symbolic",
                                                         label="Floor plan…"))
        plan_button.connect("clicked", self.choose_plan)
        map_button = Gtk.Button(child=Adw.ButtonContent(icon_name="mark-location-symbolic",
                                                        label="Site map…"))
        map_button.set_tooltip_text("Find the site on a map (GPS, nearby Wi-Fi or address), lay "
                                    "the blueprint over its building, and take the scale from "
                                    "the map")
        map_button.connect("clicked", lambda *_: self.open_site_map())
        measure = Gtk.Button(label="Measure at centre", css_classes=["suggested-action"])
        measure.set_tooltip_text("Measures without a position on the plan. Tapping the plan "
                                 "measures where you tap.")
        measure.connect("clicked", lambda *_: self.measure(self.session.project.width / 2,
                                                           self.session.project.height / 2))
        self.wall_mode = Gtk.ToggleButton(child=Adw.ButtonContent(
            icon_name="document-edit-symbolic", label="Draw wall"))
        self.wall_mode.set_tooltip_text("Drag across the plan to mark a wall")
        self.wall_mode.connect("toggled", self._wall_toggled)
        self.scale_mode = Gtk.ToggleButton(child=Adw.ButtonContent(
            icon_name="view-continuous-symbolic", label="Set scale"))
        self.scale_mode.set_tooltip_text("Draw a line between two spots a known distance apart "
                                         "(or tap each end), then enter the distance in feet")
        self.scale_mode.connect("toggled", self._scale_toggled)
        undo = Gtk.Button(child=Adw.ButtonContent(icon_name="edit-undo-symbolic", label="Undo"))
        undo.connect("clicked", self.undo)
        redraw = Gtk.Button(child=Adw.ButtonContent(icon_name="view-refresh-symbolic", label="Redraw"),
                            tooltip_text="Work the map and the access points out again")
        redraw.connect("clicked", lambda *_: self.redraw_heatmap())

        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text="Zoom out")
        zoom_out.connect("clicked", lambda *_: self.set_scale(self.scale / 1.5))
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text="Zoom in")
        zoom_in.connect("clicked", lambda *_: self.set_scale(self.scale * 1.5))
        fit = Gtk.Button(icon_name="zoom-fit-best-symbolic", tooltip_text="Fit to window")
        fit.connect("clicked", lambda *_: self.fit())
        zooming = Gtk.Box(css_classes=["linked"])
        for button in (zoom_out, fit, zoom_in):
            zooming.append(button)

        help_button = Gtk.MenuButton(icon_name="dialog-information-symbolic",
                                     tooltip_text="Touch gestures", popover=_gesture_help())
        self.panel_toggle = Gtk.ToggleButton(icon_name="sidebar-show-right-symbolic",
                                             tooltip_text="Map settings", active=True)

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self.draw)
        self.area.connect("resize", self._maybe_fit)
        self.area.set_has_tooltip(True)
        self.area.connect("query-tooltip", self._tooltip)
        self.status = Gtk.Label(label="Tap the plan to measure. Drag to pan, pinch to zoom.",
                                xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                css_classes=["osd", "map-status"],
                                halign=Gtk.Align.START, valign=Gtk.Align.END,
                                margin_start=12, margin_end=12, margin_bottom=12)
        stage = Gtk.Overlay(child=self.area)
        stage.add_overlay(self.status)
        stage.add_overlay(self._scale_bar())
        stage.add_overlay(self._timeline_bar())

        self.banner = Adw.Banner(button_label="Stop comparing", revealed=False)
        self.banner.connect("button-clicked", lambda *_: self.stop_comparing())
        # Easy mode's guide through the wall test: the next thing to do, and a button for it
        self.guide = Adw.Banner(revealed=False, use_markup=False)
        self.guide.connect("button-clicked", lambda *_: self._guide_action())
        self._guide_step: tonetest.Step | None = None
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.append(self.guide)
        main.append(self.banner)
        self.timeline_button = Gtk.ToggleButton(child=Adw.ButtonContent(
            icon_name="document-open-recent-symbolic", label="Timeline"))
        self.timeline_button.set_tooltip_text("Earlier surveys of this site: see what changed, "
                                              "or the map as it was")
        self.timeline_button.connect("toggled", self._timeline_toggled)
        main.append(toolbar(plan_button, map_button, measure, self.wall_mode, self.scale_mode, undo,
                            redraw, self.timeline_button, zooming, help_button, self.panel_toggle))
        main.append(stage)

        self.split = Adw.OverlaySplitView(content=main, sidebar=sidebar,
                                          sidebar_position=Gtk.PackType.END,
                                          min_sidebar_width=260, max_sidebar_width=320,
                                          vexpand=True)
        self.split.bind_property("show-sidebar", self.panel_toggle, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL
                                 | GObject.BindingFlags.SYNC_CREATE)
        self.append(self.split)

        self.menu = Gtk.Popover(has_arrow=True)
        self.menu.set_parent(self.area)

        self.connect("map", lambda *_: self.devices.refill())
        self._add_gestures()
        session.on("project", self.reload)
        session.on("project", self._setup_from_project)
        session.on("explorer", self._describe_setup)
        session.on("spectrum-choice", self._choice_from_elsewhere)
        session.on("changed", self.redraw_heatmap)     # any edit, here or in another view
        session.on("changed", self._forget_locate_model)
        session.on("project", self._new_survey_for_locator)
        session.on("project", self._describe_focus)
        session.on("add-watch", self.add_watch)
        session.on("project", self._pending_watch)
        self._describe_focus()
        for event in ("project", "changed"):
            session.on(event, self._describe_radar)
            session.on(event, self._describe_tone)
        for event in ("project", "changed", "explorer", "options"):
            session.on(event, self._update_guide)
        self._describe_radar()
        self._describe_tone()
        self._update_guide()
        self.reload()
        if self.show_me.get_active():
            GLib.timeout_add(LOCATE_EVERY_MS, self._locate_tick)

    # ---------- gestures ----------

    def _add_gestures(self) -> None:
        """
        One finger: tap to measure, drag to pan (flick to coast), hold for a point's options.
        Two fingers: pinch to zoom while dragging to pan, tap to fit the whole plan.
        Double-tap zooms in about the tap. A mouse gets the same through clicks and the wheel.
        """
        area = self.area

        tap = Gtk.GestureClick()
        tap.connect("pressed", self._tap_pressed)
        tap.connect("released", self._tap_released)
        area.add_controller(tap)

        secondary = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        secondary.connect("pressed", lambda _g, _n, x, y: self._context(x, y))
        area.add_controller(secondary)

        hold = Gtk.GestureLongPress()
        hold.connect("pressed", self._held)
        area.add_controller(hold)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        area.add_controller(drag)

        fling = Gtk.GestureSwipe(touch_only=True)
        fling.connect("swipe", self._flung)
        area.add_controller(fling)

        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._pinch_begin)
        zoom.connect("scale-changed", self._pinched)
        zoom.connect("end", self._pinch_end)
        zoom.connect("cancel", self._pinch_end)
        area.add_controller(zoom)

        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        wheel.connect("scroll", self.on_scroll)
        area.add_controller(wheel)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: setattr(self, "_pointer", (x, y)))
        area.add_controller(motion)

    # ----- taps -----

    def _tap_pressed(self, _gesture, n_press: int, _x, _y) -> None:
        self._stop_fling()
        if n_press == 1:
            self._dragged = False
            self._suppress_tap = False

    def _tap_released(self, _gesture, n_press: int, x: float, y: float) -> None:
        if self._dragged or self._suppress_tap or self.wall_mode.get_active():
            return
        if self.viewing is not None:
            self.status.set_text("This is an earlier survey, as it was. Move the timeline to "
                                 "this survey to measure.")
            return
        if self.scale_mode.get_active():
            self._ruler_tap(x, y)
            return
        if self._placing is not None:
            if n_press == 1:
                self._place(*self._to_plan(x, y))
            return
        delay = Gtk.Settings.get_default().get_property("gtk-double-click-time")
        target = self._tap_target(x, y) if n_press == 1 else None
        if target == "aps":
            self._cancel_tap()
            self._show_aps(x, y)          # a tap on a ring's access point, not a measurement
            return
        if n_press >= 2:
            self._cancel_tap()
            self.set_scale(self.scale * 2, (x, y))
            return
        self._cancel_tap()
        if target is not None:
            # A tap on a measured point or a map icon shows its details, after the double-tap
            # window so a double-tap there still zooms
            self._pending_tap = GLib.timeout_add(delay, self._details_confirmed, x, y, target)
            return
        if self.busy:
            return
        # Wait out the double-tap window, so zooming in never measures by accident
        plan_x, plan_y = self._to_plan(x, y)
        self._pending_tap = GLib.timeout_add(delay, self._tap_confirmed, plan_x, plan_y)

    def _tap_target(self, x: float, y: float):
        """
        What a single tap at (x, y) is on: "aps" for a ring's access point dots, a (kind, index)
        hit for a measured point or map icon, or None for open floor, which measures. A tap on
        the point's own centre beats a weak access point's dot drawn over it.
        """
        project = self.session.project
        for index, point in enumerate(project.points):
            sx, sy = self._to_screen(point.x, point.y)
            if math.hypot(sx - x, sy - y) <= 12:
                return ("point", index)
        if self._aps_at(x, y):
            return "aps"
        hit = self._hit(x, y)
        return hit if hit and hit[0] != "wall" else None

    def _details_confirmed(self, x: float, y: float, target) -> bool:
        self._pending_tap = None
        self._context(x, y, target)
        return False

    def _tap_confirmed(self, plan_x: float, plan_y: float) -> bool:
        self._pending_tap = None
        project = self.session.project
        if not self.busy and 0 <= plan_x <= project.width and 0 <= plan_y <= project.height:
            if _asks_what_taps_are():
                self._ask_tap(plan_x, plan_y)
                return False
            self._scan_here(plan_x, plan_y)
        return False

    def _scan_here(self, plan_x: float, plan_y: float) -> None:
        if self.show_me.get_active():
            self._calibrate(plan_x, plan_y, quiet=True)   # you tapped where you're standing
        self.measure(plan_x, plan_y)

    def _ask_tap(self, plan_x: float, plan_y: float) -> None:
        """Setup → Ask what each tap is: a scanning point, or where the tone generator sits."""
        moving = self.session.project.tone_source is not None
        dialog = Adw.AlertDialog(
            heading="What's here?",
            body="Is this a point where you're scanning, or where the tone generator sits?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("tone", "Move tone generator here" if moving else "Tone generator")
        dialog.add_response("scan", "Scanning point")
        dialog.set_response_appearance("scan", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("scan")
        dialog.set_close_response("cancel")

        def answered(_dialog, response: str) -> None:
            if response == "scan":
                self._scan_here(plan_x, plan_y)
            elif response == "tone":
                self._set_tone_source(plan_x, plan_y)
                self.show_tone_setup()

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    def _set_tone_source(self, plan_x: float, plan_y: float) -> None:
        project = self.session.project
        before = project.tone_source
        # Moving it keeps its frequency: points already measured heard that one
        freq = before["freq_mhz"] if before else list(hrf.TONES.values())[self.tone_choice.get_selected()][0]
        project.tone_source = {"x": plan_x, "y": plan_y, "freq_mhz": freq}
        self._describe_tone()
        self.session.changed()
        self.status.set_text(
            "Tone generator moved. Points measured before heard it from the old spot, so measure "
            "them again." if before and project.points else
            f"Tone generator placed, at {freq:.0f} MHz. Set it up as shown, then tap where you're "
            "standing to measure.")

    def _cancel_tap(self) -> None:
        if self._pending_tap is not None:
            GLib.source_remove(self._pending_tap)
            self._pending_tap = None

    def _held(self, _gesture, x: float, y: float) -> None:
        self._suppress_tap = True
        self._cancel_tap()
        self._context(x, y)

    # ----- one-finger drag: pan, or draw a wall -----

    def _drag_begin(self, _gesture, x: float, y: float) -> None:
        self._press = (x, y)
        self._drag_last = (0.0, 0.0)
        self._rubber = None
        self._pinched_during_drag = self._pinching

    def _drag_update(self, _gesture, dx: float, dy: float) -> None:
        if abs(dx) + abs(dy) > 8:
            self._dragged = True
        # Move by the change since last time, so a pinch in between doesn't make it jump
        step = (dx - self._drag_last[0], dy - self._drag_last[1])
        self._drag_last = (dx, dy)
        if self._pinching:
            self._pinched_during_drag = True
            self._rubber = None
        elif self.scale_mode.get_active() and not self._pinched_during_drag:
            # The scale line is drawn like a wall: from where the finger went down to where it is
            self._ruler = [self._to_plan(*self._press),
                           self._to_plan(self._press[0] + dx, self._press[1] + dy)]
            self.scale_bar.set_visible(False)
        elif self.wall_mode.get_active() and not self._pinched_during_drag:
            self._rubber = (
                self._to_plan(*self._press),
                self._to_plan(self._press[0] + dx, self._press[1] + dy),
            )
        else:
            self.offset = [self.offset[0] + step[0], self.offset[1] + step[1]]
        self.area.queue_draw()

    def _drag_end(self, _gesture, dx: float, dy: float) -> None:
        if self.scale_mode.get_active() and not self._pinched_during_drag and self._dragged:
            if math.hypot(dx, dy) >= 20 and len(self._ruler) == 2:
                (x1, y1), (x2, y2) = self._ruler
                self._ask_distance(math.hypot(x2 - x1, y2 - y1))
            else:
                self._ruler = []
                self.status.set_text("Too short: draw a line between two spots a known "
                                     "distance apart.")
        if self.wall_mode.get_active() and not self._pinched_during_drag and self._rubber:
            start = self._to_plan(*self._press)
            finish = self._to_plan(self._press[0] + dx, self._press[1] + dy)
            if math.hypot(finish[0] - start[0], finish[1] - start[1]) >= 8:
                walls = self.session.project.walls
                walls.append(Wall(start[0], start[1], finish[0], finish[1],
                                  db=float(self.wall_db.get_value())))
                self._history.append(("added", "wall", len(walls) - 1, walls[-1]))
                self.status.set_text(f"{len(walls)} walls marked")
                self.session.changed()
        self._rubber = None
        self.area.queue_draw()

    # ----- flick to coast -----

    def _flung(self, _gesture, vx: float, vy: float) -> None:
        if (not self._dragged or self._pinched_during_drag or self._pinching
                or self.wall_mode.get_active() or self.scale_mode.get_active()
                or math.hypot(vx, vy) < 150):
            return
        self._velocity = [vx, vy]
        self._fling_last = None
        if self._fling_id is None:
            self._fling_id = self.area.add_tick_callback(self._coast)

    def _coast(self, _widget, clock) -> bool:
        now = clock.get_frame_time()
        if self._fling_last is None:
            self._fling_last = now
            return True
        seconds = (now - self._fling_last) / 1e6
        self._fling_last = now
        self.offset = [self.offset[0] + self._velocity[0] * seconds,
                       self.offset[1] + self._velocity[1] * seconds]
        decay = math.exp(-4.0 * seconds)
        self._velocity = [self._velocity[0] * decay, self._velocity[1] * decay]
        self.area.queue_draw()
        if math.hypot(*self._velocity) < 20:
            self._fling_id = None
            return False
        return True

    def _stop_fling(self) -> None:
        if self._fling_id is not None:
            self.area.remove_tick_callback(self._fling_id)
            self._fling_id = None

    # ----- two fingers: pinch and pan together, or tap to fit -----

    def _pinch_begin(self, gesture, _sequence) -> None:
        self._pinching = True
        self._dragged = True
        self._suppress_tap = True
        self._cancel_tap()
        self._stop_fling()
        self._rubber = None
        found, x, y = gesture.get_bounding_box_center()
        if not found:
            x, y = self.area.get_width() / 2, self.area.get_height() / 2
        self._pinch = {"scale": self.scale, "anchor": self._to_plan(x, y), "centre": (x, y),
                       "started": GLib.get_monotonic_time(), "moved": False}

    def _pinched(self, gesture, factor: float) -> None:
        pinch = self._pinch
        found, x, y = gesture.get_bounding_box_center()
        if pinch is None or not found:
            return
        # Keep the spot that started under the fingers under them, wherever they go
        self.scale = _clamp_scale(pinch["scale"] * factor)
        self.offset = [x - pinch["anchor"][0] * self.scale, y - pinch["anchor"][1] * self.scale]
        if abs(factor - 1) > 0.05 or math.hypot(x - pinch["centre"][0], y - pinch["centre"][1]) > 12:
            pinch["moved"] = True
        self.area.queue_draw()

    def _pinch_end(self, _gesture, _sequence) -> None:
        pinch, self._pinch = self._pinch, None
        self._pinching = False
        if pinch and not pinch["moved"] and GLib.get_monotonic_time() - pinch["started"] < 300_000:
            self.fit()

    # ----- mouse and touchpad -----

    def on_scroll(self, controller, dx, dy) -> bool:
        if controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            self.set_scale(self.scale * (0.9 if dy > 0 else 1.1), self._pointer)
            return True
        step = 40 if controller.get_unit() == Gdk.ScrollUnit.WHEEL else 1
        self.offset = [self.offset[0] - dx * step, self.offset[1] - dy * step]
        self.area.queue_draw()
        return True

    def set_scale(self, value: float, anchor: tuple[float, float] | None = None) -> None:
        """Zooms about ``anchor`` (widget coordinates), so what's under it stays put."""
        new = _clamp_scale(value)
        if anchor is None:
            anchor = (self.area.get_width() / 2, self.area.get_height() / 2)
        plan_x, plan_y = self._to_plan(*anchor)
        self.offset = [anchor[0] - plan_x * new, anchor[1] - plan_y * new]
        self.scale = new
        self.area.queue_draw()

    def _to_plan(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.offset[0]) / self.scale, (y - self.offset[1]) / self.scale

    def _to_screen(self, x: float, y: float) -> tuple[float, float]:
        return x * self.scale + self.offset[0], y * self.scale + self.offset[1]

    # ----- the access point dots on each point's spectrum ring -----

    def _aps_at(self, x: float, y: float, reach: float = 12) -> list[dict]:
        """The access points whose dots are under (x, y), nearest first."""
        near = sorted((math.hypot(hx - x, hy - y), index)
                      for index, (hx, hy, _ap) in enumerate(self._ap_hits)
                      if math.hypot(hx - x, hy - y) <= reach)
        return [self._ap_hits[index][2] for _distance, index in near]

    def _tooltip(self, _area, x: int, y: int, _keyboard: bool, tooltip: Gtk.Tooltip) -> bool:
        aps = self._aps_at(x, y, reach=7)
        if not aps:
            return False
        tooltip.set_text("\n\n".join(f"{hidden.label(ap['ssid'], ap.get('hidden'))}\n"
                                     f"{describe(ap, BAND_NAMES)}"
                                     for ap in aps[:4]))
        return True

    def _show_aps(self, x: float, y: float) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["map-menu"])
        for ap in self._aps_at(x, y)[:4]:
            box.append(Gtk.Label(label=hidden.label(ap["ssid"], ap.get("hidden")), xalign=0,
                                 css_classes=["heading"]))
            box.append(Gtk.Label(label=describe(ap, BAND_NAMES), xalign=0, wrap=True,
                                 max_width_chars=40, css_classes=["dim-label"]))
        self._popup(box, x, y)

    # ----- press and hold: what's here, and delete it -----

    def _hit(self, x: float, y: float) -> tuple[str, int] | None:
        """The point or map icon, or failing those the wall, under a fingertip at (x, y)."""
        project = self.session.project
        best = None
        for index, point in enumerate(project.points):
            sx, sy = self._to_screen(point.x, point.y)
            distance = math.hypot(sx - x, sy - y)
            if distance <= 28 and (best is None or distance < best[0]):
                best = (distance, "point", index)
        for sx, sy, kind, index in getattr(self, "_icon_hits", []):
            distance = math.hypot(sx - x, sy - y)
            if distance <= 22 and (best is None or distance < best[0]):
                best = (distance, kind, index)
        if best is None:
            for index, wall in enumerate(project.walls):
                distance = _segment_distance((x, y), self._to_screen(wall.x1, wall.y1),
                                             self._to_screen(wall.x2, wall.y2))
                if distance <= 20 and (best is None or distance < best[0]):
                    best = (distance, "wall", index)
        return (best[1], best[2]) if best else None

    def _context(self, x: float, y: float, hit: tuple[str, int] | None = None) -> None:
        hit = hit or self._hit(x, y)
        if hit is None:
            if self.show_me.get_active():
                self._here_menu(x, y)
            return
        kind, index = hit
        project = self.session.project
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["map-menu"])
        if kind == "virtual":
            self._virtual_menu(box, index)
            self._popup(box, x, y)
            return
        if kind == "watch":
            watch = project.watches[index]
            box.append(Gtk.Label(label=f"Watched: {watch['ssid'] or '(hidden)'}", xalign=0,
                                 css_classes=["heading"]))
            box.append(Gtk.Label(label=_watch_details(watch), xalign=0, wrap=True,
                                 max_width_chars=40, css_classes=["dim-label"]))
            self._watch_menu(box, index)
            self._popup(box, x, y)
            return
        if kind in ("site", "noise", "bt", "bt_near", "client", "tag"):
            box.append(Gtk.Label(label=_icon_title(self, kind, index), xalign=0, css_classes=["heading"]))
            box.append(Gtk.Label(label=_icon_details(self, kind, index), xalign=0, wrap=True,
                                 max_width_chars=40, css_classes=["dim-label"]))
            if kind == "site":
                self._site_menu(box, self.sites[index])
            self._popup(box, x, y)
            return
        if kind == "point":
            point = project.points[index]
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            strongest = max((n["rssi"] for n in point.networks), default=None)
            details = [f"{len(analysis.access_points(point.networks))} access points"]
            if strongest is not None:
                details.append(f"strongest {strongest} dBm")
            if point.spectrum:
                details.append("spectrum captured")
            ssid = selected_text(self.ssid) if self.ssid.get_selected() > 0 else None
            band = BANDS[min(self.band.get_selected(), len(BANDS) - 1)][1]
            context = analysis.Context(project, profile=self._profile_key())
            ok, why = analysis.requirement(point, context, ssid, band)
            others = rfplan.contenders(point, ssid, band)
            second = rfplan.nth_strongest(point, 2, ssid, band)
            if point.capture:
                details.append("capture attached")
            box.append(Gtk.Label(label=f"Point {index + 1}", xalign=0, css_classes=["heading"]))
            info.append(Gtk.Label(label=", ".join(details), xalign=0, wrap=True,
                                  max_width_chars=32, css_classes=["dim-label"]))
            if ok is not None:
                verdict = (f"Meets {context.profile.name}" if ok
                           else f"Fails {context.profile.name}: " + "; ".join(why))
                info.append(Gtk.Label(label=verdict, xalign=0, wrap=True, max_width_chars=40,
                                      css_classes=["success" if ok else "error"]))
            extra = []
            if second is not None:
                extra.append(f"second access point {second} dBm")
            if others is not None:
                extra.append(f"{len(others)} other radios on its channel"
                             + (f" ({', '.join(sorted({s for r in others for s in r.ssids}) or ['hidden'])})"
                                if others else ""))
            if extra:
                info.append(Gtk.Label(label=(lambda text: text[:1].upper() + text[1:])(", ".join(extra)), xalign=0, wrap=True,
                                      max_width_chars=40, css_classes=["dim-label"]))
            for result in point.checks or ([point.connectivity] if point.connectivity else []):
                text = (f"{result['ssid']}: not joined, {result['skipped']}" if result.get("skipped")
                        else (f"{result['ssid']}: " if point.checks and result.get("ssid") else "")
                        + connectivity.describe(result))
                info.append(Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=40,
                                      css_classes=["error" if connectivity.problem(result)
                                                   else "dim-label"]))
            if point.throughput:
                info.append(Gtk.Label(label=throughput.describe(point.throughput), xalign=0,
                                      wrap=True, max_width_chars=40, css_classes=["dim-label"]))
            info.append(_ap_list(point, context.profile.primary))
            todo = [item for item in advice.improvements(point, context, ssid, band, self.sites)
                    if item.severity != "Low"][:4]
            if todo:
                info.append(Gtk.Label(label="To improve here", xalign=0,
                                      css_classes=["heading"], margin_top=6))
                for item in todo:
                    info.append(Gtk.Label(
                        label=f"{item.title}: {item.issue}\n→ {item.fix}", xalign=0, wrap=True,
                        max_width_chars=44,
                        css_classes=["error" if item.severity == "High" else "warning"]))
            box.append(Gtk.ScrolledWindow(child=info, propagate_natural_height=True,
                                          propagate_natural_width=True, max_content_height=380,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER))
            def noted(text: str) -> None:
                point.note = text

            box.append(self._text_button(
                f"Note: {point.note}" if point.note else "Add a note…", "Note for this point",
                point.note, "Note, e.g. behind the freezer", noted))
            move = Gtk.Button(child=Adw.ButtonContent(icon_name="find-location-symbolic",
                                                      label="Move this point"))
            move.set_tooltip_text("Keeps its readings and puts it where you really measured. "
                                  "Undo puts it back.")
            move.connect("clicked", lambda *_: self._start_placing("move_point", point))
            box.append(move)
            again = Gtk.Button(child=Adw.ButtonContent(icon_name="view-refresh-symbolic",
                                                       label="Measure again here"))
            again.set_tooltip_text("Replaces this point's readings with new ones. Undo brings "
                                   "the earlier ones back.")

            def remeasure(*_args) -> None:
                self.menu.popdown()
                self.measure(point.x, point.y, replace=project.points.index(point))

            again.connect("clicked", remeasure)
            again.set_sensitive(not self.busy)
            box.append(again)
            recheck = Gtk.Button(child=Adw.ButtonContent(icon_name="view-refresh-symbolic",
                                                         label="Check connection again"))
            recheck.set_tooltip_text("Only the latency, loss and DNS check, from wherever you are "
                                     "now: stand at this point first.")
            recheck.connect("clicked", lambda *_: self._recheck(point))
            box.append(recheck)
            label = "Delete point"
        else:
            wall = project.walls[index]
            box.append(Gtk.Label(label=f"Wall, {wall.db:g} dB", xalign=0, css_classes=["heading"]))
            label = "Delete wall"
        delete = Gtk.Button(child=Adw.ButtonContent(icon_name="user-trash-symbolic", label=label),
                            css_classes=["destructive-action"])

        def remove(*_args) -> None:
            self.menu.popdown()
            self._delete(kind, index)

        delete.connect("clicked", remove)
        box.append(delete)
        self._popup(box, x, y)

    # ----- access points: the estimated ones moved to where they really are, and planned ones -----

    def _start_placing(self, kind: str, target) -> None:
        self.menu.popdown()
        self._cancel_tap()
        self.wall_mode.set_active(False)
        self.scale_mode.set_active(False)
        self._placing = (kind, target)
        self.status.set_text({"add_virtual": "Tap where the planned access point would go.",
                              "move_virtual": "Tap the planned access point's new position.",
                              "move_site": "Tap where this access point really is.",
                              "move_point": "Tap where this point was really measured.",
                              "place_tone": "Tap where the HackRF sending the test tone is.",
                              "place_watch": "Tap where you watched the network from.",
                              "move_watch": "Tap where you watched the network from."}[kind]
                             + " (Tap Undo to cancel.)")

    def _place(self, plan_x: float, plan_y: float) -> None:
        kind, target = self._placing
        self._placing = None
        project = self.session.project
        if not (0 <= plan_x <= project.width and 0 <= plan_y <= project.height):
            self.status.set_text("That's off the plan: nothing moved.")
            return
        if kind == "add_virtual":
            project.virtual_aps.append({"x": plan_x, "y": plan_y, "bands": ["2.4", "5"],
                                        "name": f"Planned AP {len(project.virtual_aps) + 1}"})
            self._history.append(("added", "virtual", len(project.virtual_aps) - 1,
                                  project.virtual_aps[-1]))
            self.status.set_text("Planned access point added. Choose 'Predicted signal with "
                                 "planned access points' under Map to see what it changes.")
        elif kind == "place_tone":
            self._set_tone_source(plan_x, plan_y)
            self.show_tone_setup()
            return
        elif kind == "place_watch":
            project.watches.append(dict(target, x=plan_x, y=plan_y))
            self.status.set_text(f"Added the watch of {target['ssid'] or 'the network'}: it's on "
                                 "the map and in the report under Networks watched.")
        elif kind == "move_watch":
            project.watches[target].update(x=plan_x, y=plan_y)
            self.status.set_text("Moved the watch.")
        elif kind == "move_virtual":
            ap = project.virtual_aps[target]
            ap["x"], ap["y"] = plan_x, plan_y
            self.status.set_text(f"Moved {ap.get('name') or 'the planned access point'}.")
        elif kind == "move_point":
            if not any(point is target for point in project.points):
                self.status.set_text("That point is gone: nothing moved.")
                return
            index = next(i for i, point in enumerate(project.points) if point is target)
            self._history.append(("moved", "point", index, (target, target.x, target.y)))
            target.x, target.y = plan_x, plan_y
            self.status.set_text(f"Moved point {index + 1}. Undo puts it back.")
        else:
            pin = self._pin_for(target)
            pin["x"], pin["y"] = plan_x, plan_y
            self.status.set_text("Access point moved to its real position. Its reach and the "
                                 "advice now use it.")
        self.session.changed()

    def _pin_for(self, site: dict) -> dict:
        """The saved pin for an estimated access point, made if it has none yet."""
        pins = self.session.project.ap_pins
        index = site.get("pin")
        if index is not None and index < len(pins):
            return pins[index]
        pin = {"bssids": list(site.get("bssids", [])), "name": site.get("name") or "",
               "networks": [[name, list(bands)] for name, bands in site["networks"]],
               "x": None, "y": None}
        pins.append(pin)
        site["pin"] = len(pins) - 1
        return pin

    def _site_menu(self, box: Gtk.Box, site: dict) -> None:
        def named(text: str) -> None:
            self._pin_for(site)["name"] = text
            site["name"] = text

        current = site.get("name") or ""
        box.append(self._text_button(
            f"Name: {current}" if current else "Name this access point…",
            "Name this access point", current, "Name, e.g. Dry Produce AP", named))
        move = Gtk.Button(child=Adw.ButtonContent(icon_name="find-location-symbolic",
                                                  label="Move to its real position"))
        move.connect("clicked", lambda *_: self._start_placing("move_site", site))
        box.append(move)
        if site.get("placed"):
            back = Gtk.Button(child=Adw.ButtonContent(icon_name="edit-undo-symbolic",
                                                      label="Back to the estimate"))

            def unpin(*_args) -> None:
                self.menu.popdown()
                pin = self._pin_for(site)
                pin["x"] = pin["y"] = None
                self.session.changed()

            back.connect("clicked", unpin)
            box.append(back)

    def _virtual_menu(self, box: Gtk.Box, index: int) -> None:
        project = self.session.project
        ap = project.virtual_aps[index]
        box.append(Gtk.Label(label="Planned access point", xalign=0, css_classes=["heading"]))
        def named(text: str) -> None:
            ap["name"] = text

        current = ap.get("name") or ""
        box.append(self._text_button(
            f"Name: {current}" if current else "Name this planned access point…",
            "Name this planned access point", current, "Name, e.g. Back office AP", named))
        bands = Gtk.Box(spacing=6, css_classes=["linked"])
        for band in ("2.4", "5", "6"):
            toggle = Gtk.ToggleButton(label=BAND_NAMES[band], active=band in ap.get("bands", []))

            def toggled(button, band=band) -> None:
                chosen = set(ap.get("bands") or [])
                (chosen.add if button.get_active() else chosen.discard)(band)
                ap["bands"] = sorted(chosen or {band}, key=lambda b: analysis.BAND_ORDER.get(b, 9))
                self.session.changed()

            toggle.connect("toggled", toggled)
            bands.append(toggle)
        box.append(bands)
        gain = planning.planned_gain(project, analysis.Context(project, profile=self._profile_key()),
                                     *self._filters(), sites=self.sites)
        if gain:
            box.append(Gtk.Label(
                label=f"With the planned access points {gain['after'] * 100:.0f}% of the surveyed "
                      f"floor would reach {gain['target']:.0f} dBm, from {gain['now'] * 100:.0f}% "
                      "now" + (f", plus about {gain['beyond_sqft']:,.0f} sq ft beyond it"
                               if gain.get("beyond_sqft") else "")
                      + f". Model: {gain['model']}.", xalign=0, wrap=True, max_width_chars=40,
                css_classes=["dim-label"]))
        move = Gtk.Button(child=Adw.ButtonContent(icon_name="find-location-symbolic", label="Move"))
        move.connect("clicked", lambda *_: self._start_placing("move_virtual", index))
        box.append(move)
        delete = Gtk.Button(child=Adw.ButtonContent(icon_name="user-trash-symbolic", label="Delete"),
                            css_classes=["destructive-action"])

        def remove(*_args) -> None:
            self.menu.popdown()
            self._delete("virtual", index)

        delete.connect("clicked", remove)
        box.append(delete)

    def _text_button(self, label: str, heading: str, current: str, placeholder: str,
                     then: Callable[[str], None]) -> Gtk.Button:
        """
        A popup menu button that asks for text in a dialog. Text boxes can't live in the popup
        itself on a touch screen: tapping the on-screen keyboard dismisses the popup, and with
        the text box's touch handle popup still open over it GNOME kills the app ("destroyed
        popup not top most popup").
        """
        if len(label) > 34:                          # a long note would stretch the popup
            label = label[:33].rstrip() + "…"
        button = Gtk.Button(child=Adw.ButtonContent(icon_name="document-edit-symbolic", label=label))

        def clicked(*_args) -> None:
            self.menu.popdown()
            entry = Gtk.Entry(text=current, placeholder_text=placeholder, activates_default=True,
                              hexpand=True)
            dialog = Adw.AlertDialog(heading=heading, extra_child=entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("save", "Save")
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("save")
            dialog.set_close_response("cancel")

            def answered(_dialog, response: str) -> None:
                text = entry.get_text().strip()
                if response == "save" and text != current:
                    then(text)
                    self.session.changed()

            dialog.connect("response", answered)
            dialog.present(self)
            # Focused, so the on-screen keyboard opens for it on a tablet
            GLib.idle_add(lambda: entry.grab_focus() and False)

        button.connect("clicked", clicked)
        return button

    def _filters(self) -> tuple[str | None, str]:
        ssid = selected_text(self.ssid) if self.ssid.get_selected() > 0 else None
        return ssid, BANDS[min(self.band.get_selected(), len(BANDS) - 1)][1]

    def _allowance_changed(self, row, _param) -> None:
        project = self.session.project
        value = float(row.get_value())
        if abs(value - (project.client_allowance_db or 0)) < 0.01:
            return
        project.client_allowance_db = value
        self.session.changed()

    def _recheck(self, point) -> None:
        self.menu.popdown()
        device = selected_device(self.devices)
        number = self.session.project.points.index(point) + 1 if point in self.session.project.points else 0
        self.status.set_text(f"Checking the connection for point {number}…")

        def done(result, error) -> None:
            if error:
                self.status.set_text(f"Couldn't check the connection: {error}")
                return
            if point.checks:
                # Replaces this network's check, keeping the other focus networks'
                point.checks = [c for c in point.checks if c.get("ssid") != result.get("ssid")]
                point.checks.append(result)
                point.connectivity = point.checks_made()[0]
            else:
                point.connectivity = result
            self.session.changed()
            self.status.set_text(f"Point {number}: {connectivity.describe(result)}")

        run_in_thread(lambda: connectivity.check(device), done)

    def _popup(self, box: Gtk.Widget, x: float, y: float) -> None:
        self.menu.set_child(box)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.menu.set_pointing_to(rect)
        self.menu.popup()

    def _delete(self, kind: str, index: int) -> None:
        project = self.session.project
        items = _items(project, kind)
        if not 0 <= index < len(items):
            return
        item = items.pop(index)
        self._history.append(("removed", kind, index, item))
        if kind == "point":
            self.refresh_networks()
            self.status.set_text(f"Deleted point {index + 1}. Undo brings it back.")
        else:
            self.status.set_text(f"Deleted the {_KIND_WORDS[kind]}. Undo brings it back.")
        self.session.changed()

    def _wall_toggled(self, button) -> None:
        if button.get_active():
            self.scale_mode.set_active(False)
        self.status.set_text(
            "Drag across the plan to mark a wall. Pinch still zooms." if button.get_active()
            else "Tap the plan to measure. Drag to pan, pinch to zoom."
        )

    # ----- the scale: two spots a known distance apart -----

    def _scale_bar(self) -> Gtk.Widget:
        """
        Where the real distance is typed once the line is drawn. It sits on the map rather than
        in a dialog, so the line stays in view and it can't open behind the window.
        """
        self.scale_feet = Gtk.SpinButton.new_with_range(0.5, 5000, 1)
        self.scale_feet.set_digits(1)
        self.scale_feet.set_width_chars(6)
        self.scale_feet.set_numeric(True)             # digits only, and a number keypad on screen
        self.scale_feet.connect("activate", lambda *_: self._apply_scale())
        self.scale_prompt = Gtk.Label(label="How far apart are the two ends?", xalign=0, wrap=True,
                                      max_width_chars=34, css_classes=["heading"])
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._retry_scale())
        apply = Gtk.Button(label="Set scale", css_classes=["suggested-action"])
        apply.connect("clicked", lambda *_: self._apply_scale())
        feet = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        feet.append(self.scale_feet)
        feet.append(Gtk.Label(label="ft"))
        self.scale_bar = Gtk.Box(spacing=12, css_classes=["osd", "toolbar", "scale-bar"],
                                 halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                                 margin_top=12, margin_start=12, margin_end=12, visible=False)
        for widget in (self.scale_prompt, feet, cancel, apply):
            self.scale_bar.append(widget)
        self._scale_pixels = 0.0
        return self.scale_bar

    def _scale_toggled(self, button) -> None:
        self._ruler = []
        self.scale_bar.set_visible(False)
        if button.get_active():
            self.wall_mode.set_active(False)
            self._cancel_tap()
            project = self.session.project
            now = (f" Now {project.feet(project.width):.0f} × {project.feet(project.height):.0f} ft."
                   if project.meters_per_pixel else "")
            self.status.set_text("Draw a line between two spots a known distance apart, such as "
                                 "both ends of a wall or a doorway, or tap each end." + now)
        else:
            self.status.set_text("Tap the plan to measure. Drag to pan, pinch to zoom.")
        self.area.queue_draw()

    def _ruler_tap(self, x: float, y: float) -> None:
        spot = self._to_plan(x, y)
        if len(self._ruler) == 2:
            self._ruler = []              # a finished line: this tap starts a new one
            self.scale_bar.set_visible(False)
        if self._ruler:
            sx, sy = self._to_screen(*self._ruler[0])
            if math.hypot(sx - x, sy - y) < 10:
                return                    # the same spot again, or the second tap of a double-tap
        self._ruler = self._ruler + [spot]
        self.area.queue_draw()
        if len(self._ruler) == 1:
            self.status.set_text("Now tap the other end.")
            return
        (x1, y1), (x2, y2) = self._ruler
        self._ask_distance(math.hypot(x2 - x1, y2 - y1))

    def _ask_distance(self, pixels: float) -> None:
        known = self.session.project.feet(pixels)
        self._scale_pixels = pixels
        self.scale_prompt.set_text(
            f"By the current scale the ends are {known:.1f} ft apart. Real distance:" if known
            else "How far apart are the two ends?")
        self.scale_feet.set_value(round(known, 1) if known else 10)
        self.scale_bar.set_visible(True)
        self.scale_feet.grab_focus()
        self.status.set_text("Enter the distance in feet, then Set scale. Draw again to redo the line.")
        self.area.queue_draw()

    def _apply_scale(self) -> None:
        if not self.scale_bar.get_visible() or self._scale_pixels <= 0:
            return
        project = self.session.project
        self.scale_feet.update()
        feet = self.scale_feet.get_value()
        project.set_scale(self._scale_pixels, feet)
        self.scale_mode.set_active(False)         # hides the bar and clears the line
        self.status.set_text(
            f"Scale set: {feet:g} ft. The plan is {project.feet(project.width):.0f} × "
            f"{project.feet(project.height):.0f} ft.")
        self.session.changed()                    # redraws the heatmap at its true radius

    def _retry_scale(self) -> None:
        self._ruler = []
        self.scale_bar.set_visible(False)
        self.status.set_text("Draw a line between two spots a known distance apart, or tap each end.")
        self.area.queue_draw()

    def undo(self, *_args) -> None:
        project = self.session.project
        if self._placing is not None:
            self._placing = None
            self.status.set_text("Cancelled.")
            return
        if self._history:
            action, kind, index, item = self._history.pop()
            items = _items(project, kind)
            if action == "replaced":
                old, new = item
                if index < len(items) and items[index] is new:
                    items[index] = old
                    self.status.set_text(f"Point {index + 1} has its earlier readings back.")
                else:
                    self.undo()
                    return
            elif action == "moved":
                point, x, y = item
                if not any(existing is point for existing in items):
                    self.undo()         # deleted since; step further back
                    return
                point.x, point.y = x, y
                self.status.set_text("Point moved back.")
            elif action == "removed":
                items.insert(min(index, len(items)), item)
                self.status.set_text(f"Restored the {_KIND_WORDS[kind]}.")
            elif any(existing is item for existing in items):
                items.remove(item)
                self.status.set_text(f"Removed the last {_KIND_WORDS[kind]} added.")
            else:
                self.undo()             # already gone some other way; step further back
                return
        # A survey opened from disk has no history here, so fall back to the newest item
        elif self.wall_mode.get_active() and project.walls:
            project.walls.pop()
            self.status.set_text(f"{len(project.walls)} walls marked")
        elif project.points:
            project.points.pop()
            self.status.set_text(f"{len(project.points)} measurement points")
        else:
            return
        self.refresh_networks()
        self.session.changed()

    def fit(self) -> None:
        width = self.area.get_width()
        height = self.area.get_height()
        if width <= 1 or height <= 1:
            self._needs_fit = True
            return
        project = self.session.project
        self.scale = min(width / project.width, height / project.height) * 0.95
        self.offset = [(width - project.width * self.scale) / 2,
                       (height - project.height * self.scale) / 2]
        self._needs_fit = False
        self.area.queue_draw()

    def _maybe_fit(self, *_args) -> None:
        if self._needs_fit:
            self.fit()

    # ---------- measuring ----------

    # ---------- the survey setup ----------

    def current_setup(self) -> surveysetup.Setup:
        """What a point measures now, from the rows on this page and the setup's choices."""
        stored = surveysetup.Setup.from_dict(self.session.project.survey_setup
                                             or settings.get("survey_setup"))
        # The spectrum ranges are the ones ticked on the Spectrum page
        wifi_ranges, bands, extra_ranges, band_groups = rangechoice.setup_choice()
        setup = surveysetup.Setup(
            wifi_ranges=wifi_ranges,
            sweeps=int(self.sweeps.get_value()), dwell_count=int(self.dwell_count.get_value()),
            dwell_seconds=int(self.dwell_seconds.get_value()),
            check_link=self.check_link.get_active(), join=self.join.get_active(),
            speed=self.speed.get_active(), bluetooth=self.scan_bt.get_active(),
            tags=self.scan_tags.get_active(), identify=self.iq_identify.get_active(),
            cellular=self.measure_cellular.get_active(),
            hidden_names=self.hidden_row.get_active(),
            band_groups=band_groups, bands=bands, extra_ranges=extra_ranges)
        setup.kind = stored.kind if stored.same_choices(setup) else surveysetup.matching_kind(setup)
        return setup

    def apply_setup(self, setup: surveysetup.Setup, keep: bool = True) -> None:
        """Sets every row to the setup; ``keep`` saves it with the survey as well."""
        self.sweeps.set_value(setup.sweeps)
        self.dwell_count.set_value(setup.dwell_count)
        self.dwell_seconds.set_value(setup.dwell_seconds)
        self.check_link.set_active(setup.check_link)
        self.join.set_active(setup.join)
        self.speed.set_active(setup.speed)
        self.scan_bt.set_active(setup.bluetooth)
        self.scan_tags.set_active(setup.tags)
        self.iq_identify.set_active(setup.identify)
        self.measure_cellular.set_active(setup.cellular)
        self.hidden_row.set_active(setup.hidden_names)
        rangechoice.choose_for_setup(setup.wifi_ranges, setup.bands, setup.extra_ranges,
                                     setup.band_groups)
        settings.put("survey_setup", setup.to_dict())
        if keep and self.session.project.survey_setup != setup.to_dict():
            self.session.project.survey_setup = setup.to_dict()
            self.session.changed()
        self._describe_setup()
        # The Spectrum page ticks the same ranges
        self.session.emit("spectrum-choice", self)

    def _choice_from_elsewhere(self, source=None) -> None:
        """Ranges ticked on the Spectrum page: the survey measures those now, and keeps them."""
        if source is self:
            return
        setup = self.current_setup()
        settings.put("survey_setup", setup.to_dict())
        stored = self.session.project.survey_setup
        if not stored or not surveysetup.Setup.from_dict(stored).same_choices(setup):
            self.session.project.survey_setup = setup.to_dict()
            self.session.changed()
        self._describe_setup()

    def open_setup(self) -> None:
        session = self.session
        analyzers = len({id(a) for a in (*session.sweepers(), *session.band_analyzers())})
        dialog = SurveySetupDialog(self.current_setup(), self.apply_setup, analyzers,
                                   bool(session.listening_hackrfs()))
        dialog.present(self.get_root())

    def _describe_setup(self, *_args) -> None:
        if not hasattr(self, "setup_row"):
            return
        setup = self.current_setup()
        session = self.session
        analyzers = len({id(a) for a in (*session.sweepers(), *session.band_analyzers())})
        seconds = surveysetup.seconds_per_point(setup, analyzers, bool(session.listening_hackrfs()))
        self.setup_row.set_subtitle(f"{surveysetup.describe(setup)} About {seconds:.0f} s a point.")

    def _setup_from_project(self, *_args) -> None:
        """A survey that carries its setup measures the same way when it's opened again."""
        stored = self.session.project.survey_setup
        if stored:
            self.apply_setup(surveysetup.Setup.from_dict(stored), keep=False)
        else:
            self._describe_setup()

    def measure(self, x: float, y: float, replace: int | None = None) -> None:
        """Measures at (x, y): a new point, or new readings for point ``replace`` (0-based)."""
        if self.busy:
            return
        if self.viewing is not None:
            self.status.set_text("An earlier survey is on the map. Move the timeline to this "
                                 "survey to measure.")
            return
        session = self.session
        device = selected_device(self.devices)
        if not device or device == "none":
            self.status.set_text("No wireless adapter to scan with.")
            return
        self.busy = True
        self.pending = (x, y)
        self._progress = {}
        self.status.set_text("Scanning…")
        self.area.queue_draw()
        sweeps_per_band = int(self.sweeps.get_value())
        dwell_count = int(self.dwell_count.get_value())
        dwell_seconds = float(self.dwell_seconds.get_value())
        chosen = self.ssid.get_selected()
        chosen_ssid = None if chosen <= 0 else self.ssid.get_model().get_string(chosen)
        check_link = self.check_link.get_active()
        focus = list(session.project.focus_networks or [])
        join_first = self.join.get_active() and chosen_ssid is not None and not focus
        speed_server = self.speed_server.get_text().strip() if self.speed.get_active() else ""
        scan_bluetooth = self.scan_bt.get_active()
        scan_finder_tags = self.scan_tags.get_active()
        identify_on = self.iq_identify.get_active()
        cellular_on = self.measure_cellular.get_active()
        listen_hidden = self.hidden_row.get_active()
        known_names = dict(session.project.hidden_names)
        monitor_users = sorted(session.monitor_users)
        _wifi, band_keys, extra_ranges, _groups = rangechoice.setup_choice()
        other_bands = [rfbands.BY_KEY[k] for k in band_keys]
        wifi_choice = set(_wifi_choice())
        tone_source = dict(self.session.project.tone_source or {}) or None
        plan = advanced_tests.active_plan(session.project)
        fallback_server = self.speed_server.get_text().strip()
        scan_devices = wifi.scan_adapters(
            device, session.monitor_device, bool(settings.get("scan_all_adapters", True)),
            [r["adapter"] for r in plan["rows"]] if plan and plan["scan_with"] else [])
        scan_notes: list[str] = []
        revealed: dict[str, str] = {}             # hidden names heard here, for done()
        hidden_method: dict = {}

        def say(part: str, text: str) -> None:
            GLib.idle_add(self._show_progress, part, text)

        # The Advanced table speed-tests its own networks on their adapters
        advanced_ssids = {r["ssid"] for r in plan["rows"]} if plan else set()

        def test_focus(point) -> None:
            """
            Joins each focus network heard at this point, strongest first, and checks it (and
            speed-tests it), keeping one result per network in point.checks. One not heard here,
            or without a saved password, is noted as skipped, not tried.
            """
            strongest: dict[str, int] = {}
            for network in point.networks:
                if network["ssid"] in focus:
                    strongest[network["ssid"]] = max(strongest.get(network["ssid"], -200),
                                                     int(network["rssi"]))
            order = sorted(strongest, key=lambda name: -strongest[name])
            speeds = []
            for number, name in enumerate(order, start=1):
                prefix = f"{name} ({number} of {len(order)})"
                skip = connectivity.why_not_test(name, point.networks, allow_open=True)
                if skip:
                    point.checks.append({"ssid": name, "skipped": skip})
                    say("wifi", f"{prefix}: not joined, {skip}")
                    continue
                say("wifi", f"{prefix}: joining…")
                trouble = connectivity.join(device, name)
                if trouble:
                    point.checks.append({"ssid": name, "error": f"couldn't join: {trouble}"})
                    point.notes.append(f"Couldn't join {name}: {trouble}")
                    continue
                if not point.checks_made():
                    point.link = wifi.link(device) or point.link    # the network a client would use
                if check_link:
                    say("wifi", f"{prefix}: checking the connection…")
                    result = connectivity.check(device)
                    result.setdefault("ssid", name)
                    point.checks.append(result)
                    trouble = connectivity.problem(result)
                    if trouble:
                        point.notes.append(f"Connection on {name}: {trouble}")
                if speed_server and name not in advanced_ssids:
                    say("wifi", f"{prefix}: testing throughput to {speed_server}…")
                    speed = throughput.test(device, speed_server)
                    speed["ssid"] = name
                    speeds.append(speed)
                    if speed.get("error"):
                        point.notes.append(f"{name}: {throughput.describe(speed)}")
            missing = [name for name in focus if name not in strongest]
            for name in missing:
                point.checks.append({"ssid": name, "skipped": "not heard here"})
            made = point.checks_made()
            point.connectivity = made[0] if made else None
            if speeds:
                point.throughput = speeds[0]
                point.throughputs.extend(speeds[1:])
            say("wifi", f"Networks tested: {len(made)} of {len(focus)}"
                        + (f"; not heard here: {', '.join(missing)}" if missing else ""))

        def scan_wifi(passive: bool):
            many = len(scan_devices) > 1
            say("wifi", ("Listening for Wi-Fi…" if passive else "Scanning Wi-Fi…")
                + (f" ({len(scan_devices)} adapters)" if many else ""))
            if many:
                readings, notes = wifi.scan_split(scan_devices, passive=passive)
                scan_notes.extend(notes)
            else:
                readings = wifi.scan(device, passive=passive)
            found = wifi.link(device)
            say("wifi", f"Wi-Fi: {len(readings)} access points")
            return readings, found

        def sweep_spectrum(pool: list):
            """
            Every analyzer at once, each with its share of the ranges (rf.analyzer.share_ranges:
            the same share at every point). Each segment says which analyzer measured it.
            """
            wanted = [r for i, r in enumerate(ranges_for(pool)) if i in wifi_choice]
            jobs = share_ranges(wanted, pool)
            segments, notes = [], []
            lock = threading.Lock()

            def run(analyzer, mine) -> None:
                name_of = analyzer_label(analyzer)
                # Held for the whole capture so the live spectrum view can't retune between ranges
                with analyzer.lock:
                    for name, start, end in mine:
                        say("rf", f"Sweeping {name}" + (f" with the {name_of}" if len(pool) > 1 else "") + "…")
                        try:
                            for config, sweeps in analyzer.sweep_range(start, end, count=sweeps_per_band):
                                segment = spec.build_segment(config, sweeps)
                                segment["analyzer"] = name_of
                                with lock:
                                    segments.append(segment)
                        except Exception as error:
                            with lock:
                                notes.append(f"Spectrum {start:.0f}-{end:.0f} MHz ({name_of}): {error}")

            per = {}
            for analyzer, job in jobs:
                per.setdefault(id(analyzer), (analyzer, []))[1].append(job)
            threads = [threading.Thread(target=run, args=work, daemon=True) for work in per.values()]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            segments.sort(key=lambda seg: seg["start_mhz"])
            say("rf", f"Spectrum: {len(segments)} of {len(jobs)} ranges"
                + (f" from {len(per)} analyzers" if len(per) > 1 else ""))
            return segments, notes

        def watch_channels(pool: list, networks: list[dict]) -> tuple[list[dict], list[str]]:
            """The quiet dwell: the analyzers only listen, sharing the channels, and the Wi-Fi scan is finished."""
            channels = spec.dwell_channels(networks, dwell_count, chosen_ssid)
            jobs = share_channels(channels, pool)
            dwells, notes = [], []
            lock = threading.Lock()
            counter = [0]

            def run(analyzer, mine) -> None:
                with analyzer.lock:
                    for center, width in mine:
                        with lock:
                            counter[0] += 1
                            say("rf", f"Watching {center:.0f} MHz ({width:.0f} MHz wide), "
                                      f"{counter[0]} of {len(jobs)}…")
                        try:
                            config, sweeps = analyzer.dwell(center, dwell_seconds, width)
                            dwell = spec.summarize_dwell(config, sweeps, center, width)
                            dwell["analyzer"] = analyzer_label(analyzer)
                            with lock:
                                dwells.append(dwell)
                        except Exception as error:
                            with lock:
                                notes.append(f"Watching {center:.0f} MHz: {error}")

            per = {}
            for analyzer, job in jobs:
                per.setdefault(id(analyzer), (analyzer, []))[1].append(job)
            threads = [threading.Thread(target=run, args=work, daemon=True) for work in per.values()]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            order = {job: i for i, (_a, job) in enumerate(jobs)}
            dwells.sort(key=lambda d: order.get((d.get("center_mhz"), d.get("width_mhz")), 0))
            return dwells, notes

        def identify_channels(radios: list, networks: list[dict], segments: list[dict]):
            """
            The HackRFs' close look: the strongest networks' channels and unexplained energy,
            shared between them when there are several.
            """
            centres: list[float] = []
            ranked = sorted(networks, key=lambda n: (not chosen_ssid or n.get("ssid") != chosen_ssid,
                                                     -n["rssi"]))
            for network in ranked:
                if len(centres) >= max(1, dwell_count):
                    break
                freq = float(network.get("freq_mhz") or 0)
                if freq and all(abs(freq - c) >= 10 for c in centres):
                    centres.append(freq)
            if segments:
                probe = Point(x=0, y=0, networks=networks, spectrum={"segments": segments})
                for emitter in analysis.find_emitters(probe, None)[:2]:
                    if all(abs(emitter["center"] - c) >= 10 for c in centres):
                        centres.append(emitter["center"])
            # hackrf_transfer only tunes to 6 GHz, though hackrf_sweep goes further
            centres = [c for c in centres if c <= hrf.IQ_TOP_MHZ - 10]
            results, notes = hrf.identify_all(radios, centres, lambda text: say("rf", text))
            named = sorted({f["name"] for r in results for f in r["findings"] if f["kind"] != "wifi"})
            say("rf", f"HackRF heard {', '.join(named)}" if named else "HackRF: only Wi-Fi")
            return results, notes

        def measure_tone(receiver, freq_mhz: float) -> dict:
            say("rf", f"Listening for the test tone at {freq_mhz:.0f} MHz…")
            config, sweeps = receiver.dwell(freq_mhz, 2.0, width_mhz=2)
            found = spec.tone_level(config, sweeps, freq_mhz)
            found["analyzer"] = getattr(receiver, "model", None) or receiver.name
            say("rf", f"Test tone: {found['dbm']:.0f} dBm" if found["dbm"] is not None
                else "Test tone: not heard")
            return found

        def listen_for_hidden(networks: list[dict]) -> dict:
            """
            Monitor mode on the channels of the hidden networks heard here with no name yet,
            strongest first, for the names they say when a device joins. What it heard goes in
            ``revealed``; returns what it did, for the point's method.
            """
            waiting = sorted((n for n in networks if n.get("hidden") and not n.get("ssid")),
                             key=lambda n: -n["rssi"])
            if not waiting:
                return {}
            listener = session.monitor_device or wifi.choose_roles(wifi.interfaces())[1]
            why = ("it needs root: relaunch as root" if not wifi.is_root() else
                   "Scapy isn't installed (pip install scapy)" if not capture.available() else
                   "there's no second adapter to listen with (the scanning one can't)"
                   if not listener or listener == device or listener in scan_devices else
                   f"{listener} doesn't do monitor mode" if not wifi.supports_monitor(listener)
                   else f"{' and '.join(monitor_users)} is using {listener}" if monitor_users
                   else "")
            if why:
                say("wifi", f"Hidden network names not listened for: {why}")
                return {"skipped": why}
            channels: list[tuple[float, int, float]] = []
            for network in waiting:
                freq = float(network.get("freq_mhz") or 0)
                if freq and all(abs(freq - c[0]) >= 1 for c in channels):
                    channels.append((freq, int(network.get("width_mhz") or 20),
                                     float(network.get("center_mhz") or freq)))
                if len(channels) >= surveysetup.HIDDEN_CHANNELS:
                    break
            if not monitor.is_monitor(listener):
                say("wifi", f"Switching {listener} to monitor mode…")
                lines: list[str] = []
                if not monitor.enable(listener, None, True, lines.append):
                    why = f"{listener} wouldn't switch to monitor mode ({lines[-1] if lines else '?'})"
                    say("wifi", f"Hidden network names not listened for: {why}")
                    return {"skipped": why}
                self._keep_in_monitor(listener)
            session.monitor_users.add("Survey")
            try:
                heard = capture.listen_for_names(
                    listener, channels, surveysetup.HIDDEN_SECONDS, lambda text: say("wifi", text))
            finally:
                session.monitor_users.discard("Survey")
            revealed.update(heard)
            hidden.apply(networks, {**known_names, **heard})
            say("wifi", f"Hidden networks named: {', '.join(sorted(set(heard.values())))}" if heard
                else f"Hidden networks: no device joined one in {len(channels) * surveysetup.HIDDEN_SECONDS} s")
            return {"adapter": listener, "channels": [c[0] for c in channels],
                    "seconds": surveysetup.HIDDEN_SECONDS, "named": sorted(heard)}

        def measure_into(point: Point) -> None:
            # No Wi-Fi ranges ticked in the setup: no Wi-Fi spectrum, so no analyzers for it
            pool = session.sweepers() if wifi_choice else []
            segments, notes = [], []
            passive = False
            if not pool:
                readings, point.link = scan_wifi(passive=False)
            elif wifi.can_scan_passively():
                passive = True
                # Both at once. The card only listens meanwhile, so the analyzers never
                # mistake the card's own probe requests for someone else's energy.
                with ThreadPoolExecutor(max_workers=2) as executor:
                    spectrum_job = executor.submit(sweep_spectrum, pool)
                    wifi_job = executor.submit(scan_wifi, True)
                    readings, point.link = wifi_job.result()
                    segments, notes = spectrum_job.result()
            else:
                # NetworkManager scans by sending probe requests, which the analyzers would pick
                # up, so without root the two take turns.
                readings, point.link = scan_wifi(passive=False)
                segments, notes = sweep_spectrum(pool)

            point.networks = [_network_dict(r) for r in readings]
            if any(r.source == "nmcli" for r in readings):
                point.notes.append(
                    "Signal levels estimated from NetworkManager's 0-100 quality (not running "
                    "as root), so they aren't true dBm."
                )
            point.notes.extend(notes)
            dwells = []
            if pool and segments and dwell_count:
                dwells, notes = watch_channels(pool, point.networks)
                point.notes.extend(notes)
            radios = session.listening_hackrfs()        # not gone, and not sending the tone
            for radio in [d for d in pool if d in session.hackrfs]:
                theirs = [seg for seg in segments if seg.get("analyzer") == analyzer_label(radio)]
                if not theirs:
                    continue
                if not hrf.calibrated(radio.unit):
                    point.notes.append(f"Spectrum from the {radio.model} before it was calibrated "
                                       "(Setup → HackRF), so its noise floor and SNR are estimates.")
                hearing = hrf.band_hearing(point.networks, theirs)
                hrf.note_hearing(hearing, radio.unit)
                deaf = sorted(band for band, heard in hearing.items() if not heard)
                if deaf:
                    point.notes.append(hrf.antenna_note(deaf))
            # Before the follow-up scan, whose probe requests would be named as Wi-Fi here
            if radios and identify_on:
                point.iq, notes = identify_channels(radios, point.networks, segments)
                point.notes.extend(notes)
            if radios and cellular_on:
                say("rf", "HackRF: measuring the cellular bands…")
                try:
                    point.cellular = cell.summarize(*radios[0].sweep_bands(cell.sweep_ranges()))
                    best_cell = cell.strongest(point.cellular)
                    say("rf", f"Cellular: best {best_cell:.0f} dBm per 5 MHz" if best_cell is not None
                        else "Cellular: nothing heard")
                except Exception as error:
                    point.notes.append(f"Cellular bands: {error}")
            if other_bands or extra_ranges:
                point.bands, point.ranges, notes = measure_other_ranges(
                    session, other_bands, extra_ranges, say)
                point.notes.extend(notes)
            if tone_source:
                # An analyzer that reads true dBm first, then a HackRF that isn't the one sending
                freq = tone_source["freq_mhz"]
                listeners = [a for a in session.serial_analyzers if a.alive and a.max_mhz >= freq + 1]
                listeners += [d for d in radios if d.max_mhz >= freq + 1]
                if not listeners:
                    point.notes.append("The test tone wasn't measured: it needs an analyzer here "
                                       "(a HackRF sending the tone can't also hear it).")
                else:
                    try:
                        point.tone = measure_tone(listeners[0], freq)
                    except Exception as error:
                        point.notes.append(f"Test tone: {error}")
            if passive:
                # A passive scan only hears the beacons that happen to go by while it listens on
                # each channel, and misses many access points. Now the analyzer is done, a normal
                # scan can probe without its probes landing in the spectrum.
                say("wifi", "Scanning Wi-Fi again, now the analyzer is done…")
                try:
                    extra = [_network_dict(r) for r in
                             (wifi.scan_split(scan_devices)[0] if len(scan_devices) > 1
                              else wifi.scan(device, passive=False))]
                except Exception as error:
                    extra = []
                    point.notes.append(f"The follow-up Wi-Fi scan failed: {error}")
                before = len(point.networks)
                point.networks = _merge_scans(point.networks, extra)
                say("wifi", f"Wi-Fi: {len(point.networks)} access points "
                            f"({len(point.networks) - before} more from the second scan)")
            hidden.apply(point.networks, known_names)
            if listen_hidden:
                hidden_method.update(listen_for_hidden(point.networks))
            if segments:
                def floor(band: str):
                    # The channels watched closely measure it better than the wide sweep
                    return spec.band_noise_floor(dwells, band) or spec.band_noise_floor(segments, band)

                point.spectrum = {
                    "segments": segments,
                    "dwells": dwells,
                    "noise_24": floor("2.4"),
                    "noise_5": floor("5"),
                    "noise_6": floor("6"),
                }
                try:
                    point.spectrum_png = base64.b64encode(
                        snapshot_png(segments, point.networks)).decode("ascii")
                except Exception as error:     # the measurement stands without its picture
                    point.notes.append(f"Couldn't keep a picture of the spectrum: {error}")

            if focus and (check_link or speed_server):
                # Every focus network heard here in turn, strongest first: join, check, speed
                test_focus(point)
            elif join_first and (check_link or speed_server):
                skip = connectivity.why_not_test(chosen_ssid, point.networks)
                if skip:
                    say("wifi", f"Not joining {chosen_ssid}: {skip}")
                else:
                    say("wifi", f"Joining {chosen_ssid}…")
                    trouble = connectivity.join(device, chosen_ssid)
                    if trouble:
                        point.notes.append(f"Couldn't join the network: {trouble}")
                    else:
                        point.link = wifi.link(device) or point.link
            if check_link and not focus:
                # Last, so its packets can't show up in the spectrum
                say("wifi", "Checking the connection…")
                point.connectivity = connectivity.check(device)
                trouble = connectivity.problem(point.connectivity)
                if trouble:
                    point.notes.append(f"Connection: {trouble}")

            on = (wifi.link(device) or {}).get("ssid") if speed_server and not focus else None
            if focus:
                pass                              # tested network by network above
            elif speed_server and not chosen_ssid:
                say("wifi", "Throughput not tested: choose the network under Heatmap → Network")
            elif speed_server and on != chosen_ssid:
                say("wifi", f"Throughput not tested: the adapter isn't on {chosen_ssid}"
                            + (f" (it's on {on})" if on else ""))
            elif speed_server:
                say("wifi", f"Testing throughput to {speed_server}…")
                point.throughput = throughput.test(device, speed_server)
                point.throughput["ssid"] = chosen_ssid
                if point.throughput.get("error"):
                    point.notes.append(throughput.describe(point.throughput))

            if plan:
                rows = plan["rows"]
                say("wifi", f"Speed tests: {len(rows)} networks on "
                            f"{len({r['adapter'] for r in rows})} adapters…")
                slots = None
                if any(r.get("server") == iperf_servers.RANDOM or
                       (r.get("server") and ":" not in r["server"]) for r in rows):
                    try:
                        servers, _ = iperf_servers.get()
                        slots = iperf_servers.Slots(servers, plan["region"] or None)
                    except Exception as error:
                        point.notes.append(f"Couldn't load the public iperf3 server list: {error}")
                slots = slots or iperf_servers.Slots([])
                ran = throughput.run_plan(
                    rows, fallback_server, slots, say=lambda text: say("wifi", text),
                    heard=point.networks)
                point.throughputs.extend(ran)
                point.notes.extend(throughput.describe(r) for r in ran if r.get("error"))
                tested = [r for r in ran if not r.get("skipped")]
                skipped = [r for r in ran if r.get("skipped")]
                say("wifi", f"Speed tests: {len(tested)} of {len(rows)} networks tested"
                            + (f"; skipped {', '.join(r['ssid'] + ' (' + r['skipped'].split(':')[0] + ')' for r in skipped)}"
                               if skipped else ""))

            point.notes.extend(scan_notes)
            used = sorted({seg.get("analyzer") for seg in segments if seg.get("analyzer")})
            names_used = set(used) | {r.get("hackrf") for r in point.iq or [] if r.get("hackrf")}
            hackrfs_used = [d for d in session.hackrfs
                            if d.model in names_used or (d is (radios[0] if radios else None)
                                                         and (point.iq or point.cellular))]
            point.method = {
                "adapter": device, "adapter_name": _adapter_name(device),
                "scan_adapters": scan_devices if len(scan_devices) > 1 else None,
                "scan": "iw passive, then active" if passive else
                        ("iw" if any(n.get("rssi_source") == "iw" for n in point.networks)
                         else "NetworkManager (estimated)"),
                "analyzer": ", ".join(used) or None,
                # Which analyzer measured which range, so mixed analyzers can be told apart
                "analyzers": {f"{seg['start_mhz']:.0f}-{spec_end(seg):.0f}": seg.get("analyzer")
                              for seg in segments} or None,
                "sweeps_per_band": sweeps_per_band if segments else 0,
                "dwell": [dwell_count, dwell_seconds] if dwells else None,
                "hackrf": ", ".join(d.model + ("" if hrf.calibrated(d.unit) else " (uncalibrated)")
                                    for d in hackrfs_used) or None,
                # Each HackRF's calibration as it stood, since Setup's can change afterwards
                "hackrf_cal": {d.model: hrf.snapshot(d.unit) for d in hackrfs_used} or None,
                "wifi_readings": {"merged": sum(1 for n in point.networks if (n.get("readings") or 1) > 1)},
                "root": wifi.is_root(),
                "hidden_listen": hidden_method or None,
            }

            if scan_bluetooth:
                if not bt.available():
                    point.notes.append(f"Bluetooth scan skipped: {bt.why_unavailable()}")
                else:
                    say("bt", "Scanning Bluetooth…")
                    found = bt.scan_serialised(6.0, lambda text: say("bt", text))
                    point.bluetooth = [d.as_dict() for d in found]
                    point.method["bt_adapters"] = bt.controller_names() or None
                    fixed = sum(1 for d in found if d.stable)
                    say("bt", f"Bluetooth: {len(found)} devices, {fixed} with fixed addresses")

            if scan_finder_tags:
                if not trackers.available():
                    point.notes.append(f"Finder tag scan skipped: {bt.why_unavailable()}")
                else:
                    say("tags", "Looking for finder tags…")
                    point.trackers = trackers.scan(8.0, lambda text: say("tags", text))
                    away = sum(1 for t in point.trackers if t.get("away"))
                    say("tags", f"Finder tags: {len(point.trackers)}"
                                + (f", {away} away from their owner" if away else ""))

            return

        def work() -> Point:
            """
            Everything after the Wi-Fi scan is extra: if a step fails, the point keeps what was
            measured, says what stopped, and the map still gets its access points.
            """
            point = Point(x=x, y=y)
            try:
                measure_into(point)
            except Exception as error:
                if not point.networks:
                    raise                     # not even the scan: nothing worth keeping
                point.notes.append(f"Measuring stopped early ({type(error).__name__}: {error}). "
                                   "The Wi-Fi scan is kept; hold the point and measure it again "
                                   "for the rest.")
            fix = session.gps.fix
            if fix and fix.fresh:
                point.gps = {"lat": fix.lat, "lon": fix.lon, "quality": fix.quality,
                             "satellites": fix.satellites, "hdop": fix.hdop}
            return point

        def done(point, error) -> None:
            self.busy = False
            self.pending = None
            points = self.session.project.points
            learned = []
            if point is not None:
                # Named everywhere, including this point, and kept with the survey
                learned = hidden.learn(self.session.project, revealed)
                hidden.apply(point.networks, self.session.project.hidden_names)
            if error:
                self.status.set_text(f"Measurement failed: {error}")
            elif replace is not None and replace < len(points):
                old = points[replace]
                point.note = old.note
                points[replace] = point
                self._history.append(("replaced", "point", replace, (old, point)))
                self.refresh_networks()
                self.session.changed()
                self.status.set_text(f"Point {replace + 1} measured again: "
                                     f"{len(point.networks)} access points. Undo brings back "
                                     "the earlier readings.")
            else:
                self.session.project.points.append(point)
                self._history.append(("added", "point", len(self.session.project.points) - 1, point))
                self.refresh_networks()
                self.session.changed()
                strongest = max((n["rssi"] for n in point.networks), default=None)
                estimated = any(n.get("rssi_source") == "nmcli" for n in point.networks)
                self.status.set_text(
                    f"Point {len(self.session.project.points)}: {len(point.networks)} access points"
                    + (f", strongest {strongest} dBm" if strongest is not None else "")
                    + (f", {len(point.spectrum['segments'])} spectrum ranges" if point.spectrum else "")
                    + (f", {len(point.spectrum['dwells'])} channels watched"
                       if point.spectrum and point.spectrum.get("dwells") else "")
                    + (" (estimated: run as root for true dBm)" if estimated else "")
                    + _tone_words(point)
                    + (". Some steps failed; hold the point to see why, or measure it again"
                       if any(n.startswith("Measuring stopped early") for n in point.notes) else "")
                )
            if learned and not error:
                self.status.set_text(f"{self.status.get_text()} "
                                     f"{hidden.describe_learned(self.session.project, learned)}")
            self.area.queue_draw()

        run_in_thread(work, done)

    def _keep_in_monitor(self, device: str) -> None:
        """
        The adapter the survey switched to monitor mode stays in it between points, since
        switching takes seconds, and goes back to NetworkManager when the app closes.
        """
        if getattr(self, "_monitor_switched", None) == device:
            return
        self._monitor_switched = device
        atexit.register(monitor.restore, device, lambda _line: None)

    def _show_progress(self, part: str, text: str) -> bool:
        if self.busy:
            self._progress[part] = text
            self.status.set_text(" · ".join(self._progress[key] for key in ("wifi", "rf", "bt", "tags")
                                            if key in self._progress))
        return False

    # ---------- advanced network tests ----------

    def open_advanced(self) -> None:
        networks = {n["ssid"] for p in self.session.project.points for n in p.networks if n["ssid"]}
        networks |= {r.ssid for r in self.session.last_scan if r.ssid}
        advanced_tests.AdvancedDialog(self.session.project, sorted(networks),
                                      self._advanced_saved).present(self.get_root())

    def add_watch(self, watch: dict) -> None:
        """A Watch-tab session into this survey: placed where it was watched from, if there's a plan."""
        if self.viewing is not None:
            self.timeline_button.set_active(False)
            self._leave_timeline()
        if not self.session.project.plan_png:
            self.session.project.watches.append(dict(watch))
            self.session.changed()
            self.status.set_text(f"Added the watch of {watch['ssid'] or 'the network'} to this "
                                 "survey's report. Load a plan to place it on the map.")
            return
        self._start_placing("place_watch", watch)

    def _pending_watch(self, *_args) -> None:
        """A watch waiting for the survey just opened: placed on it once everything has loaded."""
        pending = getattr(self.session, "pending_watch", None)
        project = self.session.project
        if not pending or not project.path:
            return
        # The file chosen, or where it's saved from now on when it lived in root's folder
        wanted = {pending["path"], str(Path(library.shared_path_for(pending["path"])).resolve())}
        if str(Path(project.path).resolve()) in wanted:
            self.session.pending_watch = None
            GLib.idle_add(lambda: (self.session.emit("add-watch", pending["watch"]), False)[1])

    def _watch_menu(self, box: Gtk.Box, index: int) -> None:
        move = Gtk.Button(child=Adw.ButtonContent(icon_name="find-location-symbolic", label="Move"))
        move.connect("clicked", lambda *_: self._start_placing("move_watch", index))
        delete = Gtk.Button(child=Adw.ButtonContent(icon_name="user-trash-symbolic",
                                                    label="Remove from the survey"),
                            css_classes=["destructive-action"])

        def remove(*_args) -> None:
            self.menu.popdown()
            del self.session.project.watches[index]
            self.session.changed()

        delete.connect("clicked", remove)
        box.append(move)
        box.append(delete)

    def choose_focus(self) -> None:
        focus_networks.FocusDialog(self.session.project, self.session.last_scan,
                                   self._focus_saved).present(self.get_root())

    def _focus_saved(self) -> None:
        self.session.changed()            # part of the survey: it changes the report and map
        self._describe_focus()

    def _describe_focus(self, *_args) -> None:
        self.focus_row.set_subtitle(focus_networks.summary(self.session.project))

    def _advanced_saved(self) -> None:
        self.session.changed()            # the table is part of the survey
        self._describe_advanced()

    def _describe_advanced(self) -> None:
        text = advanced_tests.summary(self.session.project)
        self.advanced_row.set_subtitle(
            text or "Off for this survey. Choose Advanced… at the end of the Network list (or "
                    "Edit) to test several networks at once, each on its own adapter.")

    # ---------- where I am ----------

    def _show_me_toggled(self, row, _param) -> None:
        settings.put("survey_locate", row.get_active())
        if row.get_active():
            self.show_me.set_subtitle("Finding you…")
            GLib.timeout_add(LOCATE_EVERY_MS, self._locate_tick)
        else:
            self.me = None
            self.show_me.set_subtitle(ME_HELP)
            self.area.queue_draw()

    def _forget_locate_model(self, *_args) -> None:
        self._locate_model = None           # rebuilt from the edited survey on the next scan

    def _new_survey_for_locator(self, *_args) -> None:
        self._locate_model = None
        self.locator.reset()
        self.me = None

    def _locate_tick(self) -> bool:
        """Scans just the survey's channels and moves the marker. Steps aside while measuring."""
        if not self.show_me.get_active():
            return False
        if self._locating or self.busy or not self.get_mapped():
            return True
        project = self.session.project
        if not project.points:
            self.show_me.set_subtitle("Measure some points first: you're placed from what "
                                      "was heard at them.")
            return True
        device = selected_device(self.devices)
        if not device or device == "none":
            self.show_me.set_subtitle("No wireless adapter to scan with.")
            return True
        model = self._locate_model
        if model is None:
            snapshot = copy.copy(project)
            snapshot.points = list(project.points)
            snapshot.walls = list(project.walls)
        self._locating = True

        def work():
            built = model or positioning.build_model(snapshot)
            return built, positioning.readings_from(wifi.scan(device, freqs=built.freqs or None))

        def done(result, error) -> None:
            self._locating = False
            if error or not self.show_me.get_active():
                return                       # a busy radio: try again on the next tick
            built, heard = result
            if model is None and project is self.session.project:
                self._locate_model = built
            self.me = self.locator.update(built, heard)
            if self.me is None:
                surveyed = sum(1 for bssid, rssi in heard.items()
                               if bssid in built.known and rssi >= positioning.USABLE_DBM)
                self.show_me.set_subtitle(
                    f"Can't place you: {surveyed} surveyed access point{'s' if surveyed != 1 else ''} "
                    f"heard here, and it takes {positioning.MIN_APS}. Measure a point here.")
            else:
                spread = _feet(project, self.me.radius)
                self.show_me.set_subtitle(
                    f"{self.me.aps} access points"
                    + (f", ±{spread:.0f} ft" if spread else "")
                    + (f", {self.locator.bias_db:+.0f} dB calibration" if self.locator.bias_db else "")
                    + ". Hold where you really are and choose I'm here to correct it.")
            self.area.queue_draw()

        run_in_thread(work, done)
        return True

    def _calibrate(self, plan_x: float, plan_y: float, quiet: bool = False) -> None:
        model = self._locate_model
        if model is None:
            self.locator.track = (plan_x, plan_y)
            return
        shift = self.locator.calibrate(model, plan_x, plan_y)
        if self.me is not None:
            self.me.x, self.me.y = plan_x, plan_y
        self.area.queue_draw()
        if quiet:
            return
        if shift is None:
            self.status.set_text("Moved you here. Too few access points in the last scan to "
                                 "calibrate the readings; it'll follow from here.")
        else:
            self.status.set_text(
                "Recalibrated here: access points read "
                + (f"{abs(shift):.0f} dB {'stronger' if shift > 0 else 'weaker'} than the survey "
                   "expects, and that's now allowed for." if abs(shift) >= 1
                   else "as the survey expects."))

    def _here_menu(self, x: float, y: float) -> None:
        plan_x, plan_y = self._to_plan(x, y)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["map-menu"])
        box.append(Gtk.Label(label="Where you are", xalign=0, css_classes=["heading"]))
        here = Gtk.Button(label="I'm here", css_classes=["suggested-action"])

        def chosen(*_args) -> None:
            self.menu.popdown()
            self._calibrate(plan_x, plan_y)

        here.connect("clicked", chosen)
        box.append(here)
        self._popup(box, x, y)

    # ---------- drawing ----------

    def reload(self) -> None:
        project = self.session.project
        self.timeline_button.set_active(False)
        self._leave_timeline()
        self._describe_advanced()
        self.plan = None
        self._history.clear()
        self._placing = None
        self.allowance.set_value(project.client_allowance_db or 0)
        data = project.plan_bytes()
        if data:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            self.plan = _surface_from_pixbuf(loader.get_pixbuf())
        self.refresh_networks()
        self.redraw_heatmap()
        self._needs_fit = True
        self._maybe_fit()

    def _ssid_changed(self, *_args) -> None:
        if self._refreshing_ssids:
            return
        if selected_text(self.ssid) == ADVANCED:
            # Not a network to filter by: opens the table and goes back to what was chosen
            self._refreshing_ssids = True
            self.ssid.set_selected(self._ssid_before_advanced)
            self._refreshing_ssids = False
            self.open_advanced()
            return
        self._ssid_before_advanced = self.ssid.get_selected()
        self._refresh_bssids()
        self.update_quality()
        self.redraw_heatmap()

    def refresh_networks(self) -> None:
        current = selected_text(self.ssid)
        names = ["Any network"] + sorted(
            {n["ssid"] for p in self.session.project.points for n in p.networks if n["ssid"]}
        ) + [ADVANCED]
        channels = sorted({
            n["channel"] for p in self.session.project.points for n in p.networks
            if n.get("band") in ("2.4", "5") and n.get("channel")
        })
        channel_names = [AUTO_CHANNEL] + [f"Channel {c}" for c in channels]
        current_channel = selected_text(self.channel)
        self._refreshing_ssids = True
        self.ssid.set_model(Gtk.StringList.new(names))
        if current in names and current != ADVANCED:
            self.ssid.set_selected(names.index(current))
        self._ssid_before_advanced = self.ssid.get_selected()
        self.channel.set_model(Gtk.StringList.new(channel_names))
        if current_channel in channel_names:
            self.channel.set_selected(channel_names.index(current_channel))
        self._refresh_bssids()
        self._refreshing_ssids = False
        self.update_quality()

    def _refresh_bssids(self) -> None:
        """The access points of the chosen network, each with its manufacturer."""
        ssid = selected_text(self.ssid) if self.ssid.get_selected() > 0 else None
        seen: dict[str, str] = {}
        for point in self.session.project.points:
            for n in point.networks:
                if ssid is None or n["ssid"] == ssid:
                    seen.setdefault(n["bssid"], n.get("vendor") or "")
        labels = [ALL_BSSIDS] + [f"{b} — {v}" if v else b for b, v in sorted(seen.items())]
        current = selected_text(self.bssid)
        guard = self._refreshing_ssids
        self._refreshing_ssids = True
        self.bssid.set_model(Gtk.StringList.new(labels))
        self.bssid.set_selected(labels.index(current) if current in labels else 0)
        self.bssid.set_sensitive(len(labels) > 1)
        self._refreshing_ssids = guard

    def _selected_bssid(self) -> str | None:
        text = selected_text(self.bssid)
        return None if not text or text == ALL_BSSIDS else text.split(" — ", 1)[0]

    def update_quality(self) -> None:
        """The one-line summary in the sidebar for the chosen network / access point."""
        ssid = selected_text(self.ssid) if self.ssid.get_selected() > 0 else None
        bssid = self._selected_bssid()
        band = BANDS[min(self.band.get_selected(), len(BANDS) - 1)][1]
        if ssid is None and bssid is None:
            self.quality_row.set_title("Choose a network above")
            self.quality_row.set_subtitle("Its coverage and signal across the survey show here.")
            return
        q = analysis.network_quality(self.session.project, ssid, bssid, band)
        what = bssid or ssid
        if not q["points"]:
            self.quality_row.set_title(f"{what}: not heard")
            self.quality_row.set_subtitle(f"None of the {q['total']} points heard it"
                                          + ("" if band == "All" else f" on {band} GHz") + ".")
            return
        self.quality_row.set_title(f"{what}: {q['grade']}")
        snr = "" if q["median_snr"] is None else f", SNR {q['median_snr']:.0f} dB"
        self.quality_row.set_subtitle(
            f"heard at {q['points']}/{q['total']} points, {q['usable'] * 100:.0f}% usable "
            f"(≥‑70 dBm). Signal {q['worst']} to {q['best']} dBm, median {q['median']}{snr}.")

    def compare_with(self, baseline) -> None:
        """Shows how each spot changed since ``baseline``, an earlier survey of this floor plan."""
        project = self.session.project
        if (baseline.width, baseline.height) != (project.width, project.height):
            self.status.set_text(f"{baseline.name} uses a floor plan of a different size "
                                 f"({baseline.width}×{baseline.height}), so the two can't be lined up.")
            return
        self.baseline = baseline
        self.banner.set_title(f"Comparing with {GLib.markup_escape_text(baseline.name)}: green is "
                              "better now, red worse. Hollow circles are its points.")
        self.banner.set_revealed(True)
        self.redraw_heatmap()

    def stop_comparing(self) -> None:
        self.baseline = None
        self.banner.set_revealed(False)
        self.status.set_text("")
        self.redraw_heatmap()

    def toggle_radar(self) -> None:
        """
        Listens on each DFS channel for radar pulse trains, the whole watch split evenly between
        them (and the channels between the HackRFs, when there are several), and keeps what it
        hears with the survey. Stopping keeps what was heard.
        """
        if self._radar_stop is not None:
            self._radar_stop.set()
            self.radar_row.set_subtitle("Stopping…")
            return
        devices = self.session.listening_hackrfs()
        if not devices:
            working = [d for d in self.session.hackrfs if d.alive]
            self.radar_row.set_subtitle(
                "The HackRF is sending the test tone: stop that first." if working else
                "Needs a HackRF connected (Setup → HackRF).")
            return
        stop = threading.Event()
        self._radar_stop = stop
        self.radar_button.set_label("Stop")
        project = self.session.project
        seconds = self.radar_row.get_value() * 60
        channels = hrf.RADAR_CHANNELS
        started = time.time()

        def say(text: str) -> None:
            GLib.idle_add(lambda: (self.radar_row.set_subtitle(text), False)[1])

        def work():
            return hrf.watch_dfs(devices, seconds, stop, say, channels)

        def done(result, error) -> None:
            self._radar_stop = None
            self.radar_button.set_label("Start")
            if error:
                self.radar_row.set_subtitle(f"The radar watch stopped: {error}")
                return
            if result.channels:
                watch = {"time": started, "seconds": round(time.time() - started),
                         "channels": result.channels,
                         "hackrfs": [d.model for d in devices]}
                for key in ("overloaded", "behind"):
                    if getattr(result, key):
                        watch[key] = getattr(result, key)
                project.radar.setdefault("watches", []).append(watch)
                project.radar.setdefault("events", []).extend(result.events)
                self.session.changed()
            hit = sorted({e["channel"] for e in result.events})
            text = (f"Radar on channel{'s' if len(hit) > 1 else ''} {', '.join(map(str, hit))}. "
                    "The audit, channel plan and report now steer clear of them." if hit else
                    f"No radar heard on {len(result.channels)} DFS channels"
                    + (" (stopped early)." if len(result.channels) < len(channels) else "."))
            if result.error:
                text += (f" It stopped {result.error}; what it heard on "
                         f"{len(result.channels)} channels before that is kept.")
            if result.overloaded:
                text += (f" The HackRF overloaded on {', '.join(map(str, result.overloaded))}: "
                         "lower its gain in Setup.")
            if result.behind:
                text += " This computer fell behind the samples, so short pulses may have been missed."
            self.radar_row.set_subtitle(text)

        run_in_thread(work, done)

    def _describe_radar(self) -> None:
        if self._radar_stop is not None:
            return                      # a watch is running and saying how it's going
        radar = self.session.project.radar or {}
        watches, events = radar.get("watches") or [], radar.get("events") or []
        if not watches:
            self.radar_row.set_subtitle("Listens on each DFS channel (52–64, 100–144) for radar "
                                        "pulses. Radar forces access points off a channel.")
            return
        minutes = sum(w.get("seconds", 0) for w in watches) / 60
        hit = sorted({e.get("channel") or e["center_mhz"] for e in events})
        self.radar_row.set_subtitle(
            f"{minutes:.0f} min watched so far: radar on {', '.join(map(str, hit))}." if hit else
            f"{minutes:.0f} min watched so far: no radar heard.")

    def _describe_tone(self) -> None:
        source = self.session.project.tone_source
        self.tone_remove.set_visible(bool(source))
        if not source:
            self.tone_row.set_subtitle("Place where a HackRF will send a steady tone, then measure "
                                       "around it: the tone's fade measures each wall.")
            return
        heard = len(analysis.tone_samples(self.session.project))
        self.tone_row.set_subtitle(f"{source['freq_mhz']:.0f} MHz, heard at {heard} point"
                                   f"{'' if heard == 1 else 's'}. Each new point listens for it.")

    def _remove_tone(self) -> None:
        self.session.project.tone_source = None
        self._describe_tone()
        self.session.changed()

    def show_wall_fit(self) -> None:
        project = self.session.project
        fit = analysis.fit_walls(project)
        if fit is None or not fit["walls"]:
            _show_list(self, "Wall losses from the tone", [],
                       "Place the test tone source, start the tone, and measure at 5 or more "
                       "points, some on the far side of the walls to be measured."
                       if fit is None else "No marked wall lies between the tone and a point "
                                           "that heard it. Draw the walls first.")
            return
        rows = []
        for wall in fit["walls"]:
            item = project.walls[wall["index"]]
            rows.append(("Wall", f"{item.material} #{wall['index'] + 1}: {wall['before']:.0f} → "
                                 f"{wall['measured']:.1f} dB",
                         f"Crossed by {wall['paths']} path{'s' if wall['paths'] != 1 else ''}."
                         + (" Few paths: rough." if wall["paths"] < 3 else "")))
        summary = (f"Exponent {fit['exponent']:.1f}" + (" (held)" if fit["exponent_held"] else "")
                   + f", {fit['rms']:.1f} dB left unexplained, from {fit['points']} points.")
        rows.append(("Fit", summary, "Apply sets each wall to its measured loss. The fit can be "
                                     "run again after measuring more points."))

        def apply() -> None:
            for wall in fit["walls"]:
                project.walls[wall["index"]].db = round(wall["measured"], 1)
            self.session.changed()
            self.redraw_heatmap()
            self.status.set_text(f"{len(fit['walls'])} walls set to their measured losses.")

        _show_list(self, "Wall losses from the tone", rows, "", action=("Apply", apply))

    # ---------- the timeline ----------

    def _timeline_bar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["osd", "toolbar"],
                      margin_start=12, margin_end=12, margin_top=12)
        top = Gtk.Box(spacing=8)
        self.timeline_label = Gtk.Label(xalign=0, wrap=True, hexpand=True, max_width_chars=90)
        top.append(self.timeline_label)
        modes = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.START)
        self.timeline_change = Gtk.ToggleButton(label="Change since then", active=True)
        self.timeline_then = Gtk.ToggleButton(label="As it was", group=self.timeline_change)
        for button in (self.timeline_change, self.timeline_then):
            button.connect("toggled", lambda b: b.get_active() and self._timeline_show())
            modes.append(button)
        top.append(modes)
        box.append(top)
        self.timeline_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, draw_value=False,
                                        hexpand=True, margin_start=24, margin_end=24)
        self.timeline_scale.connect("value-changed", self._timeline_moved)
        box.append(self.timeline_scale)
        self.timeline_reveal = Gtk.Revealer(child=box, reveal_child=False, valign=Gtk.Align.START,
                                            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        return self.timeline_reveal

    def _timeline_toggled(self, button: Gtk.ToggleButton) -> None:
        if not button.get_active():
            self._leave_timeline()
            return
        self.timeline_reveal.set_reveal_child(True)
        self.timeline_scale.set_sensitive(False)
        self.timeline_label.set_text("Finding earlier surveys of this site…")
        project = self.session.project
        ssid, band = self._filters()
        profile = self._profile_key()

        def work():
            found = []
            for info in library.same_site(project):
                try:
                    other = Project.load(info.path)
                except Exception:
                    continue
                found.append((info.surveyed_at, other, history.figures(other, profile, ssid, band)))
            mine = history.figures(project, profile, ssid, band)
            found.append((mine["when"] or time.time(), None, mine))
            return sorted(found, key=lambda item: item[0])

        def done(found, error) -> None:
            if not self.timeline_button.get_active() or project is not self.session.project:
                return
            if error or found is None or len(found) < 2:
                self.timeline_label.set_text(
                    f"Couldn't look: {error}" if error else
                    "No other surveys of this site yet. Next time, start with Main menu → Start a "
                    "follow-up survey, so it lines up here. Surveys of the same floor plan count too.")
                return
            self._timeline = found
            scale = self.timeline_scale
            scale.clear_marks()
            scale.set_range(0, len(found) - 1)
            scale.set_increments(1, 1)
            span_years = time.localtime(found[0][0]).tm_year != time.localtime(found[-1][0]).tm_year
            for index, (when, other, _figures) in enumerate(found):
                label = "This survey" if other is None else time.strftime(
                    "%b %Y" if span_years else "%d %b", time.localtime(when))
                scale.add_mark(index, Gtk.PositionType.BOTTOM, label)
            scale.set_sensitive(True)
            here = next(i for i, item in enumerate(found) if item[1] is None)
            # Start on the survey before this one: that's the change people come to see
            start = here - 1 if here > 0 else here + 1
            scale.set_value(start)
            self._timeline_show()

        run_in_thread(work, done)

    def _timeline_moved(self, scale: Gtk.Scale) -> None:
        index = round(scale.get_value())
        if abs(scale.get_value() - index) > 1e-6:
            scale.set_value(index)            # stops on a survey, never between two
            return
        self._timeline_show()

    def _timeline_show(self) -> None:
        if not self._timeline:
            return
        index = min(len(self._timeline) - 1, round(self.timeline_scale.get_value()))
        when, other, figures = self._timeline[index]
        mine = next(item for item in self._timeline if item[1] is None)
        if other is None:
            self.viewing = self.baseline = None
            self.timeline_label.set_text(
                f"This survey, {library.when(when, False)}: {figures['points']} points. Move the "
                "slider to an earlier survey to see what changed.")
        elif self.timeline_then.get_active():
            self.baseline = None
            self.viewing = other
            meets = f", {figures['meets']:.0f}% meet {figures['requirement']}" if figures["meets"] is not None else ""
            signal = f", median signal {figures['signal']:.0f} dBm" if figures["signal"] is not None else ""
            self.timeline_label.set_text(
                f"As it was on {library.when(when, False)} ({history.span(mine[0] - when)} "
                f"{'before' if when <= mine[0] else 'after'} this survey): {figures['points']} points"
                f"{signal}{meets}. Its map, for the measure chosen under Map.")
        else:
            self.viewing = None
            self.baseline = other
            earlier, later = (figures, mine[2]) if when <= mine[0] else (mine[2], figures)
            self.timeline_label.set_text(
                f"{'Since' if when <= mine[0] else 'Before'} {library.when(when, False)} "
                f"({history.span(mine[0] - when)}): {history.describe_change(earlier, later)} "
                "On the map: green got better, red worse.")
        self.redraw_heatmap()
        self.area.queue_draw()

    def _leave_timeline(self) -> None:
        was = self.viewing is not None or (self._timeline and self.baseline is not None)
        self.timeline_reveal.set_reveal_child(False)
        if self._timeline:
            self.baseline = None
        self.viewing = None
        self._timeline = []
        if was:
            self.redraw_heatmap()

    def history_for_report(self, finished) -> None:
        """
        For the report's History section: every survey of this site with its figures, oldest
        first, a small signal map of each (the latest six), and the survey just before this
        one, which the report's change maps compare against when nothing else is chosen.
        """
        project = self.session.project
        ssid, band = self._filters()
        profile = self._profile_key()

        def work():
            surveys = []
            for info in library.same_site(project):
                try:
                    other = Project.load(info.path)
                except Exception:
                    continue
                surveys.append((other, history.figures(other, profile, ssid, band)))
            mine = history.figures(project, profile, ssid, band)
            surveys.append((None, {**mine, "this": True}))
            surveys.sort(key=lambda item: item[1]["when"] or time.time())
            overlays = []
            for other, figures in surveys[-6:]:
                shown = other or project
                overlay, markers, *_rest = _compute_map(shown, "signal", ssid, band, None, False,
                                                        False, profile, None, False, False, False)
                overlays.append((figures["when"], other, overlay, markers))
            return surveys, overlays

        def done(result, error) -> None:
            if error or result is None:
                finished({"rows": [], "maps": [], "previous": None, "error": str(error)})
                return
            surveys, overlays = result
            maps = []
            kept = self.viewing
            try:
                for when, other, overlay, markers in overlays:
                    if overlay is None:
                        continue
                    self.viewing = other
                    png = self.map_png(700, overlay, markers, rings=False)
                    if png:
                        maps.append((when, png))
            finally:
                self.viewing = kept
            here = next(i for i, (other, _f) in enumerate(surveys) if other is None)
            previous = surveys[here - 1][0] if here > 0 else None
            finished({"rows": [figures for _other, figures in surveys] if len(surveys) > 1 else [],
                      "maps": maps if len(surveys) > 1 else [], "previous": previous})

        run_in_thread(work, done)

    # ---------- the wall test, guided ----------

    def _can_hear_tone(self) -> bool:
        serial, device = self.session.serial_analyzer, self.session.hackrf
        return bool((serial is not None and serial.alive)
                    or (device is not None and device.alive and not device.sending))

    def _update_guide(self) -> None:
        if not settings.get("easy_mode", False):
            self.guide.set_revealed(False)
            return
        step = tonetest.next_step(self.session.project, self._can_hear_tone())
        self._guide_step = step
        self.guide.set_title(f"Wall test, step {step.number} of 4: {step.text}")
        self.guide.set_button_label({"setup": "How to set it up", "fit": "Fit the walls"}.get(step.action))
        self.guide.set_revealed(True)

    def _guide_action(self) -> None:
        action = self._guide_step.action if self._guide_step else None
        if action == "setup":
            self.show_tone_setup()
        elif action == "fit":
            self.show_wall_fit()

    def show_tone_setup(self) -> None:
        """How to run the tone with what's connected to this tablet, in steps."""
        project = self.session.project
        source = project.tone_source
        freq = source["freq_mhz"] if source else list(hrf.TONES.values())[self.tone_choice.get_selected()][0]
        working = [d for d in self.session.hackrfs if d.alive]
        device = working[0] if working else None
        serial = self.session.serial_analyzer if self.session.serial_analyzer is not None \
            and self.session.serial_analyzer.alive else None
        many = len(working) > 1
        guide = tonetest.setup(freq, (device.model if many else device.name) if device else None,
                               (getattr(serial, "name", None) or "analyzer") if serial else None,
                               working[1].model if many else None)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18,
                       margin_end=18, margin_top=6, margin_bottom=18)
        body.append(Gtk.Label(label=guide.title, xalign=0, wrap=True, css_classes=["title-4"]))
        for number, step in enumerate(guide.steps, start=1):
            text, _sep, command_line = step.partition(guide.command) if guide.command in step \
                else (step, "", "")
            row = Gtk.Box(spacing=10)
            row.append(Gtk.Label(label=f"{number}", valign=Gtk.Align.START, width_chars=2,
                                 css_classes=["heading", "accent"]))
            words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
            words.append(Gtk.Label(label=text.strip(), xalign=0, wrap=True, max_width_chars=60))
            if guide.command in step:
                line = Gtk.Box(spacing=6)
                code = Gtk.Label(label=guide.command, xalign=0, wrap=True, selectable=True,
                                 hexpand=True, css_classes=["monospace", "card"])
                line.append(code)
                copy_button = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                                         tooltip_text="Copy the command")
                copy_button.connect("clicked", lambda *_: self._copy(guide.command))
                line.append(copy_button)
                words.append(line)
                if command_line.strip():
                    words.append(Gtk.Label(label=command_line.strip(), xalign=0, wrap=True,
                                           max_width_chars=60))
            row.append(words)
            body.append(row)
        status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        body.append(status)

        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=body, propagate_natural_height=True, max_content_height=600,
            hscrollbar_policy=Gtk.PolicyType.NEVER))
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        dialog = Adw.Dialog(title="Setting up the tone", child=view, content_width=600)
        if guide.start_here and device is not None:
            start = Gtk.Button(label="Stop the tone" if device.sending else "Start the tone here",
                               css_classes=["suggested-action"])

            def toggle(*_args) -> None:
                if device.sending:
                    device.stop_tone()
                    start.set_label("Start the tone here")
                    status.set_text("Stopped.")
                    self._update_guide()
                    return
                start.set_sensitive(False)
                status.set_text("Starting…")

                def done(_result, error) -> None:
                    start.set_sensitive(True)
                    if error:
                        status.set_text(f"Couldn't start it: {error}")
                        return
                    start.set_label("Stop the tone")
                    status.set_text(f"Sending {freq:.0f} MHz. It stops by itself after "
                                    f"{hrf.TONE_MINUTES} minutes.")
                    self._update_guide()

                run_in_thread(lambda: device.start_tone(freq, 10), done)

            start.connect("clicked", toggle)
            header.pack_start(start)
        dialog.present(self.get_root())

    def _copy(self, text: str) -> None:
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_bytes(
            "text/plain;charset=utf-8", GLib.Bytes.new(text.encode())))
        self.status.set_text("Command copied.")

    def show_audit(self) -> None:
        findings = rfplan.audit(self.session.project)
        rows = [(f.severity, f.title, f.detail) for f in findings]
        _show_list(self, "Configuration audit", rows,
                   "Nothing to flag." if self.session.project.points else "Measure some points first.")

    def show_plan(self) -> None:
        ssid = selected_text(self.ssid) if self.ssid.get_selected() > 0 else None
        if ssid is None:
            _show_list(self, "Channel plan", [], "Choose your network under Heatmap → Network "
                                                 "first: the plan moves its radios, not anyone else's.")
            return
        plan = rfplan.channel_plan(self.session.project, ssid, self.allow_dfs.get_active())
        rows = []
        for item in plan:
            radio = item.radio
            where = f"{BAND_NAMES.get(radio.band, radio.band)} radio, best {radio.rssi} dBm"
            macs = ", ".join(sorted(b.upper() for b in radio.bssids)[:2])
            if item.changed:
                rows.append(("Change", f"{item.current} → {item.suggested}",
                             f"{where} ({macs}). Shares its channel with {item.shared_before} of "
                             f"the network's radios now, {item.shared_after} after."))
            else:
                rows.append(("Keep", item.current,
                             f"{where} ({macs}). Shares with {item.shared_after} of the network's radios."))
        _show_list(self, f"Channel plan for {ssid}", rows,
                   f"{ssid} wasn't heard at any point.")

    def report_choices(self) -> tuple[str, str | None, str, bool]:
        """What the report should judge by: as chosen on this page."""
        ssid = selected_text(self.ssid) if self.ssid.get_selected() > 0 else None
        band = BANDS[min(self.band.get_selected(), len(BANDS) - 1)][1]
        return self._profile_key(), ssid, band, self.allow_dfs.get_active()

    def _requirement_changed(self, *_args) -> None:
        self.session.profile_key = self._profile_key()     # the Discover page grades by it too
        self.redraw_heatmap()

    def _profile_key(self) -> str:
        return list(rfplan.PROFILES)[min(self.requirement.get_selected(), len(rfplan.PROFILES) - 1)]

    def _metric_key(self) -> str:
        return list(analysis.METRICS)[self.metric.get_selected()]

    def _selected_channel(self) -> int | None:
        text = selected_text(self.channel)
        return int(text.split()[-1]) if text.startswith("Channel ") else None

    def redraw_heatmap(self) -> None:
        """
        Works the map out on a worker thread from a snapshot of the survey, so large plans and
        source location never freeze the window. A newer request makes an older result stale.
        """
        project = self.shown()
        self._render_generation += 1
        generation = self._render_generation
        if not project.points:
            self.overlay = None
            self.markers = []
            self.sites = []
            self.noises = []
            self.bt = []
            self.bt_other = {"fixed": [], "rotating": []}
            self.clients = []
            self.tags = []
            self.area.queue_draw()
            return

        key = self._metric_key()
        self.cellular_band.set_visible(key == "cellular")
        picked = self.cellular_band.get_selected()
        cellular_band = cell.BANDS[picked - 1][0] if 0 < picked <= len(cell.BANDS) else None
        self.other_band.set_visible(key == "bands")
        picked = self.other_band.get_selected()
        other_band = rfbands.BANDS[picked - 1].key if 0 < picked <= len(rfbands.BANDS) else None
        chosen = self.ssid.get_selected()
        ssid = None if chosen <= 0 else self.ssid.get_model().get_string(chosen)
        bssid = self._selected_bssid()
        band = BANDS[min(self.band.get_selected(), len(BANDS) - 1)][1]
        channel = self._selected_channel()
        show_aps = self.show_aps.get_active()
        show_noise = self.show_noise.get_active()
        show_bt = self.show_bt.get_active()
        show_clients = self.show_clients.get_active()
        show_tags = self.show_tags.get_active()
        profile = self._profile_key()
        focus = set(project.focus_networks or [])
        ap_networks = focus if focus and self.ap_scope.get_selected() == 1 else None

        snapshot = copy.copy(project)
        snapshot.points = list(project.points)
        snapshot.walls = list(project.walls)
        baseline = self.baseline

        def work():
            result = _compute_map(snapshot, key, ssid, band, channel, show_aps, show_noise,
                                  profile, bssid, show_bt, show_clients, show_tags, cellular_band,
                                  other_band, ap_networks)
            if baseline is None or key not in CHANGE_SCALE:
                return result, None
            return result, _compare(snapshot, baseline, key, ssid, band, channel, profile, bssid)

        def done(result, error) -> None:
            if generation != self._render_generation:
                return                          # something newer was asked for meanwhile
            if error:
                self.status.set_text(f"Couldn't draw the map: {error}")
                return
            (self.overlay, self.markers, self.sites, self.noises, self.bt,
             self.bt_other, self.clients, self.tags), compared = result
            if baseline is None:
                title = analysis.METRICS[key][0]
                if self.overlay is None and self.session.project.points:
                    self._map_message = _no_map_reason(title, key, bssid or ssid)
                    self.status.set_text(self._map_message)
                elif self.status.get_text() == self._map_message:
                    # Only its own message: a measurement's summary stays until the next one
                    self.status.set_text("")
            if baseline is not None:
                title = analysis.METRICS[key][0]
                if compared is None:
                    self.status.set_text(f"{title} can't be compared: showing this survey only.")
                else:
                    self.overlay, stats = compared
                    unit = analysis.METRICS[key][1]
                    if stats["cells"]:
                        self.status.set_text(
                            f"{title}: {stats['better'] * 100:.0f}% of the area better, "
                            f"{stats['worse'] * 100:.0f}% worse, average change "
                            f"{stats['average']:+.1f} {unit} (positive is better).")
                    else:
                        self.status.set_text("The two surveys don't measure the same areas.")
            self.area.queue_draw()

        run_in_thread(work, done)

    def shown(self) -> Project:
        """The survey on the map: an earlier one picked on the timeline, or this one."""
        return self.viewing or self.session.project

    def draw(self, _area, cr: cairo.Context, width: int, height: int) -> None:
        project = self.shown()
        cr.set_source_rgb(0.90, 0.91, 0.93)
        cr.paint()

        cr.save()
        cr.translate(*self.offset)
        cr.scale(self.scale, self.scale)

        if self.plan:
            cr.set_source_surface(self.plan, 0, 0)
            cr.paint()
        else:
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(0, 0, project.width, project.height)
            cr.fill()
            cr.set_source_rgb(0.89, 0.91, 0.94)
            cr.set_line_width(1)
            for x in range(0, project.width, 50):
                cr.move_to(x, 0)
                cr.line_to(x, project.height)
            for y in range(0, project.height, 50):
                cr.move_to(0, y)
                cr.line_to(project.width, y)
            cr.stroke()

        if self.overlay:
            cr.set_source_surface(self.overlay, 0, 0)
            cr.paint()

        for wall in project.walls:
            cr.set_source_rgba(0.28, 0.33, 0.41, 0.75)
            cr.set_line_width(4 / self.scale)
            cr.move_to(wall.x1, wall.y1)
            cr.line_to(wall.x2, wall.y2)
            cr.stroke()

        if self.baseline is not None:
            cr.set_line_width(2 / self.scale)
            for point in self.baseline.points:
                cr.new_path()
                cr.arc(point.x, point.y, 7 / self.scale, 0, 2 * math.pi)
                cr.set_source_rgba(0.35, 0.38, 0.44, 0.9)
                cr.stroke()
        rings = self._ring_points()
        for index, (x, y, colour) in enumerate(self.markers):
            if index in rings:
                continue                  # drawn as the middle of its ring, below
            cr.set_source_rgb(*colour)
            cr.arc(x, y, 9 / self.scale, 0, 2 * math.pi)
            cr.fill_preserve()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(2 / self.scale)
            cr.stroke()

        # How far off each estimate could be: a faint dashed ring, not a false pinpoint
        cr.set_dash([6 / self.scale, 5 / self.scale])
        cr.set_line_width(1.5 / self.scale)
        for site in self.sites:
            if site["outside"] and not site.get("beyond"):
                continue                  # a fit at the plan's edge has no meaningful ring
            cr.set_source_rgba(0.15, 0.45, 0.90, 0.45)
            cr.arc(site["x"], site["y"], max(site["radius"], 14 / self.scale), 0, 2 * math.pi)
            cr.stroke()
        for noise in self.noises:
            located = noise["located"]
            if located and not located["outside"]:
                cr.set_source_rgba(0.93, 0.33, 0.13, 0.45)
                cr.arc(located["x"], located["y"], max(located["radius"], 14 / self.scale), 0, 2 * math.pi)
                cr.stroke()
        cr.set_dash([])

        if self._rubber:
            (x1, y1), (x2, y2) = self._rubber
            cr.set_source_rgba(0.15, 0.39, 0.92, 0.9)
            cr.set_line_width(2 / self.scale)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()

        if self._ruler:
            cr.set_source_rgba(0.86, 0.15, 0.55, 0.95)
            cr.set_line_width(3 / self.scale)
            cr.move_to(*self._ruler[0])
            for end in self._ruler[1:]:
                cr.line_to(*end)
            cr.stroke()
            for end in self._ruler:
                cr.arc(end[0], end[1], 7 / self.scale, 0, 2 * math.pi)
                cr.fill()

        if self.busy and self.pending:
            x, y = self.pending
            cr.set_source_rgb(0.11, 0.31, 0.85)
            cr.set_line_width(3 / self.scale)
            cr.arc(x, y, 22 / self.scale, 0, 2 * math.pi)
            cr.stroke()

        cr.restore()

        # Spectrum rings, 10 ft across once the plan has a scale
        self._ap_hits = []
        outer = _ring_radius(project) * self.scale
        for index in rings:
            point = project.points[index]
            sx, sy = self._to_screen(point.x, point.y)
            if -outer <= sx <= width + outer and -outer <= sy <= height + outer:
                self._ap_hits += draw_ring(cr, sx, sy, outer, point, self.markers[index][2],
                                           BAND_COLOURS)

        # A warning badge on points whose connection had trouble
        for point in project.points:
            if any(connectivity.problem(check) for check in point.checks_made()):
                sx, sy = self._to_screen(point.x, point.y)
                _draw_badge(cr, sx + 9, sy - 9)

        # Each point's number, as the report and the details call it. The report pins its own.
        if not self._for_report:
            for number, point in enumerate(project.points, start=1):
                sx, sy = self._to_screen(point.x, point.y)
                if -20 <= sx <= width + 20 and -20 <= sy <= height + 20:
                    _draw_number(cr, sx + 9, sy + 9, number)
            _draw_scale_bar(cr, project, self.scale, height)

        # Icons and their labels in screen pixels, so they stay small and sharp at any zoom
        self._icon_hits = []
        for index, noise in enumerate(self.noises):
            located = noise["located"]
            if not located:
                continue
            sx, sy = self._to_screen(located["x"], located["y"])
            _draw_noise_icon(cr, sx, sy, located["rough"])
            lines = [(noise["name"] + (" (rough)" if located["rough"] else ""), None),
                     (_noise_detail(noise), None)]
            _draw_label(cr, sx + 16, sy, lines)
            self._icon_hits.append((sx, sy, "noise", index))
        for index, site in enumerate(self.sites):
            sx, sy = self._to_screen(site["x"], site["y"])
            beyond = site.get("beyond")
            if beyond and not site.get("placed"):
                # Off the property: a dashed line from the plan's nearest edge out to it
                ex, ey = self._to_screen(beyond["edge_x"], beyond["edge_y"])
                cr.save()
                cr.set_dash([5, 4])
                cr.set_line_width(1.5)
                cr.set_source_rgba(0.15, 0.45, 0.90, 0.8)
                cr.move_to(ex, ey)
                cr.line_to(sx, sy)
                cr.stroke()
                cr.restore()
            _draw_site(cr, sx, sy, site["rough"], site.get("placed", False))
            lines = [(site["name"], None)] if site.get("name") else []
            lines += [(name, bands) for name, bands in site["networks"][:3]]
            if len(site["networks"]) > 3:
                lines.append((f"+{len(site['networks']) - 3} more", None))
            if beyond and not site.get("placed"):
                lines.append((_beyond_text(beyond), None))
            _draw_label(cr, sx + 18, sy, lines)
            self._icon_hits.append((sx, sy, "site", index))
        for index, watch in enumerate(project.watches):
            if watch.get("x") is None:
                continue
            sx, sy = self._to_screen(watch["x"], watch["y"])
            _draw_watch(cr, sx, sy, _watch_colour(watch))
            _draw_label(cr, sx + 18, sy, [(watchreport.headline(watch), None)])
            self._icon_hits.append((sx, sy, "watch", index))
        for index, device in enumerate(self.bt):
            located = device["located"]
            if not located:
                continue
            sx, sy = self._to_screen(located["x"], located["y"])
            _draw_bt_icon(cr, sx, sy, device["kind"], located["rough"])
            label = device["name"] or device["vendor"] or device["detail"] or "Bluetooth device"
            lines = [(label + (" (rough)" if located["rough"] else ""), None),
                     (_bt_detail(device), None)]
            _draw_label(cr, sx + 16, sy, lines)
            self._icon_hits.append((sx, sy, "bt", index))
        for index, device in enumerate(self.bt_other.get("fixed", [])):
            if not device.get("near"):
                continue
            sx, sy = self._to_screen(*device["near"])
            _draw_bt_icon(cr, sx, sy, device.get("kind", "unknown"), rough=True)
            self._icon_hits.append((sx, sy, "bt_near", index))
        # Clients only known to be near a point are fanned round it so each can be tapped
        fanned: dict[int, int] = {}
        for index, client in enumerate(self.clients):
            located = client["located"]
            sx, sy = self._to_screen(located["x"], located["y"])
            if located.get("near"):
                slot = fanned[client["near_point"]] = fanned.get(client["near_point"], -1) + 1
                ring = 30 + 14 * (slot // 10)
                angle = slot * 2 * math.pi / 10 + math.pi / 2
                sx, sy = sx + ring * math.cos(angle), sy + ring * math.sin(angle)
            _draw_client_icon(cr, sx, sy, client["best"], located["rough"])
            if not located.get("near"):
                _draw_label(cr, sx + 14, sy, [(_client_name(client)
                                               + (" (rough)" if located["rough"] else ""), None)])
            self._icon_hits.append((sx, sy, "client", index))
        # Finder tags fan out round their point on the other side from clients
        fanned_tags: dict[int, int] = {}
        for index, tag in enumerate(self.tags):
            located = tag["located"]
            sx, sy = self._to_screen(located["x"], located["y"])
            if located.get("near"):
                slot = fanned_tags[tag["near_point"]] = fanned_tags.get(tag["near_point"], -1) + 1
                angle = -math.pi / 2 - slot * 2 * math.pi / 8
                ring = 34 + 14 * (slot // 8)
                sx, sy = sx + ring * math.cos(angle), sy + ring * math.sin(angle)
            _draw_tag_icon(cr, sx, sy, bool(tag["away"]), located["rough"])
            words = tag["label"] + (", away from owner" if tag["away"] else "")
            _draw_label(cr, sx + 15, sy, [(words + (" (rough)" if located["rough"] else ""), None)])
            self._icon_hits.append((sx, sy, "tag", index))
        if self.me is not None and self.show_me.get_active() and not self._for_report:
            sx, sy = self._to_screen(self.me.x, self.me.y)
            _draw_me(cr, sx, sy, self.me.radius * self.scale, self.me.rough)
            lines = [("You" + (" (rough)" if self.me.rough else ""), None)]
            spread = _feet(project, self.me.radius)
            lines.append((f"±{spread:.0f} ft · {self.me.aps} access points" if spread
                          else f"{self.me.aps} access points", None))
            _draw_label(cr, sx + 16, sy, lines)
        for index, ap in enumerate(project.virtual_aps):
            sx, sy = self._to_screen(ap["x"], ap["y"])
            _draw_virtual(cr, sx, sy)
            _draw_label(cr, sx + 18, sy, [(ap.get("name") or "Planned AP", None),
                                          ("planned", ap.get("bands"))])
            self._icon_hits.append((sx, sy, "virtual", index))
        if project.tone_source:
            sx, sy = self._to_screen(project.tone_source["x"], project.tone_source["y"])
            _draw_tone(cr, sx, sy)
            heard = len(analysis.tone_samples(project))
            _draw_label(cr, sx + 16, sy, [("Test tone", None),
                                          (f"{project.tone_source['freq_mhz']:.0f} MHz · heard at {heard}", None)])

    def map_png(self, longest: int = 2000, overlay=None, markers=None,
                rings: bool = True) -> bytes | None:
        """
        The whole plan as the map shows it now, heatmap, rings and icons, for the report, which
        pins its own clickable point numbers and scale bar on top.
        """
        project = self.shown()
        if not project.points and not self.plan:
            return None
        scale = min(1.0, longest / max(project.width, project.height, 1))
        width, height = max(1, round(project.width * scale)), max(1, round(project.height * scale))
        kept = (self.scale, list(self.offset), self._ap_hits, getattr(self, "_icon_hits", []),
                self._ruler, self.overlay, self.markers)
        self.scale, self.offset, self._ruler, self._for_report = scale, [0.0, 0.0], [], True
        self._rings_off = not rings
        if overlay is not None:
            self.overlay, self.markers = overlay, markers if markers is not None else self.markers
        try:
            surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
            self.draw(None, cairo.Context(surface), width, height)
        finally:
            (self.scale, self.offset, self._ap_hits, self._icon_hits, self._ruler, self.overlay,
             self.markers) = kept
            self._for_report = self._rings_off = False
        out = io.BytesIO()
        surface.write_to_png(out)
        return out.getvalue()

    REPORT_MAPS = ("signal", "snr", "secondary", "contention", "throughput", "predicted", "bands")
    COMPARED = ("signal", "snr", "secondary")

    def report_maps(self, finished, baseline=None) -> None:
        """
        The extra maps the report shows beside the main one, for the network and band chosen
        here, and how each compares with the survey being compared against, if any. The maps
        are worked out on a worker thread and drawn here; ``finished`` gets
        {"maps": [(key, title, png)], "compared": [(key, title, stats, png)], "baseline": name}.
        """
        project = self.session.project
        ssid, band = self._filters()
        bssid, channel, profile = self._selected_bssid(), self._selected_channel(), self._profile_key()
        snapshot = copy.copy(project)
        snapshot.points = list(project.points)
        # What the map is comparing with, else the survey the timeline found just before this
        baseline = (self.baseline if self.viewing is None and self.baseline is not None
                    else baseline)
        shown = self._metric_key()

        def work():
            maps = []
            for key in self.REPORT_MAPS:
                if key == shown or (key == "predicted" and not snapshot.virtual_aps):
                    continue
                overlay, markers, *_rest = _compute_map(snapshot, key, ssid, band, channel,
                                                        False, False, profile, bssid, False)
                if overlay is not None:
                    maps.append((key, overlay, markers))
            compared = []
            if baseline is not None:
                for key in self.COMPARED:
                    overlay, stats = _compare(snapshot, baseline, key, ssid, band, channel,
                                              profile, bssid)
                    compared.append((key, overlay, stats))
            return maps, compared

        def done(result, error) -> None:
            if error:
                finished({"maps": [], "compared": [], "error": str(error)})
                return
            maps, compared = result
            finished({
                "maps": [(key, analysis.METRICS[key][0],
                          self.map_png(1400, overlay, markers, rings=False))
                         for key, overlay, markers in maps],
                "compared": [(key, analysis.METRICS[key][0], stats,
                              self.map_png(1400, overlay, None, rings=False) if overlay else None)
                             for key, overlay, stats in compared],
                "baseline": baseline.name if baseline is not None else None,
                "shown": analysis.METRICS[shown][0],
            })

        run_in_thread(work, done)

    def _ring_points(self) -> set[int]:
        """The points drawn as spectrum rings: those with a sweep, once the map has their colour."""
        if not self.show_rings.get_active() or self._rings_off:
            return set()
        if _ring_radius(self.shown()) * self.scale < MIN_RADIUS:
            return set()                  # zoomed too far out to read: plain markers
        points = self.shown().points
        return {index for index in range(min(len(points), len(self.markers)))
                if points[index].spectrum and points[index].spectrum.get("segments")}

    def open_site_map(self) -> None:
        from .site_map import open_site_map

        open_site_map(self)

    def choose_plan(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Choose a floor plan")
        images = Gtk.FileFilter()
        images.set_name("Images")
        images.add_pixbuf_formats()
        dialog.set_default_filter(images)
        dialog.open(self.get_root(), None, self._plan_chosen)

    def _plan_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except Exception:
            return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            with open(path, "rb") as handle:
                self.session.project.set_plan(handle.read(), pixbuf.get_width(), pixbuf.get_height())
        except Exception as error:
            self.status.set_text(f"Couldn't open that image: {error}")
            return
        had_scale = self.session.project.meters_per_pixel is not None
        self.session.project.meters_per_pixel = None      # the old scale belongs to the old image
        self.session.project.geo = {}                     # and so does its place on the map
        self.reload()
        self.session.changed()
        self.status.set_text("New floor plan. Use Set scale to give it its size in feet."
                             if had_scale or self.session.project.points else
                             "Use Set scale to give the plan its size in feet.")


def _wifi_choice() -> list[int]:
    """The Wi-Fi ranges ticked on the Spectrum page (none is a choice)."""
    return rangechoice.setup_choice()[0]


OTHER_SWEEPS = 2           # sweeps of each range beyond Wi-Fi at a survey point, peak-held


def measure_other_ranges(session, bands: list, extras: list, say,
                         sweeps: int = OTHER_SWEEPS) -> tuple[dict | None, list[dict], list[str]]:
    """
    The ranges beyond Wi-Fi ticked on the Spectrum page, at a survey point, shared between
    the analyzers that reach them and swept at once. Returns ({band key: rf.bands summary} for
    the bands of rf.bands, every range's trace for the report's pictures, notes).
    """
    pool = session.band_analyzers()
    if not pool:
        return None, [], ["Other ranges weren't measured: no RTL-SDR, HackRF, tinySA or RF "
                          "Explorer here."]
    # Ranges that aren't bands of rf.bands are swept the same way, under a key of their own
    targets = list(bands) + [rfbands.Band(f"range:{low:g}-{high:g}", name, float(low), float(high), ())
                             for name, low, high in extras]
    say("rf", f"Sweeping {len(targets)} other ranges…")
    found: dict = {}
    traces: list[dict] = []
    notes = [f"{b.name}: nothing here reaches it." for b in rfbands.out_of_reach(targets, pool)]
    lock = threading.Lock()

    def run(analyzer, mine) -> None:
        name_of = analyzer_label(analyzer)
        with analyzer.lock:
            for target in mine:
                say("rf", f"{name_of}: {target.name}…")
                try:
                    trace = analyzer.scan(target.low, target.high, rfbands.resolution_khz(target), sweeps)
                except Exception as error:
                    with lock:
                        notes.append(f"{target.name}: {error}")
                    continue
                record = range_record(target.name, trace, name_of)
                summary = (rfbands.summarize(trace.freqs, trace.peak, [target], trace.rbw_khz)
                           if target.key in rfbands.BY_KEY else {})
                with lock:
                    if record:
                        traces.append(record)
                    for key, entry in summary.items():
                        entry["analyzer"] = getattr(analyzer, "model", "") or analyzer.name
                        found[key] = entry

    threads = [threading.Thread(target=run, args=job, daemon=True)
               for job in rfbands.share_bands(targets, pool)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    busiest = max(((rfbands.activity(e) or 0.0, k) for k, e in found.items()), default=None)
    if busiest:
        say("rf", f"Other ranges: busiest {rfbands.BY_KEY[busiest[1]].name}, "
                  f"{busiest[0]:.0f} dB over its floor")
    return found or None, sorted(traces, key=lambda r: r["start_mhz"]), notes


def range_record(name: str, trace, analyzer: str) -> dict | None:
    """
    A range's sweep as a survey point keeps it, for the report's picture: evenly spaced bins
    from start to stop, average and peak-held levels to a tenth of a dB.
    """
    freqs = list(trace.freqs)
    if len(freqs) < 2:
        return None
    return {"name": name, "start_mhz": round(float(freqs[0]), 4), "stop_mhz": round(float(freqs[-1]), 4),
            "rbw_khz": round(float(trace.rbw_khz), 1), "analyzer": analyzer,
            "avg": [round(float(v), 1) for v in trace.avg],
            "max": [round(float(v), 1) for v in trace.peak]}


def _compute_map(project, key: str, ssid: str | None, band: str, channel: int | None,
                 show_aps: bool = True, show_noise: bool = True, profile: str = "data",
                 bssid: str | None = None, show_bt: bool = True, show_clients: bool = True,
                 show_tags: bool = True, cellular_band: str | None = None,
                 other_band: str | None = None, ap_networks: set[str] | None = None):
    _title, _unit, low, high, higher_is_better = analysis.METRICS[key]
    context = analysis.Context(project, profile=profile)
    context.cellular_band = cellular_band
    context.other_band = other_band
    samples = []
    markers = []
    for point in project.points:
        value = analysis.metric(point, key, context, ssid=ssid, bssid=bssid, band=band,
                                channel=channel)
        markers.append((point.x, point.y, _marker_colour(key, value, low, high, higher_is_better)))
        if value is not None:
            samples.append((point.x, point.y, value))

    radius = _heat_radius(project)
    sites = (analysis.ap_sites(project, ssid, band, networks=ap_networks)
             if (show_aps or key == "predicted") else [])
    if key == "predicted":
        overlay = planning.render_predicted(project, context, ssid, band, sites)
    elif key in ("verdict", "requirement"):
        colours = ([colour for _, _, colour in analysis.VERDICTS] if key == "verdict"
                   else analysis.REQUIREMENT_COLOURS)
        overlay = heatmap.categorical(project.width, project.height, samples, colours, radius)
    else:
        overlay = heatmap.render(project.width, project.height, samples, low, high,
                                 higher_is_better, radius)
    sites = sites if show_aps else []
    noises = [source for source in analysis.noise_sources(project, context)
              if band == "All" or source["band"] == band] if show_noise else []
    devices = analysis.bt_devices(project) if show_bt else []
    other = analysis.bt_other(project) if show_bt else {"fixed": [], "rotating": []}
    clients = analysis.wifi_clients(project) if show_clients else []
    tags = analysis.finder_tags(project) if show_tags else []
    return overlay, markers, sites, noises, devices, other, clients, tags


def _ap_list(point, target: float) -> Gtk.Widget:
    """
    Every access point heard at a point, strongest first: its network's colour, name, band and
    channel, and its level in green, amber or red against the requirement.
    """
    grid = Gtk.Grid(column_spacing=8, row_spacing=4, margin_top=6)
    aps = analysis.access_points(point.networks)
    grid.attach(Gtk.Label(label=f"Access points heard ({len(aps)})", xalign=0,
                          css_classes=["heading"]), 0, 0, 4, 1)
    for row, ap in enumerate(aps, start=1):
        colour = ssid_colour(ap["ssid"])
        dot = Gtk.DrawingArea(content_width=12, content_height=12, valign=Gtk.Align.CENTER)

        def paint(_area, cr, width, height, colour=colour) -> None:
            cr.arc(width / 2, height / 2, min(width, height) / 2 - 0.5, 0, 2 * math.pi)
            cr.set_source_rgb(*colour)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.45)
            cr.set_line_width(1)
            cr.stroke()

        dot.set_draw_func(paint)
        grid.attach(dot, 0, row, 1, 1)
        grid.attach(Gtk.Label(label=hidden.label(ap["ssid"], ap.get("hidden")), xalign=0,
                              max_width_chars=18,
                              ellipsize=Pango.EllipsizeMode.END), 1, row, 1, 1)
        where = f"{BAND_NAMES.get(ap['band'], ap['band'])} " + (
            f"ch {ap['channel']}" if ap.get("channel") else f"{ap['freq']:.0f} MHz")
        if ap["width"] > 20:
            where += f", {ap['width']} MHz"
        grid.attach(Gtk.Label(label=where, xalign=0, css_classes=["dim-label", "caption"]), 2, row, 1, 1)
        grade = signal_quality(ap["rssi"], target)
        level = Gtk.Label(label=f"{'~' if ap['estimate'] else ''}{ap['rssi']} dBm", xalign=1,
                          css_classes=["numeric", {"good": "success", "fair": "warning"}.get(grade, "error")])
        level.set_tooltip_text(" · ".join(part for part in (ap.get("vendor"), ap.get("bssid")) if part))
        grid.attach(level, 3, row, 1, 1)
    if not aps:
        grid.attach(Gtk.Label(label="None", xalign=0, css_classes=["dim-label"]), 0, 1, 4, 1)
    return grid


def _draw_number(cr: cairo.Context, x: float, y: float, number: int) -> None:
    """A point's number in a small dark pill beside it."""
    text = str(number)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(11)
    extents = cr.text_extents(text)
    w, h = max(16, extents.x_advance + 8), 16
    cr.new_sub_path()
    cr.arc(x + h / 2, y + h / 2, h / 2, math.pi / 2, 3 * math.pi / 2)
    cr.arc(x + w - h / 2, y + h / 2, h / 2, -math.pi / 2, math.pi / 2)
    cr.close_path()
    cr.set_source_rgba(0.10, 0.12, 0.16, 0.85)
    cr.fill()
    cr.set_source_rgb(1, 1, 1)
    cr.move_to(x + (w - extents.x_advance) / 2, y + 12)
    cr.show_text(text)


def _draw_scale_bar(cr: cairo.Context, project, scale: float, height: int) -> None:
    """A bar of a round number of feet in the bottom-left corner, once the plan has a scale."""
    if not project.meters_per_pixel:
        return
    fits = [f for f in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500)
            if 50 <= project.pixels(f) * scale <= 220]
    if not fits:
        return
    feet = fits[-1]
    length = project.pixels(feet) * scale
    x, y = 14, height - 64                         # clear of the status line
    cr.set_source_rgba(1, 1, 1, 0.85)
    cr.rectangle(x - 6, y - 16, length + 12, 26)
    cr.fill()
    cr.set_source_rgb(0.10, 0.12, 0.16)
    cr.set_line_width(2)
    cr.move_to(x, y)
    cr.line_to(x, y + 5)
    cr.line_to(x + length, y + 5)
    cr.line_to(x + length, y)
    cr.stroke()
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(11)
    cr.move_to(x + 2, y - 4)
    cr.show_text(f"{feet} ft")


def _draw_badge(cr: cairo.Context, x: float, y: float) -> None:
    """A small red "!" for a point where connecting or the connection check went wrong."""
    cr.new_path()
    cr.arc(x, y, 7, 0, 2 * math.pi)
    cr.set_source_rgb(0.86, 0.15, 0.15)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(1.5)
    cr.stroke()
    cr.set_line_width(2)
    cr.move_to(x, y - 3.5)
    cr.line_to(x, y + 1)
    cr.stroke()
    cr.arc(x, y + 3.5, 1, 0, 2 * math.pi)
    cr.fill()


def _show_list(parent: Gtk.Widget, title: str, rows: list[tuple[str, str, str]], empty: str,
               action: tuple[str, Callable[[], None]] | None = None) -> None:
    """A dialog listing findings: a tag, a title and a sentence each, and optionally a button."""
    tags = {"High": "error", "Medium": "warning", "Low": "accent", "Change": "warning",
            "Keep": "success", "Wall": "accent"}
    listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    for tag, heading, detail in rows:
        row = Adw.ActionRow(title=GLib.markup_escape_text(heading),
                            subtitle=GLib.markup_escape_text(detail), subtitle_lines=6)
        row.add_prefix(Gtk.Label(label=tag, width_chars=7, css_classes=[tags.get(tag, "dim-label"),
                                                                         "caption-heading"]))
        listing.append(row)
    if not rows:
        listing.append(Adw.ActionRow(title=GLib.markup_escape_text(empty)))
    body = Gtk.ScrolledWindow(child=listing, propagate_natural_height=True, max_content_height=560,
                              hscrollbar_policy=Gtk.PolicyType.NEVER, margin_start=12,
                              margin_end=12, margin_bottom=12)
    view = Adw.ToolbarView(content=body)
    header = Adw.HeaderBar()
    view.add_top_bar(header)
    dialog = Adw.Dialog(title=title, child=view, content_width=560)
    if action is not None:
        label, run = action
        button = Gtk.Button(label=label, css_classes=["suggested-action"])
        button.connect("clicked", lambda *_: (run(), dialog.close()))
        header.pack_start(button)
    dialog.present(parent)


def _compare(now, before, key: str, ssid, band: str, channel, profile: str, bssid=None):
    """The change in one metric between two surveys of the same plan, as a map and a summary."""
    _title, _unit, _low, _high, higher_is_better = analysis.METRICS[key]

    def samples(project):
        context = analysis.Context(project, profile=profile)
        found = []
        for point in project.points:
            value = analysis.metric(point, key, context, ssid=ssid, bssid=bssid, band=band,
                                    channel=channel)
            if value is not None:
                found.append((point.x, point.y, value))
        return found

    return heatmap.difference(now.width, now.height, samples(now), samples(before),
                              CHANGE_SCALE[key], higher_is_better, _heat_radius(now))


def _no_map_reason(title: str, key: str, what: str | None) -> str:
    """Why the coloured map is blank, so it never just vanishes silently."""
    if key in ("retries", "airtime"):
        return f"{title}: no capture is attached to any point yet (Capture tab → Attach to point)."
    if key == "unexplained":
        return f"{title}: needs the analyzer's spectrum and Wi-Fi both at a point to compare them."
    if key in ("latency", "loss"):
        return f"{title}: no connection check has been recorded at any point yet."
    if key == "throughput":
        return (f"{title}: no throughput test yet. Turn on Test throughput under Measuring and "
                "give it an iperf3 server.")
    if key == "predicted":
        return f"{title}: add a planned access point under Planning first."
    if what:
        return f"{title}: {what} wasn't heard at any measured point."
    return f"{title}: nothing to show for the current selection."


def _ring_radius(project) -> float:
    """A spectrum ring's radius in plan pixels: 10 ft across, or inside the heatmap circle."""
    return project.pixels(RING_DIAMETER_FEET / 2) or _heat_radius(project) * RING_SHARE


def _heat_radius(project) -> float:
    return planning.heat_radius(project)


def _clamp_scale(value: float) -> float:
    return max(0.05, min(6.0, value))


def _network_dict(r) -> dict:
    return {"ssid": r.ssid, "bssid": r.bssid, "rssi": r.rssi, "freq_mhz": r.freq_mhz,
            "channel": r.channel, "band": r.band, "secured": r.secured,
            "security": r.security, "rssi_source": r.source,
            "center_mhz": r.span_center_mhz, "width_mhz": r.width_mhz,
            "vendor": r.vendor, "rssi_offset": r.offset_db, **r.beacon_details()}


def _merge_scans(first: list[dict], second: list[dict]) -> list[dict]:
    """
    Two scans of the same spot as one: every access point either heard. One heard by both is
    read twice at the same spot, so its level is the mean of the readings, which is closer to
    the truth than either alone (the stronger would read high on average). A true dBm reading
    beats an estimate. Beacon details come from whichever scan had them.
    """
    merged = {n["bssid"]: n for n in first}
    for network in second:
        known = merged.get(network["bssid"])
        if known is None:
            merged[network["bssid"]] = network
            continue
        if network.get("rssi_source") != known.get("rssi_source"):
            if network.get("rssi_source") == "iw":
                merged[network["bssid"]] = {**known, **{k: v for k, v in network.items() if v}}
            continue
        count = int(known.get("readings") or 1)
        level = (known["rssi"] * count + network["rssi"]) / (count + 1)
        merged[network["bssid"]] = {**known, **{k: v for k, v in network.items() if v},
                                    "rssi": int(round(level)), "readings": count + 1}
    return sorted(merged.values(), key=lambda n: n["rssi"], reverse=True)


def _adapter_name(device: str) -> str:
    for iface in wifi.interfaces():
        if iface.name == device:
            return getattr(iface, "model", "") or iface.driver or device
    return device


_KIND_WORDS = {"point": "point", "wall": "wall", "virtual": "planned access point"}


def _items(project, kind: str) -> list:
    return {"point": project.points, "wall": project.walls,
            "virtual": project.virtual_aps}[kind]


def _segment_distance(p, a, b) -> float:
    """Distance from p to the segment a-b, all in the same coordinates."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _gesture_help() -> Gtk.Popover:
    grid = Gtk.Grid(column_spacing=16, row_spacing=8, margin_start=12, margin_end=12,
                    margin_top=12, margin_bottom=12, css_classes=["gesture-help"])
    for row, (gesture, meaning) in enumerate((
        ("Tap", "Measure here; on a numbered point, its details and access points"),
        ("Drag", "Pan (flick to coast), or draw a wall in wall mode"),
        ("Pinch", "Zoom, and pan with the same two fingers"),
        ("Double-tap", "Zoom in on that spot"),
        ("Two-finger tap", "Fit the whole plan"),
        ("Touch and hold", "Details of a point, access point or noise source; delete a point or wall"),
        ("Set scale", "Tap two spots a known distance apart and enter it in feet"),
        ("Tap a spectrum ring's dot", "That access point's name, band, channel and strength"),
        ("Swipe in from the right", "Map settings, when the window is narrow"),
    )):
        grid.attach(Gtk.Label(label=gesture, xalign=0, css_classes=["key"]), 0, row, 1, 1)
        grid.attach(Gtk.Label(label=meaning, xalign=0, wrap=True, max_width_chars=34), 1, row, 1, 1)
    return Gtk.Popover(child=grid)


def spec_end(segment: dict) -> float:
    return segment["start_mhz"] + segment["step_mhz"] * (len(segment["avg"]) - 1)


def _marker_colour(key: str, value: float | None, low: float, high: float,
                   higher_is_better: bool) -> tuple[float, float, float]:
    if value is None:
        return (0.7, 0.72, 0.75)
    if key == "verdict":
        return analysis.VERDICTS[int(value)][2]
    if key == "requirement":
        return analysis.REQUIREMENT_COLOURS[int(value)]
    span = (high - low) or 1
    t = (value - low) / span
    t = t if higher_is_better else 1 - t
    red, green, blue = heatmap.colour(max(0.0, min(1.0, t)))
    return (float(red) / 255, float(green) / 255, float(blue) / 255)


BAND_COLOURS = {"2.4": (0.85, 0.52, 0.04), "5": (0.15, 0.45, 0.90), "6": (0.55, 0.25, 0.85)}
BAND_TAGS = {"2.4": "2.4G", "5": "5G", "6": "6G"}
BAND_NAMES = {"2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}


def _channel_text(channels: list[int]) -> str:
    if not channels:
        return "between channels"
    if len(channels) == 1:
        return f"ch {channels[0]}"
    return f"ch {channels[0]}–{channels[-1]}"


def _beyond_text(beyond: dict) -> str:
    """How far off the plan an access point is: "about 85 ft north-east of the plan"."""
    feet = beyond.get("feet")
    return (f"about {feet:.0f} ft " if feet is not None else "") + beyond["way"]


def _noise_detail(noise: dict) -> str:
    return (f"{noise['center']:.0f} MHz · {BAND_NAMES.get(noise['band'], '')} "
            f"{_channel_text(noise['channels'])} · peak {noise['peak']:.0f} dBm")


def _icon_title(view, kind: str, index: int) -> str:
    if kind == "site":
        site = view.sites[index]
        if site.get("placed"):
            return site.get("name") or "Access point (placed by you)"
        return (site.get("name") + ", probably about here") if site.get("name") \
            else "Access point, probably about here"
    if kind == "bt":
        device = view.bt[index]
        return device["name"] or device["vendor"] or device["detail"] or "Bluetooth device"
    if kind == "bt_near":
        device = view.bt_other["fixed"][index]
        return device.get("name") or device.get("vendor") or device.get("detail") or "Bluetooth device"
    if kind == "client":
        return _client_name(view.clients[index])
    if kind == "tag":
        tag = view.tags[index]
        return tag["label"] + (", away from its owner" if tag["away"] else "")
    return view.noises[index]["name"]


def _icon_details(view, kind: str, index: int) -> str:
    if kind == "tag":
        tag = view.tags[index]
        located = tag["located"]
        lines = [f"{tag['address']} · strongest {tag['best']} dBm at point {tag['near_point']}, "
                 f"heard at {tag['points']} point{'s' if tag['points'] != 1 else ''}."]
        if tag["state"]:
            lines.append(f"It reports being {tag['state']}"
                         + (f"; battery {tag['battery']}." if tag["battery"] else "."))
        if tag["away"]:
            lines.append("Nobody it belongs to was nearby: it may be lost, left on purpose, or "
                         "planted. Worth finding. Phones can play its sound to help "
                         "(iPhone: Find My → Items → Identify Found Item; Android: Unknown "
                         "tracker alerts).")
        else:
            lines.append("Its owner was nearby, so most likely someone's keys or bag.")
        if located.get("near"):
            lines.append("Shown beside the point where it was strongest. Tags with their owner "
                         "change address every 15 minutes or so, so measure 4 or more points "
                         "around it in quick succession to pin it down.")
        elif located.get("rough"):
            lines.append("Rough: measure from more sides of it to pin it down.")
        else:
            lines.append("Estimated from how its signal fades between points.")
        return "\n".join(lines)
    if kind == "client":
        client = view.clients[index]
        located = client["located"]
        rssi = client["best"]
        word = "strong" if rssi >= -60 else "fair" if rssi >= -70 else "weak"
        lines = [f"{client['mac'].upper()} on {client['ssid'] or '(hidden)'}",
                 f"Strongest {rssi:.0f} dBm ({word}) at point {client['near_point']}, "
                 f"heard at {client['points']} point{'s' if client['points'] != 1 else ''}."]
        if client["vendor"] == "Private address":
            lines.append("A private (randomised) address, so the maker can't be looked up.")
        if located.get("near"):
            lines.append("Too few points to place it, so shown beside the point where it was "
                         "strongest. Capture on the same channel at 4 or more points around it "
                         "to pin it down.")
        elif located.get("outside"):
            lines.append("The best fit is at the edge of the plan, so it's probably outside.")
        elif located.get("rough"):
            lines.append("Rough: capture from more sides of it to pin it down.")
        else:
            lines.append("Estimated from how its signal fades between points.")
        lines.append("Clients move: this is where it was during the captures.")
        return "\n".join(lines)
    if kind == "bt_near":
        device = view.bt_other["fixed"][index]
        return (f"{_bt_detail(device)}. Heard at only {device['points']} point"
                f"{'s' if device['points'] != 1 else ''}, too few to place — shown where it was "
                "strongest. Measure more points near it to pin it down.")
    if kind == "bt":
        device = view.bt[index]
        located = device["located"] or {}
        lines = [_bt_detail(device)]
        if device["vendor"] and device["vendor"] not in (device["name"] or ""):
            lines.append(f"Made by {device['vendor']}.")
        lines.append("A Bluetooth device sharing 2.4 GHz. If it's not needed here, switching its "
                     "Bluetooth off frees airtime; visit it to check.")
        if located.get("outside"):
            lines.append("The best fit is at the edge of the plan, so it's probably outside.")
        elif located.get("rough"):
            lines.append("Rough: measure from more sides of it to pin it down.")
        return "\n".join(lines)
    if kind == "site":
        site = view.sites[index]
        lines = [f"{name}: {', '.join(BAND_NAMES.get(b, b) for b in bands)}"
                 for name, bands in site["networks"]]
        if site.get("placed"):
            lines.append("Where you put it. Its reach is worked out from this position.")
        elif site.get("beyond"):
            lines.append(f"Off the plan: {_beyond_text(site['beyond'])}"
                         + (", rough" if site["rough"] else "")
                         + ". Located from how its signal fades across the points.")
        elif site.get("unfitted"):
            lines.append("Its signal changes too little between points to place it, so shown "
                         "in the middle of where it was heard loudest. Measure closer to it, "
                         "or move it to where it really is.")
        elif site.get("guess"):
            lines.append("Heard at too few points to locate, so shown beside the point where "
                         "it was strongest. Move it to where it really is.")
        elif site["outside"]:
            lines.append("The best fit is at the edge of the plan, so it's probably outside.")
        elif site["rough"]:
            lines.append("Rough: measure from more sides of it, or move it to where it really is.")
        else:
            lines.append("Estimated from how its signal fades. If you know where it is, move it "
                         "there.")
        if site.get("lat_lon") and not site.get("guess"):
            lines.append("At {:.5f}, {:.5f}".format(*site["lat_lon"]))
        project = view.session.project
        reaches = [(fit["ssid"], fit["band"], advice.reach_feet(fit, analysis.Context(project).profile.primary,
                                                                project.meters_per_pixel))
                   for fit in site.get("fits", []) if not fit.get("rough")]
        reaches = [f"{name or '(hidden)'} {band} GHz ≈{feet:.0f} ft" for name, band, feet in reaches if feet]
        if reaches:
            lines.append("Reach in the open: " + ", ".join(reaches[:4]))
        return "\n".join(lines)
    noise = view.noises[index]
    located = noise["located"] or {}
    lines = [
        f"{noise['center']:.0f} MHz, about {noise['width']:.0f} MHz wide, on "
        f"{BAND_NAMES.get(noise['band'], '')} {_channel_text(noise['channels'])}.",
        f"Peak {noise['peak']:.0f} dBm, seen at {noise['points']} points.",
        noise["description"],
    ]
    if noise.get("identified"):
        lines.append("Named by the HackRF, which heard its transmissions whole.")
    if located.get("outside"):
        lines.append("The best fit is at the edge of the plan, so it's probably outside.")
    elif located.get("rough"):
        lines.append("Rough: measure from more sides of it to pin it down.")
    return "\n".join(lines)


BT_ICON_COLOUR = (0.15, 0.35, 0.85)
ME_COLOUR = (0.10, 0.45, 0.95)
ADVANCED = "Advanced…"
LOCATE_EVERY_MS = 2000
ME_HELP = ("Follows you on the plan from the access points you can hear, rescanning every "
           "couple of seconds. Only access points are used, never other devices. Tapping to "
           "measure recalibrates; so does holding an open spot and choosing I'm here.")


def _feet(project, plan_pixels: float) -> float | None:
    mpp = project.meters_per_pixel
    return plan_pixels * mpp / 0.3048 if mpp else None


def _draw_me(cr: cairo.Context, x: float, y: float, spread: float, rough: bool) -> None:
    """The usual "you are here": a blue dot in a white ring, inside a pale circle as wide as
    the uncertainty."""
    cr.new_path()
    cr.arc(x, y, max(spread, 14), 0, 2 * math.pi)
    cr.set_source_rgba(*ME_COLOUR, 0.10 if rough else 0.16)
    cr.fill_preserve()
    cr.set_source_rgba(*ME_COLOUR, 0.45)
    cr.set_line_width(1.5)
    if rough:
        cr.set_dash([5, 4])
    cr.stroke()
    cr.set_dash([])
    cr.arc(x, y, 9, 0, 2 * math.pi)
    cr.set_source_rgb(1, 1, 1)
    cr.fill()
    cr.arc(x, y, 6.5, 0, 2 * math.pi)
    cr.set_source_rgba(*ME_COLOUR, 0.6 if rough else 1.0)
    cr.fill()


def _client_name(client: dict) -> str:
    vendor = client["vendor"]
    if not vendor:
        return "Unknown maker"
    return "Private address" if vendor == "Private address" else vendor.split(",")[0][:28]


TAG_AWAY_COLOUR = (0.86, 0.15, 0.15)


def _draw_tag_icon(cr: cairo.Context, x: float, y: float, away: bool, rough: bool) -> None:
    """A white disc like an AirTag with a key-ring hole; a red rim when it's away from its owner."""
    alpha = 0.65 if rough else 1.0
    cr.new_path()
    cr.arc(x, y, 10, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.fill_preserve()
    cr.set_source_rgba(*(TAG_AWAY_COLOUR if away else (0.35, 0.37, 0.42)), alpha)
    cr.set_line_width(3 if away else 2)
    cr.stroke()
    cr.arc(x, y, 4.5, 0, 2 * math.pi)
    cr.set_source_rgba(0.55, 0.57, 0.62, alpha)
    cr.set_line_width(1.5)
    cr.stroke()
    cr.arc(x + 6.5, y - 6.5, 2.2, 0, 2 * math.pi)      # the loop where a key ring would go
    cr.set_source_rgba(0.35, 0.37, 0.42, alpha)
    cr.fill()


def _draw_client_icon(cr: cairo.Context, x: float, y: float, rssi: float, rough: bool) -> None:
    """
    A small rounded square with a screen inside, for a phone or laptop. Its edge is green,
    amber or red by how strongly the client was heard, so weak clients stand out.
    """
    alpha = 0.6 if rough else 1.0
    edge = ((0.08, 0.50, 0.24) if rssi >= -60 else (0.90, 0.60, 0.05) if rssi >= -70
            else (0.86, 0.15, 0.15))
    cr.new_path()
    radius, half = 3, 8
    cr.arc(x + half - radius, y - half + radius, radius, -math.pi / 2, 0)
    cr.arc(x + half - radius, y + half - radius, radius, 0, math.pi / 2)
    cr.arc(x - half + radius, y + half - radius, radius, math.pi / 2, math.pi)
    cr.arc(x - half + radius, y - half + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.set_source_rgba(0.20, 0.22, 0.26, alpha)
    cr.fill_preserve()
    cr.set_source_rgba(*edge, alpha)
    cr.set_line_width(2.5)
    cr.stroke()
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.rectangle(x - 4, y - 5, 8, 8)
    cr.fill()


def _bt_detail(device: dict) -> str:
    kinds = {"printer": "Printer", "imaging": "Scanner/imaging", "audio": "Audio device",
             "peripheral": "Peripheral", "computer": "Computer", "network": "Network device",
             "phone": "Phone", "wearable": "Wearable", "health": "Health device",
             "unknown": "Bluetooth device"}
    what = kinds.get(device["kind"], "Bluetooth device")
    return f"{what} · {device['address']} · strongest {device['best']} dBm, {device['points']} points"


def _draw_bt_icon(cr: cairo.Context, x: float, y: float, kind: str, rough: bool) -> None:
    """A blue Bluetooth badge, with a tiny glyph for printers so they stand out from the rest."""
    alpha = 0.6 if rough else 1.0
    cr.new_path()
    cr.arc(x, y, 11, 0, 2 * math.pi)
    cr.set_source_rgba(*BT_ICON_COLOUR, alpha)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.set_line_width(2)
    cr.stroke()
    cr.set_source_rgba(1, 1, 1, alpha)
    if kind == "printer":
        # a little printer: body, paper slot, tray
        cr.set_line_width(1.4)
        cr.rectangle(x - 4.5, y - 2.5, 9, 5)
        cr.stroke()
        cr.move_to(x - 2.5, y - 2.5)
        cr.line_to(x - 2.5, y - 5)
        cr.line_to(x + 2.5, y - 5)
        cr.line_to(x + 2.5, y - 2.5)
        cr.stroke()
        cr.rectangle(x - 3, y + 2.5, 6, 2.5)
        cr.stroke()
    else:
        # the Bluetooth rune: a spine, two right-pointing triangles, two left ticks
        cr.set_line_width(1.4)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.move_to(x, y - 6)                  # spine and right edges, as one polyline
        cr.line_to(x + 3, y - 3)
        cr.line_to(x, y)
        cr.line_to(x + 3, y + 3)
        cr.line_to(x, y + 6)
        cr.line_to(x, y - 6)
        cr.move_to(x - 3, y - 3)              # left ticks crossing the spine
        cr.line_to(x, y)
        cr.line_to(x - 3, y + 3)
        cr.stroke()


def _draw_site(cr: cairo.Context, x: float, y: float, rough: bool, placed: bool = False) -> None:
    """
    A plain circle where an access point probably is, dashed when the fix is rough; a square
    with a dot where the surveyor put it, since that's known rather than estimated.
    """
    cr.new_path()
    if placed:
        cr.rectangle(x - 8, y - 8, 16, 16)
    else:
        cr.arc(x, y, 8, 0, 2 * math.pi)
    cr.set_source_rgba(0.15, 0.45, 0.90, 0.55 if rough else 1.0)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(2)
    if rough:
        cr.set_dash([3, 2.5])
    cr.stroke()
    cr.set_dash([])
    if placed:
        cr.arc(x, y, 2.5, 0, 2 * math.pi)
        cr.fill()


def _watch_colour(watch: dict) -> tuple[float, float, float]:
    """Green, amber or red by how much of the watched time the network was usable."""
    worst = min((b["availability"] for b in watch.get("bands", []) if b.get("availability") is not None),
                default=None)
    return {"good": (0.13, 0.64, 0.29), "fair": (0.92, 0.70, 0.03),
            "poor": (0.86, 0.15, 0.15)}[watchreport.availability_class(worst)]


def _watch_details(watch: dict) -> str:
    lines = [watchreport.headline(watch) + "."]
    for band in watch.get("bands", []):
        causes = sorted({i["cause"] for i in band["incidents"] if i["cause"]})
        share = band.get("availability")
        lines.append(f"{band['band'] or '?'}: " + ("—" if share is None else f"up {share:.2%}")
                     + (f", {len(band['incidents'])} dropouts: {'; '.join(causes)}" if band["incidents"] else ""))
    return "\n".join(lines)


def _draw_watch(cr: cairo.Context, x: float, y: float, colour) -> None:
    """Where a network was watched from: a rounded square with a pulse line through it."""
    cr.new_path()
    r, s = 4, 10
    cr.move_to(x - s + r, y - s)
    cr.arc(x + s - r, y - s + r, r, -math.pi / 2, 0)
    cr.arc(x + s - r, y + s - r, r, 0, math.pi / 2)
    cr.arc(x - s + r, y + s - r, r, math.pi / 2, math.pi)
    cr.arc(x - s + r, y - s + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.set_source_rgb(*colour)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(2)
    cr.stroke()
    cr.move_to(x - 7, y)
    for dx, dy in ((-3, 0), (-1, -5), (1, 5), (3, -2), (4, 0), (7, 0)):
        cr.line_to(x + dx, y + dy)
    cr.set_line_width(1.6)
    cr.stroke()


def _draw_virtual(cr: cairo.Context, x: float, y: float) -> None:
    """A planned access point: a hollow purple diamond, dashed, so it never passes for a real one."""
    cr.new_path()
    cr.move_to(x, y - 11)
    cr.line_to(x + 11, y)
    cr.line_to(x, y + 11)
    cr.line_to(x - 11, y)
    cr.close_path()
    cr.set_source_rgba(1, 1, 1, 0.85)
    cr.fill_preserve()
    cr.set_source_rgb(0.55, 0.25, 0.85)
    cr.set_line_width(2.5)
    cr.set_dash([4, 2.5])
    cr.stroke()
    cr.set_dash([])
    cr.arc(x, y, 3, 0, 2 * math.pi)
    cr.fill()

def _asks_what_taps_are() -> bool:
    return bool(settings.get("tap_asks", False) or settings.get("easy_mode", False))


def _tone_words(point) -> str:
    """What the last point made of the tone, for the status line, so a miss is noticed at once."""
    tone = point.tone
    if not tone:
        return ""
    if tone.get("dbm") is None:
        return (f". Test tone not heard here (floor {tone.get('floor_dbm', 0):.0f} dBm): check "
                "it's running, or raise its level")
    return f", test tone {tone['dbm']:.0f} dBm"


def _draw_tone(cr: cairo.Context, x: float, y: float) -> None:
    """The test tone source: a teal mast with rings, so it never passes for an access point."""
    cr.set_source_rgb(0.05, 0.58, 0.53)
    cr.set_line_width(2.2)
    for radius in (6, 10):
        cr.new_sub_path()
        cr.arc(x, y - 3, radius, -math.pi * 0.85, -math.pi * 0.15)
        cr.stroke()
    cr.move_to(x, y - 3)
    cr.line_to(x, y + 10)
    cr.stroke()
    cr.arc(x, y - 3, 2.5, 0, 2 * math.pi)
    cr.fill()


def _draw_noise_icon(cr: cairo.Context, x: float, y: float, rough: bool) -> None:
    """A static-crackle badge: where the noise is probably coming from."""
    alpha = 0.65 if rough else 1.0
    cr.arc(x, y, 11, 0, 2 * math.pi)
    cr.set_source_rgba(0.93, 0.33, 0.13, alpha)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.set_line_width(2)
    cr.stroke()
    cr.move_to(x - 7, y)
    for step, dy in enumerate((-4, 4, -5, 5, -3, 0)):
        cr.line_to(x - 7 + (step + 1) * 14 / 6, y + dy)
    cr.set_line_width(1.8)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.stroke()


def _draw_label(cr: cairo.Context, x: float, y: float,
                lines: list[tuple[str, list[str] | None]]) -> None:
    """
    A small card beside an icon: each line is text, optionally led by band badges
    (2.4G, 5G, 6G) in their band's colour.
    """
    if not lines:
        return
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    line_height, badge_gap, pad = 17, 4, 6
    widths = []
    for row, (text, bands) in enumerate(lines):
        width = 0.0
        cr.set_font_size(10)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        for band in bands or []:
            width += cr.text_extents(BAND_TAGS.get(band, band)).x_advance + 8 + badge_gap
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD if row == 0 and not bands else cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        width += cr.text_extents(text).x_advance
        widths.append(width)
    box_w = max(widths) + pad * 2
    box_h = line_height * len(lines) + pad
    top = y - box_h / 2
    radius = 6
    cr.new_sub_path()
    cr.arc(x + box_w - radius, top + radius, radius, -math.pi / 2, 0)
    cr.arc(x + box_w - radius, top + box_h - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, top + box_h - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, top + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.set_source_rgba(1, 1, 1, 0.88)
    cr.fill()

    for row, (text, bands) in enumerate(lines):
        baseline = top + pad / 2 + line_height * (row + 1) - 4
        cursor = x + pad
        for band in bands or []:
            tag = BAND_TAGS.get(band, band)
            cr.set_font_size(10)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            tag_w = cr.text_extents(tag).x_advance + 8
            cr.rectangle(cursor, baseline - 11, tag_w, 14)
            cr.set_source_rgb(*BAND_COLOURS.get(band, (0.4, 0.4, 0.4)))
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(cursor + 4, baseline)
            cr.show_text(tag)
            cursor += tag_w + badge_gap
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD if row == 0 and not bands else cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        cr.set_source_rgb(0.10, 0.12, 0.16)
        cr.move_to(cursor, baseline)
        cr.show_text(text)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)


def _surface_from_pixbuf(pixbuf) -> cairo.ImageSurface:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixbuf.get_width(), pixbuf.get_height())
    context = cairo.Context(surface)
    Gdk.cairo_set_source_pixbuf(context, pixbuf, 0, 0)
    context.paint()
    return surface
