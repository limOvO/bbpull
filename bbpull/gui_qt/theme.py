"""Qt look-and-feel: a QSS stylesheet plus QPainter-drawn icons.

Icons are painted rather than shipped as files, for the same reason as the Tk
build: no asset-path problems and crisp output at any DPI. Qt has a real alpha
channel and antialiasing, so unlike Tk these can use genuine translucency.
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from ..palette import KIND_LABELS, Palette, blend  # noqa: F401  (re-exported)

#: Icon strokes are drawn in a 24x24 coordinate space and scaled to fit.
GRID = 24.0


def qcolor(value, alpha=None):
    colour = QColor(value)
    if alpha is not None:
        colour.setAlphaF(max(0.0, min(1.0, alpha)))
    return colour


def draw_icon(painter, kind, colour, rect):
    """Paint one kind glyph into `rect` (a QRectF)."""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(1.3, rect.width() / 13.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    s = rect.width()
    ox, oy = rect.x(), rect.y()
    grid = GRID

    def pt(x, y):
        return QPointF(ox + x / grid * s, oy + y / grid * s)

    if kind == "folder":
        path = QPainterPath(pt(3, 6))
        path.lineTo(pt(9, 6))
        path.lineTo(pt(11, 9))
        path.lineTo(pt(21, 9))
        path.lineTo(pt(21, 19))
        path.lineTo(pt(3, 19))
        path.closeSubpath()
        painter.setBrush(qcolor(colour))
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)
    elif kind == "document":
        path = QPainterPath(pt(5, 3))
        path.lineTo(pt(14, 3))
        path.lineTo(pt(19, 8))
        path.lineTo(pt(19, 21))
        path.lineTo(pt(5, 21))
        path.closeSubpath()
        painter.setBrush(qcolor(colour))
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)
        line = QPen(qcolor(blend("#ffffff", colour, 0.15)))
        line.setWidthF(max(1.0, s / 22.0))
        painter.setPen(line)
        painter.drawLine(pt(8, 12), pt(16, 12))
        painter.drawLine(pt(8, 15.5), pt(14, 15.5))
    elif kind == "file":
        path = QPainterPath(pt(5, 3))
        path.lineTo(pt(14, 3))
        path.lineTo(pt(19, 8))
        path.lineTo(pt(19, 21))
        path.lineTo(pt(5, 21))
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(pt(14, 3), pt(14, 8))
        painter.drawLine(pt(14, 8), pt(19, 8))
    elif kind == "link":
        painter.drawArc(QRectF(*_rect_args(rect, 2, 9, 12, 12)), 40 * 16, 230 * 16)
        painter.drawArc(QRectF(*_rect_args(rect, 10, 3, 12, 12)), 220 * 16, 230 * 16)
        painter.drawLine(pt(9, 15), pt(15, 9))
    elif kind == "assessment":
        path = QPainterPath(pt(12, 3))
        path.lineTo(pt(21, 7))
        path.lineTo(pt(21, 17))
        path.lineTo(pt(12, 21))
        path.lineTo(pt(3, 17))
        path.lineTo(pt(3, 7))
        path.closeSubpath()
        painter.drawPath(path)
        tick = QPen(qcolor(colour))
        tick.setWidthF(max(1.8, s / 10.0))
        tick.setCapStyle(Qt.RoundCap)
        painter.setPen(tick)
        painter.drawLine(pt(8, 12), pt(11, 15))
        painter.drawLine(pt(11, 15), pt(16, 9))
    elif kind == "tool":
        painter.drawRoundedRect(QRectF(*_rect_args(rect, 3, 5, 18, 14)), 2.5, 2.5)
        painter.drawLine(pt(3, 10), pt(21, 10))
        painter.drawLine(pt(8, 14.5), pt(16, 14.5))
    elif kind == "announcement":
        path = QPainterPath(pt(3, 9))
        path.lineTo(pt(7, 9))
        path.lineTo(pt(15, 4))
        path.lineTo(pt(15, 20))
        path.lineTo(pt(7, 15))
        path.lineTo(pt(3, 15))
        path.closeSubpath()
        painter.setBrush(qcolor(colour))
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)
        wave = QPen(qcolor(colour))
        wave.setWidthF(max(1.4, s / 14.0))
        wave.setCapStyle(Qt.RoundCap)
        painter.setPen(wave)
        painter.drawArc(QRectF(*_rect_args(rect, 14, 8, 9, 8)), -60 * 16, 120 * 16)
    else:
        painter.drawEllipse(QRectF(*_rect_args(rect, 4, 4, 16, 16)))

    painter.restore()


def _rect_args(rect, x, y, w, h):
    s = rect.width() / GRID
    return (rect.x() + x * s, rect.y() + y * s, w * s, h * s)


def draw_chevron(painter, direction, colour, rect):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(1.6, rect.width() / 9.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    s = rect.width()
    ox, oy = rect.x(), rect.y()

    def pt(x, y):
        return QPointF(ox + x / GRID * s, oy + y / GRID * s)

    if direction == "left":
        painter.drawPolyline([pt(15, 5), pt(9, 12), pt(15, 19)])
    elif direction == "right":
        painter.drawPolyline([pt(9, 5), pt(15, 12), pt(9, 19)])
    elif direction == "up":
        painter.drawPolyline([pt(5, 15), pt(12, 8), pt(19, 15)])
    else:
        painter.drawPolyline([pt(5, 9), pt(12, 16), pt(19, 9)])
    painter.restore()


def draw_tick(painter, colour, rect):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(2.0, rect.width() / 6.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    s = rect.width()
    ox, oy = rect.x(), rect.y()
    painter.drawPolyline([
        QPointF(ox + 5 / GRID * s, oy + 12 / GRID * s),
        QPointF(ox + 10 / GRID * s, oy + 17 / GRID * s),
        QPointF(ox + 19 / GRID * s, oy + 7 / GRID * s),
    ])
    painter.restore()


def draw_close(painter, colour, rect):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(1.8, rect.width() / 8.0))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    s = rect.width()
    ox, oy = rect.x(), rect.y()
    painter.drawLine(QPointF(ox + 6 / GRID * s, oy + 6 / GRID * s),
                     QPointF(ox + 18 / GRID * s, oy + 18 / GRID * s))
    painter.drawLine(QPointF(ox + 18 / GRID * s, oy + 6 / GRID * s),
                     QPointF(ox + 6 / GRID * s, oy + 18 / GRID * s))
    painter.restore()


def draw_account(painter, colour, rect):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(1.5, rect.width() / 11.0))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    s = rect.width() / GRID
    ox, oy = rect.x(), rect.y()
    painter.drawEllipse(QRectF(ox + 8 * s, oy + 4 * s, 8 * s, 8 * s))
    painter.drawArc(QRectF(ox + 4 * s, oy + 13 * s, 16 * s, 13 * s), 0, 180 * 16)
    painter.restore()


def draw_refresh(painter, colour, rect):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(1.6, rect.width() / 10.0))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    s = rect.width() / GRID
    ox, oy = rect.x(), rect.y()
    painter.drawArc(QRectF(ox + 5 * s, oy + 5 * s, 14 * s, 14 * s), 60 * 16, 280 * 16)
    painter.drawPolyline([
        QPointF(ox + 15 * s, oy + 4 * s),
        QPointF(ox + 19 * s, oy + 7 * s),
        QPointF(ox + 15 * s, oy + 8 * s),
    ])
    painter.restore()


def draw_download(painter, colour, rect):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(1.6, rect.width() / 10.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    s = rect.width() / GRID
    ox, oy = rect.x(), rect.y()
    painter.drawLine(QPointF(ox + 12 * s, oy + 4 * s), QPointF(ox + 12 * s, oy + 16 * s))
    painter.drawPolyline([
        QPointF(ox + 7 * s, oy + 11 * s),
        QPointF(ox + 12 * s, oy + 16 * s),
        QPointF(ox + 17 * s, oy + 11 * s),
    ])
    painter.drawLine(QPointF(ox + 5 * s, oy + 20 * s), QPointF(ox + 19 * s, oy + 20 * s))
    painter.restore()


def draw_folder_open(painter, colour, rect):
    draw_icon(painter, "folder", colour, rect)


def draw_moon(painter, colour, rect):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(1.6, rect.width() / 10.0))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    s = rect.width() / GRID
    ox, oy = rect.x(), rect.y()
    painter.drawArc(QRectF(ox + 5 * s, oy + 4 * s, 15 * s, 16 * s), 60 * 16, 240 * 16)
    painter.drawArc(QRectF(ox + 8 * s, oy + 2 * s, 16 * s, 16 * s), 120 * 16, 200 * 16)
    painter.restore()


def draw_sun(painter, colour, rect):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(qcolor(colour))
    pen.setWidthF(max(1.6, rect.width() / 11.0))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    s = rect.width() / GRID
    ox, oy = rect.x(), rect.y()
    painter.drawEllipse(QRectF(ox + 9 * s, oy + 9 * s, 6 * s, 6 * s))
    import math

    for i in range(8):
        angle = math.radians(i * 45)
        x1 = ox + 12 * s + math.cos(angle) * 7.0 * s
        y1 = oy + 12 * s + math.sin(angle) * 7.0 * s
        x2 = ox + 12 * s + math.cos(angle) * 10.0 * s
        y2 = oy + 12 * s + math.sin(angle) * 10.0 * s
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    painter.restore()


#: UI glyph names (as opposed to content kinds) and the painter for each.
UI_GLYPHS = {
    "chevron_left": lambda p, c, r: draw_chevron(p, "left", c, r),
    "chevron_right": lambda p, c, r: draw_chevron(p, "right", c, r),
    "chevron_up": lambda p, c, r: draw_chevron(p, "up", c, r),
    "chevron_down": lambda p, c, r: draw_chevron(p, "down", c, r),
    "tick": draw_tick,
    "close": draw_close,
    "account": draw_account,
    "refresh": draw_refresh,
    "download": draw_download,
    "moon": draw_moon,
    "sun": draw_sun,
}


def make_icon(name, colour, size=32):
    """An actual QIcon (for buttons/dialogs) rather than a paint call.

    Accepts both UI glyph names ("chevron_left") and content kinds ("folder"),
    so callers do not need to know which table a name lives in.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    rect = QRectF(0, 0, size, size)
    glyph = UI_GLYPHS.get(name)
    if glyph:
        glyph(painter, colour, rect)
    else:
        draw_icon(painter, name, colour, rect)
    painter.end()
    return QIcon(pixmap)


def stylesheet(pal):
    """The application-wide QSS for one palette.

    Uses object names / dynamic properties as hooks instead of a cascade of
    per-widget inline styles, so a theme switch is one `setStyleSheet` call.
    """
    return f"""
    QWidget {{
        background: {pal.bg};
        color: {pal.text};
        font-family: "Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI";
        font-size: 12px;
    }}
    QMainWindow, QDialog {{ background: {pal.bg}; }}

    /* ---------------------------------------------------------- top bar */
    #TopBar {{ background: {pal.bg_elev}; border-bottom: 1px solid {pal.line}; }}
    #BrandMark {{ background: {pal.accent}; color: #ffffff;
                  border-radius: 7px; font-weight: bold; font-size: 11px; }}
    #BrandText {{ font-size: 13px; font-weight: bold; color: {pal.text};
                  background: transparent; }}
    #Chip {{ background: {pal.chip}; color: {pal.text_dim};
             border-radius: 11px; padding: 2px 10px; font-size: 11px; }}
    #Chip[state="ok"] {{ color: {pal.ok}; }}
    #Chip[state="dim"] {{ color: {pal.text_dim}; }}
    #Chip[state="off"] {{ color: {pal.text_faint}; }}

    /* ------------------------------------------------------- icon button */
    QToolButton {{ background: transparent; border: none; border-radius: 8px;
                   padding: 4px; }}
    QToolButton:hover {{ background: {pal.panel2}; }}
    QToolButton:pressed {{ background: {pal.hover}; }}
    QToolButton:disabled {{ opacity: 0.4; }}

    /* ---------------------------------------------------------- sidebar */
    #Sidebar {{ background: {pal.side}; border-right: 1px solid {pal.line}; }}
    #SidebarTitle {{ color: {pal.text_faint}; font-size: 10px;
                     font-weight: bold; background: transparent; }}
    #SidebarFooter {{ color: {pal.text_faint}; font-size: 10px;
                      background: transparent; }}

    /* ------------------------------------------------------------ lists */
    QListView, QTreeView {{
        background: {pal.bg};
        border: none;
        outline: none;
        selection-background-color: transparent;
    }}
    QListView#CourseList {{ background: {pal.side}; }}
    QListView::item, QTreeView::item {{ border: none; }}

    /* --------------------------------------------------------- scrollbar */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {pal.scroll}; border-radius: 5px;
                                   min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {pal.text_faint}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{ background: {pal.scroll}; border-radius: 5px; }}

    /* ------------------------------------------------------------ inputs */
    QLineEdit {{ background: {pal.panel2}; border: 1px solid {pal.line};
                 border-radius: 8px; padding: 6px 9px; color: {pal.text}; }}
    QLineEdit:focus {{ border: 1px solid {pal.accent}; }}
    QLineEdit::placeholder {{ color: {pal.text_faint}; }}

    QComboBox {{ background: {pal.panel2}; border: 1px solid {pal.line};
                 border-radius: 8px; padding: 5px 9px; color: {pal.text_dim}; }}
    QComboBox:hover {{ border: 1px solid {pal.accent_line}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{
        background: {pal.panel}; border: 1px solid {pal.line};
        selection-background-color: {pal.accent_soft};
        selection-color: {pal.text}; color: {pal.text}; outline: none;
    }}

    /* ----------------------------------------------------------- buttons */
    QPushButton {{ background: {pal.panel2}; border: 1px solid {pal.line};
                   border-radius: 9px; padding: 7px 14px; color: {pal.text}; }}
    QPushButton:hover {{ border: 1px solid {pal.accent}; }}
    QPushButton:disabled {{ color: {pal.text_faint}; }}
    QPushButton#Primary {{ background: {pal.accent}; border: none; color: #ffffff;
                           font-weight: bold; }}
    QPushButton#Primary:hover {{ background: {pal.accent2}; }}
    QPushButton#Ghost {{ background: transparent; color: {pal.text_dim}; }}
    QPushButton#Ghost:hover {{ background: {pal.panel2}; color: {pal.text}; }}

    /* -------------------------------------------------------------- tray */
    #Tray {{ background: {pal.panel}; border: 1px solid {pal.line};
             border-radius: 13px; }}
    #TrayCount {{ font-weight: bold; font-size: 12px; background: transparent; }}
    #TraySub {{ color: {pal.text_faint}; font-size: 10px; background: transparent; }}

    /* ------------------------------------------------------------ panels */
    #JobsPanel {{ background: {pal.bg_elev}; border-left: 1px solid {pal.line}; }}
    #JobsTitle {{ color: {pal.text_faint}; font-size: 10px; font-weight: bold;
                  background: transparent; }}
    #JobCard {{ background: {pal.panel}; border: 1px solid {pal.line};
                border-radius: 10px; }}
    #JobLabel {{ font-weight: bold; font-size: 11px; background: transparent; }}
    #JobMeta {{ color: {pal.text_faint}; font-size: 10px; background: transparent; }}
    #LogView {{ background: {pal.panel}; border: 1px solid {pal.line};
                border-radius: 8px; color: {pal.text_faint}; font-size: 10px; }}

    /* -------------------------------------------------------------- misc */
    #Crumbs {{ background: transparent; }}
    #Crumb {{ background: transparent; border: none; border-radius: 7px;
              padding: 4px 8px; color: {pal.text_dim}; font-size: 11px; }}
    #Crumb:hover {{ background: {pal.panel2}; color: {pal.text}; }}
    #Crumb[current="true"] {{ background: {pal.accent_soft}; color: {pal.text};
                              font-weight: bold; }}
    #ListHead {{ color: {pal.text_faint}; font-size: 10px; background: transparent; }}
    #StatusBar {{ color: {pal.text_faint}; font-size: 10px; background: transparent; }}

    QProgressBar {{ background: {pal.panel2}; border: none; border-radius: 3px;
                    height: 5px; text-align: center; }}
    QProgressBar::chunk {{ background: {pal.accent}; border-radius: 3px; }}

    QCheckBox {{ background: transparent; }}
    QMenu {{ background: {pal.panel}; border: 1px solid {pal.line}; color: {pal.text}; }}
    QMenu::item:selected {{ background: {pal.accent_soft}; }}
    QToolTip {{ background: {pal.panel}; color: {pal.text};
                border: 1px solid {pal.line}; padding: 4px 6px; }}
    """
