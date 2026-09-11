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


def window_titles(needle):
    """Visible window titles containing `needle`.

    Matching is by title rather than by pid on purpose: PyInstaller's bootloader
    runs the app in a *child* process, so the pid returned by `Popen` owns only
    hidden bootloader windows ("PyInstaller Onefile Hidden Window"). Matching the
    spawned pid therefore never finds the GUI, and would report a working build
    as broken.
    """
    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    titles = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if needle.lower() in buffer.value.lower():
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
    """The windowed binary must open an actual window.

    "The process is still alive" is not evidence: a failed import makes
    PyInstaller show a modal error dialog, which also keeps the process running.
    Only a real window title counts.
    """
    kill_tree(gui.name)
    time.sleep(1.0)
    try:
        proc = subprocess.Popen([str(gui), "gui"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        print(f"  FAIL windowed gui could not start: {exc}")
        failures.append("windowed gui")
        return

    title = ""
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"  FAIL windowed gui exited early ({proc.returncode})")
                failures.append("windowed gui")
                return
            found = window_titles("bbpull")
            if found:
                title = found[0]
                break
            time.sleep(0.5)
    finally:
        kill_tree(gui.name)
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass

    if title:
        print(f"  ok   windowed gui opened a window: {title!r}")
    else:
        print("  FAIL windowed gui opened no window in 60s")
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
