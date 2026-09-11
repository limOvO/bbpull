"""Fast widgets for the desktop GUI.

Perf note (measured, 120 rows, Python 3.13 / Tk 8.6):

    structure                            build      restyle 40 rows
    CTkFrame + inner + CTkButton         1921 ms    193 ms
    tk.Frame + CTkButton                  702 ms     60 ms
    tk.Frame + tk.Label/Canvas            216 ms      1 ms

`CTkFrame` and `CTkLabel` each own an internal canvas for rounded corners and
text drawing, so every restyle repaints them. Rows therefore use plain `tk`
widgets, and every state change is an `itemconfigure`/`configure` on cached
items instead of a delete-and-recreate. customtkinter is still used for the
outer chrome (panels, dialogs, scroll containers) where it costs nothing.

This is why the list no longer stutters when ticking checkboxes.
"""

import tkinter as tk

from .anim import lerp_color

BUTTON_H = 26


def round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """Draw a rounded rectangle (Tk has no native one) and return its item id."""
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=10, **kwargs)


class CheckMark(tk.Canvas):
    """Checkbox drawn on a canvas.

    Items are created once and restyled with `itemconfigure`; the earlier
    delete-and-redraw version was both slower and the cause of a real bug (the
    tick's `delete("all")` erased the box it was supposed to sit on).
    """

    def __init__(self, master, pal, size=20, command=None):
        super().__init__(master, width=size, height=size, highlightthickness=0,
                         bd=0, bg=pal.panel)
        self.pal = pal
        self.size = size
        self.command = command
        self.state = "off"        # off | on | implied
        self.hover = False
        self._box = None
        self._tick = None
        self._dash = None
        self._build()
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.configure(cursor="hand2")
        self.apply()

    def _build(self):
        s = self.size
        pad = 2
        self._box = round_rect(self, pad, pad, s - pad, s - pad, 5,
                               fill=self.pal.panel2, outline=self.pal.line, width=1.6)
        self._tick = self.create_line(
            s * 0.22, s * 0.52, s * 0.42, s * 0.72, s * 0.80, s * 0.28,
            fill="#ffffff", width=max(2.0, s / 6.0), capstyle="round",
            joinstyle="round", state="hidden")
        self._dash = self.create_line(
            s * 0.32, s * 0.5, s * 0.68, s * 0.5, fill=self.pal.accent,
            width=2.0, capstyle="round", state="hidden")

    # -- state -----------------------------------------------------------
    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        self.apply()

    def apply(self, bg=None):
        """Restyle in place. `bg` keeps the canvas flush with its row."""
        pal = self.pal
        if bg is not None:
            self.configure(bg=bg)
        if self.state == "on":
            fill, outline = pal.accent, pal.accent
        elif self.state == "implied":
            fill, outline = pal.muted_accent, pal.accent_line
        else:
            fill = pal.panel2
            outline = pal.accent if self.hover else pal.line
        self.itemconfigure(self._box, fill=fill, outline=outline)
        self.itemconfigure(self._tick,
                           state="normal" if self.state == "on" else "hidden")
        self.itemconfigure(self._dash,
                           state="normal" if self.state == "implied" else "hidden")
        if self.state == "off":
            self.itemconfigure(self._dash,
                               fill=pal.accent if self.hover else pal.text_faint)

    def _click(self, _event=None):
        if self.command:
            self.command()

    def _enter(self, _event=None):
        self.hover = True
        self.apply()

    def _leave(self, _event=None):
        self.hover = False
        self.apply()


class CanvasButton(tk.Canvas):
    """A flat button drawn on one canvas.

    Restyling is a single `itemconfigure`, so hover and disabled states are
    essentially free (measured ~1ms for 40 buttons vs ~60ms for CTkButton).
    """

    def __init__(self, master, pal, text, command=None, width=76, height=BUTTON_H,
                 variant="accent", font=None, radius=7, padx=12, bg=None):
        self.pal = pal
        self.variant = variant
        self.command = command
        self._enabled = True
        self._hover = False
        self._pad = padx
        try:
            probe = tk.font.Font(font=font)
            text_width = probe.measure(text)
        except Exception:  # noqa: BLE001 - font probing is best-effort
            text_width = len(text) * 8
        self.width = max(width, text_width + padx * 2)
        self.height = height
        self._surface = bg or pal.panel
        super().__init__(master, width=self.width, height=self.height,
                        highlightthickness=0, bd=0, bg=self._surface, cursor="hand2")
        self.text = text
        self._shape = None
        self._label = None
        self._build()
        self.apply()
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _build(self):
        w, h = self.width, self.height
        self._shape = round_rect(self, 0, 0, w - 1, h - 1, 7, fill=self.pal.panel2,
                                 outline="")
        self._label = self.create_text(w / 2, h / 2, text=self.text,
                                       fill=self.pal.text_dim)

    def _colors(self):
        """Return (shape fill, label colour).

        Tk has no `transparent` colour name - passing it raises
        `TclError: unknown color name "transparent"`. A ghost button therefore
        fills its shape with the surface it sits on, which looks identical.
        """
        pal = self.pal
        if not self._enabled:
            return self._surface, pal.text_faint
        if self.variant == "accent":
            fill = pal.muted_accent if self._hover else pal.accent_soft
            return fill, pal.accent
        if self.variant == "primary":
            return (pal.accent2 if self._hover else pal.accent), "#ffffff"
        # ghost: blend into the current surface, brighten on hover
        fill = pal.hover if self._hover else self._surface
        return fill, (pal.text if self._hover else pal.text_dim)

    def apply(self, bg=None, font=None):
        fill, fg = self._colors()
        if bg is not None:
            self._surface = bg
        self.configure(bg=self._surface)
        self.itemconfigure(self._shape, fill=fill)
        self.itemconfigure(self._label, fill=fg)
        if font is not None:
            self.itemconfigure(self._label, font=font)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self.apply()

    def set_text(self, text):
        self.text = text
        self.itemconfigure(self._label, text=text)

    def _click(self, _event=None):
        if self._enabled and self.command:
            self.command()

    def _enter(self, _event=None):
        self._hover = True
        self.apply()

    def _leave(self, _event=None):
        self._hover = False
        self.apply()


class IconButton(tk.Canvas):
    """Square icon button.

    The glyph is a child canvas placed on top of this one. Both must carry the
    bindings: measured, the glyph covers 32% of the button, and with bindings
    only on the parent every click that landed on the visible icon was swallowed
    by a widget that did nothing. That is why pressing theme/settings appeared to
    have no effect.
    """

    def __init__(self, master, pal, painter, size=28, icon_size=15, command=None,
                 bg=None, radius=8):
        self.pal = pal
        self.painter = painter
        self.command = command
        self._size = size
        self._icon_size = icon_size
        self._enabled = True
        self._hover = False
        self._surface = bg or pal.bg_elev
        super().__init__(master, width=size, height=size, highlightthickness=0,
                         bd=0, bg=self._surface, cursor="hand2")
        self._shape = round_rect(self, 1, 1, size - 1, size - 1, radius,
                                 fill="", outline="")
        self._icon = tk.Canvas(self, width=icon_size, height=icon_size,
                               highlightthickness=0, bd=0, bg=self._surface,
                               cursor="hand2")
        offset = (size - icon_size) // 2
        self._icon.place(x=offset, y=offset)
        self.apply(bg)
        for widget in (self, self._icon):
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def apply(self, bg=None, kind=None):
        pal = self.pal
        if bg is not None:
            self._surface = bg
        hovered = self._hover and self._enabled
        fill = pal.panel2 if hovered else self._surface
        self.configure(bg=self._surface)
        self._icon.configure(bg=fill)
        self.itemconfigure(self._shape, fill=pal.panel2 if hovered else "")
        color = pal.text if (self._enabled and self._hover) else (
            pal.text_dim if self._enabled else pal.text_faint)
        self._icon.delete("all")
        target = kind or self.painter
        if callable(target):
            target(self._icon, color)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        cursor = "hand2" if self._enabled else "arrow"
        self.configure(cursor=cursor)
        self._icon.configure(cursor=cursor)
        self.apply()

    def _click(self, _event=None):
        if self._enabled and self.command:
            self.command()

    def _enter(self, _event=None):
        if self._hover:
            return
        self._hover = True
        self.apply()

    def _leave(self, _event=None):
        # Moving between the parent and the glyph fires Leave on one and Enter
        # on the other; only the pair means the pointer really left.
        self.after_idle(self._end_hover)

    def _end_hover(self):
        try:
            pointer = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
        except tk.TclError:
            return
        widget = pointer
        while widget is not None:
            if widget is self:
                return
            widget = getattr(widget, "master", None)
        self._hover = False
        self.apply()


class Pill(tk.Canvas):
    """A small rounded tag, used for the year/term badge on course cards.

    Fill and text colour are passed explicitly: deriving the text colour from the
    theme background produced dark-on-dark text in the dark palette.
    """

    def __init__(self, master, pal, text, bg, fill, fg, font=None, padx=8, height=19):
        self.pal = pal
        try:
            probe = tk.font.Font(font=font)
            width = probe.measure(text) + padx * 2
        except Exception:  # noqa: BLE001
            width = len(text) * 7 + padx * 2
        width = max(width, 26)
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, bd=0, bg=bg)
        round_rect(self, 0, 0, width - 1, height - 1, height / 2, fill=fill, outline="")
        self.create_text(width / 2, height / 2, text=text, fill=fg, font=font)


class ItemRow:
    """One row in the content list.

    Deliberately built from plain tk widgets and made incrementally restylable:
    `set_selection` is a no-op when nothing changed, which is what makes ticking
    a checkbox instant even with hundreds of rows on screen.
    """

    def __init__(self, parent, pal, fonts, node, on_toggle, on_open, on_download,
                 on_hover=None):
        self.pal = pal
        self.fonts = fonts
        self.node = node
        self.node_id = node["id"]
        self.selected = False
        self.implied = False
        self._hover = False
        self._buttons = []

        self.frame = tk.Frame(parent, bg=pal.panel, bd=0,
                              highlightthickness=1,
                              highlightbackground=pal.line_soft,
                              highlightcolor=pal.line_soft)
        self.frame.pack(fill="x", pady=1, padx=2)

        self.check = CheckMark(self.frame, pal, size=20,
                               command=lambda i=self.node_id: on_toggle(i))
        self.check.pack(side="left", padx=(11, 9), pady=8)

        self.icon = tk.Canvas(self.frame, width=22, height=22, highlightthickness=0,
                              bd=0, bg=pal.panel)
        self.painter = None
        self.icon.pack(side="left", padx=(0, 10), pady=8)

        # Buttons are packed right-to-left, so later ones sit further right.
        is_folder = node["kind"] == "folder"
        can_download = bool(node.get("downloadable")) or (
            is_folder and node.get("itemCount"))
        if can_download:
            label = "下載全部" if is_folder else "下載"
            self.download_btn = CanvasButton(
                self.frame, pal, label, width=72,
                command=lambda i=self.node_id: on_download(i),
                variant="accent", font=fonts["tiny"])
            self.download_btn.pack(side="right", padx=(0, 10), pady=9)
            self._buttons.append(self.download_btn)
        if is_folder:
            self.open_btn = CanvasButton(
                self.frame, pal, "開啟", width=54,
                command=lambda i=self.node_id: on_open(i),
                variant="ghost", font=fonts["tiny"])
            self.open_btn.pack(side="right", padx=3, pady=9)
            self._buttons.append(self.open_btn)

        self.title = tk.Label(self.frame, text=node["title"], anchor="w",
                              bg=pal.panel, fg=pal.text, font=fonts["body"],
                              justify="left")
        self.title.pack(side="left", fill="x", expand=True, pady=(7, 0))
        self.meta = tk.Label(self.frame, text="", anchor="w", bg=pal.panel,
                             fg=pal.text_faint, font=fonts["tiny"])
        self.meta.pack(side="left", fill="x", expand=True, pady=(0, 7))

        self.set_meta(node.get("meta_text", ""))

        self._on_open = on_open
        for widget in (self.frame, self.title, self.meta):
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)
        if is_folder:
            for widget in (self.frame, self.title, self.meta):
                widget.configure(cursor="hand2")
                widget.bind("<Double-Button-1>", self._double)
        if on_hover:
            self.frame.bind("<Enter>", lambda _e: on_hover(self.node_id), add="+")

    # -- content ---------------------------------------------------------
    def set_meta(self, text):
        self.meta.configure(text=text)

    def set_icon(self, kind):
        from .theme import IconPainter

        self.icon.delete("all")
        IconPainter.draw(self.icon, kind, self.pal.kind(kind), size=22,
                         bg=self.surface())

    def surface(self):
        pal = self.pal
        if self.selected:
            return pal.sel
        if self.implied:
            return pal.accent_soft if pal.mode == "dark" else pal.card_sel
        if self._hover:
            return pal.hover
        return pal.panel

    # -- state -----------------------------------------------------------
    def set_selection(self, selected, implied):
        """Restyle for a selection change. No-op when nothing actually changed."""
        if selected == self.selected and implied == self.implied:
            return False
        self.selected = selected
        self.implied = implied
        self.apply()
        return True

    def sync_check(self):
        """Drive the checkbox from the row's selection state.

        Without this the row could be `selected` while its checkbox still drew
        the empty "off" glyph - the two are separate objects and only the row
        was being updated.
        """
        state = "on" if self.selected else ("implied" if self.implied else "off")
        self.check.set_state(state)

    def set_meta_text(self, text):
        if self.meta.cget("text") != text:
            self.meta.configure(text=text)

    def apply_surface(self, colour):
        """Paint every child with `colour` without touching logical state.

        Used by the entrance animation: it interpolates between the list
        background and the row's real surface, then `apply()` restores the
        selection-aware colours on the final frame.
        """
        self.frame.configure(bg=colour)
        self.title.configure(bg=colour)
        self.meta.configure(bg=colour)
        self.icon.configure(bg=colour)
        self.check.apply(bg=colour)
        for button in self._buttons:
            button.apply(bg=colour)

    def apply(self):
        pal = self.pal
        surface = self.surface()
        border = pal.accent_line if (self.selected or self.implied) else pal.line_soft
        self.frame.configure(bg=surface, highlightbackground=border,
                             highlightcolor=border)
        self.title.configure(bg=surface)
        self.meta.configure(bg=surface)
        self.icon.configure(bg=surface)
        self.sync_check()
        self.check.apply(bg=surface)
        for button in self._buttons:
            button.apply(bg=surface)
        self.title.configure(fg=pal.text)
        self.meta.configure(fg=pal.text_dim if self.selected else pal.text_faint)

    def destroy(self):
        try:
            self.frame.destroy()
        except tk.TclError:
            pass

    # -- events ----------------------------------------------------------
    def _enter(self, _event=None):
        if self._hover:
            return
        self._hover = True
        self.apply()

    def _leave(self, _event=None):
        if not self._hover:
            return
        self._hover = False
        self.apply()

    def _double(self, _event=None):
        if self._on_open:
            self._on_open(self.node_id)


class CourseCard:
    """A course entry in the sidebar.

    Card background, a 1px border and real vertical gaps are what make two
    neighbouring courses read as separate items rather than one text list.
    """

    def __init__(self, parent, pal, fonts, meta, command, selected=False):
        self.pal = pal
        self.meta = meta
        self.command = command
        self.selected = selected
        self._hover = False

        self.frame = tk.Frame(parent, bg=pal.card, bd=0, highlightthickness=1,
                              highlightbackground=pal.card_border_sel if selected
                              else pal.card_border)
        self.frame.pack(fill="x", pady=3, padx=2)

        top = tk.Frame(self.frame, bg=pal.card)
        top.pack(fill="x", padx=10, pady=(8, 3))
        self.top = top

        badge_text = meta.year_label if not meta.year else meta.year
        self.badge = Pill(top, pal, badge_text, bg=pal.card,
                          fill=pal.accent_soft, fg=pal.accent, font=fonts["badge"])
        self.badge.pack(side="left")

        term_text = meta.term_label
        self.term = tk.Label(top, text=term_text, bg=pal.card, fg=pal.text_faint,
                             font=fonts["badge"])
        self.term.pack(side="left", padx=(6, 0))

        if meta.unresolved:
            self.warn = tk.Label(top, text="名稱待取得", bg=pal.card, fg=pal.warn,
                                 font=fonts["badge"])
            self.warn.pack(side="right")

        self.title = tk.Label(self.frame, text=meta.display_name, bg=pal.card,
                              fg=pal.text if not meta.unresolved else pal.text_dim,
                              font=fonts["small"], anchor="w", justify="left",
                              wraplength=196)
        self.title.pack(fill="x", padx=10)

        self.sub = tk.Label(self.frame, text=meta.subtitle, bg=pal.card,
                            fg=pal.text_faint, font=fonts["badge"], anchor="w")
        self.sub.pack(fill="x", padx=10, pady=(1, 8))

        for widget in (self.frame, top, self.title, self.sub, self.badge, self.term):
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)
            try:
                widget.configure(cursor="hand2")
            except tk.TclError:
                pass

    def _click(self, _event=None):
        if self.command:
            self.command(self.meta.course_id)

    def _enter(self, _event=None):
        if self.selected:
            return
        self._hover = True
        self.apply(self.pal.card_hover)

    def _leave(self, _event=None):
        if self.selected:
            return
        self._hover = False
        self.apply(self.pal.card)

    def apply(self, surface=None):
        pal = self.pal
        if surface is None:
            surface = pal.card_sel if self.selected else (
                pal.card_hover if self._hover else pal.card)
        for widget in (self.frame, self.top, self.title, self.sub,
                       self.badge, self.term):
            widget.configure(bg=surface)
        try:
            self.warn.configure(bg=surface)
        except AttributeError:
            pass
        self.frame.configure(
            highlightbackground=pal.card_border_sel if self.selected else pal.card_border)

    def destroy(self):
        try:
            self.frame.destroy()
        except tk.TclError:
            pass


class SectionHeader:
    """A year heading in the grouped course list."""

    def __init__(self, parent, pal, fonts, title, count):
        self.frame = tk.Frame(parent, bg=pal.side)
        self.frame.pack(fill="x", padx=4, pady=(10, 2))
        tk.Label(self.frame, text=title, bg=pal.side, fg=pal.text_dim,
                 font=fonts["section"], anchor="w").pack(side="left")
        tk.Label(self.frame, text=str(count), bg=pal.side, fg=pal.text_faint,
                 font=fonts["badge"]).pack(side="right")

    def destroy(self):
        try:
            self.frame.destroy()
        except tk.TclError:
            pass


class EmptyState:
    """The placeholder page shown when nothing is open yet.

    Centring uses two expanding spacers rather than `place()`. `place()` adds
    nothing to the parent's requested size, so an empty state inside a container
    that sizes to its content collapsed to 1px tall and rendered off-screen -
    which is precisely what happened before this was changed. With pack spacers
    the geometry is real and the block stays centred at any window size.
    """

    def __init__(self, parent, pal, fonts, title, hint, tips=(), icon="layers",
                 animator=None):
        self.pal = pal
        self.frame = tk.Frame(parent, bg=pal.bg)
        self.frame.pack(fill="both", expand=True)

        top_spacer = tk.Frame(self.frame, bg=pal.bg)
        top_spacer.pack(fill="both", expand=True)

        center = tk.Frame(self.frame, bg=pal.bg)
        center.pack()
        self.center = center

        size = 68
        canvas = tk.Canvas(center, width=size, height=size, highlightthickness=0,
                           bd=0, bg=pal.bg)
        canvas.pack(pady=(0, 18))
        self.canvas = canvas
        self._draw_icon(icon, pal.text_faint)

        self.title = tk.Label(center, text=title, bg=pal.bg, fg=pal.text_dim,
                              font=fonts["empty_title"])
        self.title.pack()

        self.hint = tk.Label(center, text=hint, bg=pal.bg, fg=pal.text_faint,
                             font=fonts["body"], justify="center")
        self.hint.pack(pady=(8, 0))

        if tips:
            self.tips = tk.Frame(center, bg=pal.bg)
            self.tips.pack(pady=(20, 0))
            for tip in tips:
                line = tk.Frame(self.tips, bg=pal.bg)
                line.pack(pady=2)
                dot = tk.Canvas(line, width=6, height=6, highlightthickness=0,
                                bd=0, bg=pal.bg)
                dot.pack(side="left", padx=(0, 8), pady=4)
                dot.create_oval(0, 0, 5, 5, fill=pal.accent, outline="")
                tk.Label(line, text=tip, bg=pal.bg, fg=pal.text_faint,
                         font=fonts["small"], anchor="w").pack(side="left")

        bottom_spacer = tk.Frame(self.frame, bg=pal.bg)
        bottom_spacer.pack(fill="both", expand=True)
        self.spacers = (top_spacer, bottom_spacer)
        self._fade_targets = []
        for widget, colour in (
            (self.title, pal.text_dim),
            (self.hint, pal.text_faint),
            (self.canvas, None),
        ):
            self._fade_targets.append((widget, colour))

    def fade_in(self, animator, duration=220):
        """Fade the text up from the background colour.

        Tk has no alpha, so this animates the foreground colours instead. It
        makes the hint page appear rather than snap, which is what makes the
        transition between pages feel continuous.
        """
        if animator is None:
            return
        pal = self.pal
        pairs = [(self.title, pal.text_dim), (self.hint, pal.text_faint)]
        if hasattr(self, "tips"):
            for line in self.tips.winfo_children():
                for child in line.winfo_children():
                    if isinstance(child, tk.Label):
                        pairs.append((child, pal.text_faint))

        def frame(eased, _raw):
            for widget, target in pairs:
                try:
                    widget.configure(fg=lerp_color(pal.bg, target, eased))
                except tk.TclError:
                    return

        animator.run(duration, frame)

        if hasattr(self, "canvas"):
            icon_target = pal.text_faint

            def icon_frame(eased, _raw):
                try:
                    self.canvas.delete("all")
                    self._draw_icon("folder",
                                    lerp_color(pal.bg, icon_target, eased))
                except tk.TclError:
                    return

            animator.run(duration, icon_frame, on_done=self._restore_icon)

    def _restore_icon(self):
        try:
            self._draw_icon("folder", self.pal.text_faint)
        except tk.TclError:
            pass

    def _draw_icon(self, name, colour):
        from .theme import IconPainter

        size = 68
        IconPainter.draw(self.canvas, "folder", colour, size=size, clear=False)
        # Two offset outlines behind the glyph give the "stack" impression.
        self.canvas.create_line(size * 0.30, size * 0.84, size * 0.70, size * 0.84,
                                fill=self.pal.line, width=2, capstyle="round")
        self.canvas.create_line(size * 0.38, size * 0.94, size * 0.62, size * 0.94,
                                fill=self.pal.line_soft, width=2, capstyle="round")

    def destroy(self):
        try:
            self.frame.destroy()
        except tk.TclError:
            pass


class SkeletonList:
    """Breathing placeholder rows shown while the catalog is being read.

    A spinner in the caption makes the state explicit ("working", not "empty"),
    and the rows themselves pulse so the screen is never static.
    """

    def __init__(self, parent, pal, fonts, count=9, animator=None, caption="正在讀取課程結構…"):
        from .anim import Spinner

        self.pal = pal
        self.frame = tk.Frame(parent, bg=pal.bg)
        self.frame.pack(fill="both", expand=True, padx=6, pady=4)

        header = tk.Frame(self.frame, bg=pal.bg)
        header.pack(fill="x", padx=6, pady=(4, 6))
        self.spinner_canvas = tk.Canvas(header, width=20, height=20,
                                        highlightthickness=0, bd=0, bg=pal.bg)
        self.spinner_canvas.pack(side="left")
        tk.Label(header, text=caption, bg=pal.bg, fg=pal.text_faint,
                 font=fonts["small"], anchor="w").pack(side="left", padx=(8, 0))
        self.spinner = Spinner(self.spinner_canvas, pal, size=20, width=2.4,
                               animator=animator)

        self.bars = []
        for _ in range(count):
            row = tk.Frame(self.frame, bg=pal.skeleton_a, highlightthickness=1,
                           highlightbackground=pal.line_soft)
            row.pack(fill="x", pady=2, padx=2)
            box = tk.Frame(row, bg=pal.skeleton_b, width=20, height=20)
            box.pack(side="left", padx=(12, 12), pady=11)
            box.pack_propagate(False)
            text = tk.Frame(row, bg=pal.skeleton_b, height=10)
            text.pack(side="left", fill="x", expand=True, pady=11)
            self.bars.append(row)

    def start(self):
        self.spinner.start()

    def stop(self):
        self.spinner.stop()

    def widgets(self):
        return list(self.bars)

    def destroy(self):
        self.stop()
        try:
            self.frame.destroy()
        except tk.TclError:
            pass
