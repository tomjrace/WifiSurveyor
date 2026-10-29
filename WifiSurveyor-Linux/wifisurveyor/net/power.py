"""
Keeping the computer awake through a watch, and noticing when it sleeps anyway.

Through logind on the system bus, so it works the same run as root or not: a *block*
inhibitor stops the computer sleeping (idle suspend included) while it's on mains power or
the battery is above LOW_BATTERY; below that it's let go, so the computer can sleep before
the battery runs out. A *delay* inhibitor holds any sleep that does come (low battery, the
lid, the suspend menu) for long enough to pause the watch and put its log safely on disk.
"""

from __future__ import annotations

import os

from gi.repository import Gio, GLib

LOW_BATTERY = 15.0            # percent: at or below this, on battery, the computer may sleep
CHECK_EVERY = 30              # seconds between looks at the battery
DISCHARGING = (2, 3, 6)       # UPower's discharging, empty and pending-discharge

LOGIND = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
UPOWER_DISPLAY = ("org.freedesktop.UPower", "/org/freedesktop/UPower/devices/DisplayDevice")


def stay_awake(battery: dict | None) -> bool:
    """Whether to keep the computer awake: always on mains power, on battery above LOW_BATTERY."""
    if not battery or not battery.get("IsPresent"):
        return True                            # no battery: a desktop, or UPower isn't there
    if battery.get("State") not in DISCHARGING:
        return True
    return battery.get("Percentage", 100.0) > LOW_BATTERY


class KeepAwake:
    """
    Held for as long as a watch runs. ``on_sleep`` is called (on the main thread) as the
    computer goes to sleep, before it does; ``on_wake`` once it's back; ``on_change`` whenever
    ``note`` (what's being done about sleep, in a few words) changes.
    """

    def __init__(self, why: str, on_sleep, on_wake, on_change=None) -> None:
        self.why = why
        self.on_sleep, self.on_wake = on_sleep, on_wake
        self.on_change = on_change or (lambda: None)
        self.note = ""
        self.battery: dict | None = None
        self._bus: Gio.DBusConnection | None = None
        self._block: int | None = None         # the inhibitors' file descriptors
        self._delay: int | None = None
        self._subscription = 0
        self._timer = 0

    def start(self) -> None:
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as error:
            self._say(f"Can't keep the computer awake: {error.message}")
            return
        self._subscription = self._bus.signal_subscribe(
            LOGIND[0], LOGIND[2], "PrepareForSleep", LOGIND[1], None, Gio.DBusSignalFlags.NONE,
            self._prepare_for_sleep)
        self._delay = self._inhibit("sleep", "delay")
        self._check()
        self._timer = GLib.timeout_add_seconds(CHECK_EVERY, self._check)

    def stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._bus is not None and self._subscription:
            self._bus.signal_unsubscribe(self._subscription)
            self._subscription = 0
        self._block = _release(self._block)
        self._delay = _release(self._delay)
        self._bus = None

    # ---------- staying awake ----------

    def _check(self) -> bool:
        self.battery = self._read_battery()
        awake = stay_awake(self.battery)
        if awake and self._block is None:
            self._block = self._inhibit("sleep:idle", "block")
        elif not awake and self._block is not None:
            self._block = _release(self._block)
        if self._block is not None:
            self._say("Keeping the computer awake while watching")
        elif not awake:
            self._say(f"Battery at {self.battery.get('Percentage', 0):.0f}%: the computer may "
                      "sleep now. The watch pauses and picks up when it wakes")
        return True

    def _read_battery(self) -> dict | None:
        try:
            reply = self._bus.call_sync(
                UPOWER_DISPLAY[0], UPOWER_DISPLAY[1], "org.freedesktop.DBus.Properties", "GetAll",
                GLib.Variant("(s)", ("org.freedesktop.UPower.Device",)), GLib.VariantType("(a{sv})"),
                Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error:
            return None
        return reply.unpack()[0]

    def _inhibit(self, what: str, mode: str) -> int | None:
        try:
            reply, fds = self._bus.call_with_unix_fd_list_sync(
                LOGIND[0], LOGIND[1], LOGIND[2], "Inhibit",
                GLib.Variant("(ssss)", (what, "Wi-Fi Surveyor", self.why, mode)),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 5000, None, None)
        except GLib.Error as error:
            self._say(f"Can't keep the computer awake: {error.message}")
            return None
        return fds.get(reply.unpack()[0])        # a copy of its own, closed to let go

    # ---------- sleeping anyway ----------

    def _prepare_for_sleep(self, _bus, _sender, _path, _interface, _signal, parameters) -> None:
        going = parameters.unpack()[0]
        if going:
            try:
                self.on_sleep()
            finally:
                self._delay = _release(self._delay)       # done: the computer may go now
        else:
            if self._delay is None:
                self._delay = self._inhibit("sleep", "delay")
            self.on_wake()
            self._check()

    def _say(self, note: str) -> None:
        if note != self.note:
            self.note = note
            self.on_change()


def _release(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    return None
