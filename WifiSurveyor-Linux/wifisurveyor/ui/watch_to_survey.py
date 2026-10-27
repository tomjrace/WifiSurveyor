"""
Choosing the survey a watch goes into: the one open, a recent one, or any survey file. A survey
that isn't open is opened first (after unsaved work is dealt with); the watch then waits for it
(Session.pending_watch) and is placed on its plan as soon as it's loaded.
"""

from __future__ import annotations

import time
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ..core import library
from .common import run_in_thread


class WatchToSurvey(Adw.Dialog):
    def __init__(self, session, window, watch: dict) -> None:
        super().__init__(title="Add the watch to a survey", content_width=560, content_height=560)
        self.session, self.window, self.watch = session, window, watch
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        page.append(header)
        content = Adw.PreferencesPage(vexpand=True)
        page.append(content)
        self.set_child(page)

        project = session.project
        if project.plan_png:
            group = Adw.PreferencesGroup(title="Open now")
            group.add(self._row(project.name, f"{len(project.points)} points", self._here))
            content.add(group)
        self.recent = Adw.PreferencesGroup(
            title="Recent surveys",
            description="Opens it (asking about unsaved work first), then you tap where you "
                        "watched from.")
        waiting = Adw.ActionRow(title="Looking for surveys…")
        self.recent.add(waiting)
        content.add(self.recent)
        other = Adw.PreferencesGroup()
        other.add(self._row("Open another survey…", "Any survey file", self._pick_file))
        content.add(other)

        mine = str(project.path) if project.path else None

        def done(infos, error) -> None:
            self.recent.remove(waiting)
            shown = 0
            for info in infos or []:
                if info.path == mine or info.root_only:
                    continue
                when = time.strftime("%d %b %Y", time.localtime(info.saved_at or info.surveyed_at))
                self.recent.add(self._row(info.name, f"{info.points} points, saved {when}",
                                          lambda path=info.path: self._open(path)))
                shown += 1
                if shown >= 8:
                    break
            if not shown:
                self.recent.add(Adw.ActionRow(title=error and f"Couldn't list them: {error}"
                                              or "No other saved surveys."))

        run_in_thread(library.known, done)

    @staticmethod
    def _row(title: str, subtitle: str, action) -> Adw.ActionRow:
        row = Adw.ActionRow(title=GLib.markup_escape_text(title or "Untitled survey"),
                            subtitle=GLib.markup_escape_text(subtitle), activatable=True)
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda *_: action())
        return row

    def _here(self) -> None:
        self.close()
        self.session.emit("add-watch", self.watch)

    def _open(self, path: str) -> None:
        self.close()
        # Placed once that survey is loaded; only that one, so a cancelled open leaves nothing behind
        self.session.pending_watch = {"path": str(Path(path).resolve()), "watch": self.watch}
        self.window.open_path(path)

    def _pick_file(self) -> None:
        dialog = Gtk.FileDialog(title="Choose the survey")
        surveys = Gtk.FileFilter(name="Surveys")
        surveys.add_suffix("wsurvey")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(surveys)
        dialog.set_filters(filters)
        try:
            dialog.set_initial_folder(Gio.File.new_for_path(str(library.surveys_dir())))
        except Exception:
            pass

        def chosen(dialog, result) -> None:
            try:
                path = dialog.open_finish(result).get_path()
            except GLib.Error:
                return
            self._open(path)

        dialog.open(self.window, None, chosen)

