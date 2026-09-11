"""Runtime health check, for the packaged executables.

`bbpull selftest` runs the offline unit suite from a source checkout. A packaged
executable has no `tests/` directory - it is unpacked into a temporary `_MEI…`
folder - so `unittest discover` fails with "Start directory is not importable".

Rather than ship the test suite, the frozen build checks what a user actually
needs to know: are the dependencies really inside this executable, will a window
open, and can the program write where it intends to. Offline, no credentials, and
no writes outside a temporary probe file.
"""

import importlib
import os
import sys
import tempfile

#: (module name, label, required). `tkinter` is optional: it is only the fallback
#: interface, and some Python builds omit it.
MODULES = (
    ("requests", "HTTP 用戶端", True),
    ("PySide6", "Qt 介面", True),
    ("customtkinter", "Tk 降級介面", False),
    ("tkinter", "內建 tkinter", False),
)


def _check_modules():
    lines = []
    ok = True
    for name, label, required in MODULES:
        try:
            module = importlib.import_module(name)
            version = getattr(module, "__version__", "ok")
            lines.append(f"  [ok]   {label:16} {name} {version}")
        except Exception as exc:  # noqa: BLE001
            marker = "FAIL" if required else "warn"
            if required:
                ok = False
            lines.append(f"  [{marker}] {label:16} {name} - {type(exc).__name__}")
    return ok, lines


def _check_engine():
    from .gui_select import ENGINE_QT, decide

    choice = decide()
    lines = [f"  [{'ok' if choice['engine'] else 'FAIL'}] "
             f"GUI 引擎：{choice['engine'] or '無可用引擎'}",
             f"         {choice['reason']}"]
    if choice["warnings"]:
        for warning in choice["warnings"]:
            lines.append(f"         {warning.splitlines()[0]}")
    if choice["hint"] and choice["engine"] != ENGINE_QT:
        lines.append(f"         {choice['hint'].splitlines()[-1].strip()}")
    return choice["engine"] is not None, lines


def _check_theme():
    """Both palettes must load; a bundled resource mistake would show up here."""
    lines = []
    ok = True
    try:
        from .palette import DARK, LIGHT, Palette

        for mode, source in (("dark", DARK), ("light", LIGHT)):
            palette = Palette(mode)
            missing = [key for key in source if not getattr(palette, key, None)]
            if missing:
                ok = False
                lines.append(f"  [FAIL] {mode} 調色盤缺少 {missing[:3]}")
            else:
                lines.append(f"  [ok]   {mode} 調色盤 {len(source)} 色")
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"  [FAIL] 調色盤載入失敗：{type(exc).__name__}: {exc}")
    return ok, lines


def _check_write_access():
    """The program must be able to write, and keep, its state.

    Writing successfully is not enough: a frozen build whose state directory
    lands inside the PyInstaller `_MEI…` temp folder can write fine and still
    lose the saved login on exit.
    """
    lines = []
    ok = True
    try:
        from .config import load_config

        cfg = load_config(None)
        temporary = os.path.normcase(tempfile.gettempdir())
        for label, path in (("state", getattr(cfg, "state_dir", None)),
                            ("output", getattr(cfg, "out_dir", None))):
            if not path:
                continue
            resolved = os.path.abspath(path)
            try:
                os.makedirs(resolved, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=resolved,
                                                 prefix=".bbpull-probe-",
                                                 delete=True):
                    pass
            except OSError as exc:
                ok = False
                lines.append(f"  [FAIL] {label} 目錄不可寫 {resolved} - {exc}")
                continue

            if label == "state" and os.path.normcase(resolved).startswith(temporary):
                ok = False
                lines.append(f"  [FAIL] state 目錄在暫存區，登入狀態會在結束時消失："
                             f"{resolved}")
            else:
                lines.append(f"  [ok]   {label} 目錄可寫：{resolved}")

        if os.path.normcase(os.path.abspath(cfg.state_dir)).startswith(temporary):
            lines.append("         請設定 BB_STATE_DIR 指向固定位置。")
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"  [FAIL] 設定載入失敗：{type(exc).__name__}: {exc}")
    return ok, lines


def _check_qt_platform():
    """Qt needs its platform plugin; a missing one is a common packaging failure."""
    lines = []
    try:
        import PySide6
        from pathlib import Path

        plugins = Path(PySide6.__file__).parent / "plugins" / "platforms"
        if plugins.is_dir():
            found = [p.name for p in plugins.glob("qwindows*")]
            if found:
                lines.append(f"  [ok]   Qt 平台外掛 {', '.join(found[:3])}")
                return True, lines
            lines.append(f"  [FAIL] Qt 平台外掛目錄存在但沒有 qwindows：{plugins}")
            return False, lines
        lines.append(f"  [warn] 找不到 Qt 平台外掛目錄：{plugins}")
        return True, lines
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  [warn] 無法檢查 Qt 外掛：{type(exc).__name__}")
        return True, lines


def run_checks():
    """Return (ok, lines). Never raises: a failing check is a reported line."""
    header = [
        "執行檔自我檢查",
        f"  Python {sys.version.split()[0]}",
        f"  執行檔 {sys.executable}",
        f"  打包模式 {'是' if getattr(sys, 'frozen', False) else '否'}",
        "",
    ]
    results = []
    for check in (_check_modules, _check_engine, _check_theme,
                  _check_qt_platform, _check_write_access):
        try:
            ok, lines = check()
        except Exception as exc:  # noqa: BLE001
            ok, lines = False, [f"  [FAIL] {check.__name__} 檢查時發生例外：{exc}"]
        results.append(ok)
        header.extend(lines)

    failed = results.count(False)
    header.append("")
    header.append("自我檢查通過" if not failed else f"自我檢查有 {failed} 項失敗")
    return not failed, header
