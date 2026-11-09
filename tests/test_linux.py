"""Linux / POSIX behaviour.

The application was written on Windows, and a few "cross-platform" paths were
only nominally so: opening the output folder ran `webbrowser.open("file://…")`
on Linux (a *browser* listing the directory, not the file manager), the data
directory was a dotted folder in `$HOME`, and the Qt build imported the Tk app
package, which made Tk a hard requirement for an interface that does not use it.

These tests pin the behaviour that makes the Linux build work: the freedesktop
opener, XDG data locations, the platform-independent Qt plugin check, and the
POSIX launcher/installer the release archive ships.
"""

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbpull import config, desktop, healthcheck  # noqa: E402

POSIX = os.name != "nt"


class OpenInFileManagerTests(unittest.TestCase):
    def test_missing_path_is_not_opened(self):
        self.assertFalse(
            desktop.open_in_file_manager(str(ROOT / "definitely-not-here")))

    def test_empty_path_is_not_opened(self):
        self.assertFalse(desktop.open_in_file_manager(""))

    @unittest.skipUnless(POSIX, "the Windows branch uses os.startfile")
    def test_linux_uses_xdg_open_not_a_browser(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                    desktop.shutil, "which",
                    side_effect=lambda name: "/usr/bin/xdg-open"
                    if name == "xdg-open" else None), \
                    mock.patch.object(desktop, "_spawn",
                                      side_effect=lambda argv: calls.append(list(argv)) or True):
                self.assertTrue(desktop.open_in_file_manager(tmp))
        self.assertEqual(calls, [["/usr/bin/xdg-open", tmp]],
                         "a directory must go to the file manager, not a browser")

    @unittest.skipUnless(POSIX, "GLib fallback is POSIX-only")
    def test_gio_is_the_fallback_when_xdg_open_is_missing(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                    desktop.shutil, "which",
                    side_effect=lambda name: "/usr/bin/gio" if name == "gio" else None), \
                    mock.patch.object(desktop, "_spawn",
                                      side_effect=lambda argv: calls.append(list(argv)) or True):
                self.assertTrue(desktop.open_in_file_manager(tmp))
        self.assertEqual(calls, [["/usr/bin/gio", "open", tmp]])

    def test_no_opener_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(desktop.shutil, "which", return_value=None), \
                    mock.patch.object(desktop, "_spawn", return_value=False):
                self.assertFalse(desktop.open_in_file_manager(tmp))


@unittest.skipUnless(POSIX, "linux/macos dialog helpers are not used on Windows")
class ShowMessageTests(unittest.TestCase):
    def test_linux_uses_zenity_when_present(self):
        calls = []
        with mock.patch.object(
                desktop.shutil, "which",
                side_effect=lambda name: "/usr/bin/zenity" if name == "zenity" else None), \
                mock.patch.object(desktop, "_spawn",
                                  side_effect=lambda argv: calls.append(list(argv)) or True):
            self.assertTrue(desktop.show_message("bbpull", "cannot start"))
        self.assertEqual(calls[0][0], "/usr/bin/zenity")
        self.assertIn("cannot start", calls[0])

    def test_missing_dialog_helper_is_not_an_error(self):
        with mock.patch.object(desktop.shutil, "which", return_value=None):
            self.assertFalse(desktop.show_message("bbpull", "no helper installed"))


@unittest.skipUnless(POSIX, "XDG is a POSIX convention")
class UserDataDirTests(unittest.TestCase):
    def test_xdg_data_home_is_respected(self):
        with mock.patch.dict(os.environ,
                             {"XDG_DATA_HOME": "/tmp/xdg-bbpull-test"}):
            self.assertEqual(config.user_data_dir(),
                             "/tmp/xdg-bbpull-test/bbpull")

    def test_default_is_the_local_share_directory(self):
        saved = os.environ.pop("XDG_DATA_HOME", None)
        try:
            expected = os.path.join(os.path.expanduser("~"), ".local",
                                    "share", "bbpull")
            self.assertEqual(config.user_data_dir(), expected)
        finally:
            if saved is not None:
                os.environ["XDG_DATA_HOME"] = saved


class QtPlatformPluginTests(unittest.TestCase):
    """Qt names its platform plugin differently on every OS."""

    def _check_with(self, names, layout="legacy"):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            if layout == "qt":
                platforms = base / "Qt" / "plugins" / "platforms"
            else:
                platforms = base / "plugins" / "platforms"
            platforms.mkdir(parents=True)
            for name in names:
                (platforms / name).write_bytes(b"")
            fake = types.SimpleNamespace(__file__=str(base / "__init__.py"))
            with mock.patch.dict(sys.modules, {"PySide6": fake}):
                return healthcheck._check_qt_platform()

    def test_linux_qxcb_plugin_is_accepted(self):
        ok, lines = self._check_with(["libqxcb.so", "libqwayland-egl.so"])
        self.assertTrue(ok, "\n".join(lines))
        self.assertTrue(any("libqxcb" in line for line in lines))

    def test_modern_pyside_qt_plugin_layout_is_checked(self):
        """PySide6 6.5+ puts the plugins under `PySide6/Qt/plugins`."""
        ok, lines = self._check_with(["libqxcb.so"], layout="qt")
        self.assertTrue(ok, "\n".join(lines))
        self.assertTrue(any("libqxcb" in line for line in lines))

    def test_windows_plugin_is_still_accepted(self):
        ok, lines = self._check_with(["qwindows.dll"])
        self.assertTrue(ok, "\n".join(lines))

    def test_empty_plugin_directory_fails(self):
        ok, lines = self._check_with([])
        self.assertFalse(ok)
        self.assertTrue(any("沒有" in line for line in lines))


@unittest.skipUnless(POSIX, "the POSIX launcher only exists on Linux/macOS")
class PosixLauncherTests(unittest.TestCase):
    LAUNCHER = ROOT / "bbpull.sh"

    def setUp(self):
        if not self.LAUNCHER.is_file():
            self.skipTest("bbpull.sh missing")

    def test_launcher_keeps_lf_line_endings(self):
        data = self.LAUNCHER.read_bytes()
        self.assertNotIn(b"\r\n", data,
                         "a CRLF shebang fails with '/bin/sh^M: bad interpreter'")

    def test_launcher_is_executable(self):
        self.assertTrue(os.access(self.LAUNCHER, os.X_OK))

    def test_launcher_prefers_the_project_venv(self):
        text = self.LAUNCHER.read_text(encoding="utf-8")
        self.assertIn(".venv/bin/python", text)
        self.assertIn("-m bbpull", text)
        self.assertIn("PYTHONPATH", text,
                      "the launcher must work from any current directory")


class BuildToolPlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools import build_exe

        cls.build = build_exe

    def test_binary_names_match_the_platform(self):
        if POSIX:
            self.assertEqual(self.build.CONSOLE_NAME, "bbpull")
            self.assertEqual(self.build.GUI_NAME, "bbpull-gui")
        else:
            self.assertEqual(self.build.CONSOLE_NAME, "bbpull.exe")
            self.assertEqual(self.build.GUI_NAME, "bbpull-gui.exe")

    def test_release_is_tar_gz_on_posix(self):
        if POSIX:
            self.assertTrue(self.build.platform_tag().startswith("linux-"))
            self.assertTrue(hasattr(self.build, "tar_release"))
        else:
            zip_tag = self.build.platform_tag()
            self.assertTrue(zip_tag.startswith("windows-"), zip_tag)

    def test_the_window_check_is_a_real_window(self):
        """A process that stays alive is not evidence that a window exists."""
        argvs = [argv for _label, argv, _expected in self.build.SMOKE_TESTS]
        self.assertIn(["gui", "--smoke", "--qt"], argvs)

    @unittest.skipUnless(POSIX, "sh -n needs a POSIX shell")
    def test_generated_installer_is_valid_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "install.sh"
            script.write_text(self.build.INSTALL_SH, encoding="utf-8")
            result = subprocess.run(["sh", "-n", str(script)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_needs_no_root(self):
        self.assertNotIn("sudo", self.build.INSTALL_SH)
        self.assertIn('BBPULL_PREFIX:-$HOME/.local', self.build.INSTALL_SH)

    def test_gui_wrapper_runs_the_console_binary(self):
        self.assertIn('./bbpull gui', self.build.GUI_WRAPPER)

    @unittest.skipUnless(POSIX, "shell wrapper is POSIX-only")
    def test_gui_wrapper_is_valid_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "bbpull-gui"
            script.write_text(self.build.GUI_WRAPPER, encoding="utf-8")
            result = subprocess.run(["sh", "-n", str(script)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
