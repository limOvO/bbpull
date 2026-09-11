"""bbpull desktop GUI, Qt edition (PySide6).

Chosen over the Tk build for one measured reason: rendering a list. Tk needs a
widget (and therefore an OS window) per row, so a 5,000-row list costs ~14 s;
Qt's model/view paints only the visible rows, so the same list costs 9 ms. The
engine underneath is untouched - only this presentation layer differs.

The Tk build remains available as a fallback: `bbpull gui --tk`.
"""

from .window import MainWindow, run_gui  # noqa: F401

__all__ = ["MainWindow", "run_gui"]
