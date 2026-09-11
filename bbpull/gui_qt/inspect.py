"""Interactive inspection mode: click anywhere and the app records what you meant.

Built because reverse-engineering cropped screenshots was unreliable - two crops
at different sizes and offsets made me chase a difference that measured as zero
pixels. This records ground truth instead: for every click, which widget was
under the cursor, its full style state, and a magnified crop of that exact spot.

Output goes to a session directory as JSONL plus PNGs, so an agent can read the
result without needing the screenshots described to it.

Enable with `bbpull gui --inspect`.
"""

import json
import os
import time

from PySide6.QtCore import QObject, QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

#: Crop geometry for the automatic shot taken on every click:
#: (half-width, half-height, zoom) — a tight 4x view for pixel detail and a
#: wider 2x view for context.
CROPS = ((110, 46, 4), (300, 130, 2))


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - inspection must never break the app
        return default


def describe_widget(widget):
    """Everything about one widget that could explain how it looks."""
    if widget is None:
        return None
    geo = _safe(widget.geometry)
    grect = _safe(widget.frameGeometry)
    font = _safe(widget.font)
    palette = _safe(widget.palette)

    text = None
    for getter in ("text", "currentText", "placeholderText", "windowTitle"):
        if hasattr(widget, getter):
            value = _safe(getattr(widget, getter))
            if value:
                text = str(value)
                break

    info = {
        "class": _safe(lambda: widget.metaObject().className()),
        "objectName": _safe(widget.objectName) or "",
        "text": text,
        "size": [geo.width(), geo.height()] if geo else None,
        "pos_in_parent": [geo.x(), geo.y()] if geo else None,
        "global_rect": ([grect.x(), grect.y(), grect.width(), grect.height()]
                        if grect else None),
        "enabled": bool(_safe(widget.isEnabled, True)),
        "visible": bool(_safe(widget.isVisible, False)),
        "own_stylesheet": (_safe(widget.styleSheet) or "")[:400] or None,
        "dynamic_props": {},
    }

    for name in _safe(widget.dynamicPropertyNames, []) or []:
        key = name.decode() if isinstance(name, bytes) else str(name)
        info["dynamic_props"][key] = str(_safe(lambda k=key: widget.property(k)))

    if font is not None:
        info["font"] = {
            "family": _safe(font.family),
            "pixelSize": _safe(font.pixelSize),
            "pointSizeF": _safe(font.pointSizeF),
            "weight": int(_safe(font.weight, 400) or 400),
            "bold": bool(_safe(font.bold, False)),
        }
    if palette is not None:
        info["palette"] = {
            key: _safe(lambda r=role: palette.color(r).name())
            for key, role in (
                ("window", QPalette.Window),
                ("windowText", QPalette.WindowText),
                ("base", QPalette.Base),
                ("text", QPalette.Text),
                ("button", QPalette.Button),
                ("buttonText", QPalette.ButtonText),
                ("highlight", QPalette.Highlight),
            )
        }
    return info


def widget_chain(widget, limit=8):
    chain = []
    current = widget
    depth = 0
    while current is not None and depth < limit:
        chain.append(describe_widget(current))
        current = _safe(current.parent)
        depth += 1
    return chain


class RegionSelector(QWidget):
    """Full-screen overlay for dragging out a rectangle to capture."""

    selected = Signal(QRect)
    cancelled = Signal()

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.origin = None
        self.current = None

        screens = QGuiApplication.screens()
        rect = screens[0].geometry()
        for screen in screens[1:]:
            rect = rect.united(screen.geometry())
        self._origin_offset = rect.topLeft()
        self.setGeometry(rect)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self.origin and self.current:
            band = QRect(self.origin, self.current).normalized()
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(band, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            pen = QPen(QColor("#6d6cf6"))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(band)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(band.adjusted(6, -22, 0, 0),
                             f"{band.width()}x{band.height()}")

    def mousePressEvent(self, event):
        self.origin = event.position().toPoint()
        self.current = self.origin
        self.update()

    def mouseMoveEvent(self, event):
        if self.origin:
            self.current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.origin and self.current:
            band = QRect(self.origin, self.current).normalized()
            if band.width() > 6 and band.height() > 6:
                band.translate(self._origin_offset)
                self.selected.emit(band)
                self.close()
                return
        self.cancelled.emit()
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()


class Inspector(QObject):
    """Application-wide click recorder.

    Keeps a reference to the window so each entry can carry the app's state
    (catalog loaded? theme? chip text?) — without it, the log cannot say *which*
    screen a click belonged to.
    """

    def __init__(self, window, out_dir, log=None):
        super().__init__(window)
        self.window = window
        self.log = log or (lambda *a: None)
        self.out_dir = os.path.abspath(out_dir)
        self.shots_dir = os.path.join(self.out_dir, "shots")
        os.makedirs(self.shots_dir, exist_ok=True)
        self.log_path = os.path.join(self.out_dir, "clicks.jsonl")
        self.count = 0
        self._selector = None
        self._last_signature = None

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self.hud = self._build_hud()
        self._reposition_hud()
        window.installEventFilter(self)
        self.log(f"檢查模式已開啟：點任何位置都會記錄到 {self.out_dir}")

    # ------------------------------------------------------------- HUD
    def _build_hud(self):
        hud = QFrame(self.window)
        hud.setObjectName("InspectHud")
        hud.setStyleSheet(
            "QFrame#InspectHud { background: #1a1f2b; border: 1px solid #6d6cf6;"
            " border-radius: 10px; }"
            "QLabel { color: #e7eaf3; background: transparent; font-size: 11px; }"
            "QPushButton { background: #232641; color: #e7eaf3; border: none;"
            " border-radius: 7px; padding: 4px 10px; font-size: 11px; }"
            "QPushButton:hover { background: #6d6cf6; }")
        layout = QHBoxLayout(hud)
        layout.setContentsMargins(10, 5, 8, 5)
        layout.setSpacing(8)
        self.hud_label = QLabel("檢查模式：點任何位置都會記錄")
        layout.addWidget(self.hud_label)

        region = QPushButton("框選區域")
        region.setCursor(Qt.PointingHandCursor)
        region.clicked.connect(self.start_region)
        layout.addWidget(region)

        opener = QPushButton("開啟記錄")
        opener.setCursor(Qt.PointingHandCursor)
        opener.clicked.connect(self._open_folder)
        layout.addWidget(opener)

        hud.adjustSize()
        hud.raise_()
        return hud

    def _reposition_hud(self):
        if not _safe(self.hud.isVisible, False):
            return
        margin = 14
        self.hud.adjustSize()
        self.hud.move(max(margin, self.window.width() - self.hud.width() - margin),
                      max(margin, self.window.height() - self.hud.height() - 44))

    def _open_folder(self):
        try:
            if os.name == "nt":
                os.startfile(self.out_dir)  # noqa: S606 - user-initiated
        except OSError:
            pass

    # ------------------------------------------------------- selection
    def start_region(self):
        self._selector = RegionSelector()
        self._selector.selected.connect(self._capture_region)
        self._selector.showFullScreen()

    def _capture_region(self, rect):
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        pixmap = screen.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height())
        rel = f"shots/region-{self.count + 1:04d}.png"
        path = os.path.join(self.out_dir, rel)
        zoomed = pixmap.scaled(pixmap.width() * 3, pixmap.height() * 3,
                               Qt.KeepAspectRatio, Qt.FastTransformation)
        zoomed.save(path, "PNG")
        self._record({
            "kind": "region",
            "global_rect": [rect.x(), rect.y(), rect.width(), rect.height()],
            "shot": rel,
        })

    # ------------------------------------------------------- recording
    def eventFilter(self, obj, event):
        etype = event.type()

        if obj is self.window and etype == QEvent.Resize:
            self._reposition_hud()

        if etype == QEvent.MouseButtonRelease:
            # Logged on release so the app has already reacted to the click and
            # the recorded state matches what the user saw.
            self._capture_click(event, obj)

        return False

    def _capture_click(self, event, _obj):
        global_pos = _safe(event.globalPosition)
        if global_pos is None:
            return
        point = global_pos.toPoint()

        # One physical click can be delivered to the filter more than once (the
        # same release propagating through the delivery chain). Collapsing
        # identical clicks inside a short window keeps the log one-entry-per-click
        # - measured, a single synthetic click produced five records without this.
        button = int(_safe(lambda: event.button().value, 0) or 0)
        signature = (point.x(), point.y(), button, int(time.monotonic() * 10))
        if signature == self._last_signature:
            return
        self._last_signature = signature

        try:
            widget = QApplication.widgetAt(point)
        except Exception:  # noqa: BLE001
            widget = None

        window_pos = _safe(lambda: self.window.mapFromGlobal(point))
        entry = {
            "kind": "click",
            "button": button,
            "global": [point.x(), point.y()],
            "in_window": [window_pos.x(), window_pos.y()] if window_pos else None,
            "widget": describe_widget(widget),
            "chain": widget_chain(widget),
            "app": self._app_state(),
            "shots": self._shots_for(window_pos),
        }
        self._record(entry)

    def _app_state(self):
        window = self.window
        chip = _safe(lambda: window.chip)
        return {
            "has_catalog": bool(_safe(lambda: window.catalog is not None, False)),
            "loading": bool(_safe(lambda: window._loading, False)),
            "theme": _safe(lambda: window.theme_mode),
            "selection_count": len(_safe(lambda: window.topmost_selection(), []) or []),
            "chip_text": _safe(lambda: chip.text()) if chip is not None else None,
            "chip_state": _safe(lambda: chip.property("state")) if chip is not None else None,
            "status": _safe(lambda: window.status.text()),
        }

    def _shots_for(self, window_pos):
        """Magnified crops around the click, in window coordinates."""
        if window_pos is None:
            return {}
        full = _safe(self.window.grab)
        if full is None or full.isNull():
            return {}
        shots = {}
        index = self.count + 1
        for half_w, half_h, zoom in CROPS:
            left = max(0, window_pos.x() - half_w)
            top = max(0, window_pos.y() - half_h)
            right = min(full.width(), window_pos.x() + half_w)
            bottom = min(full.height(), window_pos.y() + half_h)
            band = full.copy(QRect(left, top, right - left, bottom - top))
            if band.isNull():
                continue
            scaled = band.scaled(band.width() * zoom, band.height() * zoom,
                                 Qt.KeepAspectRatio, Qt.FastTransformation)
            rel = f"shots/click-{index:04d}-{zoom}x.png"
            scaled.save(os.path.join(self.out_dir, rel), "PNG")
            shots[f"{zoom}x"] = {
                "file": rel,
                "window_rect": [left, top, right - left, bottom - top],
                "click_offset_in_crop": [window_pos.x() - left, window_pos.y() - top],
            }
        return shots

    def _record(self, entry):
        self.count += 1
        entry["id"] = self.count
        entry["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.log(f"無法寫入檢查記錄：{exc}")
        widget = entry.get("widget") or {}
        summary = f"{widget.get('class', '?')}"
        if widget.get("objectName"):
            summary += f"#{widget['objectName']}"
        if widget.get("text"):
            summary += f" 「{str(widget['text'])[:24]}」"
        size = widget.get("size")
        if size:
            summary += f" {size[0]}x{size[1]}"
        self.hud_label.setText(f"#{entry['id']} 已記錄：{summary[:52]}")
        self.hud.adjustSize()
        self._reposition_hud()
        self.hud.raise_()
        if entry["kind"] == "click":
            self.log(f"[inspect] #{entry['id']} {summary}")
