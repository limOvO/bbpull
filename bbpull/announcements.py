"""Course announcements.

  GET /learn/api/public/v1/courses/{courseId}/announcements

Announcement bodies are BBML, exactly like Ultra document bodies, so they get
the same treatment: Markdown rendering plus download of embedded attachments
and inline images.
"""

import json
import os
from collections import OrderedDict
from datetime import datetime

from . import bbml
from .logging_util import wrap_logger
from .paths import filename_from_url, long_path, readable_name, sanitize


def _parse_iso(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    for candidate in (text, text.split(".")[0] + "+00:00"):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _stamp(announcement):
    """Best available date for ordering/naming: created, else modified, else start."""
    for key in ("created", "modified"):
        parsed = _parse_iso(announcement.get(key))
        if parsed:
            return parsed
    duration = (announcement.get("availability") or {}).get("duration") or {}
    return _parse_iso(duration.get("start"))


class AnnouncementPuller:
    def __init__(self, client, course_id, out_dir, logger, download_files=True, overwrite=False):
        self.client = client
        self.course_id = course_id
        self.out_dir = out_dir
        # A course title with an unencodable character must never abort the pull.
        self.log = wrap_logger(logger)
        self.download_files = download_files
        self.overwrite = overwrite
        self.items = []
        self.stats = {"announcements": 0, "files_saved": 0, "files_failed": 0, "bytes": 0}

    def fetch(self):
        self.log(f"[announcements] reading for {self.course_id}")
        raw = self.client.get_all(f"/courses/{self.course_id}/announcements")
        self.items = sorted(raw, key=lambda a: (_stamp(a) or datetime.min, a.get("id") or ""))
        self.stats["announcements"] = len(self.items)
        return self.items

    def write(self, only_ids=None):
        """Write announcements. `only_ids` restricts output to those ids.

        Selective download uses this so picking three announcements does not
        also materialise the other twenty.
        """
        os.makedirs(long_path(self.out_dir), exist_ok=True)
        assets_dir = os.path.join(self.out_dir, "attachments")
        rows = []
        wanted = set(only_ids) if only_ids is not None else None
        selected = [
            ann for ann in self.items if wanted is None or ann.get("id") in wanted
        ]
        for i, ann in enumerate(selected, start=1):
            stamp = _stamp(ann)
            date_part = stamp.strftime("%Y-%m-%d") if stamp else "undated"
            title = (ann.get("title") or "").strip() or "announcement"
            base = f"{date_part}_{i:03d}_{sanitize(title, fallback=ann.get('id', 'announcement'))}"
            link_map = {}
            saved = []
            if ann.get("body"):
                for link in bbml.extract_file_links(ann["body"]):
                    url = link["url"]
                    name_hint = readable_name(link.get("name")) or filename_from_url(
                        url, fallback=f"{base}_file"
                    )
                    name = sanitize(name_hint, fallback=f"{base}_file")
                    dest = os.path.join(assets_dir, name)
                    status, size, info = self._download(url, dest)
                    if status in ("saved", "skipped"):
                        # Files live in ./attachments; the .md sits one level up.
                        link_map[url] = f"attachments/{name}"
                        saved.append({"path": f"attachments/{name}", "bytes": size})
                    else:
                        link_map[url] = url
                        self.log(f"[warn] announcement attachment failed: {name}: {info}")
            markdown = self._render(ann, stamp, body_links=link_map)
            md_name = f"{base}.md"
            self._write_text(os.path.join(self.out_dir, md_name), markdown)
            self._write_json(
                os.path.join(self.out_dir, f"{base}.json"),
                OrderedDict(
                    [
                        ("courseId", self.course_id),
                        ("baseUrl", self.client.base),
                        ("announcement", ann),
                        ("plainText", bbml.to_text(ann.get("body") or "")),
                    ]
                ),
            )
            rows.append(
                {
                    "date": stamp.isoformat() if stamp else None,
                    "title": title,
                    "id": ann.get("id"),
                    "markdown": md_name,
                    "attachments": saved,
                }
            )
        self._write_index(rows)
        return rows

    def _render(self, ann, stamp, body_links):
        lines = [f"# {ann.get('title') or '(untitled announcement)'}", ""]
        meta = []
        if stamp:
            meta.append(f"- posted: {stamp.isoformat()}")
        if ann.get("modified"):
            meta.append(f"- modified: {ann['modified']}")
        if ann.get("id"):
            meta.append(f"- id: `{ann['id']}`")
        duration = (ann.get("availability") or {}).get("duration") or {}
        if duration.get("type"):
            meta.append(f"- availability: {duration.get('type')}")
        if duration.get("start") or duration.get("end"):
            meta.append(f"- window: {duration.get('start') or '...'} -> {duration.get('end') or '...'}")
        lines.extend(meta)
        lines.append("")
        body_md = bbml.to_markdown(ann.get("body") or "", link_map=body_links)
        if body_md:
            lines.extend([body_md, ""])
        else:
            lines.extend(["_This announcement has no body text._", ""])
        return "\n".join(lines).rstrip() + "\n"

    def _download(self, url, dest):
        if not self.download_files:
            return "skipped", 0, "downloads disabled"
        if os.path.exists(dest) and not self.overwrite:
            return "skipped", os.path.getsize(dest), "already present"
        try:
            status, size, info = self.client.download(url, dest, overwrite=self.overwrite)
        except Exception as exc:
            self.stats["files_failed"] += 1
            return "failed", 0, str(exc)
        if status == "saved":
            self.stats["files_saved"] += 1
            self.stats["bytes"] += size
        elif status == "failed":
            self.stats["files_failed"] += 1
        return status, size, info

    def _write_index(self, rows):
        lines = [f"# Announcements - course `{self.course_id}`", ""]
        lines.append(f"Source: {self.client.base}/ultra/courses/{self.course_id}/announcements")
        lines.append(f"Total: {len(rows)}")
        lines.append("")
        lines.append("| posted | title | local file | attachments |")
        lines.append("| --- | --- | --- | --- |")
        for row in reversed(rows):  # newest first
            atts = ", ".join(a["path"] for a in row["attachments"]) or "-"
            lines.append(
                f"| {row['date'] or 'undated'} | {row['title']} | "
                f"[{row['markdown']}]({row['markdown']}) | {atts} |"
            )
        lines.append("")
        self._write_text(os.path.join(self.out_dir, "announcements.md"), "\n".join(lines))
        self._write_json(os.path.join(self.out_dir, "announcements.json"), {"results": rows})

    def _write_text(self, path, text):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(long_path(path), "w", encoding="utf-8") as handle:
            handle.write(text)

    def _write_json(self, path, payload):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(long_path(path), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
