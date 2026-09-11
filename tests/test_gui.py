"""Offline tests for the desktop GUI.

These run without a visible desktop: a Tk root is created and withdrawn, widgets
are built, and their **canvas item state** is asserted. That is what catches the
class of bug that actually bit this code - a checkbox that looked right in code
but painted nothing, because one draw call cleared another's work.

Run them directly:
    python -m unittest tests.test_gui

They are excluded from `unittest discover` (and therefore from
`bbpull selftest`) by default: they build real Tk windows, and doing that inside
the discovery process produces noisy Tcl teardown messages from Tk and
customtkinter internals. Opt in with:
    BBPULL_RUN_GUI_TESTS=1 python -m unittest discover -s tests -t .

Skipped automatically when Tk cannot be created (headless CI).
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import customtkinter as ctk
    import tkinter as tk

    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    HAS_TK = True
except Exception:  # noqa: BLE001 - no display available
    HAS_TK = False


#: GUI tests build real Tk windows. Inside a `unittest discover` run (which is
#: also what `bbpull selftest` uses) that produces noisy Tcl teardown messages
#: from Tk/customtkinter internals, so they are opt-in:
#:     python -m unittest tests.test_gui            (direct, always works)
#:     BBPULL_RUN_GUI_TESTS=1 python -m unittest discover -s tests -t .
GUI_ENABLED = (
    os.environ.get("BBPULL_RUN_GUI_TESTS") == "1"
    or __name__ == "tests.test_gui"
)


@unittest.skipUnless(HAS_TK and GUI_ENABLED, "GUI tests are opt-in (see module docstring)")
class ThemeTests(unittest.TestCase):
    def setUp(self):
        from bbpull.gui.theme import Palette

        self.dark = Palette("dark")
        self.light = Palette("light")

    def test_known_kinds_have_colours(self):
        for mode, pal in (("dark", self.dark), ("light", self.light)):
            for kind in ("folder", "document", "file", "link", "assessment",
                         "tool", "announcement", "other"):
                colour = pal.kind(kind)
                self.assertTrue(colour.startswith("#"), f"{mode}/{kind}")
                self.assertEqual(len(colour), 7, f"{mode}/{kind}")

    def test_unknown_kind_falls_back(self):
        self.assertEqual(self.dark.kind("mystery"), self.dark.kind("other"))

    def test_blend_is_precomputed_not_alpha(self):
        """Tk has no alpha, so translucent accents must be solid hex colours."""
        for pal in (self.dark, self.light):
            self.assertTrue(pal.muted_accent.startswith("#"))
            self.assertNotIn("rgba", pal.muted_accent)

    def test_light_and_dark_differ(self):
        self.assertNotEqual(self.dark.bg, self.light.bg)
        self.assertNotEqual(self.dark.accent, self.light.accent)


@unittest.skipUnless(HAS_TK and GUI_ENABLED, "GUI tests are opt-in (see module docstring)")
class CheckMarkPaintTests(unittest.TestCase):
    """Regression tests for the invisible-checkbox bug.

    `IconPainter.check()` used to call `delete("all")`, erasing the accent
    square drawn moments earlier. The result was a white tick on the row
    background - no visible checkbox - which a screenshot caught.
    """

    def setUp(self):
        import customtkinter as ctk

        from bbpull.gui.theme import Palette
        from bbpull.gui.widgets import CheckMark

        self.ctk = ctk
        self.pal = Palette("dark")
        self.root = ctk.CTk()
        self.root.withdraw()
        self.CheckMark = CheckMark

    def tearDown(self):
        self.root.destroy()

    def _mark(self, state):
        box = self.CheckMark(self.root, self.pal, size=20)
        box.set_state(state)
        return box

    def _fills(self, canvas):
        out = []
        for item in canvas.find_all():
            try:
                out.append(canvas.itemcget(item, "fill"))
            except tk.TclError:
                pass
        return out

    def test_off_state_has_a_visible_border(self):
        canvas = self._mark("off")
        polygons = [i for i in canvas.find_all() if canvas.type(i) == "polygon"]
        self.assertTrue(polygons, "off state must draw its box")

    def test_on_state_paints_the_accent_fill(self):
        canvas = self._mark("on")
        fills = self._fills(canvas)
        self.assertIn(self.pal.accent, fills,
                      "checked box must keep its accent fill, not just the tick")

    def test_on_state_draws_both_box_and_tick(self):
        canvas = self._mark("on")
        kinds = sorted(canvas.type(i) for i in canvas.find_all())
        self.assertIn("polygon", kinds, "accent square missing")
        self.assertIn("line", kinds, "tick missing")

    def test_tick_is_drawn_after_the_box(self):
        """Order matters: the tick must be on top of the fill."""
        canvas = self._mark("on")
        polygons = [i for i in canvas.find_all() if canvas.type(i) == "polygon"]
        lines = [i for i in canvas.find_all() if canvas.type(i) == "line"]
        self.assertTrue(polygons and lines)
        self.assertLess(polygons[0], lines[-1])

    def test_tick_is_white_on_accent(self):
        canvas = self._mark("on")
        line_fills = [canvas.itemcget(i, "fill")
                      for i in canvas.find_all() if canvas.type(i) == "line"]
        self.assertIn("#ffffff", line_fills)

    def test_implied_state_shows_a_dash_and_hides_the_tick(self):
        """The new widget keeps all items and toggles their state, so assert on
        item state rather than on which items exist."""
        canvas = self._mark("implied")
        fills = self._fills(canvas)
        self.assertIn(self.pal.muted_accent, fills)
        lines = [i for i in canvas.find_all() if canvas.type(i) == "line"]
        self.assertEqual(len(lines), 2, "one tick + one dash")
        states = [canvas.itemcget(i, "state") for i in lines]
        self.assertIn("normal", states)
        # The dash is the 4-coordinate line and must be the visible one.
        dash = [i for i in lines if len(canvas.coords(i)) == 4][0]
        self.assertEqual(canvas.itemcget(dash, "state"), "normal")
        tick = [i for i in lines if len(canvas.coords(i)) == 6][0]
        self.assertEqual(canvas.itemcget(tick, "state"), "hidden")

    def test_on_state_hides_the_dash(self):
        canvas = self._mark("on")
        lines = [i for i in canvas.find_all() if canvas.type(i) == "line"]
        dash = [i for i in lines if len(canvas.coords(i)) == 4][0]
        self.assertEqual(canvas.itemcget(dash, "state"), "hidden")

    def test_off_state_hides_both_glyphs(self):
        canvas = self._mark("off")
        lines = [i for i in canvas.find_all() if canvas.type(i) == "line"]
        for item in lines:
            self.assertEqual(canvas.itemcget(item, "state"), "hidden")

    def test_widget_does_not_recreate_items(self):
        """Items are built once and restyled: recreating them was both slow and
        the cause of the invisible-checkbox bug."""
        canvas = self._mark("off")
        before = tuple(canvas.find_all())
        for state in ("on", "implied", "off", "on"):
            canvas.set_state(state)
        self.assertEqual(tuple(canvas.find_all()), before)

    def test_all_states_differ_visually(self):
        off = self._fills(self._mark("off"))
        on = self._fills(self._mark("on"))
        implied = self._fills(self._mark("implied"))
        self.assertNotEqual(off, implied)
        self.assertNotEqual(on, off)

    def test_set_state_repaints(self):
        canvas = self._mark("off")
        before = self._fills(canvas)
        canvas.set_state("on")
        self.assertNotEqual(before, self._fills(canvas))


@unittest.skipUnless(HAS_TK and GUI_ENABLED, "GUI tests are opt-in (see module docstring)")
class IconPainterTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk

        from bbpull.gui.theme import IconPainter

        self.tk = tk
        self.painter = IconPainter
        self.root = tk.Tk()
        self.root.withdraw()
        self.canvas = tk.Canvas(self.root, width=24, height=24)

    def tearDown(self):
        self.root.destroy()

    def test_every_kind_draws_something(self):
        for kind in ("folder", "document", "file", "link", "assessment",
                     "tool", "announcement", "other"):
            self.painter.draw(self.canvas, kind, "#abcdef", size=22)
            self.assertTrue(self.canvas.find_all(), f"{kind} drew nothing")

    def test_clear_true_wipes_previous_content(self):
        self.painter.draw(self.canvas, "folder", "#111111", size=22)
        first = len(self.canvas.find_all())
        self.painter.draw(self.canvas, "file", "#222222", size=22)
        self.assertLessEqual(len(self.canvas.find_all()), first + 4)

    def test_clear_false_preserves_previous_content(self):
        """The checkbox depends on this: layer the tick over the box."""
        self.canvas.create_rectangle(0, 0, 20, 20, fill="#6d6cf6", outline="")
        before = len(self.canvas.find_all())
        self.painter.check(self.canvas, "#ffffff", size=14, clear=False)
        self.assertGreater(len(self.canvas.find_all()), before)

    def test_chevrons_and_glyphs_draw(self):
        for direction in ("left", "right", "up", "down"):
            self.painter.chevron(self.canvas, direction, "#ffffff", size=16)
            self.assertTrue(self.canvas.find_all(), direction)
        for fn in (self.painter.check, self.painter.close, self.painter.refresh,
                   self.painter.download, self.painter.sun, self.painter.moon):
            fn(self.canvas, "#ffffff", size=16)
            self.assertTrue(self.canvas.find_all(), fn.__name__)


@unittest.skipUnless(HAS_TK and GUI_ENABLED, "GUI tests are opt-in (see module docstring)")
class AppConstructionTests(unittest.TestCase):
    """The window must build and its core rendering must not raise."""

    def setUp(self):
        from bbpull.config import load_config

        self.cfg = load_config(None)
        self.cfg.username = ""
        self.cfg.password = ""

    def test_window_builds_without_network(self):
        import customtkinter as ctk

        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda *a: None)
        app.withdraw()
        try:
            # No credentials => the connect dialog opens and no login thread runs.
            self.assertIsNotNone(app.chip)
            self.assertIsNotNone(app.course_list)
            app.render_all()          # must not raise on an empty catalog
            app.render_tray()
            app.render_chip()
            self.assertFalse(app.catalog)
        finally:
            app._shutdown()

    def test_empty_catalog_renders_placeholder(self):
        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda *a: None)
        app.withdraw()
        try:
            app.render_content()
            self.assertIsNotNone(app._placeholder, "the empty page must be built")
            # It must live OUTSIDE the scrollable area: `place()` contributes
            # nothing to a parent's requested size, so a placeholder inside the
            # scroll frame collapsed to 1px and rendered off-screen.
            host = app._placeholder_host
            self.assertIsNotNone(host)
            self.assertEqual(str(host.master), str(app.list_holder))
            self.assertFalse(app.scroll.winfo_ismapped(),
                             "the scroll area must be hidden while the hint page shows")
        finally:
            app._shutdown()

    def test_placeholder_is_centred_inside_a_real_area(self):
        """Regression: `place()` gave the host a requested height of 1, which put
        the hint block off-screen. Centring must produce real geometry."""
        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda _m: None)
        app.geometry("1000x700")
        app.withdraw()
        try:
            app.update_idletasks()
            app.render_placeholder()
            app.update_idletasks()
            host = app._placeholder_host
            self.assertGreater(host.winfo_reqheight(), 100,
                               "the hint page must request a usable height")
            self.assertGreater(app._placeholder.center.winfo_reqheight(), 60)
            self.assertGreaterEqual(app._placeholder.center.winfo_y(), 0,
                                    "the hint block must not sit above the top edge")
        finally:
            app._shutdown()

    def test_real_content_restores_the_scroll_area(self):
        from bbpull.catalog import Catalog
        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda _m: None)
        app.withdraw()
        try:
            app.render_placeholder()
            self.assertFalse(app.scroll.winfo_ismapped())
            app.catalog = Catalog("_1_1", "T", "http://x", build_flat_tree(3))
            app.by_id = {n["id"]: n for n in app.catalog.tree}
            app.parent_of = {n["id"]: None for n in app.catalog.tree}
            app.render_content()
            app.update_idletasks()
            self.assertTrue(app.scroll.winfo_ismapped(),
                            "the list must come back after a placeholder")
            self.assertEqual(len(app._rows), 3)
        finally:
            app._shutdown()

    def test_startup_page_is_the_blank_hint_page(self):
        """The user asked for a hint page on the right when the app opens."""
        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda *a: None)
        app.withdraw()
        try:
            app.render_placeholder()
            self.assertIsNotNone(app._placeholder)
            # The title label is the second child of the centred block.
            texts = []

            def collect(widget):
                for child in widget.winfo_children():
                    try:
                        texts.append(child.cget("text"))
                    except Exception:  # noqa: BLE001
                        pass
                    collect(child)

            collect(app._placeholder.frame)
            joined = " ".join(str(t) for t in texts)
            self.assertTrue(any(t for t in texts), "placeholder must show text")
            self.assertIn("尚未", joined)
            # And the hint must be actionable, not just a heading.
            self.assertGreaterEqual(len(texts), 3)
        finally:
            app._shutdown()

    def test_selection_logic_needs_no_network(self):
        import customtkinter as ctk

        from bbpull.catalog import Catalog
        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda *a: None)
        app.withdraw()
        try:
            tree = [
                {"id": "f1", "title": "Folder", "kind": "folder", "handler": "resource/x-bb-folder",
                 "itemCount": 2, "childCount": 2, "downloadable": False, "children": [
                     {"id": "d1", "title": "Doc", "kind": "document",
                      "handler": "resource/x-bb-document", "itemCount": 1,
                      "childCount": 0, "downloadable": True, "children": []},
                     {"id": "d2", "title": "File", "kind": "file",
                      "handler": "resource/x-bb-file", "itemCount": 1,
                      "childCount": 0, "downloadable": True, "children": []},
                 ]},
                {"id": "d3", "title": "Loose", "kind": "document",
                 "handler": "resource/x-bb-document", "itemCount": 1,
                 "childCount": 0, "downloadable": True, "children": []},
            ]
            app.catalog = Catalog("_1_1", "Test", "http://x", tree)
            from bbpull.catalog import walk
            app.by_id = {n["id"]: n for n in walk(app.catalog.tree)}
            app.parent_of = {"f1": None, "d1": "f1", "d2": "f1", "d3": None}

            # Folder tick covers children implicitly.
            app.toggle_select("f1")
            self.assertTrue(app._is_implied("d1"))
            self.assertEqual(app.topmost_selection(), ["f1"])
            self.assertEqual(app.selection_summary()["items"], 2)

            # Unticking one child releases the folder and re-selects the rest.
            app.toggle_select("d1")
            self.assertNotIn("f1", app.selection)
            self.assertIn("d2", app.selection)
            self.assertNotIn("d1", app.selection)

            # Cross-branch selection keeps both.
            app.clear_selection()
            app.toggle_select("d1")
            app.toggle_select("d3")
            self.assertEqual(sorted(app.topmost_selection()), ["d1", "d3"])
        finally:
            app._shutdown()

    def test_theme_toggle_rebuilds_without_losing_selection(self):
        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda *a: None)
        app.withdraw()
        try:
            app.selection = {"x", "y"}
            app.toggle_theme()
            self.assertEqual(app.theme_mode, "light")
            self.assertEqual(app.selection, {"x", "y"})
            app.toggle_theme()
            self.assertEqual(app.theme_mode, "dark")
            self.assertEqual(app.selection, {"x", "y"})
        finally:
            app._shutdown()

    def test_theme_toggle_keeps_the_widget_tree_alive(self):
        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda *a: None)
        app.withdraw()
        try:
            for _ in range(2):
                app.toggle_theme()
                app.update_idletasks()
            self.assertIsNotNone(app.chip)
            self.assertIsNotNone(app.course_list)
            self.assertIsNotNone(app.list_frame)
        finally:
            app._shutdown()


def build_flat_tree(count):
    """A catalog tree with `count` sibling documents under the root."""
    return [
        {
            "id": f"n{i}",
            "title": f"項目 {i} 課程教材與文件範例",
            "kind": "document",
            "handler": "resource/x-bb-document",
            "itemCount": 1,
            "childCount": 0,
            "downloadable": True,
            "hasBody": True,
            "children": [],
        }
        for i in range(count)
    ]


@unittest.skipUnless(HAS_TK and GUI_ENABLED, "GUI tests are opt-in (see module docstring)")
class RenderingPerformanceTests(unittest.TestCase):
    """Regression guards for the stutter the user reported.

    Measured on the old implementation: building 120 rows took ~1900 ms, and 25
    selection toggles with 120 rows on screen took ~9562 ms because every toggle
    destroyed and rebuilt the whole list. These tests assert the *shape* of the
    fix (incremental updates, bounded widget churn) rather than absolute
    timings, so they stay meaningful on a slower or busier machine.
    """

    def setUp(self):
        from bbpull.catalog import walk
        from bbpull.config import load_config
        from bbpull.gui.app import BbpullApp

        cfg = load_config(None)
        cfg.username = ""
        cfg.password = ""
        self.app = BbpullApp(cfg, lambda *a: None)
        self.app.withdraw()
        tree = build_flat_tree(60)
        from bbpull.catalog import Catalog

        self.app.catalog = Catalog("_1_1", "Perf", "http://x", tree)
        self.app.by_id = {n["id"]: n for n in walk(self.app.catalog.tree)}
        self.app.parent_of = {f"n{i}": None for i in range(60)}
        self.app.render_content()

    def tearDown(self):
        self.app._shutdown()

    def test_rows_are_built_once(self):
        self.assertEqual(len(self.app._rows), 60)

    def test_toggle_does_not_rebuild_rows(self):
        """The core fix: row widget identity must survive a selection change."""
        original = dict(self.app._rows)
        for node_id in list(original)[:10]:
            self.app.toggle_select(node_id)
        self.assertIs(original["n0"], self.app._rows["n0"])
        self.assertIs(original["n59"], self.app._rows["n59"])
        self.assertEqual(len(self.app._rows), 60)

    def test_toggle_only_restyles_changed_rows(self):
        """Ticking one row must change exactly one row's visual state."""
        before = {nid: (r.selected, r.implied) for nid, r in self.app._rows.items()}
        self.app.toggle_select("n5")
        after = {nid: (r.selected, r.implied) for nid, r in self.app._rows.items()}
        diff = [nid for nid in before if before[nid] != after[nid]]
        self.assertEqual(diff, ["n5"])

    def test_refresh_row_states_reports_only_real_changes(self):
        self.app.selection = {"n1", "n2"}
        changed = self.app.refresh_row_states()
        self.assertEqual(changed, 2)
        # Calling again with no state change must touch nothing.
        self.assertEqual(self.app.refresh_row_states(), 0)

    def test_checkbox_shows_the_tick_for_a_selected_row(self):
        """Regression: the row was marked selected while its checkbox still drew
        the empty 'off' glyph, so ticking an item showed no tick at all (found by
        sampling the rendered screenshot's pixels)."""
        self.app.toggle_select("n5")
        row = self.app._rows["n5"]
        self.assertTrue(row.selected)
        self.assertEqual(row.check.state, "on")
        fills = [row.check.itemcget(i, "fill") for i in row.check.find_all()]
        self.assertIn(self.app.pal.accent, fills,
                      "a selected row's checkbox must paint the accent fill")

    def test_checkbox_clears_when_deselected(self):
        self.app.toggle_select("n7")
        self.assertEqual(self.app._rows["n7"].check.state, "on")
        self.app.toggle_select("n7")
        self.assertFalse(self.app._rows["n7"].selected)
        self.assertEqual(self.app._rows["n7"].check.state, "off")

    def test_rows_rendered_as_already_selected_show_ticks(self):
        """Rebuilding the list must not lose the visual selection state."""
        self.app.selection = {"n3", "n4"}
        self.app.render_content()
        for node_id in ("n3", "n4"):
            self.assertEqual(self.app._rows[node_id].check.state, "on", node_id)
        self.assertEqual(self.app._rows["n5"].check.state, "off")

    def test_implied_children_show_the_dash(self):
        """A folder tick must mark its children as implied, with a dash."""
        from bbpull.catalog import Catalog, walk

        tree = [{
            "id": "folder", "title": "F", "kind": "folder",
            "handler": "resource/x-bb-folder", "itemCount": 1, "childCount": 1,
            "downloadable": False, "children": [{
                "id": "inside", "title": "Inner", "kind": "document",
                "handler": "resource/x-bb-document", "itemCount": 1,
                "childCount": 0, "downloadable": True, "children": [],
            }],
        }]
        self.app.catalog = Catalog("_1_1", "T", "http://x", tree)
        self.app.by_id = {n["id"]: n for n in walk(self.app.catalog.tree)}
        self.app.parent_of = {"folder": None, "inside": "folder"}
        self.app.selection = set()
        self.app.render_content()

        self.app.toggle_select("folder")
        self.assertEqual(self.app._rows["folder"].check.state, "on")
        # The child is only visible after navigating in, so check the model.
        self.assertTrue(self.app._is_implied("inside"))

    def test_twenty_five_toggles_stay_fast(self):
        """Old code took ~9.5 s for this; the budget here is deliberately loose
        (2 s) so it only fails on a real regression, not on machine noise."""
        import time

        start = time.perf_counter()
        for i in range(25):
            self.app.toggle_select(f"n{i % 60}")
            self.app.update_idletasks()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0,
                        f"25 selection toggles took {elapsed:.2f}s; the list is "
                        f"rebuilding instead of restyling")

    def test_building_rows_is_not_absurdly_slow(self):
        import time

        self.app._clear_content()
        start = time.perf_counter()
        self.app.render_content()
        self.app.update_idletasks()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 3.0, f"rendering 60 rows took {elapsed:.2f}s")


@unittest.skipUnless(HAS_TK and GUI_ENABLED, "GUI tests are opt-in (see module docstring)")
class CourseFilterUiTests(unittest.TestCase):
    """The sidebar year/term filters, driven without any network."""

    REAL = [
        {"courseId": "_1_1", "name": "[2026/27-1] Alpha Course", "role": "Student"},
        {"courseId": "_2_1", "name": "[2025/26-3] Beta Course", "role": "Student"},
        {"courseId": "_3_1", "name": "[2025/26-1] Gamma Course", "role": "Student"},
        {"courseId": "_4_1", "name": "Library", "role": "Student"},
    ]

    def setUp(self):
        from bbpull.config import load_config
        from bbpull.gui.app import BbpullApp
        from bbpull.gui.courses import parse_courses

        cfg = load_config(None)
        cfg.username = ""
        cfg.password = ""
        self.app = BbpullApp(cfg, lambda *a: None)
        self.app.withdraw()
        self.app.courses = list(self.REAL)
        self.app.metas = parse_courses(self.REAL)
        self.app._sync_filter_options()
        self.app.render_courses()

    def tearDown(self):
        self.app._shutdown()

    def _footer(self):
        return self.app.course_footer.cget("text")

    def test_all_courses_listed_by_default(self):
        self.assertEqual(len(self.app.filtered_metas()), 4)
        self.assertIn("4 門課程", self._footer())

    def test_year_menu_offers_the_real_years(self):
        values = self.app.year_menu.cget("values")
        self.assertIn("全部學年", values)
        self.assertIn("2026/27", values)
        self.assertIn("2025/26", values)
        self.assertIn("未標示學年", values)

    def test_filtering_by_year_updates_the_list_and_footer(self):
        self.app._on_year("2025/26")
        self.assertEqual(len(self.app.filtered_metas()), 2)
        self.assertIn("2 / 4", self._footer())

    def test_term_options_depend_on_the_selected_year(self):
        self.app._on_year("2025/26")
        values = self.app.term_menu.cget("values")
        self.assertIn("第 1 學期", values)
        self.assertIn("第 3 學期", values)

    def test_filtering_by_term_narrows_further(self):
        self.app._on_year("2025/26")
        self.app._on_term("第 3 學期")
        got = self.app.filtered_metas()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].course_id, "_2_1")

    def test_changing_year_resets_an_impossible_term(self):
        self.app._on_year("2025/26")
        self.app._on_term("第 3 學期")
        self.assertEqual(len(self.app.filtered_metas()), 1)
        # Switch to a year without term 3: the term must not silently hide all.
        self.app._on_year("2026/27")
        self.assertEqual(len(self.app.filtered_metas()), 1)

    def test_unspecified_year_filter(self):
        self.app._on_year("未標示學年")
        got = self.app.filtered_metas()
        self.assertEqual([m.course_id for m in got], ["_4_1"])

    def test_text_filter_is_applied_after_debounce(self):
        self.app.course_search.insert(0, "Beta")
        self.app._apply_course_filter()
        self.assertEqual(len(self.app.filtered_metas()), 1)
        self.assertEqual(self.app.filtered_metas()[0].course_id, "_2_1")

    def test_typing_debounces_instead_of_rebuilding_every_keystroke(self):
        self.app.course_search.insert(0, "Beta")
        self.app._on_course_filter()
        # A timer is pending; no immediate rebuild happened.
        self.assertIsNotNone(self.app._course_token)
        self.app._apply_course_filter()
        self.assertEqual(len(self.app.filtered_metas()), 1)

    def test_cards_are_rendered_for_each_visible_course(self):
        self.assertEqual(len(self.app._cards), 4)
        self.app._on_year("2025/26")
        self.assertEqual(len(self.app._cards), 2)

    def test_empty_result_shows_a_message(self):
        self.app.course_search.insert(0, "zzzz")
        self.app._apply_course_filter()
        self.assertEqual(self.app.filtered_metas(), [])
        self.assertIn("0 / 4", self._footer())


class HumanBytesTests(unittest.TestCase):
    def test_formatting(self):
        from bbpull.gui.app import human_bytes

        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(512), "512 B")
        self.assertIn("KB", human_bytes(2048))
        self.assertIn("MB", human_bytes(5 * 1024 * 1024))
        self.assertIn("GB", human_bytes(3 * 1024 ** 3))

    def test_none_is_safe(self):
        from bbpull.gui.app import human_bytes

        self.assertEqual(human_bytes(None), "0 B")


@unittest.skipUnless(HAS_TK and GUI_ENABLED, "GUI tests are opt-in (see module docstring)")
class ReportedBugRegressionTests(unittest.TestCase):
    """Guards for the five problems reported after the first GUI build."""

    def setUp(self):
        from bbpull.config import load_config
        from bbpull.gui.app import BbpullApp

        cfg = load_config(None)
        cfg.username = "someone"
        cfg.password = "secret"
        self.cfg = cfg
        self.app = BbpullApp(cfg, lambda *a: None)
        # Cancel the deferred startup so it cannot re-render mid-test, and map
        # the window off-screen (withdrawn windows receive no generated events).
        if getattr(self.app, "_bootstrap_id", None):
            try:
                self.app.after_cancel(self.app._bootstrap_id)
            except Exception:  # noqa: BLE001
                pass
            self.app._bootstrap_id = None
        self.app.geometry(f"{self.app.px(1100)}x{self.app.px(700)}+-3000+-3000")
        self.app.deiconify()

    def tearDown(self):
        self.app._shutdown()

    # -- 1. icon buttons must be clickable over their whole area -----------
    def test_icon_button_is_clickable_on_the_glyph(self):
        """The glyph is a child canvas on top of the button. With bindings only
        on the parent, clicking the visible icon did nothing - which is why
        theme/settings appeared unresponsive."""
        for name in ("btn_theme", "btn_connect", "btn_jobs", "btn_output"):
            button = getattr(self.app, name)
            self.assertTrue(button.bind("<Button-1>"), f"{name} parent unbound")
            self.assertTrue(button._icon.bind("<Button-1>"),
                            f"{name}: the glyph canvas has no click binding")

    def test_icon_button_command_fires(self):
        fired = []
        self.app.btn_theme.command = lambda: fired.append(1)
        self.app.update()
        self.app.btn_theme._icon.event_generate("<Button-1>", x=2, y=2)
        self.app.update()
        self.assertEqual(fired, [1], "clicking the glyph must run the command")

    def test_icon_button_command_fires_from_the_parent_too(self):
        fired = []
        self.app.btn_theme.command = lambda: fired.append(1)
        self.app.update()
        self.app.btn_theme.event_generate("<Button-1>", x=1, y=1)
        self.app.update()
        self.assertEqual(fired, [1])

    def test_icon_button_glyph_covers_a_meaningful_share(self):
        button = self.app.btn_theme
        self.app.update_idletasks()
        self.app.update()
        share = ((button._icon.winfo_width() * button._icon.winfo_height())
                 / float(button.winfo_width() * button.winfo_height()))
        self.assertGreater(share, 0.2)
        self.assertLessEqual(share, 0.7)

    # -- 2. the top bar must be compact ------------------------------------
    def test_topbar_height_is_compact(self):
        """Scale-aware: scale-independent logical height must be modest."""
        self.app.update_idletasks()
        self.app.update()
        height = self.app.topbar.winfo_height()
        logical = height / float(self.app.scale)
        self.assertLess(logical, 46, f"top bar is {height}px ({logical:.0f} logical)")

    def test_chip_is_not_oversized(self):
        self.app.update_idletasks()
        self.app.update()
        self.assertLess(self.app.chip.winfo_height(),
                        self.app.px(34), "chip is too tall")
        for name in ("btn_output", "btn_jobs", "btn_theme", "btn_connect"):
            button = getattr(self.app, name)
            self.assertLessEqual(button.winfo_height(), self.app.px(30), name)

    def test_ui_scale_is_sane(self):
        self.assertGreaterEqual(self.app.scale, 1.0)
        self.assertLessEqual(self.app.scale, 2.0)

    def test_pixel_fonts_are_used_for_text(self):
        """Point sizes get multiplied by Tk's scaling; pixels do not, which is
        what makes text and canvas widgets agree."""
        for key, value in self.app._fonts.items():
            if key == "mono":
                continue
            self.assertLess(value[1], 0, f"{key} is not a pixel size: {value}")

    # -- 3. the hint page must follow the real login state -----------------
    def test_hint_page_stops_saying_not_logged_in(self):
        from bbpull.gui.app import BbpullApp

        app = BbpullApp(self.cfg, lambda *a: None)
        app.withdraw()
        try:
            app.session = None
            app.render_placeholder()
            self.assertIn("尚未登入", self._placeholder_text(app))

            # Simulate the login result arriving through the normal queue path.
            app.queue.put(("session", object()))
            app._pump()
            app.update()
            text = self._placeholder_text(app)
            self.assertNotIn("尚未登入", text,
                             "after login the page must stop claiming otherwise")
            self.assertIn("尚未選擇課程", text)
        finally:
            app._shutdown()

    @staticmethod
    def _placeholder_text(app):
        texts = []

        def collect(widget):
            for child in widget.winfo_children():
                try:
                    texts.append(str(child.cget("text")))
                except Exception:  # noqa: BLE001
                    pass
                collect(child)

        if app._placeholder is not None:
            collect(app._placeholder.frame)
        return " ".join(texts)

    # -- 4. theme/settings must work while loading -------------------------
    def test_theme_toggle_keeps_the_loading_state(self):
        """Toggling the theme mid-load must not replace the skeleton with the
        empty hint page (that made the load look like it had silently failed)."""
        self.app.session = object()
        self.app._loading_course_id = "_12599_1"
        self.app._show_skeleton()
        self.app.update()
        self.assertIsNotNone(self.app._skeleton, "skeleton should be up")
        self.assertIsNone(self.app._placeholder)

        self.app.rebuild_theme()
        self.app.update()
        self.assertIsNotNone(self.app._skeleton,
                             "the loading indicator must survive a theme toggle")
        self.assertIsNone(self.app._placeholder,
                          "the hint page must not replace the loading state")
        self.assertEqual(self.app._loading_course_id, "_12599_1",
                         "the in-flight course must still be remembered")

    def test_settings_dialog_opens_while_loading(self):
        self.app.session = object()
        self.app.load_catalog("_12599_1")
        self.app.update()
        self.app.open_connect()
        self.app.update()
        import customtkinter as ctk

        toplevels = [w for w in self.app.winfo_children() if isinstance(w, ctk.CTkToplevel)]
        self.assertTrue(toplevels, "the connect dialog must open during loading")
        for window in toplevels:
            window.destroy()

    # -- 5. catalog loading must report determinate progress ---------------
    def test_progress_callback_feeds_the_job(self):
        from bbpull.catalog import Catalog
        from bbpull.gui.courses import parse_courses

        seen = []

        class FakeSession:
            base = "http://x"

            def get_all(self, path, params=None, max_pages=500):
                if path.endswith("/contents"):
                    return [{"id": "_1", "title": "F", "hasChildren": True,
                             "contentHandler": {"id": "resource/x-bb-folder"}}]
                return []

        Catalog.build(FakeSession(), "_1", "T", lambda *a: None,
                      progress=lambda done, total: seen.append((done, total)))
        self.assertTrue(seen, "progress must be reported during the walk")
        done, total = seen[-1]
        self.assertGreaterEqual(done, 1)
        self.assertGreaterEqual(total, done)

    def test_sweep_bar_becomes_determinate(self):
        self.app.session = object()
        self.app._start_sweep()
        self.assertIsNone(self.app._sweep_fraction, "starts indeterminate")
        self.app._set_sweep_progress(0.5)
        self.assertAlmostEqual(self.app._sweep_fraction, 0.5)
        self.app.update()
        coords = self.app.sweep.coords(self.app.sweep_block)
        # Determinate mode fills from the left edge.
        self.assertEqual(coords[0], 0)

    def test_progress_message_updates_the_bar(self):
        self.app.session = object()
        self.app._start_sweep()
        self.app.queue.put(("catalog_progress", (3, 10)))
        self.app._pump()
        self.assertAlmostEqual(self.app._sweep_fraction, 0.3)

    # -- 6. page transitions ----------------------------------------------
    def test_navigate_fades_out_before_rebuilding(self):
        from bbpull.catalog import Catalog

        tree = build_flat_tree(6)
        self.app.catalog = Catalog("_1_1", "T", "http://x", tree)
        self.app.by_id = {n["id"]: n for n in tree}
        self.app.parent_of = {n["id"]: None for n in tree}
        self.app.render_content()
        before = dict(self.app._rows)
        self.assertTrue(before, "rows should be rendered first")

        self.app.navigate("root")
        # The rebuild is deferred to the fade's completion, so on this frame the
        # original row objects must still be in place.
        self.assertIs(before["n0"], self.app._rows["n0"],
                      "the list must not be rebuilt before the fade completes")

        for _ in range(40):
            self.app.update()
            time.sleep(0.02)
        self.assertTrue(self.app._rows, "content must come back after the fade")
        self.assertIsNot(self.app._rows.get("n0"), before["n0"],
                         "the fade should end in a rebuilt list")

    def test_placeholder_fades_in(self):
        self.app.session = None
        self.app.render_placeholder()
        self.app.update()
        self.assertIsNotNone(self.app._placeholder)
        # Fading animates the label colours from the background upwards.
        title = self.app._placeholder.title.cget("fg")
        self.assertTrue(str(title).startswith("#"))


if __name__ == "__main__":
    unittest.main()
