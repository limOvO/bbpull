"""Create a desktop shortcut for this installation.

Two very different things are called "a shortcut":

* on Linux/BSD, a freedesktop `.desktop` file dropped in the desktop folder -
  double-clickable once the desktop environment trusts it;
* on Windows, a `.lnk` created through `WScript.Shell`.

Both point at *this* installation: the frozen executable when packaged, or the
project launcher when running from source. Nothing is copied and nothing is
downloaded; the shortcut is a small text file (or link) that runs the app with
`gui`.

The desktop directory is discovered through `xdg-user-dir DESKTOP` where
available (localised desktops use e.g. `~/桌面`), then `~/Desktop`, then
`~/桌面`.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "bbpull"
APP_TITLE = "bbpull"
APP_TITLE_ZH = "bbpull 課程下載器"
APP_COMMENT = "Download Blackboard Ultra course content and announcements"

#: Fallback icon name when the application PNG has not been generated.
FALLBACK_ICON = "applications-education"


def project_root():
    return Path(__file__).resolve().parent.parent


def desktop_directory(environ=None):
    """The user's desktop folder. Never raises; falls back to ~/Desktop."""
    env = dict(os.environ if environ is None else environ)
    home = env.get("HOME") or os.path.expanduser("~")

    executable = shutil.which("xdg-user-dir", path=env.get("PATH"))
    if executable:
        try:
            result = subprocess.run([executable, "DESKTOP"], capture_output=True,
                                    text=True, timeout=5, env=env)
            path = (result.stdout or "").strip()
            # `xdg-user-dir` answers with $HOME when no desktop is configured.
            if result.returncode == 0 and path and path != home:
                return Path(path)
        except (OSError, subprocess.SubprocessError):
            pass

    for name in ("Desktop", "桌面"):
        candidate = Path(home) / name
        if candidate.is_dir():
            return candidate
    return Path(home) / "Desktop"


def launch_argv(frozen=None, executable=None, root=None):
    """The argv that opens the GUI for this installation."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return [str(executable or sys.executable), "gui"]
    root = Path(root or project_root())
    launcher = root / "bbpull.sh"
    venv_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt"
                                    else "bin/python")
    # The launcher is only meaningful when the project venv it selects exists
    # for *this* platform. A checkout shared between Windows and Linux can have
    # a `.venv/` that belongs to the other OS, in which case `bbpull.sh` would
    # fall through to a bare python without the dependencies.
    if (os.name != "nt" and launcher.is_file()
            and os.access(launcher, os.X_OK) and venv_python.is_file()):
        return [str(launcher), "gui"]
    return [sys.executable, "-m", "bbpull", "gui"]


def icon_reference(frozen=None, root=None):
    """An absolute PNG when available, else a themed icon name."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    base = Path(sys.executable).parent if frozen else Path(root or project_root())
    for candidate in (base / "bbpull.png", base / "installer" / "bbpull.png"):
        if candidate.is_file():
            return str(candidate)
    return FALLBACK_ICON


def quote_exec_arg(value):
    """Quote one Exec argument per the Desktop Entry spec.

    Runs of spaces would split the argument, and the spec reserves `"` `\\`
    `` ` `` `$` inside quoted arguments, so those are escaped.
    """
    text = str(value)
    if not text or any(char in text for char in ' \t"\\`$'):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def desktop_entry(argv=None, icon=None, name=APP_NAME):
    """The contents of a `.desktop` launcher."""
    argv = list(argv or launch_argv())
    exec_line = " ".join(quote_exec_arg(part) for part in argv)
    # `icon=None` means "find the app PNG"; an explicit empty string means the
    # caller wants the themed fallback.
    icon = icon_reference() if icon is None else (icon or FALLBACK_ICON)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        f"Name={APP_TITLE}\n"
        f"Name[zh_TW]={APP_TITLE_ZH}\n"
        "GenericName=Blackboard course downloader\n"
        f"Comment={APP_COMMENT}\n"
        f"Exec={exec_line}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Education;\n"
        f"StartupWMClass={name}\n"
    )


def _mark_trusted(path):
    """Best effort: tell the desktop environment the launcher is trusted.

    GNOME refuses to run an untrusted `.desktop` from the desktop. `gio set`
    records the trust flag on desktops that use it; where it does not apply the
    user gets the standard "Untrusted application launcher" prompt, and the
    error message printed by `bbpull shortcut` says so.
    """
    executable = shutil.which("gio")
    if not executable:
        return False
    for value in ("true", "yes"):
        try:
            result = subprocess.run(
                [executable, "set", str(path), "metadata::trusted", value],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def _ps_quote(value):
    """A single-quoted PowerShell string literal (embedded quotes doubled)."""
    return str(value).replace("'", "''")


def _create_windows_shortcut(name=APP_NAME):
    """Create a `.lnk` on the Windows desktop via WScript.Shell."""
    desktop = (Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
               / "Desktop")
    desktop.mkdir(parents=True, exist_ok=True)
    path = desktop / f"{name}.lnk"

    argv = launch_argv()
    target = argv[0]
    arguments = " ".join(argv[1:])
    if not getattr(sys, "frozen", False):
        # A console-less launch: pythonw.exe avoids a command window.
        pythonw = Path(target).with_name("pythonw.exe")
        if pythonw.is_file():
            target = str(pythonw)
    icon = icon_reference()
    icon_location = f"{icon},0" if icon != FALLBACK_ICON else ""

    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{_ps_quote(path)}'); "
        f"$s.TargetPath = '{_ps_quote(target)}'; "
        f"$s.Arguments = '{_ps_quote(arguments)}'; "
        f"$s.WorkingDirectory = '{_ps_quote(Path(target).parent)}'; "
        f"$s.IconLocation = '{_ps_quote(icon_location)}'; "
        "$s.Description = 'Blackboard Ultra course downloader'; "
        "$s.Save()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or not path.is_file():
        raise OSError(result.stderr.strip() or "could not create the shortcut")
    return {"path": str(path), "trusted": True}


def create_desktop_shortcut(directory=None, name=APP_NAME):
    """Create the shortcut and return {"path", "trusted"}.

    Raises OSError with an actionable message when the file cannot be written.
    """
    if os.name == "nt":
        return _create_windows_shortcut(name)

    target_dir = Path(directory) if directory else desktop_directory()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{name}.desktop"
        path.write_text(desktop_entry(name=name), encoding="utf-8", newline="\n")
        path.chmod(0o755)
    except OSError as exc:
        raise OSError(f"cannot write the shortcut to {target_dir}: {exc}") from exc

    return {"path": str(path), "trusted": _mark_trusted(path)}
