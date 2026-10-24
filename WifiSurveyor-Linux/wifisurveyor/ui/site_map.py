"""Placing the site on a map: find it, lay the blueprint over the building, take the map's scale.

Find the site with the GPS, the access points heard nearby (BeaconDB), an address search, or
by moving the map. The map is OpenStreetMap or US aerial imagery; OpenStreetMap's building
outlines can be drawn on it. Lay the blueprint over its building by dragging, pinching and
turning it (or the sliders), or tap the building and Fit, which lines it up by itself. The map
knows its own scale, so the blueprint takes it: Use blueprint makes it the survey's plan with
its size in feet already set. Use map makes the map itself the plan, with the blueprint on it.
No API key is used anywhere; see net.maps.
"""

from __future__ import annotations

import io
import math

import cairo
from gi.repository import Adw, GdkPixbuf, GLib, Gtk

from ..core import geo, settings
from ..net import maps, wifi
from .common import run_in_thread

MAP_W, MAP_H = 1536, 1024
DEFAULT_ZOOM = 18
BEACONDB_ASKED = "beacondb_ok"


def _surface(png: bytes) -> cairo.ImageSurface:
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def _inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


class SiteMapDialog(Adw.Dialog):
    def __init__(self, survey) -> None:
        super().__init__(title="Place the site on a map", content_width=1240, content_height=780)
        self.survey = survey
        self.session = survey.session
        self.map: maps.MapImage | None = None
        self.map_surface = None
        self.source, self.zoom = "osm", DEFAULT_ZOOM
        self.here: tuple[float, float, float | None] | None = None   # lat, lon, accuracy (m)
        self.located_by = ""
        self._wanted_outline: dict | None = None    # the searched building, until its map is in
        self.outlines: list[dict] = []          # OSM buildings, each with "px" on the map
        self.selected: int | None = None
        self.blueprint: dict | None = None      # pixbuf, surface, w, h, cx, cy, scale, turn
        self.view = [1.0, 0.0, 0.0]             # map to screen: scale, x offset, y offset
        self._drag: dict | None = None
        self._pinch: dict | None = None
        self._syncing = False

        # ----- top: finding the site, and the map -----
        gps = Gtk.Button(label="GPS", tooltip_text="Centre on the GPS fix (Setup → GPS)")
        gps.connect("clicked", lambda *_: self._from_gps())
        nearby = Gtk.Button(label="Nearby Wi-Fi",
                            tooltip_text="Locate from the access points heard here, through "
                                         "BeaconDB (asks first)")
        nearby.connect("clicked", lambda *_: self._from_wifi())
        self.search = Gtk.SearchEntry(placeholder_text="Building address: its outline comes too",
                                      width_chars=34)
        self.search.set_tooltip_text("Type the building's street address (or name) and press "
                                     "Find: the map centres on it and its outline is downloaded "
                                     "from OpenStreetMap and selected, ready for Fit to building.")
        self.search.connect("activate", lambda *_: self._from_search())
        find = Gtk.Button(label="Find")
        find.connect("clicked", lambda *_: self._from_search())
        self.source_choice = Gtk.DropDown.new_from_strings([v[0] for v in maps.SOURCES.values()])
        self.source_choice.connect("notify::selected", self._source_changed)
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text="Wider area")
        zoom_out.connect("clicked", lambda *_: self._set_zoom(self.zoom - 1))
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text="Closer")
        zoom_in.connect("clicked", lambda *_: self._set_zoom(self.zoom + 1))
        zooms = Gtk.Box(css_classes=["linked"])
        zooms.append(zoom_out)
        zooms.append(zoom_in)
        self.buildings_button = Gtk.Button(label="Buildings",
                                           tooltip_text="Draw OpenStreetMap's building outlines "
                                                        "(can take half a minute)")
        self.buildings_button.connect("clicked", lambda *_: self._load_buildings())
        blueprint = Gtk.Button(label="Blueprint…", css_classes=["suggested-action"])
        blueprint.connect("clicked", lambda *_: self._choose_blueprint())
        top = Adw.WrapBox(child_spacing=8, line_spacing=8, margin_start=12, margin_end=12,
                          margin_top=8, margin_bottom=8)
        address = Gtk.Box(spacing=6)
        address.append(Gtk.Label(label="Address", css_classes=["dim-label"]))
        address.append(self.search)
        address.append(find)
        for widget in (address, gps, nearby, self.source_choice, zooms,
                       self.buildings_button, blueprint):
            top.append(widget)

        # ----- the map -----
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self._draw)
        self.area.connect("resize", lambda *_: self._fit_view())
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.area.add_controller(drag)
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._pinch_begin)
        zoom.connect("scale-changed", self._pinched)
        zoom.connect("end", lambda *_: setattr(self, "_pinch", None))
        self.area.add_controller(zoom)
        turn = Gtk.GestureRotate()
        turn.connect("begin", self._turn_begin)
        turn.connect("angle-changed", self._turned)
        self.area.add_controller(turn)
        zoom.group(turn)
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._scrolled)
        self.area.add_controller(wheel)
        self.status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"], margin_start=12,
                                margin_end=12)

        # ----- bottom: the blueprint -----
        self.size = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -7, 3, 0.01)
        self.size.set_draw_value(False)
        self.size.set_hexpand(True)
        self.size.connect("value-changed", self._size_moved)
        self.turn = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -180, 180, 0.5)
        self.turn.set_draw_value(False)
        self.turn.set_hexpand(True)
        self.turn.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.turn.connect("value-changed", self._turn_moved)
        self.see = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.15, 1.0, 0.05)
        self.see.set_draw_value(False)
        self.see.set_value(0.6)
        self.see.set_size_request(120, -1)
        self.see.connect("value-changed", lambda *_: self.area.queue_draw())
        self.fit_button = Gtk.Button(label="Fit to building",
                                     tooltip_text="Lines the blueprint up with the building "
                                                  "tapped (or the one under it)")
        self.fit_button.connect("clicked", lambda *_: self._fit_to_building())
        bottom = Adw.WrapBox(child_spacing=8, line_spacing=6, margin_start=12, margin_end=12,
                             margin_top=6, margin_bottom=8)
        for label, widget in (("Size", self.size), ("Turn", self.turn), ("See-through", self.see)):
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Label(label=label, css_classes=["dim-label"]))
            box.append(widget)
            box.set_size_request(260 if widget is not self.see else 180, -1)
            bottom.append(box)
        bottom.append(self.fit_button)
        self.readout = Gtk.Label(xalign=0, wrap=True, margin_start=12, margin_end=12, margin_bottom=8)

        # ----- header: what to do with it -----
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.use_map = Gtk.Button(label="Use map", sensitive=False,
                                  tooltip_text="The map itself as the plan (with the blueprint on "
                                               "it, if there is one), its scale set")
        self.use_map.connect("clicked", lambda *_: self._use(map_plan=True))
        self.use_blueprint = Gtk.Button(label="Use blueprint", css_classes=["suggested-action"],
                                        sensitive=False,
                                        tooltip_text="The blueprint as the plan, its scale "
                                                     "taken from the map")
        self.use_blueprint.connect("clicked", lambda *_: self._use(map_plan=False))
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self.use_blueprint)
        header.pack_end(self.use_map)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for widget in (top, self.area, self.status, bottom, self.readout):
            body.append(widget)
        view = Adw.ToolbarView(content=body)
        view.add_top_bar(header)
        self.set_child(view)
        self._enable_blueprint_controls()
        GLib.idle_add(self._start)

    # ---------- where the site is ----------

    def _start(self) -> bool:
        geo_info = self.session.project.geo or {}
        fix = self.session.gps.fix
        if geo_info.get("lat") is not None:
            self._go(geo_info["lat"], geo_info["lon"], None, "where this survey was placed before")
        elif fix is not None and fix.quality:
            self._from_gps()
        else:
            self._say("Find the site: GPS (when connected and it has a fix), Nearby Wi-Fi, or "
                      "type its address and press Find.")
        return False

    def _say(self, text: str) -> None:
        self.status.set_text(text)

    def _from_gps(self) -> None:
        fix = self.session.gps.fix
        if fix is None or not fix.quality:
            self._say("No GPS fix. Connect the GPS (Setup → GPS); indoors it may need to be by a "
                      "window for a few minutes. Or use Nearby Wi-Fi, or the address.")
            return
        self._go(fix.lat, fix.lon, fix.accuracy_m, f"GPS ({fix.describe()})")

    def _from_wifi(self) -> None:
        if not settings.get(BEACONDB_ASKED, False):
            dialog = Adw.AlertDialog(
                heading="Locate from nearby Wi-Fi?",
                body="This sends the MAC addresses (BSSIDs) and signal levels of the access "
                     "points heard here to BeaconDB (beacondb.net), a free community Wi-Fi "
                     "location service with no account or key. Network names aren't sent; "
                     "networks named …_nomap and randomised addresses are left out. Nothing else "
                     "is sent, and only when you press Nearby Wi-Fi.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("go", "Send and locate")
            dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_close_response("cancel")

            def answered(_d, response: str) -> None:
                if response == "go":
                    settings.put(BEACONDB_ASKED, True)
                    self._from_wifi()

            dialog.connect("response", answered)
            dialog.present(self)
            return
        self._say("Scanning Wi-Fi, then asking BeaconDB…")
        device = self.session.scan_device or wifi.choose_roles(wifi.interfaces())[0]

        def work():
            readings = self.session.last_scan or (wifi.scan(device) if device else [])
            return maps.wifi_locate(readings)

        def done(result, error) -> None:
            if error:
                self._say(f"Couldn't locate from Wi-Fi: {error}")
                return
            lat, lon, accuracy = result
            self._go(lat, lon, accuracy, f"nearby Wi-Fi, BeaconDB (±{accuracy:.0f} m)")

        run_in_thread(work, done)

    def _from_search(self) -> None:
        text = self.search.get_text().strip()
        if not text:
            return
        self._say(f"Looking up “{text}”…")

        def done(found, error) -> None:
            if error or not found:
                self._say(f"Couldn't find it: {error}" if error else
                          f"Nothing found for “{text}”. Try the street address with the town.")
                return
            name, lat, lon, outline = found[0]
            others = f" ({len(found) - 1} other matches: make the search more exact if it's wrong)" \
                if len(found) > 1 else ""
            # Its outline goes on the map once the map is in; without one, the building at
            # the address is looked for among OpenStreetMap's outlines
            self._wanted_outline = {"name": name.split(",")[0], "points": outline, "lat": lat,
                                    "lon": lon}
            self._go(lat, lon, None, f"the address search: {name}{others}")

        run_in_thread(lambda: maps.search(text), done)

    def _go(self, lat: float, lon: float, accuracy: float | None, how: str) -> None:
        self.here = (lat, lon, accuracy)
        self.located_by = how
        self._load_map(lat, lon, f"Located by {how}.")

    # ---------- the map ----------

    def _load_map(self, lat: float, lon: float, note: str = "") -> None:
        old = self.map
        source, zoom = self.source, self.zoom
        self._say((note + " " if note else "") + "Fetching the map…")

        def progress(text: str) -> None:
            GLib.idle_add(lambda: self._say((note + " " if note else "") + text) and False)

        def done(image, error) -> None:
            if error:
                self._say(f"Couldn't fetch the map: {error}")
                return
            self.map, self.map_surface = image, _surface(image.png)
            for outline in self.outlines:
                outline["px"] = [image.to_px(*p) for p in outline["points"]]
            if self.blueprint is not None and old is not None:
                self._carry_blueprint(old, image)
            self.use_map.set_sensitive(True)
            self._fit_view()
            if self._wanted_outline is not None:
                wanted, self._wanted_outline = self._wanted_outline, None
                GLib.idle_add(lambda: self._take_outline(wanted) and False)
            self._say((note + " " if note else "")
                      + "Drag to move the map; pinch or scroll to zoom the view. Load the "
                        "blueprint, then drag, pinch and turn it over its building.")
            self._describe()

        run_in_thread(lambda: maps.stitch(source, lat, lon, zoom, MAP_W, MAP_H, progress), done)

    def _centre(self) -> tuple[float, float] | None:
        """Where the map is centred now, or the site when there's no map."""
        if self.map is not None:
            return self.map.to_lat_lon(MAP_W / 2, MAP_H / 2)
        return (self.here[0], self.here[1]) if self.here else None

    def _source_changed(self, *_args) -> None:
        self.source = list(maps.SOURCES)[self.source_choice.get_selected()]
        centre = self._centre()
        if centre:
            self._load_map(*centre)

    def _set_zoom(self, zoom: int) -> None:
        zoom = max(14, min(maps.MAX_ZOOM, zoom))
        if zoom == self.zoom:
            return
        self.zoom = zoom
        centre = self._centre()
        if centre:
            self._load_map(*centre)

    def _recentre(self, x: float, y: float) -> None:
        """A new map centred on a map pixel: where the view was dragged to."""
        if self.map is not None:
            self._load_map(*self.map.to_lat_lon(x, y))

    def _take_outline(self, wanted: dict) -> None:
        """The searched building's outline, selected; or, without one, the outline at the address."""
        if wanted["points"]:
            outline = {"id": None, "name": wanted["name"], "points": wanted["points"],
                       "px": [self.map.to_px(*p) for p in wanted["points"]]}
            self.outlines = [o for o in self.outlines if o.get("id") is not None] + [outline]
            self._select_outline(len(self.outlines) - 1, "from the address")
            return
        self._load_buildings(at=(wanted["lat"], wanted["lon"]), radius=80)

    def _select_outline(self, index: int, how: str) -> None:
        self.selected = index
        name = self.outlines[index]["name"] or "The building"
        frame = geo.building_frame(self.outlines[index]["px"])
        size = ""
        if frame is not None:
            mpp = self.map.meters_per_pixel(frame[1])
            size = (f" ({frame[2] * mpp * geo.FEET_PER_M:.0f} × {frame[3] * mpp * geo.FEET_PER_M:.0f} ft "
                    "on the map)")
        if self.blueprint is not None:
            self._fit_to_building()
            self._say(f"{name}'s outline {how}{size}: the blueprint is fitted to it. Check the "
                      "walls line up.")
        else:
            self._say(f"{name}'s outline {how}{size} is selected. Load the blueprint, then Fit "
                      "to building.")
        self.area.queue_draw()

    def _load_buildings(self, at: tuple[float, float] | None = None, radius: float | None = None) -> None:
        """
        OpenStreetMap's building outlines round the map's centre; with ``at``, the one at (or
        nearest) that place is selected.
        """
        centre = at or self._centre()
        if centre is None or self.map is None:
            self._say("Find the site first.")
            return
        radius = radius or min(400.0, self.map.meters_per_pixel() * max(MAP_W, MAP_H) / 2)
        self.buildings_button.set_sensitive(False)
        self._say("Asking OpenStreetMap for building outlines (this can take half a minute)…")

        def done(found, error) -> None:
            self.buildings_button.set_sensitive(True)
            if error:
                self._say(f"Couldn't get building outlines: {error}")
                return
            self.outlines = found
            for outline in self.outlines:
                outline["px"] = [self.map.to_px(*p) for p in outline["points"]]
            self.selected = None
            if at is not None and found:
                point = self.map.to_px(*at)
                index = next((i for i, o in enumerate(found) if _inside(point, o["px"])), None)
                if index is None:              # the address point sits just outside, often
                    index = min(range(len(found)), key=lambda i: min(
                        math.dist(point, p) for p in found[i]["px"]))
                self._select_outline(index, "at the address, from OpenStreetMap")
                return
            self._say(f"{len(found)} buildings outlined. Tap the building the blueprint is of, "
                      "then Fit to building." if found else
                      "OpenStreetMap has no building outlines here. Line the blueprint up by eye.")
            self.area.queue_draw()

        run_in_thread(lambda: maps.buildings(*centre, radius), done)

    # ---------- the blueprint ----------

    def _choose_blueprint(self) -> None:
        dialog = Gtk.FileDialog(title="Choose the blueprint or floor plan")
        images = Gtk.FileFilter()
        images.set_name("Images")
        images.add_pixbuf_formats()
        dialog.set_default_filter(images)
        dialog.open(self.get_root(), None, self._blueprint_chosen)

    def _blueprint_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        self.load_blueprint(path)

    def load_blueprint(self, path: str) -> None:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            with open(path, "rb") as handle:
                data = handle.read()
            ok, png = pixbuf.save_to_bufferv("png", [], [])
        except GLib.Error:
            return
        except Exception as error:
            self._say(f"Couldn't open that image: {error}")
            return
        w, h = pixbuf.get_width(), pixbuf.get_height()
        # Start it over the middle of the map, about a third of the map across
        scale = (MAP_W / 3) / max(w, h)
        self.blueprint = {"data": data, "surface": _surface(bytes(png)), "w": w, "h": h,
                          "cx": MAP_W / 2, "cy": MAP_H / 2, "scale": scale, "turn": 0.0}
        self._enable_blueprint_controls()
        self._sync_sliders()
        self._describe()
        self.area.queue_draw()

    def _carry_blueprint(self, old: maps.MapImage, new: maps.MapImage) -> None:
        """Keeps the blueprint on the same ground when the map moves or changes zoom."""
        bp = self.blueprint
        lat, lon = old.to_lat_lon(bp["cx"], bp["cy"])
        bp["cx"], bp["cy"] = new.to_px(lat, lon)
        bp["scale"] *= 2 ** (new.zoom - old.zoom)
        self._sync_sliders()

    def _enable_blueprint_controls(self) -> None:
        on = self.blueprint is not None
        for widget in (self.size, self.turn, self.see, self.fit_button, self.use_blueprint):
            widget.set_sensitive(on)

    def _sync_sliders(self) -> None:
        bp = self.blueprint
        if bp is None:
            return
        self._syncing = True
        self.size.set_value(math.log2(bp["scale"]))
        self.turn.set_value(((bp["turn"] + 180) % 360) - 180)
        self._syncing = False

    def _size_moved(self, scale: Gtk.Scale) -> None:
        if self._syncing or self.blueprint is None:
            return
        self.blueprint["scale"] = 2 ** scale.get_value()
        self._describe()
        self.area.queue_draw()

    def _turn_moved(self, scale: Gtk.Scale) -> None:
        if self._syncing or self.blueprint is None:
            return
        self.blueprint["turn"] = scale.get_value()
        self._describe()
        self.area.queue_draw()

    def _fit_to_building(self) -> None:
        bp = self.blueprint
        if bp is None:
            return
        index = self.selected
        if index is None:
            index = next((i for i, o in enumerate(self.outlines)
                          if _inside((bp["cx"], bp["cy"]), o["px"])), None)
        if index is None:
            self._say("Tap a building outline first (Buildings draws them), or move the "
                      "blueprint onto one.")
            return
        frame = geo.building_frame(self.outlines[index]["px"])
        if frame is None:
            return
        bp["cx"], bp["cy"], bp["scale"], bp["turn"] = geo.fit_into(bp["w"], bp["h"], frame)
        self._sync_sliders()
        name = self.outlines[index]["name"] or "the building"
        self._say(f"Fitted to {name}: its longer side along the building's length. Check the "
                  "walls line up; if the blueprint shows more or less than the building, adjust "
                  "Size, or turn it half round with Turn.")
        self._describe()
        self.area.queue_draw()

    def blueprint_meters_per_pixel(self) -> float | None:
        """The blueprint's scale: the map's at its centre, times map pixels per blueprint pixel."""
        if self.blueprint is None or self.map is None:
            return None
        return self.map.meters_per_pixel(self.blueprint["cy"]) * self.blueprint["scale"]

    def _describe(self) -> None:
        if self.map is None:
            self.readout.set_text("")
            return
        text = f"Map: {geo.describe_scale(self.map.meters_per_pixel())}, zoom {self.map.zoom}."
        mpp = self.blueprint_meters_per_pixel()
        if mpp is not None:
            bp = self.blueprint
            width_ft = bp["w"] * mpp * geo.FEET_PER_M
            height_ft = bp["h"] * mpp * geo.FEET_PER_M
            text += (f"  Blueprint: {geo.describe_scale(mpp)}, so it covers "
                     f"{width_ft:.0f} × {height_ft:.0f} ft, turned {bp['turn']:.1f}°.")
        self.readout.set_text(text)

    # ---------- using it ----------

    def _use(self, map_plan: bool) -> None:
        if self.map is None:
            return
        project = self.session.project
        bp = self.blueprint
        if map_plan:
            data, w, h = self._map_with_blueprint(), MAP_W, MAP_H
            mpp = self.map.meters_per_pixel()
            lat, lon = self.map.to_lat_lon(MAP_W / 2, MAP_H / 2)
            turn = 0.0
        else:
            if bp is None:
                return
            data, w, h = bp["data"], bp["w"], bp["h"]
            mpp = self.blueprint_meters_per_pixel()
            lat, lon = self.map.to_lat_lon(bp["cx"], bp["cy"])
            turn = bp["turn"]
        had_points = bool(project.points)
        project.set_plan(data, w, h)
        project.meters_per_pixel = mpp
        project.geo = {"lat": lat, "lon": lon, "rotation_deg": round(turn, 2),
                       "meters_per_pixel": mpp, "zoom": self.map.zoom, "source": self.map.source,
                       "credit": self.map.credit if map_plan else "",
                       "located_by": self.located_by, "plan": "map" if map_plan else "blueprint"}
        self.survey.reload()
        self.session.changed()
        self.survey.status.set_text(
            f"Plan placed from the map: {geo.describe_scale(mpp)}."
            + (" The points measured before stay where they were on the old plan; check them."
               if had_points else " Tap the plan to measure."))
        self.close()

    def _map_with_blueprint(self) -> bytes:
        """The map as a plan image: the blueprint and outlines on it, and the map's credit."""
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, MAP_W, MAP_H)
        cr = cairo.Context(surface)
        self._paint_map(cr)
        cr.set_font_size(14)
        credit = self.map.credit
        extents = cr.text_extents(credit)
        cr.set_source_rgba(1, 1, 1, 0.8)
        cr.rectangle(MAP_W - extents.width - 16, MAP_H - 24, extents.width + 16, 24)
        cr.fill()
        cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.move_to(MAP_W - extents.width - 8, MAP_H - 7)
        cr.show_text(credit)
        out = io.BytesIO()
        surface.write_to_png(out)
        return out.getvalue()

    # ---------- drawing ----------

    def _fit_view(self) -> None:
        width, height = self.area.get_width(), self.area.get_height()
        if width <= 0 or height <= 0:
            return
        scale = min(width / MAP_W, height / MAP_H)
        self.view = [scale, (width - MAP_W * scale) / 2, (height - MAP_H * scale) / 2]
        self.area.queue_draw()

    def _paint_map(self, cr) -> None:
        """Map, outlines, blueprint and position, in map pixels."""
        if self.map_surface is not None:
            cr.set_source_surface(self.map_surface, 0, 0)
            cr.paint()
        for index, outline in enumerate(self.outlines):
            points = outline.get("px") or []
            if len(points) < 3:
                continue
            cr.move_to(*points[0])
            for point in points[1:]:
                cr.line_to(*point)
            cr.close_path()
            if index == self.selected:
                cr.set_source_rgba(0.95, 0.55, 0.1, 0.30)
                cr.fill_preserve()
            cr.set_source_rgba(0.85, 0.35, 0.05, 0.95)
            cr.set_line_width(2.5 if index == self.selected else 1.5)
            cr.stroke()
        bp = self.blueprint
        if bp is not None:
            cr.save()
            cr.translate(bp["cx"], bp["cy"])
            cr.rotate(math.radians(bp["turn"]))
            cr.scale(bp["scale"], bp["scale"])
            cr.translate(-bp["w"] / 2, -bp["h"] / 2)
            cr.set_source_surface(bp["surface"], 0, 0)
            cr.paint_with_alpha(self.see.get_value())
            cr.rectangle(0, 0, bp["w"], bp["h"])
            cr.restore()
            cr.set_source_rgba(0.1, 0.45, 0.95, 0.9)
            cr.set_line_width(2)
            cr.stroke()
        if self.here and self.map is not None:
            x, y = self.map.to_px(self.here[0], self.here[1])
            accuracy = self.here[2]
            if accuracy:
                radius = accuracy / self.map.meters_per_pixel()
                cr.arc(x, y, radius, 0, 2 * math.pi)
                cr.set_source_rgba(0.2, 0.5, 1.0, 0.15)
                cr.fill()
            cr.arc(x, y, 7, 0, 2 * math.pi)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(0.15, 0.45, 0.95)
            cr.set_line_width(3)
            cr.stroke()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        cr.set_source_rgb(0.85, 0.86, 0.88)
        cr.paint()
        if self.map is None:
            return
        scale, ox, oy = self.view
        cr.save()
        cr.translate(ox, oy)
        cr.scale(scale, scale)
        self._paint_map(cr)
        cr.restore()
        cr.set_font_size(12)
        credit = self.map.credit
        extents = cr.text_extents(credit)
        cr.set_source_rgba(1, 1, 1, 0.8)
        cr.rectangle(width - extents.width - 14, height - 20, extents.width + 14, 20)
        cr.fill()
        cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.move_to(width - extents.width - 7, height - 6)
        cr.show_text(credit)

    # ---------- touch ----------

    def _to_map(self, x: float, y: float) -> tuple[float, float]:
        scale, ox, oy = self.view
        return (x - ox) / scale, (y - oy) / scale

    def _on_blueprint(self, mx: float, my: float) -> bool:
        bp = self.blueprint
        if bp is None:
            return False
        a = -math.radians(bp["turn"])
        dx, dy = mx - bp["cx"], my - bp["cy"]
        u = (dx * math.cos(a) - dy * math.sin(a)) / bp["scale"]
        v = (dx * math.sin(a) + dy * math.cos(a)) / bp["scale"]
        return abs(u) <= bp["w"] / 2 and abs(v) <= bp["h"] / 2

    def _drag_begin(self, _gesture, x: float, y: float) -> None:
        mx, my = self._to_map(x, y)
        on = self._on_blueprint(mx, my)
        self._drag = {"blueprint": on, "x": x, "y": y, "view": list(self.view),
                      "bp": (self.blueprint["cx"], self.blueprint["cy"]) if on else None}

    def _drag_update(self, _gesture, dx: float, dy: float) -> None:
        drag = self._drag
        if drag is None or self._pinch is not None:
            return
        if drag["blueprint"]:
            scale = self.view[0]
            self.blueprint["cx"] = drag["bp"][0] + dx / scale
            self.blueprint["cy"] = drag["bp"][1] + dy / scale
        else:
            self.view = [drag["view"][0], drag["view"][1] + dx, drag["view"][2] + dy]
        self.area.queue_draw()

    def _drag_end(self, _gesture, dx: float, dy: float) -> None:
        drag, self._drag = self._drag, None
        if drag is None:
            return
        if abs(dx) < 8 and abs(dy) < 8:
            self._tapped(drag["x"], drag["y"])
            return
        if drag["blueprint"]:
            self._describe()
            return
        # The view was dragged: when the map's edge comes into view, fetch it centred anew
        scale, ox, oy = self.view
        width, height = self.area.get_width(), self.area.get_height()
        if ox > 0 or oy > 0 or ox + MAP_W * scale < width or oy + MAP_H * scale < height:
            self._recentre(*self._to_map(width / 2, height / 2))

    def _tapped(self, x: float, y: float) -> None:
        mx, my = self._to_map(x, y)
        hit = next((i for i, o in enumerate(self.outlines) if _inside((mx, my), o.get("px") or [])), None)
        if hit is not None:
            self.selected = None if hit == self.selected else hit
            name = self.outlines[hit]["name"]
            if self.selected is not None:
                self._say(f"Building selected{': ' + name if name else ''}. "
                          + ("Fit to building lines the blueprint up with it." if self.blueprint
                             else "Load the blueprint, then Fit to building."))
            self.area.queue_draw()

    def _pinch_begin(self, gesture, _sequence) -> None:
        found, x, y = gesture.get_bounding_box_center()
        mx, my = self._to_map(x, y)
        on = found and self._on_blueprint(mx, my)
        self._pinch = {"blueprint": on, "scale": self.blueprint["scale"] if on else None,
                       "view": list(self.view), "x": x, "y": y}

    def _pinched(self, _gesture, factor: float) -> None:
        pinch = self._pinch
        if pinch is None or factor <= 0:
            return
        if pinch["blueprint"]:
            self.blueprint["scale"] = pinch["scale"] * factor
            self._sync_sliders()
            self._describe()
        else:
            scale, ox, oy = pinch["view"]
            new = max(0.2, min(8.0, scale * factor))
            x, y = pinch["x"], pinch["y"]
            self.view = [new, x - (x - ox) * new / scale, y - (y - oy) * new / scale]
        self.area.queue_draw()

    def _turn_begin(self, _gesture, _sequence) -> None:
        self._turn_start = self.blueprint["turn"] if self.blueprint else None

    def _turned(self, _gesture, _angle: float, delta: float) -> None:
        if self.blueprint is None or getattr(self, "_turn_start", None) is None or \
                not (self._pinch and self._pinch["blueprint"]):
            return
        self.blueprint["turn"] = self._turn_start + math.degrees(delta)
        self._sync_sliders()
        self._describe()
        self.area.queue_draw()

    def _scrolled(self, controller, _dx: float, dy: float) -> bool:
        scale, ox, oy = self.view
        new = max(0.2, min(8.0, scale * (0.9 if dy > 0 else 1.1)))
        width, height = self.area.get_width(), self.area.get_height()
        x, y = width / 2, height / 2
        self.view = [new, x - (x - ox) * new / scale, y - (y - oy) * new / scale]
        self.area.queue_draw()
        return True


def open_site_map(survey) -> None:
    SiteMapDialog(survey).present(survey.get_root())

