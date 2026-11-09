"""The preview dialog: what an item is, before downloading it.

A downloader that only shows titles forces a guess: "Week 3 notes" could be a
paragraph or a 40 MB scan. This dialog shows type, file name, dates and the
rendered body for documents and announcements, lists the files embedded in the
body, and opens any of them on demand - PDF/Excel in the system's own viewer,
images inline.

The text logic lives in `bbpull.preview` (pure, Qt-free); this module only
turns it into widgets. Fetching an attachment is delegated to the window, which
downloads it through the authenticated session into a small temp cache.
"""

import html
from urllib.parse import urljoin

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..catalog import KIND_LABELS
from ..desktop import open_in_file_manager
from ..preview import (
    is_image_name,
    item_attachments,
    item_body_markdown,
    item_metadata,
    placeholder_text,
)
from .theme import ui_font


class PreviewDialog(QDialog):
    """Metadata + body preview for one catalog node."""

    def __init__(self, node, pal, path_text="", parent=None,
                 on_download=None, link_url="", base_url="",
                 on_preview_attachment=None, on_preview_file=None):
        super().__init__(parent)
        self.node = node
        self.pal = pal
        self._on_download = on_download
        self._base_url = base_url or ""
        self._on_preview_attachment = on_preview_attachment
        self._on_preview_file = on_preview_file

        title = (node.get("title") or "").strip() or "(未命名項目)"
        self.setWindowTitle(f"預覽 — {title}")
        self.setMinimumSize(760, 600)
        self.resize(820, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(8)

        heading = QLabel(title)
        heading.setFont(ui_font(15, QFont.Bold))
        heading.setWordWrap(True)
        layout.addWidget(heading)

        badge = QLabel(KIND_LABELS.get(node.get("kind"),
                                       node.get("kind") or "項目"))
        badge.setStyleSheet(
            f"color: {pal.accent}; background: {pal.accent_soft};"
            f" border-radius: 6px; padding: 1px 8px;")
        badge_row = QHBoxLayout()
        badge_row.addWidget(badge)
        badge_row.addStretch(1)
        layout.addLayout(badge_row)

        meta = QLabel("<br>".join(
            f"<span style='color:{pal.text_faint}'>{html.escape(label)}</span>"
            f"　{html.escape(str(value))}"
            for label, value in item_metadata(node, path_text)))
        meta.setWordWrap(True)
        meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(meta)

        # Embedded PDFs/Excel/DOCX are part of the body, not child rows. Each
        # gets a preview button so the file can be opened without downloading
        # the whole item first.
        attachments = item_attachments(node)
        if attachments:
            layout.addWidget(self._attachment_section(attachments))

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet(
            f"background: {pal.panel}; border: 1px solid {pal.line};"
            f" border-radius: 8px; padding: 4px;")
        self.image_label.hide()
        layout.addWidget(self.image_label)

        self.body = QTextBrowser()
        self.body.setObjectName("PreviewBody")
        self.body.setOpenLinks(False)
        self.body.setOpenExternalLinks(False)
        self.body.anchorClicked.connect(self._open_link)
        self.body.setStyleSheet(
            f"QTextBrowser {{ background: {pal.panel}; color: {pal.text};"
            f" border: 1px solid {pal.line}; border-radius: 8px; padding: 10px; }}")
        markdown = item_body_markdown(node)
        if markdown:
            self.body.setMarkdown(markdown)
        else:
            self.body.setPlainText(placeholder_text(node))
        layout.addWidget(self.body, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {pal.text_faint};")
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        actions.addStretch(1)
        if node.get("kind") == "file" and on_preview_file:
            preview = QPushButton("預覽檔案")
            preview.setObjectName("Ghost")
            preview.clicked.connect(self._preview_file)
            actions.addWidget(preview)
        if on_download:
            download = QPushButton("下載此項目")
            download.setObjectName("Primary")
            download.clicked.connect(self._download)
            actions.addWidget(download)
        if link_url:
            opener = QPushButton("在瀏覽器開啟")
            opener.setObjectName("Ghost")
            opener.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(link_url)))
            actions.addWidget(opener)
        close = QPushButton("關閉")
        close.setObjectName("Ghost")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)

    # -- attachments ---------------------------------------------------
    def _attachment_section(self, attachments):
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)

        header = QLabel(f"附件（{len(attachments)}）　按「預覽」用系統程式開啟")
        header.setStyleSheet(f"color: {self.pal.text_faint};")
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMaximumHeight(112)
        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 4, 0)
        rows.setSpacing(2)
        for item in attachments:
            row = QHBoxLayout()
            row.setSpacing(6)
            name = QLabel(item["name"])
            name.setTextInteractionFlags(Qt.TextSelectableByMouse)
            button = QPushButton("預覽")
            button.setObjectName("Ghost")
            button.setFixedWidth(64)
            button.clicked.connect(
                lambda _checked=False, entry=item: self._preview_attachment(entry))
            row.addWidget(name, 1)
            row.addWidget(button)
            rows.addLayout(row)
        rows.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll)
        return box

    def _preview_attachment(self, item):
        if not self._on_preview_attachment:
            return
        self.status.setText(f"正在取得附件：{item['name']} …")
        self._on_preview_attachment(
            item["url"], item["name"], self.node.get("id") or "",
            self._attachment_ready, self._attachment_failed)

    def _preview_file(self):
        if not self._on_preview_file:
            return
        self.status.setText("正在取得檔案 …")
        self._on_preview_file(self.node, self._attachment_ready,
                              self._attachment_failed)

    def _attachment_ready(self, path, name):
        if is_image_name(name):
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                width = max(240, min(700, pixmap.width()))
                self.image_label.setPixmap(
                    pixmap.scaledToWidth(width, Qt.SmoothTransformation))
                self.image_label.show()
                self.status.setText(f"圖片預覽：{name}")
                return
        opened = open_in_file_manager(path)
        self.status.setText(
            f"已用系統程式開啟：{name}" if opened else f"已下載：{path}")

    def _attachment_failed(self, message):
        self.status.setText(f"無法預覽：{message}")

    # -- actions -------------------------------------------------------
    def _download(self):
        if self._on_download:
            self._on_download()
        self.accept()

    def _open_link(self, url):
        target = url.toString()
        if not target.startswith(("http://", "https://", "mailto:")):
            if not self._base_url:
                return
            target = urljoin(self._base_url.rstrip("/") + "/", target)
        if target.startswith(("http://", "https://", "mailto:")):
            QDesktopServices.openUrl(QUrl(target))
