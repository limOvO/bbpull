"""Tests for the Qt build.

Two groups:

* **Pure logic / model tests** run anywhere Qt can create an offscreen
  application, and cover the parts that actually changed: the virtualised
  models, selection semantics, and the delegate geometry that hit-testing
  depends on.
* **Performance guards** assert the *shape* of the fix - widget count and
  per-row cost must not grow with row count. They use loose bounds so they fail
  only on a real regression, not on machine noise.

Run directly:
    python -m unittest tests.test_gui_qt

Excluded from `unittest discover` by default (opt in with
`BBPULL_RUN_QT_TESTS=1`) for the same reason as the Tk suite: it builds a real
Qt application inside the runner's process.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from bbpull.gui_qt import delegates, models, theme, window as qt_window

    _app = QApplication.instance() or QApplication([])
    HAS_QT = True
except Exception:  # noqa: BLE001 - PySide6 or a display is missing
    HAS_QT = False

QT_ENABLED = (
    os.environ.get("BBPULL_RUN_QT_TESTS") == "1"
    or __name__ == "tests.test_gui_qt"
)


def build_tree():
    """A small catalog tree: one folder with children, plus loose items."""
    return [
        {
            "id": "f1", "title": "Week 1", "kind": "folder",
            "handler": "resource/x-bb-folder", "itemCount": 2, "childCount": 2,
            "downloadable": False, "hasBody": False, "synthetic": False,
            "children": [
                {"id": "d1", "title": "Notes", "kind": "document",
                 "handler": "resource/x-bb-document", "itemCount": 1,
                 "childCount": 0, "downloadable": True, "hasBody": True,
                 "synthetic": False, "children": []},
                {"id": "d2", "title": "Slides.pdf", "kind": "file",
                 "handler": "resource/x-bb-file", "itemCount": 1,
                 "childCount": 0, "downloadable": True, "hasBody": False,
                 "synthetic": False, "children": []},
            ],
        },
        {"id": "d3", "title": "Loose doc", "kind": "document",
         "handler": "resource/x-bb-document", "itemCount": 1, "childCount": 0,
         "downloadable": True, "hasBody": True, "synthetic": False, "children": []},
        {"id": "a1", "title": "Quiz", "kind": "assessment",
         "handler": "resource/x-bb-asmt-test-link", "itemCount": 1,
         "childCount": 0, "downloadable": False, "hasBody": False,
         "synthetic": False, "children": []},
    ]


def flat_tree(count):
    return [
        {"id": f"n{i}", "title": f"項目 {i} 課程教材與文件範例", "kind": "document",
         "handler": "resource/x-bb-document", "itemCount": 1, "childCount": 0,
         "downloadable": True, "hasBody": True, "synthetic": False, "children": []}
        for i in range(count)
    ]


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class CatalogModelTests(unittest.TestCase):
    def setUp(self):
        from bbpull.catalog import Catalog, walk

        self.catalog = Catalog("_1_1", "Test", "http://x", build_tree())
        self.model = models.CatalogModel()
        self.model.set_catalog(self.catalog)
        self.parents = {}
        for node in walk(self.catalog.tree):
            self.parents[node["id"]] = None
        self.parents["d1"] = "f1"
        self.parents["d2"] = "f1"
        self.model.selection.set_tree(self.catalog.tree, self.parents)

    def test_root_rows(self):
        self.assertEqual(self.model.rowCount(), 3)
        self.assertEqual(self.model.node_at(0)["id"], "f1")

    def test_navigate_into_folder_and_back(self):
        self.model.set_folder("f1")
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.node_at(0)["id"], "d1")
        self.model.set_folder("root")
        self.assertEqual(self.model.rowCount(), 3)

    def test_missing_folder_is_empty_not_crashing(self):
        self.model.set_folder("nope")
        self.assertEqual(self.model.rowCount(), 0)

    def test_filter_narrows_the_folder(self):
        self.model.set_folder("f1")
        self.model.set_filter("slide")
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.node_at(0)["id"], "d2")

    def test_whole_course_scope_searches_everything(self):
        self.model.scope_all = True
        self.model.set_filter("Notes")
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.node_at(0)["id"], "d1")

    def test_row_of_finds_visible_rows(self):
        self.assertEqual(self.model.row_of("d3"), 1)
        self.assertEqual(self.model.row_of("nope"), -1)

    def test_meta_text_mentions_counts(self):
        self.assertIn("2 個項目", self.model.meta_text(self.model.node_at(0)))

    def test_data_roles(self):
        index = self.model.index(0, 0)
        self.assertEqual(self.model.data(index, Qt.DisplayRole), "Week 1")
        self.assertEqual(self.model.data(index, models.NodeRole)["id"], "f1")
        self.assertFalse(self.model.data(index, models.SelectedRole))


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class AttachmentModelTests(unittest.TestCase):
    """Embedded files are shown as indented rows, not buried in a dialog."""

    BODY = ('<p>Dear students</p>'
            '<a href="https://twc.blackboard.com/bbcswebdav/pid-1-dt-content-'
            'rid-2_1/xid-2_1" data-bbfile=\'{"linkName":"notes.pdf"}\'>notes.pdf</a>')

    def setUp(self):
        from bbpull.catalog import Catalog

        tree = [
            {"id": "doc", "title": "Manual", "kind": "document",
             "attachmentCount": 1, "hasBody": True, "downloadable": True,
             "payload": {"body": self.BODY}, "children": []},
            {"id": "plain", "title": "No attachments", "kind": "document",
             "attachmentCount": 0, "hasBody": True, "downloadable": True,
             "payload": {"body": "<p>text</p>"}, "children": []},
        ]
        self.catalog = Catalog("_1_1", "Test", "http://x", tree)
        self.model = models.CatalogModel()
        self.model.set_catalog(self.catalog)

    def test_attachment_row_follows_its_document(self):
        self.assertEqual(self.model.rowCount(), 3)
        self.assertEqual(self.model.node_at(0)["id"], "doc")
        attachment = self.model.node_at(1)
        self.assertEqual(attachment["kind"], "attachment")
        self.assertEqual(attachment["title"], "notes.pdf")
        self.assertEqual(attachment["parentContentId"], "doc")
        self.assertTrue(attachment["virtual"])

    def test_attachment_row_has_no_content_id(self):
        attachment = self.model.node_at(1)
        self.assertIsNone(self.catalog.find(attachment["id"]),
                          "virtual rows must not be found as content items")

    def test_filter_can_match_an_attachment(self):
        self.model.set_filter("notes.pdf")
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.node_at(0)["kind"], "attachment")


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class SelectionTests(unittest.TestCase):
    def setUp(self):
        from bbpull.catalog import Catalog, walk

        self.catalog = Catalog("_1_1", "Test", "http://x", build_tree())
        self.parents = {"f1": None, "d1": "f1", "d2": "f1", "d3": None, "a1": None}
        self.selection = models.SelectionState()
        self.selection.set_tree(self.catalog.tree, self.parents)
        self.children_of = lambda nid: (self.catalog.find(nid) or {}).get("children") or []

    def test_toggle_adds_and_removes(self):
        self.selection.toggle("d3")
        self.assertTrue(self.selection.is_selected("d3"))
        self.selection.toggle("d3")
        self.assertFalse(self.selection.is_selected("d3"))

    def test_folder_tick_implies_children(self):
        self.selection.toggle("f1")
        self.assertTrue(self.selection.is_implied("d1"))
        self.assertTrue(self.selection.is_implied("d2"))
        self.assertEqual(self.selection.topmost(), ["f1"])

    def test_unticking_implied_child_releases_the_folder(self):
        """The file-manager behaviour: excluding one item must not drop the rest."""
        self.selection.toggle("f1")
        self.selection.toggle("d1", children_of=self.children_of)
        self.assertFalse(self.selection.is_selected("f1"))
        self.assertFalse(self.selection.is_selected("d1"))
        self.assertTrue(self.selection.is_selected("d2"),
                        "the sibling must stay selected")

    def test_cross_folder_selection_keeps_both(self):
        self.selection.toggle("d1")
        self.selection.toggle("d3")
        self.assertEqual(sorted(self.selection.topmost()), ["d1", "d3"])

    def test_clear(self):
        self.selection.add_many(["d1", "d3"])
        self.selection.clear()
        self.assertEqual(self.selection.topmost(), [])

    def test_topmost_collapses_nested(self):
        self.selection.add_many(["f1", "d1"])
        self.assertEqual(self.selection.topmost(), ["f1"])


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class CourseListModelTests(unittest.TestCase):
    REAL = [
        {"courseId": "_1_1", "name": "[2026/27-1] Alpha", "role": "Student"},
        {"courseId": "_2_1", "name": "[2025/26-3] Beta", "role": "Student"},
        {"courseId": "_3_1", "name": "[2025/26-1] Gamma", "role": "Student"},
        {"courseId": "_4_1", "name": "Library", "role": "Student"},
    ]

    def setUp(self):
        from bbpull.gui.courses import parse_courses

        self.model = models.CourseListModel()
        self.model.set_courses(parse_courses(self.REAL), "_1_1")

    def test_headers_and_rows(self):
        # 3 year headers (2026/27, 2025/26, unspecified) + 4 courses
        self.assertEqual(self.model.rowCount(), 7)

    def test_year_headers_descend(self):
        headers = [self.model.index(r, 0).data(models.NodeRole)
                   for r in range(self.model.rowCount())
                   if self.model.is_header(self.model.index(r, 0))]
        self.assertEqual(headers[0], "2026/27")
        self.assertEqual(headers[-1], "未標示學年")

    def test_terms_descend_within_a_year(self):
        metas = [self.model.meta_at(self.model.index(r, 0))
                 for r in range(self.model.rowCount())
                 if not self.model.is_header(self.model.index(r, 0))]
        year_2025 = [m.term for m in metas if m and m.year == "2025/26"]
        self.assertEqual(year_2025, ["3", "1"])

    def test_headers_are_not_selectable_items(self):
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            if self.model.is_header(index):
                self.assertTrue(self.model.data(index, models.ImpliedRole))
                self.assertIsNone(self.model.meta_at(index))

    def test_active_course_is_marked(self):
        rows = [self.model.index(r, 0) for r in range(self.model.rowCount())]
        active = [r for r in rows if self.model.data(r, models.SelectedRole)]
        self.assertEqual(len(active), 1)
        self.assertEqual(self.model.meta_at(active[0]).course_id, "_1_1")

    def test_set_active_moves_the_marker(self):
        self.model.set_active("_2_1")
        rows = [self.model.index(r, 0) for r in range(self.model.rowCount())]
        active = [r for r in rows if self.model.data(r, models.SelectedRole)]
        self.assertEqual(self.model.meta_at(active[0]).course_id, "_2_1")


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class DelegateGeometryTests(unittest.TestCase):
    """Painting and hit-testing must agree, or clicks land on the wrong button."""

    def setUp(self):
        self.pal = theme.Palette("dark")
        self.delegate = delegates.RowDelegate(self.pal, None)

    def test_layout_is_inside_the_row(self):
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        node = build_tree()[0]
        check, icon, text, buttons = self.delegate._layout(rect, node)
        for piece in (check, icon, text):
            self.assertGreaterEqual(piece.x(), rect.x())
            self.assertLessEqual(piece.right(), rect.right())
        for _action, button in buttons:
            self.assertLessEqual(button.right(), rect.right())

    def test_folder_gets_both_buttons(self):
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        _c, _i, _t, buttons = self.delegate._layout(rect, build_tree()[0])
        self.assertEqual([a for a, _r in buttons], ["open", "download"])

    def test_assessment_gets_preview_only(self):
        """An assessment cannot be downloaded, but it can still be inspected."""
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        _c, _i, _t, buttons = self.delegate._layout(rect, build_tree()[2])
        self.assertEqual([a for a, _r in buttons], ["preview"])

    def test_document_row_has_preview_and_download(self):
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        _c, _i, _t, buttons = self.delegate._layout(rect, build_tree()[1])
        self.assertEqual([a for a, _r in buttons], ["preview", "download"])

    def test_preview_is_the_primary_rightmost_action(self):
        """Preview is the default action; downloading is deliberate."""
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        _c, _i, _t, buttons = self.delegate._layout(rect, build_tree()[1])
        rects = dict(buttons)
        self.assertGreater(rects["preview"].left(), rects["download"].left(),
                           "preview takes the right-most (primary) position")
        self.assertEqual(rects["preview"].right(), rect.right() - 11,
                         "the primary button should sit against the row edge")

    def test_folders_have_no_preview_button(self):
        """Double-clicking a folder navigates; "開啟" already covers it."""
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        _c, _i, _t, buttons = self.delegate._layout(rect, build_tree()[0])
        self.assertNotIn("preview", [action for action, _r in buttons])

    def test_attachment_row_is_indented_and_not_selectable(self):
        from PySide6.QtCore import QRect

        from bbpull.preview import attachment_row

        node = attachment_row({"id": "doc", "title": "Manual"},
                              {"url": "https://x/y.pdf", "name": "y.pdf"})
        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        check, icon, _text, buttons = self.delegate._layout(rect, node)
        self.assertTrue(check.isNull(), "no checkbox on a virtual attachment row")
        self.assertGreater(icon.x(), 10, "attachment rows are indented")
        self.assertEqual([a for a, _r in buttons], ["preview", "download"])

    def test_button_at_matches_painted_rect(self):
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        node = build_tree()[0]
        _c, _i, _t, buttons = self.delegate._layout(rect, node)
        for action, button in buttons:
            self.assertEqual(self.delegate.button_at(rect, node, button.center()),
                             action)
        self.assertIsNone(self.delegate.button_at(rect, node,
                                                  __import__("PySide6.QtCore",
                                                             fromlist=["QPoint"])
                                                  .QPoint(rect.x() + 2, rect.center().y())))

    def test_row_height_is_compact(self):
        self.assertLessEqual(delegates.ROW_HEIGHT, 52)


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class QtPerformanceTests(unittest.TestCase):
    """The whole point of the Qt build: cost must not scale with row count."""

    def test_building_a_huge_model_is_fast(self):
        model = models.CatalogModel()
        from bbpull.catalog import Catalog

        catalog = Catalog("_1_1", "Perf", "http://x", flat_tree(5000))
        start = time.perf_counter()
        model.set_catalog(catalog)
        model.set_folder("root")
        elapsed = (time.perf_counter() - start) * 1000
        self.assertEqual(model.rowCount(), 5000)
        # Tk needed ~14 s for this many rows; the budget here is loose on
        # purpose so the test only fails on a real regression.
        self.assertLess(elapsed, 500, f"building a 5,000-row model took {elapsed:.0f} ms")

    def test_row_count_does_not_create_widgets(self):
        """A model is data only: no QWidget per row, which is the entire fix."""
        from bbpull.catalog import Catalog
        from PySide6.QtWidgets import QWidget

        model = models.CatalogModel()
        model.set_catalog(Catalog("_1_1", "Perf", "http://x", flat_tree(2000)))
        model.set_folder("root")
        widgets = [c for c in model.parent().findChildren(QWidget)] \
            if model.parent() else []
        self.assertEqual(len(widgets), 0, "the model must not own widgets")
        self.assertEqual(model.rowCount(), 2000)

    def test_scrolling_cost_does_not_scale_with_row_count(self):
        """The property that matters: cost is flat in the number of rows.

        Timed as a *ratio*, not an absolute budget. An absolute bound measures the
        machine and whatever else shares the process as much as the code: the
        earlier version of this test asserted "< 3 s for 3,000 rows" and began
        failing at ~11.5 s once neighbouring Qt tests changed the process state.
        Ten times the rows costing about the same per scroll step is the actual
        claim, and it holds regardless of how fast the machine is.
        """
        from bbpull.catalog import Catalog
        from PySide6.QtWidgets import QListView

        def per_step_ms(rows, steps=40):
            view = QListView()
            view.setUniformItemSizes(True)
            view.resize(800, 600)
            model = models.CatalogModel()
            model.set_catalog(Catalog("_1_1", "Perf", "http://x", flat_tree(rows)))
            model.set_folder("root")
            view.setModel(model)
            view.setItemDelegate(delegates.RowDelegate(theme.Palette("dark"), view))
            view.show()
            _app.processEvents()

            stride = max(1, rows // steps)
            positions = list(range(0, rows, stride))
            start = time.perf_counter()
            for value in positions:
                view.verticalScrollBar().setValue(value)
                _app.processEvents()
            elapsed = (time.perf_counter() - start) * 1000

            view.hide()
            view.deleteLater()
            _app.processEvents()
            return elapsed / max(1, len(positions))

        small = per_step_ms(300)
        large = per_step_ms(3000)

        self.assertLess(large, max(small, 0.5) * 4,
                        f"scrolling slowed down with more rows: {small:.2f} ms/step "
                        f"at 300 rows vs {large:.2f} ms/step at 3,000 rows")
        self.assertLess(large, 150,
                        f"{large:.2f} ms per scroll step is not virtualised")


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class ThemeTests(unittest.TestCase):
    def test_stylesheet_covers_both_modes(self):
        for mode in ("dark", "light"):
            qss = theme.stylesheet(theme.Palette(mode))
            self.assertIn("QListView", qss)
            self.assertIn("QPushButton#Primary", qss)
            self.assertIn("#TopBar", qss)

    def test_icons_render_for_every_name(self):
        from PySide6.QtGui import QPixmap

        names = list(theme.UI_GLYPHS) + [
            "folder", "document", "file", "link", "assessment", "tool",
            "announcement", "other",
        ]
        for name in names:
            icon = theme.make_icon(name, "#ffffff", 32)
            self.assertFalse(icon.isNull(), name)
            pixmap = icon.pixmap(QPixmap(32, 32).size())
            self.assertFalse(pixmap.isNull(), name)


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class WorkerTests(unittest.TestCase):
    @staticmethod
    def _wait(pred, seconds=10):
        """Pump the event loop until `pred()` or the deadline.

        Cross-thread signals are delivered when the receiving thread runs its
        event loop, so a plain `processEvents()` spin can miss them; this also
        flushes deferred-delete events and yields the GIL.
        """
        from PySide6.QtCore import QCoreApplication, QEvent

        deadline = time.time() + seconds
        while time.time() < deadline:
            QCoreApplication.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            if pred():
                return True
            time.sleep(0.01)
        return pred()

    def test_task_reports_success(self):
        from bbpull.gui_qt.workers import TaskRunner

        runner = TaskRunner()
        results = []
        runner.start(lambda: 1 + 1, results.append)
        self.assertTrue(self._wait(lambda: bool(results)),
                        "the success signal never arrived")
        self.assertEqual(results, [2])

    def test_task_reports_failure_without_crashing(self):
        from bbpull.gui_qt.workers import TaskRunner

        runner = TaskRunner()
        errors = []

        def boom():
            raise RuntimeError("kaboom")

        runner.start(boom, lambda _r: None, errors.append)
        self.assertTrue(self._wait(lambda: bool(errors)),
                        "the failure signal never arrived")
        self.assertIn("kaboom", errors[0])

    def test_task_runner_tracks_and_cancels(self):
        from bbpull.gui_qt.workers import TaskRunner

        runner = TaskRunner()
        done = []
        runner.start(lambda: "ok", done.append)
        self.assertTrue(self._wait(lambda: bool(done)))
        self.assertTrue(self._wait(lambda: runner.active_count == 0),
                        "finished tasks must be released")
        runner.cancel_all()

    def test_job_poller_emits_snapshots(self):
        from bbpull.gui_qt.workers import JobPoller
        from bbpull.selective import JobManager

        manager = JobManager()
        manager.create("download", "demo")
        poller = JobPoller(manager, parent=None, interval_ms=20)
        seen = []
        poller.updated.connect(seen.append)
        poller.start()
        try:
            self.assertTrue(self._wait(lambda: bool(seen)),
                            "the poller never emitted")
        finally:
            poller.stop()
        self.assertEqual(seen[-1][0]["label"], "demo")


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class CourseCardLayoutTests(unittest.TestCase):
    """Regression guards for the sidebar text overlap.

    Reported: the Qt sidebar drew text on top of other text. Cause: fixed pixel
    offsets put the title band (y 32..62) straight through the subtitle band
    (y 40..54), and a two-line wrapped title also spilled past the card. These
    tests pin the properties that were violated.
    """

    SCALES = (1.0, 1.25, 1.5, 2.0)

    def bands(self, scale, height=None):
        from PySide6.QtCore import QRect

        height = height or delegates.course_row_height(scale)
        rect = QRect(0, 0, 260, height)
        return delegates.course_card_geometry(rect, scale)

    def test_title_and_subtitle_never_overlap(self):
        for scale in self.SCALES:
            card, badge, title, meta, _fonts = self.bands(scale)
            self.assertFalse(title.intersects(meta),
                             f"scale {scale}: title {title} overlaps meta {meta}")

    def test_badge_and_title_never_overlap(self):
        for scale in self.SCALES:
            _card, badge, title, _meta, _fonts = self.bands(scale)
            self.assertFalse(badge.intersects(title),
                             f"scale {scale}: badge overlaps title")

    def test_all_bands_sit_inside_the_card(self):
        for scale in self.SCALES:
            card, badge, title, meta, _fonts = self.bands(scale)
            for name, band in (("badge", badge), ("title", title), ("meta", meta)):
                self.assertGreaterEqual(band.top(), card.top() - 0.01,
                                        f"scale {scale}: {name} above card")
                self.assertLessEqual(band.bottom(), card.bottom() + 0.01,
                                     f"scale {scale}: {name} below card")

    def test_bands_are_stacked_in_reading_order(self):
        for scale in self.SCALES:
            _card, badge, title, meta, _fonts = self.bands(scale)
            self.assertLessEqual(badge.bottom(), title.top() + 0.01)
            self.assertLessEqual(title.bottom(), meta.top() + 0.01)

    def test_row_height_fits_all_bands(self):
        for scale in self.SCALES:
            height = delegates.course_row_height(scale)
            card, _badge, _title, meta, _fonts = self.bands(scale, height)
            self.assertLessEqual(meta.bottom(), card.bottom() + 0.01,
                                 f"scale {scale}: content exceeds the row height")
            self.assertGreater(height, 40)

    def test_row_height_scales_with_dpi(self):
        small = delegates.course_row_height(1.0)
        large = delegates.course_row_height(2.0)
        self.assertGreater(large, small)


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class WrapLinesTests(unittest.TestCase):
    """`drawText(TextWordWrap)` paints outside its rect; `wrap_lines` must not."""

    def setUp(self):
        from PySide6.QtGui import QFontMetricsF

        self.font = delegates.ui_font(11)
        self.metrics = QFontMetricsF(self.font)

    def test_short_text_is_one_line(self):
        lines = delegates.wrap_lines("Short", self.metrics, 200)
        self.assertEqual(lines, ["Short"])

    def test_long_text_wraps_within_the_limit(self):
        text = ("NUR2051/NUR2046 Nursing Practicum I (NY2023) very long name here")
        lines = delegates.wrap_lines(text, self.metrics, 180, max_lines=2)
        self.assertLessEqual(len(lines), 2)
        self.assertTrue(lines[0])

    def test_every_line_fits_the_width(self):
        text = "Averyveryverylongsinglewordthatcannotwrapatall 2345678901234567890"
        for width in (60, 120, 200):
            lines = delegates.wrap_lines(text, self.metrics, width, max_lines=2)
            for line in lines:
                self.assertLessEqual(self.metrics.horizontalAdvance(line), width + 1,
                                     f"line {line!r} overflows {width}px")

    def test_dropped_text_is_shown_as_an_ellipsis(self):
        text = " ".join(f"word{i}" for i in range(60))
        lines = delegates.wrap_lines(text, self.metrics, 150, max_lines=2)
        self.assertLessEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"),
                        "a truncated title must say so: " + repr(lines[-1]))

    def test_empty_and_tiny_inputs(self):
        self.assertEqual(delegates.wrap_lines("", self.metrics, 100), [""])
        self.assertEqual(delegates.wrap_lines("x", self.metrics, 0), [""])
        self.assertEqual(delegates.wrap_lines(None, self.metrics, 100), [""])

    def test_delegate_row_text_bands_do_not_overlap(self):
        """The content rows use the same metric-stacked approach."""
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtGui import QFontMetricsF, QPixmap, QPainter

        pal = theme.Palette("dark")
        delegate = delegates.RowDelegate(pal, None)
        node = build_tree()[0]
        rect = QRect(0, 0, 700, delegates.ROW_HEIGHT)
        _check, _icon, text_rect, _buttons = delegate._layout(rect, node)

        tfm = QFontMetricsF(delegates.ui_font(12))
        mfm = QFontMetricsF(delegates.ui_font(10))
        total = tfm.height() + 1 + mfm.height()
        self.assertLessEqual(total, text_rect.height() + 1,
                             "both text lines must fit inside the row")


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class ConnectDialogSubmitTests(unittest.TestCase):
    """Pressing 登入 must actually work.

    It did not: `_submit_connect` imported `..gui.wizard`, which does not exist
    (the module is `bbpull.wizard`), so the shipped executable answered a login
    attempt with `No module named 'bbpull.gui.wizard'`. Nothing caught it because
    the import is deferred inside the handler and no test ever submitted the
    dialog.

    These run the handler with a fake session and a temporary `.env`, so every
    save mode is exercised without touching the network or the real config.
    """

    def setUp(self):
        import tempfile

        from bbpull.config import load_config
        from bbpull.gui_qt import window as qt_window

        self.qt_window = qt_window
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(None)
        cfg.username = "tester"
        cfg.password = "test-old-password"
        cfg.env_file = os.path.join(self.tmp.name, ".env")
        cfg.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        self.cfg = cfg

        self.session_calls = []
        self._saved_session = qt_window.LearnSession

        # Bind the shared list to a local name: inside the fake, `self` is the
        # fake session, so `self.session_calls.append(...)` would quietly create
        # an attribute on it and leave the test's list empty. That mistake made
        # the first version of this test assert nothing.
        calls = self.session_calls

        class FakeSession:
            def __init__(self, config, log=None):
                self.config = config
                self.log = log

            def login(self, force=False):
                calls.append(force)
                return True

        qt_window.LearnSession = FakeSession

        self.app = qt_window.BbpullApp if hasattr(qt_window, "BbpullApp") else None
        self.win = qt_window.MainWindow(cfg, lambda *a: None)
        self.win.hide()

        # Run "background" work inline so the test is deterministic.
        class InlineTasks:
            def start(inner, fn, on_ok, on_fail=None, *args, **kwargs):
                try:
                    result = fn()
                except Exception as exc:  # noqa: BLE001
                    if on_fail:
                        on_fail(str(exc))
                    return None
                if on_ok:
                    on_ok(result)
                return None

            def cancel_all(inner):
                pass

        self.win.tasks = InlineTasks()

        self.logged_in = []
        self.win._on_login = lambda session: self.logged_in.append(session)

    def tearDown(self):
        self.qt_window.LearnSession = self._saved_session
        try:
            self.win._shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def _dialog(self, save_mode, password="test-new-password"):
        """A dialog wired exactly as `open_connect()` wires it.

        `on_submit` is connected so `_submit()` - the real 登入 button handler,
        including its validation - can be exercised rather than calling the
        window's handler directly and skipping the checks in between.
        """
        dialog = self.qt_window.ConnectDialog(self.cfg, self.win,
                                              on_submit=self.win._submit_connect)
        dialog.base_url.setText("https://twc.blackboard.com")
        dialog.username.setText("23002220")
        dialog.password.setText(password)
        dialog.save_mode.setCurrentText(save_mode)
        return dialog

    def test_submit_does_not_raise_a_missing_module(self):
        """The exact regression: the handler must import what exists."""
        dialog = self._dialog("不要儲存")
        dialog._submit()                          # must not raise
        self.assertTrue(self.session_calls, "login was never attempted")

    def test_every_save_mode_works(self):
        for mode in ("加密儲存（推薦）", "存進 .env（明文）", "不要儲存", "維持現狀"):
            with self.subTest(mode=mode):
                dialog = self._dialog(mode)
                dialog._submit()
                self.assertEqual(dialog.values()["save"],
                                 {"加密儲存（推薦）": "secure", "存進 .env（明文）": "env",
                                  "不要儲存": "none", "維持現狀": "keep"}[mode])
        self.assertTrue(self.session_calls, "no save mode reached the login call")

    def test_env_mode_writes_the_file(self):
        dialog = self._dialog("存進 .env（明文）")
        dialog._submit()
        self.assertTrue(os.path.isfile(self.cfg.env_file),
                        "the .env file should have been written")
        text = open(self.cfg.env_file, encoding="utf-8").read()
        self.assertIn("BB_USERNAME", text)

    def test_login_is_forced_on_submit(self):
        """Credentials just changed, so a cached session must not be reused."""
        self._dialog("不要儲存")._submit()
        self.assertEqual(self.session_calls, [True])

    def test_empty_password_is_rejected_before_the_network(self):
        dialog = self._dialog("不要儲存", password="")
        dialog._submit()
        self.assertEqual(self.session_calls, [],
                         "an empty password must not reach the network")
        self.assertTrue(dialog.error.text(), "the dialog should say what is wrong")

    def test_failure_is_shown_in_the_dialog(self):
        """A bad password should say so in the dialog, not vanish."""
        original = self.qt_window.LearnSession

        class Failing:
            def __init__(self, config, log=None):
                pass

            def login(self, force=False):
                raise RuntimeError("invalid credentials")

        self.qt_window.LearnSession = Failing
        try:
            dialog = self._dialog("不要儲存")
            dialog._submit()
        finally:
            self.qt_window.LearnSession = original
        self.assertIn("invalid credentials", dialog.error.text())


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class PreviewDialogTests(unittest.TestCase):
    """Previewing must show the body and never download by itself."""

    def setUp(self):
        self.pal = theme.Palette("dark")

    def _node(self, **overrides):
        node = {
            "id": "_101_1", "title": "Week 1 notes", "kind": "document",
            "downloadable": True,
            "payload": {"body": "<p>Hello preview body</p>",
                        "modified": "2026-08-28T08:34:34.214Z",
                        "contentHandler": {"id": "resource/x-bb-document"}},
        }
        node.update(overrides)
        return node

    def test_document_body_is_shown(self):
        from PySide6.QtWidgets import QPushButton

        from bbpull.gui_qt.preview import PreviewDialog

        dialog = PreviewDialog(self._node(), self.pal,
                               path_text="Week 1 › Notes",
                               on_download=lambda: None)
        try:
            self.assertIn("Hello preview body", dialog.body.toPlainText())
            labels = [button.text() for button in dialog.findChildren(QPushButton)]
            self.assertIn("下載此項目", labels)
        finally:
            dialog.close()

    def test_download_button_triggers_the_callback(self):
        from bbpull.gui_qt.preview import PreviewDialog

        calls = []
        dialog = PreviewDialog(self._node(), self.pal,
                               on_download=lambda: calls.append(1))
        try:
            dialog._download()
        finally:
            dialog.close()
        self.assertEqual(calls, [1])

    def test_assessment_has_no_download_but_explains_itself(self):
        from PySide6.QtWidgets import QPushButton

        from bbpull.gui_qt.preview import PreviewDialog

        dialog = PreviewDialog(self._node(kind="assessment", downloadable=False,
                                          payload={}), self.pal)
        try:
            self.assertIn("預覽", dialog.body.toPlainText())
            labels = [button.text() for button in dialog.findChildren(QPushButton)]
            self.assertNotIn("下載此項目", labels)
        finally:
            dialog.close()

    def test_external_link_offers_browser_button(self):
        from PySide6.QtWidgets import QPushButton

        from bbpull.gui_qt.preview import PreviewDialog

        dialog = PreviewDialog(self._node(kind="link", downloadable=False,
                                          payload={}), self.pal,
                               link_url="https://zoom.example/room")
        try:
            labels = [button.text() for button in dialog.findChildren(QPushButton)]
            self.assertIn("在瀏覽器開啟", labels)
        finally:
            dialog.close()

    # -- embedded attachments -----------------------------------------
    ATTACHMENT_BODY = (
        '<p>Dear students</p>'
        '<a href="https://twc.blackboard.com/bbcswebdav/pid-1-dt-content-'
        'rid-2_1/xid-2_1" data-bbfile=\'{"linkName":"notes.pdf"}\'>notes.pdf</a>')

    def test_embedded_attachment_has_a_preview_button(self):
        from PySide6.QtWidgets import QPushButton

        from bbpull.gui_qt.preview import PreviewDialog

        calls = {}

        def preview(url, name, item_id, done, fail):
            calls.update(url=url, name=name, item_id=item_id,
                         done=done, fail=fail)

        node = self._node(payload={"body": self.ATTACHMENT_BODY})
        dialog = PreviewDialog(node, self.pal,
                               on_preview_attachment=preview)
        try:
            buttons = [button for button in dialog.findChildren(QPushButton)
                       if button.text() == "預覽"]
            self.assertEqual(len(buttons), 1)
            buttons[0].click()
        finally:
            dialog.close()
        self.assertEqual(calls["name"], "notes.pdf")
        self.assertIn("bbcswebdav", calls["url"])
        self.assertEqual(calls["item_id"], "_101_1")

    def test_image_attachment_is_shown_inline(self):
        import tempfile

        from PySide6.QtGui import QPixmap

        from bbpull.gui_qt.preview import PreviewDialog

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.png")
            pixmap = QPixmap(8, 8)
            pixmap.fill()
            pixmap.save(path)
            dialog = PreviewDialog(self._node(), self.pal)
            try:
                dialog._attachment_ready(path, "photo.png")
                shown = dialog.image_label.pixmap()
                self.assertIsNotNone(shown)
                self.assertFalse(shown.isNull())
                self.assertIn("圖片預覽", dialog.status.text())
            finally:
                dialog.close()

    def test_non_image_attachment_opens_the_system_viewer(self):
        from unittest import mock

        from bbpull.gui_qt import preview as preview_module

        dialog = preview_module.PreviewDialog(self._node(), self.pal)
        try:
            with mock.patch.object(preview_module, "open_in_file_manager",
                                   return_value=True):
                dialog._attachment_ready("/tmp/x.pdf", "x.pdf")
            self.assertIn("已用系統程式開啟", dialog.status.text())
        finally:
            dialog.close()

    def test_file_item_offers_a_file_preview_button(self):
        from PySide6.QtWidgets import QPushButton

        from bbpull.gui_qt.preview import PreviewDialog

        seen = []
        dialog = PreviewDialog(self._node(kind="file", downloadable=True,
                                          payload={}), self.pal,
                               on_preview_file=lambda node, done, fail: seen.append(node["id"]))
        try:
            buttons = [button for button in dialog.findChildren(QPushButton)
                       if button.text() == "預覽檔案"]
            self.assertEqual(len(buttons), 1)
            buttons[0].click()
        finally:
            dialog.close()
        self.assertEqual(seen, ["_101_1"])


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class OutputDirTests(unittest.TestCase):
    """The download root must be choosable and must survive a restart."""

    def setUp(self):
        import tempfile

        from bbpull.config import load_config
        from bbpull.gui_qt import window as qt_window

        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(None)
        cfg.username = "tester"
        cfg.password = "secret"
        cfg.env_file = os.path.join(self.tmp.name, ".env")
        cfg.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        self.cfg = cfg
        self.win = qt_window.MainWindow(cfg, lambda *a: None)
        # `_bootstrap` is a singleShot timer; if it fires during another test's
        # event processing it would start a real login thread. This class only
        # exercises the output-directory setting.
        self.win.tasks.start = lambda *a, **k: None
        self.win.open_connect = lambda: None
        self.win.hide()

    def tearDown(self):
        try:
            self.win.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def test_apply_output_dir_updates_ui_and_env(self):
        target = os.path.join(self.tmp.name, "downloads")
        os.makedirs(target)
        self.assertTrue(self.win.apply_output_dir(target))
        self.assertEqual(self.win.out_root, os.path.abspath(target))
        self.assertEqual(self.cfg.out_dir, os.path.abspath(target))
        self.assertIn(os.path.abspath(target), self.win.out_label.text())
        with open(self.cfg.env_file, encoding="utf-8") as handle:
            self.assertIn("BB_OUT_DIR=", handle.read())

    def test_blank_path_is_ignored(self):
        before = self.win.out_root
        self.assertFalse(self.win.apply_output_dir(""))
        self.assertEqual(self.win.out_root, before)

    def test_preview_handler_is_wired(self):
        from bbpull.gui_qt import window as qt_window

        seen = {}

        class FakeDialog:
            def __init__(self, node, pal, **kwargs):
                seen["node"] = node
                seen["kwargs"] = kwargs

            def exec(self):
                seen["exec"] = True

        saved = qt_window.PreviewDialog
        qt_window.PreviewDialog = FakeDialog
        try:
            node = {"id": "d3", "title": "Loose doc", "kind": "document",
                    "downloadable": True, "payload": {}}
            self.win.open_preview(node)
        finally:
            qt_window.PreviewDialog = saved
        self.assertIs(seen.get("node"), node)
        self.assertTrue(seen.get("exec"))


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class PreviewFetchTests(unittest.TestCase):
    """Fetching an attachment must go through the authenticated session."""

    def setUp(self):
        import tempfile

        from bbpull.config import load_config
        from bbpull.gui_qt import window as qt_window

        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(None)
        cfg.username = "tester"
        cfg.password = "secret"
        cfg.course_id = "_12529_1"
        cfg.env_file = os.path.join(self.tmp.name, ".env")
        cfg.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        self.win = qt_window.MainWindow(cfg, lambda *a: None)
        self.win.hide()

        class InlineTasks:
            def start(inner, fn, on_ok, on_fail=None, *args, **kwargs):
                try:
                    result = fn()
                except Exception as exc:  # noqa: BLE001
                    if on_fail:
                        on_fail(str(exc))
                    return None
                if on_ok:
                    on_ok(result)
                return None

            def cancel_all(inner):
                pass

        self.win.tasks = InlineTasks()

    def tearDown(self):
        try:
            self.win.close()
        except Exception:  # noqa: BLE001
            pass
        import shutil
        import tempfile
        root = os.path.join(tempfile.gettempdir(), "bbpull-preview")
        for item_id in ("_test_preview_att", "_test_preview_file",
                        "_test_preview_fail"):
            shutil.rmtree(os.path.join(root, item_id), ignore_errors=True)
        self.tmp.cleanup()

    def _fake_session(self, fail=False):
        session = self

        class Fake:
            def download_preview(self, url, target):
                if fail:
                    raise RuntimeError("HTTP 403")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as handle:
                    handle.write(b"%PDF-1.4")
                return "saved", 8, target

            def api_get(self, path):
                return {"results": [{"id": "_att1",
                                     "fileName": "notes.pdf"}]}

            def url(self, path):
                return "https://twc.blackboard.com" + path

        return Fake()

    def test_attachment_is_downloaded_and_reported(self):
        import tempfile

        self.win.session = self._fake_session()
        done = {}
        failures = []
        self.win.preview_attachment(
            "https://twc.blackboard.com/bbcswebdav/xid-1_1", "notes.pdf",
            "_test_preview_att", lambda path, name: done.update(path=path, name=name),
            failures.append)
        self.assertEqual(done["name"], "notes.pdf")
        self.assertTrue(os.path.isfile(done["path"]))
        self.assertTrue(done["path"].startswith(tempfile.gettempdir()))
        self.assertEqual(failures, [])

    def test_file_item_resolves_its_attachment_then_downloads(self):
        self.win.session = self._fake_session()
        seen = {}
        self.win.preview_file_node(
            {"id": "_test_preview_file", "title": "Slides"},
            lambda path, name: seen.update(path=path, name=name),
            lambda message: seen.update(error=message))
        self.assertEqual(seen.get("name"), "notes.pdf")
        self.assertTrue(os.path.isfile(seen["path"]))
        self.assertNotIn("error", seen)

    def test_failure_reaches_the_dialog(self):
        self.win.session = self._fake_session(fail=True)
        errors = []
        self.win.preview_attachment(
            "https://twc.blackboard.com/bbcswebdav/xid-1_1", "notes.pdf",
            "_test_preview_fail", lambda path, name: None, errors.append)
        self.assertTrue(errors)
        self.assertIn("403", errors[0])

    def test_attachment_row_buttons_route_outside_the_dialog(self):
        calls = []
        self.win.preview_attachment_row = lambda node: calls.append(
            ("preview", node["id"]))
        self.win.download_attachment = lambda node: calls.append(
            ("download", node["id"]))
        node = {"id": "__attachment__:doc:0", "kind": "attachment"}
        self.win._on_row_button("preview", node)
        self.win._on_row_button("download", node)
        self.assertEqual(calls, [("preview", "__attachment__:doc:0"),
                                 ("download", "__attachment__:doc:0")])

    def test_opening_a_preview_on_an_attachment_row_opens_the_file(self):
        seen = []
        self.win.preview_attachment_row = lambda node: seen.append(node["id"])
        self.win.open_preview({"id": "__attachment__:doc:0",
                               "kind": "attachment", "title": "notes.pdf"})
        self.assertEqual(seen, ["__attachment__:doc:0"])

    def test_download_attachment_lands_in_the_course_tree(self):
        from bbpull.catalog import Catalog
        from bbpull.preview import attachment_row
        from bbpull.selective import course_dir_for

        tree = [{
            "id": "doc", "title": "Manual", "kind": "document",
            "attachmentCount": 1, "hasBody": True, "downloadable": True,
            "payload": {}, "children": [],
        }]
        self.win.catalog = Catalog("_1_1", "Course", "http://x", tree)
        self.win.out_root = os.path.join(self.tmp.name, "out")
        self.win.session = self._fake_session()
        node = attachment_row(self.win.catalog.find("doc"),
                              {"url": "https://twc.blackboard.com/xid-1_1",
                               "name": "notes.pdf"})
        self.win.download_attachment(node)
        expected = os.path.join(course_dir_for(self.win.out_root,
                                               self.win.catalog), "notes.pdf")
        self.assertTrue(os.path.isfile(expected))
        self.assertEqual(os.path.getsize(expected), 8)


@unittest.skipUnless(HAS_QT and QT_ENABLED, "Qt tests are opt-in (see module docstring)")
class DelegatePaintTests(unittest.TestCase):
    """Paint every row kind for real.

    A shipped bug motivated this: `theme.draw_icon` passed a `QColor` into
    `blend()`, which expected a hex string, so the `document` branch raised
    `AttributeError: 'QColor' object has no attribute 'lstrip'`. Because Qt
    swallows exceptions raised inside `QStyledItemDelegate.paint`, the app kept
    running and simply drew nothing - and since the icon is painted before the
    title, meta text and buttons, an entire document row came out blank.

    Nothing caught it: no test called `paint()`, and the course used for manual
    screenshots happened to have no plain documents at its root.

    These call `paint()` **directly**, so an exception propagates into the test
    instead of disappearing into Qt's event loop.
    """

    KINDS = ("folder", "document", "file", "link", "assessment", "tool",
             "announcement", "other")

    def _node(self, kind):
        return {
            "id": f"n-{kind}", "title": f"項目 {kind}", "kind": kind,
            "handler": "resource/x-bb-document", "itemCount": 2, "childCount": 0,
            "downloadable": True, "hasBody": True, "synthetic": False,
            "children": [],
        }

    def setUp(self):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QPainter, QPixmap

        self.pal = theme.Palette("dark")
        self.rect = QRect(0, 0, 800, delegates.ROW_HEIGHT)
        self.pixmap = QPixmap(800, delegates.ROW_HEIGHT)
        self.pixmap.fill(Qt.black)
        self.painter = QPainter(self.pixmap)

    def tearDown(self):
        self.painter.end()

    def _paint(self, delegate, node, selected=False, implied=False):
        """Invoke the delegate's paint directly so errors surface."""
        from PySide6.QtGui import QStandardItem, QStandardItemModel
        from PySide6.QtWidgets import QStyleOptionViewItem

        model = QStandardItemModel()
        model.appendRow(QStandardItem("row"))
        index = model.index(0, 0)
        model.setData(index, node, models.NodeRole)
        model.setData(index, selected, models.SelectedRole)
        model.setData(index, implied, models.ImpliedRole)
        model.setData(index, False, models.HoverRole)

        option = QStyleOptionViewItem()
        option.rect = self.rect
        delegate.paint(self.painter, option, index)

    def test_every_content_kind_paints(self):
        delegate = delegates.RowDelegate(self.pal, None)
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self._paint(delegate, self._node(kind))

    def test_every_kind_paints_when_selected_and_hovered(self):
        delegate = delegates.RowDelegate(self.pal, None)
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.pixmap.fill(Qt.black)
                self._paint(delegate, self._node(kind), selected=True)

    def test_painting_actually_draws_something(self):
        """A swallowed exception leaves the pixmap untouched, so assert on pixels."""
        delegate = delegates.RowDelegate(self.pal, None)
        self.pixmap.fill(Qt.black)
        self._paint(delegate, self._node("document"))
        image = self.pixmap.toImage()
        drawn = 0
        for y in range(0, image.height(), 2):
            for x in range(0, image.width(), 4):
                colour = image.pixelColor(x, y)
                if colour.red() + colour.green() + colour.blue() > 90:
                    drawn += 1
        self.assertGreater(drawn, 40,
                           "a document row must draw its icon, title and buttons")

    def test_course_card_paints_for_every_meta_shape(self):
        """Matches the name shapes in the live account, including 403 names."""
        from bbpull.gui.courses import parse_courses
        from PySide6.QtGui import QStandardItem, QStandardItemModel
        from PySide6.QtWidgets import QStyleOptionViewItem

        metas = parse_courses([
            {"courseId": "_1_1", "name": "[2026/27-1] NUR2051/NUR2046 Nursing "
                                         "Practicum I (NY2023)"},
            {"courseId": "_2_1", "name": "Library"},
            {"courseId": "_3_1", "name": "[2023/24] Year only"},
            {"courseId": "_4_1", "name": ""},
        ])
        delegate = delegates.CourseDelegate(self.pal, None, on_click=lambda _c: None)
        for meta in metas:
            with self.subTest(course=meta.course_id):
                model = QStandardItemModel()
                model.appendRow(QStandardItem("card"))
                index = model.index(0, 0)
                model.setData(index, meta, models.NodeRole)
                model.setData(index, True, models.SelectedRole)
                option = QStyleOptionViewItem()
                option.rect = self.rect
                delegate.paint(self.painter, option, index)

    def test_blend_accepts_a_qcolor(self):
        """The exact type error that caused the blank rows."""
        from PySide6.QtGui import QColor

        from bbpull.palette import blend

        self.assertEqual(blend("#ffffff", "#000000", 0.5), "#808080")
        self.assertEqual(blend(QColor("#ffffff"), QColor("#000000"), 0.5), "#808080")
        self.assertEqual(blend("#ffffff", QColor("#000000"), 0.5), "#808080")

    def test_blend_is_tolerant_of_junk(self):
        from bbpull.palette import blend

        self.assertTrue(blend(None, None, 0.5).startswith("#"))
        self.assertTrue(blend("nonsense", "#123456", 0.5).startswith("#"))


if __name__ == "__main__":
    unittest.main()
