"""bbpull desktop GUI (customtkinter) — a standalone window, no browser.

The Tk app is imported lazily on purpose. `bbpull.gui.courses` and
`bbpull.gui.anim` are pure modules that the Qt build and the tests import;
importing them used to drag in `.app`, which imports `tkinter` at module level.
On Linux, Tk is an optional system package (`python3-tk` / `tk`), so a machine
without it could not run the Qt build at all - a hard failure for a GUI it does
not use. The package now imports nothing until an attribute is actually asked
for, so the Qt path never needs Tk.
"""

__all__ = ["BbpullApp", "run_gui"]


def __getattr__(name):
    """Import the Tk app only when one of its public names is requested."""
    if name in __all__:
        from .app import BbpullApp, run_gui

        return {"BbpullApp": BbpullApp, "run_gui": run_gui}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
