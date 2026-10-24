"""What's installed, what's missing, and the licences involved."""

from __future__ import annotations

import base64
from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk

from ..net import bluetooth as bt
from ..core import settings
from ..net import capture, gps, monitor, oui, wifi
from ..core import updates
from ..rf import detect, hackrf, iq, rtlsdr
from ..rf import spectrum as spec
from .common import fit_dropdown, run_in_thread, scrolled, set_strings


class SetupView(Gtk.Box):
    def __init__(self, session) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session = session

        page = Adw.PreferencesPage()
        page.add(self._surveying_group())
        page.add(self._branding_group())
        self.checks = Adw.PreferencesGroup(
            title="This machine",
            description="Everything here comes from your distribution, under its own licence. "
                        "Nothing is bundled or installed behind your back.",
        )
        page.add(self.checks)
        self._check_rows: list[Adw.ActionRow] = []

        self.gps_group = Adw.PreferencesGroup(title="GPS")
        self.gps_ports = fit_dropdown(Gtk.DropDown.new_from_strings(
            gps.port_labels(prefer="gps") or ["no serial ports"]), chars=18)
        self.gps_ports.set_valign(Gtk.Align.CENTER)
        gps_row = Adw.ActionRow(title="NMEA serial port")
        gps_row.add_suffix(self.gps_ports)
        start_serial = Gtk.Button(label="Use", valign=Gtk.Align.CENTER)
        start_serial.connect("clicked", self.use_serial_gps)
        gps_row.add_suffix(start_serial)
        self.gps_group.add(gps_row)

        gpsd_row = Adw.ActionRow(title="gpsd", subtitle="Use the daemon if it's running")
        use_gpsd = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
        use_gpsd.connect("clicked", lambda *_: self._start(self.session.gps.start_gpsd))
        gpsd_row.add_suffix(use_gpsd)
        self.gps_group.add(gpsd_row)
        self.gps_status = Adw.ActionRow(title="Status", subtitle="off")
        self.gps_group.add(self.gps_status)
        page.add(self.gps_group)

        self.rfe_group = Adw.PreferencesGroup(
            title="Spectrum analyzer",
            description="RF Explorers and tinySA Ultra/Ultra+, found by themselves when "
                        "they're plugged in: every USB serial port that isn't the GPS is asked "
                        "whether it's one. With several, each survey point shares the sweeps "
                        "between them, each range always to the same one.",
        )
        self.rfe_ports = fit_dropdown(Gtk.DropDown.new_from_strings(
            gps.port_labels(prefer="rf") or ["no serial ports"]), chars=18)
        self.rfe_ports.set_valign(Gtk.Align.CENTER)
        rfe_row = Adw.ActionRow(title="Serial port")
        rfe_row.add_suffix(self.rfe_ports)
        connect = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
        connect.connect("clicked", self.connect_explorer)
        rfe_row.add_suffix(connect)
        self.rfe_group.add(rfe_row)
        self.rfe_status = Adw.ActionRow(title="Status", subtitle="looking for it…")
        self.grant = Gtk.Button(label="Grant serial access", valign=Gtk.Align.CENTER,
                                css_classes=["suggested-action"], visible=False)
        self.grant.set_tooltip_text(
            "Installs a udev rule so the logged-in user can open this USB serial device, "
            "now and every time it's plugged in. Asks for your password once."
        )
        self.grant.connect("clicked", self.grant_access)
        self.rfe_status.add_suffix(self.grant)
        self.rfe_group.add(self.rfe_status)
        page.add(self.rfe_group)
        page.add(self._hackrf_group())
        page.add(self._rtlsdr_group())
        page.add(self._updates_group())
        session.on("explorer", self._explorer_changed)

        vendors = Adw.PreferencesGroup(
            title="Manufacturers",
            description="Who made each access point and device, from the IEEE's register of "
                        "MAC address blocks. Downloading gets the latest, about 6 MB.",
        )
        self.oui_row = Adw.ActionRow(title="Manufacturer database", subtitle="reading…")
        self.oui_button = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
        self.oui_button.connect("clicked", self.download_oui)
        self.oui_row.add_suffix(self.oui_button)
        vendors.add(self.oui_row)
        page.add(vendors)
        run_in_thread(oui.describe, lambda text, _error: self.oui_row.set_subtitle(text or ""))

        buttons = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=12,
                          halign=Gtk.Align.START)
        refresh = Gtk.Button(label="Check again")
        refresh.connect("clicked", lambda *_: self.refresh(look=True))
        buttons.append(refresh)
        if not wifi.is_root():
            relaunch = Gtk.Button(label="Relaunch as root", css_classes=["suggested-action"])
            relaunch.connect("clicked", self.relaunch)
            buttons.append(relaunch)
        self.append(scrolled(page))
        self.append(buttons)
        self.refresh(look=False)
        GLib.idle_add(self.autodetect)
        GLib.timeout_add_seconds(1, self._poll_gps)

    def _poll_gps(self) -> bool:
        device = self._chosen_hackrf()
        # The tone stops by itself after a while: the button follows it, so it never says Stop
        # for a tone that's already off (pressing it would start another)
        self.tone_button.set_label("Stop" if device is not None and device.sending else "Start")
        if self.get_mapped():
            fix = self.session.gps.fix
            if fix is not None:
                self.gps_status.set_subtitle(f"{self.session.gps.status}: {fix.describe()}")
            else:
                self.gps_status.set_subtitle(self.session.gps.status)
        return True

    def download_oui(self, *_args) -> None:
        self.oui_button.set_sensitive(False)

        def say(text: str) -> None:
            GLib.idle_add(lambda: self.oui_row.set_subtitle(text) and False)

        def done(text, error) -> None:
            self.oui_button.set_sensitive(True)
            self.oui_row.set_subtitle(f"Download failed: {error}" if error else text)
            if not error:
                self.session.emit("scan")          # redraw what shows manufacturers

        run_in_thread(lambda: oui.download(say), done)

    def relaunch(self, *_args) -> None:
        try:
            self.session.close_all()
            wifi.relaunch_as_root()
        except Exception as error:
            self.gps_status.set_subtitle(str(error))

    def refresh_ports(self) -> None:
        set_strings(self.gps_ports, gps.port_labels(prefer="gps"), "no serial ports")
        set_strings(self.rfe_ports, gps.port_labels(prefer="rf"), "no serial ports")

    def refresh(self, look: bool = False) -> None:
        for row in self._check_rows:
            self.checks.remove(row)
        self._check_rows.clear()
        self.refresh_ports()

        rows = [
            ("Root", "yes — monitor mode and capture are available" if wifi.is_root() else
             "no — use Relaunch as root for monitor mode and capture"),
            ("iw", "found" if wifi.have("iw") else "missing — install the iw package"),
            ("ip", "found" if wifi.have("ip") else "missing — install iproute2"),
            ("NetworkManager", "found" if wifi.have("nmcli") else "missing — scanning then needs root"),
            ("Scapy", "found" if capture.available() else "missing — pip install scapy"),
            ("ping", "found — used for the connection check at each point" if wifi.have("ping")
             else "missing — sudo apt install iputils-ping (for the connection check)"),
            ("Bluetooth scan", "ready — finds interfering Bluetooth equipment"
             if bt.available() else bt.why_unavailable().rstrip(".").lower()
             .replace("btmgmt isn't installed (the bluez package)", "missing — sudo apt install bluez")),
        ]
        interfaces = wifi.interfaces(refresh=True)
        busy = monitor.interfering()
        if busy:
            rows.append(("Running", ", ".join(busy) +
                         " — released automatically when you enable monitor mode"))

        for title, subtitle in rows:
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            self.checks.add(row)
            self._check_rows.append(row)
        for interface in interfaces:
            row = self._adapter_row(interface, [i.name for i in interfaces if i.name != interface.name])
            self.checks.add(row)
            self._check_rows.append(row)
        self._poll_gps()
        if look:
            self.autodetect()
            self.session.detect_explorer()
            self.session.detect_hackrf()
            self.session.detect_rtlsdr()

    def _adapter_row(self, interface, others: list[str]) -> Adw.ActionRow:
        """An adapter, and the correction that lines its readings up with the others'."""
        row = Adw.ActionRow(
            title=f"Interface {interface.name}",
            subtitle=(f"{interface.model}, " if interface.model else f"{interface.driver or 'driver unknown'}, ")
            + interface.mode
            + (", monitor capable" if interface.monitor_capable else ", no monitor mode"),
        )
        offset = Gtk.SpinButton.new_with_range(-20, 20, 0.5)
        offset.set_digits(1)
        offset.set_value(wifi.adapter_offset(interface.name))
        offset.set_valign(Gtk.Align.CENTER)
        offset.set_tooltip_text("Signal correction in dB, added to every reading this adapter "
                                "model makes, so surveys with different adapters line up.")
        offset.connect("value-changed",
                       lambda spin: wifi.set_adapter_offset(interface.name, spin.get_value()))
        row.add_suffix(Gtk.Label(label="dB correction", css_classes=["dim-label"]))
        row.add_suffix(offset)
        if others:
            match = Gtk.Button(label=f"Match {others[0]}", valign=Gtk.Align.CENTER)
            match.set_tooltip_text(
                f"Scans with {interface.name} and {others[0]} at the same moment and sets this "
                f"correction so {interface.name} reads like {others[0]} does (with its own "
                "correction). Both need to be in normal mode, and the app running as root.")
            match.connect("clicked", lambda *_: self._match(interface.name, others[0], offset, row))
            row.add_suffix(match)
        return row

    def _match(self, device: str, reference: str, spin: Gtk.SpinButton, row: Adw.ActionRow) -> None:
        row.set_subtitle(f"Scanning with {device} and {reference} together…")

        def done(result, error) -> None:
            if error:
                row.set_subtitle(f"Couldn't match: {error}")
                return
            difference, count = result
            spin.set_value(difference + wifi.adapter_offset(reference))
            row.set_subtitle(f"Set from {count} access points both heard: {device} read "
                             f"{abs(difference):.1f} dB {'below' if difference > 0 else 'above'} {reference}.")

        run_in_thread(lambda: wifi.compare_adapters(reference, device), done)

    def autodetect(self) -> None:
        """Use gpsd or a recognised GPS without blocking the window. The analyzer finds itself."""
        if self.session.gps.status != "off":
            return

        def done(choice, error) -> None:
            if error:
                self.gps_status.set_subtitle(f"Detection failed: {error}")
                return
            if choice and self.session.gps.status == "off":
                kind, device = choice
                if kind == "gpsd":
                    self.session.gps.start_gpsd()
                    self.gps_status.set_subtitle("gpsd")
                elif device.startswith("/dev"):
                    self.session.gps.start_serial(device)
                    self.session.gps_port = device
                    _select_label(self.gps_ports, device)
                    self.gps_status.set_subtitle(f"NMEA on {device}")

        run_in_thread(gps.preferred_gps, done)

    def _explorer_changed(self) -> None:
        self._refill_hackrfs()
        self._describe_hackrf()
        self._describe_rtlsdr()
        explorer = self.session.serial_analyzer
        found = self.session.rf_detection
        self.refresh_ports()
        if explorer is not None:
            _select_label(self.rfe_ports, explorer.device)
            self.grant.set_visible(False)
            self._describe_explorer()
            return
        denied = bool(found and found.denied)
        self.grant.set_visible(denied)
        if denied:
            _select_label(self.rfe_ports, found.denied[0])
        self.rfe_status.set_subtitle(found.message if found else "not connected")

    def _describe_explorer(self) -> None:
        explorer = self.session.serial_analyzer
        if explorer is None:
            return
        config = explorer.config
        if config is None and explorer.alive:
            self.rfe_status.set_subtitle(f"connected on {explorer.device}, waiting for it to answer")
            GLib.timeout_add(700, lambda: self._describe_explorer() and False)
            return
        others = [a for a in self.session.serial_analyzers if a is not explorer and a.alive]
        self.rfe_status.set_subtitle(
            explorer.summary
            + (f", covers {config.min_mhz:.0f}-{config.max_mhz:.0f} MHz"
               if config and explorer.baud else "")
            + "".join(f". Also {a.summary}" for a in others)
        )

    def grant_access(self, *_args) -> None:
        found = self.session.rf_detection
        ports = found.denied if found and found.denied else []
        if not ports:
            return
        self.grant.set_sensitive(False)
        self.rfe_status.set_subtitle("Asking for permission…")

        def done(message, error) -> None:
            self.grant.set_sensitive(True)
            self.rfe_status.set_subtitle(str(error) if error else message)
            if not error:
                # The rule applies to the device node at once; look again straight away
                self.session.detect_explorer()

        run_in_thread(lambda: detect.grant_access(ports), done)

    def _start(self, starter) -> None:
        starter()
        self.gps_status.set_subtitle(self.session.gps.status)

    def use_serial_gps(self, *_args) -> None:
        port = gps.port_device(_selected(self.gps_ports))
        if port.startswith("/dev"):
            self.session.gps.start_serial(port)
            self.session.gps_port = port
            self.gps_status.set_subtitle(self.session.gps.status)

    def connect_explorer(self, *_args) -> None:
        port = gps.port_device(_selected(self.rfe_ports))
        if not port.startswith("/dev"):
            self.rfe_status.set_subtitle("No serial port selected.")
            return

        current = next((a for a in self.session.serial_analyzers if a.device == port), None)
        if current is not None and current.alive:
            self._describe_explorer()
            return

        def work():
            if current is not None:
                current.close()          # free the port if it's the one being reopened
            found = detect.identify(port)
            if found is None:
                raise RuntimeError(f"Nothing on {port} answered as an RF Explorer or a tinySA.")
            return found

        def done(explorer, error) -> None:
            if error:
                self.session.rf_detection = detect.Detection(
                    denied=[port] if isinstance(error, PermissionError) else [],
                    error=f"{port}: {error}",
                )
                self.session.set_explorer(None)
                return
            self.session.set_explorer(explorer)

        run_in_thread(work, done)


    # ---------- how the survey page behaves ----------

    def _surveying_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Surveying")
        self.easy_mode = Adw.SwitchRow(
            title="Easy mode",
            subtitle="Step-by-step guidance on the map for the wall test: where to put the tone "
                     "generator, how to set it up with what's connected, and what to measure "
                     "next. Also asks what each tap is.",
            active=bool(settings.get("easy_mode", False)))
        self.tap_asks = Adw.SwitchRow(
            title="Ask what each tap is",
            subtitle="Tapping the plan asks whether it's a point where you're scanning or where "
                     "the tone generator sits. Off: every tap scans.",
            active=bool(settings.get("tap_asks", False)))
        self.easy_mode.connect("notify::active", self._easy_toggled)
        self.tap_asks.connect("notify::active", self._tap_asks_toggled)
        self.start_chooser = Adw.SwitchRow(
            title="Show the start screen",
            subtitle="Opening the app offers a new survey, watching a network for dropouts, or "
                     "carrying on with a survey saved by you or the root copy.",
            active=bool(settings.get("start_chooser", True)))
        self.start_chooser.connect("notify::active",
                                   lambda row, _p: settings.put("start_chooser", row.get_active()))
        self.scan_all = Adw.SwitchRow(
            title="Scan with every Wi-Fi adapter",
            subtitle="With more than one adapter, each scans its own share of the channels at "
                     "once: faster, and each access point is read by the same adapter at every "
                     "point. The monitor-mode adapter is left free for captures. Calibrate the "
                     "adapters against each other (above) so their readings line up.",
            active=bool(settings.get("scan_all_adapters", True)))
        self.scan_all.connect("notify::active",
                              lambda row, _p: settings.put("scan_all_adapters", row.get_active()))
        group.add(self.start_chooser)
        group.add(self.scan_all)
        group.add(self.easy_mode)
        group.add(self.tap_asks)
        self._easy_toggled(self.easy_mode, None, announce=False)
        return group

    # ---------- report branding ----------

    def _branding_group(self) -> Adw.PreferencesGroup:
        """Who every report is from: shown at the top of its cover, kept on this computer."""
        group = Adw.PreferencesGroup(
            title="Report branding",
            description="At the top of every report this computer exports. Who each report is "
                        "for is in the menu, under Report details.")
        for key, title in (("company", "Company"), ("surveyor", "Surveyor"),
                           ("contact", "Phone or email")):
            row = Adw.EntryRow(title=title, text=settings.get(f"report_{key}", "") or "")
            row.connect("changed", lambda row, key=key:
                        settings.put(f"report_{key}", row.get_text().strip()))
            group.add(row)

        self.logo_row = Adw.ActionRow(title="Logo", subtitle="PNG, JPEG, SVG or WebP, up to 2 MB")
        self.logo_preview = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN,
                                        width_request=96, height_request=40,
                                        valign=Gtk.Align.CENTER)
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        self.logo_clear = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                     tooltip_text="Remove the logo")
        choose.connect("clicked", self._choose_logo)
        self.logo_clear.connect("clicked", lambda *_: (settings.put("report_logo", None),
                                                       self._show_logo()))
        self.logo_row.add_suffix(self.logo_preview)
        self.logo_row.add_suffix(choose)
        self.logo_row.add_suffix(self.logo_clear)
        group.add(self.logo_row)
        self._show_logo()
        return group

    def _show_logo(self) -> None:
        logo = settings.get("report_logo") or {}
        texture = None
        if logo.get("data"):
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(logo["data"])))
            except (GLib.Error, ValueError):
                pass                      # kept for the report even when it can't be shown here
        self.logo_preview.set_paintable(texture)
        self.logo_preview.set_visible(texture is not None)
        self.logo_clear.set_sensitive(bool(logo.get("data")))
        self.logo_row.set_subtitle("PNG, JPEG, SVG or WebP, up to 2 MB" if not logo.get("data")
                                   else "Shown at the top of the report")

    def _choose_logo(self, *_args) -> None:
        images = Gtk.FileFilter(name="Images")
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.svg", "*.webp"):
            images.add_pattern(pattern)
        chooser = Gtk.FileDialog(title="Choose a logo", default_filter=images)
        chooser.open(self.get_root(), None, self._logo_picked)

    def _logo_picked(self, dialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        window = self.get_root()
        data = Path(path).read_bytes()
        if len(data) > 2_000_000:
            window.toast("That image is over 2 MB; choose a smaller logo.")
            return
        kind = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".svg": "image/svg+xml", ".webp": "image/webp"}.get(Path(path).suffix.lower())
        if kind is None:
            window.toast("Choose a PNG, JPEG, SVG or WebP image.")
            return
        settings.put("report_logo", {"mime": kind, "data": base64.b64encode(data).decode()})
        self._show_logo()

    def _easy_toggled(self, row, _param, announce: bool = True) -> None:
        easy = row.get_active()
        if announce:
            settings.put("easy_mode", easy)
        # Easy mode always asks, so the tone generator can be placed with a tap
        self.tap_asks.set_sensitive(not easy)
        if easy:
            self.tap_asks.set_active(True)
        else:
            self.tap_asks.set_active(bool(settings.get("tap_asks", False)))
        if announce:
            self.session.emit("options")

    def _tap_asks_toggled(self, row, _param) -> None:
        if row.get_sensitive():           # not while easy mode is holding it on
            settings.put("tap_asks", row.get_active())
            self.session.emit("options")

    # ---------- the HackRF ----------

    def _hackrf_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="HackRF",
            description="Optional, alongside the analyzer. It names what's transmitting on a "
                        "channel, watches the DFS channels for radar, measures cellular bands "
                        "and sends the test tone for measuring walls. Found by itself when it's "
                        "plugged in; needs the hackrf package.")
        self.hackrf_status = Adw.ActionRow(title="Status", subtitle="looking for it…")
        group.add(self.hackrf_status)
        # With several HackRFs: which one calibration, the close look and the tone apply to.
        # The gains and antenna power apply to them all.
        self.hackrf_choice = Adw.ComboRow(title="Which HackRF", model=Gtk.StringList.new([]),
                                          visible=False)
        self.hackrf_choice.set_subtitle("Calibration, What's on a channel and the test tone "
                                        "apply to this one; the gains to all of them.")
        self.hackrf_choice.connect("notify::selected", lambda *_: self._describe_hackrf())
        self._hackrf_units: list[str] = []
        group.add(self.hackrf_choice)

        self.hackrf_sweeps = Adw.SwitchRow(
            title="Sweep with the HackRF",
            subtitle="Much faster sweeps, so short bursts aren't missed. Off: the RF Explorer or "
                     "tinySA sweeps when connected, since it reads true dBm and the tinySA "
                     "Ultra+ reaches all of 6 GHz. With nothing else connected, the HackRF "
                     "sweeps anyway.",
            active=bool(settings.get("hackrf_sweeps", False)))
        self.hackrf_sweeps.connect("notify::active", self._prefer_hackrf)
        group.add(self.hackrf_sweeps)

        lna, vga, amp = hackrf.gains()
        self.hackrf_lna = Adw.SpinRow.new_with_range(0, 40, 8)
        self.hackrf_lna.set_title("LNA gain (dB)")
        self.hackrf_lna.set_value(lna)
        self.hackrf_vga = Adw.SpinRow.new_with_range(0, 62, 2)
        self.hackrf_vga.set_title("VGA gain (dB)")
        self.hackrf_vga.set_subtitle("Lower both if a nearby access point overloads it (a flat "
                                     "top, or copies of signals elsewhere). Calibrate again "
                                     "after changing them.")
        self.hackrf_vga.set_value(vga)
        self.hackrf_amp = Adw.SwitchRow(title="Front-end amplifier",
                                        subtitle="About 11 dB more sensitive, and easier to overload",
                                        active=amp)
        for row, key in ((self.hackrf_lna, "hackrf_lna"), (self.hackrf_vga, "hackrf_vga")):
            row.connect("notify::value", lambda r, _p, k=key: self._hackrf_setting(k, int(r.get_value())))
            group.add(row)
        self.hackrf_amp.connect("notify::active",
                                lambda r, _p: self._hackrf_setting("hackrf_amp", r.get_active()))
        group.add(self.hackrf_amp)
        self.hackrf_bias = Adw.SwitchRow(
            title="Antenna power (bias tee)",
            subtitle="3.3 V on the antenna port for an active antenna or an inline amplifier. "
                     "Leave it off for a plain antenna. It's always off while sending the tone.",
            active=hackrf.bias_tee())
        self.hackrf_bias.connect("notify::active",
                                 lambda r, _p: self._hackrf_setting("hackrf_bias_tee", r.get_active()))
        group.add(self.hackrf_bias)

        self.hackrf_cal = Adw.ActionRow(title="Calibration")
        self.hackrf_offset = Gtk.SpinButton.new_with_range(-90, 30, 0.5)
        self.hackrf_offset.set_digits(1)
        self.hackrf_offset.set_value(hackrf.offset_db(unit=self._chosen_unit()))
        self.hackrf_offset.set_valign(Gtk.Align.CENTER)
        self.hackrf_offset.set_tooltip_text("dB added to every HackRF reading to make it dBm")
        self.hackrf_offset.connect("value-changed", self._offset_typed)
        self.hackrf_cal.add_suffix(self.hackrf_offset)
        self.match_card = Gtk.Button(label="Match the Wi-Fi card", valign=Gtk.Align.CENTER)
        self.match_card.set_tooltip_text(
            "Scans Wi-Fi, then sweeps 2.4 GHz and the bottom of 5 GHz, and sets the offset so the "
            "HackRF reads each strong network as the card does. Best near a strong access point.")
        self.match_card.connect("clicked", lambda *_: self._calibrate("wifi"))
        self.hackrf_cal.add_suffix(self.match_card)
        self.match_analyzer = Gtk.Button(label="Match the analyzer", valign=Gtk.Align.CENTER)
        self.match_analyzer.set_tooltip_text(
            "Sweeps the same channels with the RF Explorer or tinySA and with the HackRF, and sets "
            "the offset so the HackRF reads like the analyzer.")
        self.match_analyzer.connect("clicked", lambda *_: self._calibrate("analyzer"))
        self.hackrf_cal.add_suffix(self.match_analyzer)
        group.add(self.hackrf_cal)

        self.identify_row = Adw.ActionRow(
            title="What's on a channel",
            subtitle="Listens to one 20 MHz channel for a quarter of a second and names what's "
                     "transmitting: Wi-Fi, Bluetooth, Zigbee, a microwave oven, a carrier, radar.")
        self.identify_channel = Gtk.Entry(text="6", width_chars=5, valign=Gtk.Align.CENTER,
                                          tooltip_text="A Wi-Fi channel (1-14, 36-177) or a "
                                                       "frequency in MHz")
        self.identify_row.add_suffix(self.identify_channel)
        self.identify_button = Gtk.Button(label="Listen", valign=Gtk.Align.CENTER)
        self.identify_button.connect("clicked", self._identify)
        self.identify_row.add_suffix(self.identify_button)
        group.add(self.identify_row)

        self.keep_iq = Adw.SwitchRow(
            title="Keep raw I/Q of the unexplained",
            subtitle="",
            active=hackrf.keep_iq())
        self.keep_iq.connect("notify::active", lambda r, _p: (settings.put("hackrf_keep_iq", r.get_active()),
                                                              self._describe_recordings()))
        group.add(self.keep_iq)

        self.tone_row = Adw.ActionRow(title="Test tone", subtitle=TONE_ABOUT)
        self.tone_band = Gtk.DropDown.new_from_strings([text for _f, text in hackrf.TONES.values()])
        self.tone_band.set_valign(Gtk.Align.CENTER)
        self.tone_row.add_suffix(self.tone_band)
        self.tone_level = Gtk.SpinButton.new_with_range(0, hackrf.MAX_TX_VGA, 2)
        self.tone_level.set_value(10)
        self.tone_level.set_valign(Gtk.Align.CENTER)
        self.tone_level.set_tooltip_text("Transmit gain in dB. Raise it only if the tone isn't "
                                         "heard well above the floor across the area.")
        self.tone_row.add_suffix(self.tone_level)
        self.tone_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.tone_button.connect("clicked", self._toggle_tone)
        self.tone_row.add_suffix(self.tone_button)
        group.add(self.tone_row)
        GLib.idle_add(lambda: self._describe_hackrf() and False)
        return group

    def _chosen_hackrf(self):
        """The HackRF Setup is acting on: the one chosen, or the only one."""
        devices = self.session.hackrfs
        if len(devices) > 1 and getattr(self, "hackrf_choice", None) is not None:
            index = self.hackrf_choice.get_selected()
            if index < len(devices):
                return devices[index]
        return devices[0] if devices else None

    def _chosen_unit(self) -> str | None:
        device = self._chosen_hackrf()
        return device.unit if device is not None else None

    def _refill_hackrfs(self) -> None:
        units = [d.unit for d in self.session.hackrfs]
        if units == self._hackrf_units:
            return
        chosen = self._chosen_unit()
        self._hackrf_units = units
        self.hackrf_choice.set_model(Gtk.StringList.new([d.model for d in self.session.hackrfs]))
        if chosen in units:
            self.hackrf_choice.set_selected(units.index(chosen))
        self.hackrf_choice.set_visible(len(units) > 1)
        self.hackrf_offset.set_value(hackrf.offset_db(unit=self._chosen_unit()))

    def _describe_hackrf(self) -> None:
        device = self._chosen_hackrf()
        ready = device is not None and device.alive
        if ready:
            lines = []
            for each in self.session.hackrfs:
                sweeps = " It does the live sweeping." if self.session.explorer is each else ""
                deaf = hackrf.antenna_note(hackrf.deaf_bands(each.unit))
                when = hackrf.deaf_since(each.unit)
                lines.append(each.summary + "." + sweeps
                             + (" Sending the test tone." if each.sending else "")
                             + (f" {deaf} ({when})" if deaf else ""))
            self.hackrf_status.set_subtitle("\n".join(lines))
        else:
            self.hackrf_status.set_subtitle(self.session.hackrf_status or "not connected")
        for widget in (self.match_card, self.identify_button, self.tone_button):
            widget.set_sensitive(ready)
        self.match_analyzer.set_sensitive(ready and self.session.serial_analyzer is not None)
        self.hackrf_cal.set_subtitle(calibration_text(self._chosen_unit()))
        sending = ready and device.sending
        self.tone_button.set_label("Stop" if sending else "Start")
        if not sending and (self.tone_row.get_subtitle() or "").startswith("Sending"):
            self.tone_row.set_subtitle(TONE_ABOUT)
        self._describe_recordings()

    def _describe_recordings(self) -> None:
        count, size = hackrf.recordings_size()
        kept = f" {count} kept, {size / 1e6:.0f} MB." if count else ""
        self.keep_iq.set_subtitle(
            "When the HackRF hears something it can't name, or radar, it keeps the raw I/Q as a "
            f"SigMF recording (for inspectrum or GNU Radio) in {hackrf.recordings_dir()}. The "
            f"oldest go past {hackrf.KEEP_IQ_MB / 1000:.0f} GB.{kept}")

    def _hackrf_setting(self, key: str, value) -> None:
        """A gain or the antenna power: saved, and the HackRF sweeping now picks it up."""
        settings.put(key, value)
        for device in self.session.hackrfs:
            if device.alive:
                device.apply_settings()
        self._describe_hackrf()
        self.hackrf_offset.set_value(hackrf.offset_db(unit=self._chosen_unit()))

    def _prefer_hackrf(self, row, _param) -> None:
        settings.put("hackrf_sweeps", row.get_active())
        self.session._choose()

    def _offset_typed(self, spin: Gtk.SpinButton) -> None:
        unit = self._chosen_unit()
        if abs(spin.get_value() - hackrf.offset_db(unit=unit)) >= 0.05:
            hackrf.set_offset_by_hand(spin.get_value(), unit)
            self._describe_hackrf()

    def _calibrate(self, against: str) -> None:
        device = self._chosen_hackrf()
        if device is None:
            return
        self.match_card.set_sensitive(False)
        self.match_analyzer.set_sensitive(False)
        self.hackrf_cal.set_subtitle("Measuring… (about half a minute)")
        reference = self.session.serial_analyzer

        def work():
            if against == "analyzer":
                if reference is None:
                    raise RuntimeError("No RF Explorer or tinySA connected.")
                return hackrf.match_analyzer(device, reference)
            adapter = self.session.scan_device or wifi.choose_roles(wifi.interfaces())[0]
            if not adapter:
                raise RuntimeError("No Wi-Fi adapter to compare with.")
            networks = [{"ssid": r.ssid, "bssid": r.bssid, "rssi": r.rssi, "freq_mhz": r.freq_mhz,
                         "channel": r.channel, "band": spec.band_of(r.freq_mhz)}
                        for r in wifi.scan(adapter)]
            if any(r.get("rssi") is None for r in networks):
                raise RuntimeError("The scan had no signal levels.")
            return hackrf.match_wifi(device, networks)

        def done(result, error) -> None:
            self._describe_hackrf()
            if error:
                self.hackrf_cal.set_subtitle(f"Couldn't calibrate: {error}")
                return
            self.hackrf_offset.set_value(result.offset)
            bands = ", ".join(f"{band} GHz {value:+.1f}" for band, value in result.bands.items())
            text = (f"Matched on {result.channels} channels: the offset is now "
                    f"{result.offset:+.1f} dB" + (f" ({bands})." if bands else "."))
            if result.deaf:
                text += " " + hackrf.antenna_note(result.deaf) + " That band was left out."
            self.hackrf_cal.set_subtitle(text)

        run_in_thread(work, done)

    def _identify(self, *_args) -> None:
        device = self._chosen_hackrf()
        text = self.identify_channel.get_text().strip()
        try:
            number = float(text)
        except ValueError:
            self.identify_row.set_subtitle(f"'{text}' isn't a channel or a frequency.")
            return
        centre = number if number > 500 else spec.CHANNELS.get(int(number))
        if device is None or not centre:
            self.identify_row.set_subtitle(f"No channel {text}." if device else "No HackRF.")
            return
        self.identify_button.set_sensitive(False)
        self.identify_row.set_subtitle(f"Listening at {centre:.0f} MHz…")

        def done(result, error) -> None:
            self.identify_button.set_sensitive(True)
            if error:
                self.identify_row.set_subtitle(f"Couldn't listen: {error}")
                return
            found = "; ".join(iq.describe(f) for f in result["findings"]) or "nothing transmitting"
            extra = iq.overload_note(result.get("clipped", 0.0), "")
            if result.get("recording"):
                extra += f" Raw I/Q kept: {result['recording']}."
                self._describe_recordings()
            self.identify_row.set_subtitle(
                f"{centre:.0f} MHz, busy {result['airtime'] * 100:.0f}% of the time: {found}."
                + (f" {extra.strip()}" if extra else ""))

        run_in_thread(lambda: device.identify(centre), done)

    def _toggle_tone(self, *_args) -> None:
        device = self._chosen_hackrf()
        if device is None:
            return
        if self.tone_button.get_label() == "Stop" or device.sending:
            device.stop_tone()
            self._describe_hackrf()
            self.tone_row.set_subtitle("Stopped.")
            return
        freq = list(hackrf.TONES.values())[self.tone_band.get_selected()][0]
        level = int(self.tone_level.get_value())

        def done(_result, error) -> None:
            self._describe_hackrf()
            self.tone_row.set_subtitle(
                f"Couldn't start the tone: {error}" if error else
                f"Sending {freq:.0f} MHz at transmit gain {level} dB. Sweeping is paused until "
                f"it stops (Stop, or by itself after {hackrf.TONE_MINUTES} minutes).")

        run_in_thread(lambda: device.start_tone(freq, level), done)


    # ---------- the RTL-SDR ----------

    def _rtlsdr_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="RTL-SDR",
            description="Optional. Sweeps the bands below 1.8 GHz (wireless mics and IEMs, "
                        "sub-GHz industrial, business radio) for Frequencies, the Spectrum page "
                        "and the survey, leaving the other analyzers free. Found by itself when "
                        "it's plugged in; needs the rtl-sdr package. It can't reach Wi-Fi.")
        self.rtl_status = Adw.ActionRow(use_markup=False, title="Status", subtitle="looking for it…")
        self.rtl_status.set_subtitle_lines(8)
        self.rtl_test = Gtk.Button(label="Test reception", valign=Gtk.Align.CENTER)
        self.rtl_test.set_tooltip_text("A quick capture at 100 MHz: checks the tuner locks and "
                                       "signal reaches the dongle.")
        self.rtl_test.connect("clicked", self._rtl_test_reception)
        self.rtl_status.add_suffix(self.rtl_test)
        group.add(self.rtl_status)
        keys = list(rtlsdr.MODELS)
        self.rtl_model = Adw.ComboRow(title="Model", model=Gtk.StringList.new(list(rtlsdr.MODELS.values())))
        self.rtl_model.set_subtitle("Many RTL-SDR Blog V3s say only \"Realtek RTL2838UHIDIR\" over "
                                    "USB, like any dongle: choose yours if it's a Blog V3 or V4.")
        self.rtl_model.set_selected(keys.index(settings.get("rtlsdr_model", "auto"))
                                    if settings.get("rtlsdr_model", "auto") in keys else 0)
        self.rtl_model.connect("notify::selected", self._rtl_model_changed)
        group.add(self.rtl_model)
        self.rtl_bias = Adw.SwitchRow(
            title="Antenna power (bias tee)",
            subtitle="5 V on the antenna port of a Blog V3 or V4, for an active antenna or an "
                     "inline LNA. Leave it off for a plain antenna. Needs the RTL-SDR Blog tools.",
            active=rtlsdr.bias_tee())
        self.rtl_bias.connect("notify::active", lambda r, _p: self._rtl_setting("rtlsdr_bias_tee", r.get_active()))
        group.add(self.rtl_bias)
        self.rtl_gain = Adw.SpinRow.new_with_range(0, 49.6, 0.1)
        self.rtl_gain.set_title("Gain (dB)")
        self.rtl_gain.set_digits(1)
        self.rtl_gain.set_subtitle("Fixed rather than automatic, so readings stay comparable. "
                                   "Lower it if strong nearby transmitters make copies appear "
                                   "elsewhere; calibrate again after changing it.")
        self.rtl_gain.set_value(rtlsdr.gain())
        self.rtl_gain.connect("notify::value", lambda r, _p: self._rtl_setting("rtlsdr_gain", r.get_value()))
        group.add(self.rtl_gain)
        self.rtl_ppm = Adw.SpinRow.new_with_range(-150, 150, 1)
        self.rtl_ppm.set_title("Frequency correction (ppm)")
        self.rtl_ppm.set_subtitle("Cheap dongles' clocks are off by up to 50 ppm (25 kHz at "
                                  "500 MHz). Tune a known carrier and adjust until it reads right.")
        self.rtl_ppm.set_value(rtlsdr.ppm())
        self.rtl_ppm.connect("notify::value", lambda r, _p: self._rtl_setting("rtlsdr_ppm", int(r.get_value())))
        group.add(self.rtl_ppm)
        self.rtl_cal = Adw.ActionRow(use_markup=False, title="Calibration")
        self.rtl_offset = Gtk.SpinButton.new_with_range(-130, 30, 0.5)
        self.rtl_offset.set_digits(1)
        self.rtl_offset.set_valign(Gtk.Align.CENTER)
        self.rtl_offset.set_tooltip_text("dB added to every RTL-SDR reading to make it dBm")
        self.rtl_offset.set_value(rtlsdr.offset_db(self._rtl_unit()))
        self.rtl_offset.connect("value-changed", self._rtl_offset_typed)
        self.rtl_cal.add_suffix(self.rtl_offset)
        self.rtl_match = Gtk.Button(label="Match the analyzer", valign=Gtk.Align.CENTER)
        self.rtl_match.set_tooltip_text(
            "Sweeps FM broadcast (88-108 MHz) with a tinySA or RF Explorer and with the RTL-SDR, "
            "and sets the offset so the RTL-SDR reads the strong stations as the analyzer does.")
        self.rtl_match.connect("clicked", self._rtl_calibrate)
        self.rtl_cal.add_suffix(self.rtl_match)
        group.add(self.rtl_cal)
        GLib.idle_add(lambda: self._describe_rtlsdr() and False)
        return group

    def _rtl_model_changed(self, row, _param) -> None:
        settings.put("rtlsdr_model", list(rtlsdr.MODELS)[row.get_selected()])
        # The name and reach come from the model: open it afresh
        for device in list(self.session.rtlsdrs):
            self.session.drop_rtlsdr(device)
        self.session.detect_rtlsdr()

    def _rtl_test_reception(self, *_args) -> None:
        devices = [d for d in self.session.rtlsdrs if d.alive]
        if not devices:
            self.rtl_status.set_subtitle(self.session.rtlsdr_status or "No RTL-SDR connected.")
            return
        self.rtl_test.set_sensitive(False)
        self.rtl_status.set_subtitle("Testing reception…")

        def work():
            for device in devices:
                device.fault = device.check_reception()
            return devices

        def done(_result, error) -> None:
            self.rtl_test.set_sensitive(True)
            self.session.emit("explorer")          # a fixed or failed dongle joins or leaves
            if error:
                self.rtl_status.set_subtitle(f"Couldn't test: {error}")
            elif all(d.healthy for d in devices):
                self.rtl_status.set_subtitle("Reception OK: the tuner locks and signal reaches "
                                             "the dongle.\n" + "\n".join(d.summary + "." for d in devices))

        run_in_thread(work, done)

    def _rtl_unit(self) -> str | None:
        devices = getattr(self.session, "rtlsdrs", [])
        return devices[0].unit if devices else None

    def _describe_rtlsdr(self) -> None:
        devices = [d for d in self.session.rtlsdrs if d.alive]
        self.rtl_status.set_subtitle("\n".join(d.summary + "." for d in devices)
                                     or self.session.rtlsdr_status or "not connected")
        reference = self._rtl_reference()
        self.rtl_match.set_sensitive(bool(devices) and reference is not None)
        unit = self._rtl_unit()
        if rtlsdr.calibrated(unit):
            with_what = rtlsdr.calibrated_with(unit)
            self.rtl_cal.set_subtitle(f"Matched to {with_what}." if with_what else "Set by hand.")
        else:
            self.rtl_cal.set_subtitle("Not calibrated: RTL-SDR levels are rough estimates until "
                                      "it's matched to an analyzer that reads true dBm.")

    def _rtl_reference(self):
        """What the RTL-SDR is calibrated against: a tinySA or RF Explorer, else a calibrated HackRF."""
        serial = self.session.serial_analyzer
        if serial is not None and serial.alive:
            return serial
        return next((d for d in self.session.listening_hackrfs() if hackrf.calibrated(d.unit)), None)

    def _rtl_setting(self, key: str, value) -> None:
        settings.put(key, value)
        for device in self.session.rtlsdrs:
            if device.alive:
                device.apply_settings()
        self.rtl_offset.set_value(rtlsdr.offset_db(self._rtl_unit()))

    def _rtl_offset_typed(self, spin: Gtk.SpinButton) -> None:
        unit = self._rtl_unit()
        if abs(spin.get_value() - rtlsdr.offset_db(unit)) >= 0.05:
            rtlsdr.save_offset(spin.get_value(), None, unit)
            self._describe_rtlsdr()

    def _rtl_calibrate(self, *_args) -> None:
        devices = [d for d in self.session.rtlsdrs if d.alive]
        reference = self._rtl_reference()
        if not devices or reference is None:
            return
        self.rtl_match.set_sensitive(False)
        self.rtl_cal.set_subtitle("Sweeping FM broadcast with both… (about a minute)")

        def done(result, error) -> None:
            self._describe_rtlsdr()
            if error:
                self.rtl_cal.set_subtitle(f"Couldn't calibrate: {error}")
                return
            value, count = result
            self.rtl_offset.set_value(value)
            self.rtl_cal.set_subtitle(f"Matched on {count} bins of strong stations: the offset is "
                                      f"now {value:+.1f} dB.")

        run_in_thread(lambda: rtlsdr.match_reference(devices[0], reference, 88.0, 108.0), done)

    # ---------- software and firmware updates ----------

    def _updates_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Device software",
            description="Checks for newer firmware and drivers for each radio, and installs them "
                        "when you choose to. A check only looks; installing asks first. Needs "
                        "the internet.")
        self._update_rows: dict[str, Adw.ActionRow] = {}
        self._update_buttons: dict[str, list[Gtk.Widget]] = {}
        for key, title in (("hackrf", "HackRF"), ("rtlsdr", "RTL-SDR"), ("tinysa", "tinySA"),
                           ("rfexplorer", "RF Explorer")):
            row = Adw.ActionRow(use_markup=False, title=title, subtitle="Not checked yet.")
            row.set_subtitle_lines(6)
            check = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
            check.connect("clicked", lambda _b, k=key: self._check_updates(k))
            row.add_suffix(check)
            group.add(row)
            self._update_rows[key] = row
            self._update_buttons[key] = []
        return group

    def _device_for(self, key: str):
        from ..rf import explorer as rfe
        from ..rf import tinysa

        if key == "hackrf":
            return self._chosen_hackrf()
        if key == "rtlsdr":
            return [d for d in self.session.rtlsdrs if d.alive]
        kind = tinysa.TinySa if key == "tinysa" else rfe.RfExplorer
        return next((a for a in self.session.serial_analyzers if isinstance(a, kind) and a.alive), None)

    def _release(self, device):
        """A callback that drops a serial analyzer from the app, on the main loop, and waits."""
        import threading

        def release() -> None:
            finished = threading.Event()

            def drop() -> bool:
                self.session.drop_explorer(device)
                finished.set()
                return False

            GLib.idle_add(drop)
            finished.wait(10)

        return release

    def _check_updates(self, key: str) -> None:
        row = self._update_rows[key]
        for button in self._update_buttons[key]:
            row.remove(button)
        self._update_buttons[key] = []
        device = self._device_for(key)
        if key == "hackrf" and device is None:
            row.set_subtitle("Plug in the HackRF to check its firmware. Checking the tools only…")

        def say(text: str) -> None:
            GLib.idle_add(lambda: row.set_subtitle(text) and False)

        def work():
            if key == "hackrf":
                return updates.check_hackrf(device, say)
            if key == "rtlsdr":
                return updates.check_rtlsdr(device, say)
            if key == "tinysa":
                release = self._release(device) if device is not None else None
                return updates.check_tinysa(device, say, release)
            return updates.check_rfexplorer(device, say, self._release(device) if device else None)

        def done(status, error) -> None:
            if error:
                row.set_subtitle(f"Couldn't check: {error}")
                return
            row.set_subtitle(status.text)
            for action in status.actions:
                button = Gtk.Button(label=action.label, valign=Gtk.Align.CENTER,
                                    css_classes=["suggested-action"], tooltip_text=action.about)
                button.connect("clicked", lambda _b, a=action, k=key: self._run_update(k, a))
                row.add_suffix(button)
                self._update_buttons[key].append(button)

        run_in_thread(work, done)

    def _run_update(self, key: str, action) -> None:
        if action.confirm:
            dialog = Adw.AlertDialog(heading=action.label, body=action.confirm)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("go", "Install")
            dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, r: r == "go" and self._do_update(key, action))
            dialog.present(self.get_root())
        else:
            self._do_update(key, action)

    def _do_update(self, key: str, action) -> None:
        row = self._update_rows[key]
        for button in self._update_buttons[key]:
            button.set_sensitive(False)
        row.set_subtitle(f"{action.label}…")

        def say(text: str) -> None:
            GLib.idle_add(lambda: row.set_subtitle(text[-300:]) and False)

        def done(message, error) -> None:
            for button in self._update_buttons[key]:
                button.set_sensitive(True)
            row.set_subtitle(f"{action.label} failed: {error}" if error else message)
            # Whatever was flashed or rebuilt comes back as a new device: look for it shortly
            GLib.timeout_add_seconds(4, lambda: self.refresh(look=True) and False)

        run_in_thread(lambda: action.run(say), done)


TONE_ABOUT = ("A steady carrier for measuring walls (Survey → Planning → Test tone). It stays in "
              "the unlicensed ISM bands at a low level, never uses the amplifier, and stops by "
              f"itself after {hackrf.TONE_MINUTES} minutes.")


def calibration_text(unit: str | None = None) -> str:
    """Where a HackRF's offsets came from, and how they've followed the gains since."""
    if not hackrf.calibrated(unit):
        return ("Not calibrated: HackRF levels are rough estimates until it's matched to "
                "something that reads true dBm.")
    with_what = hackrf.calibrated_with(unit)
    text = f"Matched to {with_what}." if with_what else "Set by hand."
    bands = hackrf.band_offsets(unit)
    if bands:
        text += " Per band: " + ", ".join(f"{band} GHz {value:+.1f} dB"
                                          for band, value in bands.items()) + "."
    shift = hackrf.gain_shift(unit=unit)
    if abs(shift) >= 0.5:
        text += (f" Adjusted {-shift:+.0f} dB for the gain change since; calibrate again to be "
                 "sure.")
    return text


def _selected(dropdown: Gtk.DropDown) -> str:
    model = dropdown.get_model()
    if model is None or model.get_n_items() == 0:
        return ""
    return model.get_string(dropdown.get_selected())


def _select_label(dropdown: Gtk.DropDown, device: str) -> None:
    model = dropdown.get_model()
    if model is None or not device:
        return
    for index in range(model.get_n_items()):
        if model.get_string(index).startswith(device):
            dropdown.set_selected(index)
            return
