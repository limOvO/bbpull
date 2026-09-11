"""Browse model: the course outline as a navigable, cacheable tree.

The CLI pulls a whole course in one pass. A file manager cannot work that way:
the user needs to *see* the structure first, navigate it, and only then choose
what to download. So discovery is separated from materialisation here.

`Catalog.build` uses the proven `CoursePuller.fetch_tree` walk (metadata only -
it never writes a file), then converts the in-memory `Node` tree into plain,
JSON-serialisable dicts the GUI can navigate directly. Announcements are folded
in as a synthetic top-level folder so the UI has exactly one tree to browse.

Pure conversion + planning logic, no I/O beyond the optional cache file, so all
of it is testable offline.
"""

import json
import os
import time
from collections import OrderedDict

from .course import Node
from .logging_util import wrap_logger
from .paths import long_path, sanitize

CATALOG_VERSION = 1

# Node kinds the UI renders differently.
KIND_FOLDER = "folder"
KIND_DOCUMENT = "document"
KIND_FILE = "file"
KIND_LINK = "link"
KIND_ASSESSMENT = "assessment"
KIND_TOOL = "tool"
KIND_ANNOUNCEMENT = "announcement"
KIND_OTHER = "other"

# Synthetic container id for the announcements branch.
ANNOUNCEMENTS_ID = "__announcements__"

_HANDLER_KINDS = {
    "resource/x-bb-folder": KIND_FOLDER,
    "resource/x-bb-lesson": KIND_FOLDER,
    "resource/x-bb-learning-module": KIND_FOLDER,
    "resource/x-bb-module": KIND_FOLDER,
    "resource/x-bb-blankpage": KIND_FOLDER,
    "resource/x-bb-folder-file": KIND_FOLDER,
    "resource/x-bb-document": KIND_DOCUMENT,
    "resource/x-bb-file": KIND_FILE,
    "resource/x-bb-externallink": KIND_LINK,
    "resource/x-bb-courselink": KIND_LINK,
    "resource/x-bb-asmt-test-link": KIND_ASSESSMENT,
    "resource/x-bb-asmt-assignment": KIND_ASSESSMENT,
    "resource/x-bb-asmt-survey-link": KIND_ASSESSMENT,
    "resource/x-bb-blti-link": KIND_TOOL,
    "resource/x-bb-blti": KIND_TOOL,
    "resource/x-bb-scorm": KIND_TOOL,
}

# Kinds that represent something the user can actually open offline.
DOWNLOADABLE_KINDS = {KIND_DOCUMENT, KIND_FILE, KIND_ANNOUNCEMENT}

KIND_LABELS = {
    KIND_FOLDER: "資料夾",
    KIND_DOCUMENT: "文件",
    KIND_FILE: "檔案",
    KIND_LINK: "外部連結",
    KIND_ASSESSMENT: "測驗/作業",
    KIND_TOOL: "互動工具",
    KIND_ANNOUNCEMENT: "公告",
    KIND_OTHER: "其他",
}


def kind_for_handler(handler):
    if not handler:
        return KIND_OTHER
    if handler in _HANDLER_KINDS:
        return _HANDLER_KINDS[handler]
    # Unknown handlers: fall back to pattern matching so a new Blackboard
    # content type still lands in a sane bucket instead of "other".
    lowered = handler.lower()
    for needle, kind in (
        ("folder", KIND_FOLDER),
        ("lesson", KIND_FOLDER),
        ("module", KIND_FOLDER),
        ("asmt", KIND_ASSESSMENT),
        ("test", KIND_ASSESSMENT),
        ("assign", KIND_ASSESSMENT),
        ("document", KIND_DOCUMENT),
        ("file", KIND_FILE),
        ("link", KIND_LINK),
        ("blti", KIND_TOOL),
        ("scorm", KIND_TOOL),
    ):
        if needle in lowered:
            return kind
    return KIND_OTHER


# Raw content-item fields the writers need to reproduce a full pull exactly:
# `_frontmatter` reads created/modified/availability, `_describe_without_body`
# reads links, `file_name_hint` reads contentHandler.file, and `_render_body`
# needs the BBML body. Dropping any of these makes a selective download produce
# different output than a full pull (empty documents, leaf folders).
_PAYLOAD_KEYS = (
    "id",
    "parentId",
    "title",
    "body",
    "description",
    "position",
    "hasChildren",
    "contentHandler",
    "availability",
    "created",
    "modified",
    "links",
)


def payload_for_cache(raw):
    """Trim a content-item payload to the fields a download needs.

    Bodies are kept: re-fetching them per selected item would cost one request
    each, and the walk already has them. This makes the catalog cache somewhat
    larger but lets selective download run straight from the cache.
    """
    if not raw:
        return {}
    return {key: raw[key] for key in _PAYLOAD_KEYS if key in raw}


def convert_node(node, depth=0):
    """Convert a `Node` into a JSON-safe dict with precomputed counts.

    `itemCount` is the number of non-folder items at or below this node, so the
    UI can show "此資料夾含 12 個項目" and preview what a selection would fetch
    without walking the tree in JavaScript.
    """
    children = [convert_node(child, depth + 1) for child in node.children]
    kind = kind_for_handler(node.handler)
    if node.children and kind == KIND_OTHER:
        kind = KIND_FOLDER
    item_count = sum(child["itemCount"] for child in children) if children else 0
    if kind != KIND_FOLDER:
        item_count = max(item_count, 1)
    return OrderedDict(
        [
            ("id", node.id),
            ("title", node.title),
            ("kind", kind),
            ("handler", node.handler),
            ("depth", depth),
            ("hasBody", bool(node.body)),
            ("hasChildren", bool(children)),
            ("childCount", len(children)),
            ("itemCount", item_count),
            ("downloadable", kind in DOWNLOADABLE_KINDS),
            ("payload", payload_for_cache(node.raw)),
            ("children", children),
        ]
    )


def announcement_node(announcements):
    """Wrap announcements as one synthetic folder so the UI browses one tree."""
    children = []
    for ann in announcements:
        duration = (ann.get("availability") or {}).get("duration") or {}
        children.append(
            OrderedDict(
                [
                    ("id", ann.get("id") or ""),
                    ("title", (ann.get("title") or "").strip() or "(未命名公告)"),
                    ("kind", KIND_ANNOUNCEMENT),
                    ("handler", "announcement"),
                    ("depth", 1),
                    ("hasBody", bool(ann.get("body"))),
                    ("hasChildren", False),
                    ("childCount", 0),
                    ("itemCount", 1),
                    ("downloadable", True),
                    ("created", ann.get("created")),
                    ("modified", ann.get("modified")),
                    ("durationType", duration.get("type")),
                    ("children", []),
                ]
            )
        )
    children.sort(key=lambda item: item.get("created") or "", reverse=True)
    return OrderedDict(
        [
            ("id", ANNOUNCEMENTS_ID),
            ("title", "Announcements 公告"),
            ("kind", KIND_FOLDER),
            ("handler", "synthetic/announcements"),
            ("depth", 0),
            ("hasBody", False),
            ("hasChildren", bool(children)),
            ("childCount", len(children)),
            ("itemCount", len(children)),
            ("downloadable", False),
            ("synthetic", True),
            ("children", children),
        ]
    )


def walk(items):
    """Yield every node in the tree, depth first."""
    for item in items or []:
        yield item
        yield from walk(item.get("children"))


def index_by_id(items):
    return {item["id"]: item for item in walk(items)}


def client_tree(items):
    """Tree without `payload`, for sending to the browser.

    The raw BBML bodies are what make selective download work offline, but the
    UI never needs them; shipping them would bloat every poll for no benefit.
    """
    out = []
    for item in items or []:
        slim = {key: value for key, value in item.items() if key != "payload"}
        slim["children"] = client_tree(item.get("children"))
        out.append(slim)
    return out


def build_ancestor_map(items, trail=()):
    """Map node id -> tuple of ancestor ids (root first), excluding itself."""
    mapping = {}
    for item in items or []:
        mapping[item["id"]] = trail
        mapping.update(build_ancestor_map(item.get("children"), trail + (item["id"],)))
    return mapping


def topmost_selection(selected_ids, items):
    """Reduce a selection to its top-most nodes.

    If a user ticks a folder *and* something inside it, only the folder is
    downloaded - otherwise the inner item would be fetched twice into two
    different places.
    """
    selected = set(selected_ids or ())
    if not selected:
        return set()
    ancestors = build_ancestor_map(items)
    result = set()
    for node_id in selected:
        if any(parent in selected for parent in ancestors.get(node_id, ())):
            continue
        result.add(node_id)
    return result


def count_items(selected_ids, items):
    """Total non-folder items a selection would produce."""
    index = index_by_id(items)
    total = 0
    for node_id in topmost_selection(selected_ids, items):
        node = index.get(node_id)
        if node:
            total += node.get("itemCount") or 0
    return total


class Catalog:
    """A discovered course outline plus the planning helpers the UI needs."""

    def __init__(self, course_id, course_name, base_url, tree, stats=None, built_at=None):
        self.course_id = course_id
        self.course_name = course_name
        self.base_url = base_url
        self.tree = tree
        self.stats = stats or {}
        self.built_at = built_at or time.time()

    # ------------------------------------------------------------- building
    @classmethod
    def build(cls, session, course_id, course_name="", log=None, announce=None,
              progress=None):
        """Walk the course outline. Reads only - never writes a file.

        `progress(done, total)` is forwarded to the walker so a caller can show a
        determinate indicator instead of an endless spinner.
        """
        from .course import CoursePuller

        log = wrap_logger(log)
        puller = CoursePuller(session, course_id, "out", log, download_files=False,
                              progress=progress)
        puller.fetch_tree()

        tree = [convert_node(node) for node in puller.roots]
        stats = OrderedDict(
            [
                ("items", puller.stats["nodes"]),
                ("folders", puller.stats["folders"]),
                ("documents", puller.stats["documents"]),
            ]
        )

        announcements = []
        if announce is not None:
            announcements = announce
        if announcements:
            tree.insert(0, announcement_node(announcements))
        stats["announcements"] = len(announcements)
        stats["downloadable"] = sum(1 for n in walk(tree) if n.get("downloadable"))

        return cls(
            course_id=course_id,
            course_name=course_name or course_id,
            base_url=getattr(session, "base", ""),
            tree=tree,
            stats=stats,
        )

    @classmethod
    def from_announcements(cls, course_id, course_name, base_url, announcements):
        """Catalog containing only the announcements branch (fast preview)."""
        tree = [announcement_node(announcements)] if announcements else []
        return cls(
            course_id=course_id,
            course_name=course_name,
            base_url=base_url,
            tree=tree,
            stats={"items": len(announcements), "announcements": len(announcements),
                   "downloadable": len(announcements)},
        )

    # ------------------------------------------------------------ accessors
    def to_dict(self):
        return OrderedDict(
            [
                ("version", CATALOG_VERSION),
                ("courseId", self.course_id),
                ("courseName", self.course_name),
                ("baseUrl", self.base_url),
                ("builtAt", self.built_at),
                ("stats", self.stats),
                ("tree", self.tree),
            ]
        )

    @classmethod
    def from_dict(cls, payload):
        return cls(
            course_id=payload.get("courseId", ""),
            course_name=payload.get("courseName", ""),
            base_url=payload.get("baseUrl", ""),
            tree=payload.get("tree") or [],
            stats=payload.get("stats") or {},
            built_at=payload.get("builtAt") or 0,
        )

    def index(self):
        return index_by_id(self.tree)

    def find(self, node_id):
        return self.index().get(node_id)

    def breadcrumb(self, node_id):
        """Ancestor chain (root first) ending with `node_id`, for the UI trail."""
        ancestors = build_ancestor_map(self.tree)
        index = self.index()
        chain = [index[i] for i in ancestors.get(node_id, ()) if i in index]
        node = index.get(node_id)
        if node:
            chain.append(node)
        return [
            OrderedDict([("id", n["id"]), ("title", n["title"]), ("kind", n["kind"])])
            for n in chain
        ]

    def children_of(self, node_id):
        if node_id in (None, "", "root"):
            return list(self.tree)
        node = self.find(node_id)
        return list(node.get("children") or []) if node else []

    def count_items(self, selected_ids):
        return count_items(selected_ids, self.tree)

    def plan(self, selected_ids):
        """Top-most selected ids (folders cover their own subtree)."""
        return topmost_selection(selected_ids, self.tree)

    def announce_ids(self, selected_ids):
        """Announcement ids covered by a selection (expands the synthetic folder)."""
        out = set()
        for node_id in self.plan(selected_ids):
            if node_id == ANNOUNCEMENTS_ID:
                folder = self.find(ANNOUNCEMENTS_ID) or {}
                out.update(
                    child["id"] for child in folder.get("children") or [] if child.get("id")
                )
            elif (self.find(node_id) or {}).get("kind") == KIND_ANNOUNCEMENT:
                out.add(node_id)
        return out

    def content_ids(self, selected_ids):
        """Selected ids excluding announcements, with the synthetic folder dropped."""
        out = set()
        for node_id in self.plan(selected_ids):
            if node_id == ANNOUNCEMENTS_ID:
                continue
            node = self.find(node_id) or {}
            if node.get("kind") == KIND_ANNOUNCEMENT:
                continue
            out.add(node_id)
        return out

    # ---------------------------------------------------------------- cache
    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(long_path(path), "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=1, ensure_ascii=False)
        return path

    @classmethod
    def load(cls, path):
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(long_path(path), "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None
        if payload.get("version") != CATALOG_VERSION:
            return None
        return cls.from_dict(payload)


def catalog_cache_path(state_dir, course_id):
    name = f"catalog_{sanitize(course_id, fallback='course', max_len=40)}.json"
    return os.path.join(state_dir, name)
