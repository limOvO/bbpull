"""Selective download: fetch exactly the items the user picked.

Reuses `CoursePuller`'s per-node writers (`_write_node` / `_write_child`) rather
than reimplementing naming, BBML rendering, attachment discovery or collision
handling. Those paths are already covered by tests and validated against the
live site, so selective download inherits that correctness instead of forking it.

Layout matches a full pull, so mixing the two never produces a confusing tree:

    <root>/<courseId>_<name>/content/...        class content
    <root>/<courseId>_<name>/announcements/...  announcements

Discovery is expected to have happened already (see `catalog.py`); this module
only materialises a chosen subset, with progress reporting and cancellation.
"""

import os
import threading
import time
import uuid
from collections import OrderedDict, deque

from .announcements import AnnouncementPuller
from .catalog import ANNOUNCEMENTS_ID, KIND_ANNOUNCEMENT, walk
from .course import CoursePuller, Node
from .logging_util import wrap_logger
from .paths import long_path, sanitize

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_ERROR = "error"
JOB_CANCELLED = "cancelled"

TERMINAL_STATES = {JOB_DONE, JOB_ERROR, JOB_CANCELLED}


class _StopRequested(Exception):
    """Internal: raised to unwind a job when the user cancels it."""


class Job:
    """A cancellable unit of background work with observable progress."""

    def __init__(self, kind, label, total=0, log_limit=300, result_limit=200):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label
        self.state = JOB_PENDING
        self.total = total
        self.processed = 0
        self.current = ""
        self.files_saved = 0
        self.files_skipped = 0
        self.files_failed = 0
        self.bytes = 0
        self.errors = []
        self.results = deque(maxlen=result_limit)
        self.log = deque(maxlen=log_limit)
        self.started = None
        self.finished = None
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.result = None

    # -- mutation helpers ------------------------------------------------
    def note(self, message):
        with self.lock:
            self.log.append({"t": time.time(), "m": str(message)})

    def error(self, where, message):
        with self.lock:
            if len(self.errors) < 200:
                self.errors.append({"where": str(where), "error": str(message)})

    def add_result(self, entry):
        with self.lock:
            self.results.append(entry)

    def bump(self, saved=0, skipped=0, failed=0, bytes_=0):
        with self.lock:
            self.files_saved += saved
            self.files_skipped += skipped
            self.files_failed += failed
            self.bytes += bytes_

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise _StopRequested()

    def snapshot(self):
        with self.lock:
            percent = 0
            if self.total:
                percent = min(100, int(round(100.0 * self.processed / self.total)))
            if self.state == JOB_DONE:
                percent = 100
            return OrderedDict(
                [
                    ("id", self.id),
                    ("kind", self.kind),
                    ("label", self.label),
                    ("state", self.state),
                    ("total", self.total),
                    ("processed", self.processed),
                    ("percent", percent),
                    ("current", self.current),
                    ("filesSaved", self.files_saved),
                    ("filesSkipped", self.files_skipped),
                    ("filesFailed", self.files_failed),
                    ("bytes", self.bytes),
                    ("errorCount", len(self.errors)),
                    ("errors", list(self.errors[-40:])),
                    ("results", list(self.results)[-60:]),
                    ("log", [entry["m"] for entry in list(self.log)[-40:]]),
                    ("started", self.started),
                    ("finished", self.finished),
                ]
            )


class JobManager:
    """Runs jobs on threads and exposes their state for polling."""

    def __init__(self, keep=40):
        self._jobs = OrderedDict()
        self._lock = threading.Lock()
        self._keep = keep

    def create(self, kind, label, total=0):
        job = Job(kind, label, total=total)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self):
        with self._lock:
            if not self._jobs:
                return None
            return next(reversed(self._jobs.values()))

    def all_snapshots(self, limit=10):
        with self._lock:
            jobs = list(self._jobs.values())[-limit:]
        return [job.snapshot() for job in reversed(jobs)]

    def cancel(self, job_id=None):
        job = self.get(job_id) if job_id else self.latest()
        if not job:
            return False
        if job.state in TERMINAL_STATES:
            return False
        job.cancel_event.set()
        job.note("收到取消要求，正在停止 ...")
        return True

    def start(self, job, target, *args, **kwargs):
        """Run `target(job, *args, **kwargs)` on a daemon thread."""
        def runner():
            job.state = JOB_RUNNING
            job.started = time.time()
            try:
                job.result = target(job, *args, **kwargs)
            except _StopRequested:
                job.state = JOB_CANCELLED
                job.note("已取消。")
            except Exception as exc:  # never let a thread die silently
                job.state = JOB_ERROR
                job.error("job", exc)
                job.note(f"錯誤: {exc}")
            else:
                if job.state not in TERMINAL_STATES:
                    job.state = JOB_DONE
            finally:
                job.finished = time.time()
                job.current = ""

        thread = threading.Thread(target=runner, name=f"bbpull-job-{job.id}", daemon=True)
        thread.start()
        return job


class SelectiveDownloader:
    """Materialise a chosen subset of a catalog, preserving its folder structure."""

    def __init__(
        self,
        session,
        catalog,
        out_dir,
        logger=None,
        overwrite=False,
        download_files=True,
    ):
        self.session = session
        self.catalog = catalog
        self.out_dir = out_dir
        self.log = wrap_logger(logger)
        self.overwrite = overwrite
        self.download_files = download_files
        self.puller = CoursePuller(
            session,
            catalog.course_id,
            os.path.join(out_dir, "content"),
            self.log,
            download_files=download_files,
            overwrite=overwrite,
        )

    # ------------------------------------------------------------------ api
    def run(self, job, selected_ids):
        """Download the selection. `job` carries progress and cancellation."""
        selected = set(selected_ids or ())
        plan = self.catalog.plan(selected)
        if not plan:
            job.note("沒有選取任何項目。")
            return OrderedDict([("nodes", 0), ("files", 0), ("bytes", 0)])

        content_ids = self._expand(self.catalog.content_ids(selected))
        announce_ids = self.catalog.announce_ids(selected)
        job.total = len(content_ids) + len(announce_ids)
        job.note(
            f"準備下載 {len(plan)} 個選取項目"
            f"（展開為 {job.total} 個寫入步驟）"
        )

        content_dir = os.path.join(self.out_dir, "content")
        os.makedirs(long_path(content_dir), exist_ok=True)

        if content_ids:
            self._walk(job, self.catalog.tree, content_dir, depth=0, selected=content_ids)
        if announce_ids:
            self._download_announcements(job, announce_ids)

        summary = OrderedDict(
            [
                ("nodes", job.processed),
                ("files", job.files_saved + job.files_skipped),
                ("saved", job.files_saved),
                ("skipped", job.files_skipped),
                ("failed", job.files_failed),
                ("bytes", job.bytes),
                ("outDir", os.path.abspath(self.out_dir)),
            ]
        )
        job.note(
            f"完成：{summary['saved']} 個新檔案、{summary['skipped']} 個已存在、"
            f"{summary['failed']} 個失敗"
        )
        return summary

    def _expand(self, selected_ids):
        """Turn a selection into the exact set of write steps.

        A selected folder is written as a shell (its own directory + index
        sidecar) followed by each descendant individually. Collapsing a folder
        into one write would make progress jump from 0% to 100% and - worse -
        would leave a huge folder uncancellable, because cancellation is only
        checked between steps.
        """
        index = self.catalog.index()
        expanded = set()
        for node_id in self.catalog.plan(selected_ids):
            if node_id == ANNOUNCEMENTS_ID:
                continue
            node = index.get(node_id)
            if not node:
                continue
            expanded.add(node_id)
            for descendant in walk(node.get("children") or []):
                expanded.add(descendant["id"])
        return expanded

    # ------------------------------------------------------------- internals
    def _walk(self, job, nodes, parent_dir, depth, selected):
        """Write selected nodes, creating only the ancestor folders needed."""
        for position, node in enumerate(nodes or [], start=1):
            job.check_cancelled()
            node_id = node.get("id")
            if node_id == ANNOUNCEMENTS_ID:
                continue
            children = node.get("children") or []
            chosen = node_id in selected

            if chosen and not children:
                self._write_selected(job, node, parent_dir, depth, position)
                continue

            if chosen and children:
                # Selected folder: write its shell, then each child on its own so
                # progress advances and cancellations are honoured promptly.
                shell = self._open_folder(job, node, parent_dir, depth, position)
                if shell is not None:
                    self._walk(job, children, shell, depth + 1, selected)
                continue

            if children and self._contains_selection(children, selected):
                segment = self._segment(node, position, depth)
                child_dir = os.path.join(parent_dir, segment)
                os.makedirs(long_path(child_dir), exist_ok=True)
                # Tell the puller this name is taken, or a sibling file written
                # later could claim it and land in the wrong place.
                self.puller.claim_name(parent_dir, segment)
                self._walk(job, children, child_dir, depth + 1, selected)

    def _open_folder(self, job, node, parent_dir, depth, position):
        """Create a selected folder's directory and index sidecar. Returns its path."""
        label = node.get("title") or node.get("id")
        job.current = label
        try:
            real = node_from_catalog(node)
            segment = self._segment(node, position, depth)
            if depth == 0:
                # CoursePuller.write() dedupes root names; mirror that exactly.
                segment = self.puller._unique(parent_dir, segment)
            dir_path = os.path.join(parent_dir, segment)
            os.makedirs(long_path(dir_path), exist_ok=True)
            self.puller.claim_name(parent_dir, segment)
            real.rel_dir = os.path.relpath(dir_path, self.puller.out_dir).replace(os.sep, "/")
            self.puller._write_sidecar(real, dir_path, index_name="index.json")
        except Exception as exc:
            job.bump(failed=1)
            job.error(label, exc)
            job.add_result({"id": node.get("id"), "title": label, "error": str(exc)})
            job.note(f"! {label}: {exc}")
            return None
        job.add_result(
            {"id": node.get("id"), "title": label, "path": real.rel_dir, "folder": True}
        )
        job.note(f"▸ {label}")
        return dir_path

    def _write_selected(self, job, node, parent_dir, depth, position):
        """Write one selected leaf, using the puller's own ordering rules."""
        label = node.get("title") or node.get("id")
        job.current = label
        before = dict(self.puller.stats)
        try:
            real = node_from_catalog(node)
            if depth == 0:
                # Mirror CoursePuller.write(): roots are not ordinal-prefixed.
                self.puller._write_node(real, parent_dir)
            else:
                planned = os.path.join(parent_dir, self._segment(node, position, depth))
                self.puller._write_child(real, parent_dir, planned, position)
        except Exception as exc:
            job.bump(failed=1)
            job.error(label, exc)
            job.add_result({"id": node.get("id"), "title": label, "error": str(exc)})
            job.note(f"! {label}: {exc}")
        else:
            saved = self.puller.stats["files_saved"] - before["files_saved"]
            skipped = self.puller.stats["files_skipped"] - before["files_skipped"]
            failed = self.puller.stats["files_failed"] - before["files_failed"]
            gained = self.puller.stats["bytes"] - before["bytes"]
            job.bump(saved=saved, skipped=skipped, failed=failed, bytes_=gained)
            job.add_result(
                {
                    "id": node.get("id"),
                    "title": label,
                    "path": real.rel_dir,
                    "saved": saved,
                    "skipped": skipped,
                    "bytes": gained,
                }
            )
            job.note(f"✓ {label} ({saved} 個檔案)")
        finally:
            job.processed += 1

    def _download_announcements(self, job, announce_ids):
        puller = AnnouncementPuller(
            self.session,
            self.catalog.course_id,
            os.path.join(self.out_dir, "announcements"),
            self.log,
            download_files=self.download_files,
            overwrite=self.overwrite,
        )
        job.current = "公告"
        try:
            puller.fetch()
        except Exception as exc:
            job.error("announcements", exc)
            job.note(f"讀取公告失敗: {exc}")
            return
        before = dict(puller.stats)
        try:
            rows = puller.write(only_ids=announce_ids)
        except Exception as exc:
            job.error("announcements", exc)
            job.note(f"寫入公告失敗: {exc}")
            return
        job.bump(
            saved=puller.stats["files_saved"] - before["files_saved"],
            failed=puller.stats["files_failed"] - before["files_failed"],
            bytes_=puller.stats["bytes"] - before["bytes"],
        )
        for row in rows:
            job.add_result(
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "path": row.get("markdown"),
                    "announcement": True,
                }
            )
        job.processed += len(rows)
        job.note(f"✓ 公告 {len(rows)} 則")

    @staticmethod
    def _contains_selection(children, selected):
        stack = list(children)
        while stack:
            node = stack.pop()
            if node.get("id") in selected:
                return True
            stack.extend(node.get("children") or [])
        return False

    @staticmethod
    def _segment(node, position, depth):
        """Directory name matching CoursePuller's scheme: ordinal only below root."""
        base = sanitize(node.get("title"), fallback=node.get("id") or "item")
        return base if depth == 0 else f"{position:02d}_{base}"


def node_from_catalog(item):
    """Rebuild a real `Node` (with children and body) from a catalog node.

    The catalog is the single source of truth for what to download, so the
    writers must receive a fully populated `Node` - not a shell. Without the
    recursive children a selected folder would be written as a leaf .md, and
    without `body` documents would come out empty.
    """
    payload = dict(item.get("payload") or {})
    payload.setdefault("id", item.get("id"))
    payload.setdefault("title", item.get("title"))
    if not payload.get("contentHandler"):
        payload["contentHandler"] = {"id": item.get("handler") or ""}
    node = Node(payload)
    node.children = [node_from_catalog(child) for child in item.get("children") or []]
    return node


def run_download_job(job, session, catalog, selected_ids, out_dir, overwrite=False):
    """Job target: download `selected_ids` into `out_dir`."""
    downloader = SelectiveDownloader(
        session, catalog, out_dir, job.note, overwrite=overwrite
    )
    return downloader.run(job, selected_ids)


def course_dir_for(root, catalog):
    """Per-course output directory, matching the CLI's naming."""
    folder = sanitize(
        f"{catalog.course_id}_{catalog.course_name}",
        fallback=catalog.course_id or "course",
        max_len=90,
    )
    return os.path.join(root, folder)
