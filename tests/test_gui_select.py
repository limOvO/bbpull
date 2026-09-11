"""Tests for GUI engine selection.

These exist because a silent fallback from Qt to Tk made a whole round of
debugging describe the wrong interface. The rules under test:

* a chosen engine is always reported, with a reason;
* `require_qt` turns a missing PySide6 into a failure, never a downgrade;
* the install hint names the interpreter that actually needs the package,
  because `pip install PySide6` with a different `python` is what caused it;
* contradictory flags are rejected.

Pure logic: no GUI is created and no interpreter is launched.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull.gui_select import (  # noqa: E402
    ENGINE_QT,
    ENGINE_TK,
    QT_PACKAGE,
    candidate_interpreters,
    decide,
    pip_command,
    report,
)

WITH_QT = {"exe": r"C:\Python313\python.exe", "qt": "6.11.2",
           "tk": "5.2.2", "tkinter": True}
WITHOUT_QT = {"exe": r"C:\Python312\python.exe", "qt": None,
              "tk": "5.2.2", "tkinter": True}
BARE = {"exe": r"C:\Python312\python.exe", "qt": None, "tk": None, "tkinter": True}
NO_TKINTER = {"exe": r"C:\Python312\python.exe", "qt": None, "tk": None,
              "tkinter": False}


class DecideTests(unittest.TestCase):
    def test_prefers_qt_when_available(self):
        choice = decide(info=WITH_QT)
        self.assertEqual(choice["engine"], ENGINE_QT)
        self.assertIn("6.11.2", choice["reason"])
        self.assertFalse(choice["warnings"])

    def test_falls_back_to_tk_but_warns(self):
        choice = decide(info=WITHOUT_QT)
        self.assertEqual(choice["engine"], ENGINE_TK)
        self.assertTrue(choice["warnings"],
                        "a downgrade must not be silent")

    def test_warning_names_the_interpreter_that_needs_the_package(self):
        choice = decide(info=WITHOUT_QT)
        self.assertIn("C:\\Python312\\python.exe", choice["warnings"][0])
        self.assertIn("PySide6", choice["warnings"][0])

    def test_hint_targets_the_running_interpreter(self):
        """`python -m pip install` with the wrong python is the original bug."""
        choice = decide(info=WITHOUT_QT)
        self.assertIn("C:\\Python312\\python.exe -m pip install", choice["hint"])
        self.assertIn(QT_PACKAGE, choice["hint"])

    def test_require_qt_refuses_to_downgrade(self):
        choice = decide(require_qt=True, info=WITHOUT_QT)
        self.assertIsNone(choice["engine"], "must not silently open Tk")
        self.assertIn("--qt", choice["reason"])

    def test_require_qt_succeeds_when_available(self):
        choice = decide(require_qt=True, info=WITH_QT)
        self.assertEqual(choice["engine"], ENGINE_QT)

    def test_force_tk_wins_even_with_qt(self):
        choice = decide(force_tk=True, info=WITH_QT)
        self.assertEqual(choice["engine"], ENGINE_TK)
        self.assertIn("--tk", choice["reason"])
        self.assertFalse(choice["warnings"],
                         "an explicitly requested engine is not a downgrade")

    def test_no_engine_at_all_is_reported(self):
        choice = decide(info=NO_TKINTER)
        self.assertIsNone(choice["engine"])
        self.assertIn("neither", choice["reason"])
        self.assertTrue(choice["hint"])

    def test_bare_interpreter_still_uses_tk_when_tkinter_exists(self):
        choice = decide(info=BARE)
        self.assertEqual(choice["engine"], ENGINE_TK)
        self.assertTrue(choice["warnings"])


class PipCommandTests(unittest.TestCase):
    def test_plain_path(self):
        self.assertEqual(
            pip_command("/usr/bin/python3", "PySide6-Essentials"),
            "/usr/bin/python3 -m pip install PySide6-Essentials")

    def test_windows_path_with_spaces_is_quoted(self):
        command = pip_command(r"C:\Program Files\Python\python.exe", "x")
        self.assertTrue(command.startswith('"C:\\Program Files\\Python\\python.exe"'),
                        command)

    def test_missing_exe_falls_back_to_python(self):
        self.assertIn("python -m pip install", pip_command(None, "x"))


class CandidateInterpreterTests(unittest.TestCase):
    def test_current_interpreter_is_included(self):
        found = candidate_interpreters()
        self.assertTrue(found)
        keys = {os.path.normcase(os.path.abspath(p)) for p in found}
        self.assertIn(os.path.normcase(os.path.abspath(sys.executable)), keys)

    def test_no_case_insensitive_duplicates(self):
        """`py -0p` reports `python.EXE` while sys.executable says `python.exe`."""
        found = candidate_interpreters()
        keys = [os.path.normcase(os.path.abspath(p)) for p in found]
        self.assertEqual(len(keys), len(set(keys)),
                         f"duplicate interpreters: {found}")

    def test_every_candidate_exists_on_disk(self):
        for path in candidate_interpreters():
            self.assertTrue(os.path.isfile(path), path)


class ReportTests(unittest.TestCase):
    def test_report_states_the_engine_and_reason(self):
        text = report(scan=False)
        self.assertIn("GUI 引擎檢查", text)
        self.assertIn("執行中的 Python", text)
        self.assertIn("PySide6", text)
        self.assertIn("預設會使用", text)
        self.assertIn("原因", text)

    def test_report_never_claims_qt_without_pyside6(self):
        """The report must agree with `decide`, or it misleads again."""
        text = report(scan=False)
        from bbpull.gui_select import current_interpreter

        choice = decide(info=current_interpreter())
        if choice["engine"] == ENGINE_QT:
            self.assertIn("Qt（PySide6）", text)
        else:
            self.assertNotIn("預設會使用 : Qt（PySide6）", text)


class CliEngineRoutingTests(unittest.TestCase):
    """End-to-end: the CLI must route to the chosen engine and say so.

    These intercept both `run_gui` entry points, so no window is ever created.
    """

    def setUp(self):
        from bbpull import cli, gui_select

        self.cli = cli
        self.gui_select = gui_select
        self.calls = []
        self._saved = {}

        class Args:
            tk = False
            qt = False
            check = False
            inspect = False
            inspect_dir = None

        self.args = Args()
        self.cfg = type("Cfg", (), {"username": "u", "course_id": "_1_1"})()

    def _patch(self, module, name, replacement):
        self._saved[(module, name)] = getattr(module, name)
        setattr(module, name, replacement)

    def tearDown(self):
        for (module, name), original in self._saved.items():
            setattr(module, name, original)

    def _patch_runners(self):
        import bbpull.gui.app as tk_app
        import bbpull.gui_qt.window as qt_window

        self._patch(qt_window, "run_gui",
                    lambda *a, **k: (self.calls.append(("qt", k)), 0)[1])
        self._patch(tk_app, "run_gui",
                    lambda *a, **k: (self.calls.append(("tk", k)), 0)[1])

    def _run(self):
        return self.cli.cmd_gui(self.args, self.cfg, lambda *a: None)

    def test_routes_to_qt_when_available(self):
        self._patch_runners()
        self._patch(self.gui_select, "decide",
                    lambda **k: {"engine": "qt", "reason": "r", "hint": None,
                                 "warnings": [], "info": {"exe": "x", "qt": "6"}})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.calls[0][0], "qt")

    def test_downgrade_passes_a_notice_to_the_tk_window(self):
        """The user must see the downgrade inside the app, not only in a log."""
        self._patch_runners()
        self._patch(self.gui_select, "decide",
                    lambda **k: {"engine": "tk", "reason": "no PySide6",
                                 "hint": "C:\\Python312\\python.exe -m pip install "
                                         "PySide6-Essentials",
                                 "warnings": ["PySide6 不在這個 Python 裡，改用 Tk"],
                                 "info": {"exe": "C:\\Python312\\python.exe",
                                          "qt": None}})
        self.assertEqual(self._run(), 0)
        kind, kwargs = self.calls[0]
        self.assertEqual(kind, "tk")
        self.assertIn("notice", kwargs)
        self.assertIn("PySide6", kwargs["notice"],
                      "the notice must name the missing package")

    def test_require_qt_without_qt_is_a_config_error_not_a_window(self):
        self._patch_runners()
        self._patch(self.gui_select, "decide",
                    lambda **k: {"engine": None, "reason": "--qt was requested",
                                 "hint": "install it", "warnings": [],
                                 "info": {"exe": "x", "qt": None}})
        self.assertEqual(self._run(), self.cli.EXIT_CONFIG)
        self.assertEqual(self.calls, [], "nothing may open on a hard failure")

    def test_check_prints_a_report_and_exits_without_a_window(self):
        self._patch_runners()
        lines = []
        self.args.check = True
        rc = self.cli.cmd_gui(self.args, self.cfg, lines.append)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [], "--check must not open a window")
        self.assertTrue(any("GUI 引擎檢查" in str(line) for line in lines))

    def test_conflicting_flags_are_rejected(self):
        self._patch_runners()
        self.args.tk = True
        self.args.qt = True
        self.assertEqual(self._run(), self.cli.EXIT_USAGE)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
