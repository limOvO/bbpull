"""Design tokens shared by every front-end (Tk and Qt).

Deliberately free of any GUI import: the Qt build and the Tk fallback must show
the same colours, and a single source of truth is the only way to guarantee that.
Anything toolkit-specific (font picking, icon painting, scaling) lives in the
respective `gui/theme.py` or `gui_qt/theme.py`.
"""

DARK = {
    "bg": "#0e1016",
    "bg_elev": "#14171f",
    "side": "#12151d",
    "panel": "#171b24",
    "panel2": "#1c2130",
    "line": "#262c3a",
    "line_soft": "#1f2531",
    "text": "#e7eaf3",
    "text_dim": "#9aa3b8",
    "text_faint": "#6b7488",
    "accent": "#6d6cf6",
    "accent2": "#9a6cf6",
    "accent_soft": "#232641",
    "accent_line": "#4a4a8f",
    "ok": "#3ecf8e",
    "warn": "#f5b544",
    "err": "#f2637b",
    "hover": "#1e2331",
    "sel": "#232641",
    "chip": "#1a1f2b",
    # Course cards in the sidebar: deliberately lighter than `side` so the gap
    # between two courses is obvious, not just implied by spacing.
    "card": "#1a1f2b",
    "card_hover": "#212739",
    "card_sel": "#232a45",
    "card_border": "#2a3143",
    "card_border_sel": "#4a4a8f",
    # Skeleton placeholders breathe between these two.
    "skeleton_a": "#171b24",
    "skeleton_b": "#242c3e",
    "scroll": "#262c3a",
}

LIGHT = {
    "bg": "#f4f6fb",
    "bg_elev": "#ffffff",
    "side": "#eaeef7",
    "panel": "#ffffff",
    "panel2": "#f7f8fc",
    "line": "#e2e6f0",
    "line_soft": "#eceff6",
    "text": "#1a1f2b",
    "text_dim": "#5d6679",
    "text_faint": "#8a93a6",
    "accent": "#5b5ae0",
    "accent2": "#8a5ae0",
    "accent_soft": "#f0f0fe",
    "accent_line": "#c3c3f5",
    "ok": "#18a06a",
    "warn": "#c98a12",
    "err": "#d8455f",
    "hover": "#eef1f8",
    "sel": "#f0f0fe",
    "chip": "#eef1f8",
    "card": "#ffffff",
    "card_hover": "#f4f6fd",
    "card_sel": "#eef0ff",
    "card_border": "#dfe4f0",
    "card_border_sel": "#b9baf2",
    "skeleton_a": "#ffffff",
    "skeleton_b": "#e8ecf6",
    "scroll": "#d5dbe8",
}

# Icon colours per content kind, tuned per theme for contrast.
KIND_COLORS = {
    "dark": {
        "folder": "#7aa2ff",
        "document": "#b98cff",
        "file": "#4fd1a5",
        "link": "#58b6f0",
        "assessment": "#f5b544",
        "tool": "#f08fb0",
        "announcement": "#ffb347",
        "other": "#8a93a6",
    },
    "light": {
        "folder": "#3b6fe0",
        "document": "#8050e0",
        "file": "#128f6c",
        "link": "#1f7fc4",
        "assessment": "#b87a00",
        "tool": "#c04a76",
        "announcement": "#c47a00",
        "other": "#8a93a6",
    },
}

KIND_LABELS = {
    "folder": "資料夾",
    "document": "文件",
    "file": "檔案",
    "link": "外部連結",
    "assessment": "測驗／作業",
    "tool": "互動工具",
    "announcement": "公告",
    "other": "其他",
}


def _as_hex(value):
    """Coerce a colour to `#rrggbb`.

    Accepts a hex string or anything with a `name()` method (a `QColor`), via
    duck typing so this module stays toolkit-free. Without it, passing a QColor
    from the Qt delegate raised `AttributeError: 'QColor' object has no attribute
    'lstrip'` - inside `QStyledItemDelegate.paint`, where Qt swallows the
    exception and simply draws nothing.
    """
    if value is None:
        return "#000000"
    name = getattr(value, "name", None)
    if callable(name):
        try:
            return name()
        except Exception:  # noqa: BLE001
            return "#000000"
    return str(value)


def blend(fg, bg, alpha):
    """Mix `fg` over `bg` at `alpha` and return an opaque hex colour.

    Needed because neither Tk nor a plain QSS rule can express partial alpha on
    every surface; computing the solid equivalent keeps both front-ends identical.
    """
    fg_hex = _as_hex(fg)
    bg_hex = _as_hex(bg)
    fg_body = fg_hex.lstrip("#")
    bg_body = bg_hex.lstrip("#")
    parts = []
    for i in (0, 2, 4):
        try:
            f = int(fg_body[i:i + 2], 16)
            b = int(bg_body[i:i + 2], 16)
        except ValueError:
            # Unparseable input: fall back to the background, which is at least a
            # valid colour. Returning the already-stripped body here produced a
            # value without its leading `#`.
            return bg_hex
        parts.append(int(round(f * alpha + b * (1 - alpha))))
    return "#%02x%02x%02x" % tuple(parts)


class Palette:
    """One active colour set, resolved from the theme name."""

    def __init__(self, mode="dark"):
        self.set_mode(mode)

    def set_mode(self, mode):
        self.mode = mode if mode in ("dark", "light") else "dark"
        src = DARK if self.mode == "dark" else LIGHT
        for key, value in src.items():
            setattr(self, key, value)
        self.kinds = KIND_COLORS[self.mode]
        # Derived surfaces
        self.row_hover = self.hover
        self.row_sel = self.sel
        self.tray_bg = self.panel
        self.muted_accent = blend(self.accent, self.panel, 0.16)
        self.accent_on_bg = blend(self.accent, self.bg_elev, 0.14)

    def kind(self, kind):
        return self.kinds.get(kind, self.kinds["other"])

    def as_dict(self):
        data = dict(DARK if self.mode == "dark" else LIGHT)
        data.update({"muted_accent": self.muted_accent,
                     "accent_on_bg": self.accent_on_bg})
        return data
