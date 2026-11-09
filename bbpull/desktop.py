"""Platform glue: opening a folder in the desktop's file manager, and showing a
message to someone who has no console.

Both jobs differ per platform and both used to be written inline as
``if os.name == "nt": os.startfile(...) else: webbrowser.open(file://...)``.
That fallback is wrong on Linux in a way users notice immediately: a
``file://`` URL opens a *browser* showing a directory listing instead of the
file manager the button promises. ``xdg-open`` is the desktop's own "open this"
verb, so that is what Linux gets.

``show_message`` matters for a launcher (.desktop / menu entry) with no terminal:
the only way a startup failure can reach the user is a toolkit dialog, and the
program must be able to show one without importing Qt or Tk - the situation
where those are broken is exactly when this is needed.
"""

import os
import shutil
import subprocess
import sys

#: Win32 MessageBox flags, kept here so no `ctypes.wintypes` import is needed.
_MB_ICONERROR = 0x10
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000


def _spawn(argv):
    """Start a detached helper process. Returns True if it was started."""
    try:
        subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, ValueError):
        return False


def open_in_file_manager(path):
    """Open `path` (a file or directory) with the platform's own opener.

    Returns True when an opener was started. Starting `xdg-open` is not proof
    that a file manager appeared - it exits non-zero in some sessions - so
    callers should report "已開啟" as a request, not a guarantee.
    """
    if not path:
        return False
    path = os.path.abspath(str(path))
    if not os.path.exists(path):
        return False

    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if startfile is None:  # pragma: no cover - only on non-Windows builds
            return False
        try:
            startfile(path)  # noqa: S606 - user-initiated, their own path
            return True
        except OSError:
            return False

    if sys.platform == "darwin":
        return _spawn(["open", path])

    # Linux/BSD: the freedesktop opener first, then the GLib equivalent.
    for name, prefix in (("xdg-open", ()), ("kde-open5", ()),
                         ("gnome-open", ()), ("gio", ("open",))):
        executable = shutil.which(name)
        if executable:
            if _spawn([executable, *prefix, path]):
                return True
    return False


def show_message(title, text, error=True):
    """Show a native message box without importing a GUI toolkit.

    Returns True when a dialog was actually shown. Windows uses the Win32 API
    directly; macOS and Linux use the desktop's standard dialog helper when it
    is installed. A missing helper is not an error - the caller has already
    written the same text to a log file.
    """
    title = str(title or "bbpull")
    text = str(text or "")
    if os.name == "nt":
        try:
            import ctypes

            flags = (_MB_ICONERROR if error else _MB_ICONINFORMATION) | _MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(None, text, title, flags)
            return True
        except Exception:  # noqa: BLE001 - reporting must never raise
            return False

    if sys.platform == "darwin":
        script = (
            'display dialog {} with title {} buttons {{"OK"}} default button "OK"'
            .format(_applescript_string(text), _applescript_string(title))
        )
        return _spawn(["osascript", "-e", script])

    for name, args in (("zenity", ["--error" if error else "--info",
                                    "--title", title, "--text", text,
                                    "--no-wrap"]),
                       ("kdialog", ["--title", title,
                                    "--error" if error else "--msgbox", text])):
        executable = shutil.which(name)
        if executable and _spawn([executable, *args]):
            return True
    return False


def _applescript_string(text):
    """Quote a Python string as an AppleScript string literal."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("\n", "\\n") + '"'
