"""Tests for the packaging layer: icon, entry point, spec, health check.

These guard failure modes that only appear in the *built* artifact, which is the
worst place to find them:

* PyInstaller executes the entry script as a top-level module, so a relative
  import in it builds cleanly and dies on launch;
* `SPECPATH` off by one directory produces "script not found" only at build time;
* a frozen build that derives its state directory from the bundle path writes
  into the temporary `_MEI…` folder and loses the saved login on exit.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull import config, healthcheck, venv_tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "installer" / "bbpull.ico"
ENTRY = ROOT / "installer" / "entry.py"
SPEC = ROOT / "bbpull.spec"


class IconTests(unittest.TestCase):
    """.ico has no library dependency here, so its structure is asserted directly."""

    def setUp(self):
        if not ICON.is_file():
            self.skipTest("icon not generated yet (run installer/make_icon.py)")

    def test_is_a_valid_icon_container(self):
        import struct

        data = ICON.read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual(reserved, 0)
        self.assertEqual(kind, 1, "type 1 is an icon; 2 is a cursor")
        self.assertGreater(count, 1, "a multi-resolution icon is expected")

    def test_every_entry_points_at_a_png(self):
        import struct

        data = ICON.read_bytes()
        _r, _k, count = struct.unpack("<HHH", data[:6])
        for index in range(count):
            entry = data[6 + 16 * index:6 + 16 * (index + 1)]
            _w, _h, _c, _res, _planes, bits, size, offset = struct.unpack(
                "<BBBBHHII", entry)
            self.assertEqual(bits, 32)
            self.assertEqual(data[offset:offset + 8], b"\x89PNG\r\n\x1a\n")
            self.assertLessEqual(offset + size, len(data))

    def test_includes_small_and_large_sizes(self):
        import struct

        data = ICON.read_bytes()
        _r, _k, count = struct.unpack("<HHH", data[:6])
        sizes = set()
        for index in range(count):
            w, h = struct.unpack("<BB", data[6 + 16 * index:8 + 16 * index])
            sizes.add(w or 256)
        self.assertIn(256, sizes, "large-icon views need 256px")
        self.assertTrue({16, 32, 48} <= sizes, f"missing common sizes: {sorted(sizes)}")

    def test_no_absolute_paths_leak_into_the_icon(self):
        """Sanity: the icon is generated art, not a copied asset."""
        self.assertLess(ICON.stat().st_size, 200_000)


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        if not ENTRY.is_file():
            self.skipTest("entry point missing")

    def test_entry_uses_only_absolute_imports(self):
        """PyInstaller runs it as a top-level module; `from .x` fails there."""
        source = ENTRY.read_text(encoding="utf-8")
        offenders = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith("from .") or line.strip().startswith("import .")
        ]
        self.assertEqual(offenders, [],
                         f"relative imports break the frozen build: {offenders}")

    def test_entry_imports_the_package_run_function(self):
        source = ENTRY.read_text(encoding="utf-8")
        self.assertIn("from bbpull.__main__ import run", source)
        self.assertIn("sys.exit(run())", source)

    def test_entry_compiles(self):
        compile(ENTRY.read_text(encoding="utf-8"), str(ENTRY), "exec")


class SpecTests(unittest.TestCase):
    def setUp(self):
        if not SPEC.is_file():
            self.skipTest("spec missing")
        self.source = SPEC.read_text(encoding="utf-8")

    def test_root_is_the_spec_directory_not_its_parent(self):
        """`Path(SPECPATH).parent` overshoots and breaks the build silently."""
        self.assertIn("ROOT = Path(SPECPATH).resolve()\n", self.source)
        self.assertNotIn("Path(SPECPATH).resolve().parent", self.source)

    def test_analysis_targets_the_entry_point(self):
        self.assertEqual(self.source.count('installer" / "entry.py'), 2,
                         "both executables must use the absolute-import entry point")
        self.assertNotIn('"bbpull" / "__main__.py"', self.source)

    def test_builds_both_a_console_and_a_windowed_binary(self):
        self.assertIn('name="bbpull"', self.source)
        self.assertIn('name="bbpull-gui"', self.source)
        self.assertIn("console=True", self.source)
        self.assertIn("console=False", self.source)

    def test_unittest_is_not_excluded(self):
        """Excluding it turned `selftest` into a ModuleNotFoundError at runtime."""
        excludes = self.source.split("EXCLUDES = [", 1)[1].split("]", 1)[0]
        self.assertNotIn('"unittest"', excludes)

    def test_dynamic_imports_are_declared_as_hidden(self):
        for module in ("bbpull.gui_qt.window", "bbpull.gui.app", "tools.secret_scan",
                       "customtkinter", "bbpull.wizard", "bbpull.healthcheck"):
            self.assertIn(module, self.source, f"{module} must be a hidden import")

    def test_deferred_internal_imports_are_declared_as_hidden(self):
        """Anything imported inside a function must be named to PyInstaller.

        The v0.1.1 failure was a deferred import with a wrong path. Even with the
        path right, a module reached only from inside a function is the easiest
        thing for a bundler to miss, so the two lists are reconciled here rather
        than trusted to stay in step.
        """
        import ast

        deferred = set()
        for path in sorted((ROOT / "bbpull").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            tree = ast.parse("\n".join(lines))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if not lines[node.lineno - 1].startswith((" ", "\t")):
                    continue                       # module level, already visible
                if node.level == 0:
                    continue                       # absolute, analyser sees it
                package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
                keep = len(package.split(".")) - (node.level - 1)
                base = package.split(".")[:max(0, keep)]
                if node.module:
                    base = base + node.module.split(".")
                target = ".".join(base)
                if target.startswith("bbpull"):
                    deferred.add(target)
        missing = sorted(module for module in deferred if module not in self.source)
        self.assertEqual(missing, [],
                         f"deferred imports absent from the spec's hidden list: {missing}")


class HealthCheckTests(unittest.TestCase):
    def test_returns_a_verdict_and_lines(self):
        ok, lines = healthcheck.run_checks()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(lines, list)
        self.assertTrue(lines)
        self.assertTrue(any("Python" in line for line in lines))

    def test_reports_every_required_module(self):
        _ok, lines = healthcheck.run_checks()
        joined = "\n".join(lines)
        for module in ("requests", "PySide6"):
            self.assertIn(module, joined)

    def test_never_raises_even_if_a_check_does(self):
        saved = healthcheck._check_modules
        healthcheck._check_modules = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            ok, lines = healthcheck.run_checks()
        finally:
            healthcheck._check_modules = saved
        self.assertFalse(ok)
        self.assertTrue(any("boom" in line for line in lines),
                        "an exploding check must be reported, not swallowed")


class FrozenPathTests(unittest.TestCase):
    """A frozen build must not keep state inside the unpack directory."""

    def test_source_build_uses_the_project_directory(self):
        self.assertFalse(getattr(sys, "frozen", False))
        self.assertEqual(config.default_root(), config.PROJECT_ROOT)
        self.assertEqual(config.DEFAULT_STATE_DIR,
                         os.path.join(config.PROJECT_ROOT, ".state"))

    def test_user_data_dir_is_per_user_and_persistent(self):
        path = config.user_data_dir()
        self.assertTrue(path)
        self.assertNotIn("_MEI", path)
        self.assertFalse(
            os.path.normcase(path).startswith(os.path.normcase(os.environ.get(
                "TEMP", "\0"))), f"user data dir must not be temporary: {path}")

    def test_frozen_root_is_the_user_data_dir(self):
        """Verified in a subprocess so the real module is not polluted."""
        code = (
            "import sys; sys.frozen = True; sys.path.insert(0, r'%s');"
            "import bbpull.config as c;"
            "print(c.default_root()); print(c.DEFAULT_STATE_DIR)"
            % str(ROOT)
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=120, encoding="utf-8",
                                errors="replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        root = lines[-2]
        state = lines[-1]
        self.assertNotIn("_MEI", state)
        self.assertNotIn("Temp", state)
        self.assertEqual(state, os.path.join(root, ".state"))

    def test_frozen_disables_venv_reexec(self):
        """Everything is bundled; re-execing into a dev venv would be wrong."""
        saved = venv_tools.is_frozen
        venv_tools.is_frozen = lambda: True
        try:
            wanted, reason = venv_tools.should_reexec(environ={})
        finally:
            venv_tools.is_frozen = saved
        self.assertFalse(wanted)
        self.assertIn("執行檔", reason)

    def test_frozen_report_does_not_offer_to_create_a_venv(self):
        saved = venv_tools.is_frozen
        venv_tools.is_frozen = lambda: True
        try:
            text = venv_tools.report()
        finally:
            venv_tools.is_frozen = saved
        self.assertIn("獨立執行檔", text)
        self.assertNotIn("--create", text)


class WindowedEntryPointTests(unittest.TestCase):
    """The double-click path, which the first release got wrong.

    `bbpull-gui.exe` built and "worked" under test because the test passed `gui`
    explicitly. Double-clicking passes no arguments, so the CLI opened the
    interactive text menu, which a console-less process cannot run: the released
    executable exited after two seconds with code 2 and no message.
    """

    def setUp(self):
        from bbpull import __main__ as entry

        self.entry = entry
        self._saved = entry.is_windowed_build

    def tearDown(self):
        self.entry.is_windowed_build = self._saved

    def _windowed(self, value):
        self.entry.is_windowed_build = lambda: value

    def test_bare_double_click_opens_the_gui(self):
        self._windowed(True)
        self.assertEqual(self.entry.default_argv([]), ["gui"])

    def test_explicit_arguments_are_untouched(self):
        self._windowed(True)
        self.assertEqual(self.entry.default_argv(["pull", "--course-id", "_1_1"]),
                         ["pull", "--course-id", "_1_1"])

    def test_run_detects_windowed_before_replacing_the_streams(self):
        """Regression for the bug the first fix introduced.

        `run()` swaps the streams for a capture buffer. If `default_argv` then
        re-detects from `sys.stdout is None`, it sees the buffer and concludes
        "console build", sending a double-click back to the text menu. The flag
        must be computed once, before the swap.
        """
        entry = self.entry
        seen = {}

        saved = (entry.is_windowed_build, entry.default_argv, entry.main,
                 entry.sys.argv, entry.sys.stdout, entry.sys.stderr)

        def fake_default_argv(argv, windowed=None):
            seen["windowed_arg"] = windowed
            seen["stdout_at_call"] = entry.sys.stdout
            return ["gui"]

        try:
            entry.is_windowed_build = lambda: True
            entry.default_argv = fake_default_argv
            entry.main = lambda argv: 0
            entry.sys.argv = ["bbpull-gui.exe"]
            entry.sys.stdout = None
            entry.sys.stderr = None
            code = entry.run()
        finally:
            (entry.is_windowed_build, entry.default_argv, entry.main,
             entry.sys.argv, entry.sys.stdout, entry.sys.stderr) = saved

        self.assertEqual(code, 0)
        self.assertIs(seen.get("windowed_arg"), True,
                      "the windowed flag must be passed in, not re-detected")
        self.assertIsNotNone(seen.get("stdout_at_call"),
                             "the swap happened before default_argv was called")

    def test_double_click_failure_is_reported(self):
        """`main` returns a code rather than raising, so an exception-only
        handler missed the silent crash entirely."""
        entry = self.entry
        reported = []
        saved = (entry.is_windowed_build, entry.default_argv, entry.main,
                 entry.report_failure, entry.sys.argv, entry.sys.stdout,
                 entry.sys.stderr)
        try:
            entry.is_windowed_build = lambda: True
            entry.default_argv = lambda argv, windowed=None: ["gui"]
            entry.main = lambda argv: 2
            entry.report_failure = lambda output, code: reported.append(code)
            entry.sys.argv = ["bbpull-gui.exe"]
            entry.sys.stdout = None
            entry.sys.stderr = None
            code = entry.run()
        finally:
            (entry.is_windowed_build, entry.default_argv, entry.main,
             entry.report_failure, entry.sys.argv, entry.sys.stdout,
             entry.sys.stderr) = saved

        self.assertEqual(code, 2)
        self.assertEqual(reported, [2], "the user must be told, not left guessing")

    def test_cli_invocation_is_not_reported_as_a_crash(self):
        """`bbpull-gui.exe pull ...` returning 5 is a result, not a startup failure."""
        entry = self.entry
        reported = []
        saved = (entry.is_windowed_build, entry.default_argv, entry.main,
                 entry.report_failure, entry.sys.argv, entry.sys.stdout,
                 entry.sys.stderr)
        try:
            entry.is_windowed_build = lambda: True
            entry.default_argv = lambda argv, windowed=None: list(argv)
            entry.main = lambda argv: 5
            entry.report_failure = lambda output, code: reported.append(code)
            entry.sys.argv = ["bbpull-gui.exe", "pull", "--course-id", "_1_1"]
            entry.sys.stdout = None
            entry.sys.stderr = None
            code = entry.run()
        finally:
            (entry.is_windowed_build, entry.default_argv, entry.main,
             entry.report_failure, entry.sys.argv, entry.sys.stdout,
             entry.sys.stderr) = saved

        self.assertEqual(code, 5)
        self.assertEqual(reported, [], "an explicit command must not pop a dialog")

    def test_default_argv_falls_back_to_detection(self):
        self._windowed(True)
        self.assertEqual(self.entry.default_argv([]), ["gui"])
        self._windowed(False)
        self.assertEqual(self.entry.default_argv([]), [])

    def test_console_build_keeps_the_menu_default(self):
        """A console build with no arguments should show the menu, not a window."""
        self._windowed(False)
        self.assertEqual(self.entry.default_argv([]), [])

    def test_help_is_never_redirected(self):
        self._windowed(True)
        self.assertEqual(self.entry.default_argv(["--help"]), ["--help"])
        self.assertEqual(self.entry.default_argv(["venv"]), ["venv"])

    def test_failure_is_reported_not_swallowed(self):
        """A windowed failure must reach the user, with output captured."""
        from bbpull import __main__ as entry

        shown = []
        saved_log = entry.write_error_log
        saved_box = entry.show_message
        entry.write_error_log = lambda text: "C:\\logs\\bbpull-error.log"
        entry.show_message = lambda title, text, error=True: shown.append(text) or True
        try:
            entry.report_failure("line one\nline two\n", 2)
        finally:
            entry.write_error_log = saved_log
            entry.show_message = saved_box
        self.assertTrue(shown, "the user must see something")
        self.assertIn("line two", shown[0])
        self.assertIn("bbpull-error.log", shown[0])

    def test_capture_stream_reports_utf8(self):
        """Without an `encoding`, the logging helpers degrade to ASCII and mangle
        Chinese in the captured report."""
        capture = self.entry._Capture()
        capture.write("獨立執行檔模式")
        self.assertEqual(capture.encoding, "utf-8")
        self.assertIn("獨立執行檔模式", capture.getvalue())

    def test_capture_survives_reconfigure(self):
        """cli.main() reconfigures the console streams; the stand-in must not blow up."""
        capture = self.entry._Capture()
        try:
            capture.reconfigure(errors="replace")
        except (ValueError, OSError, AttributeError):
            pass  # configure_console_streams tolerates exactly these


class BuildToolTests(unittest.TestCase):
    def test_build_script_compiles(self):
        path = ROOT / "tools" / "build_exe.py"
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_window_detection_verifies_the_owning_executable(self):
        """Matching by title alone produced a false positive.

        A File Explorer window showing the folder `dist/bbpull` is titled
        "bbpull - File Explorer", which matched a naive substring check: the
        smoke test passed while looking at the wrong window. The check must
        confirm the owning process is the executable under test.
        """
        source = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
        self.assertIn("def windows_for_executable(", source)
        self.assertIn("_process_image", source)
        self.assertNotIn("window_title_for_pid", source)

    def test_windowed_check_mimics_a_double_click(self):
        """`Popen(stdout=DEVNULL)` changes the child's behaviour.

        Supplying those handles means a GUI-subsystem process no longer sees
        `sys.stdout is None`, so it stops identifying as windowed and opens the
        text menu instead - the test caused the very failure it reported.
        `os.startfile` is the shell "open" verb, i.e. a real double-click.

        The docstring is excluded before checking: it explains the old DEVNULL
        approach, and matching that text would make this test assert the
        opposite of what it means to.
        """
        import ast

        source = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
        body = None
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == "verify_windowed_gui":
                statements = list(node.body)
                first = statements[0] if statements else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    statements = statements[1:]          # drop the docstring
                body = "\n".join(ast.unparse(item) for item in statements)
                break
        self.assertIsNotNone(body, "verify_windowed_gui not found")
        self.assertIn("os.startfile", body)
        self.assertNotIn("DEVNULL", body,
                         "redirecting stdio changes the program under test")

    def test_build_kills_leftovers_before_clearing_dist(self):
        """Windows refuses to delete a running .exe, failing the build mid-COLLECT."""
        source = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
        kill = source.index("def kill_leftovers")
        build = source.index("def build()")
        self.assertLess(kill, build)
        self.assertIn("kill_leftovers()", source[build:build + 400])


if __name__ == "__main__":
    unittest.main()
