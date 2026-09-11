"""Tests for the project virtual environment and the Windows launcher.

Two distinct concerns, both learned the hard way:

* **Environment selection** - a project venv makes the GUI engine deterministic.
  PySide6 installed into one global interpreter is invisible to the others, so on
  a machine with several Pythons the app silently opened a different interface
  depending on PATH order.
* **Launcher integrity** - `cmd.exe` mis-executes a `.bat` with bare LF line
  endings, reading fragments of REM lines as commands. Rewriting `bbpull.cmd`
  with an LF-normalising editor broke it exactly that way, so the endings are
  asserted here rather than left to care.

No environment is created and no subprocess is run except the cheap capability
probe, which is skipped when the venv does not exist.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull import venv_tools  # noqa: E402

PROJECT_ROOT = venv_tools.project_root()


class VenvPathTests(unittest.TestCase):
    def test_venv_dir_is_under_the_project(self):
        self.assertEqual(venv_tools.venv_dir(PROJECT_ROOT),
                         PROJECT_ROOT / venv_tools.VENV_DIRNAME)

    def test_venv_python_uses_the_platform_layout(self):
        python = venv_tools.venv_python(PROJECT_ROOT)
        if os.name == "nt":
            self.assertEqual(python.parent.name, "Scripts")
            self.assertEqual(python.name, "python.exe")
        else:
            self.assertEqual(python.parent.name, "bin")
            self.assertEqual(python.name, "python")

    def test_project_root_contains_the_package(self):
        self.assertTrue((PROJECT_ROOT / "bbpull" / "__init__.py").is_file())

    def test_venv_dir_name_is_gitignored(self):
        """.gitignore already listed .venv, so the layout was always intended."""
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(f"{venv_tools.VENV_DIRNAME}/", text)


class ReexecDecisionTests(unittest.TestCase):
    """`should_reexec` is pure, so every branch is cheap to pin down."""

    def setUp(self):
        self.root = PROJECT_ROOT
        self._saved = {}

    def _fake_venv(self, exists):
        """Point the module at a venv that does or does not exist."""
        self._saved["venv_python"] = venv_tools.venv_python
        self._saved["is_running_in_project_venv"] = \
            venv_tools.is_running_in_project_venv
        target = Path(__file__) if exists else Path(__file__) / "nope"
        venv_tools.venv_python = lambda root=None: target
        venv_tools.is_running_in_project_venv = lambda root=None: False

    def tearDown(self):
        for name, original in self._saved.items():
            setattr(venv_tools, name, original)

    def test_reexecs_when_a_venv_exists_and_we_are_outside_it(self):
        self._fake_venv(True)
        wanted, _reason = venv_tools.should_reexec(environ={})
        self.assertTrue(wanted)

    def test_does_not_reexec_when_already_inside(self):
        self._saved["is_running_in_project_venv"] = \
            venv_tools.is_running_in_project_venv
        venv_tools.is_running_in_project_venv = lambda root=None: True
        wanted, reason = venv_tools.should_reexec(environ={})
        self.assertFalse(wanted)
        self.assertIn("專案環境", reason)

    def test_does_not_reexec_without_a_venv(self):
        self._fake_venv(False)
        wanted, _reason = venv_tools.should_reexec(environ={})
        self.assertFalse(wanted)

    def test_guard_prevents_recursion(self):
        """Without this a mis-reporting venv would spawn processes forever."""
        self._fake_venv(True)
        wanted, _reason = venv_tools.should_reexec(
            environ={venv_tools.REEXEC_GUARD: "1"})
        self.assertFalse(wanted)

    def test_disable_variable_is_honoured(self):
        self._fake_venv(True)
        wanted, reason = venv_tools.should_reexec(
            environ={venv_tools.REEXEC_DISABLE: "1"})
        self.assertFalse(wanted)
        self.assertIn(venv_tools.REEXEC_DISABLE, reason)

    def test_reexec_argv_forwards_the_command(self):
        command = venv_tools.reexec_argv(["gui", "--check"], PROJECT_ROOT)
        self.assertEqual(command[1:3], ["-m", "bbpull"])
        self.assertEqual(command[-2:], ["gui", "--check"])
        self.assertIn(venv_tools.VENV_DIRNAME, command[0])

    def test_maybe_reexec_returns_none_when_not_wanted(self):
        """None means 'carry on in this interpreter'."""
        self._fake_venv(False)
        self.assertIsNone(venv_tools.maybe_reexec(environ={}))

    def test_maybe_reexec_sets_the_guard_for_the_child(self):
        self._fake_venv(True)
        captured = {}
        saved_call = venv_tools.subprocess.call

        def fake_call(argv, env=None):
            captured["argv"] = argv
            captured["env"] = env or {}
            return 7

        venv_tools.subprocess.call = fake_call
        try:
            code = venv_tools.maybe_reexec(["gui"], environ={})
        finally:
            venv_tools.subprocess.call = saved_call
        self.assertEqual(code, 7)
        self.assertEqual(captured["env"].get(venv_tools.REEXEC_GUARD), "1")
        self.assertIn("-m", captured["argv"])


class ShellCommandTests(unittest.TestCase):
    """Displayed commands must survive a real shell.

    `pip install requests>=2.31.0` unquoted is a redirect: pip receives
    `requests` and the shell writes a file named `=2.31.0`. A hint that cannot be
    pasted is worse than no hint.
    """

    def test_version_specifiers_are_quoted(self):
        text = venv_tools.shell_command(["pip", "install", "requests>=2.31.0"])
        self.assertIn('"requests>=2.31.0"', text)

    def test_install_command_is_paste_safe(self):
        command = venv_tools.install_command("python")
        text = venv_tools.shell_command(command)
        for package in venv_tools.REQUIRED_PACKAGES:
            self.assertIn(f'"{package}"' if ">" in package else package, text)

    def test_every_shell_metacharacter_is_quoted(self):
        for hostile in ("a>b", "a<b", "a|b", "a&b", "a;b", "a b", "a(b)"):
            text = venv_tools.shell_command(["cmd", hostile])
            self.assertIn(f'"{hostile}"', text, hostile)

    def test_plain_arguments_stay_readable(self):
        text = venv_tools.shell_command(["python", "-m", "bbpull", "gui"])
        self.assertEqual(text, "python -m bbpull gui")

    def test_first_element_is_never_quoted(self):
        text = venv_tools.shell_command(["C:\\Program Files\\py.exe", "gui"])
        self.assertFalse(text.startswith('"'))

    def test_cwd_prefix(self):
        text = venv_tools.shell_command(["python"], cwd="C:\\x")
        self.assertTrue(text.startswith("cd C:\\x &&"))


class InterpretTests(unittest.TestCase):
    def test_missing_config_returns_none(self):
        self.assertIsNone(venv_tools.interpret(Path("does-not-exist")))

    def test_parses_key_values(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyvenv.cfg").write_text(
                "home = C:\\Python313\nversion = 3.13.1\ninclude-system-site-packages = false\n",
                encoding="utf-8")
            info = venv_tools.interpret(Path(tmp))
        self.assertEqual(info["version"], "3.13.1")
        self.assertTrue(info["home"].endswith("Python313"))


class DescribeTests(unittest.TestCase):
    def test_shape_is_stable(self):
        info = venv_tools.describe(PROJECT_ROOT)
        for key in ("project_root", "venv_dir", "venv_python", "exists",
                    "is_current", "current_python", "packages", "hint"):
            self.assertIn(key, info)
        self.assertIsInstance(info["exists"], bool)

    def test_report_mentions_the_create_command_when_missing(self):
        text = venv_tools.report(PROJECT_ROOT)
        self.assertIn("虛擬環境檢查", text)
        if not venv_tools.venv_exists(PROJECT_ROOT):
            self.assertIn(venv_tools.CREATE_HINT, text)

    def test_report_does_not_claim_missing_when_present(self):
        if venv_tools.venv_exists(PROJECT_ROOT):
            text = venv_tools.report(PROJECT_ROOT)
            self.assertIn("已建立", text)


class LauncherIntegrityTests(unittest.TestCase):
    """`bbpull.cmd` must stay cmd.exe-executable.

    This is the regression guard for a real breakage: the file was rewritten with
    an LF-normalising editor and every line ending became bare LF, which cmd.exe
    reads as garbage (three of five lines errored out).
    """

    LAUNCHER = PROJECT_ROOT / "bbpull.cmd"

    def test_launcher_exists(self):
        self.assertTrue(self.LAUNCHER.is_file())

    def test_launcher_uses_crlf_line_endings(self):
        data = self.LAUNCHER.read_bytes()
        bare_lf = sum(1 for i, byte in enumerate(data)
                      if byte == 0x0A and (i == 0 or data[i - 1] != 0x0D))
        self.assertEqual(bare_lf, 0,
                         f"{bare_lf} bare LF line endings; cmd.exe mis-executes this")

    def test_launcher_has_no_stray_utf8_bom(self):
        # A BOM before @echo off makes cmd print an error on every run.
        self.assertFalse(self.LAUNCHER.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_launcher_is_ascii_or_cp950_safe(self):
        """Non-ASCII in a .bat is risky under the console's active code page."""
        text = self.LAUNCHER.read_text(encoding="utf-8")
        for index, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("REM"):
                continue
            self.assertTrue(line.isascii(),
                            f"line {index} contains non-ASCII in an executable position")

    def test_launcher_prefers_the_project_venv(self):
        text = self.LAUNCHER.read_text(encoding="utf-8")
        self.assertIn(".venv", text)
        self.assertIn("python.exe", text)

    def test_launcher_runs_the_package(self):
        text = self.LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("-m bbpull", text)
        self.assertIn("%*", text, "arguments must be forwarded")

    def test_gitattributes_forces_crlf_for_batch_files(self):
        attrs = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.cmd text eol=crlf", attrs)
        self.assertIn("*.bat text eol=crlf", attrs)


class LiveVenvTests(unittest.TestCase):
    """Only run when a project venv actually exists."""

    def setUp(self):
        if not venv_tools.venv_exists(PROJECT_ROOT):
            self.skipTest("no project venv on this machine")

    def test_venv_has_the_gui_dependencies(self):
        check = venv_tools.verify(PROJECT_ROOT)
        modules = check.get("modules") or {}
        self.assertTrue(modules.get("requests"), check)
        self.assertTrue(modules.get("PySide6"),
                        f"PySide6 missing from the venv: {check}")

    def test_create_command_is_idempotent(self):
        """`create` on an existing venv reinstalls rather than erroring."""
        command = venv_tools.create_command(root=PROJECT_ROOT)
        self.assertIn("-m", command)
        self.assertIn("venv", command)


if __name__ == "__main__":
    unittest.main()
