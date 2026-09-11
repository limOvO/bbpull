"""End-to-end-ish tests for the content/announcement writers using a fake client.

These exercise the real traversal + rendering + naming + link-rewriting code
paths against canned Blackboard JSON, with no network access.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull.announcements import AnnouncementPuller  # noqa: E402
from bbpull.cli import match_course  # noqa: E402
from bbpull.course import CoursePuller  # noqa: E402
from bbpull.errors import ConfigError  # noqa: E402

WEBDAV = "https://bb.example.edu/bbcswebdav/pid-101-dt-content-rid-202_1/xid-202_1?t=1"
WEBDAV_IMG = "https://bb.example.edu/bbcswebdav/xid-303_1"
WEBDAV_ANN = "https://bb.example.edu/bbcswebdav/pid-901-dt-content-rid-902_1/xid-902_1?t=2"

CONTENTS = [
    {
        "id": "_10_1",
        "parentId": "_0_1",
        "title": "Week 1 / 第一週",
        "position": 0,
        "hasChildren": True,
        "contentHandler": {"id": "resource/x-bb-folder"},
    },
    {
        "id": "_20_1",
        "parentId": "_0_1",
        "title": "Syllabus.pdf",
        "position": 1,
        "contentHandler": {"id": "resource/x-bb-file", "file": {"fileName": "Syllabus.pdf"}},
    },
]

CHILDREN = {
    "_10_1": [
        {
            "id": "_30_1",
            "parentId": "_10_1",
            "title": "Lecture notes",
            "position": 0,
            "body": (
                '<div><h4>Agenda</h4><ul><li>Intro</li></ul>'
                f'<p><a href="{WEBDAV}" data-bbfile="{{&quot;linkName&quot;:&quot;notes.pdf&quot;}}">notes.pdf</a></p>'
                f'<p><img src="{WEBDAV_IMG}" alt="chart"></p></div>'
            ),
            "contentHandler": {"id": "resource/x-bb-document"},
        },
        {
            "id": "_40_1",
            "parentId": "_10_1",
            "title": "Discussion link",
            "position": 1,
            "contentHandler": {"id": "resource/x-bb-externallink"},
            "links": [{"href": "/ultra/redirect?redirectType=nautilus&contentId=_40_1"}],
        },
        {
            "id": "_50_1",
            "parentId": "_10_1",
            "title": "Empty folder",
            "position": 2,
            "hasChildren": False,
            "contentHandler": {"id": "resource/x-bb-folder"},
        },
    ]
}

ATTACHMENTS = {
    "_20_1": {
        "results": [
            {"id": "_21_1", "fileName": "Syllabus.pdf"},
        ]
    }
}

DOWNLOADS = {
    WEBDAV: b"%PDF-1.4 notes",
    WEBDAV_IMG: b"\x89PNG chart",
    WEBDAV_ANN: b"%PDF-1.4 notes",
    "https://bb.example.edu/learn/api/public/v1/courses/_999_1/contents/_20_1/attachments/_21_1/download": b"%PDF-1.4 syllabus",
}

ANNOUNCEMENTS = [
    {
        "id": "_a1_1",
        "title": "Welcome",
        "created": "2026-02-01T09:00:00.000Z",
        "modified": "2026-02-01T09:05:00.000Z",
        "body": (
            '<div><p>Read <a href="' + WEBDAV_ANN + '" '
            'data-bbfile="{&quot;linkName&quot;:&quot;notes.pdf&quot;}">notes.pdf</a> first.</p></div>'
        ),
        "availability": {"duration": {"type": "Permanent"}},
    },
    {
        "id": "_a2_1",
        "title": "No body here",
        "created": "2026-03-11T23:30:00.000Z",
        "availability": {"duration": {"type": "Restricted", "start": "2026-03-11T00:00:00.000Z"}},
    },
]


class FakeClient:
    """Stands in for LearnSession: same surface, canned data, real file writes."""

    base = "https://bb.example.edu"
    course_id = "_999_1"

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
            content_id = path.split("/contents/")[1].split("/")[0]
            return CHILDREN.get(content_id, [])
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
        if url not in DOWNLOADS:
            return "failed", 0, "not in fake store"
        if os.path.exists(dest) and not overwrite:
            return "skipped", os.path.getsize(dest), dest
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(DOWNLOADS[url])
        return "saved", len(DOWNLOADS[url]), dest


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class CoursePullerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_test_")
        self.client = FakeClient()
        self.logs = []
        self.puller = CoursePuller(
            self.client, "_999_1", os.path.join(self.tmp, "content"), self.logs.append
        )
        self.puller.fetch_tree()
        self.index = self.puller.write()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tree_shape_counts(self):
        self.assertEqual(self.puller.stats["nodes"], 5)
        self.assertEqual(self.puller.stats["documents"], 1)
        self.assertEqual(len(self.puller.roots), 2)

    def test_top_level_names_and_unicode(self):
        children = os.listdir(os.path.join(self.tmp, "content"))
        self.assertIn("Week 1 _ 第一週", children)
        self.assertTrue(any(name.endswith("Syllabus.pdf") for name in children))

    def test_unsafe_slash_in_title_is_neutralised(self):
        # "Week 1 / 第一週" must not create an extra directory level.
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "content", "Week 1 ")))

    def test_document_markdown_written(self):
        path = os.path.join(self.tmp, "content", "Week 1 _ 第一週", "01_Lecture notes.md")
        self.assertTrue(os.path.isfile(path), "document markdown missing")
        md = read(path)
        self.assertIn("#### Agenda", md)
        self.assertIn("- Intro", md)
        self.assertIn("resource/x-bb-document", md)

    def test_embedded_files_downloaded_into_same_dir(self):
        folder = os.path.join(self.tmp, "content", "Week 1 _ 第一週")
        self.assertTrue(os.path.isfile(os.path.join(folder, "notes.pdf")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "xid-303_1")))

    def test_markdown_links_rewritten_to_local_paths(self):
        md = read(os.path.join(self.tmp, "content", "Week 1 _ 第一週", "01_Lecture notes.md"))
        self.assertIn("](notes.pdf)", md)
        self.assertIn("![chart](xid-303_1)", md)
        self.assertNotIn("pid-101-dt-content-rid-202_1", md)

    def test_sidecar_json_written_with_plain_text(self):
        sidecar = os.path.join(self.tmp, "content", "Week 1 _ 第一週", "01_Lecture notes.json")
        payload = json.loads(read(sidecar))
        self.assertEqual(payload["content"]["id"], "_30_1")
        self.assertIn("Agenda", payload["plainText"])

    def test_file_node_uses_attachment_download(self):
        root = os.path.join(self.tmp, "content")
        pdfs = [n for n in os.listdir(root) if n.endswith("Syllabus.pdf")]
        self.assertEqual(len(pdfs), 1)
        self.assertTrue(os.path.getsize(os.path.join(root, pdfs[0])) > 0)
        self.assertTrue(
            any("/attachments/_21_1/download" in url for url in self.client.downloads)
        )

    def test_empty_folder_creates_directory(self):
        week = os.path.join(self.tmp, "content", "Week 1 _ 第一週")
        entries = os.listdir(week)
        self.assertTrue(
            any("Empty folder" in name and os.path.isdir(os.path.join(week, name)) for name in entries),
            f"expected an Empty folder directory, saw {entries}",
        )

    def test_link_without_body_written_as_note(self):
        week = os.path.join(self.tmp, "content", "Week 1 _ 第一週")
        notes = [n for n in os.listdir(week) if n.startswith("02_Discussion link") and n.endswith(".md")]
        self.assertEqual(len(notes), 1)
        self.assertIn("UI view", read(os.path.join(week, notes[0])))

    def test_rerun_is_idempotent_and_skips(self):
        before = self.client.downloads[:]
        second = CoursePuller(
            self.client, "_999_1", os.path.join(self.tmp, "content"), self.logs.append
        )
        second.fetch_tree()
        second.write()
        self.assertEqual(second.stats["files_failed"], 0)
        # Second pass must not create " (2)" duplicates of the same files.
        week = os.path.join(self.tmp, "content", "Week 1 _ 第一週")
        self.assertEqual(len([n for n in os.listdir(week) if n == "notes.pdf"]), 1)
        self.assertTrue(len(self.client.downloads) > len(before))


class AnnouncementPullerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_ann_")
        self.client = FakeClient()
        self.puller = AnnouncementPuller(
            self.client, "_999_1", self.tmp, lambda *a: None
        )
        self.puller.fetch()
        self.rows = self.puller.write()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_count_and_order(self):
        self.assertEqual(len(self.rows), 2)
        self.assertLess(self.rows[0]["date"], self.rows[1]["date"])

    def test_files_named_with_date_and_title(self):
        names = os.listdir(self.tmp)
        self.assertTrue(any(n.startswith("2026-02-01_001_Welcome") for n in names))
        self.assertTrue(any(n.startswith("2026-03-11_002_No body here") for n in names))

    def test_body_markdown_and_attachment(self):
        md = read(os.path.join(self.tmp, "2026-02-01_001_Welcome.md"))
        self.assertIn("Read", md)
        self.assertIn("attachments/notes.pdf", md)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "attachments", "notes.pdf")))

    def test_empty_body_gets_placeholder(self):
        md = read(os.path.join(self.tmp, "2026-03-11_002_No body here.md"))
        self.assertIn("no body text", md)

    def test_index_files(self):
        index = read(os.path.join(self.tmp, "announcements.md"))
        self.assertIn("Welcome", index)
        payload = json.loads(read(os.path.join(self.tmp, "announcements.json")))
        self.assertEqual(len(payload["results"]), 2)


class MatchCourseTests(unittest.TestCase):
    COURSES = [
        {"courseId": "_1_1", "name": "Data Structures"},
        {"courseId": "_2_1", "name": "作業系統"},
        {"courseId": "_3_1", "name": "Operating Systems Lab"},
    ]

    def test_by_index(self):
        self.assertEqual(match_course("1", self.COURSES), "_1_1")

    def test_by_id(self):
        self.assertEqual(match_course("_2_1", self.COURSES), "_2_1")

    def test_by_keyword(self):
        self.assertEqual(match_course("作業", self.COURSES), "_2_1")

    def test_ambiguous_keyword_raises(self):
        with self.assertRaises(ConfigError):
            match_course("s", self.COURSES)

    def test_unknown_raises(self):
        with self.assertRaises(ConfigError):
            match_course("nope", self.COURSES)


if __name__ == "__main__":
    unittest.main()
