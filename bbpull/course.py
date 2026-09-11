"""Course content tree: discover, render, download.

Traversal contract (verified against the Learn REST swagger and the official
"Managing content with REST API in ULTRA" / "cURL Demo for attachments" docs):

  GET /learn/api/public/v1/courses/{courseId}/contents            -> root items
  GET /learn/api/public/v1/courses/{courseId}/contents/{id}/children -> nested items
  GET /learn/api/public/v1/courses/{courseId}/contents/{id}/attachments
  GET .../contents/{id}/attachments/{attachmentId}/download        -> bytes

Ultra keeps attachments inside the item's `body` as BBML `<a href=".../bbcswebdav/
pid-...-xid-...">` links, so body parsing is a first-class download source.
"""

import json
import os
import threading
from collections import OrderedDict

from . import bbml
from .errors import ApiError
from .logging_util import wrap_logger
from .paths import dedupe, filename_from_url, long_path, readable_name, sanitize

# Content handler ids we understand.
HANDLER_FOLDER = {
    "resource/x-bb-folder",
    "resource/x-bb-lesson",
    "resource/x-bb-blankpage",
}
HANDLER_DOCUMENT = {
    "resource/x-bb-document",
    "resource/x-bb-blti-link",  # rendered page, body may still hold text
}
HANDLER_FILE = {"resource/x-bb-file"}
HANDLER_LINK = {"resource/x-bb-externallink", "resource/x-bb-courselink"}

FOLDER_LIKE = HANDLER_FOLDER | {
    "resource/x-bb-learning-module",
    "resource/x-bb-module",
    "resource/x-bb-folder-file",
}


class Node:
    """One content item in the course outline."""

    __slots__ = (
        "id",
        "parent_id",
        "title",
        "body",
        "description",
        "position",
        "handler",
        "raw",
        "children",
        "rel_dir",
        "attachments",
    )

    def __init__(self, payload):
        self.raw = payload or {}
        self.id = self.raw.get("id") or ""
        self.parent_id = self.raw.get("parentId")
        self.title = (self.raw.get("title") or "").strip() or self.id or "untitled"
        self.body = self.raw.get("body") or ""
        self.description = self.raw.get("description") or ""
        self.position = self.raw.get("position")
        if self.position is None:
            self.position = 0
        handler = self.raw.get("contentHandler") or {}
        self.handler = (handler.get("id") or "").strip()
        self.children = []
        self.rel_dir = ""
        self.attachments = []

    @property
    def has_children(self):
        return bool(self.raw.get("hasChildren"))

    @property
    def is_folder(self):
        return self.handler in FOLDER_LIKE or (self.has_children and not self.handler)

    @property
    def is_file(self):
        return self.handler in HANDLER_FILE

    @property
    def is_document(self):
        return self.handler in HANDLER_DOCUMENT

    @property
    def file_name_hint(self):
        handler = self.raw.get("contentHandler") or {}
        for key in ("file", "resource"):
            blob = handler.get(key)
            if isinstance(blob, dict):
                name = blob.get("fileName") or blob.get("name")
                if name:
                    return str(name)
        return ""

    def to_summary(self):
        return OrderedDict(
            [
                ("id", self.id),
                ("parentId", self.parent_id),
                ("title", self.title),
                ("handler", self.handler),
                ("position", self.position),
                ("hasChildren", self.has_children),
                ("dir", self.rel_dir),
                ("attachments", self.attachments),
            ]
        )


class CoursePuller:
    def __init__(self, client, course_id, out_dir, logger, download_files=True,
                 overwrite=False, progress=None):
        self.client = client
        self.course_id = course_id
        self.out_dir = out_dir
        # A course title with an unencodable character must never abort the pull.
        self.log = wrap_logger(logger)
        self.download_files = download_files
        self.overwrite = overwrite
        #: Optional `progress(done, total)` for a determinate indicator. The total
        #: is the number of folders discovered so far, which grows as the walk
        #: finds more - so the fraction is a real measure that converges to 1.
        self.progress = progress
        self.folders_seen = 0
        self.folders_done = 0
        self.roots = []
        self.problems = []
        self.stats = {
            "nodes": 0,
            "folders": 0,
            "documents": 0,
            "files_saved": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "bytes": 0,
        }
        self._lock = threading.Lock()
        self._used_names = {}

    # ------------------------------------------------------------ discovery
    def fetch_tree(self):
        self.log(f"[content] reading outline for {self.course_id}")
        raw_roots = self.client.get_all(f"/courses/{self.course_id}/contents")
        self.roots = [Node(item) for item in raw_roots]
        self._sort(self.roots)
        self.folders_seen = 0
        self.folders_done = 0
        self._emit_progress()
        self._walk(self.roots, depth=0)
        self._emit_progress()
        return self.roots

    def _emit_progress(self):
        if self.progress:
            try:
                self.progress(self.folders_done, max(1, self.folders_seen))
            except Exception:  # noqa: BLE001 - progress must never break a pull
                pass

    def _sort(self, nodes):
        nodes.sort(key=lambda n: (n.position if isinstance(n.position, int) else 0, n.title))

    def _walk(self, nodes, depth):
        for node in nodes:
            self.stats["nodes"] += 1
            if node.is_folder:
                self.stats["folders"] += 1
            elif node.is_document:
                self.stats["documents"] += 1
            if node.has_children or node.is_folder:
                pad = "  " * depth
                self.log(f"{pad}- {node.title}  [{node.handler or 'unknown'}]")
                # Counted before the request so the denominator reflects work
                # that is definitely queued, not only what has finished.
                self.folders_seen += 1
                self._emit_progress()
                try:
                    raw_children = self.client.get_all(
                        f"/courses/{self.course_id}/contents/{node.id}/children"
                    )
                except ApiError as exc:
                    self.problems.append(
                        {"where": f"children of {node.id} ({node.title})", "error": str(exc)}
                    )
                    self.log(f"{pad}  ! could not read children: {exc}")
                    self.folders_done += 1
                    self._emit_progress()
                    continue
                node.children = [Node(item) for item in raw_children]
                self._sort(node.children)
                self.folders_done += 1
                self._emit_progress()
                self._walk(node.children, depth + 1)

    # ------------------------------------------------------------- materialise
    def write(self):
        os.makedirs(long_path(self.out_dir), exist_ok=True)
        index = []
        for node in self.roots:
            index.append(self._write_node(node, parent_dir=self.out_dir))
        return index

    def _unique(self, directory, name):
        key = (os.path.normcase(os.path.abspath(directory)),)
        with self._lock:
            used = self._used_names.setdefault(key, set())
            final = dedupe(used, name)
            used.add(final)
            return final

    def claim_name(self, directory, name):
        """Reserve `name` in `directory` so a later write cannot reuse it.

        Used by selective downloads: ancestor directories are created by the
        caller (to reach a deeply nested selection), and the puller must know
        those names are taken or a sibling file could collide with one.
        """
        key = (os.path.normcase(os.path.abspath(directory)),)
        with self._lock:
            self._used_names.setdefault(key, set()).add(name)

    def _write_node(self, node, parent_dir):
        if node.is_file:
            return self._write_file_node(node, parent_dir)
        if node.children:
            return self._write_folder_node(node, parent_dir)
        return self._write_leaf_node(node, parent_dir)

    def _write_folder_node(self, node, parent_dir):
        dir_name = self._unique(parent_dir, sanitize(node.title, fallback=node.id))
        rel_dir = os.path.join(parent_dir, dir_name)
        node.rel_dir = os.path.relpath(rel_dir, self.out_dir).replace(os.sep, "/")
        os.makedirs(long_path(rel_dir), exist_ok=True)
        self._write_sidecar(node, rel_dir, index_name="index.json")

        children = []
        for i, child in enumerate(node.children, start=1):
            child_dir = os.path.join(rel_dir, f"{i:02d}_{sanitize(child.title, fallback=child.id)}")
            children.append(self._write_child(child, rel_dir, child_dir, i))
        return node.to_summary() | {"children": children}

    def _write_child(self, child, parent_dir, planned_dir, ordinal):
        """Write a child, using `planned_dir` only when the child is a container."""
        if child.is_file:
            return self._write_file_node(child, parent_dir, ordinal=ordinal)
        if child.children:
            # Container: take the ordinal-prefixed directory we planned above.
            child.rel_dir = os.path.relpath(planned_dir, self.out_dir).replace(os.sep, "/")
            os.makedirs(long_path(planned_dir), exist_ok=True)
            self._write_sidecar(child, planned_dir, index_name="index.json")
            with self._lock:
                used = self._used_names.setdefault(
                    (os.path.normcase(os.path.abspath(parent_dir)),), set()
                )
                used.add(os.path.basename(planned_dir))
            grand = []
            for j, gc in enumerate(child.children, start=1):
                grand.append(
                    self._write_child(
                        gc,
                        planned_dir,
                        os.path.join(planned_dir, f"{j:02d}_{sanitize(gc.title, fallback=gc.id)}"),
                        j,
                    )
                )
            return child.to_summary() | {"children": grand}
        return self._write_leaf_node(child, parent_dir, ordinal=ordinal)

    def _write_leaf_node(self, node, parent_dir, ordinal=None):
        """A leaf that is not a file: a document page, a link, or an empty folder."""
        base = sanitize(node.title, fallback=node.id)
        prefix = f"{ordinal:02d}_" if ordinal else ""
        saved = []
        if node.body:
            markdown, saved = self._render_body(node, parent_dir, base, prefix)
        else:
            markdown = self._frontmatter(node) + self._describe_without_body(node)
        name = self._unique(parent_dir, f"{prefix}{base}.md")
        path = os.path.join(parent_dir, name)
        self._write_text(path, markdown)
        node.rel_dir = os.path.relpath(path, self.out_dir).replace(os.sep, "/")
        self._write_sidecar(node, parent_dir, index_name=f"{prefix}{base}.json")
        # Empty folder with no body: still create a directory so the outline shape survives.
        if node.is_folder and not node.body:
            dir_name = self._unique(parent_dir, f"{prefix}{base}")
            os.makedirs(long_path(os.path.join(parent_dir, dir_name)), exist_ok=True)
        return node.to_summary() | {"files": saved}

    def _write_file_node(self, node, parent_dir, ordinal=None):
        prefix = f"{ordinal:02d}_" if ordinal else ""
        base = sanitize(node.title, fallback=node.id)
        # The attachments sub-resource is authoritative for x-bb-file items; its
        # `fileName` carries the real name (node title is only the display name).
        results = self._download_content_attachments(node, parent_dir, prefix)
        self._write_sidecar(node, parent_dir, index_name=f"{prefix}{base}.json")
        return node.to_summary() | {"files": results}

    # --------------------------------------------------------------- bodies
    def _frontmatter(self, node):
        lines = [
            f"# {node.title}",
            "",
        ]
        meta = []
        if node.handler:
            meta.append(f"- type: `{node.handler}`")
        meta.append(f"- content id: `{node.id}`")
        if node.raw.get("created"):
            meta.append(f"- created: {node.raw['created']}")
        if node.raw.get("modified"):
            meta.append(f"- modified: {node.raw['modified']}")
        availability = (node.raw.get("availability") or {}).get("available")
        if availability:
            meta.append(f"- available: {availability}")
        if meta:
            lines.extend(meta)
            lines.append("")
        if node.description:
            lines.extend(["> " + node.description.replace("\n", "\n> "), ""])
        return "\n".join(lines)

    def _describe_without_body(self, node):
        handler = node.raw.get("contentHandler") or {}
        detail = []
        if node.handler in HANDLER_LINK:
            for key in ("url", "href", "target", "absoluteUrl"):
                value = handler.get(key) or (node.raw.get("links") or [{}])[0].get(key)
                if value:
                    detail.append(f"- link: <{value}>")
        if node.handler not in HANDLER_LINK and node.handler not in FOLDER_LIKE:
            detail.append(
                "- note: no body text was returned by the REST API for this item "
                "(locked, adaptive-release gated, or an interactive tool)."
            )
        for link in node.raw.get("links") or []:
            href = link.get("href")
            if href:
                detail.append(f"- UI view: <{self.client.safe_url(href)}>")
        return "\n".join(detail) + ("\n" if detail else "")

    def _render_body(self, node, parent_dir, base, prefix):
        """Convert a content body to Markdown, downloading embedded files."""
        links = bbml.extract_file_links(node.body)
        link_map = {}
        saved = []
        for link in links:
            url = link["url"]
            # data-bbfile's linkName gives the real name; the URL basename is
            # only an opaque xid fallback.
            name_hint = readable_name(link.get("name")) or filename_from_url(
                url, fallback=f"{base}_file"
            )
            name = self._unique(parent_dir, sanitize(name_hint, fallback=f"{base}_file"))
            dest = os.path.join(parent_dir, name)
            status, size, info = self._download_url(url, dest, node.id)
            if status == "saved":
                link_map[url] = name
                saved.append({"url": url, "path": name, "bytes": size})
            elif status == "skipped":
                link_map[url] = name
                saved.append({"url": url, "path": name, "bytes": size, "note": "already present"})
            else:
                link_map[url] = url
                saved.append({"url": url, "error": info})
                self.problems.append({"where": f"body embed of {node.id}", "error": f"{url}: {info}"})
        markdown = self._frontmatter(node) + "\n" + bbml.to_markdown(node.body, link_map=link_map)
        markdown = markdown.rstrip() + "\n"
        return markdown, saved

    def _download_content_attachments(self, node, parent_dir, prefix=""):
        """Try the attachments sub-resource and download everything readable."""
        results = []
        path = f"/courses/{self.course_id}/contents/{node.id}/attachments"
        try:
            payload = self.client.api_get(path, allow_404=True)
        except ApiError as exc:
            self.log(f"[warn] attachments list failed for {node.id}: {exc}")
            return results
        if not payload:
            return results
        for att in payload.get("results", []):
            att_id = att.get("id")
            if not att_id:
                continue
            name_hint = att.get("fileName") or att.get("name") or f"{node.id}_{att_id}"
            name = self._unique(parent_dir, f"{prefix}{sanitize(name_hint, fallback=att_id)}")
            dest = os.path.join(parent_dir, name)
            url = (
                f"{self.client.base}/learn/api/public/v1/courses/{self.course_id}"
                f"/contents/{node.id}/attachments/{att_id}/download"
            )
            status, size, info = self._download_url(url, dest, node.id)
            if status in ("saved", "skipped"):
                results.append({"url": url, "path": name, "bytes": size, "source": "attachments"})
            else:
                results.append({"url": url, "error": info, "source": "attachments"})
            node.attachments = results
        return results

    def _download_url(self, url, dest, content_id):
        if not self.download_files:
            return "skipped", 0, "downloads disabled (--no-files)"
        try:
            status, size, info = self.client.download(url, dest, overwrite=self.overwrite)
        except Exception as exc:  # network/URL policy problems must not kill the run
            with self._lock:
                self.stats["files_failed"] += 1
            return "failed", 0, str(exc)
        with self._lock:
            if status == "saved":
                self.stats["files_saved"] += 1
                self.stats["bytes"] += size
            elif status == "skipped":
                self.stats["files_skipped"] += 1
            else:
                self.stats["files_failed"] += 1
        if status == "failed":
            self.log(f"[warn] download failed {os.path.basename(dest)}: {info}")
        return status, size, info

    # -------------------------------------------------------------- outputs
    def _write_sidecar(self, node, parent_dir, index_name):
        name = self._unique(parent_dir, sanitize(index_name, fallback="index.json", max_len=100))
        path = os.path.join(parent_dir, name)
        payload = OrderedDict(
            [
                ("courseId", self.course_id),
                ("baseUrl", self.client.base),
                ("content", node.raw),
                ("plainText", bbml.to_text(node.body)),
            ]
        )
        self._write_json(path, payload)

    def _write_json(self, path, payload):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(long_path(path), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)

    def _write_text(self, path, text):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(long_path(path), "w", encoding="utf-8") as handle:
            handle.write(text)
