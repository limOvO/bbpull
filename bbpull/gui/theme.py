"""Design tokens and canvas-drawn icons for the desktop GUI.

Icons are painted on tiny tkinter canvases instead of shipped as image files:
it keeps the app a pure-Python drop-in with no asset path problems, and vector
drawing stays crisp at any DPI. Tkinter has no alpha, so translucent accents are
pre-blended against the surface they sit on (see `_blend`).
"""

import tkinter as tk
import tkinter.font as tkfont

# Colour tokens live in `bbpull/palette.py` so the Qt build and the Tk fallback
# cannot drift apart. Re-exported here for backwards compatibility.
from ..palette import (  # noqa: F401
    DARK,
    KIND_COLORS,
    KIND_LABELS,
    LIGHT,
    Palette,
    blend as _blend,
)

def pick_font(root, size=10, weight="normal"):
    """Return a font tuple that renders both Latin and CJK well on this machine."""
    return (pick_family(root), size, weight) if weight != "normal" else (pick_family(root), size)


def pick_family(root):
    available = set(tkfont.families(root))
    for family in (
        "Microsoft JhengHei UI",
        "Microsoft JhengHei",
        "Noto Sans TC",
        "PingFang TC",
        "Segoe UI",
        "Helvetica",
    ):
        if family in available:
            return family
    return "TkDefaultFont"


def ui_scale(root, reference_dpi=96.0, damp=0.75):
    """Pixels of screen space per logical pixel, for this display.

    Derived from Tk's own `scaling` (points -> pixels). On this machine that is
    ~1.33 at 96dpi and ~2.67 at 192dpi, which is why a 13pt label asked for 42px.

    `damp` deliberately softens the result: a 200% display would otherwise make
    every control twice as large, which is what made the top bar feel oversized.
    """
    try:
        scaling = float(root.tk.call("tk", "scaling"))
    except (tk.TclError, ValueError, TypeError):
        return 1.0
    dpi = scaling * 72.0
    raw = dpi / float(reference_dpi)
    return max(1.0, min(2.0, 1.0 + (raw - 1.0) * damp))


def pixel_font(root, px, weight="normal"):
    """A font tuple sized in **pixels** (Tk: negative size means pixels).

    Pixel sizes are the only way to make text and canvas-drawn widgets agree:
    point sizes get multiplied by `tk scaling`, pixel sizes do not.
    """
    family = pick_family(root)
    size = -max(6, int(round(px)))
    return (family, size, weight) if weight != "normal" else (family, size)


class IconPainter:
    """Draws small vector icons onto a tkinter Canvas.

    Every draw method clears the canvas first, so they are safe to reuse - but a
    caller that has already painted a backing shape must pass `clear=False` or
    its work will be erased. (That bug hid the checkbox fill: the tick cleared
    the accent square, leaving a white tick on the row background.)
    """

    @staticmethod
    def _prepare(canvas, clear):
        if clear:
            canvas.delete("all")

    @staticmethod
    def draw(canvas, kind, color, size=22, bg=None, clear=True):
        IconPainter._prepare(canvas, clear)
        c = canvas
        s = size
        w = max(1.6, s / 13.0)
        pad = s * 0.14
        lo, hi = pad, s - pad

        def poly(points, fill=True, closed=True):
            c.create_polygon(points, fill=color if fill else "", outline=color,
                             width=w, joinstyle="round", smooth=False)

        if kind == "folder":
            # Tab + body, the classic file-manager folder.
            c.create_polygon(
                lo, lo + s * 0.16, lo + s * 0.30, lo + s * 0.16,
                lo + s * 0.38, lo + s * 0.28, hi, lo + s * 0.28,
                hi, hi, lo, hi,
                fill=color, outline=color, width=w, joinstyle="round",
            )
        elif kind == "document":
            c.create_polygon(
                lo, lo, hi - s * 0.26, lo, hi, lo + s * 0.26, hi, hi, lo, hi,
                fill=color, outline=color, width=w, joinstyle="round",
            )
            c.create_line(lo + s * 0.16, lo + s * 0.52, hi - s * 0.16, lo + s * 0.52,
                          fill=bg or "#000", width=max(1.0, w * 0.6))
            c.create_line(lo + s * 0.16, lo + s * 0.70, hi - s * 0.30, lo + s * 0.70,
                          fill=bg or "#000", width=max(1.0, w * 0.6))
        elif kind == "file":
            c.create_polygon(
                lo, lo, hi - s * 0.28, lo, hi, lo + s * 0.28, hi, hi, lo, hi,
                fill="", outline=color, width=w, joinstyle="round",
            )
            c.create_line(hi - s * 0.28, lo, hi - s * 0.28, lo + s * 0.28,
                          hi, lo + s * 0.28, fill=color, width=w)
        elif kind == "link":
            c.create_arc(lo - s * 0.06, lo + s * 0.28, lo + s * 0.62, hi,
                         start=40, extent=230, style="arc", outline=color, width=w)
            c.create_arc(hi - s * 0.62, lo, hi + s * 0.06, hi - s * 0.28,
                         start=220, extent=230, style="arc", outline=color, width=w)
            c.create_line(lo + s * 0.30, lo + s * 0.70, hi - s * 0.30, lo + s * 0.30,
                          fill=color, width=w)
        elif kind == "assessment":
            cx = s / 2
            c.create_polygon(
                cx, lo, hi, lo + s * 0.22, hi, hi - s * 0.30, cx, hi,
                lo, hi - s * 0.30, lo, lo + s * 0.22,
                fill="", outline=color, width=w, joinstyle="round",
            )
            c.create_line(cx - s * 0.16, s * 0.50, cx - s * 0.03, s * 0.63,
                          cx + s * 0.19, s * 0.36, fill=color, width=w,
                          capstyle="round", joinstyle="round")
        elif kind == "tool":
            c.create_rectangle(lo, lo + s * 0.10, hi, hi - s * 0.10,
                               outline=color, width=w)
            c.create_line(lo, lo + s * 0.34, hi, lo + s * 0.34, fill=color, width=w)
            c.create_line(lo + s * 0.16, lo + s * 0.62, hi - s * 0.16, lo + s * 0.62,
                          fill=color, width=w)
        elif kind == "announcement":
            c.create_polygon(
                lo, s * 0.36, lo + s * 0.24, s * 0.36, s * 0.62, lo, s * 0.62, hi,
                lo + s * 0.24, s * 0.64, lo, s * 0.64,
                fill=color, outline=color, width=w, joinstyle="round",
            )
            c.create_arc(s * 0.56, s * 0.34, hi + s * 0.10, s * 0.66,
                         start=-70, extent=140, style="arc", outline=color, width=w)
        else:
            c.create_oval(lo, lo, hi, hi, outline=color, width=w)

    @staticmethod
    def chevron(canvas, direction, color, size=16, clear=True):
        IconPainter._prepare(canvas, clear)
        s = size
        w = max(1.6, s / 9.0)
        if direction == "left":
            canvas.create_line(s * 0.62, s * 0.22, s * 0.36, s * 0.5, s * 0.62, s * 0.78,
                               fill=color, width=w, capstyle="round", joinstyle="round")
        elif direction == "right":
            canvas.create_line(s * 0.38, s * 0.22, s * 0.64, s * 0.5, s * 0.38, s * 0.78,
                               fill=color, width=w, capstyle="round", joinstyle="round")
        elif direction == "up":
            canvas.create_line(s * 0.5, s * 0.72, s * 0.5, s * 0.30, fill=color, width=w,
                               capstyle="round")
            canvas.create_line(s * 0.30, s * 0.48, s * 0.5, s * 0.28, s * 0.70, s * 0.48,
                               fill=color, width=w, capstyle="round", joinstyle="round")
        elif direction == "down":
            canvas.create_line(s * 0.5, s * 0.28, s * 0.5, s * 0.70, fill=color, width=w,
                               capstyle="round")
            canvas.create_line(s * 0.30, s * 0.52, s * 0.5, s * 0.72, s * 0.70, s * 0.52,
                               fill=color, width=w, capstyle="round", joinstyle="round")

    @staticmethod
    def check(canvas, color, size=14, clear=True):
        IconPainter._prepare(canvas, clear)
        s = size
        canvas.create_line(s * 0.22, s * 0.52, s * 0.42, s * 0.72, s * 0.80, s * 0.28,
                           fill=color, width=max(2.0, s / 6.0), capstyle="round",
                           joinstyle="round")

    @staticmethod
    def close(canvas, color, size=14, clear=True):
        IconPainter._prepare(canvas, clear)
        s = size
        w = max(1.8, s / 8.0)
        canvas.create_line(s * 0.25, s * 0.25, s * 0.75, s * 0.75, fill=color, width=w,
                           capstyle="round")
        canvas.create_line(s * 0.75, s * 0.25, s * 0.25, s * 0.75, fill=color, width=w,
                           capstyle="round")

    @staticmethod
    def refresh(canvas, color, size=16, clear=True):
        IconPainter._prepare(canvas, clear)
        s = size
        w = max(1.7, s / 9.0)
        canvas.create_arc(s * 0.2, s * 0.2, s * 0.8, s * 0.8, start=60, extent=280,
                          style="arc", outline=color, width=w)
        canvas.create_line(s * 0.66, s * 0.16, s * 0.80, s * 0.30, s * 0.62, s * 0.34,
                           fill=color, width=w, capstyle="round", joinstyle="round")

    @staticmethod
    def download(canvas, color, size=16, clear=True):
        IconPainter._prepare(canvas, clear)
        s = size
        w = max(1.7, s / 9.0)
        canvas.create_line(s * 0.5, s * 0.16, s * 0.5, s * 0.66, fill=color, width=w,
                           capstyle="round")
        canvas.create_line(s * 0.28, s * 0.46, s * 0.5, s * 0.68, s * 0.72, s * 0.46,
                           fill=color, width=w, capstyle="round", joinstyle="round")
        canvas.create_line(s * 0.22, s * 0.84, s * 0.78, s * 0.84, fill=color, width=w,
                           capstyle="round")

    @staticmethod
    def sun(canvas, color, size=16, clear=True):
        IconPainter._prepare(canvas, clear)
        s = size
        w = max(1.6, s / 10.0)
        canvas.create_oval(s * 0.34, s * 0.34, s * 0.66, s * 0.66, outline=color, width=w)
        import math
        for i in range(8):
            a = math.radians(i * 45)
            x1 = s * 0.5 + math.cos(a) * s * 0.27
            y1 = s * 0.5 + math.sin(a) * s * 0.27
            x2 = s * 0.5 + math.cos(a) * s * 0.40
            y2 = s * 0.5 + math.sin(a) * s * 0.40
            canvas.create_line(x1, y1, x2, y2, fill=color, width=w, capstyle="round")

    @staticmethod
    def account(canvas, color, size=16, clear=True):
        """A person/key glyph for the account button.

        The generic fallback drew a bare circle, which read as a stray dot rather
        than a button.
        """
        IconPainter._prepare(canvas, clear)
        s = size
        w = max(1.5, s / 11.0)
        canvas.create_oval(s * 0.34, s * 0.16, s * 0.66, s * 0.48,
                           outline=color, width=w)
        canvas.create_arc(s * 0.18, s * 0.56, s * 0.82, s * 1.10,
                          start=0, extent=180, style="arc", outline=color, width=w)

    @staticmethod
    def moon(canvas, color, size=16, clear=True):
        IconPainter._prepare(canvas, clear)
        s = size
        w = max(1.6, s / 10.0)
        canvas.create_arc(s * 0.20, s * 0.18, s * 0.86, s * 0.84, start=290, extent=250,
                          style="arc", outline=color, width=w)
        canvas.create_arc(s * 0.32, s * 0.10, s * 1.02, s * 0.80, start=110, extent=250,
                          style="arc", outline=color, width=w)
