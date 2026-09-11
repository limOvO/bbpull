"""BBML (Blackboard Markup Language) post-processing.

Two jobs:
  1. Pull embedded file URLs out of an Ultra content item body so attachments
     that live inside the rich text can be downloaded.
  2. Convert the HTML-ish body to readable Markdown + plain text, rewriting
     embedded file links to the local relative path we actually saved.

Pure functions only - no I/O, no network. That makes the whole module testable
offline, which is what `tests/test_bbml.py` does.
"""

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

# Absolute or protocol-relative URLs that live in the Blackboard content store.
_WEBDAV_HINT = re.compile(r"(bbcswebdav|/xid-|/pid-|/bbcswebdav/)", re.I)
_ABS_URL = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"']+)["']""",
    re.I,
)
_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(
    r"</\s*(p|div|li|tr|h[1-6]|blockquote|pre|table|section|article)\s*>", re.I
)
_BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
_BBML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_STYLE = re.compile(r"<\s*(script|style)\b.*?</\s*\1\s*>", re.I | re.S)
_IMG = re.compile(r"<\s*img\b[^>]*>", re.I)
_MULTI_NL = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")


def is_webdav_url(url):
    """True when a URL points at a Blackboard-stored file we could download."""
    if not url:
        return False
    if url.startswith("data:") or url.startswith("mailto:"):
        return False
    parsed = urlparse(url)
    if parsed.scheme and parsed.scheme not in ("http", "https", ""):
        return False
    return bool(_WEBDAV_HINT.search(url))


def extract_file_links(body):
    """Return an ordered, de-duplicated list of candidate attachment URLs.

    Also understands the `data-bbfile` JSON attribute Blackboard attaches to
    embedded-file anchors, which carries the human-readable file name.
    """
    if not body:
        return []
    out = []
    seen = set()
    for match in _ABS_URL.finditer(body):
        url = html.unescape(match.group(1)).strip()
        if not is_webdav_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "name": _name_from_databbfile(body, url) or None})
    return out


def _name_from_databbfile(body, url):
    """Find the anchor carrying `url` and read linkName out of data-bbfile."""
    for anchor in re.finditer(r"<a\b[^>]*>", body, re.I):
        tag = anchor.group(0)
        if url.split("?")[0] not in html.unescape(tag):
            continue
        raw = re.search(r'data-bbfile\s*=\s*(["\'])(.*?)\1', tag, re.S | re.I)
        if not raw:
            continue
        payload = html.unescape(raw.group(2))
        try:
            meta = json.loads(payload)
        except (ValueError, TypeError):
            continue
        for key in ("linkName", "alternativeText", "fileName"):
            value = meta.get(key)
            if value and str(value).strip():
                return str(value).strip()
    return None


def to_text(body):
    """Flatten BBML/HTML to plain text (kept for txt sidecars and search)."""
    if not body:
        return ""
    text = _SCRIPT_STYLE.sub(" ", body)
    text = _BBML_COMMENT.sub("", text)
    text = _IMG.sub(" [image] ", text)
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = _TRAILING_WS.sub("\n", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


class _Markdown(HTMLParser):
    """Small, dependency-free HTML -> Markdown converter.

    Handles the subset Blackboard actually emits: headings, paragraphs, lists,
    tables, links, images, bold/italic, code, blockquote.
    """

    def __init__(self, link_map=None):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.link_map = link_map or {}
        self._href = None
        self._pending = []
        self._list_stack = []
        self._in_pre = False
        self._table_cells = None
        self._table_rows = []

    # -- helpers ---------------------------------------------------------
    def _emit(self, text):
        self.parts.append(text)

    def _newline(self, count=1):
        tail = "".join(self.parts[-3:])
        if tail.endswith("\n" * count):
            return
        stripped = "".join(self.parts).rstrip("\n")
        self.parts.append("\n" * count if stripped else "")

    def _img_placeholder(self, attrs):
        alt = ""
        src = ""
        for key, value in attrs:
            if key == "alt" and value:
                alt = value
            elif key == "src" and value:
                src = value
        local = self.link_map.get(src)
        if local:
            return f"![{alt or 'image'}]({local})"
        label = alt or _basename(src) or "image"
        return f"![{label}]({src})" if src else f"[image: {label}]"

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline(2)
            self._emit("#" * int(tag[1]) + " ")
        elif tag == "p":
            self._newline(2)
        elif tag == "br":
            self._emit("  \n")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag in ("del", "s", "strike"):
            self._emit("~~")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag == "pre":
            self._newline(2)
            self._emit("```\n")
            self._in_pre = True
        elif tag == "blockquote":
            self._newline(2)
            self._emit("> ")
        elif tag in ("ul", "ol"):
            self._newline(1)
            self._list_stack.append(tag)
        elif tag == "li":
            self._newline(1)
            depth = max(0, len(self._list_stack) - 1)
            bullet = "1." if (self._list_stack and self._list_stack[-1] == "ol") else "-"
            self._emit("  " * depth + bullet + " ")
        elif tag == "hr":
            self._newline(2)
            self._emit("---")
            self._newline(2)
        elif tag == "a":
            href = html.unescape(a.get("href", "") or "")
            self._href = href
            self._pending = []
        elif tag == "img":
            self._emit(self._img_placeholder(attrs))
            self._newline(1)
        elif tag == "tr":
            self._newline(1)
            self._table_cells = []
        elif tag in ("td", "th"):
            self._pending = []
        elif tag == "table":
            self._newline(2)
            self._table_rows = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div"):
            self._newline(2)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag in ("del", "s", "strike"):
            self._emit("~~")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag == "pre":
            self._emit("\n```")
            self._in_pre = False
            self._newline(2)
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._newline(2)
        elif tag == "li":
            self._newline(1)
        elif tag == "a":
            label = "".join(self._pending).strip() or self._href
            href = self.link_map.get(self._href, self._href)
            if href:
                self._emit(f"[{label}]({href})")
            else:
                self._emit(label)
            self._href = None
            self._pending = []
        elif tag == "blockquote":
            self._newline(2)
        elif tag in ("td", "th"):
            cell = " ".join("".join(self._pending).split())
            if self._table_cells is not None:
                self._table_cells.append(cell)
            self._pending = []
        elif tag == "tr":
            if self._table_cells:
                self._table_rows.append(self._table_cells)
                self._emit("| " + " | ".join(self._table_cells) + " |")
                if len(self._table_rows) == 1:
                    self._emit("| " + " | ".join(["---"] * len(self._table_cells)) + " |")
                self._newline(1)
            self._table_cells = None
        elif tag == "table":
            self._newline(2)

    def handle_data(self, data):
        if not data:
            return
        if self._href is not None or self._table_cells is not None:
            self._pending.append(data)
            return
        if self._in_pre:
            self._emit(data)
            return
        # Collapse whitespace the way a browser would.
        self._emit(re.sub(r"\s+", " ", data))

    def result(self):
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = _MULTI_NL.sub("\n\n", text)
        return text.strip()


def to_markdown(body, link_map=None):
    """Convert BBML to Markdown. `link_map` maps original URL -> local path."""
    if not body:
        return ""
    cleaned = _SCRIPT_STYLE.sub(" ", body)
    cleaned = _BBML_COMMENT.sub("", cleaned)
    parser = _Markdown(link_map=link_map)
    parser.feed(cleaned)
    parser.close()
    return parser.result()


def _basename(url):
    if not url:
        return ""
    try:
        return urlparse(url).path.rsplit("/", 1)[-1]
    except ValueError:
        return ""
