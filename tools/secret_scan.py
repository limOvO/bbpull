"""Secret-scan gate: refuse to commit credentials.

Run before every commit, and wired into `bbpull selftest` so it cannot be
forgotten. This repository keeps a plaintext `.env` (gitignored) and a
`.state/` cookie jar, so "it is in .gitignore" is not evidence - a scan must
confirm nothing sensitive is actually staged.

Calibration note: the first version of this gate reported 180 findings on a
clean tree - a 100% false-positive rate, which trains people to ignore it. It
was flagging `BB_OUT_DIR=output` (the literal word "output", present in every
docstring) and Python assignments such as `password: str = ""` and
`self.password = QLineEdit()`. The rules are now deliberately narrow:

* **token patterns** are exact shapes (`ghp_…`, `AKIA…`, PEM headers);
* **known values** are only taken from `.env` keys whose *name* marks them
  secret, so a URL, a course id or an output directory is never treated as one;
* **hardcoded credentials** must be a *quoted string literal* of real length.
  An identifier, a call, a subscript or an empty default is code, not a secret.

Exit code 0 = clean, 1 = findings. Findings never print the secret itself, only
its location and a short fingerprint, so the gate's output is safe to paste.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

#: Exact shapes that are a secret regardless of this project.
PATTERNS = (
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
)

#: A `.env` key is only treated as holding a secret when its *name* says so.
#: `BB_OUT_DIR` and `BB_BASE_URL` deliberately do not match.
SECRET_KEY = re.compile(
    r"(PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|JSESSIONID)",
    re.IGNORECASE,
)

#: A hardcoded credential must be a quoted literal, not code.
ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?P<key>
        BB_PASSWORD | PASSWORD | PASSWD | PASSPHRASE | SECRET |
        API_?KEY | ACCESS_?TOKEN | AUTH_?TOKEN | CLIENT_?SECRET | JSESSIONID
    )\b
    \s*[:=]\s*
    (?P<quote>["'])
    (?P<value>[^"'\n]{8,})
    (?P=quote)
    """
)

#: Values that are clearly placeholders rather than credentials.
PLACEHOLDER = re.compile(
    r"""(?ix)
    ^(
        your | xxx+ | todo | changeme | change_me | placeholder | example |
        dummy | test | sample | secret-here | password | username | none |
        null | empty | fake | redacted | \.\.\.+ | <.*> | \{\{.*\}\} |
        %\(.*\)s | \{.*\}
    )
    """,
)

MIN_SECRET_LENGTH = 8

#: Extensions worth scanning. Binaries are skipped: a credential should never
#: live in one, and scanning them is slow and noisy.
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".jsonl", ".cfg", ".ini", ".toml", ".yaml",
    ".yml", ".cmd", ".bat", ".ps1", ".sh", ".html", ".css", ".js", ".spec",
    ".example", ".env", ".gitignore", ".gitattributes", "",
}

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist",
             ".state", "output", "node_modules", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".exe", ".dll",
                 ".pyd", ".zip", ".whl", ".pyc", ".db", ".sqlite", ".pdf"}


def fingerprint(value):
    """A short, non-reversible tag for a secret, safe to print."""
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
    return f"sha256:{digest[:10]} len={len(value)}"


def looks_like_placeholder(value):
    return bool(PLACEHOLDER.match(value.strip()))


def secret_values(root):
    """Values from `.env` that must never appear elsewhere.

    Only keys whose name marks them secret, so `BB_OUT_DIR=output` is not
    hunted for across the tree.
    """
    values = {}
    env_file = Path(root) / ".env"
    if not env_file.is_file():
        return values
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not SECRET_KEY.search(key):
            continue
        if len(value) < MIN_SECRET_LENGTH or looks_like_placeholder(value):
            continue
        values[key] = value
    return values


def find_in_text(text, extra_values=None):
    """Findings in a block of text: list of (kind, line_number, detail)."""
    extra_values = extra_values or {}
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS:
            if pattern.search(line):
                findings.append((kind, number, ""))

        match = ASSIGNMENT.search(line)
        if match:
            value = match.group("value")
            if not looks_like_placeholder(value):
                findings.append((
                    "hardcoded-credential", number,
                    f"{match.group('key')}={value[:1]}*** {fingerprint(value)}"))

        for key, secret in extra_values.items():
            if secret and secret in line:
                findings.append((f"leaked {key}", number, fingerprint(secret)))
    return findings


def scan_file(path, extra_values=None):
    path = Path(path)
    if path.suffix.lower() in SKIP_SUFFIXES:
        return []
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in (
            ".env", ".gitignore", ".gitattributes"):
        return []
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    return find_in_text(text, extra_values)


def tracked_files(root):
    """Files git would track. Falls back to a walk when git is unavailable."""
    root = Path(root)
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(root), capture_output=True, timeout=60,
        )
        if proc.returncode == 0:
            names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0")
                     if n]
            return [root / name for name in names]
    except (OSError, subprocess.SubprocessError):
        pass

    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            found.append(Path(dirpath) / name)
    return found


def scan(root=None):
    """Scan everything git would track. Returns (findings, files_scanned)."""
    root = Path(root or Path(__file__).resolve().parent.parent)
    values = secret_values(root)
    findings = []
    files = []
    for path in tracked_files(root):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
        for kind, number, detail in scan_file(path, values):
            try:
                shown = path.relative_to(root)
            except ValueError:
                shown = path
            findings.append({"file": str(shown), "line": number,
                             "kind": kind, "detail": detail})
    return findings, len(files)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refuse to commit credentials.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    findings, count = scan(args.root)
    if not args.quiet:
        print(f"secret scan: {count} file(s) checked")

    if not findings:
        if not args.quiet:
            print("secret scan: clean")
        return 0

    print(f"secret scan: {len(findings)} finding(s) - do not commit")
    for item in findings:
        print(f"  {item['file']}:{item['line']}  {item['kind']}  {item['detail']}")
    print()
    print("If a finding is a genuine false positive, narrow the rule in")
    print("tools/secret_scan.py rather than bypassing the gate.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
