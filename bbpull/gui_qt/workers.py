"""Background work for the Qt build.

Qt wants UI updates on the main thread only. `Task` runs a callable on a
`QThread` and reports back through signals, which Qt marshals to the main thread
automatically - no polling loop and no shared mutable state.

Downloads reuse the existing thread-based `JobManager` (it is already
thread-safe and reports snapshots) and are polled with a `QTimer`, because a
progress display does not need signal-level latency.
"""

import traceback

from PySide6.QtCore import QObject, QThread, QTimer, Signal


class Task(QThread):
    """Run `fn` off the UI thread; deliver the result via signals."""

    succeeded = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, int)

    def __init__(self, fn, *args, parent=None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._emit_progress = kwargs.pop("progress", None) is not None

    def run(self):  # noqa: D102 - QThread entry point
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - reported to the UI, never lost
            detail = f"{exc}"
            if not detail:
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.failed.emit(detail or type(exc).__name__)
            return
        self.succeeded.emit(result)


class TaskRunner(QObject):
    """Owns running tasks so they are not garbage-collected mid-flight.

    A `QThread` that goes out of scope while running crashes the process; keeping
    a reference here until the thread finishes is the standard guard.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = set()

    def start(self, fn, on_ok, on_fail=None, *args, **kwargs):
        task = Task(fn, *args, parent=self, **kwargs)
        self._active.add(task)
        task.succeeded.connect(on_ok)

        def handle_failure(message):
            if on_fail:
                on_fail(message)

        task.failed.connect(handle_failure)

        def cleanup():
            self._active.discard(task)
            task.deleteLater()

        task.finished.connect(cleanup)
        task.start()
        return task

    def cancel_all(self):
        for task in list(self._active):
            try:
                task.requestInterruption()
            except RuntimeError:
                pass
        self._active.clear()

    @property
    def active_count(self):
        return len(self._active)


class JobPoller(QObject):
    """Polls the existing JobManager and emits snapshots for the UI."""

    updated = Signal(list)

    def __init__(self, manager, parent=None, interval_ms=350):
        super().__init__(parent)
        self.manager = manager
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)
        self._last = None

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll(self):
        snapshots = self.manager.all_snapshots(6)
        if snapshots != self._last:
            self._last = snapshots
            self.updated.emit(snapshots)
