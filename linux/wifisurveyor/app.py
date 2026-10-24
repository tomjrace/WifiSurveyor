"""The application shell: a libadwaita window with one view per job."""

from __future__ import annotations

import base64
import copy
import os
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .core import hidden, library, report, settings  # noqa: E402
from .core.project import (  # noqa: E402
    Project, clear_recovery, hand_back, in_root_home, read_recovery, write_recovery,
)
from .net import gps, oui, wifi  # noqa: E402
from .rf import detect, hackrf, rtlsdr  # noqa: E402
from .ui.capture_view import CaptureView  # noqa: E402
from .ui.common import load_style, run_in_thread  # noqa: E402
from .ui.discover import DiscoverView  # noqa: E402
from .ui.frequencies_view import FrequenciesView  # noqa: E402
from .ui.monitor_view import MonitorView  # noqa: E402
from .ui.setup_view import SetupView  # noqa: E402
from .ui.spectrum_view import SpectrumView, band_png, range_png, snapshot_png  # noqa: E402
from .ui.survey_view import SurveyView  # noqa: E402

AUTOSAVE_SECONDS = 120


def report_details(project) -> dict:
    """The report's cover: who it's from (this machine's settings) and for (the survey)."""
    details = {key: settings.get(f"report_{key}", "") or ""
               for key in ("company", "surveyor", "contact")}
    details.update({key: (project.report_info or {}).get(key, "")
                    for key in ("client", "site", "purpose")})
    details["logo"] = settings.get("report_logo")
    return details


class Session:
    """What the views share: the open survey, the GPS, and the analyzer."""

    def __init__(self) -> None:
        self.project = Project()
        self.gps = gps.GpsSource()
        # The analyzer the live views sweep with: a serial one or a HackRF (see _choose). A
        # survey point sweeps with all of them at once (see sweepers).
        self.explorer = None
        self.serial_analyzers: list = []      # every RF Explorer and tinySA connected
        self.hackrfs: list[hackrf.HackRf] = []
        self.hackrf_status = ""       # why there's no HackRF, when there isn't
        self.hackrf_probing = False
        self.rtlsdrs: list[rtlsdr.RtlSdr] = []
        self.rtlsdr_status = ""       # why there's no RTL-SDR, when there isn't
        self.rtlsdr_probing = False
        self.rf_detection: detect.Detection | None = None
        self.last_scan: list = []
        self.scan_device: str | None = None
        self.monitor_device: str | None = None
        # Who is listening on the monitor adapter now ("Discover", "Capture", "Survey"), so
        # the survey doesn't retune it from under a capture
        self.monitor_users: set[str] = set()
        # Adapters kept on one job for a long time (the SSID monitor's), and by whom: the other
        # pages leave them alone rather than retune them from under it
        self.radio_holds: dict[str, str] = {}
        self.scan_manual = False
        self.monitor_manual = False
        self.scan_dropdowns: list = []
        self.monitor_dropdowns: list = []
        self.gps_port: str | None = None
        self.rfe_probing = False
        # The survey's requirement (core.rfplan.PROFILES), which the Discover page grades by
        self.profile_key = "data"
        # A spectrum recording is going, so nothing else should retune the analyzer meanwhile
        self.spectrum_recording = False
        self._listeners: dict[str, list[Callable]] = {}

    # ---------- events ----------

    def on(self, event: str, handler: Callable) -> None:
        """
        "project" (a different survey is open), "changed" (this one was edited), "explorer"
        (an analyzer or the HackRF came or went), "scan" (``last_scan`` has fresh readings),
        "options" (Setup's easy mode or tap question changed), "satellites" (one was paired
        or forgotten), "spectrum-choice" (the ranges to sweep changed, on the Spectrum page or
        in the survey setup; passed whoever changed them).
        """
        self._listeners.setdefault(event, []).append(handler)

    def emit(self, event: str, *args) -> None:
        for handler in self._listeners.get(event, []):
            handler(*args)

    def changed(self) -> None:
        self.project.modified = True
        self.emit("changed")

    def set_project(self, project: Project) -> None:
        self.project = project
        # Before any view draws it: hidden networks marked, and named where a name is known
        hidden.apply_all(project)
        self.emit("project")

    # ---------- the analyzer ----------

    @property
    def serial_analyzer(self):
        """The main RF Explorer or tinySA: the first still working. None without one."""
        working = [a for a in self.serial_analyzers if a.alive]
        return (working or self.serial_analyzers or [None])[0]

    @property
    def hackrf(self):
        """The main HackRF: the first free to listen (not sending the tone), else the first."""
        working = [d for d in self.hackrfs if d.alive]
        free = [d for d in working if not d.sending]
        return (free or working or self.hackrfs or [None])[0]

    def listening_hackrfs(self) -> list:
        """Every HackRF free to listen: working and not sending the tone."""
        return [d for d in self.hackrfs if d.alive and not d.sending]

    def sweepers(self) -> list:
        """
        What a survey point sweeps with, all at once: every serial analyzer, and the HackRFs
        when there's no serial analyzer or Setup says to sweep with them too.
        """
        serial = [a for a in self.serial_analyzers if a.alive]
        prefer = bool(settings.get("hackrf_sweeps", False))
        radios = self.listening_hackrfs() if prefer or not serial else []
        return radios + serial

    def band_analyzers(self) -> list:
        """
        What sweeps the bands beyond Wi-Fi (mics, sub-GHz, business radio): every working
        analyzer that isn't busy sending a signal. RTL-SDRs first, so they take the bands they
        reach and leave the others free for what only they can do.
        """
        serial = [a for a in self.serial_analyzers if a.alive and getattr(a, "generating", None) is None]
        # An RTL-SDR that failed its reception check would call every band quiet: left out
        return [*[d for d in self.rtlsdrs if d.healthy], *self.listening_hackrfs(), *serial]

    def all_analyzers(self) -> list:
        """Every analyzer connected and working, whatever it's doing."""
        return [a for a in (*self.serial_analyzers, *self.hackrfs, *self.rtlsdrs) if a.alive]

    def close_all(self) -> None:
        """Lets go of every radio, so a relaunched copy can open them (exec keeps children)."""
        for device in (*self.serial_analyzers, *self.hackrfs, *self.rtlsdrs):
            try:
                device.close()
            except Exception:
                pass

    def set_rtlsdr(self, device) -> None:
        """Adds an RTL-SDR (replacing one on the same USB node), or with None drops dead ones."""
        keep = []
        for current in self.rtlsdrs:
            gone = (not current.alive) if device is None else current.device == device.device
            if gone and current is not device:
                current.close()
            elif current is not device:
                keep.append(current)
        self.rtlsdrs = keep + ([device] if device is not None else [])
        self.emit("explorer")

    def drop_rtlsdr(self, device) -> None:
        if device in self.rtlsdrs:
            self.rtlsdrs.remove(device)
            device.close()
        self.emit("explorer")

    def detect_rtlsdr(self) -> None:
        """Opens every RTL-SDR plugged in and not open yet, in the background."""
        if self.rtlsdr_probing:
            return
        found = rtlsdr.plugged_in()
        if not found:
            self.rtlsdr_status = "No RTL-SDR plugged in."
            if self.rtlsdrs:
                self.set_rtlsdr(None)
            return
        if not rtlsdr.available():
            self.rtlsdr_status = f"An {found[0].model} is plugged in, but {rtlsdr.why_unavailable()}"
            self.emit("explorer")
            return
        open_now = {d.device for d in self.rtlsdrs if d.alive}
        if all(usb.node in open_now for usb in found):
            return
        self.rtlsdr_probing = True
        self.rtlsdr_status = f"Opening the {found[0].model}…"

        def finished(result, error) -> None:
            self.rtlsdr_probing = False
            opened, errors = result if result else ([], [str(error)])
            self.rtlsdr_status = "; ".join(errors)
            for device in opened:
                self.set_rtlsdr(device)
            if not opened:
                self.emit("explorer")

        run_in_thread(lambda: rtlsdr.open_all(open_now), finished)

    def set_explorer(self, explorer) -> None:
        """
        Adds a serial analyzer (RF Explorer or tinySA), replacing one on the same port. None
        drops the ones that stopped working.
        """
        keep = []
        for current in self.serial_analyzers:
            gone = (not current.alive) if explorer is None else current.device == explorer.device
            if gone and current is not explorer:
                current.close()
            elif current is not explorer:
                keep.append(current)
        self.serial_analyzers = keep + ([explorer] if explorer is not None else [])
        self._choose()

    def drop_explorer(self, explorer) -> None:
        if explorer in self.serial_analyzers:
            self.serial_analyzers.remove(explorer)
            explorer.close()
        self._choose()

    def set_hackrf(self, device) -> None:
        """Adds a HackRF (replacing one with the same serial), or with None drops dead ones."""
        keep = []
        for current in self.hackrfs:
            gone = (not current.alive) if device is None else current.unit == device.unit
            if gone and current is not device:
                current.close()
            elif current is not device:
                keep.append(current)
        self.hackrfs = keep + ([device] if device is not None else [])
        self._choose()

    def drop_hackrf(self, device) -> None:
        if device in self.hackrfs:
            self.hackrfs.remove(device)
            device.close()
        self._choose()

    def _choose(self) -> None:
        """
        Which analyzer sweeps. The serial one when there is one: it reads true dBm and the
        tinySA reaches all of 6 GHz. The HackRF when it's alone, or when Setup says to prefer
        its speed. The HackRF's own jobs (identifying, radar, cellular, the tone) use it either way.
        """
        serial, device = self.serial_analyzer, self.hackrf
        prefer = bool(settings.get("hackrf_sweeps", False))
        self.explorer = device if device is not None and (serial is None or prefer) else serial
        self.emit("explorer")

    def detect_hackrf(self) -> None:
        """
        Opens every HackRF that's plugged in and not open yet, in the background. They find
        themselves, like the analyzers.
        """
        if self.hackrf_probing:
            return
        found = hackrf.plugged_in()
        if not found:
            self.hackrf_status = "No HackRF plugged in."
            if self.hackrfs:
                self.set_hackrf(None)
            else:
                self.emit("explorer")
            return
        if not hackrf.available():
            self.hackrf_status = f"A {found[0].model} is plugged in, but {hackrf.why_unavailable()}"
            self.emit("explorer")
            return
        open_now = {d.usb.node for d in self.hackrfs if d.alive}
        new = [usb for usb in found if usb.node not in open_now]
        if not new:
            return
        self.hackrf_probing = True
        self.hackrf_status = f"Opening the {new[0].model}…"

        def work():
            opened, errors = [], []
            for usb in new:
                try:
                    opened.append(hackrf.HackRf(usb))
                except Exception as error:
                    errors.append(f"{usb.model}: {error}")
            return opened, errors

        def finished(result, error) -> None:
            self.hackrf_probing = False
            opened, errors = result if result else ([], [str(error)])
            self.hackrf_status = "; ".join(errors)
            for device in opened:
                self.set_hackrf(device)
            if not opened:
                self.set_hackrf(None)

        run_in_thread(work, finished)

    def detect_explorer(self, done: Callable[[detect.Detection], None] | None = None) -> None:
        """
        Probes likely ports in the background and connects every analyzer that answers on a
        port not already open.
        """
        if self.rfe_probing:
            return
        self.rfe_probing = True
        skip = {self.gps_port} if self.gps_port else set()
        skip |= {a.device for a in self.serial_analyzers if a.alive}

        def finished(found: detect.Detection | None, error) -> None:
            self.rfe_probing = False
            found = found or detect.Detection(error=str(error) if error else "")
            if found.found or not self.serial_analyzers:
                self.rf_detection = found
            for each in found.found:
                self.set_explorer(each)
            if not found.found:
                self.emit("explorer")
            if done:
                done(found)

        run_in_thread(lambda: detect.find_all(skip), finished)


class Window(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="Wi-Fi Surveyor",
                         default_width=1100, default_height=720,
                         width_request=360, height_request=420)
        self.add_css_class("touch")
        self.session = Session()
        self._close_confirmed = False

        stack = Adw.ViewStack()
        self.stack = stack
        self.survey = SurveyView(self.session)
        self.setup = SetupView(self.session)
        self.monitor = MonitorView(self.session)
        for child, name, title, icon in (
            (self.survey, "survey", "Survey", "mark-location-symbolic"),
            (DiscoverView(self.session), "discover", "Discover", "network-wireless-symbolic"),
            (self.monitor, "monitor", "Watch", "wifisurveyor-heartbeat-symbolic"),
            (SpectrumView(self.session), "spectrum", "Spectrum", "network-cellular-signal-excellent-symbolic"),
            (FrequenciesView(self.session), "frequencies", "Frequencies", "audio-input-microphone-symbolic"),
            (CaptureView(self.session), "capture", "Capture", "media-record-symbolic"),
            (self.setup, "setup", "Setup", "preferences-system-symbolic"),
        ):
            page = stack.add_titled(child, name, title)
            page.set_icon_name(icon)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        self.fullscreen_button = Gtk.Button(icon_name="view-fullscreen-symbolic",
                                            tooltip_text="Full screen (F11)",
                                            action_name="win.fullscreen")
        header.pack_end(self.fullscreen_button)
        self.connect("notify::fullscreened", self._fullscreen_changed)

        menu = Gio.Menu()
        menu.append("New survey", "win.new")
        menu.append("Start a follow-up survey", "win.follow-up")
        menu.append("Recent surveys…", "win.recent")
        menu.append("Open survey…", "win.open")
        menu.append("Save survey", "win.save")
        menu.append("Save survey as…", "win.save-as")
        menu.append("Save a copy for the Windows app…", "win.export-windows")
        menu.append("Export report…", "win.export-report")
        menu.append("Export overview…", "win.export-overview")
        menu.append("Report details…", "win.report-details")
        menu.append("Compare with another survey…", "win.compare")
        if os.geteuid() != 0:
            menu.append("Relaunch as root", "win.root")
        menu.append("About", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        # The tabs live in the header. Only a narrow window moves them to the bottom, where
        # they'd otherwise cost a row of the map's height for nothing.
        bar = Adw.ViewSwitcherBar(stack=stack)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(stack)
        stack.set_vexpand(True)
        content.append(bar)

        # While the HackRF sends the test tone, wherever you are in the app: it's transmitting,
        # and nothing else can use the HackRF until it stops
        self.tone_banner = Adw.Banner(button_label="Stop the tone")
        self.tone_banner.connect("button-clicked", self._stop_tone)
        self._tone_was_on = False

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.tone_banner)
        view.set_content(content)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        narrow.add_setter(header, "title-widget", Adw.WindowTitle(title="Wi-Fi Surveyor"))
        narrow.add_setter(bar, "reveal", True)
        self.add_breakpoint(narrow)
        # Below this the survey's settings panel slides over the map instead of beside it
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1000sp"))
        compact.add_setter(self.survey.split, "collapsed", True)
        compact.add_setter(switcher, "policy", Adw.ViewSwitcherPolicy.NARROW)
        self.add_breakpoint(compact)

        for name, handler in (
            ("fullscreen", self.on_fullscreen),
            ("new", self.on_new), ("open", self.on_open), ("save", self.on_save),
            ("follow-up", self.on_follow_up), ("recent", lambda *_: self.show_start(False)),
            ("save-as", self.on_save_as), ("export-windows", self.on_export_windows),
            ("export-report", self.on_export_report),
            ("export-overview", lambda *_: self.on_export_report(overview_only=True)),
            ("report-details", self.on_report_details),
            ("compare", self.on_compare),
            ("about", self.on_about), ("root", self.on_root),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        application.set_accels_for_action("win.save", ["<Control>s"])
        application.set_accels_for_action("win.open", ["<Control>o"])
        application.set_accels_for_action("win.new", ["<Control>n"])
        application.set_accels_for_action("win.fullscreen", ["F11"])

        self.session.on("changed", self._update_title)
        self.session.on("project", self._update_title)
        self.connect("close-request", self._on_close_request)

        self._hardware = self._hardware_signature()
        self._rf_ports: set[str] = set()
        self.session.on("explorer", self._explorer_changed)
        GLib.timeout_add_seconds(2, self._watch_hardware)
        GLib.timeout_add_seconds(1, self._watch_tone)
        GLib.timeout_add_seconds(AUTOSAVE_SECONDS, self._autosave)
        GLib.idle_add(lambda: self.show_start(True) and False)
        GLib.idle_add(lambda: self.session.detect_explorer() and False)
        GLib.idle_add(lambda: self.session.detect_hackrf() and False)
        GLib.idle_add(lambda: self.session.detect_rtlsdr() and False)
        oui.warm()

    # ---------- fitting the screen ----------

    def fit_screen(self) -> None:
        """On a small screen, such as the tablet this is meant for, use all of it."""
        monitors = self.get_display().get_monitors()
        monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is None:
            return
        area = monitor.get_geometry()
        if area.width <= 1440 or area.height <= 900:
            self.maximize()

    def on_fullscreen(self, *_args) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _fullscreen_changed(self, *_args) -> None:
        full = self.is_fullscreen()
        self.fullscreen_button.set_icon_name("view-restore-symbolic" if full else "view-fullscreen-symbolic")
        self.fullscreen_button.set_tooltip_text("Leave full screen (F11)" if full else "Full screen (F11)")

    # ---------- title and unsaved work ----------

    def _update_title(self, *_args) -> None:
        project = self.session.project
        name = project.path.name if project.path else project.name
        self.set_title(f"{'• ' if project.modified else ''}{name} — Wi-Fi Surveyor")

    def _autosave(self) -> bool:
        if self.session.project.modified:
            try:
                write_recovery(self.session.project)
            except OSError as error:
                self.toast(f"Autosave failed: {error}")
        return True

    def show_start(self, at_launch: bool) -> None:
        """
        Recent surveys to carry on with, unsaved work to recover, or a new one. At launch it's
        offered when Setup says so (it does by default) or when there's unsaved work.
        """
        found = read_recovery()
        if at_launch and not found and not settings.get("start_chooser", True):
            return

        def work():
            return library.known()

        def done(infos, error) -> None:
            StartDialog(self, infos or [], found, error).present(self)

        run_in_thread(work, done)

    def open_path(self, path: str) -> None:
        """Opens a survey file, after unsaved work is dealt with."""
        def go() -> None:
            try:
                project = Project.load(path)
            except Exception as error:
                self.toast(f"Couldn't open it: {error}")
                return
            note = ""
            shared = library.shared_path_for(path)
            if str(shared) != str(path):
                # Saved in root's own folder, where only the root copy can open it
                project.path = shared
                project.modified = True
                note = f" It will be saved to {shared.parent}, where you and root can both open it."
            library.remember(path)
            clear_recovery()
            self.session.set_project(project)
            self.toast(f"Opened {len(project.points)} points from {Path(path).name}.{note}")

        self.confirm_discard(go)

    def watch_network(self) -> None:
        """The Watch page, from the start screen: a new survey isn't needed for it."""
        self.stack.set_visible_child_name("monitor")

    def recover(self, project) -> None:
        self.session.set_project(project)
        self.toast("Recovered. Save it to keep it.")

    def on_follow_up(self, *_args) -> None:
        """A new survey of this site: same plan and walls, no measurements yet."""
        current = self.session.project
        if not current.points and not current.plan_png:
            self.toast("Open the earlier survey of the site first, then start the follow-up from it.")
            return
        fresh = current.follow_up()

        def go() -> None:
            self.session.set_project(fresh)
            self.toast("Follow-up survey started: same plan, walls and tests, no points yet. "
                       "Its timeline shows the earlier surveys of this site.")

        self.confirm_discard(go)

    def confirm_discard(self, then: Callable[[], None]) -> None:
        """Runs ``then`` once unsaved changes are saved or knowingly thrown away."""
        project = self.session.project
        if not project.modified:
            then()
            return
        name = project.path.name if project.path else project.name
        dialog = Adw.AlertDialog(
            heading="Save changes?",
            body=f"{name} has unsaved changes ({len(project.points)} points). "
                 "They'll be lost if you don't save them.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Don't save")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def answered(_dialog, response: str) -> None:
            if response == "save":
                self._save_then(then)
            elif response == "discard":
                clear_recovery()
                then()

        dialog.connect("response", answered)
        dialog.present(self)

    def _on_close_request(self, *_args) -> bool:
        if self.monitor.running and not self._close_confirmed:
            dialog = Adw.AlertDialog(
                heading=f"Stop watching {self.monitor.monitor.target.ssid}?",
                body="Closing ends the watch. The session so far, with every incident, is kept "
                     "in its folder.")
            dialog.add_response("cancel", "Keep watching")
            dialog.add_response("quit", "Stop and close")
            dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)

            def answered(_dialog, answer: str) -> None:
                if answer == "quit":
                    self.monitor.stop_now()
                    self.close()

            dialog.connect("response", answered)
            dialog.present(self)
            return True
        if self._close_confirmed or not self.session.project.modified:
            return False

        def close() -> None:
            self._close_confirmed = True
            self.close()

        self.confirm_discard(close)
        return True       # keep the window until the answer comes back

    # ---------- hardware ----------

    def _watch_tone(self) -> bool:
        """Keeps the banner's countdown going, and says why a tone stopped if it did by itself."""
        sending = [(d, d.tone_left) for d in self.session.hackrfs]
        sending = [(d, left) for d, left in sending if left is not None]
        if sending:
            device, left = sending[0]
            minutes, seconds = divmod(int(left), 60)
            who = f" from the {device.model}" if len(self.session.hackrfs) > 1 else ""
            self.tone_banner.set_title(f"Sending the test tone at {device.tone_mhz:.0f} MHz{who}. "
                                       f"It stops by itself in {minutes}:{seconds:02d}.")
        self.tone_banner.set_revealed(bool(sending))
        if bool(sending) != self._tone_was_on:
            self._tone_was_on = bool(sending)
            self.setup._describe_hackrf()
        for device in self.session.hackrfs:
            if device.tone_note:
                self.toast(device.tone_note)
                device.tone_note = ""
        return True

    def _stop_tone(self, *_args) -> None:
        devices = [d for d in self.session.hackrfs if d.tone_left is not None]
        if devices:
            self.tone_banner.set_title("Stopping the test tone…")
            run_in_thread(lambda: [d.stop_tone() for d in devices],
                          lambda *_: (self._watch_tone(), False)[1])

    def _hardware_signature(self) -> tuple:
        # Accessibility is part of it, so granting serial access counts as a change
        ports = tuple((item.device, gps.accessible(item.device)) for item in gps.serial_devices())
        return (tuple(wifi.wireless_names()), ports, tuple(h.node for h in hackrf.plugged_in()),
                tuple(r.node for r in rtlsdr.plugged_in()))

    def _watch_hardware(self) -> bool:
        for explorer in list(self.session.serial_analyzers):
            if not explorer.alive or not os.path.exists(explorer.device):
                self.session.drop_explorer(explorer)
                self.toast(f"{explorer.name} on {explorer.device} disconnected.")
        for device in list(self.session.hackrfs):
            if not device.alive:
                self.session.drop_hackrf(device)
                self.toast(f"{device.model}: {device.failure}" if device.failure
                           else f"{device.model} disconnected.")
                if device.failure:
                    self.session.detect_hackrf()      # still plugged in: open it afresh, once
        for device in list(self.session.rtlsdrs):
            if not device.alive:
                self.session.drop_rtlsdr(device)
                self.toast(f"{device.model}: {device.failure}" if device.failure
                           else f"{device.model} disconnected.")

        signature = self._hardware_signature()
        if signature != self._hardware:
            self._hardware = signature
            wifi.interfaces(refresh=True)
            for dropdown in (*self.session.scan_dropdowns, *self.session.monitor_dropdowns):
                dropdown.refill()
            self.setup.refresh_ports()
            self.setup.autodetect()
            self.session.detect_explorer()
            self.session.detect_hackrf()
            self.session.detect_rtlsdr()
        return True

    def _explorer_changed(self) -> None:
        connected = {a.device: a for a in (*self.session.serial_analyzers, *self.session.hackrfs,
                                           *self.session.rtlsdrs) if a.alive}
        for port, device in connected.items():
            if port not in self._rf_ports:
                # A serial analyzer's model is its firmware version ("tinySA4_v1.4-…-dirty"),
                # so it goes by its name; a HackRF's or RTL-SDR's model names the unit
                serial = device in self.session.serial_analyzers
                self.toast(f"{device.name if serial else getattr(device, 'model', '') or device.name} connected"
                           + ("" if port.startswith("/dev/bus/usb") else f" on {port}") + ".")
        if not connected and self.session.rf_detection and self.session.rf_detection.denied \
                and not self._rf_ports:
            self.toast(self.session.rf_detection.message)
        self._rf_ports = set(connected)

    def on_root(self, *_args) -> None:
        if wifi.is_root():
            self.toast("Already running as root.")
            return

        def relaunch() -> None:
            self.toast("Relaunching as root…")
            try:
                self.monitor.stop_now()
                self.session.close_all()
                wifi.relaunch_as_root()
            except Exception as error:
                self.toast(f"Couldn't relaunch as root: {error}")

        # Relaunching replaces this process, so unsaved work has to be dealt with first
        self.confirm_discard(relaunch)

    # ---------- documents ----------

    def on_new(self, *_args) -> None:
        def start(name: str) -> None:
            self.session.set_project(Project(name=name))
            # Then by choosing what it measures, as thoroughly as the site needs
            self.survey.open_setup()

        # A new survey starts with its name, which saving and exporting then offer as the file's
        self.confirm_discard(lambda: self.ask_name(start))

    def ask_name(self, then: Callable[[str], None], current: str = "") -> None:
        entry = Gtk.Entry(text=current, placeholder_text="Site or client, such as Main Street Office",
                          activates_default=True, hexpand=True)
        dialog = Adw.AlertDialog(
            heading="Name the survey",
            body="Saving the survey and exporting its report use this name for the file.",
            extra_child=entry,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("next", "Next")
        dialog.set_response_appearance("next", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("next")
        dialog.set_close_response("cancel")

        def typed(*_args) -> None:
            dialog.set_response_enabled("next", bool(entry.get_text().strip()))

        def answered(_dialog, response: str) -> None:
            if response == "next":
                # A slash would make the file dialog's name a folder path
                then(entry.get_text().strip().replace("/", "-").replace("\\", "-"))

        entry.connect("changed", typed)
        typed()
        dialog.connect("response", answered)
        dialog.present(self)
        # Focused, so the on-screen keyboard opens for it on a tablet
        GLib.idle_add(lambda: entry.grab_focus() and False)

    def on_open(self, *_args) -> None:
        def choose() -> None:
            dialog = Gtk.FileDialog(title="Open survey")
            dialog.set_filters(_survey_filters())
            _set_initial_folder(dialog)
            dialog.open(self, None, self._opened)

        self.confirm_discard(choose)

    def _opened(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.open_path(file.get_path())

    def on_save(self, *_args) -> None:
        self._save_then(None)

    def _save_then(self, then: Callable[[], None] | None) -> None:
        if not self.session.project.path:
            self._save_as(then)
            return
        if self._write(self.session.project.path) and then:
            then()

    def _write(self, path) -> bool:
        try:
            saved = self.session.project.save(path)
        except Exception as error:
            self.toast(f"Couldn't save: {error}")
            return False
        clear_recovery()
        self._update_title()
        self.toast(f"Saved to {saved}")
        return True

    def on_save_as(self, *_args) -> None:
        self._save_as(None)

    def _save_as(self, then: Callable[[], None] | None) -> None:
        dialog = Gtk.FileDialog(title="Save survey", initial_name=f"{self.session.project.name}.wsurvey")
        dialog.set_filters(_survey_filters())
        _set_initial_folder(dialog)

        def chosen(dialog: Gtk.FileDialog, result) -> None:
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                return
            if self._write(file.get_path()) and then:
                then()

        dialog.save(self, None, chosen)

    def on_export_windows(self, *_args) -> None:
        stem = (self.session.project.path.stem if self.session.project.path
                else self.session.project.name)
        dialog = Gtk.FileDialog(title="Save a copy for the Windows app",
                                initial_name=f"{stem} (Windows).wsurvey")
        dialog.set_filters(_survey_filters())
        _set_initial_folder(dialog)

        def chosen(dialog: Gtk.FileDialog, result) -> None:
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                return
            try:
                self.session.project.save_for_windows(file.get_path())
                self.toast(f"Windows copy saved to {file.get_path()}")
            except Exception as error:
                self.toast(f"Couldn't save the Windows copy: {error}")

        dialog.save(self, None, chosen)

    def on_compare(self, *_args) -> None:
        """Opens an earlier survey of the same floor plan, to map what changed since."""
        dialog = Gtk.FileDialog(title="Compare with another survey")
        dialog.set_filters(_survey_filters())
        _set_initial_folder(dialog)

        def chosen(dialog: Gtk.FileDialog, result) -> None:
            try:
                path = dialog.open_finish(result).get_path()
            except GLib.Error:
                return
            try:
                baseline = Project.load(path)
            except Exception as error:
                self.toast(f"Couldn't open it: {error}")
                return
            library.remember(path)
            self.survey.compare_with(baseline)

        dialog.open(self, None, chosen)

    def on_export_report(self, *_args, overview_only: bool = False) -> None:
        project = self.session.project
        if not project.points:
            self.toast("Measure some points first: the report is about them.")
            return
        stem = project.path.stem if project.path else project.name
        kind = "overview" if overview_only else "report"
        dialog = Gtk.FileDialog(title=f"Export {kind}", initial_name=f"{stem} {kind}.html")
        web = Gtk.FileFilter(name="Web page (*.html)")
        web.add_pattern("*.html")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(web)
        dialog.set_filters(filters)
        _set_initial_folder(dialog)

        def chosen(dialog: Gtk.FileDialog, result) -> None:
            try:
                path = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            map_png = self.survey.map_png()             # drawn here, on the main loop
            profile, ssid, band, allow_dfs = self.survey.report_choices()
            snapshot = copy.copy(project)
            snapshot.points = list(project.points)
            details = report_details(project)

            def picture(point) -> bytes | None:
                if point.spectrum_png:
                    return base64.b64decode(point.spectrum_png)
                # Measured before pictures were kept, or on Windows: draw it from the sweeps
                return snapshot_png(point.spectrum.get("segments") or [], point.networks)

            def band_picture(point, band: str) -> bytes | None:
                return band_png(point.spectrum.get("segments") or [], point.networks, band)

            def done(written, error) -> None:
                self.toast(f"Couldn't write the {kind}: {error}" if error
                           else f"{kind.capitalize()} saved to {written}")

            def maps_ready(extra: dict, history: dict) -> None:
                def work() -> str:
                    if in_root_home(path):
                        raise ValueError(f"{Path(path).parent} is root's folder. Save it in your "
                                         f"own, such as {library.surveys_dir()}.")
                    Path(path).write_text(report.build_html(
                        snapshot, map_png, picture, profile, ssid, band, allow_dfs,
                        overview_only=overview_only, extra_maps=extra, details=details,
                        version=__version__, history=history, range_png=range_png,
                        band_png=band_picture),
                        encoding="utf-8")
                    hand_back(path)
                    return path

                run_in_thread(work, done)

            self.toast(f"Writing the {kind}…")

            def history_ready(history: dict) -> None:
                # The change maps compare with the survey before this one, unless the map is
                # already comparing with one chosen by hand
                self.survey.report_maps(lambda extra: maps_ready(extra, history),
                                        baseline=history.get("previous"))

            self.survey.history_for_report(history_ready)

        dialog.save(self, None, chosen)

    def on_report_details(self, *_args) -> None:
        """Who the report is for, saved with the survey. Who it's from is on the Setup page."""
        project = self.session.project
        info = project.report_info
        rows = {}
        for_group = Adw.PreferencesGroup(
            title="For", description="Saved with this survey. The company, logo and contact "
                                     "details are under Report branding on the Setup page.")
        rows["name"] = Adw.EntryRow(title="Survey name", text=project.name)
        for_group.add(rows["name"])
        for key, title in (("client", "Client"), ("site", "Site or address"),
                           ("purpose", "Purpose of the survey")):
            rows[key] = Adw.EntryRow(title=title, text=info.get(key, ""))
            for_group.add(rows[key])

        page = Adw.PreferencesPage()
        page.add(for_group)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        dialog = Adw.Dialog(title="Report details", child=view, content_width=460,
                            content_height=420)

        def closed(*_args) -> None:
            changed = False
            name = rows["name"].get_text().strip().replace("/", "-").replace("\\", "-")
            if name and name != project.name:
                project.name = name
                changed = True
            for key in ("client", "site", "purpose"):
                text = rows[key].get_text().strip()
                if info.get(key, "") != text:
                    info[key] = text
                    changed = True
            if changed:
                project.report_info = info
                self.session.changed()

        dialog.connect("closed", closed)
        dialog.present(self)

    def on_about(self, *_args) -> None:
        Adw.AboutDialog(
            application_name="Wi-Fi Surveyor",
            application_icon="org.wifisurveyor.App",
            developer_name="Tom Race",
            version=__version__,
            copyright="© 2026 Tom Race",
            comments=(
                "Wi-Fi site surveys with heatmaps, RF Explorer and tinySA spectrum analysis, "
                "802.11 capture and GPS.\n\n"
                "Scanning uses iw or NetworkManager, monitor mode uses iw, capture uses "
                "libpcap through Scapy, and none of it is bundled: they come from your "
                "distribution under their own licences."
            ),
            license_type=Gtk.License.MIT_X11,
        ).present(self)

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))


def _survey_filters() -> Gio.ListStore:
    survey = Gtk.FileFilter()
    survey.set_name("Wi-Fi Surveyor files")
    survey.add_pattern("*.wsurvey")
    any_file = Gtk.FileFilter()
    any_file.set_name("All files")
    any_file.add_pattern("*")
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(survey)
    filters.append(any_file)
    return filters


def _set_initial_folder(dialog: Gtk.FileDialog) -> None:
    """The shared surveys folder, where both you and the root copy save."""
    try:
        dialog.set_initial_folder(Gio.File.new_for_path(str(library.surveys_dir())))
    except Exception:
        pass


class StartDialog(Adw.Dialog):
    """What to do: survey a site, watch a network, or pick up a survey where you left off."""

    def __init__(self, window, infos: list, recovery, error) -> None:
        super().__init__(title="Wi-Fi Surveyor", content_width=640, content_height=640)
        self.window = window
        header = Adw.HeaderBar()
        logo = Gtk.Image.new_from_icon_name("org.wifisurveyor.App")
        logo.set_pixel_size(40)
        header.pack_start(logo)
        other = Gtk.Button(label="Open another file…")
        other.connect("clicked", lambda *_: (self.close(), window.on_open()))
        header.pack_end(other)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Start")
        for icon, title, subtitle, action in (
                ("mark-location-symbolic", "New survey",
                 "Walk the site with a floor plan, measuring at each spot, for a heat map and "
                 "a report of what to improve.", window.on_new),
                ("wifisurveyor-heartbeat-symbolic", "Watch a network",
                 "Stay with one network's access point, on both its bands if it has two, and "
                 "catch every dropout with its most likely cause. Leave it running.",
                 window.watch_network)):
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True, subtitle_lines=0)
            row.add_prefix(Gtk.Image(icon_name=icon, pixel_size=32))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _row, go=action: (self.close(), go()))
            group.add(row)
        if not wifi.is_root():
            # Watching with a monitor adapter, captures and passive scans all need it
            row = Adw.ActionRow(title="Relaunch as root",
                                subtitle="Needed for monitor mode: listening on the channel while "
                                         "watching, capturing frames, and passive scans.",
                                activatable=True, subtitle_lines=0)
            row.add_prefix(Gtk.Image(icon_name="dialog-password-symbolic", pixel_size=32))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _row: (self.close(), window.on_root()))
            group.add(row)
        page.add(group)
        if recovery:
            project, origin, when = recovery
            group = Adw.PreferencesGroup(title="Unsaved work")
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(os.path.basename(origin) if origin else project.name),
                subtitle=f"{len(project.points)} points, autosaved "
                         f"{library.when(when)}. The app closed before it was saved.")
            discard = Gtk.Button(label="Discard", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
            discard.connect("clicked", lambda *_: (clear_recovery(), page.remove(group)))
            recover = Gtk.Button(label="Recover", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
            recover.connect("clicked", lambda *_: (self.close(), window.recover(project)))
            row.add_suffix(discard)
            row.add_suffix(recover)
            group.add(row)
            page.add(group)

        group = Adw.PreferencesGroup(
            title="Recent surveys",
            description="Saved by you or by the root copy. New ones go to "
                        f"{library.surveys_dir()}, which both can open.")
        if error:
            group.add(Adw.ActionRow(title=GLib.markup_escape_text(f"Couldn't list them: {error}")))
        if not infos:
            group.add(Adw.ActionRow(title="No saved surveys found yet."))
        per_site: dict[str, int] = {}
        for info in infos:
            for key in info.sites[:1]:
                per_site[key] = per_site.get(key, 0) + 1
        for info in infos[:40]:
            count = per_site.get(info.sites[0], 1) if info.sites else 1
            details = [f"surveyed {library.when(info.surveyed_at, False)}" if info.surveyed_at else "no points",
                       f"{info.points} points"]
            if info.saved_by:
                details.append(f"saved by {info.saved_by}")
            details.append(info.folder + (" (root's folder)" if info.root_only else ""))
            if count > 1:
                details.append(f"{count} surveys of this site")
            row = Adw.ActionRow(title=GLib.markup_escape_text(info.name),
                                subtitle=GLib.markup_escape_text(" · ".join(details)),
                                activatable=True, subtitle_lines=2)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _row, path=info.path: (self.close(), window.open_path(path)))
            group.add(row)
        page.add(group)

        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        self.set_child(view)


class Application(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="org.wifisurveyor.App",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        load_style()

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = Window(self)
            window.fit_screen()
        window.present()
