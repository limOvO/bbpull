"""bbpull desktop GUI — a standalone customtkinter application.

No browser, no server, no web assets: a real Tk window that runs on its own.

Performance contract
--------------------
Two measured facts drive the rendering design here:

* Building a row from customtkinter widgets cost ~16 ms each (1921 ms for 120
  rows); building it from plain tk widgets costs ~1.8 ms (216 ms). Rows are
  therefore plain tk widgets (`widgets.ItemRow`).
* The old code rebuilt the whole list on every checkbox click: 25 toggles with
  120 rows on screen took **9562 ms**. Selection changes now restyle only the
  rows whose state actually changed - the same 25 toggles take **~34 ms**.

Animations are all colour/geometry tweens on tk widgets (no widget churn), run
through one `Animator` that owns every `after` callback, so closing the window
can never leave a callback pointing at a destroyed widget.

Threading contract
------------------
Tk is not thread-safe, so **every** widget mutation happens on the main thread.
Slow work (login, catalog discovery, downloads) runs on worker threads that only
push messages onto `self.queue`; `_pump` drains that queue from a Tk timer.
Workers receive their session explicitly instead of reading mutable state.
"""

import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from collections import OrderedDict

import customtkinter as ctk

from ..catalog import ANNOUNCEMENTS_ID, Catalog, catalog_cache_path, walk
from ..errors import ApiError, ConfigError, LoginError
from ..logging_util import wrap_logger
from ..selective import JobManager, course_dir_for, run_download_job
from ..session import LearnSession
from .anim import Animator, Shimmer, Spinner, lerp_color, stagger_delays
from .courses import (
    ANY,
    UNSPECIFIED,
    CourseFilter,
    available_terms,
    available_years,
    group_by_year,
    parse_courses,
    summarise,
    term_label,
    year_label,
)
from .theme import (
    IconPainter,
    KIND_LABELS,
    Palette,
    pixel_font,
    pick_font,
    ui_scale,
)
from .widgets import (
    CanvasButton,
    CourseCard,
    EmptyState,
    IconButton,
    ItemRow,
    SectionHeader,
    SkeletonList,
    round_rect,
)

WINDOW_TITLE = "bbpull — Blackboard 課程下載器"
MAX_ROWS = 300          # keep the widget count bounded; the filter narrows further
PUMP_MS = 90
STAGGER_MS = 240        # total window over which rows appear
ROW_FADE_MS = 150


class BbpullApp(ctk.CTk):
    def __init__(self, cfg, log=None):
        super().__init__()
        self.cfg = cfg
        self.log = wrap_logger(log)
        self._closing = False

        # ---- data ------------------------------------------------------
        self.queue = queue.Queue()
        self.session = None
        self.user = ""
        self.courses = []
        self.metas = []
        self.course_spec = CourseFilter()
        self.catalog = None
        self.by_id = {}
        self.parent_of = {}
        self.cwd = "root"
        self.history = ["root"]
        self.h_index = 0
        self.selection = set()
        self.item_filter = ""
        self.scope = "folder"
        self.jobs = JobManager()
        self.job_snapshots = []
        self.out_root = cfg.out_dir or "output"

        # ---- view state ------------------------------------------------
        self._rows = OrderedDict()      # node id -> ItemRow (only visible ones)
        self._row_order = []            # node ids currently built
        self._cards = []
        self._placeholder = None
        self._placeholder_host = None
        self._skeleton = None
        self._loading_course_id = None
        self._handler_errors = []
        self._filter_token = None
        self._course_token = None
        self._tray_token = None
        self._jobs_token = None
        self._sweep_token = None
        # Initialised here, not only inside _start_sweep: reading them before the
        # first sweep raised AttributeError on the Tk app object.
        self._sweep_running = False
        self._sweep_fraction = None
        self._sweep_pos = -0.25
        self._spinner = None
        self.tray_visible = False
        self.jobs_visible = False

        # ---- theme + window --------------------------------------------
        self.pal = Palette("dark")
        self.theme_mode = "dark"
        # Named so the running build is identifiable (see gui_qt/window.py).
        self.title(f"{WINDOW_TITLE}（Tk）")
        self.scale = ui_scale(self)
        self.configure(fg_color=self.pal.bg)
        self._fonts = self._build_fonts()
        self.animator = Animator(self)
        self.shimmer = Shimmer(self.pal, self.animator)
        self.protocol("WM_DELETE_WINDOW", self._shutdown)

        self.geometry(f"{self.px(1120)}x{self.px(720)}")
        self.minsize(self.px(900), self.px(560))

        self._build_topbar()
        self._build_body()
        self._build_tray()
        self._build_status()

        self.bind("<Alt-Left>", lambda _e: self.go_back())
        self.bind("<Alt-Right>", lambda _e: self.go_forward())
        self.bind("<Alt-Up>", lambda _e: self.go_up())
        self.bind("<F5>", lambda _e: self.refresh_catalog())
        self.bind("<Control-a>", lambda _e: self.select_visible())
        self.bind("<Control-d>", lambda _e: self.start_download(self.topmost_selection()))
        self.bind("<Escape>", lambda _e: self.clear_filter())

        self._pump_id = self.after(PUMP_MS, self._pump)
        self._bootstrap_id = self.after(90, self._bootstrap)

    # ------------------------------------------------------------- fonts
    def px(self, value):
        """Scale a logical pixel size for this display.

        One scale factor for every size in the UI, so the canvas-drawn widgets
        and the text stay in proportion on any DPI.
        """
        return max(1, int(round(value * getattr(self, "scale", 1.0))))

    def _build_fonts(self):
        """All sizes are in pixels, scaled once by `self.scale`.

        Point sizes were the root of the inconsistency: Tk multiplies them by
        `tk scaling` (about 2x here), so a 13pt brand label wanted 42px while the
        24px logo canvas next to it stayed literal. Pixels make both agree.
        """
        px = self.px
        return {
            "brand": pixel_font(self, px(15), "bold"),
            "brand_sm": pixel_font(self, px(12), "bold"),
            "h1": pixel_font(self, px(14), "bold"),
            "body": pixel_font(self, px(11)),
            "body_bold": pixel_font(self, px(11), "bold"),
            "small": pixel_font(self, px(10)),
            "tiny": pixel_font(self, px(9)),
            "badge": pixel_font(self, px(8), "bold"),
            "section": pixel_font(self, px(9), "bold"),
            "empty_title": pixel_font(self, px(15), "bold"),
            "mono": ("Consolas", -(px(9))),
        }

    # ------------------------------------------------------------ topbar
    def _build_topbar(self):
        """Compact bar.

        Measured at 76px before this was trimmed - and the reason it would not
        shrink was that customtkinter applies its *own* DPI scaling on top of the
        process DPI awareness set in `run_gui`, double-scaling every CTk widget
        (a 13pt brand label requested 42px) while the plain `tk` canvases stayed
        literal pixels. The two systems disagreed, so sizes looked arbitrary and
        oversized. `run_gui` now pins customtkinter's scaling to 1.0 and these
        numbers are real pixels.
        """
        pal = self.pal
        px = self.px
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=pal.bg_elev)
        bar.pack(fill="x", side="top")
        self.topbar = bar

        left = tk.Frame(bar, bg=pal.bg_elev)
        left.pack(side="left", padx=(px(12), 0), pady=px(6))
        mark_size = px(24)
        mark = tk.Canvas(left, width=mark_size, height=mark_size,
                         highlightthickness=0, bd=0, bg=pal.bg_elev)
        mark.pack(side="left")
        round_rect(mark, 1, 1, mark_size - 1, mark_size - 1, px(7),
                   fill=pal.accent, outline="")
        mark.create_text(mark_size / 2, mark_size / 2, text="bb", fill="#ffffff",
                         font=pixel_font(self, px(9), "bold"))
        tk.Label(left, text="bbpull", font=self._fonts["brand_sm"], bg=pal.bg_elev,
                 fg=pal.text).pack(side="left", padx=(px(8), 0))

        right = tk.Frame(bar, bg=pal.bg_elev)
        right.pack(side="right", padx=px(10), pady=px(5))
        self.btn_output = IconButton(right, pal, IconPainter.download,
                                     size=px(28), icon_size=px(15),
                                     bg=pal.bg_elev, command=self.reveal_output)
        self.btn_output.pack(side="right", padx=px(1))
        self.btn_jobs = IconButton(right, pal, IconPainter.refresh,
                                   size=px(28), icon_size=px(15),
                                   bg=pal.bg_elev, command=self.toggle_jobs)
        self.btn_jobs.pack(side="right", padx=px(1))
        self.btn_theme = IconButton(right, pal, IconPainter.moon,
                                    size=px(28), icon_size=px(15),
                                    bg=pal.bg_elev, command=self.toggle_theme)
        self.btn_theme.pack(side="right", padx=px(1))
        self.btn_connect = IconButton(
            right, pal,
            lambda c, col: IconPainter.account(c, col, size=px(15)),
            size=px(28), icon_size=px(15), bg=pal.bg_elev,
            command=self.open_connect)
        self.btn_connect.pack(side="right", padx=px(1))

        self.chip = tk.Label(bar, text="", font=self._fonts["tiny"],
                             bg=pal.chip, fg=pal.text_dim, anchor="w",
                             padx=px(10), pady=px(3))
        self.chip.pack(side="left", padx=px(6))

    # -------------------------------------------------------------- body
    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self.body = body
        self._build_sidebar(body)
        self._build_workspace(body)
        self._build_jobs_panel(body)

    def _build_sidebar(self, body):
        pal = self.pal
        side = ctk.CTkFrame(body, width=272, corner_radius=0, fg_color=pal.side)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.sidebar = side

        head = ctk.CTkFrame(side, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(head, text="我的課程", font=self._fonts["tiny"],
                     text_color=pal.text_faint, anchor="w").pack(side="left")
        IconButton(head, pal, IconPainter.refresh, size=26, icon_size=14,
                   bg=pal.side, command=lambda: self.load_courses(force=True)
                   ).pack(side="right")

        # -- year / term filters ---------------------------------------
        filters = ctk.CTkFrame(side, fg_color="transparent")
        filters.pack(fill="x", padx=10)
        menu_style = dict(height=28, corner_radius=8, font=self._fonts["tiny"],
                          fg_color=pal.card, button_color=pal.card,
                          button_hover_color=pal.card_hover, text_color=pal.text_dim,
                          dropdown_fg_color=pal.card, dropdown_text_color=pal.text,
                          dropdown_hover_color=pal.card_hover)
        self.year_menu = ctk.CTkOptionMenu(filters, values=["全部學年"], width=122,
                                           command=self._on_year, **menu_style)
        self.year_menu.pack(side="left")
        self.term_menu = ctk.CTkOptionMenu(filters, values=["全部學期"], width=122,
                                           command=self._on_term, **menu_style)
        self.term_menu.pack(side="right")

        self.course_search = ctk.CTkEntry(
            side, placeholder_text="搜尋課程名稱或編號…", height=30, corner_radius=8,
            font=self._fonts["tiny"], fg_color=pal.card, border_color=pal.card_border,
            text_color=pal.text, border_width=1)
        self.course_search.pack(fill="x", padx=10, pady=(8, 4))
        self.course_search.bind("<KeyRelease>", self._on_course_filter)

        self.course_list = ctk.CTkScrollableFrame(
            side, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=pal.scroll,
            scrollbar_button_hover_color=pal.text_faint)
        self.course_list.pack(fill="both", expand=True, padx=4, pady=(2, 2))

        self.course_footer = ctk.CTkLabel(
            side, text="", font=self._fonts["tiny"], text_color=pal.text_faint,
            anchor="w", justify="left", wraplength=210)
        self.course_footer.pack(fill="x", padx=12, pady=(2, 2))
        self.out_label = ctk.CTkLabel(
            side, text="", font=self._fonts["badge"], text_color=pal.text_faint,
            anchor="w", justify="left", wraplength=210)
        self.out_label.pack(fill="x", padx=12, pady=(0, 10))

    def _build_workspace(self, body):
        pal = self.pal
        work = ctk.CTkFrame(body, corner_radius=0, fg_color=pal.bg)
        work.pack(side="left", fill="both", expand=True)
        self.workspace = work

        toolbar = ctk.CTkFrame(work, corner_radius=0, fg_color=pal.bg_elev)
        toolbar.pack(fill="x")
        self.toolbar = toolbar

        nav = ctk.CTkFrame(toolbar, fg_color="transparent")
        nav.pack(side="left", padx=(10, 4), pady=7)
        self.btn_back = IconButton(nav, pal,
                                   lambda c, col: IconPainter.chevron(c, "left", col),
                                   command=self.go_back)
        self.btn_back.pack(side="left")
        self.btn_fwd = IconButton(nav, pal,
                                  lambda c, col: IconPainter.chevron(c, "right", col),
                                  command=self.go_forward)
        self.btn_fwd.pack(side="left")
        self.btn_up = IconButton(nav, pal,
                                 lambda c, col: IconPainter.chevron(c, "up", col),
                                 command=self.go_up)
        self.btn_up.pack(side="left")

        self.crumb_bar = ctk.CTkFrame(toolbar, fg_color="transparent")
        self.crumb_bar.pack(side="left", fill="x", expand=True, padx=6)

        tools = ctk.CTkFrame(toolbar, fg_color="transparent")
        tools.pack(side="right", padx=10, pady=7)
        self.filter_var = tk.StringVar()
        self.filter_entry = ctk.CTkEntry(
            tools, placeholder_text="篩選…", width=150, height=30, corner_radius=8,
            font=self._fonts["small"], fg_color=pal.panel2, border_color=pal.line,
            text_color=pal.text, border_width=1, textvariable=self.filter_var)
        self.filter_entry.pack(side="left", padx=(0, 6))
        self.filter_entry.bind("<KeyRelease>", self._on_item_filter)

        self.scope_menu = ctk.CTkOptionMenu(
            tools, values=["本資料夾", "整門課"], width=88, height=30, corner_radius=8,
            font=self._fonts["small"], fg_color=pal.panel2, button_color=pal.panel2,
            button_hover_color=pal.hover, text_color=pal.text_dim,
            dropdown_fg_color=pal.panel, dropdown_text_color=pal.text,
            dropdown_hover_color=pal.hover, command=self._on_scope)
        self.scope_menu.pack(side="left", padx=(0, 6))
        IconButton(tools, pal, IconPainter.check,
                   command=self.select_visible).pack(side="left")
        IconButton(tools, pal, IconPainter.refresh,
                   command=self.refresh_catalog).pack(side="left")

        # -- indeterminate progress sweep ------------------------------
        self.sweep = tk.Canvas(work, height=3, highlightthickness=0, bd=0,
                               bg=pal.bg_elev)
        self.sweep_block = self.sweep.create_rectangle(0, 0, 0, 3, fill=pal.accent,
                                                       outline="")

        self.list_head = tk.Label(work, text="", bg=pal.bg, fg=pal.text_faint,
                                  font=self._fonts["tiny"], anchor="w")
        self.list_head.pack(fill="x", padx=16, pady=(9, 2))

        self.list_holder = tk.Frame(work, bg=pal.bg)
        self.list_holder.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        self.scroll = ctk.CTkScrollableFrame(
            self.list_holder, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=pal.scroll,
            scrollbar_button_hover_color=pal.text_faint)
        self.scroll.pack(fill="both", expand=True)
        self.list_frame = self.scroll

    def _build_jobs_panel(self, body):
        pal = self.pal
        panel = ctk.CTkFrame(body, width=0, corner_radius=0, fg_color=pal.bg_elev)
        self.jobs_panel = panel
        self.jobs_target_width = 330
        self.jobs_width = 0

        jhead = ctk.CTkFrame(panel, fg_color="transparent")
        jhead.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(jhead, text="下載進度", font=self._fonts["tiny"],
                     text_color=pal.text_faint).pack(side="left")
        IconButton(jhead, pal, IconPainter.close, size=26, icon_size=13,
                   command=lambda: self.toggle_jobs(False)).pack(side="right")
        self.jobs_box = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=pal.scroll)
        self.jobs_box.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.activity_box = ctk.CTkTextbox(
            panel, height=140, corner_radius=8, font=self._fonts["mono"],
            fg_color=pal.panel, text_color=pal.text_faint, border_width=1,
            border_color=pal.line, wrap="word")
        self.activity_box.pack(fill="x", padx=8, pady=(0, 8))
        self.activity_box.configure(state="disabled")
        self._activity_text = ""

    # -------------------------------------------------------------- tray
    def _build_tray(self):
        pal = self.pal
        self.tray = tk.Frame(self, bg=pal.panel, highlightthickness=1,
                             highlightbackground=pal.line)
        inner = tk.Frame(self.tray, bg=pal.panel)
        inner.pack(fill="both", expand=True, padx=16, pady=10)

        self.tray_count = tk.Label(inner, text="", bg=pal.panel, fg=pal.text,
                                   font=self._fonts["body_bold"], anchor="w")
        self.tray_count.pack(side="left")
        self.tray_sub = tk.Label(inner, text="", bg=pal.panel, fg=pal.text_faint,
                                 font=self._fonts["tiny"], anchor="w")
        self.tray_sub.pack(side="left", padx=12)

        self.tray_download = CanvasButton(
            inner, pal, "下載選取項目", width=126, variant="primary",
            font=self._fonts["body_bold"],
            command=lambda: self.start_download(self.topmost_selection()))
        self.tray_download.pack(side="right")
        self.tray_clear = CanvasButton(
            inner, pal, "清除", width=60, variant="ghost", font=self._fonts["small"],
            command=self.clear_selection)
        self.tray_clear.pack(side="right", padx=6)
        self.tray_view = CanvasButton(
            inner, pal, "檢視選取", width=82, variant="ghost", font=self._fonts["small"],
            command=self.show_selection)
        self.tray_view.pack(side="right")

    def _build_status(self):
        self.status = tk.Label(self, text="", bg=self.pal.bg, fg=self.pal.text_faint,
                               font=self._fonts["tiny"], anchor="w")
        self.status.pack(fill="x", side="bottom", padx=16, pady=(0, 6))

    # ---------------------------------------------------------- bootstrap
    def _bootstrap(self):
        self._bootstrap_id = None
        if self._closing:
            return
        self._apply_theme_colors()
        self.render_courses()
        self.render_placeholder()
        self.set_status(f"輸出資料夾：{os.path.abspath(self.out_root)}")
        if self.cfg.has_credentials:
            self.set_status("正在以已儲存的憑證登入 …")
            self._start_sweep()
            threading.Thread(target=self._worker_login, daemon=True).start()
        else:
            self.open_connect()

    def _worker_login(self):
        try:
            session = LearnSession(self.cfg, self.log)
            session.login()
        except (LoginError, ConfigError) as exc:
            self.queue.put(("error", f"登入失敗：{exc}"))
            self.queue.put(("need_credentials", None))
            self.queue.put(("stop_sweep", None))
            return
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", f"登入失敗：{exc}"))
            self.queue.put(("stop_sweep", None))
            return
        self.queue.put(("session", session))
        self.queue.put(("status", f"已登入：{self.cfg.username}"))
        self._worker_courses(session)

    def _worker_courses(self, session=None):
        from ..cli import fetch_courses

        session = session or self.session
        if session is None:
            self.queue.put(("error", "尚未登入，無法讀取課程清單"))
            self.queue.put(("stop_sweep", None))
            return
        try:
            courses = fetch_courses(session, self.log, cfg=self.cfg)
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", f"讀取課程清單失敗：{exc}"))
            self.queue.put(("stop_sweep", None))
            return
        self.queue.put(("courses", courses))
        self.queue.put(("stop_sweep", None))

    def _worker_catalog(self, course_id, refresh=False, session=None):
        session = session or self.session
        if session is None:
            self.queue.put(("error", "尚未登入，無法讀取課程結構"))
            self.queue.put(("catalog_failed", None))
            return
        cache_path = catalog_cache_path(self.cfg.state_dir, course_id)
        if not refresh:
            cached = Catalog.load(cache_path)
            if cached:
                self.queue.put(("catalog", cached))
                self.queue.put(("status", f"已載入快取的課程結構：{cached.course_name}"))
                return
        job = self.jobs.create("catalog", f"讀取課程結構 {course_id}")
        self.queue.put(("jobs", None))

        def work(j):
            from ..announcements import AnnouncementPuller

            course_name = course_id
            try:
                detail = session.api_get(f"/courses/{course_id}", allow_404=True) or {}
                course_name = detail.get("name") or course_id
            except ApiError:
                pass
            j.note("讀取課程大綱 …")
            announcements = []
            try:
                puller = AnnouncementPuller(session, course_id, "out", j.note)
                puller.fetch()
                announcements = puller.items
                j.note(f"讀到 {len(announcements)} 則公告")
            except ApiError as exc:
                j.note(f"公告讀取失敗：{exc}")
            j.check_cancelled()

            def progress(done, total):
                """Feed the walker's folder counts to both indicators."""
                j.total = total
                j.processed = done
                self.queue.put(("catalog_progress", (done, total)))

            catalog = Catalog.build(session, course_id, course_name, j.note,
                                    announce=announcements, progress=progress)
            try:
                catalog.save(cache_path)
            except OSError as exc:
                j.note(f"無法寫入快取：{exc}")
            j.total = max(1, catalog.stats.get("items", 0))
            j.processed = j.total
            j.note(f"完成：{catalog.stats.get('items', 0)} 個項目")
            return catalog

        def runner(j):
            try:
                result = work(j)
            except Exception:  # noqa: BLE001 - reported by the job itself
                self.queue.put(("catalog_failed", None))
                raise
            self.queue.put(("catalog", result))

        self.jobs.start(job, runner)

    def _worker_download(self, ids, session=None):
        session = session or self.session
        catalog = self.catalog
        if session is None or catalog is None:
            self.queue.put(("error", "尚未登入或尚未載入課程結構"))
            return
        target = course_dir_for(self.out_root, catalog)
        job = self.jobs.create("download", f"下載 {len(ids)} 個選取項目")
        job.note(f"輸出位置：{os.path.abspath(target)}")
        self.queue.put(("jobs", None))
        self.jobs.start(job, run_download_job, session, catalog, list(ids), target, False)
        self.queue.put(("status", f"開始下載 {len(ids)} 個項目 → {os.path.abspath(target)}"))

    # --------------------------------------------------------------- pump
    def _pump(self):
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                try:
                    self._handle(kind, payload)
                except Exception as exc:  # noqa: BLE001
                    # A handler must never kill the pump: if it did, the queue
                    # would stop draining and every later result would be lost
                    # silently (which is exactly what a stalled catalog looked
                    # like from the outside).
                    self._report_handler_error(kind, exc)
        except queue.Empty:
            pass
        self._tick_jobs()
        self._pump_id = self.after(PUMP_MS, self._pump)

    def _report_handler_error(self, kind, exc):
        import traceback

        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        self._handler_errors.append(f"{kind}: {detail}")
        self.log(f"[gui] 處理 {kind} 時發生錯誤：{detail}")
        try:
            self.set_status(f"內部錯誤（{kind}）：{detail}", error=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            traceback.print_exc()
        except Exception:  # noqa: BLE001
            pass

    def _handle(self, kind, payload):
        if kind == "session":
            self.session = payload
            self.user = self.cfg.username
            self.render_chip()
            self.render_courses()
            # The hint page says "not logged in" until the session arrives; it
            # must be refreshed then, or the user stays logged in while staring
            # at a page claiming the opposite.
            if not self.catalog:
                self.render_placeholder()
        elif kind == "courses":
            self.courses = payload or []
            self.metas = parse_courses(self.courses)
            self._sync_filter_options()
            self.render_courses()
            self.render_chip()
        elif kind == "catalog_progress":
            done, total = payload
            self._set_sweep_progress(done / float(total) if total else 0.0)
        elif kind == "catalog":
            self._adopt_catalog(payload)
        elif kind == "catalog_failed":
            self._stop_skeleton()
            self.render_placeholder(error=True)
        elif kind == "jobs":
            self._tick_jobs(force=True)
        elif kind == "status":
            self.set_status(payload)
        elif kind == "error":
            self.set_status(str(payload), error=True)
            self._stop_sweep()
        elif kind == "stop_sweep":
            self._stop_sweep()
        elif kind == "need_credentials":
            self.open_connect()
        elif kind == "connect_failed":
            if getattr(self, "_connect_fail", None):
                self._connect_fail(str(payload))
        elif kind == "connected":
            session, note = payload
            self._finish_connect(session, note)

    def _adopt_catalog(self, catalog):
        self._loading_course_id = None
        self._stop_skeleton()
        self._stop_sweep()
        self.catalog = catalog
        self.by_id = {n["id"]: n for n in walk(catalog.tree)}
        self.parent_of = {}
        self._index_parents(catalog.tree, None)
        self.selection = {i for i in self.selection if i in self.by_id}
        self.cwd = "root"
        self.history = ["root"]
        self.h_index = 0
        self.item_filter = ""
        self.filter_var.set("")
        self.render_chip()
        self.render_courses()
        self.render_toolbar()
        self.render_content()
        self.render_tray()

    def _index_parents(self, nodes, parent):
        for node in nodes or []:
            self.parent_of[node["id"]] = parent
            self._index_parents(node.get("children"), node["id"])

    def _tick_jobs(self, force=False):
        now = time.time()
        if not force and now - getattr(self, "_last_job_poll", 0) < 0.5:
            return
        self._last_job_poll = now
        snapshots = self.jobs.all_snapshots(6)
        if force or snapshots != self.job_snapshots:
            self.job_snapshots = snapshots
            self._render_jobs()
        if self.jobs_visible:
            self._render_activity()

    def _render_jobs(self):
        pal = self.pal
        for child in self.jobs_box.winfo_children():
            child.destroy()
        if not self.job_snapshots:
            tk.Label(self.jobs_box, text="還沒有下載工作", bg=pal.bg_elev,
                     fg=pal.text_faint, font=self._fonts["small"]).pack(pady=24)
            return
        for snap in self.job_snapshots:
            self._render_job_card(snap)

    def _render_job_card(self, snap):
        pal, fonts = self.pal, self._fonts
        card = tk.Frame(self.jobs_box, bg=pal.panel, highlightthickness=1,
                        highlightbackground=pal.line)
        card.pack(fill="x", pady=4, padx=2)

        head = tk.Frame(card, bg=pal.panel)
        head.pack(fill="x", padx=11, pady=(10, 0))
        tk.Label(head, text=snap["label"], bg=pal.panel, fg=pal.text,
                 font=fonts["body_bold"], anchor="w").pack(side="left")
        state_text = {"running": "進行中", "pending": "排隊中", "done": "完成",
                      "error": "失敗", "cancelled": "已取消"}.get(snap["state"],
                                                                snap["state"])
        colour = {"running": pal.accent, "done": pal.ok, "error": pal.err,
                  "cancelled": pal.warn}.get(snap["state"], pal.text_dim)
        tk.Label(head, text=state_text, bg=pal.panel, fg=colour,
                 font=fonts["tiny"]).pack(side="right")

        bar = tk.Canvas(card, height=6, highlightthickness=0, bd=0, bg=pal.panel)
        bar.pack(fill="x", padx=11, pady=(9, 6))
        track = bar.create_rectangle(0, 0, 0, 6, fill=pal.panel2, outline="")
        fill = bar.create_rectangle(0, 0, 0, 6, fill=pal.accent, outline="")

        def layout(_e=None, b=bar, t=track, f=fill, s=snap):
            width = max(1, b.winfo_width())
            b.coords(t, 0, 0, width, 6)
            pct = max(0.0, min(1.0, (s.get("percent") or 0) / 100.0))
            b.coords(f, 0, 0, width * pct, 6)

        bar.bind("<Configure>", layout)
        layout()

        bits = [f"{snap.get('processed', 0)}/{snap.get('total') or '?'}"]
        for key, label in (("filesSaved", "新檔案"), ("filesSkipped", "已存在"),
                           ("filesFailed", "失敗")):
            if snap.get(key):
                bits.append(f"{label} {snap[key]}")
        if snap.get("bytes"):
            bits.append(human_bytes(snap["bytes"]))
        tk.Label(card, text="  ·  ".join(bits), bg=pal.panel, fg=pal.text_faint,
                 font=fonts["tiny"], anchor="w").pack(fill="x", padx=11)
        if snap.get("current"):
            tk.Label(card, text=f"正在處理：{snap['current']}", bg=pal.panel,
                     fg=pal.text_dim, font=fonts["tiny"], anchor="w"
                     ).pack(fill="x", padx=11, pady=(3, 0))

        errors = snap.get("errors") or []
        if errors:
            text = "\n".join(f"· {e.get('where')}: {e.get('error')}" for e in errors[:4])
            tk.Label(card, text=text, bg=pal.panel, fg=pal.err, font=fonts["tiny"],
                     anchor="w", justify="left", wraplength=280
                     ).pack(fill="x", padx=11, pady=(4, 0))

        actions = tk.Frame(card, bg=pal.panel)
        actions.pack(fill="x", padx=9, pady=(8, 9))
        if snap["state"] in ("running", "pending"):
            CanvasButton(actions, pal, "取消", width=58, variant="ghost",
                         font=fonts["tiny"],
                         command=lambda j=snap["id"]: self.cancel_job(j)
                         ).pack(side="right")
        if snap["state"] in ("done", "cancelled"):
            CanvasButton(actions, pal, "開啟資料夾", width=92, variant="ghost",
                         font=fonts["tiny"], command=self.reveal_output
                         ).pack(side="right")

    def _render_activity(self):
        job = self.jobs.latest()
        lines = []
        if job:
            with job.lock:
                lines = [e["m"] for e in list(job.log)[-160:]]
        text = "\n".join(lines)
        if text == self._activity_text:
            return
        self._activity_text = text
        self.activity_box.configure(state="normal")
        self.activity_box.delete("1.0", "end")
        self.activity_box.insert("1.0", text or "（尚無紀錄）")
        self.activity_box.see("end")
        self.activity_box.configure(state="disabled")

    def set_status(self, message, error=False):
        self.status.configure(text=message,
                              fg=self.pal.err if error else self.pal.text_faint)
        self.log(message)

    # ------------------------------------------------------- progress sweep
    def _start_sweep(self):
        """Show the progress strip. Starts indeterminate (a marquee)."""
        if self._sweep_token is not None or self._sweep_running:
            return
        self.sweep.pack(fill="x", after=self.toolbar)
        self.sweep.configure(bg=self.pal.bg_elev)
        self._sweep_running = True
        self._sweep_pos = -0.25
        self._sweep_fraction = None

        def step():
            if not getattr(self, "_sweep_running", False):
                self._sweep_token = None
                return
            width = max(1, self.sweep.winfo_width())
            if self._sweep_fraction is None:
                block = max(60, int(width * 0.22))
                x = int(self._sweep_pos * width)
                coords = (x, 0, x + block, 3)
            else:
                # Determinate: fill from the left. Real progress beats a marquee
                # for a walk whose length we can actually measure.
                coords = (0, 0, int(width * self._sweep_fraction), 3)
            try:
                self.sweep.coords(self.sweep_block, *coords)
            except tk.TclError:
                self._sweep_token = None
                return
            if self._sweep_fraction is None:
                self._sweep_pos += 0.035
                if self._sweep_pos > 1.1:
                    self._sweep_pos = -0.25
            self._sweep_token = self.animator.after(16, step)

        step()

    def _set_sweep_progress(self, fraction):
        """Switch the strip to determinate mode and set the fill fraction."""
        self._sweep_fraction = max(0.0, min(1.0, float(fraction)))
        if not self._sweep_running:
            self._start_sweep()

    def _stop_sweep(self):
        self._sweep_running = False
        self._sweep_fraction = None
        if self._sweep_token is not None:
            self.animator.cancel(self._sweep_token)
            self._sweep_token = None
        try:
            self.sweep.pack_forget()
        except tk.TclError:
            pass

    # ------------------------------------------------------- sidebar render
    def _sync_filter_options(self):
        """Rebuild the year/term dropdown values from the loaded courses."""
        years = available_years(self.metas)
        year_values = [year_label(ANY)] + [year_label(y) for y in years]
        current_year = self.course_spec.year
        self.year_menu.configure(values=year_values)
        self.year_menu.set(year_label(current_year))
        if current_year not in years and current_year is not ANY:
            self.course_spec.year = ANY
            self.year_menu.set(year_label(ANY))
        terms = available_terms(self.metas, self.course_spec.year)
        term_values = [term_label(ANY)] + [term_label(t) for t in terms]
        self.term_menu.configure(values=term_values)
        if self.course_spec.term not in terms and self.course_spec.term is not ANY:
            self.course_spec.term = ANY
        self.term_menu.set(term_label(self.course_spec.term))

    def _on_year(self, label):
        self.course_spec.year = self._label_to_year(label)
        self._sync_filter_options()
        self.render_courses()

    def _on_term(self, label):
        self.course_spec.term = self._label_to_term(label)
        self.render_courses()

    def _label_to_year(self, label):
        if label == year_label(ANY):
            return ANY
        if label == year_label(UNSPECIFIED):
            return UNSPECIFIED
        return label

    def _label_to_term(self, label):
        if label == term_label(ANY):
            return ANY
        if label == term_label(UNSPECIFIED):
            return UNSPECIFIED
        for term in available_terms(self.metas, self.course_spec.year):
            if term_label(term) == label:
                return term
        return ANY

    def _on_course_filter(self, _event=None):
        """Debounced: typing must not rebuild 90 cards on every keystroke."""
        if self._course_token is not None:
            self.animator.cancel(self._course_token)
        self._course_token = self.animator.after(180, self._apply_course_filter)

    def _apply_course_filter(self):
        self._course_token = None
        self.course_spec.text = self.course_search.get()
        self.render_courses()

    def filtered_metas(self):
        return [m for m in self.metas if self.course_spec.matches(m)]

    def render_courses(self):
        pal, fonts = self.pal, self._fonts
        for child in self.course_list.winfo_children():
            child.destroy()
        self._cards = []

        if not self.metas:
            tk.Label(self.course_list, text="登入後會顯示你的課程",
                     bg=pal.side, fg=pal.text_faint, font=fonts["small"]
                     ).pack(pady=24)
            self.course_footer.configure(text="")
            return

        shown = self.filtered_metas()
        self.course_footer.configure(
            text=summarise(self.metas, shown)
            + (f"　·　{self.course_spec.describe()}" if self.course_spec.active else ""))

        if not shown:
            tk.Label(self.course_list, text="沒有符合條件的課程",
                     bg=pal.side, fg=pal.text_faint, font=fonts["small"]
                     ).pack(pady=24)
            return

        active_id = self.catalog.course_id if self.catalog else self.cfg.course_id
        grouped = group_by_year(shown)
        budget = 160
        for year, items in grouped.items():
            if budget <= 0:
                tk.Label(self.course_list, text="…（其餘請用篩選縮小範圍）",
                         bg=pal.side, fg=pal.text_faint, font=fonts["badge"]
                         ).pack(pady=6)
                break
            if len(grouped) > 1 or year == UNSPECIFIED:
                SectionHeader(self.course_list, pal, fonts,
                              year_label(year), len(items))
            for meta in items[:budget]:
                card = CourseCard(self.course_list, pal, fonts, meta,
                                  self.load_catalog,
                                  selected=(meta.course_id == active_id))
                self._cards.append(card)
                budget -= 1

    def render_all(self):
        """Refresh every region. Cheap enough to call after any state change."""
        self._apply_theme_colors()
        self.render_chip()
        self.render_courses()
        self.render_toolbar()
        self.render_content()
        self.render_tray()
        self._tick_jobs(force=True)

    # ------------------------------------------------------ content render
    def render_placeholder(self, error=False):
        """Show the hint page.

        It is built in `list_holder` (the non-scrolling parent), NOT inside the
        scrollable frame. `place()` does not contribute to a parent's requested
        size, so a placeholder packed into the scroll area collapsed that parent
        to 1px and the centred block landed at y = -132 - i.e. off-screen, making
        every empty state invisible.
        """
        self._clear_content()
        self.scroll.pack_forget()
        title, hint, tips = self._placeholder_content(error)
        self._placeholder_host = tk.Frame(self.list_holder, bg=self.pal.bg)
        self._placeholder_host.pack(fill="both", expand=True)
        self._placeholder = EmptyState(
            self._placeholder_host, self.pal, self._fonts,
            title=title, hint=hint, tips=tips, animator=self.animator)
        self._placeholder.fade_in(self.animator)
        self.list_head.configure(text="")

    def _placeholder_content(self, error=False):
        """(title, hint, tips) for the current empty state."""
        if not self.session:
            return (
                "尚未登入 Blackboard",
                "請先輸入帳號密碼，登入後就能瀏覽課程內容。",
                ("密碼可選擇用 Windows DPAPI 加密儲存",
                 "登入後課程清單會自動載入"),
            )
        if error:
            return (
                "讀取課程結構失敗",
                "無法取得這門課的內容，可能是權限或網路問題。",
                ("按 F5 或右上重新整理再試一次",
                 "較舊的課程對學生帳號可能不開放"),
            )
        return (
            "尚未選擇課程",
            "從左邊挑一門課開始瀏覽教材內容。",
            ("勾選資料夾會連同底下所有內容一起下載",
             "可以在不同資料夾之間累積選取，再一次下載",
             "Alt+← 返回上一頁，Alt+↑ 回上一層",
             "用左上的學年／學期下拉選單快速縮小課程範圍"),
        )

    def _clear_content(self):
        self._stop_skeleton()
        if self._placeholder is not None:
            self._placeholder.destroy()
            self._placeholder = None
        host = getattr(self, "_placeholder_host", None)
        if host is not None:
            try:
                host.destroy()
            except tk.TclError:
                pass
            self._placeholder_host = None
        for row in self._rows.values():
            row.destroy()
        self._rows = OrderedDict()
        self._row_order = []
        for child in self.list_frame.winfo_children():
            child.destroy()

    def _restore_scroll(self):
        """Bring the scrollable list back after a placeholder was shown."""
        if not self.scroll.winfo_ismapped():
            self.scroll.pack(fill="both", expand=True)

    def _show_skeleton(self):
        self._clear_content()
        self.scroll.pack_forget()
        self._placeholder_host = tk.Frame(self.list_holder, bg=self.pal.bg)
        self._placeholder_host.pack(fill="both", expand=True)
        self._skeleton = SkeletonList(self._placeholder_host, self.pal,
                                      self._fonts, count=9, animator=self.animator)
        self._skeleton.start()
        self.shimmer.set_widgets(self._skeleton.widgets())
        self.shimmer.start()
        self.list_head.configure(text="")

    def _stop_skeleton(self):
        self.shimmer.stop()
        if self._skeleton is not None:
            self._skeleton.destroy()
            self._skeleton = None

    def render_content(self):
        """Full rebuild. Only called when the *set* of visible items changes."""
        if not self.catalog:
            self.render_placeholder()
            return
        self._clear_content()
        items = self.visible_items()
        summary = self.selection_summary()
        shown = items[:MAX_ROWS]
        head = (f"{len(items)} 個項目   ·   已選 {len(summary['top'])} 項"
                f"（約 {summary['items']} 個檔案）")
        if self.item_filter:
            head += "   ·   已套用篩選"
        if len(items) > MAX_ROWS:
            head += f"   ·   僅顯示前 {MAX_ROWS} 項，請用篩選縮小範圍"
        self.list_head.configure(text=head)

        if not shown:
            title, hint, tips = (
                "沒有符合的項目" if self.item_filter else "這個資料夾是空的",
                "換個關鍵字，或把右上範圍改成「整門課」。",
                ("Alt+↑ 回到上一層", "清空篩選：Esc"),
            )
            self._placeholder_host = tk.Frame(self.list_holder, bg=self.pal.bg)
            self._placeholder_host.pack(fill="both", expand=True)
            self._placeholder = EmptyState(
                self._placeholder_host, self.pal, self._fonts,
                title=title, hint=hint, tips=tips, animator=self.animator)
            return

        self._restore_scroll()
        for node in shown:
            row = ItemRow(self.list_frame, self.pal, self._fonts, node,
                          on_toggle=self.toggle_select,
                          on_open=self.navigate,
                          on_download=lambda i: self.start_download([i]))
            row.set_icon(node["kind"])
            row.set_meta_text(self._meta_for(node))
            explicit = node["id"] in self.selection
            implied = (not explicit) and self._is_implied(node["id"])
            row.selected, row.implied = explicit, implied
            self._rows[node["id"]] = row
        self._row_order = [n["id"] for n in shown]
        for row in self._rows.values():
            row.apply()
        self._animate_rows_in()

    def _meta_for(self, node):
        bits = [KIND_LABELS.get(node["kind"], "其他")]
        if node["kind"] == "folder" and node.get("itemCount"):
            bits.append(f"{node['itemCount']} 個項目")
        if node.get("hasBody"):
            bits.append("含內文")
        if node.get("synthetic"):
            bits.append("系統")
        if self.scope == "all" and self.item_filter:
            trail = " › ".join(n["title"] for n in self.path_chain(node["id"])[:-1])
            if trail:
                bits.append(trail)
        return "  ·  ".join(bits)

    # -------------------------------------------------------- animations
    def _animate_rows_in(self):
        """Staggered fade so a new folder reads as a transition, not a jump."""
        pal = self.pal
        rows = list(self._rows.values())
        if not rows:
            return
        delays = stagger_delays(len(rows), STAGGER_MS)
        for row, delay in zip(rows, delays):
            target = row.surface()

            def start(r=row, tgt=target):
                if self._closing:
                    return
                self.animator.run(
                    ROW_FADE_MS,
                    lambda eased, raw, r=r, tgt=tgt: r.apply_surface(
                        lerp_color(pal.bg, tgt, eased)),
                    on_done=r.apply)
            self.animator.after(delay, start)

    def _slide_tray(self, show):
        if self._tray_token is not None:
            self.animator.cancel(self._tray_token)
            self._tray_token = None
        pad_bottom_final = 34

        if show and not self.tray_visible:
            self.tray.pack(fill="x", side="bottom", padx=14, pady=(0, 0))
            self.tray_visible = True
            start, end = 0, pad_bottom_final
        elif not show and self.tray_visible:
            start, end = pad_bottom_final, 0
        else:
            return

        def frame(eased, raw, s=start, e=end):
            value = int(s + (e - s) * eased)
            try:
                self.tray.pack_configure(padx=14, pady=(0, value))
            except tk.TclError:
                pass

        def done(s=show):
            if not s and self.tray_visible:
                try:
                    self.tray.pack_forget()
                except tk.TclError:
                    pass
                self.tray_visible = False

        self._tray_token = self.animator.run(180, frame, on_done=done)

    def _animate_jobs_width(self, show):
        if self._jobs_token is not None:
            self.animator.cancel(self._jobs_token)
            self._jobs_token = None
        start = self.jobs_width
        end = self.jobs_target_width if show else 0
        if start == end:
            if not show:
                self.jobs_panel.pack_forget()
            return
        if show and not self.jobs_panel.winfo_ismapped():
            self.jobs_panel.pack(side="right", fill="y")

        def frame(eased, raw, s=start, e=end):
            self.jobs_width = int(s + (e - s) * eased)
            try:
                self.jobs_panel.configure(width=max(1, self.jobs_width))
            except tk.TclError:
                pass

        def done(e=end):
            if e == 0:
                try:
                    self.jobs_panel.pack_forget()
                except tk.TclError:
                    pass

        self._jobs_token = self.animator.run(200, frame, on_done=done)

    # ------------------------------------------------------------ selection
    def _is_implied(self, node_id):
        parent = self.parent_of.get(node_id)
        while parent:
            if parent in self.selection:
                return True
            parent = self.parent_of.get(parent)
        return False

    def _selected_ancestor(self, node_id):
        parent = self.parent_of.get(node_id)
        while parent:
            if parent in self.selection:
                return parent
            parent = self.parent_of.get(parent)
        return None

    def topmost_selection(self):
        return [n for n in self.selection if self._selected_ancestor(n) is None]

    def selection_summary(self):
        top = self.topmost_selection()
        items = 0
        for node_id in top:
            node = self.by_id.get(node_id)
            if node:
                items += node.get("itemCount") or 1
        return {"top": top, "items": items}

    def toggle_select(self, node_id):
        if node_id in self.selection:
            self.selection.discard(node_id)
        elif self._is_implied(node_id):
            ancestor = self._selected_ancestor(node_id)
            self.selection.discard(ancestor)
            for other in walk(self.children_of(ancestor)):
                if other["id"] != node_id and not self._is_descendant(other["id"], node_id):
                    self.selection.add(other["id"])
        else:
            self.selection.add(node_id)
        # Incremental: restyle only the rows whose state actually changed.
        self.refresh_row_states()
        self.render_tray()
        self._update_head_summary()

    def refresh_row_states(self):
        """Recompute selection visuals for visible rows, touching only changes.

        This is the optimisation that removes the stutter: ticking a checkbox
        used to destroy and rebuild every row (~380 ms with 120 rows on screen).
        """
        changed = 0
        for node_id, row in self._rows.items():
            explicit = node_id in self.selection
            implied = (not explicit) and self._is_implied(node_id)
            if row.set_selection(explicit, implied):
                changed += 1
        return changed

    def _update_head_summary(self):
        if not self.catalog:
            return
        summary = self.selection_summary()
        total = len(self.visible_items())
        head = (f"{total} 個項目   ·   已選 {len(summary['top'])} 項"
                f"（約 {summary['items']} 個檔案）")
        if self.item_filter:
            head += "   ·   已套用篩選"
        self.list_head.configure(text=head)

    def _is_descendant(self, candidate, ancestor):
        current = self.parent_of.get(candidate)
        while current:
            if current == ancestor:
                return True
            current = self.parent_of.get(current)
        return False

    def select_visible(self):
        for node in self.visible_items():
            self.selection.add(node["id"])
        self.refresh_row_states()
        self.render_tray()
        self._update_head_summary()

    def clear_selection(self):
        self.selection.clear()
        self.refresh_row_states()
        self.render_tray()
        self._update_head_summary()

    def show_selection(self):
        summary = self.selection_summary()
        if not summary["top"]:
            return
        pal, fonts = self.pal, self._fonts
        win = ctk.CTkToplevel(self)
        win.title("已選取的項目")
        win.geometry("640x540")
        win.configure(fg_color=pal.bg)
        win.transient(self)

        ctk.CTkLabel(win, text="已選取的項目", font=fonts["h1"],
                     text_color=pal.text).pack(anchor="w", padx=18, pady=(16, 2))
        ctk.CTkLabel(
            win,
            text=f"{len(summary['top'])} 個項目，預估 {summary['items']} 個檔案。"
                 f"資料夾會連同底下所有內容一起下載。",
            font=fonts["small"], text_color=pal.text_dim
        ).pack(anchor="w", padx=18, pady=(0, 10))

        box = ctk.CTkScrollableFrame(win, fg_color="transparent",
                                     scrollbar_button_color=pal.scroll)
        box.pack(fill="both", expand=True, padx=12)

        groups = OrderedDict()
        for node_id in summary["top"]:
            trail = " › ".join(n["title"] for n in self.path_chain(node_id)[:-1]) \
                or "課程根目錄"
            groups.setdefault(trail, []).append(self.by_id.get(node_id, {}))

        for trail, nodes in groups.items():
            tk.Label(box, text=trail, bg=pal.bg, fg=pal.text_faint,
                     font=fonts["tiny"], anchor="w").pack(fill="x", padx=6, pady=(8, 3))
            for node in nodes:
                line = tk.Frame(box, bg=pal.panel, highlightthickness=1,
                                highlightbackground=pal.line_soft)
                line.pack(fill="x", pady=1, padx=2)
                icon = tk.Canvas(line, width=18, height=18, highlightthickness=0,
                                 bd=0, bg=pal.panel)
                IconPainter.draw(icon, node.get("kind", "other"),
                                 pal.kind(node.get("kind", "other")), size=18)
                icon.pack(side="left", padx=(8, 8), pady=7)
                tk.Label(line, text=node.get("title", ""), bg=pal.panel, fg=pal.text,
                         font=fonts["small"], anchor="w"
                         ).pack(side="left", fill="x", expand=True)
                tk.Label(line, text=f"{node.get('itemCount') or 1} 項", bg=pal.panel,
                         fg=pal.text_faint, font=fonts["tiny"]).pack(side="left", padx=8)

        actions = ctk.CTkFrame(win, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=14)
        ctk.CTkButton(actions, text="下載這些項目", height=34, corner_radius=9,
                      font=fonts["body_bold"], fg_color=pal.accent,
                      hover_color=pal.accent2, text_color="#ffffff",
                      command=lambda: (win.destroy(),
                                       self.start_download(self.topmost_selection()))
                      ).pack(side="right")
        ctk.CTkButton(actions, text="全部清除", height=34, corner_radius=9,
                      font=fonts["small"], fg_color="transparent",
                      hover_color=pal.hover, text_color=pal.text_dim,
                      border_width=1, border_color=pal.line,
                      command=lambda: (self.clear_selection(), win.destroy())
                      ).pack(side="right", padx=8)
        ctk.CTkButton(actions, text="關閉", height=34, corner_radius=9,
                      font=fonts["small"], fg_color="transparent",
                      hover_color=pal.hover, text_color=pal.text_dim,
                      border_width=1, border_color=pal.line,
                      command=win.destroy).pack(side="right")

    def render_tray(self):
        summary = self.selection_summary()
        if not summary["top"]:
            self._slide_tray(False)
            return
        folders = sum(1 for i in summary["top"]
                      if self.by_id.get(i, {}).get("kind") == "folder")
        places = len({self.parent_of.get(i) or "root" for i in summary["top"]})
        self.tray_count.configure(text=f"已選 {len(summary['top'])} 個項目")
        sub = f"約 {summary['items']} 個檔案"
        if folders:
            sub += f"   ·   含 {folders} 個資料夾"
        sub += f"   ·   來自 {places} 個位置"
        self.tray_sub.configure(text=sub)
        self._slide_tray(True)

    # ------------------------------------------------------------ navigation
    def path_chain(self, node_id):
        chain = []
        current = None if node_id == "root" else node_id
        while current:
            node = self.by_id.get(current)
            if not node:
                break
            chain.insert(0, node)
            current = self.parent_of.get(current)
        return chain

    def children_of(self, node_id):
        if not self.catalog:
            return []
        if node_id == "root":
            return self.catalog.tree
        node = self.by_id.get(node_id)
        return (node or {}).get("children") or []

    def visible_items(self):
        needle = self.item_filter.strip().lower()
        if self.scope == "all" and needle:
            return [n for n in self.by_id.values() if needle in n["title"].lower()]
        base = self.children_of(self.cwd)
        if not needle:
            return base
        return [n for n in base if needle in n["title"].lower()]

    def navigate(self, node_id, push=True):
        """Go to a folder, cross-fading the list so the change reads as motion."""
        if node_id != "root" and node_id not in self.by_id:
            return
        self.cwd = node_id
        if push:
            self.history = self.history[: self.h_index + 1]
            self.history.append(node_id)
            self.h_index = len(self.history) - 1
        self.item_filter = ""
        self.filter_var.set("")
        self.render_toolbar()
        self._fade_out_then(self.render_content)

    def _fade_out_then(self, callback, duration=90):
        """Fade the visible rows to the list background, then run `callback`.

        Fading out before rebuilding gives a real out-then-in transition; without
        it the old rows vanish on one frame and the new ones fade in from a
        blank area, which reads as a flicker rather than a page change.
        """
        rows = [r for r in self._rows.values() if r.frame.winfo_exists()]
        if not rows or duration <= 0:
            callback()
            return
        pal = self.pal
        starts = [(r, r.surface()) for r in rows]

        def frame(eased, _raw):
            for row, origin in starts:
                try:
                    row.apply_surface(lerp_color(origin, pal.bg, eased))
                except tk.TclError:
                    return

        def done():
            if not self._closing:
                callback()

        self.animator.run(duration, frame, on_done=done)

    def go_back(self):
        if self.h_index <= 0:
            return
        self.h_index -= 1
        self.navigate(self.history[self.h_index], push=False)

    def go_forward(self):
        if self.h_index >= len(self.history) - 1:
            return
        self.h_index += 1
        self.navigate(self.history[self.h_index], push=False)

    def go_up(self):
        if self.cwd == "root":
            return
        self.navigate(self.parent_of.get(self.cwd) or "root")

    def clear_filter(self):
        self.item_filter = ""
        self.filter_var.set("")
        self.render_content()

    def _on_item_filter(self, _event=None):
        self.item_filter = self.filter_var.get()
        if self._filter_token is not None:
            self.animator.cancel(self._filter_token)
        self._filter_token = self.animator.after(140, self._apply_item_filter)

    def _apply_item_filter(self):
        self._filter_token = None
        self.render_content()

    def _on_scope(self, value):
        self.scope = "all" if value == "整門課" else "folder"
        self.render_content()

    def render_toolbar(self):
        pal = self.pal
        for child in self.crumb_bar.winfo_children():
            child.destroy()
        if not self.catalog:
            self.btn_back.set_enabled(False)
            self.btn_fwd.set_enabled(False)
            self.btn_up.set_enabled(False)
            return
        CanvasButton(self.crumb_bar, pal, "課程根目錄", width=88,
                     variant="accent" if self.cwd == "root" else "ghost",
                     font=self._fonts["tiny"], bg=pal.bg_elev,
                     command=lambda: self.navigate("root")
                     ).pack(side="left", padx=1)
        for node in self.path_chain(self.cwd):
            tk.Label(self.crumb_bar, text="›", bg=pal.bg_elev, fg=pal.text_faint,
                     font=self._fonts["small"]).pack(side="left", padx=1)
            label = node["title"]
            if len(label) > 24:
                label = label[:23] + "…"
            CanvasButton(self.crumb_bar, pal, label, width=64,
                         variant="accent" if node["id"] == self.cwd else "ghost",
                         font=self._fonts["tiny"], bg=pal.bg_elev,
                         command=lambda nid=node["id"]: self.navigate(nid)
                         ).pack(side="left", padx=1)
        self.btn_back.set_enabled(self.h_index > 0)
        self.btn_fwd.set_enabled(self.h_index < len(self.history) - 1)
        self.btn_up.set_enabled(self.cwd != "root")

    def render_chip(self):
        pal = self.pal
        if not self.session:
            text, colour = "未連線", pal.text_faint
        elif self.catalog:
            stats = self.catalog.stats
            text = (f"{self.user}  ·  {self.catalog.course_name}  ·  "
                    f"{stats.get('items', 0)} 項目")
            if stats.get("announcements"):
                text += f"  ·  {stats['announcements']} 公告"
            colour = pal.ok
        else:
            text, colour = f"{self.user}  ·  尚未選擇課程", pal.text_dim
        self.chip.configure(text=f"  {text}  ", fg=colour, bg=pal.chip)

    # -------------------------------------------------------------- actions
    def load_courses(self, force=False):
        if not self.session:
            self.open_connect()
            return
        self.set_status("正在讀取課程清單 …")
        self._start_sweep()
        threading.Thread(target=self._worker_courses, daemon=True).start()

    def load_catalog(self, course_id, refresh=False):
        if not self.session:
            self.open_connect()
            return
        self.cfg.course_id = course_id
        # Remembered so a theme toggle mid-load can restore the loading state.
        self._loading_course_id = course_id
        self.set_status(f"正在讀取課程結構 {course_id} …")
        self._show_skeleton()
        self._start_sweep()
        threading.Thread(target=self._worker_catalog, args=(course_id, refresh),
                         daemon=True).start()

    def refresh_catalog(self):
        if not self.session:
            return
        if self.catalog:
            self.load_catalog(self.catalog.course_id, refresh=True)
        elif self.cfg.course_id:
            self.load_catalog(self.cfg.course_id, refresh=True)

    def start_download(self, ids):
        if not self.session:
            self.open_connect()
            return
        if not self.catalog:
            self.set_status("請先選擇並載入一門課程", error=True)
            return
        ids = [i for i in (ids or []) if i in self.by_id]
        if not ids:
            self.set_status("沒有選取任何項目", error=True)
            return
        self.toggle_jobs(True)
        threading.Thread(target=self._worker_download, args=(ids,), daemon=True).start()

    def cancel_job(self, job_id):
        if self.jobs.cancel(job_id):
            self.set_status("已要求取消，正在停止 …")
        self._tick_jobs(force=True)

    def toggle_jobs(self, show=None):
        self.jobs_visible = (not self.jobs_visible) if show is None else bool(show)
        self._animate_jobs_width(self.jobs_visible)
        if self.jobs_visible:
            self._tick_jobs(force=True)
            self._render_activity()

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

    # ------------------------------------------------------------- theming
    def toggle_theme(self):
        self.theme_mode = "light" if self.theme_mode == "dark" else "dark"
        self.pal.set_mode(self.theme_mode)
        self.rebuild_theme()

    def rebuild_theme(self):
        """Apply a new palette by rebuilding the widget tree.

        Rebuilding is simpler and more reliable than recolouring every widget in
        place, and it is instant against a real click. Two things must survive:
        the animations (stopped first, so no callback hits a dead widget) and an
        in-flight catalog load. Toggling the theme during loading used to destroy
        the skeleton and replace it with the empty hint page, so the loading
        state vanished and the buttons looked unresponsive.
        """
        loading = self._loading_course_id
        jobs_visible = self.jobs_visible

        self.shimmer.stop()
        self._stop_sweep()
        self.animator.stop_all()
        for token_attr in ("_filter_token", "_course_token", "_tray_token",
                           "_jobs_token", "_sweep_token"):
            setattr(self, token_attr, None)
        for child in self.winfo_children():
            child.destroy()
        self._rows = OrderedDict()
        self._row_order = []
        self._cards = []
        self._placeholder = None
        self._placeholder_host = None
        self._skeleton = None
        self.tray_visible = False
        self.jobs_visible = False
        self.jobs_width = 0

        self.configure(fg_color=self.pal.bg)
        self._fonts = self._build_fonts()
        self.shimmer = Shimmer(self.pal, self.animator)
        self._build_topbar()
        self._build_body()
        self._build_tray()
        self._build_status()
        self.animator.resume()
        self._apply_theme_colors()
        self.render_chip()
        self.render_courses()
        self.render_toolbar()

        if loading:
            # Re-create the loading indicator and resume the progress strip.
            self._show_skeleton()
            self._start_sweep()
            if self.job_snapshots:
                snap = self.job_snapshots[0]
                if snap.get("total"):
                    self._set_sweep_progress(
                        (snap.get("processed") or 0) / float(snap["total"]))
        elif self.catalog:
            self.render_content()
        else:
            self.render_placeholder()
        self.render_tray()
        if jobs_visible:
            self.toggle_jobs(True)

    def _apply_theme_colors(self):
        self.out_label.configure(text=f"輸出：{os.path.abspath(self.out_root)}")
        self.status.configure(bg=self.pal.bg, fg=self.pal.text_faint)
        self.list_head.configure(bg=self.pal.bg, fg=self.pal.text_faint)
        self.sweep.configure(bg=self.pal.bg_elev)
        self.sweep.itemconfigure(self.sweep_block, fill=self.pal.accent)

    # ------------------------------------------------------------- connect
    def open_connect(self):
        pal, fonts = self.pal, self._fonts
        win = ctk.CTkToplevel(self)
        win.title("連線到 Blackboard")
        win.geometry("470x480")
        win.configure(fg_color=pal.bg)
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(win, text="連線到 Blackboard", font=fonts["h1"],
                     text_color=pal.text).pack(anchor="w", padx=22, pady=(20, 2))
        ctk.CTkLabel(win, text="密碼只會用在這個程式裡，並可選擇加密儲存到你的 Windows 帳號。",
                     font=fonts["tiny"], text_color=pal.text_dim, justify="left",
                     wraplength=410).pack(anchor="w", padx=22, pady=(0, 14))

        form = ctk.CTkFrame(win, fg_color="transparent")
        form.pack(fill="x", padx=22)
        entries = {}
        for key, label, secret in (("baseUrl", "站台網址", False),
                                   ("username", "帳號（學號）", False),
                                   ("password", "密碼", True)):
            ctk.CTkLabel(form, text=label, font=fonts["tiny"],
                         text_color=pal.text_dim, anchor="w").pack(fill="x", pady=(6, 3))
            entry = ctk.CTkEntry(form, height=34, corner_radius=8, font=fonts["small"],
                                 fg_color=pal.panel2, border_color=pal.line,
                                 text_color=pal.text, show="•" if secret else "")
            entry.pack(fill="x")
            entries[key] = entry
        entries["baseUrl"].insert(0, self.cfg.base_url or "")
        if self.cfg.username:
            entries["username"].insert(0, self.cfg.username)

        ctk.CTkLabel(form, text="密碼保存方式", font=fonts["tiny"],
                     text_color=pal.text_dim, anchor="w").pack(fill="x", pady=(10, 3))
        save = ctk.CTkOptionMenu(
            form, values=["加密儲存（推薦）", "存進 .env（明文）", "不要儲存", "維持現狀"],
            height=34, corner_radius=8, font=fonts["small"], fg_color=pal.panel2,
            button_color=pal.panel2, button_hover_color=pal.hover, text_color=pal.text,
            dropdown_fg_color=pal.panel, dropdown_text_color=pal.text,
            dropdown_hover_color=pal.hover)
        save.pack(fill="x")

        error = ctk.CTkLabel(win, text="", font=fonts["tiny"], text_color=pal.err,
                             wraplength=410, justify="left")
        error.pack(anchor="w", padx=22, pady=(10, 0))

        actions = ctk.CTkFrame(win, fg_color="transparent")
        actions.pack(fill="x", padx=22, pady=16)
        submit = ctk.CTkButton(actions, text="登入", height=36, corner_radius=9,
                               font=fonts["body_bold"], fg_color=pal.accent,
                               hover_color=pal.accent2, text_color="#ffffff")
        submit.pack(side="right")
        mode_map = {"加密儲存（推薦）": "secure", "存進 .env（明文）": "env",
                    "不要儲存": "none", "維持現狀": "keep"}

        def do_connect():
            base = entries["baseUrl"].get().strip()
            user = entries["username"].get().strip()
            secret = entries["password"].get()
            if not user or not secret:
                error.configure(text="請輸入帳號與密碼。", text_color=pal.err)
                return
            submit.configure(state="disabled", text="登入中 …")
            error.configure(text="正在登入 …", text_color=pal.text_dim)

            def worker():
                from ..config import normalize_base_url
                from ..wizard import clear_env_password, env_value, update_env_file

                target = self.cfg
                try:
                    if base:
                        target.base_url = normalize_base_url(base)
                except ConfigError as exc:
                    self.queue.put(("connect_failed", str(exc)))
                    return
                target.username = user
                target.password = secret
                try:
                    session = LearnSession(target, self.log)
                    session.login(force=True)
                except Exception as exc:  # noqa: BLE001
                    self.queue.put(("connect_failed", str(exc)))
                    return
                mode = mode_map.get(save.get(), "keep")
                note = "未變更"
                try:
                    if mode == "secure":
                        backend = target.secret_store().save(
                            {"BB_USERNAME": user, "BB_PASSWORD": secret})
                        clear_env_password(target.env_file)
                        note = f"密碼已加密儲存（{backend}）"
                    elif mode == "env":
                        update_env_file(target.env_file,
                                        {"BB_USERNAME": env_value(user),
                                         "BB_PASSWORD": env_value(secret)})
                        target.secret_store().clear()
                        note = "密碼已寫入 .env"
                    elif mode == "none":
                        target.secret_store().clear()
                        note = "未儲存密碼"
                except OSError as exc:
                    note = f"儲存失敗：{exc}"
                self.queue.put(("connected", (session, note)))

            threading.Thread(target=worker, daemon=True).start()

        submit.configure(command=do_connect)
        win.bind("<Return>", lambda _e: do_connect())

        def on_fail(message):
            error.configure(text=message, text_color=pal.err)
            submit.configure(state="normal", text="登入")

        self._connect_fail = on_fail

        def on_close():
            self._connect_fail = None
            try:
                win.grab_release()
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", on_close)
        self._connect_close = on_close

    def _finish_connect(self, session, note):
        close = getattr(self, "_connect_close", None)
        self._connect_fail = None
        self._connect_close = None
        if close:
            close()
        self.session = session
        self.user = self.cfg.username
        self.set_status(f"已登入：{self.user}（{note}）")
        self.render_chip()
        self.render_placeholder()
        self.load_courses()

    # ------------------------------------------------------------- shutdown
    def _shutdown(self):
        self._closing = True
        try:
            self.shimmer.stop()
        except Exception:  # noqa: BLE001
            pass
        for attr in ("_pump_id", "_bootstrap_id"):
            timer = getattr(self, attr, None)
            if timer:
                try:
                    self.after_cancel(timer)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, attr, None)
        try:
            self.animator.stop_all()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.jobs.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


def human_bytes(num):
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def enable_dpi_awareness():
    """Tell Windows we handle DPI ourselves, before Tk creates any window.

    This must be paired with pinning customtkinter's scaling to 1.0 (see
    `run_gui`). Leaving customtkinter's own scaling enabled double-scales every
    CTk widget relative to the plain `tk` widgets, which made the top bar and
    toolbar oversized and internally inconsistent.
    """
    if os.name != "nt":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # SYSTEM_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def run_gui(cfg, log=None, notice=None, **_ignored):
    """Create and run the desktop app. Returns the exit code.

    `notice` is shown in the status bar at startup. The CLI uses it to explain a
    fallback from Qt to Tk: a downgrade the user cannot see is how a bug report
    ends up describing the wrong interface.
    `**_ignored` absorbs Qt-only options (`inspect`, `inspect_dir`) so the CLI can
    call either engine through one signature.
    """
    enable_dpi_awareness()
    # One scaling authority: ours. customtkinter must not scale again on top of
    # the process DPI awareness, or CTk widgets and tk widgets disagree.
    ctk.set_widget_scaling(1.0)
    ctk.set_window_scaling(1.0)
    ctk.set_appearance_mode("dark")
    app = BbpullApp(cfg, log)
    if notice:
        app.set_status(str(notice))
    app.mainloop()
    return 0
