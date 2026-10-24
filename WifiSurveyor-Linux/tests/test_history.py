"""Finding surveys again, telling which are the same site, and what changed between them."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from wifisurveyor.core import history, library, report
from wifisurveyor.core.project import Point, Project

DAY = 86400
PLAN = base64.b64encode(b"\x89PNG pretend floor plan").decode()


def survey(name: str, when: float, rssi: int, points: int = 6, plan: str | None = PLAN) -> Project:
    project = Project(name=name, plan_png=plan, width=1000, height=600, meters_per_pixel=0.05)
    for index in range(points):
        project.points.append(Point(x=100 + index * 120, y=300, time=when + index * 60, networks=[
            {"ssid": "Office", "bssid": f"aa:00:00:00:00:{index:02x}", "rssi": rssi - index,
             "freq_mhz": 5180, "channel": 36, "band": "5", "secured": True}],
            spectrum={"segments": [], "noise_5": -92.0}))
    return project


class Folders(unittest.TestCase):
    """A home folder of its own, so nothing touches the real one."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        patches = [mock.patch.dict(os.environ, {"XDG_DATA_HOME": f"{self.home.name}/.local/share"}),
                   mock.patch("wifisurveyor.core.library.home", lambda: Path(self.home.name)),
                   mock.patch("wifisurveyor.core.project.home", lambda: Path(self.home.name))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.home.cleanup)


class LibraryTests(Folders):
    def test_surveys_go_to_documents_and_are_found(self) -> None:
        saved = survey("Warehouse", time.time(), -60).save()
        self.assertEqual(saved.parent, Path(self.home.name) / "Documents" / "Wi-Fi Surveys")
        found = library.known()
        self.assertEqual([info.name for info in found], ["Warehouse"])
        self.assertEqual(found[0].points, 6)
        self.assertTrue(found[0].saved_by)

    def test_desktop_documents_folder_is_followed(self) -> None:
        config = Path(self.home.name) / ".config"
        config.mkdir()
        (config / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Dokumente"\n')
        self.assertEqual(library.surveys_dir(), Path(self.home.name) / "Dokumente" / "Wi-Fi Surveys")

    def test_files_saved_anywhere_are_remembered(self) -> None:
        elsewhere = Path(self.home.name) / "jobs" / "site.wsurvey"
        elsewhere.parent.mkdir()
        survey("Elsewhere", time.time(), -60).save(elsewhere)
        self.assertIn(str(elsewhere.resolve()), [info.path for info in library.known()])
        elsewhere.unlink()
        self.assertEqual(library.known(), [])            # gone: dropped, not listed

    def test_older_default_folder_is_still_searched(self) -> None:
        old = Path(self.home.name) / ".local" / "share" / "wifisurveyor"
        old.mkdir(parents=True)
        Project(name="Old").write(old / "Old.wsurvey")
        (old / "recovery.wsurvey").write_text(json.dumps(Project().to_dict()))
        self.assertEqual([info.name for info in library.known()], ["Old"])

    def test_root_only_files_move_to_the_shared_folder(self) -> None:
        moved = library.shared_path_for("/root/.local/share/wifisurveyor/Site.wsurvey")
        self.assertEqual(moved, library.surveys_dir() / "Site.wsurvey")
        mine = library.surveys_dir() / "Mine.wsurvey"
        self.assertEqual(library.shared_path_for(mine), mine)

    def test_a_windows_survey_is_listed_and_matches_the_same_plan(self) -> None:
        windows = {
            "FormatVersion": 1,
            "Name": "Head office",
            "FloorPlanBase64": PLAN,
            "FloorPlanWidth": 1000,
            "FloorPlanHeight": 600,
            "Points": [{"X": 1, "Y": 2, "Time": "2026-09-01T10:15:30Z"}],
        }
        path = library.surveys_dir() / "head.wsurvey"
        path.write_text(json.dumps(windows))
        library.remember(path)                    # opening one notes it; that must not zero the count
        found = library.known()
        self.assertEqual([info.name for info in found], ["Head office"])
        self.assertEqual(found[0].points, 1)
        self.assertGreater(found[0].surveyed_at, 0)
        self.assertEqual(found[0].size, (1000, 600))
        linux = survey("Monday", time.time(), -60)
        self.assertEqual([info.name for info in library.same_site(linux)], ["Head office"])

    def test_same_site_by_follow_up_or_identical_plan(self) -> None:
        now = time.time()
        first = survey("Monday", now - 3 * DAY, -65)
        first.save(library.surveys_dir() / "monday.wsurvey")
        follow = first.follow_up()
        self.assertEqual(follow.site_id, first.site_id)
        self.assertEqual(follow.walls, first.walls)
        self.assertEqual(follow.points, [])
        self.assertEqual(follow.follow_up().name, follow.name)     # one date, not one per follow-up
        # A survey made separately but on the very same floor plan counts too
        separate = survey("Same plan", now - 10 * DAY, -70)
        separate.save(library.surveys_dir() / "same plan.wsurvey")
        # A different building does not
        survey("Elsewhere", now, -60, plan=base64.b64encode(b"other plan").decode()).save(
            library.surveys_dir() / "elsewhere.wsurvey")
        found = library.same_site(follow)
        self.assertEqual([info.name for info in found], ["Same plan", "Monday"])  # oldest first

    def test_a_plan_of_another_size_never_lines_up(self) -> None:
        first = survey("Monday", time.time(), -65)
        first.save(library.surveys_dir() / "monday.wsurvey")
        resized = first.follow_up()
        resized.width = 2000
        self.assertEqual(library.same_site(resized), [])

    def test_old_files_without_a_site_id_open(self) -> None:
        payload = survey("Legacy", time.time(), -60).to_dict()
        del payload["site_id"]
        legacy = Project.from_dict(payload)
        self.assertIsNone(legacy.site_id)
        self.assertEqual(legacy.follow_up().plan_png, legacy.plan_png)


class SpeedTestTableTests(unittest.TestCase):
    def test_table_belongs_to_the_survey(self) -> None:
        project = Project()
        project.speed_tests = {"enabled": True, "rows": [{"ssid": "Office", "adapter": "wlan0", "server": "x"}]}
        again = Project.from_dict(project.to_dict())
        self.assertEqual(again.speed_tests, project.speed_tests)
        self.assertEqual(Project().speed_tests, {})       # a new survey tests nothing
        self.assertEqual(project.follow_up().speed_tests, project.speed_tests)


class ChangeTests(unittest.TestCase):
    def test_figures_and_what_changed(self) -> None:
        now = time.time()
        before = history.figures(survey("Monday", now - 3 * DAY, -78))
        after = history.figures(survey("Thursday", now, -62))
        self.assertEqual(before["points"], 6)
        self.assertLess(before["signal"], after["signal"])
        self.assertEqual(history.change(before, after, "signal")[1], "better")
        self.assertEqual(history.change(before, after, "noise")[1], "same")
        text = history.describe_change(before, after)
        self.assertTrue(text.startswith("Better:"), text)
        self.assertIn("median signal up 16 dB (-80 dBm → -64 dBm)", text)
        self.assertIn("points meeting Data: 0% → 100%", text)
        self.assertNotIn("Worse", text)

    def test_lower_is_better_figures(self) -> None:
        before = {"noise": -95.0, "issues": 2, "requirement": "Data"}
        after = {"noise": -85.0, "issues": 6, "requirement": "Data"}
        self.assertEqual(history.change(before, after, "noise")[1], "worse")
        self.assertIn("4 more high-priority issues", history.describe_change(before, after))

    def test_spans_people_would_say(self) -> None:
        self.assertEqual(history.span(3 * DAY), "3 days")
        self.assertEqual(history.span(5 * 7 * DAY), "5 weeks")
        self.assertEqual(history.span(200 * DAY), "7 months")
        self.assertEqual(history.span(800 * DAY), "2.2 years")

    def test_summary_covers_previous_and_first(self) -> None:
        now = time.time()
        rows = [history.figures(survey(n, now - d * DAY, r)) for n, d, r in
                (("March", 220, -80), ("Monday", 3, -72), ("Thursday", 0, -62))]
        said = history.summary(rows)
        self.assertEqual(len(said), 2)
        self.assertIn("Since the previous survey", said[0])
        self.assertIn("3 days earlier", said[0])
        self.assertIn("Since the first survey", said[1])
        self.assertIn("7 months earlier, 3 surveys in all", said[1])

    def test_report_history_section(self) -> None:
        now = time.time()
        rows = [history.figures(survey("Monday", now - 3 * DAY, -78)),
                {**history.figures(survey("Thursday", now, -62)), "this": True}]
        project = survey("Thursday", now, -62)
        html = report.build_html(project, None, lambda p: None,
                                 history={"rows": rows, "maps": [(rows[0]["when"], b"png0"),
                                                                 (rows[1]["when"], b"png1")]})
        self.assertIn("History of this site", html)
        self.assertIn("Since the previous survey", html)
        self.assertIn("(this report)", html)
        self.assertIn("▲", html)                         # signal went up: marked better
        self.assertIn("<svg", html)                      # a trend per measure
        self.assertIn("Signal, survey by survey", html)
        alone = report.build_html(project, None, lambda p: None, history={"rows": rows[1:]})
        self.assertNotIn("History of this site", alone)


if __name__ == "__main__":
    unittest.main()
