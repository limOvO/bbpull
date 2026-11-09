"""Build the distributable executables for the platform running the build.

Wraps PyInstaller so the build is one reproducible command, and verifies the
result instead of trusting a zero exit code. A build that succeeds but produces
an executable missing a dynamically imported module is the worst release
failure: it looks fine until someone tries the one feature that was dropped.

**Windows** produces two executables and a zip:

    dist/bbpull/bbpull.exe          console build (full CLI + GUI)
    dist/bbpull/bbpull-gui.exe      window build (double-click, no console)
    dist/bbpull-<version>-windows-x64.zip

**Linux / macOS** produces one executable - POSIX has no windowed subsystem, and
the desktop launcher passes `gui` explicitly - plus the integration files that
make an extracted folder usable without a terminal:

    dist/bbpull/bbpull              the executable (CLI + `gui`)
    dist/bbpull/bbpull-gui          tiny wrapper: `exec ./bbpull gui`
    dist/bbpull/bbpull.png          256px application icon
    dist/bbpull/install.sh          per-user install + menu entry
    dist/bbpull-<version>-linux-<arch>.tar.gz

Steps:
  1. regenerate the icon from the app's own palette;
  2. run PyInstaller against `bbpull.spec`;
  3. smoke-test the executable(s): `--help`, `selftest`, `gui --check`, `venv`,
     and a real window that opens and closes itself (`gui --smoke`);
  4. add the POSIX integration files and archive the folder.

Usage:
    python tools/build_exe.py            # build + verify + archive
    python tools/build_exe.py --no-zip   # build + verify only
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_DIR = DIST / "bbpull"
SPEC = ROOT / "bbpull.spec"
ICON_ICO = ROOT / "installer" / "bbpull.ico"
ICON_PNG = ROOT / "installer" / "bbpull.png"

# The release name defaults to the version the app itself reports, so the
# archive and `bbpull --version` cannot drift apart.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bbpull import __version__ as APP_VERSION  # noqa: E402

WINDOWS = os.name == "nt"
EXE = ".exe" if WINDOWS else ""
CONSOLE_NAME = f"bbpull{EXE}"
GUI_NAME = f"bbpull-gui{EXE}"

#: Checks run against the built executables. Each is (label, argv, expected).
#: `gui --smoke` opens the real window and closes it after a moment; it is the
#: only check that proves a window can be created, which is exactly what a
#: missing Qt platform plugin breaks.
SMOKE_TESTS = (
    ("console --help", ["--help"], 0),
    ("console selftest", ["selftest"], 0),
    ("console gui --check", ["gui", "--check"], 0),
    ("console venv", ["venv"], 0),
    ("gui opens a window", ["gui", "--smoke", "--qt"], 0),
)

MIN_EXPECTED_MB = 40
MAX_EXPECTED_MB = 900


def run(argv, timeout=1800, cwd=None, capture=True, env=None):
    display = " ".join(str(a) for a in argv)
    print(f"  $ {display}")
    return subprocess.run([str(a) for a in argv], cwd=str(cwd or ROOT),
                          capture_output=capture, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", env=env)


def platform_tag():
    """Release-archive platform tag, e.g. `windows-x64` / `linux-x86_64`."""
    machine = platform.machine().lower() or "unknown"
    if WINDOWS:
        return "windows-x64" if machine in ("amd64", "x86_64") else f"windows-{machine}"
    if sys.platform == "darwin":
        return f"macos-{machine}"
    return f"linux-{machine}"


def make_icon():
    """Regenerate the icon with the application's own palette."""
    print("[1/4] icon")
    target = ICON_ICO if WINDOWS else ICON_PNG
    result = run([sys.executable, str(ROOT / "installer" / "make_icon.py"),
                  str(target)])
    if result.returncode != 0:
        print(result.stdout or "", result.stderr or "")
        raise SystemExit("icon generation failed")
    print(f"  {target.name}: {target.stat().st_size} bytes")


def kill_leftovers():
    """Kill any running bbpull executable before touching dist/.

    Windows refuses to delete a running .exe, so a leftover process turns a
    rebuild into `PermissionError: [WinError 5]` mid-COLLECT. A previous smoke
    test can leave one behind, especially the windowed build, which shows a modal
    dialog on failure and therefore never exits on its own.

    On POSIX a running binary can be replaced freely, so there is nothing to do.
    """
    if not WINDOWS:
        return
    for name in (CONSOLE_NAME, GUI_NAME):
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
    if not (APP_DIR / CONSOLE_NAME).is_file():
        raise SystemExit(f"{CONSOLE_NAME} was not produced in {APP_DIR}")


# --------------------------------------------------------------------------
# Windows-only window verification
# --------------------------------------------------------------------------
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
    what a double-click does. That fidelity matters: an earlier version used
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


# --------------------------------------------------------------------------
# Smoke tests
# --------------------------------------------------------------------------
def smoke_environment():
    """Environment for the GUI smoke run.

    A build machine may be headless (CI). `gui --smoke` then runs with Qt's
    offscreen platform so the event loop and window construction are still
    exercised; the packaged platform plugin is checked separately by
    `selftest`.
    """
    environment = dict(os.environ)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    if not WINDOWS and not (environment.get("DISPLAY")
                            or environment.get("WAYLAND_DISPLAY")):
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        print("  note: no display found; the window check uses "
              "QT_QPA_PLATFORM=offscreen")
    return environment


def smoke_test():
    """Run the executables. A zero exit code from PyInstaller proves nothing.

    The build can succeed while the executable is missing a dynamically imported
    module, which is the worst release failure: it looks fine until someone
    tries the one feature that was dropped.
    """
    print("[3/4] smoke test")
    console = APP_DIR / CONSOLE_NAME
    gui = APP_DIR / GUI_NAME
    failures = []
    environment = smoke_environment()

    for label, argv, expected in SMOKE_TESTS:
        try:
            result = run([console, *argv], timeout=600, env=environment)
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

    if WINDOWS:
        if gui.is_file():
            verify_windowed_gui(gui, failures)
        else:
            print("  FAIL bbpull-gui.exe missing")
            failures.append("bbpull-gui.exe")

    if failures:
        raise SystemExit(f"smoke test failed: {', '.join(failures)}")


# --------------------------------------------------------------------------
# POSIX integration files
# --------------------------------------------------------------------------
#: Tiny wrapper so the extracted folder has a double-clickable "GUI" entry,
#: matching the Windows `bbpull-gui.exe` name. It is a script, not a second copy
#: of the ~200 MB binary.
GUI_WRAPPER = """#!/bin/sh
# Open the bbpull desktop window. Equivalent to: ./bbpull gui
exec "$(dirname "$0")/bbpull" gui "$@"
"""

#: Per-user installer: no root, no package manager, uninstallable.
INSTALL_SH = """#!/bin/sh
# Install bbpull for the current user (~/.local), with a menu entry and a
# desktop shortcut.
#
#   ./install.sh              install or update (also creates the shortcut)
#   ./install.sh --no-desktop install without touching the desktop
#   ./install.sh --uninstall  remove the app, menu entry and shortcut
#
# Your saved login lives in $XDG_DATA_HOME/bbpull (default ~/.local/share/bbpull)
# and is deliberately left alone by both modes.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX="${BBPULL_PREFIX:-$HOME/.local}"
APPDIR="$PREFIX/opt/bbpull"
BINDIR="$PREFIX/bin"
ICONDIR="$PREFIX/share/icons/hicolor/256x256/apps"
APPDESK="$PREFIX/share/applications"

desktop_dir() {
    # Localised desktops use e.g. ~/桌面; xdg-user-dir knows the configured one.
    if command -v xdg-user-dir >/dev/null 2>&1; then
        dir=$(xdg-user-dir DESKTOP 2>/dev/null || true)
        if [ -n "$dir" ] && [ "$dir" != "$HOME" ]; then
            printf '%s\\n' "$dir"
            return
        fi
    fi
    for candidate in "$HOME/Desktop" "$HOME/桌面"; do
        if [ -d "$candidate" ]; then
            printf '%s\\n' "$candidate"
            return
        fi
    done
    printf '%s\\n' "$HOME/Desktop"
}

if [ "${1:-}" = "--uninstall" ]; then
    for dir in "$(desktop_dir)" "$HOME/Desktop" "$HOME/桌面"; do
        rm -f "$dir/bbpull.desktop"
    done
    rm -f "$BINDIR/bbpull" "$APPDESK/bbpull.desktop" "$ICONDIR/bbpull.png"
    rm -rf "$APPDIR"
    if [ -x "$(command -v update-desktop-database)" ]; then
        update-desktop-database "$APPDESK" >/dev/null 2>&1 || true
    fi
    echo "bbpull removed. Saved login left in ${XDG_DATA_HOME:-$HOME/.local/share}/bbpull"
    exit 0
fi

MAKE_DESKTOP=1
if [ "${1:-}" = "--no-desktop" ]; then
    MAKE_DESKTOP=0
fi

mkdir -p "$APPDIR" "$BINDIR" "$ICONDIR" "$APPDESK"
# Copy the whole bundle, replacing a previous install.
rm -rf "$APPDIR"
cp -R "$HERE" "$APPDIR"
chmod +x "$APPDIR/bbpull" "$APPDIR/bbpull-gui" "$APPDIR/install.sh" 2>/dev/null || true

ln -sf "$APPDIR/bbpull" "$BINDIR/bbpull"
if [ -f "$HERE/bbpull.png" ]; then
    cp -f "$HERE/bbpull.png" "$ICONDIR/bbpull.png"
fi

cat > "$APPDESK/bbpull.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=bbpull
Name[zh_TW]=bbpull 課程下載器
GenericName=Blackboard course downloader
Comment=Download Blackboard Ultra course content and announcements
Exec="$APPDIR/bbpull" gui
Icon=$ICONDIR/bbpull.png
Terminal=false
Categories=Education;
StartupWMClass=bbpull
DESKTOP
chmod +x "$APPDESK/bbpull.desktop"
if [ -x "$(command -v update-desktop-database)" ]; then
    update-desktop-database "$APPDESK" >/dev/null 2>&1 || true
fi

if [ "$MAKE_DESKTOP" = "1" ]; then
    # The app knows the localised desktop folder and marks the launcher trusted
    # where the desktop environment supports it (GNOME's metadata::trusted).
    "$APPDIR/bbpull" shortcut >/dev/null 2>&1 || true
fi

echo "installed:"
echo "  app     $APPDIR"
echo "  command $BINDIR/bbpull"
echo "  menu    $APPDESK/bbpull.desktop"
if [ "$MAKE_DESKTOP" = "1" ]; then
    echo "  desktop $(desktop_dir)/bbpull.desktop"
fi
case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *)
        echo ""
        echo "NOTE: $BINDIR is not on your PATH. Add it to open bbpull from a terminal:"
        echo "  export PATH=\\"$BINDIR:\\$PATH\\""
        ;;
esac
"""

README_TXT = """bbpull — Linux 版
=================

這個資料夾是獨立的執行檔，不需要安裝 Python 或任何套件。

啟動
----
  ./bbpull gui          開啟桌面視窗（建議）
  ./bbpull-gui          同上（給檔案總管雙擊用）
  ./bbpull --help       命令列說明
  ./bbpull selftest     離線自我檢查

裝進應用程式選單與桌面（不需要 root）
------------------------------------
  ./install.sh              安裝到 ~/.local，建立指令、選單與桌面捷徑
  ./install.sh --no-desktop 安裝但不建立桌面捷徑
  ./install.sh --uninstall  全部移除（保留登入狀態）
  ./bbpull shortcut         只建立桌面捷徑（直接解壓使用時也適用）

資料位置
--------
  登入狀態  ${XDG_DATA_HOME:-~/.local/share}/bbpull/
  下載輸出  目前資料夾下的 output/（或 --out 指定的位置）

密碼儲存
--------
  有桌面鑰匙圈（GNOME Keyring / KWallet）時，密碼由系統保管。
  沒有時會退回「只有檔案權限保護」(0600)，`./bbpull doctor` 會照實說明。

Linux 上的介面需要 Qt 平台外掛（xcb 或 wayland），兩者都已包含在這個
資料夾裡。若在沒有桌面環境的機器上執行，請改用命令列：
  ./bbpull pull --course-id <課程 id>

授權：MIT
"""


def write_posix_extras():
    """Add the launcher wrapper, icon, installer and README to the bundle."""
    print("     posix integration files")
    wrapper = APP_DIR / "bbpull-gui"
    wrapper.write_text(GUI_WRAPPER, encoding="utf-8", newline="\n")
    wrapper.chmod(0o755)

    installer = APP_DIR / "install.sh"
    installer.write_text(INSTALL_SH, encoding="utf-8", newline="\n")
    installer.chmod(0o755)

    if ICON_PNG.is_file():
        shutil.copyfile(ICON_PNG, APP_DIR / "bbpull.png")

    (APP_DIR / "README-linux.txt").write_text(
        README_TXT, encoding="utf-8", newline="\n")
    print(f"  {wrapper.name}, {installer.name}, bbpull.png, README-linux.txt")


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
    target = DIST / f"bbpull-{version}-{platform_tag()}.zip"
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(APP_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(DIST))
    print(f"  {target.name}: {target.stat().st_size / (1024 * 1024):.0f} MB")
    return target


def tar_release(version="dev"):
    print("[4/4] archive")
    target = DIST / f"bbpull-{version}-{platform_tag()}.tar.gz"
    target.unlink(missing_ok=True)
    with tarfile.open(target, "w:gz", compresslevel=6) as archive:
        for path in sorted(APP_DIR.rglob("*")):
            archive.add(path, arcname=str(path.relative_to(DIST)), recursive=False)
    print(f"  {target.name}: {target.stat().st_size / (1024 * 1024):.0f} MB")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the bbpull executables for this platform.")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)

    make_icon()
    build()
    if not args.skip_tests:
        smoke_test()
    if not WINDOWS:
        write_posix_extras()
    report_size()
    if not args.no_zip:
        (zip_release if WINDOWS else tar_release)(args.version)

    print()
    print("done.")
    print(f"  run:     {APP_DIR / CONSOLE_NAME} gui")
    if WINDOWS:
        print(f"  or:      {APP_DIR / GUI_NAME}")
    else:
        print(f"  install: {APP_DIR / 'install.sh'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
