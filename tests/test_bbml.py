"""Offline unit tests - no network, no credentials required.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull import bbml  # noqa: E402
from bbpull.paths import dedupe, filename_from_url, readable_name, sanitize  # noqa: E402


class SanitizeTests(unittest.TestCase):
    def test_illegal_characters_replaced(self):
        self.assertEqual(sanitize('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_unicode_survives(self):
        self.assertEqual(sanitize("課程大綱 第一章"), "課程大綱 第一章")

    def test_windows_reserved_name(self):
        self.assertTrue(sanitize("CON").startswith("_"))
        self.assertTrue(sanitize("nul.txt").startswith("_"))

    def test_trailing_dot_and_space_stripped(self):
        self.assertEqual(sanitize("homework. "), "homework")

    def test_empty_falls_back(self):
        self.assertEqual(sanitize("   ", fallback="item_9"), "item_9")

    def test_long_name_keeps_extension(self):
        name = sanitize("x" * 300 + ".pdf", max_len=60)
        self.assertLessEqual(len(name), 60)
        self.assertTrue(name.endswith(".pdf"))


class DedupeTests(unittest.TestCase):
    def test_no_collision_passthrough(self):
        self.assertEqual(dedupe({"a.txt"}, "b.txt"), "b.txt")

    def test_collision_gets_suffix(self):
        used = {"a.txt", "a (2).txt"}
        self.assertEqual(dedupe(used, "a.txt"), "a (3).txt")


class UrlNameTests(unittest.TestCase):
    def test_xid_url(self):
        url = "https://bb.example.edu/bbcswebdav/pid-1-dt-content-rid-2_1/xid-2_1?x=1"
        self.assertEqual(filename_from_url(url), "xid-2_1")

    def test_encoded_name(self):
        url = "https://bb.example.edu/bbcswebdav/xid-9_1/%E8%AA%B2%E7%A8%8B.pdf"
        self.assertEqual(filename_from_url(url), "課程.pdf")


class ReadableNameTests(unittest.TestCase):
    """The rule that separates a real display name from an opaque xid fragment."""

    def test_plain_file_name_accepted(self):
        self.assertEqual(readable_name("lecture01.pdf"), "lecture01.pdf")

    def test_unicode_name_accepted(self):
        self.assertEqual(readable_name("課程大綱.docx"), "課程大綱.docx")

    def test_url_derived_name_accepted(self):
        self.assertEqual(readable_name("https://x/y/notes.PDF"), "notes.PDF")

    def test_query_string_rejected(self):
        self.assertIsNone(readable_name("xid-202_1?t=1"))

    def test_unknown_extension_rejected(self):
        self.assertIsNone(readable_name("xid-202_1"))

    def test_empty_rejected(self):
        self.assertIsNone(readable_name(""))
        self.assertIsNone(readable_name(None))

    def test_illegal_characters_neutralised(self):
        self.assertEqual(readable_name('a:b*c.pdf'), "a_b_c.pdf")


SAMPLE_BODY = (
    '<!-- {"bbMLEditorVersion":1} -->'
    '<div><h4>Week 1</h4>'
    '<p>Read the <strong>syllabus</strong>.</p>'
    '<ul><li>Item A</li><li>Item B</li></ul>'
    '<p><a href="https://bb.example.edu/bbcswebdav/pid-11-dt-content-rid-22_1/xid-22_1?t=1" '
    'data-bbfile="{&quot;render&quot;:&quot;attachment&quot;,'
    '&quot;linkName&quot;:&quot;lecture01.pdf&quot;,&quot;mimeType&quot;:&quot;application/pdf&quot;}">'
    "lecture01.pdf</a></p>"
    '<p><img src="https://bb.example.edu/bbcswebdav/xid-33_1" alt="diagram"></p>'
    '<p>External: <a href="https://example.com/reading">reading</a></p>'
    "</div>"
)


class BbmlTests(unittest.TestCase):
    def test_is_webdav_url(self):
        self.assertTrue(bbml.is_webdav_url("https://x/bbcswebdav/xid-1_1"))
        self.assertTrue(bbml.is_webdav_url("/bbcswebdav/pid-1-dt-content-rid-2_1/xid-2_1"))
        self.assertFalse(bbml.is_webdav_url("https://example.com/reading"))
        self.assertFalse(bbml.is_webdav_url("mailto:a@b.c"))
        self.assertFalse(bbml.is_webdav_url("data:image/png;base64,AAAA"))

    def test_extract_file_links_only_webdav(self):
        links = bbml.extract_file_links(SAMPLE_BODY)
        urls = [item["url"] for item in links]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("bbcswebdav" in u for u in urls))
        # The attachment anchor's linkName is recovered from data-bbfile.
        self.assertEqual(links[0]["name"], "lecture01.pdf")

    def test_extract_dedupes(self):
        body = SAMPLE_BODY + SAMPLE_BODY
        self.assertEqual(len(bbml.extract_file_links(body)), 2)

    def test_markdown_structure(self):
        md = bbml.to_markdown(SAMPLE_BODY)
        self.assertIn("#### Week 1", md)
        self.assertIn("**syllabus**", md)
        self.assertIn("- Item A", md)
        self.assertIn("[lecture01.pdf]", md)
        self.assertIn("[reading](https://example.com/reading)", md)

    def test_link_map_rewrites_local_paths(self):
        url = "https://bb.example.edu/bbcswebdav/pid-11-dt-content-rid-22_1/xid-22_1?t=1"
        md = bbml.to_markdown(SAMPLE_BODY, link_map={url: "lecture01.pdf"})
        self.assertIn("(lecture01.pdf)", md)
        self.assertNotIn("pid-11-dt-content-rid-22_1", md)

    def test_markdown_table(self):
        body = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        md = bbml.to_markdown(body)
        self.assertIn("| A | B |", md)
        self.assertIn("| --- | --- |", md)
        self.assertIn("| 1 | 2 |", md)

    def test_to_text_flattens(self):
        text = bbml.to_text(SAMPLE_BODY)
        self.assertIn("Week 1", text)
        self.assertNotIn("<", text)
        self.assertIn("Item A", text)

    def test_empty_body_safe(self):
        self.assertEqual(bbml.to_markdown(""), "")
        self.assertEqual(bbml.to_text(None), "")
        self.assertEqual(bbml.extract_file_links(None), [])


class MarkdownEdgeTests(unittest.TestCase):
    def test_script_and_style_dropped(self):
        md = bbml.to_markdown("<style>p{color:red}</style><script>alert(1)</script><p>hi</p>")
        self.assertNotIn("alert", md)
        self.assertNotIn("color:red", md)
        self.assertIn("hi", md)

    def test_pre_block_preserved(self):
        md = bbml.to_markdown("<pre>line1\nline2</pre>")
        self.assertIn("```", md)
        self.assertIn("line1", md)

    def test_ordered_list(self):
        md = bbml.to_markdown("<ol><li>one</li><li>two</li></ol>")
        self.assertIn("1. one", md)
        self.assertIn("1. two", md)

    def test_entities_decoded(self):
        self.assertEqual(bbml.to_markdown("<p>a &amp; b &lt;c&gt;</p>"), "a & b <c>")


if __name__ == "__main__":
    unittest.main()
