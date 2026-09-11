"""Animation toolkit for the Tk main loop.

Tk has no animation framework, so this provides the three primitives the UI
needs, all cheap enough to run at 60fps:

* `Tween`   - a time-based value interpolation with easing
* `Spinner` - a rotating arc on a canvas (loading indicator)
* `Shimmer` - pulses widget backgrounds (skeleton loading placeholders)

`Tween.advance(now)` is public and takes an explicit timestamp, so tests can
step an animation deterministically instead of sleeping on real frames.

Everything is driven by one `Animator` per window, which owns every scheduled
`after` callback so a closing window can cancel them all and never touch a
destroyed widget.
"""

import math
import time
import tkinter as tk

# ---------------------------------------------------------------- easing
def clamp01(t):
    return 0.0 if t < 0 else (1.0 if t > 1 else t)


def linear(t):
    return clamp01(t)


def ease_out_cubic(t):
    t = clamp01(t)
    return 1.0 - (1.0 - t) ** 3


def ease_out_quad(t):
    t = clamp01(t)
    return 1.0 - (1.0 - t) ** 2


def ease_in_out_quad(t):
    t = clamp01(t)
    return 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2


def ease_out_back(t):
    t = clamp01(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


EASINGS = {
    "linear": linear,
    "out": ease_out_cubic,
    "out_quad": ease_out_quad,
    "in_out": ease_in_out_quad,
    "back": ease_out_back,
}


def lerp(a, b, t):
    return a + (b - a) * t


# ---------------------------------------------------------------- colour
def hex_to_rgb(value):
    text = (value or "").lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (0, 0, 0)
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def lerp_color(start, end, t):
    """Blend two hex colours. Used for fades - cheap on tk widgets."""
    a, b = hex_to_rgb(start), hex_to_rgb(end)
    return rgb_to_hex(tuple(lerp(a[i], b[i], t) for i in range(3)))


# ---------------------------------------------------------------- tween
class Tween:
    """A time-based value interpolation.

    `on_frame(eased, raw)` receives the eased and linear progress. `advance()` is
    public so tests can drive frames without waiting on the clock.
    """

    def __init__(self, duration_ms, on_frame, on_done=None, ease=ease_out_cubic,
                 clock=time.perf_counter):
        self.duration = max(1.0, float(duration_ms)) / 1000.0
        self.on_frame = on_frame
        self.on_done = on_done
        self.ease = ease
        self.clock = clock
        self.done = False
        self.cancelled = False
        self._start = None
        self._after_id = None
        self.frames = 0

    def advance(self, now=None):
        """Apply one frame. Returns True when the tween has finished."""
        if self.cancelled or self.done:
            return True
        now = self.clock() if now is None else now
        if self._start is None:
            self._start = now
        raw = clamp01((now - self._start) / self.duration)
        self.frames += 1
        self.on_frame(self.ease(raw), raw)
        if raw >= 1.0:
            self.done = True
            if self.on_done:
                self.on_done()
            return True
        return False

    def cancel(self):
        self.cancelled = True
        self._after_id = None


class OneShot:
    """A cancellable single `after` callback.

    A class rather than a dict: tokens are held in a set for bookkeeping, and
    dicts are unhashable (that mistake raised `TypeError: unhashable type: 'dict'`
    the first time a spinner tried to schedule its next tick).
    """

    __slots__ = ("id", "cancelled")

    def __init__(self, after_id=None):
        self.id = after_id
        self.cancelled = False


class Animator:
    """Owns every scheduled animation for one window."""

    def __init__(self, widget, interval_ms=16):
        self.widget = widget
        self.interval_ms = interval_ms
        self._active = set()
        self._stopped = False

    @property
    def active_count(self):
        return len(self._active)

    def run(self, duration_ms, on_frame, on_done=None, ease=ease_out_cubic):
        """Start a tween. Returns it, so callers can cancel it early."""
        if self._stopped:
            return None
        tween = Tween(duration_ms, on_frame, on_done=on_done, ease=ease)
        self._active.add(tween)

        def tick():
            if tween.cancelled or self._stopped:
                self._active.discard(tween)
                return
            try:
                finished = tween.advance()
            except tk.TclError:
                # The widget went away mid-animation; stop quietly.
                self._active.discard(tween)
                return
            if finished:
                self._active.discard(tween)
                return
            tween._after_id = self.widget.after(self.interval_ms, tick)

        # First frame applies immediately, so a short animation is never skipped.
        try:
            finished = tween.advance()
        except tk.TclError:
            self._active.discard(tween)
            return tween
        if not finished:
            tween._after_id = self.widget.after(self.interval_ms, tick)
        else:
            self._active.discard(tween)
        return tween

    def after(self, delay_ms, callback):
        """A cancellable one-shot, tracked like the tweens."""
        if self._stopped:
            return None
        token = OneShot()
        self._active.add(token)

        def fire():
            if token.cancelled or self._stopped:
                self._active.discard(token)
                return
            self._active.discard(token)
            try:
                callback()
            except tk.TclError:
                pass

        token.id = self.widget.after(delay_ms, fire)
        return token

    def cancel(self, token):
        if not token:
            return
        if isinstance(token, Tween):
            if token._after_id:
                try:
                    self.widget.after_cancel(token._after_id)
                except (tk.TclError, ValueError):
                    pass
            token.cancel()
            self._active.discard(token)
            return
        if isinstance(token, OneShot):
            token.cancelled = True
            if token.id:
                try:
                    self.widget.after_cancel(token.id)
                except (tk.TclError, ValueError):
                    pass
            self._active.discard(token)

    def stop_all(self):
        """Cancel everything. Called on window close."""
        self._stopped = True
        for token in list(self._active):
            self.cancel(token)
        self._active.clear()

    def resume(self):
        self._stopped = False


class Spinner:
    """A rotating arc, for indeterminate loading."""

    def __init__(self, canvas, pal, size=26, color=None, width=2.6, step_ms=28,
                 animator=None, arc=100):
        self.canvas = canvas
        self.pal = pal
        self.size = size
        self.color = color or pal.accent
        self.width = width
        self.step_ms = step_ms
        self.arc = arc
        self.angle = 90
        self._running = False
        self._token = None
        self._animator = animator
        self._item = None

    def _draw(self):
        c = self.canvas
        s = self.size
        pad = self.width
        if self._item is None:
            c.delete("all")
            self._item = c.create_arc(
                pad, pad, s - pad, s - pad, start=self.angle, extent=self.arc,
                style="arc", outline=self.color, width=self.width)
        else:
            c.itemconfigure(self._item, start=self.angle)

    def start(self):
        if self._running or self._animator is None:
            return
        self._running = True
        self._draw()

        def schedule():
            if not self._running:
                return
            self.angle = (self.angle - 12) % 360
            try:
                self._draw()
            except tk.TclError:
                self._running = False
                return
            self._token = self._animator.after(self.step_ms, schedule)

        schedule()

    def stop(self):
        self._running = False
        if self._token and self._animator:
            self._animator.cancel(self._token)
        self._token = None
        try:
            self.canvas.delete("all")
        except tk.TclError:
            pass
        self._item = None

    def set_color(self, color):
        self.color = color
        if self._item is not None:
            try:
                self.canvas.itemconfigure(self._item, outline=color)
            except tk.TclError:
                pass


class Shimmer:
    """Pulses widget backgrounds between two colours.

    Colour changes on a tk widget do not trigger geometry recalculation, so this
    stays cheap even with dozens of placeholders on screen.
    """

    def __init__(self, pal, animator, period_ms=1100):
        self.pal = pal
        self.animator = animator
        self.period_ms = period_ms
        self.widgets = []
        self._token = None
        self._cancelled = False

    def add(self, widget):
        self.widgets.append(widget)

    def set_widgets(self, widgets):
        self.widgets = list(widgets)

    def start(self):
        if self._token is not None or not self.widgets:
            return
        self._cancelled = False
        phases = 14

        def run_cycle():
            if self._cancelled or not self.widgets:
                self._token = None
                return
            self.animator.run(
                self.period_ms,
                lambda t: self._apply(t),
                on_done=self._next,
                ease=linear,
            )

        def step(index=0):
            if self._cancelled:
                self._token = None
                return
            phase = index % phases
            t = phase / float(phases - 1)
            # Triangle wave so it breathes in and out.
            blended = t if t <= 0.5 else 1.0 - (t - 0.5) * 2
            self._apply(blended)
            self._token = self.animator.after(
                max(30, self.period_ms // phases), lambda: step(index + 1))

        step()

    def _next(self):
        self._token = None

    def _apply(self, t):
        colour = lerp_color(self.pal.skeleton_a, self.pal.skeleton_b, t)
        for widget in list(self.widgets):
            try:
                widget.configure(bg=colour)
            except tk.TclError:
                self.widgets.remove(widget)

    def stop(self):
        self._cancelled = True
        if self._token and self.animator:
            self.animator.cancel(self._token)
        self._token = None


def stagger_delays(count, total_ms, max_items=None):
    """Per-item delay for a staggered reveal.

    Returns a list of millisecond offsets. When `max_items` is smaller than
    `count`, the later items share the tail so a long list still finishes on
    time instead of revealing one row per frame for a second.
    """
    if count <= 0:
        return []
    items = count if max_items is None else min(count, max_items)
    if items <= 1:
        return [0] * count
    step = float(total_ms) / (items - 1)
    out = []
    for index in range(count):
        if index < items:
            out.append(int(index * step))
        else:
            out.append(total_ms)
    return out
