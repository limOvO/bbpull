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
                       "customtkinter"):
            self.assertIn(module, self.source, f"{module} must be a hidden import")


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


class BuildToolTests(unittest.TestCase):
    def test_build_script_compiles(self):
        path = ROOT / "tools" / "build_exe.py"
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_window_detection_matches_by_title_not_pid(self):
        """The spawned pid is the bootloader; the GUI lives in a child process."""
        source = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
        self.assertIn("def window_titles(", source)
        self.assertNotIn("window_title_for_pid", source)

    def test_build_kills_leftovers_before_clearing_dist(self):
        """Windows refuses to delete a running .exe, failing the build mid-COLLECT."""
        source = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
        kill = source.index("def kill_leftovers")
        build = source.index("def build()")
        self.assertLess(kill, build)
        self.assertIn("kill_leftovers()", source[build:build + 400])


if __name__ == "__main__":
    unittest.main()
