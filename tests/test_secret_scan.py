"""Tests for the secret-scan gate.

The first version of this gate reported 180 findings on a clean tree, which is
the same as having no gate at all. So it is tested from both directions:

* **positive controls** - a planted credential must be caught, or the gate is
  decorative;
* **negative controls** - real lines from this codebase must not be flagged, or
  the gate will be ignored.

The planted values are obviously fake and are assembled at runtime so this test
file is not itself a finding.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import secret_scan  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Assembled at runtime; a literal would make this file a finding. That the
#: gate flagged the first draft of this file (a PEM header and a fake secret)
#: is the evidence it actually works.
FAKE_PASSWORD = "zq" + "7Xv9" + "Kp2m" + "R4t"
FAKE_GH_TOKEN = "ghp_" + "A" * 30
FAKE_PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"
FAKE_LEAKED = "sup3r" + "s3cr3t" + "value"


class PositiveControlTests(unittest.TestCase):
    """A gate that cannot detect a real leak is worthless."""

    def test_detects_a_hardcoded_password_literal(self):
        findings = secret_scan.find_in_text(f'password = "{FAKE_PASSWORD}"\n')
        self.assertTrue(findings, "a quoted password literal must be caught")

    def test_detects_a_password_in_a_dotenv_style_line(self):
        findings = secret_scan.find_in_text(f'BB_PASSWORD="{FAKE_PASSWORD}"\n')
        self.assertTrue(findings)

    def test_detects_a_github_token(self):
        findings = secret_scan.find_in_text(f"token = {FAKE_GH_TOKEN}\n")
        kinds = {kind for kind, _line, _detail in findings}
        self.assertIn("github-token", kinds)

    def test_detects_a_private_key_header(self):
        findings = secret_scan.find_in_text(FAKE_PEM + "\n")
        self.assertTrue(findings)

    def test_detects_a_leaked_known_value(self):
        """The value currently in .env must be hunted wherever it appears."""
        findings = secret_scan.find_in_text(
            f"# oops, the real one is {FAKE_LEAKED}\n", {"BB_PASSWORD": FAKE_LEAKED})
        self.assertTrue(findings)
        self.assertIn("leaked BB_PASSWORD", {k for k, _l, _d in findings})

    def test_detects_single_quoted_literal(self):
        findings = secret_scan.find_in_text(f"SECRET = '{FAKE_PASSWORD}'\n")
        self.assertTrue(findings)

    def test_does_not_print_the_secret(self):
        findings = secret_scan.find_in_text(f'password = "{FAKE_PASSWORD}"\n')
        for _kind, _line, detail in findings:
            self.assertNotIn(FAKE_PASSWORD, detail,
                             "the gate's own output must be safe to paste")


class NegativeControlTests(unittest.TestCase):
    """Real lines from this codebase. Flagging these is why the gate was rewritten."""

    BENIGN = (
        'cfg.password = prompter.ask_secret("密碼", has_current=False)',
        "trial = replace(cfg, username=username, password=password)",
        'username, password = (list(supplied) + ["", ""])[:2]',
        "self.password = QLineEdit()",
        'password: str = ""',
        'secret = entries["password"].get()',
        '"""Write announcements. `only_ids` restricts output to those ids.',
        "BB_OUT_DIR=output",
        "BB_BASE_URL=https://twc.blackboard.com",
        "BB_COURSE_ID=_12529_1",
        'cfg.secret_store().clear()',
        "password=self.password,",
        'if not user or not secret:',
    )

    def test_benign_code_is_never_flagged(self):
        for line in self.BENIGN:
            findings = secret_scan.find_in_text(line)
            self.assertEqual(findings, [],
                             f"false positive on: {line!r} -> {findings}")

    def test_placeholders_are_not_flagged(self):
        for value in ("your-password-here", "CHANGEME", "xxx", "example",
                      "<token>", "{{ secret }}", "placeholder", "dummy"):
            findings = secret_scan.find_in_text(f'password = "{value}"\n')
            self.assertEqual(findings, [],
                             f"placeholder {value!r} must not be flagged")

    def test_empty_default_is_not_flagged(self):
        self.assertEqual(secret_scan.find_in_text('password = ""\n'), [])

    def test_short_values_are_not_flagged(self):
        self.assertEqual(secret_scan.find_in_text('password = "abc"\n'), [])


class EnvKeySelectionTests(unittest.TestCase):
    """Only secret-named keys become hunted values."""

    def test_secret_key_names_match(self):
        for key in ("BB_PASSWORD", "API_KEY", "ACCESS_TOKEN", "CLIENT_SECRET",
                    "JSESSIONID", "DB_PASSWD", "PRIVATE_KEY"):
            self.assertTrue(secret_scan.SECRET_KEY.search(key), key)

    def test_non_secret_key_names_do_not_match(self):
        for key in ("BB_USERNAME", "BB_BASE_URL", "BB_COURSE_ID", "BB_OUT_DIR",
                    "LOG_LEVEL", "PYTHONPATH"):
            self.assertIsNone(secret_scan.SECRET_KEY.search(key), key)

    def test_secret_values_only_reads_secret_keys(self):
        values = secret_scan.secret_values(PROJECT_ROOT)
        for key in values:
            self.assertTrue(secret_scan.SECRET_KEY.search(key), key)
        self.assertNotIn("BB_OUT_DIR", values)
        self.assertNotIn("BB_BASE_URL", values)
        self.assertNotIn("BB_COURSE_ID", values)

    def test_missing_env_file_is_not_an_error(self):
        self.assertEqual(secret_scan.secret_values(Path(os.devnull)), {})


class RepositoryTests(unittest.TestCase):
    def test_the_repository_is_clean(self):
        """The gate must pass on this tree, or it will be ignored."""
        findings, count = secret_scan.scan(PROJECT_ROOT)
        self.assertGreater(count, 20, "the scan should see the source tree")
        self.assertEqual(findings, [],
                         "secret scan findings: "
                         + "; ".join(f"{f['file']}:{f['line']} {f['kind']}"
                                     for f in findings[:10]))

    def test_gitignore_covers_the_sensitive_paths(self):
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (".env", ".state/", "output/", ".venv/"):
            self.assertIn(pattern, text)

    def test_env_is_not_tracked_by_git(self):
        """.gitignore is a claim; `git ls-files` is the proof."""
        if not (PROJECT_ROOT / ".git").is_dir():
            self.skipTest("not a git repository")
        import subprocess

        result = subprocess.run(["git", "ls-files"], cwd=str(PROJECT_ROOT),
                                capture_output=True, text=True, timeout=60,
                                encoding="utf-8", errors="replace")
        tracked = (result.stdout or "").splitlines()
        for dangerous in (".env", ".state/cookies.json", ".state/credentials.dat"):
            self.assertNotIn(dangerous, tracked,
                             f"{dangerous} must never be tracked")


if __name__ == "__main__":
    unittest.main()
