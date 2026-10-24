"""Everything beyond Wi-Fi: what's on the production, industrial and business bands, wireless
mic frequency coordination, the link test with a generator, and the satellites that send."""

from __future__ import annotations

import threading
import time

from gi.repository import Adw, Gdk, GLib, Gtk

from ..net import satellite as sat
from ..rf import bands as rfbands
from ..rf import coordination as coord
from ..rf import linktest, txrules
from .common import Table, run_in_thread

AUTO = "Best for each band (automatic)"
LICENCE_WORDS = {rfbands.UNLICENSED: "Unlicensed", rfbands.LICENSED: "Licensed",
                 rfbands.AVOID: "Keep out", rfbands.LISTEN: "Listen only"}
GRADE_WORDS = {"good": "Quiet", "fair": "Some activity", "poor": "Busy", "none": "—"}
CUSTOM = "Custom range"
POLL_SECONDS = 5              # how often a satellite that's sending is checked on (its lease)


def _label(analyzer) -> str:
    return getattr(analyzer, "model", "") or analyzer.name


def _reaches(analyzer, low: float, high: float) -> bool:
    return getattr(analyzer, "min_mhz", 0.0) <= low + 0.5 and high <= analyzer.max_mhz + 0.5


class FrequenciesView(Gtk.Box):
    def __init__(self, session) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session = session
        self._scan_trace = None               # the last coordination scan: rf.analyzer.Trace
        self._scan_range: tuple[float, float] | None = None
        self._link_stop: threading.Event | None = None
        self._manual: dict | None = None      # the manual tone: {"generator", "until"}

        stack = Adw.ViewStack(vexpand=True)
        for builder, name, title, icon in (
            (self._bands_page, "bands", "Band scan", "view-list-bullet-symbolic"),
            (self._mics_page, "mics", "Mic frequencies", "audio-input-microphone-symbolic"),
            (self._link_page, "link", "Link test", "network-transmit-receive-symbolic"),
            (self._satellites_page, "satellites", "Satellites", "network-wireless-symbolic"),
        ):
            page = stack.add_titled(builder(), name, title)
            page.set_icon_name(icon)
        switcher = Adw.InlineViewSwitcher(stack=stack, halign=Gtk.Align.CENTER,
                                          margin_top=8, margin_bottom=4)
        self.append(switcher)
        self.append(stack)
        session.on("explorer", self._devices_changed)
        session.on("satellites", self._devices_changed)
        GLib.idle_add(lambda: self._devices_changed() and False)
        GLib.timeout_add_seconds(POLL_SECONDS, self._keep_alive)

    # ---------- analyzers and generators to choose from ----------

    def _analyzers(self) -> list:
        return self.session.band_analyzers()

    def _generators(self) -> list:
        """Local tinySAs (as generators) and paired satellites."""
        found = [linktest.LocalGenerator(a) for a in self.session.serial_analyzers
                 if a.alive and hasattr(a, "generate")]
        found += [linktest.RemoteGenerator(s) for s in sat.all_paired()]
        return found

    def _devices_changed(self, *_args) -> None:
        names = [AUTO] + [_label(a) for a in self._analyzers()]
        for dropdown in (self.band_analyzer, self.mic_analyzer):
            _refill(dropdown, names)
        _refill(self.receiver, [_label(a) for a in self._analyzers()] or ["no analyzer here"])
        self._generator_list = self._generators()
        _refill(self.generator, [g.name for g in self._generator_list] or ["no tinySA or satellite"])
        self._refill_satellites()

    def _chosen_analyzer(self, dropdown: Gtk.DropDown):
        """None for automatic, else the analyzer picked."""
        index = dropdown.get_selected()
        pool = self._analyzers()
        return pool[index - 1] if 0 < index <= len(pool) else None

    # ================= band scan =================

    def _bands_page(self) -> Gtk.Widget:
        page = _Page()
        group = Adw.PreferencesGroup(
            title="What's on the air beyond Wi-Fi",
            description="Sweeps every band in a group and says what's there: the strongest signal, "
                        "how far it stands over the floor, how much of the band is busy, and for "
                        "the TV bands which channels carry stations. Green is quiet, amber some "
                        "activity, red busy.")
        self.band_group = Adw.ComboRow(title="Bands", model=Gtk.StringList.new(
            [name for _k, name, _a in rfbands.GROUPS] + ["Every group"]))
        self.band_group.connect("notify::selected", lambda *_: self._describe_group())
        group.add(self.band_group)
        self.band_analyzer = Gtk.DropDown.new_from_strings([AUTO])
        self.band_analyzer.set_valign(Gtk.Align.CENTER)
        row = Adw.ActionRow(use_markup=False, title="Measure with",
                            subtitle="Automatic shares the bands between every analyzer here, "
                                     "each band to one that reaches it, all at once.")
        row.add_suffix(self.band_analyzer)
        group.add(row)
        self.band_sweeps = Adw.SpinRow.new_with_range(1, 20, 1)
        self.band_sweeps.set_title("Sweeps per band")
        self.band_sweeps.set_subtitle("Peak-held: more catches more of what transmits now and then.")
        self.band_sweeps.set_value(3)
        group.add(self.band_sweeps)
        self.band_status = Adw.ActionRow(use_markup=False, title="Scan", subtitle="")
        self.band_button = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER,
                                      css_classes=["suggested-action"])
        self.band_button.connect("clicked", self._scan_bands)
        self.band_status.add_suffix(self.band_button)
        group.add(self.band_status)
        page.add(group)

        results = Adw.PreferencesGroup(title="Results")
        self.band_table = Table([("Band", "band", 260), ("Licence", "licence", 100),
                                 ("Strongest", "peak", 170), ("Over floor", "over", 90),
                                 ("Busy", "busy", 70), ("Verdict", "grade", 110),
                                 ("What to do", "advice", 420)])
        self.band_table.set_size_request(-1, 360)
        results.add(_framed(self.band_table))
        self.band_notes = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"], margin_top=6)
        results.add(self.band_notes)
        page.add(results, wide=True)
        self._describe_group()
        return page.widget()

    def _group_keys(self) -> list[str]:
        index = self.band_group.get_selected()
        keys = [key for key, _n, _a in rfbands.GROUPS]
        return keys if index >= len(keys) else [keys[index]]

    def _describe_group(self) -> None:
        index = self.band_group.get_selected()
        about = rfbands.GROUPS[index][2] if index < len(rfbands.GROUPS) else "All of the groups."
        self.band_group.set_subtitle(about)

    def _scan_bands(self, *_args) -> None:
        bands = rfbands.for_groups(self._group_keys())
        chosen = self._chosen_analyzer(self.band_analyzer)
        pool = [chosen] if chosen is not None else self._analyzers()
        if not pool:
            self.band_status.set_subtitle("Connect an RTL-SDR, HackRF, tinySA or RF Explorer first.")
            return
        sweeps = int(self.band_sweeps.get_value())
        self.band_button.set_sensitive(False)
        self.band_status.set_subtitle(f"Sweeping {len(bands)} bands…")

        def say(text: str) -> None:
            GLib.idle_add(lambda: self.band_status.set_subtitle(text) and False)

        def work():
            return measure_bands(pool, bands, sweeps, say)

        def done(result, error) -> None:
            self.band_button.set_sensitive(True)
            if error:
                self.band_status.set_subtitle(f"Couldn't scan: {error}")
                return
            found, notes = result
            rows = []
            for band in bands:
                entry = found.get(band.key)
                if entry is None:
                    continue
                over = rfbands.activity(entry)
                rows.append({"band": band.name, "licence": LICENCE_WORDS.get(band.licence, ""),
                             "peak": f"{entry['peak_dbm']:.0f} dBm at {entry['peak_mhz']:.3f}",
                             "over": f"{over:.0f} dB" if over is not None else "",
                             "busy": f"{entry['busy'] * 100:.0f}%",
                             "grade": GRADE_WORDS[rfbands.grade(entry)],
                             "advice": rfbands.advice(band, entry)})
            self.band_table.set_rows(rows)
            self.band_status.set_subtitle(f"{len(rows)} of {len(bands)} bands measured, "
                                          f"{time.strftime('%H:%M')}.")
            self.band_notes.set_text("\n".join(notes))

        run_in_thread(work, done)

    # ================= wireless mic coordination =================

    def _mics_page(self) -> Gtk.Widget:
        page = _Page()
        group = Adw.PreferencesGroup(
            title="Wireless mic and IEM frequencies",
            description="Scans your equipment's tuning range here, now, and picks frequencies "
                        "that are quiet, legal for US wireless mics, off TV stations, and clear "
                        "of each other's intermodulation. Scan where the receivers will be, with "
                        "everything else that will be on during the event switched on. The "
                        "spacing profiles are typical starting points: check your system's own "
                        "figures (Wireless Workbench, WSM) before the show.")
        self.equipment = Adw.ComboRow(title="Equipment band", model=Gtk.StringList.new(
            [name for _k, name, _l, _h in coord.EQUIPMENT] + [CUSTOM]))
        self.equipment.set_subtitle("Band edges from Shure's published charts: check the label "
                                    "on your receiver.")
        self.equipment.connect("notify::selected", lambda *_: self._equipment_changed())
        group.add(self.equipment)
        self.custom_low = Adw.SpinRow.new_with_range(25, 7125, 0.025)
        self.custom_low.set_title("From (MHz)")
        self.custom_low.set_digits(3)
        self.custom_low.set_value(470)
        self.custom_high = Adw.SpinRow.new_with_range(25, 7125, 0.025)
        self.custom_high.set_title("To (MHz)")
        self.custom_high.set_digits(3)
        self.custom_high.set_value(608)
        group.add(self.custom_low)
        group.add(self.custom_high)
        self.profile = Adw.ComboRow(title="Spacing profile", model=Gtk.StringList.new(
            [p.name for p in coord.PROFILES]))
        self.profile.connect("notify::selected", lambda *_: self._describe_profile())
        group.add(self.profile)
        self.mic_count = Adw.SpinRow.new_with_range(1, 64, 1)
        self.mic_count.set_title("How many frequencies")
        self.mic_count.set_value(8)
        group.add(self.mic_count)
        self.licensed = Adw.SwitchRow(
            title="I hold a Part 74 licence",
            subtitle="Adds the licensed-only spectrum (653-657 MHz, 941-960 MHz, 1435-1525 MHz, "
                     "169-172 MHz) where the equipment reaches it.")
        group.add(self.licensed)
        self.in_use = Adw.EntryRow(title="Already in use (MHz, separated by spaces)")
        self.in_use.set_tooltip_text("Frequencies already set on other systems in the room. New "
                                     "picks steer clear of them and their intermod products, and "
                                     "Check tests them.")
        group.add(self.in_use)
        self.mic_analyzer = Gtk.DropDown.new_from_strings([AUTO])
        self.mic_analyzer.set_valign(Gtk.Align.CENTER)
        row = Adw.ActionRow(use_markup=False, title="Scan with", subtitle="Automatic: an RTL-SDR first, then a "
                                                        "HackRF, a tinySA, an RF Explorer.")
        row.add_suffix(self.mic_analyzer)
        group.add(row)
        self.mic_passes = Adw.SpinRow.new_with_range(1, 20, 1)
        self.mic_passes.set_title("Passes")
        self.mic_passes.set_subtitle("Each pass is peak-held into the last, so a mic or IEM that "
                                     "talks now and then is caught. 25 kHz resolution.")
        self.mic_passes.set_value(3)
        group.add(self.mic_passes)
        self.mic_status = Adw.ActionRow(use_markup=False, title="Coordinate", subtitle="")
        self.mic_button = Gtk.Button(label="Scan and pick", valign=Gtk.Align.CENTER,
                                     css_classes=["suggested-action"])
        self.mic_button.connect("clicked", self._coordinate)
        self.check_button = Gtk.Button(label="Check in use", valign=Gtk.Align.CENTER)
        self.check_button.set_tooltip_text("Tests the frequencies already in use against the last "
                                           "scan (scans first if there isn't one).")
        self.check_button.connect("clicked", self._check_in_use)
        self.copy_button = Gtk.Button(label="Copy", valign=Gtk.Align.CENTER, sensitive=False)
        self.copy_button.connect("clicked", self._copy_picks)
        for button in (self.mic_button, self.check_button, self.copy_button):
            self.mic_status.add_suffix(button)
        group.add(self.mic_status)
        page.add(group)

        results = Adw.PreferencesGroup(title="Frequencies")
        self.mic_table = Table([("Frequency (MHz)", "mhz", 160), ("Level", "level", 110),
                                ("Over floor", "over", 100), ("Notes", "notes", 520)])
        self.mic_table.set_size_request(-1, 320)
        results.add(_framed(self.mic_table))
        self.mic_notes = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"], margin_top=6)
        results.add(self.mic_notes)
        page.add(results, wide=True)
        self._picks: list[float] = []
        self._equipment_changed()
        self._describe_profile()
        return page.widget()

    def _equipment_changed(self) -> None:
        custom = self.equipment.get_selected() >= len(coord.EQUIPMENT)
        self.custom_low.set_visible(custom)
        self.custom_high.set_visible(custom)

    def _describe_profile(self) -> None:
        p = coord.PROFILES[self.profile.get_selected()]
        self.profile.set_subtitle(
            f"{p.spacing_khz:.0f} kHz between transmitters; intermod guards: 2-tx 3rd "
            f"{p.imd3_khz:.0f} kHz, 2-tx 5th {p.imd5_khz:.0f} kHz, 3-tx 3rd {p.imd3x3_khz:.0f} kHz.")

    def _mic_range(self) -> tuple[float, float]:
        index = self.equipment.get_selected()
        if index < len(coord.EQUIPMENT):
            return coord.EQUIPMENT[index][2], coord.EQUIPMENT[index][3]
        low, high = self.custom_low.get_value(), self.custom_high.get_value()
        return min(low, high), max(low, high)

    def _mic_scanner(self, low: float, high: float):
        chosen = self._chosen_analyzer(self.mic_analyzer)
        if chosen is not None:
            return chosen
        return next((a for a in self._analyzers() if _reaches(a, low, high)), None)

    def _scan_then(self, then) -> None:
        """Scans the equipment range (or reuses the last scan of it), then calls ``then``."""
        low, high = self._mic_range()
        if high - low < 0.5:
            self.mic_status.set_subtitle("The range is too narrow.")
            return
        scanner = self._mic_scanner(low, high)
        if scanner is None:
            self.mic_status.set_subtitle(f"Nothing here reaches {low:.0f}-{high:.0f} MHz. An "
                                         "RTL-SDR, HackRF or tinySA does.")
            return
        passes = int(self.mic_passes.get_value())
        for button in (self.mic_button, self.check_button):
            button.set_sensitive(False)
        self.mic_status.set_subtitle(f"Scanning {low:.0f}-{high:.0f} MHz with the {_label(scanner)}, "
                                     f"{passes} pass{'es' if passes != 1 else ''}…")

        def done(trace, error) -> None:
            for button in (self.mic_button, self.check_button):
                button.set_sensitive(True)
            if error:
                self.mic_status.set_subtitle(f"Couldn't scan: {error}")
                return
            self._scan_trace, self._scan_range = trace, (low, high)
            then(trace, low, high, _label(scanner))

        run_in_thread(lambda: scanner.scan(low, high, coord.STEP_KHZ, passes, timeout=60), done)

    def _in_use(self) -> list[float] | None:
        try:
            return coord.parse_frequencies(self.in_use.get_text())
        except ValueError as error:
            self.mic_status.set_subtitle(str(error))
            return None

    def _coordinate(self, *_args) -> None:
        fixed = self._in_use()
        if fixed is None:
            return

        def pick(trace, low, high, scanner) -> None:
            profile = coord.PROFILES[self.profile.get_selected()]
            plan = coord.coordinate(trace.freqs, trace.peak, low, high, int(self.mic_count.get_value()),
                                    profile, fixed, self.licensed.get_active())
            self._picks = [p["mhz"] for p in plan.picks]
            channels = rfbands.tv_channels(0, 1000)      # every channel, even one the range cuts
            rows = []
            for p in plan.picks:
                tv = next((n for n, a, b in channels if a <= p["mhz"] < b), None)
                rows.append({"mhz": f"{p['mhz']:.3f}", "level": f"{p['level_dbm']:.0f} dBm",
                             "over": f"{p['over_floor_db']:.0f} dB",
                             "notes": f"TV channel {tv}" if tv else ""})
            rows += [{"mhz": f"{f:.3f}", "level": "", "over": "", "notes": "already in use (kept)"}
                     for f in fixed]
            self.mic_table.set_rows(sorted(rows, key=lambda r: float(r["mhz"])))
            self.copy_button.set_sensitive(bool(self._picks))
            floor = f" Floor {plan.floor_dbm:.0f} dBm." if plan.floor_dbm is not None else ""
            self.mic_status.set_subtitle(f"{plan.summary()} Scanned with the {scanner}.{floor}")
            self.mic_notes.set_text("\n".join(plan.notes + [_rough(trace, scanner)]))

        self._scan_then(pick)

    def _check_in_use(self, *_args) -> None:
        existing = self._in_use()
        if not existing:
            if existing is not None:
                self.mic_status.set_subtitle("Type the frequencies in use first.")
            return

        def check(trace, _low, _high, scanner) -> None:
            profile = coord.PROFILES[self.profile.get_selected()]
            results = coord.check(existing, trace.freqs, trace.peak, profile)
            rows = [{"mhz": f"{r['mhz']:.3f}",
                     "level": f"{r['level_dbm']:.0f} dBm" if r["level_dbm"] is not None else "",
                     "over": "", "notes": "; ".join(r["problems"]) or "No problems found"}
                    for r in results]
            self.mic_table.set_rows(rows)
            bad = sum(1 for r in results if r["problems"])
            self.mic_status.set_subtitle(
                (f"{bad} of {len(results)} need changing." if bad else
                 f"All {len(results)} look clear.") + f" Checked against a scan with the {scanner}.")

        low, high = self._mic_range()
        if self._scan_trace is not None and self._scan_range == (low, high):
            check(self._scan_trace, low, high, "last scan")
        else:
            self._scan_then(check)

    def _copy_picks(self, *_args) -> None:
        text = "\n".join(f"{f:.3f}" for f in self._picks)
        Gdk.Display.get_default().get_clipboard().set(text)
        self.mic_status.set_subtitle(f"Copied {len(self._picks)} frequencies.")

    # ================= link test =================

    def _link_page(self) -> Gtk.Widget:
        page = _Page()
        group = Adw.PreferencesGroup(
            title="Link test",
            description="A tinySA sends at each frequency in turn, from this tablet or from a "
                        "satellite, while an analyzer here measures it: the level heard, the "
                        "margin over the floor and the path loss, per frequency. "
                        + txrules.describe())
        self.generator = Gtk.DropDown.new_from_strings(["no tinySA or satellite"])
        self.generator.set_valign(Gtk.Align.CENTER)
        row = Adw.ActionRow(use_markup=False, title="Generator", subtitle="A tinySA plugged in here, or a paired "
                                                        "satellite (Satellites tab).")
        row.add_suffix(self.generator)
        group.add(row)
        self.receiver = Gtk.DropDown.new_from_strings(["no analyzer here"])
        self.receiver.set_valign(Gtk.Align.CENTER)
        row = Adw.ActionRow(use_markup=False, title="Receiver", subtitle="Any other analyzer here that reaches the "
                                                       "frequencies.")
        row.add_suffix(self.receiver)
        group.add(row)
        self.link_low = Adw.SpinRow.new_with_range(0.1, 6000, 0.1)
        self.link_low.set_title("From (MHz)")
        self.link_low.set_digits(3)
        self.link_low.set_value(903)
        self.link_high = Adw.SpinRow.new_with_range(0.1, 6000, 0.1)
        self.link_high.set_title("To (MHz)")
        self.link_high.set_digits(3)
        self.link_high.set_value(927)
        self.link_steps = Adw.SpinRow.new_with_range(1, 100, 1)
        self.link_steps.set_title("Frequencies")
        self.link_steps.set_value(8)
        self.link_level = Adw.SpinRow.new_with_range(-90, 0, 1)
        self.link_level.set_title("Level (dBm)")
        self.link_level.set_value(-30)
        self.cabled = Adw.SwitchRow(
            title="Cabled",
            subtitle="The generator is connected to the receiver by cable, through an attenuator "
                     "(or into a shielded box). Allows any frequency, up to 0 dBm. Never with an "
                     "antenna on.")
        self.cabled.connect("notify::active", lambda *_: self._cabled_changed())
        for widget in (self.link_low, self.link_high, self.link_steps, self.link_level, self.cabled):
            group.add(widget)
        presets = Adw.ActionRow(use_markup=False, title="Over-the-air bands")
        for name, low, high in (("900 MHz", 902.5, 927.5), ("2.4 GHz", 2400.5, 2483.0)):
            button = Gtk.Button(label=name, valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _b, a=low, b=high: (self.link_low.set_value(a),
                                                                self.link_high.set_value(b)))
            presets.add_suffix(button)
        group.add(presets)
        self.link_status = Adw.ActionRow(use_markup=False, title="Test", subtitle="")
        self.link_button = Gtk.Button(label="Run", valign=Gtk.Align.CENTER,
                                      css_classes=["suggested-action"])
        self.link_button.connect("clicked", self._toggle_link)
        self.link_status.add_suffix(self.link_button)
        group.add(self.link_status)
        page.add(group)

        manual = Adw.PreferencesGroup(
            title="Steady signal",
            description="Leaves the generator on one frequency so you can walk with the Spectrum "
                        f"page or a receiver. It stops by itself after {txrules.TX_MINUTES} minutes; "
                        "a satellite also stops if this tablet stops checking in.")
        self.manual_freq = Adw.SpinRow.new_with_range(0.1, 6000, 0.1)
        self.manual_freq.set_title("Frequency (MHz)")
        self.manual_freq.set_digits(3)
        self.manual_freq.set_value(915)
        manual.add(self.manual_freq)
        self.manual_row = Adw.ActionRow(use_markup=False, title="Signal", subtitle="Off")
        self.manual_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.manual_button.connect("clicked", self._toggle_manual)
        self.manual_row.add_suffix(self.manual_button)
        manual.add(self.manual_row)
        page.add(manual)

        results = Adw.PreferencesGroup(title="Results")
        self.link_table = Table([("Frequency (MHz)", "mhz", 150), ("Sent", "sent", 90),
                                 ("Heard", "heard", 100), ("Floor", "floor", 100),
                                 ("Margin", "margin", 90), ("Path loss", "loss", 100),
                                 ("Note", "note", 300)])
        self.link_table.set_size_request(-1, 300)
        results.add(_framed(self.link_table))
        page.add(results, wide=True)
        return page.widget()

    def _cabled_changed(self) -> None:
        self.link_level.set_range(-90, txrules.MAX_CABLED_DBM if self.cabled.get_active()
                                  else txrules.MAX_OTA_DBM)

    def _chosen_generator(self):
        index = self.generator.get_selected()
        found = getattr(self, "_generator_list", [])
        return found[index] if 0 <= index < len(found) else None

    def _chosen_receiver(self, generator):
        pool = self._analyzers()
        index = self.receiver.get_selected()
        receiver = pool[index] if 0 <= index < len(pool) else None
        if isinstance(generator, linktest.LocalGenerator) and receiver is generator.device:
            return None
        return receiver

    def _toggle_link(self, *_args) -> None:
        if self._link_stop is not None:
            self._link_stop.set()
            self.link_status.set_subtitle("Stopping…")
            return
        generator = self._chosen_generator()
        receiver = self._chosen_receiver(generator)
        if generator is None:
            self.link_status.set_subtitle("Plug in a tinySA, or pair a satellite, to send.")
            return
        if receiver is None:
            self.link_status.set_subtitle("Choose a receiver here other than the generator.")
            return
        low, high = sorted((self.link_low.get_value(), self.link_high.get_value()))
        freqs = linktest.frequencies(low, high, int(self.link_steps.get_value()))
        level, cabled = self.link_level.get_value(), self.cabled.get_active()
        for freq in freqs:
            trouble = txrules.check(freq, level, cabled)
            if trouble:
                self.link_status.set_subtitle(trouble)
                return
        self._link_stop = stop = threading.Event()
        self.link_button.set_label("Stop")
        rows: list[dict] = []
        self.link_table.set_rows(rows)

        def progress(step, index, total) -> None:
            row = {"mhz": f"{step.freq_mhz:.3f}", "sent": f"{step.sent_dbm:.0f} dBm",
                   "heard": f"{step.heard_dbm:.1f} dBm" if step.heard_dbm is not None else "not heard",
                   "floor": f"{step.floor_dbm:.0f} dBm" if step.floor_dbm is not None else "",
                   "margin": f"{step.margin_db:.0f} dB" if step.margin_db is not None else "",
                   "loss": f"{step.loss_db:.0f} dB" if step.loss_db is not None else "",
                   "note": step.error}

            def show() -> bool:
                rows.append(row)
                self.link_table.set_rows(rows)
                self.link_status.set_subtitle(f"{index + 1} of {total} done…")
                return False

            GLib.idle_add(show)

        def done(result, error) -> None:
            self._link_stop = None
            self.link_button.set_label("Run")
            self.link_status.set_subtitle(f"Couldn't run it: {error}" if error else result.summary())
            self.session.emit("explorer")       # a local tinySA is an analyzer again

        self.link_status.set_subtitle(f"Sending with {generator.name}, listening with "
                                      f"{_label(receiver)}…")
        run_in_thread(lambda: linktest.run(generator, receiver, freqs, level, cabled, stop, progress),
                      done)

    def _toggle_manual(self, *_args) -> None:
        if self._manual is not None:
            generator = self._manual["generator"]
            self._manual = None
            self.manual_button.set_label("Start")
            run_in_thread(generator.stop, lambda _r, e: self.manual_row.set_subtitle(
                f"Couldn't stop it: {e}" if e else "Off"))
            return
        generator = self._chosen_generator()
        if generator is None:
            self.manual_row.set_subtitle("Plug in a tinySA, or pair a satellite, to send.")
            return
        freq, level, cabled = self.manual_freq.get_value(), self.link_level.get_value(), self.cabled.get_active()
        trouble = txrules.check(freq, level, cabled)
        if trouble:
            self.manual_row.set_subtitle(trouble)
            return

        def done(_result, error) -> None:
            if error:
                self.manual_row.set_subtitle(f"Couldn't start: {error}")
                return
            self._manual = {"generator": generator, "until": time.time() + txrules.TX_MINUTES * 60,
                            "freq": freq}
            self.manual_button.set_label("Stop")
            self.manual_row.set_subtitle(f"Sending {freq:.3f} MHz at {level:.0f} dBm from {generator.name}.")

        if isinstance(generator, linktest.RemoteGenerator):
            work = lambda: generator.satellite.tone(freq, level, cabled)   # noqa: E731
        else:
            work = lambda: generator.start(freq, level, cabled)            # noqa: E731
        run_in_thread(work, done)

    def _keep_alive(self) -> bool:
        """While a steady signal is on: renews a satellite's lease, and ends it on time."""
        manual = self._manual
        if manual is None:
            return True
        generator = manual["generator"]
        left = manual["until"] - time.time()
        if left <= 0:
            self._toggle_manual()
            self.manual_row.set_subtitle(f"Turned off after {txrules.TX_MINUTES} minutes.")
            return True
        if isinstance(generator, linktest.RemoteGenerator):
            def done(status, error) -> None:
                if self._manual is not manual:
                    return
                if error:
                    self.manual_row.set_subtitle(f"Lost touch with {generator.name}: {error}. It "
                                                 f"stops by itself within {txrules.LEASE_SECONDS} s.")
                elif not (status or {}).get("sending"):
                    self._manual = None
                    self.manual_button.set_label("Start")
                    self.manual_row.set_subtitle((status or {}).get("note") or "It stopped.")
                else:
                    minutes, seconds = divmod(int(left), 60)
                    self.manual_row.set_subtitle(f"Sending {manual['freq']:.3f} MHz from "
                                                 f"{generator.name}; off in {minutes}:{seconds:02d}.")

            run_in_thread(generator.satellite.status, done)
        return True

    # ================= satellites =================

    def _satellites_page(self) -> Gtk.Widget:
        from pathlib import Path

        folder = Path(__file__).resolve().parent.parent.parent / "satellite"
        page = _Page()
        about = Adw.PreferencesGroup(
            title="Satellites",
            description="A satellite is a tinySA on a Raspberry Pi (a Pi Zero 2 W is plenty) "
                        "elsewhere on the network. This tablet tells it what to send, and measures "
                        "it here, so a band can be tested end to end as you walk. To set one up, "
                        f"copy the folder {folder} to the Pi and run: sh install.sh — it prints a "
                        "pairing code. Commands are signed with that code; nothing else can make "
                        "it transmit.")
        find = Adw.ActionRow(use_markup=False, title="On this network", subtitle="Press Find to look.")
        self.find_button = Gtk.Button(label="Find", valign=Gtk.Align.CENTER,
                                      css_classes=["suggested-action"])
        self.find_button.connect("clicked", self._find_satellites)
        find.add_suffix(self.find_button)
        self.find_row = find
        about.add(find)
        self.address = Adw.EntryRow(title="Or its address (host or host:port)")
        add = Gtk.Button(label="Pair", valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda *_: self._pair_address())
        self.address.add_suffix(add)
        about.add(self.address)
        page.add(about)
        self.found_group = Adw.PreferencesGroup(title="Found")
        self._found_rows: list[Gtk.Widget] = []
        page.add(self.found_group)
        self.paired_group = Adw.PreferencesGroup(title="Paired")
        self._paired_rows: list[Gtk.Widget] = []
        page.add(self.paired_group)
        rules = Adw.PreferencesGroup(title="What a satellite may send", description=txrules.describe()
                                     + f" It also stops when this tablet hasn't checked in for "
                                       f"{txrules.LEASE_SECONDS} seconds.")
        page.add(rules)
        return page.widget()

    def _refill_satellites(self) -> None:
        if not hasattr(self, "paired_group"):
            return
        for row in self._paired_rows:
            self.paired_group.remove(row)
        self._paired_rows = []
        for paired in sat.saved():
            row = Adw.ActionRow(use_markup=False, title=paired.name, subtitle=f"{paired.host}:{paired.port}")
            check = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
            check.connect("clicked", lambda _b, p=paired, r=row: self._check_satellite(p, r))
            forget = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
            forget.connect("clicked", lambda _b, p=paired: self._forget(p))
            row.add_suffix(check)
            row.add_suffix(forget)
            self.paired_group.add(row)
            self._paired_rows.append(row)
        if not self._paired_rows:
            row = Adw.ActionRow(use_markup=False, title="None yet", subtitle="Find one above and pair it with its code.")
            self.paired_group.add(row)
            self._paired_rows.append(row)

    def _find_satellites(self, *_args) -> None:
        self.find_button.set_sensitive(False)
        self.find_row.set_subtitle("Looking…")

        def done(found, error) -> None:
            self.find_button.set_sensitive(True)
            for row in self._found_rows:
                self.found_group.remove(row)
            self._found_rows = []
            if error:
                self.find_row.set_subtitle(f"Couldn't look: {error}")
                return
            self.find_row.set_subtitle(f"{len(found)} found." if found else
                                       "None answered. Is it on the same network, and running? "
                                       "Or type its address below.")
            for info in found:
                generator = info.get("generator") or {}
                row = Adw.ActionRow(use_markup=False, title=info.get("name", info["host"]),
                                    subtitle=f"{info['host']}:{info.get('port', sat.PORT)} — "
                                             + (generator.get("model") or info.get("problem") or "no tinySA"))
                pair = Gtk.Button(label="Pair", valign=Gtk.Align.CENTER)
                pair.connect("clicked", lambda _b, i=info: self._ask_code(
                    i.get("name", i["host"]), i["host"], int(i.get("port", sat.PORT))))
                row.add_suffix(pair)
                self.found_group.add(row)
                self._found_rows.append(row)

        run_in_thread(sat.discover, done)

    def _pair_address(self) -> None:
        text = self.address.get_text().strip()
        if not text:
            return
        host, _, port = text.partition(":")
        try:
            number = int(port) if port else sat.PORT
        except ValueError:
            self.find_row.set_subtitle(f"'{port}' isn't a port number.")
            return
        self._ask_code(host, host, number)

    def _ask_code(self, name: str, host: str, port: int) -> None:
        dialog = Adw.AlertDialog(heading=f"Pair with {name}",
                                 body="Type the pairing code the satellite printed (install.sh "
                                      "shows it; on the Pi, wifisurveyor_satellite.py --show-code).")
        entry = Gtk.Entry(placeholder_text="XXXX-XXXX-XXXX", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("pair", "Pair")
        dialog.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("pair")
        dialog.set_close_response("cancel")

        def answered(_dialog, response: str) -> None:
            if response != "pair":
                return
            paired = sat.Paired(name, host, port, entry.get_text().strip())
            self.find_row.set_subtitle(f"Pairing with {name}…")

            def done(status, error) -> None:
                if error:
                    self.find_row.set_subtitle(f"Couldn't pair: {error}")
                    return
                paired.name = status.get("name") or name
                sat.remember(paired)
                generator = (status.get("generator") or {}).get("model") or "no tinySA attached yet"
                self.find_row.set_subtitle(f"Paired with {paired.name} ({generator}).")
                self.session.emit("satellites")

            run_in_thread(lambda: sat.Satellite(paired).status(), done)

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    def _check_satellite(self, paired, row) -> None:
        row.set_subtitle(f"{paired.host}:{paired.port} — checking…")

        def done(status, error) -> None:
            if error:
                row.set_subtitle(f"{paired.host}:{paired.port} — {error}")
                return
            generator = status.get("generator") or {}
            sending = status.get("sending")
            row.set_subtitle(f"{paired.host}:{paired.port} — "
                             + (generator.get("model", "") + f" {generator.get('version', '')}".rstrip()
                                if generator else status.get("problem") or "no tinySA")
                             + (f"; sending {sending['freq_mhz']:.3f} MHz" if sending else "; idle"))

        run_in_thread(lambda: sat.Satellite(paired).status(), done)

    def _forget(self, paired) -> None:
        sat.forget(paired.host, paired.port)
        self.session.emit("satellites")


def measure_bands(pool: list, bands, sweeps: int, say=lambda _t: None):
    """
    Sweeps the bands with the pool, shared out (rf.bands.share_bands) and all at once.
    Returns ({band key: summary}, notes).
    """
    jobs = rfbands.share_bands(bands, pool)
    found: dict = {}
    notes = [f"{b.name}: nothing here reaches it." for b in rfbands.out_of_reach(bands, pool)]
    lock = threading.Lock()

    def run(analyzer, mine) -> None:
        for band in mine:
            say(f"{_label(analyzer)}: {band.name}…")
            results, problems = rfbands.scan_bands(analyzer, [band], sweeps)
            with lock:
                found.update(results)
                notes.extend(problems)

    threads = [threading.Thread(target=run, args=job, daemon=True) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return found, notes


def _rough(_trace, scanner: str) -> str:
    if "RTL-SDR" in scanner or "HackRF" in scanner:
        return (f"Levels from the {scanner} are relative until it's calibrated (Setup); the "
                "picks only depend on how quiet each frequency is compared with the rest, so "
                "they hold either way.")
    return ""


class _Page:
    """
    A scrolling page of preference groups: the settings in a readable column, and the result
    tables (``wide``) across the whole width, which a tablet in landscape has plenty of.
    """

    def __init__(self) -> None:
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=12,
                           margin_bottom=24, margin_start=12, margin_end=12)

    def add(self, group: Gtk.Widget, wide: bool = False) -> None:
        self.box.append(Adw.Clamp(maximum_size=1600 if wide else 860, child=group))

    def widget(self) -> Gtk.Widget:
        window = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        window.set_child(self.box)
        return window


def _framed(widget: Gtk.Widget) -> Gtk.Widget:
    window = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["card"])
    window.set_child(widget)
    window.set_size_request(-1, widget.get_size_request()[1])
    return window


def _refill(dropdown: Gtk.DropDown, names: list[str]) -> None:
    model = dropdown.get_model()
    current = model.get_string(dropdown.get_selected()) if model is not None and model.get_n_items() else ""
    dropdown.set_model(Gtk.StringList.new(names))
    if current in names:
        dropdown.set_selected(names.index(current))
