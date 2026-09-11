"""Project virtual environment management.

Why this exists: PySide6 was installed into a *global* interpreter's user
site-packages, which is invisible to every other Python on the machine. The
user's `python` was anaconda base, so the GUI silently fell back to Tk and a
whole debugging round described the wrong interface.

A project-local venv removes the ambiguity entirely: one interpreter, known
dependencies, reproducible. Every other interpreter on the machine becomes
irrelevant.

Pure logic (paths, package sets, result shapes) is separated from subprocess
calls so it can be tested without creating anything.
"""

import os
import subprocess
import sys
from pathlib import Path

#: Directory name of the project environment. Already in `.gitignore`.
VENV_DIRNAME = ".venv"

#: Everything the desktop app and CLI need, in install order.
#: PySide6-Essentials rather than PySide6: QtWidgets is a core module, and the
#: Addons wheel adds ~500 MB of WebEngine/3D this app never touches.
REQUIRED_PACKAGES = (
    "requests>=2.31.0",
    "PySide6-Essentials>=6.6",
    "customtkinter>=5.2.0",
)

#: Shown when a venv is missing; one command, copy-pasteable.
CREATE_HINT = "python -m bbpull venv --create"

#: Set in the child's environment before re-exec so it cannot recurse.
REEXEC_GUARD = "BBPULL_REEXEC"

#: Escape hatch for anyone who deliberately wants the current interpreter.
REEXEC_DISABLE = "BBPULL_NO_REEXEC"

_PROBE_TIMEOUT = 600
_INSTALL_TIMEOUT = 1800


def project_root():
    """The repository root, from this file's location."""
    return Path(__file__).resolve().parent.parent


def venv_dir(root=None):
    return Path(root or project_root()) / VENV_DIRNAME


def venv_python(root=None):
    """Path to the interpreter inside the project venv.

    Returned whether or not it exists: callers check `exists()`. The layout
    differs by platform (`Scripts/` on Windows, `bin/` elsewhere).
    """
    base = venv_dir(root)
    if os.name == "nt":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def venv_exists(root=None):
    return venv_python(root).is_file()


def is_running_in_project_venv(root=None):
    """True when the current interpreter *is* the project venv's.

    Compared through `sys.prefix` (what a venv actually changes) rather than
    `sys.executable`, because `python.exe` is also reachable through symlinks and
    different casings on Windows.
    """
    base = venv_dir(root)
    if not base.is_dir():
        return False
    try:
        prefix = Path(sys.prefix).resolve()
        return prefix == base.resolve()
    except OSError:
        return False


def interpret(prefix):
    """Read the version string out of a venv's `pyvenv.cfg`."""
    cfg = Path(prefix) / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    info = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        info[key.strip().lower()] = value.strip()
    return info


def describe(root=None):
    """Everything worth reporting about the environment, without mutating it."""
    root = Path(root or project_root())
    python = venv_python(root)
    base = venv_dir(root)
    info = interpret(base) if base.is_dir() else None

    return {
        "project_root": str(root),
        "venv_dir": str(base),
        "venv_python": str(python),
        "exists": python.is_file(),
        "is_current": is_running_in_project_venv(root),
        "frozen": is_frozen(),
        "current_python": sys.executable,
        "base_python": (info or {}).get("home") or (info or {}).get("executable"),
        "version": (info or {}).get("version"),
        "packages": list(REQUIRED_PACKAGES),
        "hint": CREATE_HINT,
    }


def create_command(base_python=None, root=None):
    """The `venv` creation argv, as a list (no shell involved)."""
    return [str(base_python or sys.executable), "-m", "venv", str(venv_dir(root))]


def install_command(python, packages=None):
    """The pip install argv for a venv interpreter."""
    return [str(python), "-m", "pip", "install", "--upgrade",
            *list(packages or REQUIRED_PACKAGES)]


#: Characters a shell would interpret inside an unquoted argument. The `>=`
#: in a version specifier is the dangerous one: displayed unquoted,
#: `pip install requests>=2.31.0` is read as a redirect and pip receives
#: `requests`, installing the wrong thing - or erroring out.
_SHELL_META = set(' \t<>|&^();"\'!%$*?[]{}~`')


def shell_command(argv, cwd=None):
    """A copy-pasteable equivalent of an argv, quoted for the current shell.

    Args are only quoted when they need it, so ordinary commands stay readable.
    Quoting matters for correctness here, not cosmetics: package specifiers
    contain `>=`, and an unquoted `>` redirects instead of installing.
    """
    parts = []
    for index, item in enumerate(argv):
        text = str(item)
        needs_quotes = index > 0 and any(char in _SHELL_META for char in text)
        parts.append(f'"{text}"' if needs_quotes else text)
    text = " ".join(parts)
    return f"cd {cwd} && {text}" if cwd else text


def _run(argv, timeout, log=None):
    """Run a subprocess, streaming nothing; returns (ok, tail_output)."""
    if log:
        log(f"$ {shell_command(argv)}")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        return False, f"找不到執行檔：{exc}"
    except subprocess.TimeoutExpired:
        return False, f"逾時（超過 {timeout} 秒）"
    except OSError as exc:
        return False, f"無法執行：{exc}"

    combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        tail = "\n".join(combined.splitlines()[-12:])
        return False, tail or f"退出碼 {proc.returncode}"
    return True, combined


def create(root=None, base_python=None, packages=None, log=None, install=True):
    """Create the project venv and install dependencies.

    Returns a dict: `{"ok", "created", "installed", "python", "error", "command"}`.
    Never raises - the caller reports the failure.
    """
    root = Path(root or project_root())
    python = venv_python(root)
    result = {"ok": False, "created": False, "installed": False,
              "python": str(python), "error": None,
              "command": shell_command(create_command(base_python, root))}

    if python.is_file():
        result["created"] = True
    else:
        if log:
            log(f"建立虛擬環境：{venv_dir(root)}")
        ok, output = _run(create_command(base_python, root), _PROBE_TIMEOUT, log)
        if not ok:
            result["error"] = output
            return result
        result["created"] = python.is_file()
        if not result["created"]:
            result["error"] = f"建立完成但找不到 {python}"
            return result

    if not install:
        result["ok"] = True
        return result

    if log:
        log("安裝相依套件（首次約需下載 80 MB，請稍候）…")
    ok, output = _run(install_command(python, packages), _INSTALL_TIMEOUT, log)
    if not ok:
        result["error"] = output
        return result
    result["installed"] = True
    result["ok"] = True
    return result


def is_frozen():
    """True inside a PyInstaller bundle.

    Everything is already bundled, so the venv machinery does not apply: there
    is no project checkout to hold a `.venv`, and re-execing into one would be
    both pointless and broken on a user's machine.
    """
    return bool(getattr(sys, "frozen", False))


def should_reexec(root=None, environ=None):
    """Whether to hand off to the project venv. Pure decision, no side effects.

    This is what makes the environment deterministic no matter which `python`
    the user invokes: as soon as a project venv exists, it is used. Without it,
    `python -m bbpull` from anaconda base and from Python 3.13 would take
    different code paths and open different interfaces.
    """
    env = os.environ if environ is None else environ
    if is_frozen():
        return False, "已打包成獨立執行檔，套件已內含"
    if env.get(REEXEC_GUARD) == "1":
        return False, "已在此環境中"
    if env.get(REEXEC_DISABLE):
        return False, f"{REEXEC_DISABLE} 已設定"
    if is_running_in_project_venv(root):
        return False, "目前就是專案環境"
    python = venv_python(root)
    if not python.is_file():
        return False, "沒有專案環境"
    return True, f"找到專案環境 {venv_dir(root)}"


def reexec_argv(argv=None, root=None):
    """The argv that re-runs the same command inside the project venv."""
    python = venv_python(root)
    forwarded = list(sys.argv[1:] if argv is None else argv)
    return [str(python), "-m", "bbpull", *forwarded]


def maybe_reexec(argv=None, root=None, log=None, environ=None):
    """Re-run inside the project venv when one exists.

    Returns the child's exit code, or None when no hand-off happened (so the
    caller continues normally). Never raises: if the hand-off cannot start, the
    caller keeps running with the current interpreter.
    """
    env = os.environ if environ is None else environ
    wanted, reason = should_reexec(root, env)
    if not wanted:
        return None

    command = reexec_argv(argv, root)
    if log:
        log(f"改用專案虛擬環境執行（{reason}）")
    child_env = dict(env)
    child_env[REEXEC_GUARD] = "1"
    try:
        return subprocess.call(command, env=child_env)
    except OSError as exc:
        if log:
            log(f"無法改用專案環境（{exc}），以目前的 Python 繼續。")
        return None


def verify(root=None):
    """Check that the venv actually has what the app needs.

    Installing can succeed while an import still fails (a broken wheel, a
    mismatched ABI), so this asks the venv python directly rather than trusting
    pip's exit code.
    """
    root = Path(root or project_root())
    python = venv_python(root)
    if not python.is_file():
        return {"ok": False, "error": "虛擬環境不存在", "modules": {}}

    code = (
        "import json, sys\n"
        "out = {'python': sys.executable, 'version': sys.version.split()[0]}\n"
        "for name in ('requests', 'PySide6', 'customtkinter', 'tkinter'):\n"
        "    try:\n"
        "        module = __import__(name)\n"
        "        out[name] = getattr(module, '__version__', 'ok')\n"
        "    except Exception as exc:\n"
        "        out[name] = None\n"
        "print(json.dumps(out))\n"
    )
    ok, output = _run([str(python), "-c", code], 120)
    if not ok:
        return {"ok": False, "error": output, "modules": {}}

    import json

    try:
        payload = json.loads(output.splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": f"無法解析輸出：{output[:200]}", "modules": {}}

    modules = {key: payload.get(key) for key in
               ("requests", "PySide6", "customtkinter", "tkinter")}
    return {
        "ok": bool(modules.get("requests")) and bool(modules.get("PySide6")),
        "python": payload.get("python"),
        "version": payload.get("version"),
        "modules": modules,
        "error": None,
    }


def report(root=None, log=None):
    """Human-readable environment status, for `bbpull venv`."""
    info = describe(root)

    if info["frozen"]:
        return "\n".join([
            "獨立執行檔模式",
            f"  執行檔       : {info['current_python']}",
            "  相依套件     : 已內含在執行檔中，不需要（也不能）建立虛擬環境",
            f"  Python       : {sys.version.split()[0]}",
            "",
            "  若你是從原始碼執行，請改用原始碼版本的 bbpull。",
        ])

    lines = [
        "虛擬環境檢查",
        f"  專案位置     : {info['project_root']}",
        f"  環境目錄     : {info['venv_dir']}",
    ]
    if info["exists"]:
        lines.append(f"  狀態         : 已建立（Python {info['version'] or '?'}）")
        if info["is_current"]:
            lines.append("  目前執行中   : 就是這個環境")
        else:
            lines.append(f"  目前執行中   : {info['current_python']}")
            lines.append("                 （不是這個環境；bbpull.cmd 會自動改用）")
        check = verify(root)
        lines.append("")
        lines.append("  套件：")
        for name, version in (check.get("modules") or {}).items():
            mark = version if version else "（缺少）"
            lines.append(f"    {name:14} {mark}")
        if not check.get("ok"):
            lines.append("")
            lines.append(f"  環境不完整。修復：python -m bbpull venv --create --force")
    else:
        lines.append("  狀態         : 尚未建立")
        lines.append("")
        lines.append("  目前這個 Python：")
        lines.append(f"    {info['current_python']}")
        lines.append("")
        lines.append("  建立專案環境（會下載 PySide6，約 80 MB）：")
        lines.append(f"    {info['hint']}")
        lines.append("")
        lines.append("  建立之後，用 bbpull.cmd 或 .venv\\Scripts\\python -m bbpull 執行，")
        lines.append("  就會固定使用這個環境，不受其他 Python 影響。")
    return "\n".join(lines)
