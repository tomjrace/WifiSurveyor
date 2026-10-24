"""Monitor mode and 802.11 capture."""

from __future__ import annotations

import time

from gi.repository import GLib, Gtk

from ..core import hidden
from ..core.project import data_dir, make_dir
from ..net import capture, monitor, oui, wifi
from .common import (
    Table, bind_device_dropdown, fit_dropdown, labelled, run_in_thread, scrolled, selected_device, toolbar,
)


class CaptureView(Gtk.Box):
    def __init__(self, session) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session = session
        self.summary: capture.CaptureSummary | None = None
        self.live: capture.LiveCapture | None = None
        self._last_log = ""

        self.devices = fit_dropdown(Gtk.DropDown())
        bind_device_dropdown(self.devices, session, role="monitor")
        self.channel = Gtk.SpinButton.new_with_range(1, 233, 1)
        self.channel.set_value(36)
        self.seconds = Gtk.SpinButton.new_with_range(1, 300, 1)
        self.seconds.set_value(15)

        enable = Gtk.Button(label="Enable monitor mode")
        enable.connect("clicked", self.enable)
        restore = Gtk.Button(label="Restore")
        restore.connect("clicked", self.restore)
        self.start = Gtk.Button(label="Capture", css_classes=["suggested-action"])
        self.start.connect("clicked", self.capture)
        open_file = Gtk.Button(label="Open capture…")
        open_file.connect("clicked", self.open_file)

        self.append(toolbar(labelled("Interface", self.devices),
                            labelled("Channel", self.channel), enable, restore))
        self.point = Gtk.SpinButton.new_with_range(1, 1, 1)
        self.attach = Gtk.Button(label="Attach to point", sensitive=False)
        self.attach.set_tooltip_text(
            "Stores this capture's retry rate, airtime and client count with a survey point, "
            "which fills in the retry and airtime maps and the collisions check."
        )
        self.attach.connect("clicked", self.attach_to_point)

        self.append(toolbar(labelled("Seconds", self.seconds), self.start, open_file,
                            labelled("Point", self.point), self.attach))
        session.on("changed", self._points_changed)
        session.on("project", self._points_changed)
        self._points_changed()

        self.log = Gtk.Label(xalign=0, wrap=True, margin_start=12, margin_end=12,
                             css_classes=["dim-label"],
                             label="Monitor mode needs root: start the app with pkexec or sudo.")
        self.append(self.log)

        self.table = Table([
            ("SSID", "ssid", 200), ("BSSID", "bssid", 160), ("Manufacturer", "vendor", 170),
            ("Ch", "channel", 60),
            ("Width", "width", 80), ("Generation", "generation", 130), ("Security", "security", 150),
            ("Frames", "frames", 80), ("Retries", "retries", 80), ("Airtime", "airtime", 80),
            ("Clients", "clients", 80), ("AP busy", "busy", 80),
        ])
        # Every other address heard talking to an access point: phones, laptops, cameras…
        self.devices_table = Table([
            ("Device", "mac", 170), ("Manufacturer", "vendor", 220),
            ("Network", "network", 200), ("Access point", "bssid", 160),
            ("Signal", "signal", 140), ("Airtime", "airtime", 80), ("Retries", "retries", 80),
        ])
        pages = Gtk.Stack(vexpand=True)
        pages.add_titled(scrolled(self.table), "aps", "Access points")
        pages.add_titled(scrolled(self.devices_table), "devices", "Devices")
        self.append(Gtk.StackSwitcher(stack=pages, halign=Gtk.Align.START, margin_start=12,
                                      margin_bottom=6))
        self.append(pages)
        self.connect("map", lambda *_: self.devices.refill())

    def _device(self) -> str:
        return selected_device(self.devices)

    def say(self, line: str) -> None:
        self.log.set_text(line)

    def enable(self, *_args) -> None:
        device = self._device()
        busy = monitor.interfering()
        if busy:
            self.say(f"Releasing the interface from {', '.join(busy)}…")
        def done(ok, error) -> None:
            if error:
                self.say(f"Couldn't enable it: {error}")
                return
            if ok:
                self.session.monitor_device = device
            self.say(self._last_log)

        def work() -> bool:
            # Switched without a channel, then tuned across the network's whole width
            return monitor.enable(device, None, True, self._note) and self._tune(device)

        run_in_thread(work, done)

    def _tune(self, device: str) -> bool:
        """
        Listens on the chosen channel. When the last scan saw a network there wider than
        20 MHz, listens across all of it, since clients send their data at the full width and
        on the primary 20 MHz alone they'd go unheard and uncounted.
        """
        channel = int(self.channel.get_value())
        wide = [r for r in self.session.last_scan
                if r.channel == channel and r.width_mhz > 20 and r.center_mhz]
        if wide:
            best = max(wide, key=lambda r: r.width_mhz)
            return monitor.tune(device, best.freq_mhz, best.width_mhz, best.center_mhz, self._note)
        return monitor.set_channel(device, channel, self._note)

    def restore(self, *_args) -> None:
        device = self._device()

        def done(_result, error) -> None:
            if error:
                self.say(f"Couldn't restore it: {error}")
            else:
                if self.session.monitor_device == device:
                    self.session.monitor_device = None
                self.say(self._last_log)

        run_in_thread(lambda: monitor.restore(device, self._note), done)

    def _note(self, line: str) -> None:
        self._last_log = line

    def capture(self, *_args) -> None:
        if self.live is not None:
            self.live.stop("stopped")
            return
        if not capture.available():
            self.say("Scapy isn't installed: pip install scapy")
            return
        device = self._device()
        if not device:
            self.say("No wireless interface to capture on.")
            return
        if self.session.radio_holds.get(device):
            self.say(f"{device} is in use by {self.session.radio_holds[device]}: choose another adapter.")
            return
        if not wifi.is_root():
            self.say("Capturing needs root: start the app with pkexec or sudo.")
            return
        if not monitor.is_monitor(device):
            self.say(f"{device} isn't in monitor mode yet: press Enable monitor mode first.")
            return
        if not self._tune(device):
            self.say(self._last_log)
            return
        seconds = float(self.seconds.get_value())
        channel = int(self.channel.get_value())
        folder = make_dir(data_dir() / "captures")
        path = folder / f"{time.strftime('%Y-%m-%d %H%M%S')} {device} ch{channel}.pcap"
        self.live = capture.LiveCapture(device, path, seconds)
        try:
            self.live.start()
        except Exception as error:
            self.live = None
            self.say(f"Capture failed: {error}")
            return
        self.session.monitor_users.add("Capture")
        self.start.set_label("Stop")
        self.say(f"Capturing on {device}, channel {channel}, for {seconds:.0f} s…")
        GLib.timeout_add(1000, self._capture_tick)

    def _capture_tick(self) -> bool:
        live = self.live
        if live is None:
            return False
        if live.running:
            clients = len({mac for stat in live.summary().bss for mac in stat.clients})
            self.say(f"Capturing on {live.device}: {live.elapsed:.0f} of {live.seconds:.0f} s · "
                     f"{live.written:,} frames · {clients} clients so far")
            return True
        live.wait()
        self.live = None
        self.session.monitor_users.discard("Capture")
        self.start.set_label("Capture")
        if live.error and not live.written:
            self.say(f"Capture failed: {live.error}")
            return False
        self.show(live.summary())
        self.say(self.log.get_text() + f" · saved to {live.path}"
                 + (f" · {live.error}" if live.error else ""))
        return False

    def open_file(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Open a capture")
        dialog.open(self.get_root(), None, self._opened)

    def _opened(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except Exception:
            return
        run_in_thread(
            lambda: capture.analyze(capture.read_pcap(path), path.split("/")[-1]),
            lambda summary, error: self.say(f"Couldn't read it: {error}") if error else self.show(summary),
        )

    def show(self, summary: capture.CaptureSummary) -> None:
        self.summary = summary
        self.say(
            f"{summary.frames:,} frames over {summary.seconds:.1f} s"
            + (f" on channel {summary.channel}" if summary.channel else "")
            + f" · retries {summary.retry_rate * 100:.1f}% · airtime {summary.airtime * 100:.1f}%"
            + f" · {summary.clients} clients"
            + (f" · {summary.deauths} deauth" if summary.deauths else "")
        )
        # Hidden access points it heard named name them in the survey too
        learned = hidden.learn(self.session.project, capture.hidden_names(summary))
        if learned:
            self.session.changed()
            self.say(f"{self.log.get_text()} · "
                     f"{hidden.describe_learned(self.session.project, learned)}")
        self.table.set_rows([
            {
                "ssid": stat.ssid or "(hidden)",
                "bssid": stat.bssid,
                "vendor": oui.vendor(stat.bssid, access_point=True),
                "channel": stat.channel or "",
                "width": f"{stat.width_mhz} MHz" if stat.width_mhz else "",
                "generation": stat.generation,
                "security": stat.security,
                "frames": stat.frames,
                "retries": f"{stat.retry_rate * 100:.0f}%",
                "airtime": f"{stat.airtime_us / (summary.seconds * 1e6) * 100:.1f}%",
                "clients": len(stat.clients),
                "busy": "" if stat.reported_utilization is None
                        else f"{stat.reported_utilization * 100:.0f}%",
            }
            for stat in summary.bss
        ])
        heard = {device.mac: device for device in summary.devices}
        rows = []
        for stat in summary.bss:
            for mac in stat.clients:
                device = heard.get(mac)
                rssi = device.avg_rssi if device else None
                sent = device.frames if device else 0
                rows.append({
                    "mac": mac.upper(),
                    "vendor": oui.vendor(mac),
                    "network": stat.ssid or "(hidden)",
                    "bssid": stat.bssid,
                    # Heard only being sent to, never sending, so there's nothing to measure
                    "signal": _signal_text(rssi) if rssi is not None else "not heard sending",
                    "airtime": f"{device.airtime_us / (summary.seconds * 1e6) * 100:.1f}%"
                               if device else "",
                    "retries": f"{device.retries / sent * 100:.0f}%" if sent else "",
                    "_rssi": rssi if rssi is not None else -999,
                })
        # Strongest first within each network: the nearest devices
        self.devices_table.set_rows(sorted(
            rows, key=lambda row: (row["network"], -row["_rssi"], row["mac"])))

        self._points_changed()

    def _points_changed(self) -> None:
        count = len(self.session.project.points)
        previous = int(self.point.get_value())
        self.point.set_range(1, max(1, count))
        # Follow the newest point unless a specific earlier one was picked
        if previous >= count - 1 or previous > count:
            self.point.set_value(max(1, count))
        self.attach.set_sensitive(self.summary is not None and count > 0)

    def attach_to_point(self, *_args) -> None:
        summary = self.summary
        points = self.session.project.points
        number = int(self.point.get_value())
        if summary is None or not 1 <= number <= len(points):
            return
        point = points[number - 1]
        replacing = point.capture is not None
        point.capture = {
            "retry_rate": summary.retry_rate,
            "retry_eligible": summary.retry_eligible,
            "airtime": summary.airtime,
            "clients": summary.clients,
            "channel": summary.channel,
            "source": summary.source,
            # Each client's own signal as heard here, so the map can work out where it is
            "clients_heard": _clients_heard(summary),
        }
        self.session.changed()
        self.say(f"{'Replaced the capture on' if replacing else 'Attached to'} point {number}: "
                 f"retries {summary.retry_rate * 100:.1f}%, airtime {summary.airtime * 100:.1f}%, "
                 f"{len(point.capture['clients_heard'])} clients with a signal. Capture at 4 or more "
                 "points on the same channel and the survey map places them.")


def _signal_text(rssi: float) -> str:
    """dBm with a plain word, by the same thresholds as the survey: good, fair, weak."""
    word = "strong" if rssi >= -60 else "fair" if rssi >= -70 else "weak"
    return f"{rssi:.0f} dBm, {word}"


def _clients_heard(summary: capture.CaptureSummary) -> list[dict]:
    heard = {device.mac: device for device in summary.devices}
    found = {}
    # Named networks first, so a client is filed under its SSID rather than a nameless BSSID
    for stat in sorted(summary.bss, key=lambda stat: not stat.ssid):
        for mac in stat.clients:
            device = heard.get(mac)
            if device is None or device.avg_rssi is None or mac in found:
                continue
            found[mac] = {
                "mac": mac, "rssi": round(device.avg_rssi, 1), "vendor": oui.vendor(mac),
                "ssid": stat.ssid, "bssid": stat.bssid, "frames": device.frames,
                "airtime": device.airtime_us / (summary.seconds * 1e6) if summary.seconds else 0.0,
            }
    return list(found.values())
