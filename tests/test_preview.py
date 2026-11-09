"""Preview helpers: what an item says before anything is downloaded.

These are pure functions (no Qt, no Tk, no network). The dangerous mistake they
guard against is a preview that quietly downloads: images are replaced with a
text placeholder, and a missing body produces an explanatory message instead of
an empty window.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull.catalog import announcement_node  # noqa: E402
from bbpull.preview import (  # noqa: E402
    attachment_row,
    format_stamp,
    is_image_name,
    item_attachments,
    item_body_markdown,
    item_metadata,
    placeholder_text,
    preview_cache_path,
    strip_images,
)


def document_node(body=""):
    return {
        "id": "_101_1",
        "title": "Week 1 notes",
        "kind": "document",
        "payload": {
            "id": "_101_1",
            "title": "Week 1 notes",
            "body": body,
            "created": "2026-08-01T09:00:00.000Z",
            "modified": "2026-08-28T08:34:34.214Z",
            "contentHandler": {"id": "resource/x-bb-document"},
        },
    }


class BodyTests(unittest.TestCase):
    def test_document_body_is_rendered_as_markdown(self):
        markdown = item_body_markdown(document_node(
            "<p>Hello <strong>world</strong></p>"))
        self.assertIn("Hello", markdown)
        self.assertIn("world", markdown)

    def test_missing_body_is_empty(self):
        self.assertEqual(item_body_markdown(document_node("")), "")

    def test_images_become_placeholders(self):
        """Previewing must not start downloading media."""
        self.assertEqual(strip_images("![圖A](https://x/y.png)"),
                         "（圖片：圖A）")
        self.assertEqual(strip_images("![](https://x/y.png)"), "（圖片）")

    def test_announcement_body_is_available_from_the_catalog(self):
        """Announcements now carry their body, so preview works offline."""
        node = announcement_node([{
            "id": "_9_1", "title": "Welcome",
            "body": "<p>See you on Monday.</p>",
            "created": "2026-09-01T00:00:00.000Z",
            "modified": "2026-09-01T00:00:00.000Z",
        }])["children"][0]
        self.assertIn("Monday", item_body_markdown(node))


class AttachmentTests(unittest.TestCase):
    """PDFs/Excel live inside document bodies; the preview must name them."""

    BODY = ('<p>Dear students</p>'
            '<a href="https://twc.blackboard.com/bbcswebdav/pid-1-dt-content-'
            'rid-2_1/xid-2_1" data-bbfile=\'{"linkName":"notes.pdf"}\'>notes.pdf</a>'
            '<a href="https://twc.blackboard.com/bbcswebdav/pid-1-dt-content-'
            'rid-3_1/xid-3_1" data-bbfile=\'{"linkName":"marks.xlsx"}\'>marks.xlsx</a>')

    def test_embedded_files_are_listed_with_their_names(self):
        node = document_node(self.BODY)
        names = [item["name"] for item in item_attachments(node)]
        self.assertEqual(names, ["notes.pdf", "marks.xlsx"])

    def test_duplicates_are_removed(self):
        node = document_node(self.BODY + self.BODY)
        self.assertEqual(len(item_attachments(node)), 2)


class MetadataTests(unittest.TestCase):
    def test_document_metadata(self):
        rows = dict(item_metadata(document_node("<p>x</p>")))
        self.assertEqual(rows["類型"], "文件")
        self.assertEqual(rows["內容 id"], "_101_1")
        self.assertEqual(rows["修改"], "2026-08-28 08:34")

    def test_path_is_included_when_given(self):
        rows = dict(item_metadata(document_node(), "Week 1 › Notes"))
        self.assertEqual(rows["位置"], "Week 1 › Notes")

    def test_file_metadata_comes_from_the_content_handler(self):
        node = {
            "id": "_2_1", "title": "Slides", "kind": "file",
            "payload": {"contentHandler": {"id": "resource/x-bb-file",
                                            "file": {"fileName": "slides.pdf",
                                                     "mimeType": "application/pdf"}}},
        }
        rows = dict(item_metadata(node))
        self.assertEqual(rows["檔名"], "slides.pdf")
        self.assertEqual(rows["檔案類型"], "application/pdf")

    def test_link_metadata_includes_the_url(self):
        node = {
            "id": "_3_1", "title": "Zoom", "kind": "link",
            "payload": {"contentHandler": {"id": "resource/x-bb-externallink",
                                            "url": "https://zoom.example/room"}},
        }
        self.assertEqual(dict(item_metadata(node))["網址"],
                         "https://zoom.example/room")

    def test_folder_metadata_counts_items(self):
        node = {"id": "_4_1", "title": "Week 2", "kind": "folder", "itemCount": 7}
        self.assertEqual(dict(item_metadata(node))["內含"], "7 個項目")


class StampTests(unittest.TestCase):
    def test_formats_iso_timestamp(self):
        self.assertEqual(format_stamp("2026-08-28T08:34:34.214Z"),
                         "2026-08-28 08:34")

    def test_empty_is_empty(self):
        self.assertEqual(format_stamp(None), "")
        self.assertEqual(format_stamp(""), "")


class PlaceholderTests(unittest.TestCase):
    def test_each_kind_explains_itself(self):
        self.assertIn("下載", placeholder_text({"kind": "file"}))
        self.assertIn("瀏覽器", placeholder_text({"kind": "link"}))
        self.assertIn("預覽", placeholder_text({"kind": "assessment"}))
        self.assertIn("公告", placeholder_text({"kind": "announcement"}))
        self.assertIn("資料夾", placeholder_text({"kind": "folder",
                                                  "itemCount": 3}))


class AttachmentRowTests(unittest.TestCase):
    def test_virtual_row_carries_parent_and_url(self):
        row = attachment_row(
            {"id": "_101_1", "title": "Week 1", "depth": 2},
            {"url": "https://twc.blackboard.com/bbcswebdav/xid-1_1",
             "name": "notes.pdf"}, 1)
        self.assertEqual(row["kind"], "attachment")
        self.assertEqual(row["title"], "notes.pdf")
        self.assertTrue(row["virtual"])
        self.assertTrue(row["downloadable"])
        self.assertEqual(row["parentContentId"], "_101_1")
        self.assertEqual(row["attachmentUrl"],
                         "https://twc.blackboard.com/bbcswebdav/xid-1_1")
        self.assertEqual(row["id"], "__attachment__:_101_1:1")
        self.assertEqual(row["depth"], 3)


class PreviewCacheTests(unittest.TestCase):
    def test_images_are_detected_case_insensitively(self):
        self.assertTrue(is_image_name("Photo.JPG"))
        self.assertTrue(is_image_name("diagram.png"))
        self.assertFalse(is_image_name("notes.pdf"))
        self.assertFalse(is_image_name(""))

    def test_cache_path_is_per_item_and_sanitised(self):
        import tempfile

        path = preview_cache_path("_101_1", "../evil/name.pdf")
        root = os.path.join(tempfile.gettempdir(), "bbpull-preview")
        self.assertTrue(path.startswith(root))
        self.assertEqual(os.path.dirname(path),
                         os.path.join(root, "_101_1"),
                         "each item gets its own folder")
        self.assertNotIn(os.sep, os.path.basename(path),
                         "a name can never escape its item folder")
        self.assertTrue(path.endswith(".pdf"))


if __name__ == "__main__":
    unittest.main()
