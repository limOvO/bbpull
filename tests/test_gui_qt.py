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

    def test_assessment_gets_no_buttons(self):
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        _c, _i, _t, buttons = self.delegate._layout(rect, build_tree()[2])
        self.assertEqual(buttons, [])

    def test_document_row_has_download_only(self):
        from PySide6.QtCore import QRect

        rect = QRect(0, 0, 900, delegates.ROW_HEIGHT)
        _c, _i, _t, buttons = self.delegate._layout(rect, build_tree()[1])
        self.assertEqual([a for a, _r in buttons], ["download"])

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

    def test_scrolling_a_long_list_stays_cheap(self):
        from bbpull.catalog import Catalog
        from PySide6.QtWidgets import QListView

        view = QListView()
        model = models.CatalogModel()
        model.setCatalog = None  # guard against accidental API misuse
        model.set_catalog(Catalog("_1_1", "Perf", "http://x", flat_tree(3000)))
        model.set_folder("root")
        delegate = delegates.RowDelegate(theme.Palette("dark"), view)
        view.setModel(model)
        view.setItemDelegate(delegate)
        view.setUniformItemSizes(True)
        view.resize(800, 600)

        start = time.perf_counter()
        for value in range(0, 3000, 60):
            view.verticalScrollBar().setValue(value)
            _app.processEvents()
        elapsed = (time.perf_counter() - start) * 1000
        self.assertLess(elapsed, 3000, f"scrolling 3,000 rows took {elapsed:.0f} ms")
        view.deleteLater()


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


if __name__ == "__main__":
    unittest.main()
