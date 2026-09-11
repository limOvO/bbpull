"""Decide which GUI engine to run, and never fall back silently.

Why this module exists: PySide6 was installed into one Python interpreter, but
this machine has four (3.10-3.13) and `python` resolves to whichever PATH finds
first. On an interpreter without PySide6 the launcher caught `ImportError` and
quietly opened the Tk fallback - the user believed they were looking at the Qt
build while reporting bugs against the other one. A silent downgrade is worse
than a hard failure: it makes every later observation untrustworthy.

So the rules are:

* the chosen engine is always reported, and *why*;
* `require_qt` makes a missing Qt an error instead of a downgrade;
* the install hint names the interpreter that actually needs it, because
  `pip install PySide6` with a different `python` is exactly what caused this.
"""

import os
import shutil
import subprocess
import sys

ENGINE_QT = "qt"
ENGINE_TK = "tk"

QT_PACKAGE = "PySide6-Essentials"
TK_PACKAGE = "customtkinter"

#: Interpreter discovery is best-effort and must never block startup.
_PROBE_TIMEOUT = 20
_PROBE_CODE = (
    "import sys\n"
    "sys.stdout.write(sys.executable)\n"
    "try:\n"
    "    import PySide6\n"
    "    sys.stdout.write('|qt=' + PySide6.__version__)\n"
    "except Exception:\n"
    "    sys.stdout.write('|qt=')\n"
    "try:\n"
    "    import customtkinter\n"
    "    sys.stdout.write('|tk=' + customtkinter.__version__)\n"
    "except Exception:\n"
    "    sys.stdout.write('|tk=')\n"
    "try:\n"
    "    import tkinter\n"
    "    sys.stdout.write('|tkinter=1')\n"
    "except Exception:\n"
    "    sys.stdout.write('|tkinter=')\n"
)


def pip_command(python_exe, package):
    """The exact command for `python_exe`, quoted for the shell in use."""
    exe = python_exe or "python"
    if os.name == "nt" and " " in exe:
        exe = f'"{exe}"'
    return f"{exe} -m pip install {package}"


def probe_interpreter(python_exe):
    """Import capability of one interpreter. Never raises."""
    if not python_exe or not os.path.isfile(python_exe):
        return None
    try:
        proc = subprocess.run([python_exe, "-c", _PROBE_CODE],
                              capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = (proc.stdout or "").split("|")
    exe = parts[0].strip()
    info = {"exe": exe, "qt": None, "tk": None, "tkinter": False}
    for chunk in parts[1:]:
        key, _, value = chunk.partition("=")
        if key == "qt":
            info["qt"] = value or None
        elif key == "tk":
            info["tk"] = value or None
        elif key == "tkinter":
            info["tkinter"] = bool(value)
    return info


def candidate_interpreters():
    """Every interpreter we can reasonably find, best-effort and de-duplicated.

    De-duplication is case-insensitive: on Windows `python.exe` and `python.EXE`
    are the same file, and the `py` launcher reports a different casing than
    `sys.executable`, so both turned up and were listed twice.
    """
    found = []
    seen = set()

    def add(path):
        if not path:
            return
        path = os.path.abspath(path)
        key = os.path.normcase(path)
        if os.path.isfile(path) and key not in seen:
            seen.add(key)
            found.append(path)

    add(sys.executable)

    # The py launcher knows about every registered install.
    launcher = shutil.which("py")
    if launcher:
        try:
            proc = subprocess.run([launcher, "-0p"], capture_output=True, text=True,
                                  timeout=_PROBE_TIMEOUT, encoding="utf-8",
                                  errors="replace")
            for line in (proc.stdout or "").splitlines():
                # Form: " -V:3.13 *        C:\Python313\python.exe"
                marker = line.find(":\\")
                if marker > 0:
                    add(line[marker - 1:].strip())
        except (OSError, subprocess.SubprocessError):
            pass

    for name in ("python", "python3"):
        add(shutil.which(name))

    return found


def scan_interpreters(paths=None):
    """Probe a list of interpreters, keeping the ones that can run a GUI."""
    results = []
    for path in (paths if paths is not None else candidate_interpreters()):
        info = probe_interpreter(path)
        if info:
            results.append(info)
    return results


def current_interpreter():
    """Capability of the interpreter running right now, without a subprocess."""
    info = {"exe": sys.executable, "qt": None, "tk": None, "tkinter": False}
    try:
        import PySide6  # noqa: F401

        info["qt"] = getattr(PySide6, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        pass
    try:
        import customtkinter  # noqa: F401

        info["tk"] = getattr(customtkinter, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        pass
    try:
        import tkinter  # noqa: F401

        info["tkinter"] = True
    except Exception:  # noqa: BLE001
        pass
    return info


def decide(force_tk=False, require_qt=False, info=None):
    """Pick an engine and explain the choice.

    Returns `{"engine", "reason", "hint", "warnings", "info"}`. `hint` is a
    ready-to-run fix when the preferred engine is unavailable.
    """
    info = info or current_interpreter()
    warnings = []

    if force_tk:
        if not info["tkinter"]:
            return {
                "engine": None,
                "reason": "--tk was requested but this interpreter has no tkinter",
                "hint": "安裝一個含 tkinter 的 Python，或改用 Qt（移除 --tk）。",
                "warnings": warnings,
                "info": info,
            }
        return {
            "engine": ENGINE_TK,
            "reason": "--tk was requested",
            "hint": None,
            "warnings": warnings,
            "info": info,
        }

    if info["qt"]:
        return {
            "engine": ENGINE_QT,
            "reason": f"PySide6 {info['qt']} is available in this interpreter",
            "hint": None,
            "warnings": warnings,
            "info": info,
        }

    hint = (f"在這個 Python 安裝 Qt 介面：\n"
            f"    {pip_command(info['exe'], QT_PACKAGE)}")
    if require_qt:
        return {
            "engine": None,
            "reason": ("--qt was requested but PySide6 is not installed in "
                       f"{info['exe']}"),
            "hint": hint,
            "warnings": warnings,
            "info": info,
        }

    if not info["tkinter"]:
        return {
            "engine": None,
            "reason": ("neither Qt nor Tk is usable in this interpreter, so the "
                       "desktop app cannot open"),
            "hint": hint,
            "warnings": warnings,
            "info": info,
        }

    # Not silent: the caller is expected to surface this to the user.
    warnings.append(
        f"PySide6 不在這個 Python（{info['exe']}）裡，改用內建 Tk 介面。\n"
        f"    想用較流暢的 Qt 介面：{pip_command(info['exe'], QT_PACKAGE)}")
    return {
        "engine": ENGINE_TK,
        "reason": "PySide6 is unavailable, so the built-in Tk interface is used",
        "hint": hint,
        "warnings": warnings,
        "info": info,
    }


def report(scan=False, paths=None):
    """A human-readable engine report, for `bbpull gui --check`."""
    info = current_interpreter()
    lines = [
        "GUI 引擎檢查",
        f"  執行中的 Python : {info['exe']}",
        f"  PySide6        : {info['qt'] or '（未安裝）'}",
        f"  customtkinter  : {info['tk'] or '（未安裝）'}",
        f"  tkinter        : {'有' if info['tkinter'] else '（沒有）'}",
    ]

    choice = decide(info=info)
    lines.append("")
    if choice["engine"] == ENGINE_QT:
        lines.append("  預設會使用 : Qt（PySide6）")
    elif choice["engine"] == ENGINE_TK:
        lines.append("  預設會使用 : Tk（降級）")
    else:
        lines.append("  預設會使用 : 無法開啟圖形介面")
    lines.append(f"  原因       : {choice['reason']}")
    for warning in choice["warnings"]:
        lines.append("")
        lines.append(f"  注意：{warning}")

    if scan:
        lines.append("")
        lines.append("  這台機器上的 Python：")
        others = scan_interpreters(paths)
        for other in others:
            marker = " ←使用中" if _same_path(other["exe"], info["exe"]) else ""
            qt = f"PySide6 {other['qt']}" if other["qt"] else "沒有 PySide6"
            lines.append(f"    {other['exe']}  [{qt}]{marker}")
        usable = [o for o in others
                  if o["qt"] and not _same_path(o["exe"], info["exe"])]
        if usable:
            lines.append("")
            lines.append("  這些 Python 已經有 PySide6，可以直接用它們啟動：")
            for other in usable:
                lines.append(f"    {other['exe']} -m bbpull gui")
    return "\n".join(lines)


def _same_path(left, right):
    """Windows path comparison (case- and separator-insensitive)."""
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right))
