"""The Qt main window.

Same feature set as the Tk build - file-manager navigation, cross-folder batch
selection, single-item download, year/term filters, jobs panel, theme switch -
but the list is a virtualised `QListView` + model + delegate instead of one
widget per row. That single change is what removes the stutter.
"""

import os
import webbrowser

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..catalog import Catalog, catalog_cache_path
from ..cli import fetch_courses
from ..errors import ApiError, ConfigError, LoginError
from ..selective import JobManager, course_dir_for, run_download_job
from ..session import LearnSession
from ..gui.courses import (
    ANY,
    UNSPECIFIED,
    CourseFilter,
    available_terms,
    available_years,
    parse_courses,
    summarise,
    term_label,
    year_label,
)
from . import theme
from .delegates import CourseDelegate, RowDelegate
from .models import CatalogModel, CourseListModel, NodeRole
from .workers import JobPoller, TaskRunner

APP_NAME = "bbpull"
PAGE_LIST = 0
PAGE_PLACEHOLDER = 1
PAGE_LOADING = 2


def ui_font(px, weight=QFont.Normal):
    font = QFont("Microsoft JhengHei UI")
    font.setPixelSize(int(px))
    font.setWeight(weight)
    return font


class ContentView(QListView):
    """Content list. Handles hover, checkbox clicks and folder activation.

    Row actions (download/open) are painted by the delegate and dispatched
    through `editorEvent`; this view handles the checkbox and the double-click,
    because those need to know about the *model's* notion of selection.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)          # key perf flag: no per-row measure
        self.setSelectionMode(QListView.NoSelection)  # selection lives in the model
        self.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setEditTriggers(QListView.NoEditTriggers)
        self.setSpacing(0)
        self._on_toggle = None
        self._on_open = None
        self._row_delegate = None

    def wire(self, delegate, on_toggle, on_open):
        self._row_delegate = delegate
        self._on_toggle = on_toggle
        self._on_open = on_open

    def _node_at(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return None, None
        return index, index.data(NodeRole)

    def mouseMoveEvent(self, event):
        index, node = self._node_at(event.position().toPoint())
        if self._row_delegate is None:
            return super().mouseMoveEvent(event)
        row = index.row() if index.isValid() else -1
        button = None
        if node and index.isValid():
            button = self._row_delegate.button_at(self.visualRect(index), node,
                                                  event.position().toPoint())
        changed = self._row_delegate.set_hover(row, button)
        model = self.model()
        if changed and model is not None:
            if isinstance(model, CatalogModel):
                model.hover_id = node["id"] if node else None
                model.dataChanged.emit(
                    model.index(0, 0), model.index(model.rowCount() - 1, 0))
        self.setCursor(Qt.PointingHandCursor if (node and button) else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._row_delegate is not None:
            self._row_delegate.set_hover(-1)
        model = self.model()
        if isinstance(model, CatalogModel):
            model.hover_id = None
            if model.rowCount():
                model.dataChanged.emit(model.index(0, 0),
                                       model.index(model.rowCount() - 1, 0))
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        index, node = self._node_at(event.position().toPoint())
        if node and index.isValid() and self._row_delegate is not None:
            rect = self.visualRect(index)
            check, _icon, _text, buttons = self._row_delegate._layout(rect, node)
            if check.contains(event.position().toPoint()):
                if self._on_toggle:
                    self._on_toggle(node["id"])
                return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        index, node = self._node_at(event.position().toPoint())
        if node and index.isValid() and node["kind"] == "folder" and self._on_open:
            self._on_open(node["id"])
            return
        super().mouseDoubleClickEvent(event)


class CourseView(QListView):
    """Sidebar course list with hover feedback and click activation."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QListView.NoSelection)
        self.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setEditTriggers(QListView.NoEditTriggers)
        self._delegate = None
        self._on_click = None

    def wire(self, delegate, on_click):
        self._delegate = delegate
        self._on_click = on_click

    def mouseMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if self._delegate is not None:
            row = index.row() if index.isValid() else -1
            if self._delegate.set_hover(row):
                model = self.model()
                if model is not None and model.rowCount():
                    model.dataChanged.emit(model.index(0, 0),
                                           model.index(model.rowCount() - 1, 0))
        self.setCursor(Qt.PointingHandCursor if index.isValid() else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._delegate is not None and self._delegate.set_hover(-1):
            model = self.model()
            if model is not None and model.rowCount():
                model.dataChanged.emit(model.index(0, 0),
                                       model.index(model.rowCount() - 1, 0))
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and self._on_click:
            model = self.model()
            meta = model.meta_at(index) if hasattr(model, "meta_at") else None
            if meta is not None:
                self._on_click(meta.course_id)
                return
        super().mouseReleaseEvent(event)


class ConnectDialog(QDialog):
    """Credential dialog. Also usable mid-load, so it never blocks the UI."""

    def __init__(self, cfg, parent=None, on_submit=None):
        super().__init__(parent)
        self.cfg = cfg
        self._on_submit = on_submit
        self.setWindowTitle("連線到 Blackboard")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        title = QLabel("連線到 Blackboard")
        title.setFont(ui_font(15, QFont.Bold))
        layout.addWidget(title)

        hint = QLabel("密碼只會用在這個程式裡，並可選擇加密儲存到你的 Windows 帳號。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa3b8;")
        layout.addWidget(hint)

        self.base_url = QLineEdit(cfg.base_url or "")
        self.username = QLineEdit(cfg.username or "")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        for label, widget in (("站台網址", self.base_url),
                              ("帳號（學號）", self.username),
                              ("密碼", self.password)):
            layout.addWidget(QLabel(label))
            layout.addWidget(widget)

        layout.addWidget(QLabel("密碼保存方式"))
        self.save_mode = QComboBox()
        self.save_mode.addItems(["加密儲存（推薦）", "存進 .env（明文）",
                                 "不要儲存", "維持現狀"])
        layout.addWidget(self.save_mode)

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #f2637b;")
        layout.addWidget(self.error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("稍後再說")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.submit = QPushButton("登入")
        self.submit.setObjectName("Primary")
        self.submit.clicked.connect(self._submit)
        buttons.addWidget(self.submit)
        layout.addLayout(buttons)

        self.password.returnPressed.connect(self._submit)

    def _submit(self):
        user = self.username.text().strip()
        secret = self.password.text()
        if not user or not secret:
            self.error.setText("請輸入帳號與密碼。")
            return
        self.submit.setEnabled(False)
        self.submit.setText("登入中 …")
        self.error.setText("正在登入 …")
        if self._on_submit:
            self._on_submit(self)

    def show_error(self, message):
        self.error.setText(str(message))
        self.submit.setEnabled(True)
        self.submit.setText("登入")

    def values(self):
        return {
            "baseUrl": self.base_url.text().strip(),
            "username": self.username.text().strip(),
            "password": self.password.text(),
            "save": {"加密儲存（推薦）": "secure", "存進 .env（明文）": "env",
                     "不要儲存": "none", "維持現狀": "keep"}[self.save_mode.currentText()],
        }


class MainWindow(QMainWindow):
    def __init__(self, cfg, log=None):
        super().__init__()
        self.cfg = cfg
        self.log = log or (lambda *a: None)
        self.session = None
        self.user = ""
        self.courses = []
        self.metas = []
        self.catalog = None
        self.course_filter = CourseFilter()
        self.out_root = cfg.out_dir or "output"
        self.theme_mode = "dark"
        self.pal = theme.Palette("dark")
        self.jobs = JobManager()
        self._loading = False
        self._parents = {}
        self._selection_ids = set()

        # The window title names the build. The user could not tell the Qt and Tk
        # interfaces apart, which sent a debugging round down the wrong path;
        # this makes the running engine unmistakable.
        self.setWindowTitle("bbpull — Blackboard 課程下載器（Qt）")
        self.resize(1180, 760)
        self.setMinimumSize(940, 580)

        self.tasks = TaskRunner(self)
        self.poller = JobPoller(self.jobs, self)
        self.poller.updated.connect(self._on_jobs)

        self._build_ui()
        self._install_shortcuts()
        self.apply_theme()
        self.show_placeholder()
        self.poller.start()
        QTimer.singleShot(80, self._bootstrap)

    # ------------------------------------------------------------- layout
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_topbar())

        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addWidget(self._build_sidebar())
        middle.addWidget(self._build_workspace(), 1)
        self.jobs_panel = self._build_jobs_panel()
        self.jobs_panel.setMaximumWidth(0)
        middle.addWidget(self.jobs_panel)
        outer.addLayout(middle, 1)

        outer.addWidget(self._build_tray())
        self.status = QLabel("")
        self.status.setObjectName("StatusBar")
        self.status.setContentsMargins(14, 2, 14, 6)
        outer.addWidget(self.status)

    def _icon_button(self, kind, tooltip, command, size=30, name=None):
        """A QToolButton with a painted glyph.

        Every one gets an `objectName`: without it the buttons were anonymous
        `QToolButton` instances, which made them unidentifiable both in the
        inspection log and to QSS selectors.
        """
        button = QToolButton()
        if name:
            button.setObjectName(name)
        button.setIcon(theme.make_icon(kind, self.pal.text_dim, size * 2))
        button.setIconSize(QSize(size - 12, size - 12))
        button.setFixedSize(size, size)
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(command)
        return button

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(48)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 10, 6)
        layout.setSpacing(8)

        mark = QLabel("bb")
        mark.setObjectName("BrandMark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(26, 26)
        layout.addWidget(mark)

        brand = QLabel("bbpull")
        brand.setObjectName("BrandText")
        layout.addWidget(brand)

        self.chip = QLabel("未連線")
        self.chip.setObjectName("Chip")
        self.chip.setProperty("state", "off")
        layout.addWidget(self.chip)
        layout.addStretch(1)

        self.btn_output = self._icon_button("download", "開啟下載資料夾",
                                           self.reveal_output, name="BtnOutput")
        self.btn_jobs = self._icon_button("refresh", "下載進度", self.toggle_jobs,
                                         name="BtnJobs")
        self.btn_theme = self._icon_button("moon", "切換亮／暗色", self.toggle_theme,
                                           name="BtnTheme")
        self.btn_connect = self._icon_button("account", "帳號", self.open_connect,
                                             name="BtnConnect")
        for button in (self.btn_connect, self.btn_theme, self.btn_jobs, self.btn_output):
            layout.addWidget(button)
        return bar

    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(276)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(7)

        head = QHBoxLayout()
        title = QLabel("我的課程")
        title.setObjectName("SidebarTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_reload = self._icon_button(
            "refresh", "重新整理課程清單", lambda: self.load_courses(force=True),
            size=24, name="BtnReload")
        head.addWidget(self.btn_reload)
        layout.addLayout(head)

        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.year_combo = QComboBox()
        self.year_combo.addItem(year_label(ANY))
        self.year_combo.currentTextChanged.connect(self._on_year)
        self.term_combo = QComboBox()
        self.term_combo.addItem(term_label(ANY))
        self.term_combo.currentTextChanged.connect(self._on_term)
        filters.addWidget(self.year_combo, 1)
        filters.addWidget(self.term_combo, 1)
        layout.addLayout(filters)

        self.course_search = QLineEdit()
        self.course_search.setPlaceholderText("搜尋課程名稱或編號…")
        self.course_search.textChanged.connect(self._on_course_text)
        layout.addWidget(self.course_search)

        self.course_model = CourseListModel(self)
        self.course_delegate = CourseDelegate(self.pal, self, on_click=self.load_catalog)
        self.course_view = CourseView()
        self.course_view.setModel(self.course_model)
        self.course_view.setItemDelegate(self.course_delegate)
        self.course_view.wire(self.course_delegate, self.load_catalog)
        layout.addWidget(self.course_view, 1)

        self.course_footer = QLabel("")
        self.course_footer.setObjectName("SidebarFooter")
        layout.addWidget(self.course_footer)

        self.out_label = QLabel("")
        self.out_label.setObjectName("SidebarFooter")
        self.out_label.setWordWrap(True)
        layout.addWidget(self.out_label)
        return side

    def _build_workspace(self):
        work = QWidget()
        layout = QVBoxLayout(work)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # -- toolbar ----------------------------------------------------
        toolbar = QFrame()
        toolbar.setObjectName("TopBar")
        toolbar.setFixedHeight(46)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(4)

        self.btn_back = self._icon_button("chevron_left", "上一頁 (Alt+←)", self.go_back, 28, name="BtnBack")
        self.btn_fwd = self._icon_button("chevron_right", "下一頁 (Alt+→)", self.go_forward, 28, name="BtnForward")
        self.btn_up = self._icon_button("chevron_up", "上一層 (Alt+↑)", self.go_up, 28, name="BtnUp")
        for button in (self.btn_back, self.btn_fwd, self.btn_up):
            row.addWidget(button)

        self.crumbs = QWidget()
        self.crumbs_layout = QHBoxLayout(self.crumbs)
        self.crumbs_layout.setContentsMargins(6, 0, 0, 0)
        self.crumbs_layout.setSpacing(2)
        row.addWidget(self.crumbs, 1)

        self.filter_entry = QLineEdit()
        self.filter_entry.setPlaceholderText("篩選…")
        self.filter_entry.setFixedWidth(150)
        self.filter_entry.textChanged.connect(self._on_item_text)
        row.addWidget(self.filter_entry)

        self.scope_combo = QComboBox()
        self.scope_combo.addItems(["本資料夾", "整門課"])
        self.scope_combo.setFixedWidth(92)
        self.scope_combo.currentTextChanged.connect(self._on_scope)
        row.addWidget(self.scope_combo)

        row.addWidget(self._icon_button("tick", "選取本頁全部 (Ctrl+A)",
                                        self.select_visible, 28, name="BtnSelectAll"))
        row.addWidget(self._icon_button("refresh", "重新讀取課程結構 (F5)",
                                        self.refresh_catalog, 28, name="BtnRefresh"))
        layout.addWidget(toolbar)

        # -- progress ---------------------------------------------------
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setRange(0, 0)          # indeterminate until told otherwise
        self.progress.hide()
        layout.addWidget(self.progress)

        self.list_head = QLabel("")
        self.list_head.setObjectName("ListHead")
        self.list_head.setContentsMargins(16, 7, 16, 3)
        layout.addWidget(self.list_head)

        # -- stacked pages ----------------------------------------------
        self.stack = QStackedWidget()
        self.content_model = CatalogModel(self)
        self.row_delegate = RowDelegate(self.pal, self, on_button=self._on_row_button)
        self.content_view = ContentView()
        self.content_view.setModel(self.content_model)
        self.content_view.setItemDelegate(self.row_delegate)
        self.content_view.wire(self.row_delegate, self.toggle_select, self.navigate)
        self.stack.addWidget(self.content_view)

        self.placeholder = self._build_placeholder()
        self.stack.addWidget(self.placeholder)

        self.loading_page = self._build_loading()
        self.stack.addWidget(self.loading_page)

        layout.addWidget(self.stack, 1)
        return work

    def _build_placeholder(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        self.ph_icon = QLabel()
        self.ph_icon.setAlignment(Qt.AlignCenter)
        self.ph_icon.setPixmap(theme.make_icon("folder", self.pal.text_faint, 64)
                               .pixmap(64, 64))
        layout.addWidget(self.ph_icon)
        self.ph_title = QLabel("尚未選擇課程")
        self.ph_title.setAlignment(Qt.AlignCenter)
        self.ph_title.setFont(ui_font(16, QFont.Bold))
        layout.addWidget(self.ph_title)
        self.ph_hint = QLabel("")
        self.ph_hint.setAlignment(Qt.AlignCenter)
        self.ph_hint.setWordWrap(True)
        layout.addWidget(self.ph_hint)
        self.ph_tips = QLabel("")
        self.ph_tips.setAlignment(Qt.AlignCenter)
        self.ph_tips.setWordWrap(True)
        layout.addWidget(self.ph_tips)
        layout.addStretch(1)
        return page

    def _build_loading(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        self.loading_label = QLabel("正在讀取課程結構 …")
        self.loading_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.loading_label)
        self.loading_bar = QProgressBar()
        self.loading_bar.setFixedWidth(360)
        self.loading_bar.setFixedHeight(6)
        self.loading_bar.setRange(0, 0)
        holder = QHBoxLayout()
        holder.addStretch(1)
        holder.addWidget(self.loading_bar)
        holder.addStretch(1)
        layout.addLayout(holder)
        self.loading_sub = QLabel("")
        self.loading_sub.setAlignment(Qt.AlignCenter)
        self.loading_sub.setStyleSheet("color: #6b7488;")
        layout.addWidget(self.loading_sub)
        layout.addStretch(1)
        return page

    def _build_jobs_panel(self):
        panel = QFrame()
        panel.setObjectName("JobsPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(7)

        head = QHBoxLayout()
        title = QLabel("下載進度")
        title.setObjectName("JobsTitle")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self._icon_button("close", "關閉", lambda: self.toggle_jobs(False), 24))
        layout.addLayout(head)

        self.jobs_scroll = QScrollArea()
        self.jobs_scroll.setWidgetResizable(True)
        self.jobs_scroll.setFrameShape(QFrame.NoFrame)
        self.jobs_holder = QWidget()
        self.jobs_layout = QVBoxLayout(self.jobs_holder)
        self.jobs_layout.setContentsMargins(0, 0, 0, 0)
        self.jobs_layout.setSpacing(7)
        self.jobs_layout.addStretch(1)
        self.jobs_scroll.setWidget(self.jobs_holder)
        layout.addWidget(self.jobs_scroll, 1)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(150)
        layout.addWidget(self.log_view)
        return panel

    def _build_tray(self):
        self.tray = QFrame()
        self.tray.setObjectName("Tray")
        self.tray.setFixedHeight(56)
        self.tray.setMaximumWidth(860)
        layout = QHBoxLayout(self.tray)
        layout.setContentsMargins(18, 8, 12, 8)
        layout.setSpacing(8)

        self.tray_count = QLabel("")
        self.tray_count.setObjectName("TrayCount")
        self.tray_sub = QLabel("")
        self.tray_sub.setObjectName("TraySub")
        left = QVBoxLayout()
        left.setSpacing(1)
        left.addWidget(self.tray_count)
        left.addWidget(self.tray_sub)
        layout.addLayout(left)
        layout.addStretch(1)

        view = QPushButton("檢視選取")
        view.setObjectName("Ghost")
        view.clicked.connect(self.show_selection)
        clear = QPushButton("清除")
        clear.setObjectName("Ghost")
        clear.clicked.connect(self.clear_selection)
        download = QPushButton("下載選取項目")
        download.setObjectName("Primary")
        download.clicked.connect(lambda: self.start_download(self.topmost_selection()))
        for button in (view, clear, download):
            layout.addWidget(button)

        self.tray.hide()
        wrapper = QWidget()
        wrap_layout = QHBoxLayout(wrapper)
        wrap_layout.setContentsMargins(0, 0, 0, 10)
        wrap_layout.addStretch(1)
        wrap_layout.addWidget(self.tray)
        wrap_layout.addStretch(1)
        self.tray_wrapper = wrapper
        return wrapper

    # ------------------------------------------------------------- theme
    def apply_theme(self):
        self.pal.set_mode(self.theme_mode)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(theme.stylesheet(self.pal))
        self.row_delegate.pal = self.pal
        self.course_delegate.pal = self.pal
        self.btn_theme.setIcon(theme.make_icon(
            "sun" if self.theme_mode == "dark" else "moon", self.pal.text_dim, 36))
        self.ph_icon.setPixmap(
            theme.make_icon("folder", self.pal.text_faint, 64).pixmap(64, 64))
        self.content_view.viewport().update()
        self.course_view.viewport().update()

    def toggle_theme(self):
        self.theme_mode = "light" if self.theme_mode == "dark" else "dark"
        self.apply_theme()

    # --------------------------------------------------------- shortcuts
    def _install_shortcuts(self):
        for keys, slot in (
            ("Alt+Left", self.go_back),
            ("Alt+Right", self.go_forward),
            ("Alt+Up", self.go_up),
            ("F5", self.refresh_catalog),
            ("Ctrl+A", self.select_visible),
            ("Ctrl+D", lambda: self.start_download(self.topmost_selection())),
            ("Escape", self._on_escape),
            ("Ctrl+R", lambda: self.load_courses(force=True)),
        ):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(slot)
            self._shortcuts = getattr(self, "_shortcuts", [])
            self._shortcuts.append(shortcut)

    def _on_escape(self):
        if self.filter_entry.text():
            self.filter_entry.clear()
            return
        self.clear_selection()

    # -------------------------------------------------------- bootstrap
    def _bootstrap(self):
        self._update_out_label()
        if self.cfg.has_credentials:
            self.set_status("正在以已儲存的憑證登入 …")
            self.tasks.start(self._do_login, self._on_login, self._on_login_failed)
        else:
            self.open_connect()

    def _do_login(self):
        session = LearnSession(self.cfg, self.log)
        session.login()
        return session

    def _on_login(self, session):
        self.session = session
        self.user = self.cfg.username
        self.render_chip()
        self.set_status(f"已登入：{self.user}")
        if not self.catalog:
            self.show_placeholder()
        self.load_courses()

    def _on_login_failed(self, message):
        self.set_status(f"登入失敗：{message}", error=True)
        self.open_connect()

    def load_courses(self, force=False):
        if not self.session:
            self.open_connect()
            return
        self.set_status("正在讀取課程清單 …")
        self.tasks.start(
            lambda: fetch_courses(self.session, self.log, cfg=self.cfg),
            self._on_courses, lambda m: self.set_status(f"讀取課程清單失敗：{m}", error=True))

    def _on_courses(self, courses):
        self.courses = courses or []
        self.metas = parse_courses(self.courses)
        self._sync_filters()
        self.render_courses()
        self.render_chip()

    def _sync_filters(self):
        years = available_years(self.metas)
        self.year_combo.blockSignals(True)
        self.year_combo.clear()
        self.year_combo.addItem(year_label(ANY))
        for year in years:
            self.year_combo.addItem(year_label(year))
        self.year_combo.setCurrentIndex(0)
        self.year_combo.blockSignals(False)
        self.course_filter.year = ANY
        self._sync_terms()

    def _sync_terms(self):
        terms = available_terms(self.metas, self.course_filter.year)
        self.term_combo.blockSignals(True)
        self.term_combo.clear()
        self.term_combo.addItem(term_label(ANY))
        for term in terms:
            self.term_combo.addItem(term_label(term))
        self.term_combo.setCurrentIndex(0)
        self.term_combo.blockSignals(False)
        self.course_filter.term = ANY

    def _on_year(self, label):
        if label == year_label(ANY):
            self.course_filter.year = ANY
        elif label == year_label(UNSPECIFIED):
            self.course_filter.year = UNSPECIFIED
        else:
            self.course_filter.year = label
        self._sync_terms()
        self.render_courses()

    def _on_term(self, label):
        if label == term_label(ANY):
            self.course_filter.term = ANY
        elif label == term_label(UNSPECIFIED):
            self.course_filter.term = UNSPECIFIED
        else:
            for term in available_terms(self.metas, self.course_filter.year):
                if term_label(term) == label:
                    self.course_filter.term = term
                    break
        self.render_courses()

    def _on_course_text(self, text):
        self.course_filter.text = text or ""
        self.render_courses()

    def filtered_metas(self):
        return [m for m in self.metas if self.course_filter.matches(m)]

    def render_courses(self):
        shown = self.filtered_metas()
        active = self.catalog.course_id if self.catalog else self.cfg.course_id
        self.course_model.set_courses(shown, active)
        summary = summarise(self.metas, shown)
        if self.course_filter.active:
            summary += f"　·　{self.course_filter.describe()}"
        self.course_footer.setText(summary)

    def _update_out_label(self):
        self.out_label.setText(f"輸出：{os.path.abspath(self.out_root)}")

    # ---------------------------------------------------------- catalog
    def load_catalog(self, course_id, refresh=False):
        if not self.session:
            self.open_connect()
            return
        if not course_id:
            return
        self.cfg.course_id = course_id
        self._loading = True
        self.show_loading(f"正在讀取課程結構 {course_id} …")
        self.progress.show()
        self.progress.setRange(0, 0)

        def work():
            cache_path = catalog_cache_path(self.cfg.state_dir, course_id)
            if not refresh:
                cached = Catalog.load(cache_path)
                if cached:
                    return cached
            course_name = course_id
            try:
                detail = self.session.api_get(f"/courses/{course_id}", allow_404=True) or {}
                course_name = detail.get("name") or course_id
            except ApiError:
                pass
            announcements = []
            try:
                from ..announcements import AnnouncementPuller

                puller = AnnouncementPuller(self.session, course_id, "out", self.log)
                puller.fetch()
                announcements = puller.items
            except ApiError:
                pass
            catalog = Catalog.build(self.session, course_id, course_name, self.log,
                                    announce=announcements,
                                    progress=self._progress_bridge)
            try:
                catalog.save(cache_path)
            except OSError:
                pass
            return catalog

        self.tasks.start(work, self._on_catalog, self._on_catalog_failed)

    def _progress_bridge(self, done, total):
        """Called from the walker thread; hop to the UI thread via a timer."""
        fraction = (done / float(total)) if total else 0.0
        QTimer.singleShot(0, lambda: self._set_progress(fraction, done, total))

    def _set_progress(self, fraction, done, total):
        if not self._loading:
            return
        self.progress.setRange(0, 1000)
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        self.loading_bar.setRange(0, 1000)
        self.loading_bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        self.loading_sub.setText(f"{done} / {total} 個資料夾")

    def _on_catalog(self, catalog):
        self._loading = False
        self.catalog = catalog
        self.progress.hide()
        self._parents = {}
        self._index_parents(catalog.tree, None)
        self.content_model.set_catalog(catalog)
        self.content_model.selection.set_tree(catalog.tree, self._parents)
        self._selection_ids = {i for i in self._selection_ids
                               if i in {n["id"] for n in _walk(catalog.tree)}}
        self.content_model.selection.load(self._selection_ids)
        self.content_model.set_folder("root")
        self.render_courses()
        self.render_toolbar()
        self.show_list()
        self.render_content_head()
        self.render_tray()
        self.render_chip()
        self.set_status(f"已載入：{catalog.course_name}（{catalog.stats.get('items', 0)} 個項目）")

    def _on_catalog_failed(self, message):
        self._loading = False
        self.progress.hide()
        self.show_placeholder(error=True, detail=message)
        self.set_status(f"讀取課程結構失敗：{message}", error=True)

    def _index_parents(self, nodes, parent):
        for node in nodes or []:
            self._parents[node["id"]] = parent
            self._index_parents(node.get("children"), node["id"])

    def refresh_catalog(self):
        if self.catalog:
            self.load_catalog(self.catalog.course_id, refresh=True)
        elif self.cfg.course_id:
            self.load_catalog(self.cfg.course_id, refresh=True)

    # ----------------------------------------------------------- pages
    def show_list(self):
        self.stack.setCurrentIndex(PAGE_LIST)

    def show_loading(self, message):
        self.loading_label.setText(message)
        self.loading_bar.setRange(0, 0)
        self.loading_sub.setText("")
        self.stack.setCurrentIndex(PAGE_LOADING)

    def show_placeholder(self, error=False, detail=""):
        if not self.session and not error:
            title = "尚未登入 Blackboard"
            hint = "請先輸入帳號密碼，登入後就能瀏覽課程內容。"
            tips = "· 密碼可選擇用 Windows DPAPI 加密儲存\n· 登入後課程清單會自動載入"
        elif error:
            title = "讀取課程結構失敗"
            hint = detail or "無法取得這門課的內容，可能是權限或網路問題。"
            tips = "· 按 F5 或右上重新整理再試一次\n· 較舊的課程對學生帳號可能不開放"
        else:
            title = "尚未選擇課程"
            hint = "從左邊挑一門課開始瀏覽教材內容。"
            tips = ("· 勾選資料夾會連同底下所有內容一起下載\n"
                    "· 可以在不同資料夾之間累積選取，再一次下載\n"
                    "· Alt+← 返回上一頁，Alt+↑ 回上一層\n"
                    "· 用左上的學年／學期下拉選單快速縮小課程範圍")
        self.ph_title.setText(title)
        self.ph_hint.setText(hint)
        self.ph_hint.setStyleSheet(f"color: {self.pal.text_faint};")
        self.ph_tips.setText(tips)
        self.ph_tips.setStyleSheet(f"color: {self.pal.text_faint}; font-size: 11px;")
        self.stack.setCurrentIndex(PAGE_PLACEHOLDER)

    # ------------------------------------------------------ navigation
    def navigate(self, folder_id):
        if folder_id != "root" and folder_id not in self._parents:
            return
        hist = self._history()
        if not hist or hist[self._hist_index] != folder_id:
            del hist[self._hist_index + 1:]
            hist.append(folder_id)
            self._hist_index = len(hist) - 1
        self.content_model.set_folder(folder_id)
        self.render_toolbar()
        self.render_content_head()

    def go_up(self):
        current = self.content_model.folder
        if current == "root":
            return
        self.navigate(self._parents.get(current) or "root")

    def _history(self):
        if not hasattr(self, "_hist"):
            self._hist = ["root"]
            self._hist_index = 0
        return self._hist

    def go_back(self):
        self._history()
        if self._hist_index <= 0:
            return
        self._hist_index -= 1
        self.navigate(self._hist[self._hist_index])

    def go_forward(self):
        self._history()
        if self._hist_index >= len(self._hist) - 1:
            return
        self._hist_index += 1
        self.navigate(self._hist[self._hist_index])

    def render_toolbar(self):
        while self.crumbs_layout.count():
            item = self.crumbs_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        def add_crumb(label, target, current=False):
            button = QPushButton(label if len(label) <= 26 else label[:25] + "…")
            button.setObjectName("Crumb")
            button.setProperty("current", "true" if current else "false")
            button.setCursor(Qt.PointingHandCursor)
            button.setFlat(True)
            button.clicked.connect(lambda: self.navigate(target))
            self.crumbs_layout.addWidget(button)

        if self.catalog:
            current = self.content_model.folder
            add_crumb("課程根目錄", "root", current == "root")
            for node in self._path_chain(current):
                sep = QLabel("›")
                sep.setStyleSheet(f"color: {self.pal.text_faint};")
                self.crumbs_layout.addWidget(sep)
                add_crumb(node["title"], node["id"], node["id"] == current)
        self.crumbs_layout.addStretch(1)
        self.btn_back.setEnabled(self._hist_index > 0 if hasattr(self, "_hist_index") else False)
        self.btn_fwd.setEnabled(
            hasattr(self, "_hist_index") and self._hist_index < len(self._history()) - 1)
        self.btn_up.setEnabled(self.content_model.folder != "root")

    def _path_chain(self, node_id):
        chain = []
        current = None if node_id == "root" else node_id
        while current:
            node = self.catalog.find(current) if self.catalog else None
            if not node:
                break
            chain.insert(0, node)
            current = self._parents.get(current)
        return chain

    # ------------------------------------------------------- content
    def render_content_head(self):
        if not self.catalog:
            self.list_head.setText("")
            return
        summary = self.selection_summary()
        total = self.content_model.rowCount()
        text = (f"{total} 個項目   ·   已選 {len(summary['top'])} 項"
                f"（約 {summary['items']} 個檔案）")
        if self.filter_entry.text():
            text += "   ·   已套用篩選"
        self.list_head.setText(text)

    def _on_item_text(self, text):
        self.content_model.set_filter(text or "")
        self.render_content_head()

    def _on_scope(self, value):
        self.content_model.scope_all = (value == "整門課")
        self.content_model.set_filter(self.filter_entry.text() or "")
        self.render_content_head()

    # ------------------------------------------------------ selection
    def topmost_selection(self):
        return self.content_model.selection.topmost()

    def selection_summary(self):
        top = self.topmost_selection()
        items = 0
        for node_id in top:
            node = self.catalog.find(node_id) if self.catalog else None
            if node:
                items += node.get("itemCount") or 1
        return {"top": top, "items": items}

    def toggle_select(self, node_id):
        children_of = None
        if self.catalog:
            children_of = lambda nid: (self.catalog.find(nid) or {}).get("children") or []
        self.content_model.selection.toggle(node_id, children_of=children_of)
        self._selection_ids = self.content_model.selection.ids()
        self.content_model.refresh()
        self.render_tray()
        self.render_content_head()

    def select_visible(self):
        model = self.content_model
        for row in range(model.rowCount()):
            node = model.node_at(row)
            if node:
                model.selection._selected.add(node["id"])
        self._selection_ids = model.selection.ids()
        model.refresh()
        self.render_tray()
        self.render_content_head()

    def clear_selection(self):
        self.content_model.selection.clear()
        self._selection_ids = set()
        self.content_model.refresh()
        self.render_tray()
        self.render_content_head()

    def render_tray(self):
        summary = self.selection_summary()
        if not summary["top"]:
            self.tray.hide()
            return
        folders = sum(1 for i in summary["top"]
                      if (self.catalog.find(i) or {}).get("kind") == "folder")
        places = len({self._parents.get(i) or "root" for i in summary["top"]})
        self.tray_count.setText(f"已選 {len(summary['top'])} 個項目")
        sub = f"約 {summary['items']} 個檔案"
        if folders:
            sub += f"   ·   含 {folders} 個資料夾"
        sub += f"   ·   來自 {places} 個位置"
        self.tray_sub.setText(sub)
        self.tray.show()
        self._animate_tray_in()

    def _animate_tray_in(self):
        start = self.tray.height()
        anim = QPropertyAnimation(self.tray, b"maximumHeight", self)
        anim.setDuration(160)
        anim.setStartValue(max(0, start - 14))
        anim.setEndValue(56)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def show_selection(self):
        summary = self.selection_summary()
        if not summary["top"]:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("已選取的項目")
        dialog.resize(640, 520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"{len(summary['top'])} 個項目，"
                                f"預估 {summary['items']} 個檔案。"
                                f"資料夾會連同底下所有內容一起下載。"))
        area = QScrollArea()
        area.setWidgetResizable(True)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        for node_id in summary["top"]:
            node = self.catalog.find(node_id) or {}
            trail = " › ".join(n["title"] for n in self._path_chain(node_id)[:-1]) \
                or "課程根目錄"
            row = QHBoxLayout()
            row.addWidget(QLabel(trail), 1)
            row.addWidget(QLabel(node.get("title", "")))
            row.addWidget(QLabel(f"{node.get('itemCount') or 1} 項"))
            remove = QPushButton("移除")
            remove.setObjectName("Ghost")
            remove.clicked.connect(
                lambda _c, nid=node_id, d=dialog: (self.toggle_select(nid), d.accept()))
            row.addWidget(remove)
            holder_layout.addLayout(row)
        holder_layout.addStretch(1)
        area.setWidget(holder)
        layout.addWidget(area, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        close = QPushButton("關閉")
        close.setObjectName("Ghost")
        close.clicked.connect(dialog.reject)
        actions.addWidget(close)
        clear = QPushButton("全部清除")
        clear.setObjectName("Ghost")
        clear.clicked.connect(lambda: (self.clear_selection(), dialog.accept()))
        actions.addWidget(clear)
        download = QPushButton("下載這些項目")
        download.setObjectName("Primary")
        download.clicked.connect(
            lambda: (dialog.accept(), self.start_download(self.topmost_selection())))
        actions.addWidget(download)
        layout.addLayout(actions)
        dialog.exec()

    # ------------------------------------------------------- downloads
    def _on_row_button(self, action, node):
        if action == "open":
            self.navigate(node["id"])
        else:
            self.start_download([node["id"]])

    def start_download(self, ids):
        if not self.session:
            self.open_connect()
            return
        if not self.catalog:
            self.set_status("請先選擇並載入一門課程", error=True)
            return
        ids = [i for i in (ids or []) if self.catalog.find(i)]
        if not ids:
            self.set_status("沒有選取任何項目", error=True)
            return
        target = course_dir_for(self.out_root, self.catalog)
        job = self.jobs.create("download", f"下載 {len(ids)} 個選取項目")
        job.note(f"輸出位置：{os.path.abspath(target)}")
        self.toggle_jobs(True)
        self.jobs.start(job, run_download_job, self.session, self.catalog, list(ids),
                        target, False)
        self.set_status(f"開始下載 {len(ids)} 個項目 → {os.path.abspath(target)}")

    def toggle_jobs(self, show=None):
        visible = self.jobs_panel.maximumWidth() > 0
        target = 340 if (show is None and not visible) or show is True else 0
        if show is False:
            target = 0
        anim = QPropertyAnimation(self.jobs_panel, b"maximumWidth", self)
        anim.setDuration(180)
        anim.setStartValue(self.jobs_panel.maximumWidth())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _on_jobs(self, snapshots):
        while self.jobs_layout.count() > 1:
            item = self.jobs_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        if not snapshots:
            label = QLabel("還沒有下載工作")
            label.setStyleSheet(f"color: {self.pal.text_faint};")
            self.jobs_layout.insertWidget(0, label)
        for snap in snapshots:
            self.jobs_layout.insertWidget(self.jobs_layout.count() - 1,
                                          self._job_card(snap))
        latest = self.jobs.latest()
        if latest:
            with latest.lock:
                lines = [e["m"] for e in list(latest.log)[-160:]]
            self.log_view.setPlainText("\n".join(lines))

    def _job_card(self, snap):
        card = QFrame()
        card.setObjectName("JobCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(11, 9, 11, 9)
        layout.setSpacing(5)

        head = QHBoxLayout()
        label = QLabel(snap["label"])
        label.setObjectName("JobLabel")
        head.addWidget(label, 1)
        state_text = {"running": "進行中", "pending": "排隊中", "done": "完成",
                      "error": "失敗", "cancelled": "已取消"}.get(snap["state"], "")
        state = QLabel(state_text)
        colour = {"running": self.pal.accent, "done": self.pal.ok,
                  "error": self.pal.err, "cancelled": self.pal.warn}.get(
                      snap["state"], self.pal.text_dim)
        state.setStyleSheet(f"color: {colour};")
        head.addWidget(state)
        layout.addLayout(head)

        bar = QProgressBar()
        bar.setFixedHeight(6)
        bar.setTextVisible(False)
        bar.setRange(0, 100)
        bar.setValue(int(snap.get("percent") or 0))
        layout.addWidget(bar)

        bits = [f"{snap.get('processed', 0)}/{snap.get('total') or '?'}"]
        for key, text in (("filesSaved", "新檔案"), ("filesSkipped", "已存在"),
                          ("filesFailed", "失敗")):
            if snap.get(key):
                bits.append(f"{text} {snap[key]}")
        if snap.get("bytes"):
            bits.append(_human_bytes(snap["bytes"]))
        meta = QLabel("  ·  ".join(bits))
        meta.setObjectName("JobMeta")
        layout.addWidget(meta)

        if snap.get("current"):
            current = QLabel(f"正在處理：{snap['current']}")
            current.setObjectName("JobMeta")
            layout.addWidget(current)
        for err in (snap.get("errors") or [])[:3]:
            item = QLabel(f"· {err.get('where')}: {err.get('error')}")
            item.setWordWrap(True)
            item.setStyleSheet(f"color: {self.pal.err}; font-size: 10px;")
            layout.addWidget(item)

        actions = QHBoxLayout()
        actions.addStretch(1)
        if snap["state"] in ("running", "pending"):
            cancel = QPushButton("取消")
            cancel.setObjectName("Ghost")
            cancel.clicked.connect(lambda _c, j=snap["id"]: self.jobs.cancel(j))
            actions.addWidget(cancel)
        if snap["state"] in ("done", "cancelled"):
            reveal = QPushButton("開啟資料夾")
            reveal.setObjectName("Ghost")
            reveal.clicked.connect(self.reveal_output)
            actions.addWidget(reveal)
        layout.addLayout(actions)
        return card

    # ------------------------------------------------------------ misc
    def render_chip(self):
        if not self.session:
            self.chip.setText("未連線")
            self.chip.setProperty("state", "off")
        elif self.catalog:
            stats = self.catalog.stats
            text = f"{self.user}  ·  {self.catalog.course_name}  ·  {stats.get('items', 0)} 項目"
            if stats.get("announcements"):
                text += f"  ·  {stats['announcements']} 公告"
            self.chip.setText(text)
            self.chip.setProperty("state", "ok")
        else:
            self.chip.setText(f"{self.user}  ·  尚未選擇課程")
            self.chip.setProperty("state", "dim")
        self.chip.style().unpolish(self.chip)
        self.chip.style().polish(self.chip)

    def reveal_output(self):
        path = self.out_root
        if self.catalog:
            path = course_dir_for(self.out_root, self.catalog)
        path = os.path.abspath(path)
        while path and not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - user-initiated, their own path
            else:
                webbrowser.open(f"file://{path}")
            self.set_status(f"已開啟：{path}")
        except OSError as exc:
            self.set_status(f"無法開啟資料夾：{exc}", error=True)

    def set_status(self, message, error=False):
        self.status.setText(message)
        self.status.setStyleSheet(
            f"color: {self.pal.err if error else self.pal.text_faint}; font-size: 10px;")
        self.log(message)

    # --------------------------------------------------------- connect
    def open_connect(self):
        dialog = ConnectDialog(self.cfg, self, on_submit=self._submit_connect)
        self._connect_dialog = dialog
        dialog.open()

    def _submit_connect(self, dialog):
        values = dialog.values()

        def work():
            from ..config import normalize_base_url
            from ..gui.wizard import clear_env_password, env_value, update_env_file

            cfg = self.cfg
            if values["baseUrl"]:
                cfg.base_url = normalize_base_url(values["baseUrl"])
            cfg.username = values["username"]
            cfg.password = values["password"]
            session = LearnSession(cfg, self.log)
            session.login(force=True)

            note = "未變更"
            mode = values["save"]
            if mode == "secure":
                backend = cfg.secret_store().save(
                    {"BB_USERNAME": cfg.username, "BB_PASSWORD": cfg.password})
                clear_env_password(cfg.env_file)
                note = f"密碼已加密儲存（{backend}）"
            elif mode == "env":
                update_env_file(cfg.env_file,
                                {"BB_USERNAME": env_value(cfg.username),
                                 "BB_PASSWORD": env_value(cfg.password)})
                cfg.secret_store().clear()
                note = "密碼已寫入 .env"
            elif mode == "none":
                cfg.secret_store().clear()
                note = "未儲存密碼"
            return session, note

        def ok(payload):
            session, note = payload
            try:
                dialog.accept()
            except RuntimeError:
                pass
            self._on_login(session)
            self.set_status(f"已登入：{self.user}（{note}）")

        def fail(message):
            try:
                dialog.show_error(message)
            except RuntimeError:
                pass

        self.tasks.start(work, ok, fail)

    def closeEvent(self, event):
        try:
            self.poller.stop()
            self.tasks.cancel_all()
            self.jobs.cancel()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


def _walk(nodes):
    for node in nodes or []:
        yield node
        yield from _walk(node.get("children"))


def _human_bytes(num):
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def run_gui(cfg, log=None, inspect=False, inspect_dir=None):
    """Create the Qt application and window. Returns the exit code.

    `inspect=True` turns on click inspection: every click is recorded with the
    widget under the cursor, its resolved style state, and a magnified crop, so a
    UI complaint can be diagnosed from ground truth instead of screenshots.
    """
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")          # consistent base for the QSS to sit on
    window = MainWindow(cfg, log)
    window.show()
    if inspect:
        from .inspect import Inspector

        target = inspect_dir or os.path.join(os.getcwd(), "_debug")
        window.inspector = Inspector(window, target, log)
    return app.exec()
