"""Offline tests for the browse model (catalog) and selective download engine.

The fake session serves canned Blackboard JSON and records every file write, so
selection planning, ancestor-directory creation and subset materialisation are
all verified without a network.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull import catalog as cat  # noqa: E402
from bbpull import selective  # noqa: E402

WEBDAV = "https://bb.example.edu/bbcswebdav/pid-101-dt-content-rid-202_1/xid-202_1?t=1"

CONTENTS = [
    {
        "id": "_10_1",
        "title": "Week 1",
        "position": 0,
        "hasChildren": True,
        "contentHandler": {"id": "resource/x-bb-folder"},
    },
    {
        "id": "_20_1",
        "title": "Week 2",
        "position": 1,
        "hasChildren": True,
        "contentHandler": {"id": "resource/x-bb-folder"},
    },
    {
        "id": "_30_1",
        "title": "Syllabus.pdf",
        "position": 2,
        "contentHandler": {"id": "resource/x-bb-file", "file": {"fileName": "Syllabus.pdf"}},
    },
    {
        "id": "_40_1",
        "title": "Week 1 Quiz",
        "position": 3,
        "contentHandler": {"id": "resource/x-bb-asmt-test-link"},
    },
]

CHILDREN = {
    "_10_1": [
        {
            "id": "_11_1",
            "title": "Day 1",
            "position": 0,
            "hasChildren": True,
            "contentHandler": {"id": "resource/x-bb-folder"},
        },
        {
            "id": "_14_1",
            "title": "Overview",
            "position": 1,
            "contentHandler": {"id": "resource/x-bb-document"},
            "body": "<div><p>Overview text</p></div>",
        },
    ],
    "_11_1": [
        {
            "id": "_12_1",
            "title": "Notes",
            "position": 0,
            "contentHandler": {"id": "resource/x-bb-document"},
            "body": (
                "<div><h4>Agenda</h4>"
                f'<p><a href="{WEBDAV}" data-bbfile="{{&quot;linkName&quot;:&quot;notes.pdf&quot;}}">'
                "notes.pdf</a></p></div>"
            ),
        },
        {
            "id": "_13_1",
            "title": "Slides",
            "position": 1,
            "contentHandler": {"id": "resource/x-bb-file", "file": {"fileName": "Slides.pdf"}},
        },
    ],
    "_20_1": [
        {
            "id": "_21_1",
            "title": "Recap",
            "position": 0,
            "contentHandler": {"id": "resource/x-bb-document"},
            "body": "<div><p>Recap text</p></div>",
        }
    ],
}

ATTACHMENTS = {
    "_13_1": {"results": [{"id": "_131_1", "fileName": "Slides.pdf"}]},
    "_30_1": {"results": [{"id": "_301_1", "fileName": "Syllabus.pdf"}]},
}

ANNOUNCEMENTS = [
    {"id": "_a1_1", "title": "Welcome", "created": "2026-02-01T09:00:00.000Z",
     "body": "<div><p>Hello</p></div>"},
    {"id": "_a2_1", "title": "Exam date", "created": "2026-03-01T09:00:00.000Z",
     "body": "<div><p>Bring ID</p></div>"},
]

FILES = {
    WEBDAV: b"%PDF notes",
    "https://bb.example.edu/learn/api/public/v1/courses/_999_1/contents/_13_1/attachments/_131_1/download": b"%PDF slides",
    "https://bb.example.edu/learn/api/public/v1/courses/_999_1/contents/_30_1/attachments/_301_1/download": b"%PDF syllabus",
}


class FakeSession:
    base = "https://bb.example.edu"

    def __init__(self):
        self.downloads = []

    def safe_url(self, path):
        if path.startswith("http"):
            return path
        return self.base + (path if path.startswith("/") else "/" + path)

    def get_all(self, path, params=None, max_pages=500):
        if path.endswith("/contents"):
            return CONTENTS
        if "/contents/" in path and path.endswith("/children"):
            return CHILDREN.get(path.split("/contents/")[1].split("/")[0], [])
        if path.endswith("/announcements"):
            return ANNOUNCEMENTS
        raise AssertionError(f"unexpected path {path}")

    def api_get(self, path, params=None, expect_json=True, allow_404=False):
        if "/attachments" in path:
            content_id = path.split("/contents/")[1].split("/")[0]
            return ATTACHMENTS.get(content_id) if allow_404 else ATTACHMENTS[content_id]
        if allow_404:
            return None
        raise AssertionError(f"unexpected path {path}")

    def download(self, url, dest, overwrite=False, expected_size=None):
        self.downloads.append(url)
        if url not in FILES:
            return "failed", 0, "not in fake store"
        if os.path.exists(dest) and not overwrite:
            return "skipped", os.path.getsize(dest), dest
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(FILES[url])
        return "saved", len(FILES[url]), dest


def build_catalog(session=None, with_announcements=True):
    session = session or FakeSession()
    announce = ANNOUNCEMENTS if with_announcements else None
    return cat.Catalog.build(
        session, "_999_1", "Test Course", log=lambda *a: None, announce=announce
    )


def wait_for_job(job, timeout=15.0):
    """Block until a job settles. Returns True if it reached a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in selective.TERMINAL_STATES:
            return True
        time.sleep(0.01)
    return False


class KindMappingTests(unittest.TestCase):
    def test_known_handlers(self):
        self.assertEqual(cat.kind_for_handler("resource/x-bb-folder"), cat.KIND_FOLDER)
        self.assertEqual(cat.kind_for_handler("resource/x-bb-document"), cat.KIND_DOCUMENT)
        self.assertEqual(cat.kind_for_handler("resource/x-bb-file"), cat.KIND_FILE)
        self.assertEqual(cat.kind_for_handler("resource/x-bb-asmt-test-link"), cat.KIND_ASSESSMENT)

    def test_unknown_handler_falls_back_by_pattern(self):
        self.assertEqual(cat.kind_for_handler("resource/x-bb-something-folder"), cat.KIND_FOLDER)
        self.assertEqual(cat.kind_for_handler("resource/x-vendor-asmt-quiz"), cat.KIND_ASSESSMENT)

    def test_unknown_handler_is_other(self):
        self.assertEqual(cat.kind_for_handler("resource/x-vendor-mystery"), cat.KIND_OTHER)
        self.assertEqual(cat.kind_for_handler(""), cat.KIND_OTHER)

    def test_downloadable_kinds(self):
        self.assertTrue(cat.KIND_DOCUMENT in cat.DOWNLOADABLE_KINDS)
        self.assertTrue(cat.KIND_FILE in cat.DOWNLOADABLE_KINDS)
        self.assertFalse(cat.KIND_FOLDER in cat.DOWNLOADABLE_KINDS)
        self.assertFalse(cat.KIND_ASSESSMENT in cat.DOWNLOADABLE_KINDS)


class ConvertNodeTests(unittest.TestCase):
    def test_nested_conversion_and_counts(self):
        payload = {
            "id": "_1_1",
            "title": "Root",
            "contentHandler": {"id": "resource/x-bb-folder"},
            "children": [],
        }
        node = cat.Node(payload)
        node.children = [
            cat.Node({"id": "_2_1", "title": "A", "contentHandler": {"id": "resource/x-bb-document"}}),
            cat.Node({"id": "_3_1", "title": "B", "contentHandler": {"id": "resource/x-bb-folder"}}),
        ]
        node.children[1].children = [
            cat.Node({"id": "_4_1", "title": "C", "contentHandler": {"id": "resource/x-bb-file"}})
        ]
        converted = cat.convert_node(node)
        self.assertEqual(converted["kind"], cat.KIND_FOLDER)
        self.assertEqual(converted["itemCount"], 2)  # A + C, folders do not count
        self.assertEqual(converted["childCount"], 2)
        self.assertEqual(converted["depth"], 0)

    def test_counts_are_precomputed_for_every_level(self):
        root = build_catalog()
        by_id = root.index()
        self.assertEqual(by_id["_11_1"]["itemCount"], 2)   # Notes + Slides
        self.assertEqual(by_id["_10_1"]["itemCount"], 3)   # Day1(2) + Overview
        self.assertEqual(by_id["_20_1"]["itemCount"], 1)   # Recap
        self.assertEqual(by_id["_10_1"]["childCount"], 2)

    def test_document_and_file_marked_downloadable(self):
        by_id = build_catalog().index()
        self.assertTrue(by_id["_12_1"]["downloadable"])
        self.assertTrue(by_id["_30_1"]["downloadable"])
        self.assertFalse(by_id["_10_1"]["downloadable"])

    def test_assessment_is_not_downloadable(self):
        self.assertFalse(build_catalog().index()["_40_1"]["downloadable"])


class CatalogShapeTests(unittest.TestCase):
    def test_announcements_become_first_branch(self):
        catalog = build_catalog()
        self.assertEqual(catalog.tree[0]["id"], cat.ANNOUNCEMENTS_ID)
        self.assertEqual(catalog.tree[0]["childCount"], 2)
        self.assertEqual(catalog.tree[0]["itemCount"], 2)
        self.assertTrue(catalog.tree[0]["synthetic"])

    def test_announcements_sorted_newest_first(self):
        branch = build_catalog().tree[0]
        self.assertEqual(branch["children"][0]["title"], "Exam date")

    def test_without_announcements_no_branch(self):
        catalog = build_catalog(with_announcements=False)
        self.assertNotEqual(catalog.tree[0]["id"], cat.ANNOUNCEMENTS_ID)

    def test_stats(self):
        stats = build_catalog().stats
        # 4 roots + 2 under Week 1 + 2 under Day 1 + 1 under Week 2 = 9 content items.
        self.assertEqual(stats["items"], 9)
        self.assertEqual(stats["announcements"], 2)
        self.assertGreater(stats["downloadable"], 0)

    def test_root_and_children_accessors(self):
        catalog = build_catalog()
        # 4 content roots plus the synthetic announcements branch.
        self.assertEqual(len(catalog.children_of("root")), 5)
        self.assertEqual(len(catalog.children_of("_10_1")), 2)
        self.assertEqual(catalog.children_of("_missing"), [])

    def test_breadcrumb_trail(self):
        trail = build_catalog().breadcrumb("_12_1")
        self.assertEqual([n["id"] for n in trail], ["_10_1", "_11_1", "_12_1"])
        self.assertEqual(trail[0]["title"], "Week 1")

    def test_find_and_walk(self):
        catalog = build_catalog()
        self.assertEqual(catalog.find("_12_1")["title"], "Notes")
        self.assertIsNone(catalog.find("nope"))
        # 9 content nodes + 1 announcements folder + 2 announcements.
        self.assertEqual(len(list(cat.walk(catalog.tree))), 12)

    def test_walk_visits_nested_before_later_siblings(self):
        ids = [node["id"] for node in cat.walk(build_catalog().tree)]
        self.assertLess(ids.index("_12_1"), ids.index("_20_1"))


class SelectionPlanningTests(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog()

    def test_topmost_collapses_nested_selection(self):
        """Ticking a folder and its child must download the child only once."""
        plan = self.catalog.plan({"_10_1", "_12_1"})
        self.assertEqual(plan, {"_10_1"})

    def test_siblings_are_kept(self):
        self.assertEqual(self.catalog.plan({"_12_1", "_21_1"}), {"_12_1", "_21_1"})

    def test_empty_selection(self):
        self.assertEqual(self.catalog.plan(set()), set())
        self.assertEqual(self.catalog.plan(None), set())

    def test_count_items_for_selection(self):
        self.assertEqual(self.catalog.count_items({"_10_1"}), 3)
        self.assertEqual(self.catalog.count_items({"_12_1", "_21_1"}), 2)
        self.assertEqual(self.catalog.count_items({"_40_1"}), 1)  # assessment still counts as an item

    def test_count_matches_topmost_not_raw(self):
        self.assertEqual(
            self.catalog.count_items({"_10_1", "_12_1", "_11_1"}),
            self.catalog.count_items({"_10_1"}),
        )

    def test_announcements_split_from_content(self):
        selection = {"_12_1", "_a1_1"}
        self.assertEqual(self.catalog.announce_ids(selection), {"_a1_1"})
        self.assertEqual(self.catalog.content_ids(selection), {"_12_1"})

    def test_whole_announcements_folder_selects_all_announcements(self):
        self.assertEqual(
            self.catalog.announce_ids({cat.ANNOUNCEMENTS_ID}), {"_a1_1", "_a2_1"}
        )
        self.assertEqual(self.catalog.content_ids({cat.ANNOUNCEMENTS_ID}), set())


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_cat_")
        self.path = os.path.join(self.tmp, "catalog.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        catalog = build_catalog()
        catalog.save(self.path)
        loaded = cat.Catalog.load(self.path)
        self.assertEqual(loaded.course_id, "_999_1")
        self.assertEqual(len(loaded.tree), len(catalog.tree))
        self.assertEqual(loaded.count_items({"_10_1"}), 3)
        self.assertEqual(loaded.breadcrumb("_12_1")[0]["title"], "Week 1")

    def test_saved_file_is_valid_utf8_json(self):
        build_catalog().save(self.path)
        with open(self.path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["courseId"], "_999_1")

    def test_version_mismatch_is_ignored(self):
        build_catalog().save(self.path)
        with open(self.path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["version"] = 999
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        self.assertIsNone(cat.Catalog.load(self.path))

    def test_corrupt_cache_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(cat.Catalog.load(self.path))

    def test_missing_cache_is_none(self):
        self.assertIsNone(cat.Catalog.load(os.path.join(self.tmp, "nope.json")))
        self.assertIsNone(cat.Catalog.load(None))

    def test_cache_path_is_sanitised(self):
        path = cat.catalog_cache_path(self.tmp, "_12529_1")
        self.assertTrue(path.endswith(".json"))
        self.assertNotIn("/", os.path.basename(path))


class SelectiveDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_sel_")
        self.session = FakeSession()
        self.catalog = build_catalog(self.session)
        self.jobs = selective.JobManager()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, ids, overwrite=False):
        """Run a download through the real JobManager and wait for it to finish."""
        job = self.jobs.create("download", "test", total=0)
        downloader = selective.SelectiveDownloader(
            self.session, self.catalog, self.tmp, lambda *a: None, overwrite=overwrite
        )
        self.jobs.start(job, downloader.run, ids)
        self.assertTrue(wait_for_job(job), "job did not settle")
        return job, job.result

    def _all_files(self):
        out = []
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                out.append(os.path.relpath(os.path.join(root, name), self.tmp).replace("\\", "/"))
        return sorted(out)

    def test_single_deep_leaf_creates_ancestors(self):
        job, _ = self._run({"_12_1"})
        files = self._all_files()
        self.assertIn("content/Week 1/01_Day 1/01_Notes.md", files)
        # The embedded attachment is fetched as part of the document.
        self.assertIn("content/Week 1/01_Day 1/notes.pdf", files)

    def test_single_leaf_does_not_pull_siblings(self):
        self._run({"_12_1"})
        files = self._all_files()
        self.assertFalse(any("Overview" in f for f in files))
        self.assertFalse(any("Slides" in f for f in files))
        self.assertFalse(any("Recap" in f for f in files))

    def test_selecting_folder_pulls_whole_subtree(self):
        job, _ = self._run({"_10_1"})
        files = self._all_files()
        self.assertIn("content/Week 1/01_Day 1/01_Notes.md", files)
        self.assertIn("content/Week 1/02_Overview.md", files)
        self.assertFalse(any("Recap" in f for f in files))

    def test_document_body_survives_the_catalog_round_trip(self):
        """Regression: a catalog that dropped `body` produced empty documents."""
        self._run({"_14_1"})
        path = os.path.join(self.tmp, "content", "Week 1", "02_Overview.md")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Overview text", text)

    def test_selected_folder_is_not_written_as_a_leaf(self):
        """Regression: a selected folder rendered as `Week 1.md` instead of a tree."""
        self._run({"_10_1"})
        files = self._all_files()
        self.assertNotIn("content/Week 1.md", files)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "content", "Week 1")))

    def test_file_node_uses_attachment_name_with_ordinal(self):
        """Root-level items carry no ordinal - matching a full pull exactly."""
        self._run({"_30_1"})
        files = self._all_files()
        self.assertIn("content/Syllabus.pdf", files)

    def test_nested_file_gets_ordinal_inside_folder(self):
        self._run({"_11_1"})
        files = self._all_files()
        self.assertIn("content/Week 1/01_Day 1/02_Slides.pdf", files)

    def test_folder_and_child_together_download_once(self):
        job, _ = self._run({"_10_1", "_12_1"})
        # Only the folder was materialised; the child was not written twice.
        occurrences = [f for f in self._all_files() if f.endswith("01_Notes.md")]
        self.assertEqual(len(occurrences), 1)
        self.assertGreater(job.files_saved, 0)

    def test_selection_across_distant_branches(self):
        self._run({"_12_1", "_21_1"})
        files = self._all_files()
        self.assertIn("content/Week 1/01_Day 1/01_Notes.md", files)
        self.assertIn("content/Week 2/01_Recap.md", files)

    def test_announcement_subset_only(self):
        self._run({"_a1_1"})
        files = self._all_files()
        self.assertTrue(any("Welcome" in f for f in files))
        self.assertFalse(any("Exam date" in f for f in files))

    def test_announcements_folder_selects_all(self):
        self._run({cat.ANNOUNCEMENTS_ID})
        files = self._all_files()
        self.assertTrue(any("Welcome" in f for f in files))
        self.assertTrue(any("Exam date" in f for f in files))

    def test_empty_selection_writes_nothing(self):
        _job, _summary = self._run(set())
        self.assertEqual(self._all_files(), [])

    def test_progress_is_reported(self):
        job, _ = self._run({"_10_1"})
        snapshot = job.snapshot()
        self.assertEqual(snapshot["state"], selective.JOB_DONE)
        self.assertGreater(snapshot["filesSaved"], 0)
        self.assertGreater(snapshot["bytes"], 0)
        self.assertTrue(snapshot["results"])
        self.assertTrue(snapshot["log"])
        self.assertEqual(snapshot["percent"], 100)

    def test_second_run_skips_existing_files(self):
        self._run({"_10_1"})
        job, _ = self._run({"_10_1"})
        self.assertGreater(job.files_skipped, 0, "existing files should be skipped")
        self.assertEqual(job.files_saved, 0)

    def test_layout_matches_full_pull(self):
        """Selective output must land in the same paths a full pull would use."""
        from bbpull.course import CoursePuller

        full_root = os.path.join(self.tmp, "full")
        puller = CoursePuller(self.session, "_999_1", os.path.join(full_root, "content"),
                              lambda *a: None)
        puller.fetch_tree()
        puller.write()

        self._run({"_12_1", "_21_1"})
        full_files = set()
        for root, _dirs, files in os.walk(os.path.join(full_root, "content")):
            for name in files:
                full_files.add(
                    os.path.relpath(os.path.join(root, name),
                                    os.path.join(full_root, "content")).replace("\\", "/")
                )
        for rel in self._all_files():
            if rel.startswith("content/"):
                self.assertIn(rel[len("content/"):], full_files, f"{rel} not produced by full pull")


def job_files_saved(summary):
    return (summary or {}).get("saved", 0)


class JobManagerTests(unittest.TestCase):
    def test_job_completes_and_records_state(self):
        manager = selective.JobManager()
        job = manager.create("test", "demo", total=2)

        def work(j, *_):
            j.note("working")
            j.processed = 2

        manager.start(job, work)
        for _ in range(100):
            if job.state in selective.TERMINAL_STATES:
                break
            time.sleep(0.01)
        snapshot = manager.get(job.id).snapshot()
        self.assertEqual(snapshot["state"], selective.JOB_DONE)
        self.assertEqual(snapshot["percent"], 100)
        self.assertIsNotNone(snapshot["finished"])

    def test_job_error_is_captured_not_lost(self):
        manager = selective.JobManager()
        job = manager.create("test", "boom", total=1)

        def work(_j, *_):
            raise RuntimeError("kaboom")

        manager.start(job, work)
        for _ in range(100):
            if job.state in selective.TERMINAL_STATES:
                break
            time.sleep(0.01)
        snapshot = manager.get(job.id).snapshot()
        self.assertEqual(snapshot["state"], selective.JOB_ERROR)
        self.assertEqual(snapshot["errorCount"], 1)
        self.assertIn("kaboom", snapshot["errors"][0]["error"])

    def test_cancel_stops_a_running_job(self):
        manager = selective.JobManager()
        job = manager.create("test", "long", total=100)

        def work(j, *_):
            for _ in range(100):
                j.check_cancelled()
                time.sleep(0.01)
                j.processed += 1

        manager.start(job, work)
        time.sleep(0.05)
        self.assertTrue(manager.cancel(job.id))
        for _ in range(200):
            if job.state in selective.TERMINAL_STATES:
                break
            time.sleep(0.01)
        self.assertEqual(job.state, selective.JOB_CANCELLED)

    def test_cancel_is_rejected_for_finished_job(self):
        manager = selective.JobManager()
        job = manager.create("test", "quick", total=1)
        manager.start(job, lambda j, *_: None)
        for _ in range(100):
            if job.state in selective.TERMINAL_STATES:
                break
            time.sleep(0.01)
        self.assertFalse(manager.cancel(job.id))

    def test_cancelled_download_keeps_partial_results(self):
        tmp = tempfile.mkdtemp(prefix="bbpull_cancel_")
        try:
            session = FakeSession()
            catalog = build_catalog(session)
            manager = selective.JobManager()
            job = manager.create("download", "cancel me", total=0)

            def work(j, *_):
                # Cancel immediately: the walk must stop before writing.
                j.cancel_event.set()
                selective.SelectiveDownloader(
                    session, catalog, tmp, lambda *a: None
                ).run(j, {"_10_1"})

            manager.start(job, work)
            for _ in range(200):
                if job.state in selective.TERMINAL_STATES:
                    break
                time.sleep(0.01)
            self.assertEqual(job.state, selective.JOB_CANCELLED)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_latest_and_snapshots(self):
        manager = selective.JobManager()
        first = manager.create("a", "first")
        second = manager.create("b", "second")
        self.assertEqual(manager.latest().id, second.id)
        snapshots = manager.all_snapshots()
        self.assertEqual(snapshots[0]["id"], second.id)
        self.assertEqual(len(snapshots), 2)

    def test_manager_trims_history(self):
        manager = selective.JobManager(keep=3)
        for i in range(6):
            manager.create("t", f"job{i}")
        self.assertEqual(len(manager.all_snapshots(limit=10)), 3)


class CourseDirTests(unittest.TestCase):
    def test_course_dir_naming_is_safe(self):
        catalog = build_catalog()
        catalog.course_name = "NUR2051/NUR2046 Nursing: Practicum I"
        path = selective.course_dir_for("out", catalog)
        self.assertTrue(path.startswith("out"))
        self.assertNotIn("/", os.path.basename(path).replace("\\", ""))
        self.assertIn("_999_1", os.path.basename(path))


if __name__ == "__main__":
    unittest.main()
