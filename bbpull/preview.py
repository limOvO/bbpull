"""Pure preview helpers shared by the desktop front-ends.

Kept free of Qt/Tk imports so the text handling can be tested anywhere: what a
document says, which metadata matters, and how a folder/file/link differs from
one another are not GUI concerns.

Everything works from the catalog cache - each item's BBML body is already in
`payload.body` - so previewing never downloads anything. Images are replaced
with a text placeholder instead of being fetched, because a preview must not
silently start pulling media.
"""

import os
import re
import tempfile

from . import bbml
from .catalog import KIND_FOLDER, KIND_LABELS
from .paths import filename_from_url, readable_name, sanitize

#: A document body is text; this cap only protects against a pathological item.
MAX_PREVIEW_CHARS = 400_000

#: `![alt](url)` - shown as text; loading the media is not a preview.
_IMAGE_MD = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


def strip_images(markdown):
    """Replace inline images with a placeholder so nothing is fetched."""
    def replace(match):
        alt = (match.group(1) or "").strip()
        return f"（圖片：{alt}）" if alt else "（圖片）"

    return _IMAGE_MD.sub(replace, markdown or "")


def item_attachments(node):
    """Embedded PDF/Excel/DOCX files carried in the item's body.

    Ultra stores these as `bbcswebdav` links inside the document body rather
    than as child content items, so they never appeared in the list and the row
    looked empty. Returning them lets the preview name what is actually there.
    """
    payload = node.get("payload") or {}
    body = payload.get("body") or node.get("body") or ""
    out = []
    seen = set()
    for link in bbml.extract_file_links(body):
        url = link.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        name = (link.get("name")
                or readable_name(url.rsplit("/", 1)[-1].split("?")[0])
                or filename_from_url(url))
        out.append({"url": url, "name": name})
    return out


_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                   ".svg", ".tif", ".tiff")


def is_image_name(name):
    """True when an attachment can be shown inline instead of in an external app."""
    return str(name or "").lower().endswith(_IMAGE_SUFFIXES)


def preview_cache_path(item_id, name):
    """Where an on-demand attachment preview is cached.

    A per-item folder keeps two attachments with the same base name from
    overwriting each other, and the system temp root means nothing accumulates
    in the user's home. Re-opening the same attachment reuses the file.
    """
    base = os.path.join(tempfile.gettempdir(), "bbpull-preview")
    folder = sanitize(item_id or "item", fallback="item", max_len=48)
    return os.path.join(base, folder,
                        sanitize(name, fallback="attachment", max_len=120))


def attachment_row(parent, attachment, index=0):
    """A virtual content-list row for one file embedded in `parent`'s body.

    These are not content items in the Blackboard tree, so they carry a
    synthetic id and enough context to download/preview the file on their own.
    """
    parent_id = parent.get("id") or ""
    name = attachment.get("name") or "attachment"
    return {
        "id": f"__attachment__:{parent_id}:{index}",
        "title": name,
        "kind": "attachment",
        "handler": "attachment",
        "depth": int(parent.get("depth") or 0) + 1,
        "hasBody": False,
        "hasChildren": False,
        "childCount": 0,
        "itemCount": 1,
        "attachmentCount": 0,
        "downloadable": True,
        "virtual": True,
        "parentContentId": parent_id,
        "parentTitle": parent.get("title") or "",
        "attachmentUrl": attachment.get("url") or "",
        "attachmentName": name,
        "payload": {},
        "children": [],
    }


def format_stamp(value):
    """`2026-08-28T08:34:34.214Z` -> `2026-08-28 08:34` (empty when unusable)."""
    text = str(value or "")
    if not text:
        return ""
    if "T" in text:
        date, _, rest = text.partition("T")
        return f"{date} {rest[:5]}".strip()
    return text[:16]


def item_body_markdown(node):
    """Rendered Markdown body from the cached payload, or "" when there is none."""
    payload = node.get("payload") or {}
    body = payload.get("body") or node.get("body") or ""
    if not body:
        return ""
    try:
        markdown = bbml.to_markdown(body)
    except Exception:  # noqa: BLE001 - a preview must never break on bad input
        markdown = bbml.to_text(body)
    return strip_images(markdown or "")[:MAX_PREVIEW_CHARS]


def item_metadata(node, path_text=""):
    """Ordered (label, value) pairs for the preview's metadata block."""
    payload = node.get("payload") or {}
    handler = payload.get("contentHandler") or {}
    file_info = handler.get("file") or {}
    rows = []
    if path_text:
        rows.append(("位置", path_text))
    rows.append(("類型", KIND_LABELS.get(node.get("kind"),
                                         node.get("kind") or "項目")))
    if node.get("kind") == KIND_FOLDER:
        rows.append(("內含", f"{node.get('itemCount', 0)} 個項目"))
    if file_info.get("fileName"):
        rows.append(("檔名", file_info["fileName"]))
    if file_info.get("mimeType"):
        rows.append(("檔案類型", file_info["mimeType"]))
    if handler.get("url"):
        rows.append(("網址", str(handler["url"])))
    rows.append(("內容 id", node.get("id") or "—"))
    for key, label in (("created", "建立"), ("modified", "修改")):
        stamp = format_stamp(payload.get(key))
        if stamp:
            rows.append((label, stamp))
    return rows


def placeholder_text(node):
    """What to show when an item has no renderable body."""
    kind = node.get("kind")
    if kind == KIND_FOLDER:
        return (f"這個資料夾包含 {node.get('itemCount', 0)} 個項目。\n\n"
                "雙擊資料夾即可進入瀏覽。")
    if kind == "file":
        return ("這是檔案項目，沒有可閱讀的內文。\n\n"
                "按「下載此項目」取得原始檔案。")
    if kind == "link":
        return ("這是外部連結，內容在 Blackboard 以外的網站。\n\n"
                "按「在瀏覽器開啟」前往。")
    if kind in ("assessment", "tool"):
        return ("Blackboard 的測驗／作業／互動工具沒有可離線預覽的內文；"
                "下載只會保存項目資訊與連結。")
    if kind == "announcement":
        return "這則公告沒有內文。"
    return "這個項目沒有可預覽的內文。"
