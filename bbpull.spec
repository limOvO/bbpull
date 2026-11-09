# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: build the executables for the platform running the build.

**Windows** gets two binaries, because one shape cannot serve both jobs:

* ``bbpull.exe``      console subsystem - the full CLI works, and the GUI opens
                      from it too. A console window is correct here.
* ``bbpull-gui.exe``  window subsystem - double-click to get the window with no
                      console flash, which is what a desktop app should do.

A single ``--windowed`` build would silently break every CLI subcommand
(``pull``, ``doctor``, ``selftest``) because there is no console to print to and
no exit code for a script to read.

**Linux and macOS** get one binary. POSIX has no "windowed subsystem" and
``console=False`` has no effect there; a desktop launcher simply runs
``bbpull gui``. Building the second, identical, ~200 MB folder would only double
the release size for nothing.

One-dir rather than one-file: a one-file PySide6 build extracts roughly 200 MB to
a temp directory on every launch, which costs seconds each time. The folder is
shipped as a zip (Windows) or tar.gz (POSIX) instead.

Build:  python tools/build_exe.py
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# `SPECPATH` is the directory containing this spec, which is already the project
# root - taking `.parent` here silently pointed the build one level up and made
# PyInstaller fail with "script not found".
ROOT = Path(SPECPATH).resolve()
ICON = ROOT / "installer" / "bbpull.ico"

WINDOWS = sys.platform.startswith("win")

# Submodules reached only through dynamic imports, so PyInstaller's static
# analysis cannot see them. Missing any of these produces an executable that
# builds cleanly and fails at runtime - the worst failure mode for a release.
HIDDEN = [
    # Pure modules shared by both front-ends (the Qt window reads the course
    # helpers and the animation math); safe to bundle everywhere.
    "bbpull.gui.anim",
    "bbpull.gui.courses",
    "bbpull.gui_qt.window",
    "bbpull.gui_qt.models",
    "bbpull.gui_qt.delegates",
    "bbpull.gui_qt.theme",
    "bbpull.gui_qt.workers",
    "bbpull.gui_qt.inspect",
    # Imported inside the connect dialog's submit handler rather than at module
    # load, so it only runs when someone actually logs in - which is why a wrong
    # path here reached users as "No module named 'bbpull.gui.wizard'".
    "bbpull.wizard",
    "bbpull.healthcheck",
    # Imported inside functions: the file-manager opener used by both front-ends.
    "bbpull.desktop",
    "bbpull.gui_select",
    "bbpull.venv_tools",
    # Also reached from inside functions. They are imported at module level
    # elsewhere, so the analyser would find them anyway - but naming them makes
    # the requirement explicit and a test keeps the two lists in step, because
    # the cost of missing one is a broken release, not a warning.
    "bbpull.announcements",
    "bbpull.cli",
    "bbpull.config",
    "bbpull.course",
    "bbpull.palette",
    "bbpull.paths",
    "bbpull.session",
    "tools.secret_scan",
]

# The Tk front-end is bundled only when this build interpreter can actually
# import it. On Linux, Tk is an optional distro package (`tk` / `python3-tk`) and
# the Qt build is the default, so a machine without it must still produce a
# working bundle - naming absent modules would only add warnings and ship a Tk
# path that cannot run. `customtkinter` submodules are collected the same way.
try:
    import tkinter as _tkinter_probe  # noqa: F401
    import customtkinter as _ctk_probe  # noqa: F401

    HAS_TK = True
except Exception:  # noqa: BLE001 - optional toolkit
    HAS_TK = False

if HAS_TK:
    HIDDEN += [
        "bbpull.gui.app",
        "bbpull.gui.widgets",
        "bbpull.gui.theme",
        "customtkinter",
    ]
    HIDDEN += collect_submodules("customtkinter")

# Qt ships far more than a widgets app needs; excluding these keeps the folder
# from doubling in size. Anything the app actually imports is still included.
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQml", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSpatialAudio", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtNetworkAuth", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtGraphs", "PySide6.QtHttpServer",
    "PySide6.QtUiTools", "PySide6.QtSvgWidgets",
    # Development-only dependencies that must not be bundled.
    # `unittest` is deliberately NOT excluded: it is small, and leaving it out
    # turns an incidental import into a runtime crash in the shipped executable.
    "PyInstaller", "setuptools", "pip", "pytest",
    "tkinter.test", "test", "lib2to3", "pydoc_data",
]

COMMON = dict(
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

a_console = Analysis([str(ROOT / "installer" / "entry.py")], **COMMON)
pyz_console = PYZ(a_console.pure)
# `exclude_binaries=True` is what makes the executable land in WORKPATH and be
# copied into the COLLECT folder. Without it, PyInstaller writes the executable
# straight to `dist/bbpull` - which on POSIX (no `.exe` suffix) is the same path
# as the COLLECT output directory, so it deletes the executable it just built
# and fails with "Resource 'dist/bbpull' is not a valid file!".
exe_console = EXE(
    pyz_console,
    a_console.scripts,
    [],
    exclude_binaries=True,
    name="bbpull",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if WINDOWS and ICON.is_file() else None,
)

# Only Windows needs the windowed twin; POSIX has no such subsystem.
targets = [exe_console]
binaries = list(a_console.binaries)
datas = list(a_console.datas)

if WINDOWS:
    a_windowed = Analysis([str(ROOT / "installer" / "entry.py")], **COMMON)
    pyz_windowed = PYZ(a_windowed.pure)
    exe_windowed = EXE(
        pyz_windowed,
        a_windowed.scripts,
        [],
        exclude_binaries=True,
        name="bbpull-gui",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON) if ICON.is_file() else None,
    )
    targets.append(exe_windowed)
    binaries += list(a_windowed.binaries)
    datas += list(a_windowed.datas)

COLLECT(
    *targets,
    binaries,
    datas,
    strip=False,
    upx=False,
    name="bbpull",
)
