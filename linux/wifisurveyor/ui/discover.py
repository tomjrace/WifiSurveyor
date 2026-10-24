"""What's around, live: access points, the Wi-Fi clients talking to them, and Bluetooth devices.

Access points come from the scanning adapter every 5 s. Their signal is graded against the
survey's requirement (green meets it with room to spare, amber is close, red falls short), and
with a spectrum analyzer connected each band's noise floor is swept now and then, which gives
every access point a signal-to-noise ratio, graded the same way.

Clients need monitor mode: "Listen for clients" switches the monitor adapter over (the Alfa,
when there's a second adapter to scan with) and goes round the channels the access points are
on, hearing who sends to whom. Bluetooth devices and finder tags (AirTags and the like) are
heard from their advertisements. Everything here only listens.
"""

from __future__ import annotations

import atexit
import time

from gi.repository import GLib, Gtk

from ..core import hidden, rfplan
from ..core.colours import signal_quality, snr_quality
from ..net import bluetooth, capture, monitor, oui, trackers, wifi
from ..rf import spectrum as spec
from ..rf.analyzer import ranges_for, share_ranges
from .common import (
    Table, bind_device_dropdown, fit_dropdown, labelled, run_in_thread, scrolled, selected_device,
    status_label, toolbar,
)

NOISE_EVERY = 45            # seconds between noise-floor sweeps: a floor changes slowly
NOISE_SWEEPS = 3
CLIENT_RECENT = 300         # a client not heard for this long drops off the list
BT_RECENT = 120             # likewise a Bluetooth device (most rotate their address anyway)
BT_SECONDS = 6.0


def measure_noise(pool: list, sweeps: int = NOISE_SWEEPS) -> tuple[dict, dict, list[str]]:
    """
    Each Wi-Fi band's noise floor per 20 MHz, measured as the survey does (the tenth percentile
    of a min-hold, rf.spectrum.noise_floor_per_20mhz), the analyzers sharing the ranges.
    Returns ({band: dBm}, {band: the analyzer that measured it}, notes).
    """
    floors, sources, notes = {}, {}, []
    for analyzer, (name, start, end) in share_ranges(ranges_for(pool), pool):
        try:
            with analyzer.lock:
                pieces = analyzer.sweep_range(start, end, count=sweeps)
        except Exception as error:
            notes.append(f"{name}: {error}")
            continue
        for config, got in pieces:
            segment = spec.build_segment(config, got)
            band = spec.band_of(config.start_mhz)
            floor = spec.noise_floor_per_20mhz(segment["min"], config.rbw_khz)
            if band not in floors or floor < floors[band]:
                floors[band], sources[band] = floor, analyzer
    return floors, sources, notes


def ap_rows(readings, profile: rfplan.Profile, floors: dict | None = None,
            estimated: set | None = None, clients: dict | None = None, seen: str = "",
            names: dict | None = None) -> list[dict]:
    """
    The access point table, strongest first. Signal is graded against the requirement's
    primary level, SNR against its SNR target. ``estimated`` names the bands whose floor came
    from an uncalibrated HackRF; ``clients`` is how many each BSSID has, when listening;
    ``names`` the names recovered for hidden ones (Project.hidden_names).
    """
    floors, estimated, names = floors or {}, estimated or set(), names or {}
    rows = []
    for reading in readings:
        band = spec.band_of(reading.freq_mhz)
        floor = floors.get(band)
        snr = reading.rssi - floor if floor is not None else None
        row = {
            "ssid": (reading.ssid if not hidden.is_hidden(reading.ssid) else
                     hidden.label(names.get(reading.bssid.upper()), True)),
            "bssid": reading.bssid,
            "vendor": reading.vendor,
            "band": reading.band,
            "channel": reading.channel,
            "signal": f"{reading.rssi} dBm",
            "snr": "" if snr is None else f"{'≈' if band in estimated else ''}{snr:.0f} dB",
            "clients": "" if clients is None else clients.get(reading.bssid.lower(), 0),
            "security": reading.security or ("Secured" if reading.secured else "Open"),
            "seen": seen,
            "_rssi": reading.rssi,
            "_grade_signal": signal_quality(reading.rssi, profile.primary),
        }
        if snr is not None:
            row["_grade_snr"] = snr_quality(snr, profile.snr)
        rows.append(row)
    return sorted(rows, key=lambda r: r["_rssi"], reverse=True)


def client_rows(summary: capture.CaptureSummary, profile: rfplan.Profile, now: float,
                recent: float = CLIENT_RECENT) -> list[dict]:
    """
    Every device heard sending that isn't an access point, strongest first: what it's
    connected to, or the networks it's asking for when it's connected to none.
    """
    names = {stat.bssid: stat.ssid for stat in summary.bss}
    rows = []
    for device in summary.devices:
        if device.last_seen and now - device.last_seen > recent:
            continue
        rssi = device.avg_rssi
        # The access point it sends to most is the one it's on; a roaming client has two
        bssids = sorted(device.bssids)
        bssid = bssids[0] if bssids else ""
        if bssid:
            network = names.get(bssid) or "(hidden)"
        else:
            network = "Not connected"
        rows.append({
            "mac": device.mac.upper(),
            "vendor": oui.vendor(device.mac),
            "network": network,
            "bssid": bssid.upper() + (f" +{len(bssids) - 1}" if len(bssids) > 1 else ""),
            "signal": "" if rssi is None else f"{rssi:.0f} dBm",
            "frames": device.frames,
            "probes": ", ".join(sorted(p for p in device.probes if p.strip("\x00 "))),
            "heard": f"{max(0.0, now - device.last_seen):.0f} s ago" if device.last_seen else "",
            "_rssi": rssi if rssi is not None else -999,
            "_grade_signal": signal_quality(rssi, profile.primary) if rssi is not None else None,
        })
    return sorted(rows, key=lambda r: r["_rssi"], reverse=True)


def bt_rows(devices: dict, tags: dict, now: float, recent: float = BT_RECENT) -> list[dict]:
    """
    Bluetooth devices and finder tags heard lately, each address once, strongest first.
    ``devices`` and ``tags`` map an address to (when heard, a BtDevice or a tracker dict).
    """
    rows = {}
    for address, (when, device) in devices.items():
        if now - when > recent:
            continue
        rows[address] = {
            "name": device.name or "(no name)",
            "address": address,
            "kind": device.detail or "Bluetooth device",
            "vendor": device.vendor or ("" if device.public else "Private address"),
            "signal": f"{device.rssi} dBm",
            "fixed": "Fixed" if device.public else "Rotating",
            "heard": f"{max(0.0, now - when):.0f} s ago",
            "_rssi": device.rssi,
        }
    for address, (when, tag) in tags.items():
        if now - when > recent:
            continue
        kind = tag["label"] + (", away from its owner" if tag.get("away") else "")
        row = rows.setdefault(address, {
            "name": tag["label"], "address": address, "vendor": "",
            "fixed": "Rotating", "_rssi": tag["rssi"],
        })
        row.update(kind=kind, signal=f"{tag['rssi']} dBm", heard=f"{max(0.0, now - when):.0f} s ago",
                   _rssi=max(row["_rssi"], tag["rssi"]))
        if tag.get("away"):
            # A tag away from its owner is the one worth finding: someone else's, travelling here
            row["_grade_kind"] = "poor"
    return sorted(rows.values(), key=lambda r: r["_rssi"], reverse=True)


class DiscoverView(Gtk.Box):
    def __init__(self, session) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session = session
        self.readings: list = []
        self.seen = ""
        self.busy = False
        # The noise floor per band, when an analyzer measured it, and when
        self.floors: dict[str, float] = {}
        self.estimated: set[str] = set()
        self.noise_note = ""
        self.noise_busy = False
        self.noise_at = 0.0
        # Monitor mode: the listener, and the adapter this page switched (to switch back)
        self.listener: capture.Listener | None = None
        self.switched: str | None = None
        self.learned_note = ""                # the hidden names this listening recovered
        self.starting = False
        # Bluetooth: address → (when heard, what)
        self.bt_devices: dict[str, tuple] = {}
        self.bt_tags: dict[str, tuple] = {}
        self.bt_busy = False

        self.devices = fit_dropdown(Gtk.DropDown())
        bind_device_dropdown(self.devices, session)
        self.auto = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.status = status_label()
        scan = Gtk.Button(label="Scan now")
        scan.connect("clicked", lambda *_: self.scan())
        self.append(toolbar(labelled("Adapter", self.devices), scan,
                            labelled("Every 5 s", self.auto), self.status))

        self.pages = Gtk.Stack(vexpand=True)
        self.ap_page = self.pages.add_titled(self._ap_box(), "aps", "Access points")
        self.client_page = self.pages.add_titled(self._client_box(), "clients", "Clients")
        self.bt_page = self.pages.add_titled(self._bt_box(), "bluetooth", "Bluetooth")
        self.append(Gtk.StackSwitcher(stack=self.pages, halign=Gtk.Align.START, margin_start=12,
                                      margin_bottom=6))
        self.append(self.pages)

        self.connect("map", self._mapped)
        self.connect("unmap", lambda *_: self._stop_listening())
        atexit.register(self._restore_at_exit)
        GLib.timeout_add_seconds(5, self._tick)
        GLib.idle_add(self.scan)

    # ---------- the pages ----------

    def _note(self, text: str = "") -> Gtk.Label:
        return Gtk.Label(label=text, xalign=0, wrap=True, margin_start=12, margin_end=12,
                         margin_bottom=6, css_classes=["dim-label"])

    def _ap_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.noise_label = self._note()
        box.append(self.noise_label)
        self.table = Table([
            ("SSID", "ssid", 200), ("BSSID", "bssid", 160), ("Manufacturer", "vendor", 180),
            ("Band", "band", 70), ("Channel", "channel", 80), ("Signal", "signal", 90),
            ("SNR", "snr", 80), ("Clients", "clients", 80), ("Security", "security", 110),
            ("Seen", "seen", 90),
        ])
        box.append(scrolled(self.table))
        return box

    def _client_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.monitor_devices = fit_dropdown(Gtk.DropDown())
        bind_device_dropdown(self.monitor_devices, self.session, role="monitor")
        self.listen = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.listen.connect("notify::active", self._listen_toggled)
        self.client_status = status_label()
        box.append(toolbar(labelled("Monitor adapter", self.monitor_devices),
                           labelled("Listen for clients", self.listen), self.client_status))
        box.append(self._note(
            "Puts the monitor adapter into monitor mode and goes round the channels the access "
            "points are on, listing every phone, laptop and other device heard sending. It "
            "stops, and the adapter goes back to normal, when you leave this page."))
        self.client_table = Table([
            ("Device", "mac", 170), ("Manufacturer", "vendor", 190), ("Network", "network", 180),
            ("Access point", "bssid", 170), ("Signal", "signal", 90), ("Frames", "frames", 80),
            ("Looking for", "probes", 200), ("Heard", "heard", 90),
        ])
        box.append(scrolled(self.client_table))
        return box

    def _bt_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.bt_on = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.bt_status = status_label(bluetooth.why_unavailable())
        box.append(toolbar(labelled("Listen for Bluetooth", self.bt_on), self.bt_status))
        box.append(self._note(
            "Printers, scanners, speakers, phones, wearables and finder tags, heard from what they "
            "advertise; nothing is paired with. A tag shown in red has been away from its "
            "owner, which is the kind worth finding. Rotating addresses change every few minutes, "
            "so one device can appear more than once."))
        self.bt_table = Table([
            ("Name", "name", 190), ("Address", "address", 160), ("Kind", "kind", 200),
            ("Manufacturer", "vendor", 180), ("Signal", "signal", 90),
            ("Address type", "fixed", 110), ("Heard", "heard", 90),
        ])
        box.append(scrolled(self.bt_table))
        return box

    # ---------- the timer ----------

    def _mapped(self, *_args) -> None:
        self.devices.refill()
        self.monitor_devices.refill()
        if self.listen.get_active() and self.listener is None:
            self._start_listening()

    def _profile(self) -> rfplan.Profile:
        return rfplan.PROFILES.get(self.session.profile_key, rfplan.PROFILES["data"])

    def _device(self) -> str:
        return selected_device(self.devices)

    def _tick(self) -> bool:
        if not self.get_mapped():
            return True
        if self.auto.get_active():
            self.scan()
        self._measure_noise()
        self._scan_bluetooth()
        self._show_clients()
        return True

    # ---------- access points ----------

    def scan(self) -> bool:
        if self.busy or not self._device() or self._device() == "none":
            return False
        device = self._device()
        if self.listener is not None and self.listener.device == device:
            self.status.set_text(f"{device} is listening for clients, so the access point list "
                                 "waits until that stops (a second adapter can do both).")
            return False
        self.busy = True
        self.status.set_text("Scanning…")

        def done(readings, error) -> None:
            self.busy = False
            if error:
                self.status.set_text(f"Scan failed: {error}")
                return
            self.session.last_scan = readings
            self.session.emit("scan")
            self.readings = readings
            self.seen = GLib.DateTime.new_now_local().format("%H:%M:%S")
            self._show_aps()
            self.status.set_text(f"{len(readings)} access points, updated {self.seen}")

        run_in_thread(lambda: wifi.scan(device), done)
        return False

    def _show_aps(self) -> None:
        clients = None
        if self.listener is not None:
            clients = {}
            for stat in self.listener.summary().bss:
                clients[stat.bssid.lower()] = len(stat.clients)
        self.table.set_rows(ap_rows(self.readings, self._profile(), self.floors, self.estimated,
                                    clients, self.seen, self.session.project.hidden_names))
        self.ap_page.set_title(f"Access points ({len(self.readings)})")
        self._show_noise()

    def _show_noise(self) -> None:
        profile = self._profile()
        grading = (f"Signal and SNR are graded against the {profile.name} requirement "
                   f"(Survey → Requirement): green meets {profile.primary:.0f} dBm and "
                   f"{profile.snr:.0f} dB SNR with room to spare, amber is close, red falls short.")
        if not self.session.sweepers():
            self.noise_label.set_text(
                f"{grading} Connect a spectrum analyzer (tinySA, RF Explorer or HackRF) to see "
                "each access point's signal-to-noise ratio.")
            return
        if not self.floors:
            measured = "Measuring the noise floor with the spectrum analyzer…"
        else:
            parts = [f"{band} GHz {floor:.0f}" for band, floor in sorted(
                self.floors.items(), key=lambda item: float(item[0]))]
            measured = (f"Noise floor per 20 MHz: {', '.join(parts)} dBm, updated every "
                        f"{NOISE_EVERY} s. An analyzer's own noise can sit above the true floor, "
                        "so SNR is if anything an underestimate.")
            if self.estimated:
                measured += " ≈ marks an uncalibrated HackRF's estimate (Setup → HackRF)."
        self.noise_label.set_text(f"{grading} {measured}"
                                  + (f" {self.noise_note}" if self.noise_note else ""))

    def _measure_noise(self) -> None:
        pool = self.session.sweepers()
        if (not pool or self.noise_busy or self.session.spectrum_recording
                or time.monotonic() - self.noise_at < NOISE_EVERY):
            if not pool and self.floors:
                self.floors, self.estimated = {}, set()       # the analyzer was unplugged
                self._show_aps()
            return
        self.noise_busy = True
        self._show_noise()

        def done(result, error) -> None:
            from ..rf import hackrf as hrf

            self.noise_busy = False
            self.noise_at = time.monotonic()
            if error:
                self.noise_note = f"Couldn't measure the noise floor: {error}"
            else:
                floors, sources, notes = result
                if floors:
                    self.floors = floors
                    self.estimated = {band for band, analyzer in sources.items()
                                      if analyzer in self.session.hackrfs
                                      and not hrf.calibrated(getattr(analyzer, "unit", None))}
                self.noise_note = " ".join(notes)
            self._show_aps()

        run_in_thread(lambda: measure_noise(pool), done)

    # ---------- clients ----------

    def _listen_toggled(self, switch: Gtk.Switch, _param) -> None:
        if switch.get_active():
            self._start_listening()
        else:
            self._stop_listening()

    def _hop_channels(self, device: str):
        """The channels the access points are on, across each one's whole width."""
        def channels() -> list[tuple[float, int, float]]:
            found = {}
            for reading in self.session.last_scan:
                kept = found.get(reading.freq_mhz)
                if kept is None or reading.width_mhz > kept[1]:
                    found[reading.freq_mhz] = (reading.freq_mhz, reading.width_mhz or 20,
                                               reading.center_mhz)
            if found:
                return [found[freq] for freq in sorted(found)]
            return [(freq, 20, 0.0) for freq in monitor.hop_list(device)]
        return channels

    def _start_listening(self) -> None:
        if self.listener is not None or self.starting:
            return
        device = selected_device(self.monitor_devices)
        held = self.session.radio_holds.get(device)
        problem = ("" if device else "No adapter to listen with.") \
            or (f"{device} is in use by {held}: choose another adapter." if held else "") \
            or ("" if wifi.is_root() else "Monitor mode needs root: relaunch as root.") \
            or ("" if capture.available() else "Scapy isn't installed (pip install scapy).")
        if problem:
            self.client_status.set_text(problem)
            self.listen.set_active(False)
            return
        self.starting = True
        self.client_status.set_text(f"Switching {device} to monitor mode…")
        lines: list[str] = []

        def work():
            switched = False
            if not monitor.is_monitor(device):
                if not monitor.enable(device, None, True, lines.append):
                    raise RuntimeError(lines[-1] if lines else "it wouldn't switch")
                switched = True
            listener = capture.Listener(device, self._hop_channels(device))
            listener.start()
            return listener, switched

        def done(result, error) -> None:
            self.starting = False
            if error:
                self.client_status.set_text(f"Couldn't listen on {device}: {error}")
                self.listen.set_active(False)
                return
            self.listener, switched = result
            if switched:
                self.switched = device
            self.session.monitor_device = device
            self.session.monitor_users.add("Discover")
            if not self.listen.get_active() or not self.get_mapped():
                self._stop_listening()                # turned off, or left, meanwhile
                return
            self.client_status.set_text(f"Listening on {device}…")

        run_in_thread(work, done)

    def _stop_listening(self) -> None:
        listener, self.listener = self.listener, None
        self.session.monitor_users.discard("Discover")
        if listener is not None:
            listener.stop()
        device, self.switched = self.switched, None
        if device:
            self.client_status.set_text(f"Putting {device} back to normal…")
            run_in_thread(lambda: monitor.restore(device, lambda _line: None),
                          lambda *_: self.client_status.set_text(f"{device} is back to normal."))
        if listener is not None:
            self._show_aps()

    def _restore_at_exit(self) -> None:
        """Closing the app while listening still hands the adapter back to NetworkManager."""
        if self.listener is not None:
            self.listener.stop()
        if self.switched:
            monitor.restore(self.switched, lambda _line: None)

    def _show_clients(self) -> None:
        listener = self.listener
        if listener is None:
            return
        if not listener.running:
            self.client_status.set_text(f"Listening on {listener.device} stopped: "
                                        f"{listener.error or 'the capture ended'}")
            self.listen.set_active(False)
            return
        summary = listener.summary()
        # Hidden access points heard named name them in the survey too
        learned = hidden.learn(self.session.project, capture.hidden_names(summary))
        if learned:
            self.session.changed()
            self.learned_note = hidden.describe_learned(self.session.project, learned)
        rows = client_rows(summary, self._profile(), time.time())
        self.client_table.set_rows(rows)
        self.client_page.set_title(f"Clients ({len(rows)})")
        lap = self._hop_channels(listener.device)()
        self.client_status.set_text(
            f"Listening on {listener.device}, going round {len(lap)} channels · "
            f"{summary.frames:,} frames · {len(rows)} devices"
            + (f" · {self.learned_note}" if self.learned_note else ""))
        self._show_aps()

    # ---------- Bluetooth ----------

    def _scan_bluetooth(self) -> None:
        if self.bt_busy or not self.bt_on.get_active():
            return
        why = bluetooth.why_unavailable()
        if why:
            self.bt_status.set_text(why)
            return
        self.bt_busy = True
        self.bt_status.set_text("Listening for Bluetooth…")

        def work():
            devices = bluetooth.scan_serialised(BT_SECONDS)
            return devices, trackers.scan(BT_SECONDS)

        def done(result, error) -> None:
            self.bt_busy = False
            if error:
                self.bt_status.set_text(f"Bluetooth scan failed: {error}")
                return
            devices, tags = result
            now = time.monotonic()
            for device in devices:
                self.bt_devices[device.address] = (now, device)
            for tag in tags:
                self.bt_tags[tag["address"]] = (now, tag)
            rows = bt_rows(self.bt_devices, self.bt_tags, now)
            self.bt_table.set_rows(rows)
            self.bt_page.set_title(f"Bluetooth ({len(rows)})")
            away = sum(1 for _w, tag in self.bt_tags.values() if tag.get("away"))
            self.bt_status.set_text(
                f"{len(rows)} Bluetooth devices heard in the last {BT_RECENT // 60} minutes"
                + (f" · {len(tags)} finder tags now" if tags else "")
                + (f" · {away} away from their owner" if away else ""))

        run_in_thread(work, done)
