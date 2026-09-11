"""Allow `python -m bbpull`, and make the packaged entry points behave.

Two things live here that only matter for the built executables:

**The windowed build must open the window.** With no arguments the CLI opens the
interactive text menu, which is right for a console but fatal for a GUI-subsystem
binary: there is no console to draw in and no stdin to read, so it exits
immediately with a config error. Double-clicking `bbpull-gui.exe` therefore
flashed and vanished. A windowed build with no arguments now opens the GUI.

**A windowed build must be able to explain itself.** Its `sys.stdout` and
`sys.stderr` are `None`, so every diagnostic the program prints is discarded and
any failure looks like a silent crash. Output is captured instead and, when the
run fails, written to a log file and shown in a message box.
"""

import io
import os
import sys
import traceback

from .cli import main
from .venv_tools import REEXEC_GUARD, maybe_reexec

#: Message box styles (avoid a ctypes.wintypes import just for these).
_MB_ICONERROR = 0x10
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000

ERROR_LOG_NAME = "bbpull-error.log"


def is_windowed_build():
    """True for the GUI-subsystem executable.

    PyInstaller's ``--windowed`` bootloader leaves both streams as ``None``
    because there is no console to attach them to. That is the only reliable
    signal available at runtime, and it is exactly the condition that makes the
    interactive menu unusable.
    """
    return sys.stdout is None and sys.stderr is None


def default_argv(argv, windowed=None):
    """Arguments to run with, defaulting a bare double-click to the GUI.

    `windowed` must be passed in when the caller has already replaced the
    streams: `run()` swaps them for a capture buffer before deciding, so
    re-detecting here would see non-None streams and wrongly conclude this is a
    console build - sending a double-click back to the unusable text menu.
    """
    argv = list(argv)
    if windowed is None:
        windowed = is_windowed_build()
    if not argv and windowed:
        return ["gui"]
    return argv


class _Capture(io.TextIOBase):
    """An in-memory stand-in for the missing console streams.

    Reports a UTF-8 ``encoding`` so the encoding-resilient logging helpers treat
    it like a real stream instead of falling back to ASCII and mangling Chinese.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self):
        super().__init__()
        self._parts = []

    def write(self, text):
        self._parts.append(str(text))
        return len(text)

    def flush(self):
        pass

    def writable(self):
        return True

    def getvalue(self):
        return "".join(self._parts)


def error_log_path():
    """Where a windowed failure is recorded, next to the saved credentials."""
    try:
        from .config import user_data_dir

        base = user_data_dir()
    except Exception:  # noqa: BLE001 - logging must not be able to fail
        base = os.path.join(os.path.expanduser("~"), ".bbpull")
    return os.path.join(base, ERROR_LOG_NAME)


def trace(message):
    """Append a diagnostic line when BBPULL_TRACE is set.

    A GUI-subsystem process has nowhere to print, which makes a packaged build
    nearly opaque when something goes wrong before a window exists. This is the
    escape hatch for that.
    """
    if not os.environ.get("BBPULL_TRACE"):
        return
    try:
        path = os.path.join(os.path.dirname(error_log_path()), "bbpull-trace.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def write_error_log(text):
    """Persist a failure report. Returns the path, or None."""
    path = error_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path
    except OSError:
        return None


def show_message(title, text, error=True):
    """Show a native message box. Uses Win32 directly so it works even if Qt,
    the toolkits and the console are all unavailable - which is precisely the
    situation where the user needs to be told something."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        flags = (_MB_ICONERROR if error else _MB_ICONINFORMATION) | _MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
        return True
    except Exception:  # noqa: BLE001
        return False


def report_failure(output, code):
    """Tell the user what went wrong, in a way they can act on."""
    path = write_error_log(output)
    lines = [
        f"bbpull 無法啟動（結束代碼 {code}）。",
        "",
    ]
    tail = [line for line in (output or "").splitlines() if line.strip()][-12:]
    if tail:
        lines.append("執行訊息：")
        lines.extend(tail)
        lines.append("")
    if path:
        lines.append(f"完整記錄：{path}")
    lines.append("")
    lines.append("如果問題持續，請執行 bbpull.exe selftest 並回報結果。")
    show_message("bbpull", "\n".join(lines), error=True)


def run():
    """Entry point: hand off to the project venv if one exists, then run."""
    # Detect first, then replace: once the streams are a capture buffer,
    # `is_windowed_build()` can no longer tell this is a GUI-subsystem binary.
    windowed = is_windowed_build()
    capture = None
    if windowed:
        # Streams are None; capture instead of discarding, so a failure can be
        # reported rather than looking like a crash.
        capture = _Capture()
        sys.stdout = capture
        sys.stderr = capture

    # A bare double-click is the case where a silent exit is unacceptable: the
    # user asked for a window and got nothing.
    opened_by_double_click = windowed and not sys.argv[1:]
    argv = default_argv(sys.argv[1:], windowed=windowed)
    trace(f"run: windowed={windowed} raw_argv={sys.argv[1:]!r} argv={argv!r} "
          f"exe={sys.executable!r}")

    try:
        # The guard makes the hand-off happen at most once, so a venv that
        # somehow reports the wrong prefix cannot cause an endless loop of
        # processes.
        if not os.environ.get(REEXEC_GUARD):
            code = maybe_reexec(argv=argv, log=lambda message: print(message))
            if code is not None:
                return code

        code = main(argv)
        # `main` reports failures by *returning* a code, not by raising, so an
        # exception-only handler missed exactly the case that mattered.
        if opened_by_double_click and code not in (0, None):
            report_failure(capture.getvalue() if capture else "", code)
        return code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
        if code not in (0, None) and (windowed or opened_by_double_click):
            report_failure(capture.getvalue() if capture else "", code)
        return code
    except BaseException:  # noqa: BLE001 - a silent exit is the worst outcome
        text = traceback.format_exc()
        if capture:
            capture.write(text)
        if windowed:
            report_failure(capture.getvalue() if capture else text, 1)
        else:
            sys.stderr.write(text)
        return 1


if __name__ == "__main__":
    sys.exit(run())
