"""Build the distributable executables.

Wraps PyInstaller so the build is one reproducible command, and verifies the
result instead of trusting a zero exit code. A build that succeeds but produces
an executable missing a dynamically imported module is the worst release
failure: it looks fine until someone tries the one feature that was dropped.

Steps:
  1. regenerate the icon from the app's own palette;
  2. run PyInstaller against `bbpull.spec`;
  3. smoke-test both executables (`--version`, `selftest`, engine check);
  4. zip the folder for release.

Usage:
    python tools/build_exe.py            # build + verify + zip
    python tools/build_exe.py --no-zip   # build + verify only
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_DIR = DIST / "bbpull"
SPEC = ROOT / "bbpull.spec"
ICON = ROOT / "installer" / "bbpull.ico"

#: Checks run against the built executables. Each is (label, argv, expected).
SMOKE_TESTS = (
    ("console --help", ["--help"], 0),
    ("console selftest", ["selftest"], 0),
    ("console gui --check", ["gui", "--check"], 0),
    ("console venv", ["venv"], 0),
)

MIN_EXPECTED_MB = 40
MAX_EXPECTED_MB = 900


def run(argv, timeout=1800, cwd=None, capture=True):
    display = " ".join(str(a) for a in argv)
    print(f"  $ {display}")
    return subprocess.run([str(a) for a in argv], cwd=str(cwd or ROOT),
                          capture_output=capture, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def make_icon():
    print("[1/4] icon")
    result = run([sys.executable, str(ROOT / "installer" / "make_icon.py"),
                  str(ICON)])
    if result.returncode != 0:
        print(result.stdout or "", result.stderr or "")
        raise SystemExit("icon generation failed")
    print(f"  {ICON.name}: {ICON.stat().st_size} bytes")


def kill_leftovers():
    """Kill any running bbpull executable before touching dist/.

    Windows refuses to delete a running .exe, so a leftover process turns a
    rebuild into `PermissionError: [WinError 5]` mid-COLLECT. A previous smoke
    test can leave one behind, especially the windowed build, which shows a modal
    dialog on failure and therefore never exits on its own.
    """
    for name in ("bbpull.exe", "bbpull-gui.exe"):
        result = subprocess.run(["taskkill", "/F", "/IM", name],
                                capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  stopped a running {name}")


def build():
    print("[2/4] pyinstaller")
    kill_leftovers()
    for folder in (DIST, BUILD):
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
    if DIST.exists() or BUILD.exists():
        raise SystemExit(
            "could not clear dist/ or build/ - a bbpull process may still be "
            "running; close it and try again")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--log-level", "WARN", str(SPEC)],
        cwd=str(ROOT), env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=3600,
    )
    if result.returncode != 0:
        print(result.stdout or "")
        print(result.stderr or "")
        raise SystemExit("PyInstaller failed")
    warnings = [line for line in (result.stderr or "").splitlines()
                if "WARNING" in line or "ERROR" in line]
    for line in warnings[:15]:
        print(f"  {line}")
    if not (APP_DIR / "bbpull.exe").is_file():
        raise SystemExit(f"bbpull.exe was not produced in {APP_DIR}")


def _process_image(pid):
    """Full executable path of a process, or "" if it cannot be read."""
    import ctypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(2048)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                               ctypes.byref(size)):
            return buffer.value
        return ""
    except OSError:
        return ""
    finally:
        kernel32.CloseHandle(handle)


def windows_for_executable(exe_path):
    """Visible window titles owned by a process running `exe_path`.

    Matching on the title alone is not enough: a File Explorer window showing the
    folder `dist/bbpull` is titled "bbpull - File Explorer" and matched a naive
    substring check, so the smoke test passed while looking at the wrong window
    entirely. The owning process's image path removes that ambiguity.

    The process is not necessarily the one `Popen` returned: PyInstaller's
    bootloader runs the application in a child, so every window is checked.
    """
    import ctypes

    user32 = ctypes.windll.user32
    target = os.path.normcase(os.path.abspath(exe_path))
    titles = []
    seen = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        owner = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        pid = owner.value
        if pid not in seen:
            seen[pid] = os.path.normcase(_process_image(pid))
        if seen[pid] != target:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value:
            titles.append(buffer.value)
        return True

    user32.EnumWindows(callback, 0)
    return titles


def kill_tree(image_name):
    """Kill an image and its children (the GUI lives in a child process)."""
    for args in (["taskkill", "/F", "/T", "/IM", image_name],
                 ["taskkill", "/F", "/IM", image_name]):
        subprocess.run(args, capture_output=True, text=True)


def verify_windowed_gui(gui, failures):
    """The windowed binary must open a window **when double-clicked**.

    Launched through `os.startfile`, which is the shell "open" verb - literally
    what a double-click does. That fidelity matters: the earlier version used
    `subprocess.Popen(..., stdout=DEVNULL, stderr=DEVNULL)`, and supplying those
    handles changes the program's behaviour. A GUI-subsystem process normally has
    `sys.stdout is None`; redirected to DEVNULL it has a valid (useless) stream,
    so it no longer identifies itself as windowed, falls through to the
    interactive text menu, and exits with code 2. The test was causing the
    failure it reported, and reported it against a working build.

    "The process is still alive" is not evidence either: a failed import makes
    PyInstaller show a modal error dialog, which also keeps it running. Only a
    window owned by this executable counts.
    """
    if os.name != "nt":
        print("  skip windowed gui check (os.startfile is Windows-only)")
        return

    kill_tree(gui.name)
    time.sleep(1.0)
    try:
        os.startfile(str(gui))  # noqa: S606 - the point is to mimic a double-click
    except OSError as exc:
        print(f"  FAIL windowed gui could not be started: {exc}")
        failures.append("windowed gui")
        return

    title = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        found = windows_for_executable(gui)
        if found:
            title = found[0]
            break
        time.sleep(0.5)

    running = bool(subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {gui.name}"],
                                  capture_output=True, text=True).stdout.find(
                                      gui.name) >= 0)
    kill_tree(gui.name)

    if title:
        print(f"  ok   windowed gui opened a window on double-click: {title!r}")
    elif not running:
        print("  FAIL windowed gui (double-click, no args) exited without "
              "opening a window")
        failures.append("windowed gui")
    else:
        print("  FAIL windowed gui is running but opened no window in 60s")
        failures.append("windowed gui")


def smoke_test():
    """Run the executables. A zero exit code from PyInstaller proves nothing.

    The build can succeed while the executable is missing a dynamically imported
    module, which is the worst release failure: it looks fine until someone
    tries the one feature that was dropped.
    """
    print("[3/4] smoke test")
    console = APP_DIR / "bbpull.exe"
    gui = APP_DIR / "bbpull-gui.exe"
    failures = []

    for label, argv, expected in SMOKE_TESTS:
        try:
            result = run([console, *argv], timeout=600)
        except subprocess.TimeoutExpired:
            failures.append(f"{label}: timed out")
            print(f"  FAIL {label}: timed out")
            continue
        ok = result.returncode == expected
        print(f"  {'ok  ' if ok else 'FAIL'} {label} (exit {result.returncode})")
        if not ok:
            failures.append(label)
            tail = ((result.stdout or "") + (result.stderr or "")).strip()
            for line in tail.splitlines()[-8:]:
                print(f"        {line}")

    if gui.is_file():
        verify_windowed_gui(gui, failures)
    else:
        print("  FAIL bbpull-gui.exe missing")
        failures.append("bbpull-gui.exe")

    if failures:
        raise SystemExit(f"smoke test failed: {', '.join(failures)}")


def report_size():
    total = sum(path.stat().st_size for path in APP_DIR.rglob("*") if path.is_file())
    megabytes = total / (1024 * 1024)
    print(f"  dist/bbpull: {megabytes:.0f} MB, "
          f"{sum(1 for _ in APP_DIR.rglob('*') if _.is_file())} files")
    if not MIN_EXPECTED_MB <= megabytes <= MAX_EXPECTED_MB:
        print(f"  note: size outside the expected "
              f"{MIN_EXPECTED_MB}-{MAX_EXPECTED_MB} MB range")
    return megabytes


def zip_release(version="dev"):
    print("[4/4] archive")
    target = DIST / f"bbpull-{version}-windows-x64.zip"
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(APP_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(DIST))
    print(f"  {target.name}: {target.stat().st_size / (1024 * 1024):.0f} MB")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the Windows executables.")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--version", default="dev")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)

    make_icon()
    build()
    if not args.skip_tests:
        smoke_test()
    report_size()
    if not args.no_zip:
        zip_release(args.version)

    print()
    print("done.")
    print(f"  run:     {APP_DIR / 'bbpull.exe'} gui")
    print(f"  or:      {APP_DIR / 'bbpull-gui.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
