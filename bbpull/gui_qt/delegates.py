"""Item delegates: paint rows instead of constructing widgets.

This is the other half of the performance fix. A delegate receives a painter and
a rect for each *visible* row and draws text, icons, checkbox and buttons
directly. There is no widget per row, so a 5,000-row list costs the same as a
5-row one, and hovering/selecting repaints only the affected rectangle.

All geometry lives in `_layout()` so painting and hit-testing can never disagree
about where a button is.
"""

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from ..palette import KIND_LABELS
from . import theme
from .models import NodeRole, SelectedRole, ImpliedRole, HoverRole

ROW_HEIGHT = 46
HEADER_HEIGHT = 30

# -- course card geometry -------------------------------------------------
# Every band is derived from real font metrics and packed top-down. The previous
# version used fixed offsets and they collided: the title band (y 32..62 from the
# row top) ran straight through the subtitle band (y 40..54), so a two-line
# wrapped title was painted over the course id. Deriving from metrics means the
# bands cannot overlap at any DPI or font.
CARD_INSET_X = 6
CARD_INSET_Y = 3
CARD_PADDING = 7
CARD_GAP = 4
TITLE_LINES = 2
CARD_BADGE_H = 18


def ui_font(px, weight=QFont.Normal):
    font = QFont("Microsoft JhengHei UI")
    font.setPixelSize(int(px))
    font.setWeight(weight)
    return font


def wrap_lines(text, metrics, width, max_lines=TITLE_LINES):
    """Break `text` into at most `max_lines` lines that fit `width`.

    Any line that would still overflow is elided, and when text is dropped the
    final line is elided to say so. `drawText` with `TextWordWrap` would happily
    paint outside its rect instead, which is how a long title ended up over the
    subtitle.
    """
    words = str(text or "").split()
    if not words or width <= 0:
        return [""]

    lines = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if current and metrics.horizontalAdvance(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    if len(lines) > max_lines:
        kept = lines[:max_lines]
        merged = kept[-1] + " " + " ".join(lines[max_lines:])
        kept[-1] = metrics.elidedText(merged, Qt.ElideRight, int(width))
        lines = kept

    # A single word can be wider than the card; elide rather than overflow.
    return [line if metrics.horizontalAdvance(line) <= width
            else metrics.elidedText(line, Qt.ElideRight, int(width))
            for line in lines]


def course_card_geometry(rect, scale):
    """Bands for one sidebar course card: (card, badge, title, meta, fonts).

    Pure geometry so `sizeHint` and `paint` cannot disagree, and so the
    no-overlap property is unit-testable without rendering.
    """
    def px(value):
        return max(1, int(round(value * scale)))

    rows = max(1, int(round(rect.height() * scale))) if rect.height() else None
    title_font = ui_font(px(11))
    meta_font = ui_font(px(9))
    badge_font = ui_font(px(9), QFont.Bold)
    tfm = QFontMetricsF(title_font)
    mfm = QFontMetricsF(meta_font)
    bfm = QFontMetricsF(badge_font)

    content_h = (px(CARD_BADGE_H)
                 + px(CARD_GAP)
                 + tfm.height() * TITLE_LINES
                 + px(2)
                 + mfm.height()
                 + 2 * px(CARD_PADDING)
                 + 2 * px(CARD_INSET_Y))
    row_h = rows if rows is not None else int(round(content_h))
    top = rect.y()

    card = QRectF(rect.x() + px(CARD_INSET_X), top + px(CARD_INSET_Y),
                  max(1.0, rect.width() - 2 * px(CARD_INSET_X)),
                  max(1.0, row_h - 2 * px(CARD_INSET_Y)))
    inner = card.adjusted(px(9), px(CARD_PADDING), -px(9), -px(CARD_PADDING))

    y = inner.y()
    badge = QRectF(inner.x(), y, 0.0, px(CARD_BADGE_H))
    y = badge.bottom() + px(CARD_GAP)

    title = QRectF(inner.x(), y, inner.width(), tfm.height() * TITLE_LINES)
    y = title.bottom() + px(2)

    meta = QRectF(inner.x(), y, inner.width(), mfm.height())
    return card, badge, title, meta, (title_font, meta_font, badge_font)


def course_row_height(scale):
    """Exact row height for one card, from the same metric budget as the layout."""
    px = lambda v: max(1, int(round(v * scale)))  # noqa: E731
    tfm = QFontMetricsF(ui_font(px(11)))
    mfm = QFontMetricsF(ui_font(px(9)))
    return int(round(px(CARD_BADGE_H) + px(CARD_GAP)
                     + tfm.height() * TITLE_LINES + px(2) + mfm.height()
                     + 2 * px(CARD_PADDING) + 2 * px(CARD_INSET_Y)))


class RowDelegate(QStyledItemDelegate):
    """Paints one content row: checkbox, icon, title, meta, action buttons."""

    def __init__(self, pal, parent=None, on_button=None, scale=1.0):
        super().__init__(parent)
        self.pal = pal
        self.scale = scale
        self._on_button = on_button
        self._hover_button = None      # (row, action)
        self._hover_row = -1

    # -- metrics ---------------------------------------------------------
    def px(self, value):
        return max(1, int(round(value * self.scale)))

    def sizeHint(self, option, index):
        return QSize(0, self.px(ROW_HEIGHT))

    def _layout(self, rect, node):
        """Geometry for one row. Single source of truth for paint + hit-test."""
        px = self.px
        pad = px(10)
        check = QRect(rect.x() + pad, rect.y() + (rect.height() - px(20)) // 2,
                      px(20), px(20))
        icon = QRect(check.right() + px(10),
                     rect.y() + (rect.height() - px(22)) // 2, px(22), px(22))

        buttons = []
        right = rect.right() - pad
        is_folder = node["kind"] == "folder"
        can_download = bool(node.get("downloadable")) or (
            is_folder and node.get("itemCount"))
        if is_folder:
            width = px(52)
            buttons.append(("open", QRect(right - width, rect.y() + (rect.height() - px(26)) // 2,
                                          width, px(26))))
            right -= width + px(6)
        if can_download:
            width = px(74) if is_folder else px(56)
            buttons.append(("download",
                            QRect(right - width, rect.y() + (rect.height() - px(26)) // 2,
                                  width, px(26))))
            right -= width + px(6)

        text_left = icon.right() + px(12)
        text = QRect(text_left, rect.y(), max(0, right - px(8) - text_left), rect.height())
        return check, icon, text, buttons

    # -- painting --------------------------------------------------------
    def paint(self, painter, option, index):
        node = index.data(NodeRole)
        if not node:
            return
        pal = self.pal
        selected = bool(index.data(SelectedRole))
        implied = bool(index.data(ImpliedRole))
        hovered = bool(index.data(HoverRole))
        px = self.px

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        row = QRectF(option.rect).adjusted(px(2), px(1), -px(2), -px(1))
        radius = px(9)

        if selected:
            surface = QColor(pal.sel)
        elif implied:
            surface = QColor(pal.accent_soft if pal.mode == "dark" else pal.card_sel)
        elif hovered:
            surface = QColor(pal.hover)
        else:
            surface = QColor(pal.panel)

        painter.setPen(Qt.NoPen)
        painter.setBrush(surface)
        painter.drawRoundedRect(row, radius, radius)

        border = QColor(pal.accent_line if (selected or implied) else pal.line_soft)
        pen = QPen(border)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(row.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        check_rect, icon_rect, text_rect, buttons = self._layout(option.rect, node)
        self._paint_check(painter, check_rect, selected, implied)
        theme.draw_icon(painter, node["kind"], QColor(pal.kind(node["kind"])),
                        QRectF(icon_rect))
        self._paint_text(painter, text_rect, node)
        for action, rect in buttons:
            self._paint_button(painter, rect, action, node, index.row())
        painter.restore()

    def _paint_check(self, painter, rect, selected, implied):
        pal = self.pal
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(pal.accent))
            painter.drawRoundedRect(QRectF(rect), 5, 5)
            theme.draw_tick(painter, "#ffffff", QRectF(rect))
        elif implied:
            pen = QPen(QColor(pal.accent_line))
            pen.setWidthF(1.6)
            painter.setPen(pen)
            painter.setBrush(QColor(pal.muted_accent))
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            dash = QPen(QColor(pal.accent))
            dash.setWidthF(2.0)
            dash.setCapStyle(Qt.RoundCap)
            painter.setPen(dash)
            painter.drawLine(QPointF(rect.x() + rect.width() * 0.32, rect.center().y()),
                             QPointF(rect.x() + rect.width() * 0.68, rect.center().y()))
        else:
            pen = QPen(QColor(pal.line))
            pen.setWidthF(1.6)
            painter.setPen(pen)
            painter.setBrush(QColor(pal.panel2))
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        painter.restore()

    def _paint_text(self, painter, rect, node):
        pal = self.pal
        px = self.px
        if rect.width() < px(20) or rect.height() <= 0:
            return
        painter.save()
        title_font = ui_font(px(12))
        meta_font = ui_font(px(10))
        tfm = QFontMetricsF(title_font)
        mfm = QFontMetricsF(meta_font)

        # Stack both lines from their real heights and centre the pair, so they
        # cannot collide the way fixed +/- offsets can at other DPI scales.
        gap = px(1)
        total = tfm.height() + gap + mfm.height()
        top = rect.y() + max(0.0, (rect.height() - total) / 2.0)

        painter.setFont(title_font)
        fm_title = painter.fontMetrics()
        title_band = QRectF(rect.x(), top, rect.width(), tfm.height())
        title = fm_title.elidedText(node["title"], Qt.ElideRight,
                                    int(title_band.width()))
        painter.setPen(QColor(pal.text))
        painter.drawText(title_band, Qt.AlignLeft | Qt.AlignVCenter, title)

        painter.setFont(meta_font)
        meta_band = QRectF(rect.x(), title_band.bottom() + gap,
                           rect.width(), mfm.height())
        meta = painter.fontMetrics().elidedText(self._meta_text(node), Qt.ElideRight,
                                               int(meta_band.width()))
        painter.setPen(QColor(pal.text_faint))
        painter.drawText(meta_band, Qt.AlignLeft | Qt.AlignVCenter, meta)
        painter.restore()

    @staticmethod
    def _meta_text(node):
        bits = [KIND_LABELS.get(node["kind"], "其他")]
        if node["kind"] == "folder" and node.get("itemCount"):
            bits.append(f"{node['itemCount']} 個項目")
        if node.get("hasBody"):
            bits.append("含內文")
        if node.get("synthetic"):
            bits.append("系統")
        return "  ·  ".join(bits)

    def _paint_button(self, painter, rect, action, node, row):
        """Draw one row action button.

        The label depends on the node's kind, which is passed in rather than
        looked up through `self.parent()`: the delegate's parent is the window,
        not the view, so walking up the widget tree raised AttributeError
        mid-paint (and left the painter's save/restore unbalanced).
        """
        pal = self.pal
        px = self.px
        hovered = self._hover_button == (row, action)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if action == "download":
            fill = QColor(pal.muted_accent if hovered else pal.accent_soft)
            text_colour = QColor(pal.accent)
        else:
            fill = QColor(pal.hover if hovered else pal.panel)
            text_colour = QColor(pal.text if hovered else pal.text_dim)
        pen = QPen(QColor(pal.line) if action != "download" else Qt.NoPen)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(rect), px(7), px(7))
        painter.setFont(ui_font(px(10)))
        if action == "open":
            label = "開啟"
        else:
            label = "下載全部" if node.get("kind") == "folder" else "下載"
        painter.setPen(text_colour)
        painter.drawText(rect, Qt.AlignCenter, label)
        painter.restore()

    # -- interaction -----------------------------------------------------
    def set_hover(self, row, button=None):
        if (row, button) == (self._hover_row, self._hover_button):
            return False
        self._hover_row = row
        self._hover_button = (row, button) if button else None
        return True

    def button_at(self, rect, node, pos):
        """Which action button (if any) is under `pos`?"""
        _check, _icon, _text, buttons = self._layout(rect, node)
        for action, button_rect in buttons:
            if button_rect.contains(pos):
                return action
        return None

    def editorEvent(self, event, model, option, index):
        """Delegate-painted buttons handle their own clicks."""
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.MouseButtonRelease and index.isValid():
            node = index.data(NodeRole)
            action = self.button_at(option.rect, node, event.position().toPoint())
            if action and self._on_button:
                self._on_button(action, node)
                return True
        return super().editorEvent(event, model, option, index)


class CourseDelegate(QStyledItemDelegate):
    """Paints a sidebar course card, or a slim year header."""

    def __init__(self, pal, parent=None, on_click=None, scale=1.0):
        super().__init__(parent)
        self.pal = pal
        self.scale = scale
        self._on_click = on_click
        self._hover_row = -1
        self._hover_is_action = False

    def px(self, value):
        return max(1, int(round(value * self.scale)))

    def sizeHint(self, option, index):
        payload = index.data(NodeRole)
        px = self.px
        if payload is None or index.data(ImpliedRole):
            return QSize(0, px(HEADER_HEIGHT))
        return QSize(0, course_row_height(self.scale))

    def paint(self, painter, option, index):
        pal = self.pal
        px = self.px
        payload = index.data(NodeRole)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        if payload is None or index.data(ImpliedRole):
            painter.setFont(ui_font(px(10), QFont.Bold))
            painter.setPen(QColor(pal.text_dim))
            painter.drawText(option.rect.adjusted(px(12), 0, -px(8), 0),
                             Qt.AlignLeft | Qt.AlignVCenter, str(payload))
            painter.restore()
            return

        meta = payload
        selected = index.data(SelectedRole)
        hovered = index.row() == self._hover_row

        card, badge, title_rect, meta_rect, fonts = course_card_geometry(
            option.rect, self.scale)
        title_font, meta_font, badge_font = fonts
        radius = px(9)

        if selected:
            surface = QColor(pal.card_sel)
        elif hovered:
            surface = QColor(pal.card_hover)
        else:
            surface = QColor(pal.card)
        painter.setPen(Qt.NoPen)
        painter.setBrush(surface)
        painter.drawRoundedRect(card, radius, radius)

        pen = QPen(QColor(pal.card_border_sel if selected else pal.card_border))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        # -- badge row: year pill, then the term label to its right ---------
        painter.setFont(badge_font)
        bfm = painter.fontMetrics()
        badge_text = meta.year if meta.year else "未標示學年"
        badge_w = bfm.horizontalAdvance(badge_text) + px(14)
        badge = QRectF(badge.x(), badge.y(), badge_w, badge.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(pal.accent_soft))
        painter.drawRoundedRect(badge, px(8), px(8))
        painter.setPen(QColor(pal.accent))
        painter.drawText(badge, Qt.AlignCenter, badge_text)

        term_rect = QRectF(badge.right() + px(6), badge.y(),
                           max(0.0, card.right() - px(9) - (badge.right() + px(6))),
                           badge.height())
        if term_rect.width() > px(10):
            painter.setFont(meta_font)
            painter.setPen(QColor(pal.text_faint))
            painter.drawText(term_rect, Qt.AlignLeft | Qt.AlignVCenter,
                             painter.fontMetrics().elidedText(
                                 meta.term_label, Qt.ElideRight, int(term_rect.width())))

        # -- title: at most TITLE_LINES lines, never past its band ----------
        painter.setFont(title_font)
        tfm = painter.fontMetrics()
        lines = wrap_lines(meta.display_name, tfm, title_rect.width())
        painter.setPen(QColor(pal.text_dim if meta.unresolved else pal.text))
        line_h = tfm.height()
        for row, line in enumerate(lines):
            band = QRectF(title_rect.x(), title_rect.y() + row * line_h,
                          title_rect.width(), line_h)
            painter.drawText(band, Qt.AlignLeft | Qt.AlignVCenter, line)

        # -- subtitle: its own band, below the title -----------------------
        painter.setFont(meta_font)
        painter.setPen(QColor(pal.text_faint))
        painter.drawText(meta_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(
                             meta.subtitle, Qt.ElideRight, int(meta_rect.width())))
        painter.restore()

    def set_hover(self, row):
        if row == self._hover_row:
            return False
        self._hover_row = row
        return True

    def editorEvent(self, event, model, option, index):
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.MouseButtonRelease:
            meta = index.data(NodeRole)
            if meta is not None and not index.data(ImpliedRole) and self._on_click:
                self._on_click(meta.course_id)
                return True
        return super().editorEvent(event, model, option, index)
