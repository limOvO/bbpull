"""Desktop shortcut generation (Linux `.desktop`, Windows `.lnk`).

The shortcut is a small file that points at the installation, so it keeps
working after an update. The dangerous mistakes are quoting (paths with spaces
are common under `$HOME`) and writing an untrusted launcher that the desktop
silently refuses to run - both are asserted here.
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull import shortcut  # noqa: E402


class QuoteTests(unittest.TestCase):
    def test_plain_argument_stays_unquoted(self):
        self.assertEqual(shortcut.quote_exec_arg("/usr/bin/bbpull"), "/usr/bin/bbpull")

    def test_spaces_are_quoted(self):
        self.assertEqual(shortcut.quote_exec_arg("/home/me/My Apps/bbpull"),
                         '"/home/me/My Apps/bbpull"')

    def test_quotes_and_backslashes_are_escaped(self):
        quoted = shortcut.quote_exec_arg('/tmp/a"b\\c')
        self.assertTrue(quoted.startswith('"') and quoted.endswith('"'))
        self.assertIn('\\"', quoted)
        self.assertIn("\\\\", quoted)


class LaunchArgvTests(unittest.TestCase):
    def test_frozen_uses_its_own_executable(self):
        argv = shortcut.launch_argv(frozen=True, executable="/opt/bbpull/bbpull")
        self.assertEqual(argv, ["/opt/bbpull/bbpull", "gui"])

    def test_source_prefers_the_posix_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "bbpull.sh"
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
            venv_python = Path(tmp) / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
            argv = shortcut.launch_argv(frozen=False, root=tmp)
        self.assertEqual(argv, [str(launcher), "gui"])

    def test_source_ignores_a_launcher_whose_venv_is_missing(self):
        """A checkout shared with another OS can hold the wrong `.venv`."""
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "bbpull.sh"
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
            argv = shortcut.launch_argv(frozen=False, root=tmp)
        self.assertEqual(argv[-3:], ["-m", "bbpull", "gui"])

    def test_source_falls_back_to_the_module_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = shortcut.launch_argv(frozen=False, root=tmp)
        self.assertEqual(argv[-3:], ["-m", "bbpull", "gui"])


class DesktopEntryTests(unittest.TestCase):
    def test_entry_runs_the_gui(self):
        text = shortcut.desktop_entry(
            argv=["/home/me/My Apps/bbpull", "gui"],
            icon="/home/me/My Apps/bbpull.png")
        self.assertIn('Exec="/home/me/My Apps/bbpull" gui', text)
        self.assertIn("Icon=/home/me/My Apps/bbpull.png", text)
        self.assertIn("Terminal=false", text)
        self.assertIn("Categories=Education;", text)
        self.assertIn("StartupWMClass=bbpull", text)
        self.assertTrue(text.startswith("[Desktop Entry]\n"))

    def test_entry_falls_back_to_a_themed_icon(self):
        text = shortcut.desktop_entry(argv=["/usr/bin/bbpull", "gui"], icon="")
        self.assertIn(f"Icon={shortcut.FALLBACK_ICON}", text)


class DesktopDirectoryTests(unittest.TestCase):
    def test_xdg_desktop_directory_is_used(self):
        with mock.patch.object(shortcut.shutil, "which", return_value=None):
            with tempfile.TemporaryDirectory() as tmp:
                desktop = Path(tmp) / "Desktop"
                desktop.mkdir()
                self.assertEqual(shortcut.desktop_directory({"HOME": tmp}), desktop)

    def test_localised_desktop_folder_is_used(self):
        with mock.patch.object(shortcut.shutil, "which", return_value=None):
            with tempfile.TemporaryDirectory() as tmp:
                desktop = Path(tmp) / "桌面"
                desktop.mkdir()
                self.assertEqual(shortcut.desktop_directory({"HOME": tmp}), desktop)

    def test_missing_desktop_falls_back(self):
        with mock.patch.object(shortcut.shutil, "which", return_value=None):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(shortcut.desktop_directory({"HOME": tmp}),
                                 Path(tmp) / "Desktop")


class CreateShortcutTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX .desktop path")
    def test_writes_an_executable_launcher(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(shortcut, "_mark_trusted", return_value=False):
            result = shortcut.create_desktop_shortcut(directory=tmp)
            path = Path(result["path"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.name, "bbpull.desktop")
            mode = path.stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, "launchers must be executable")
            self.assertEqual(path.read_text(encoding="utf-8").splitlines()[0],
                             "[Desktop Entry]")
            self.assertFalse(result["trusted"])

    def test_unwritable_directory_reports_oserror(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(OSError):
                shortcut.create_desktop_shortcut(directory=blocker)


if __name__ == "__main__":
    unittest.main()
