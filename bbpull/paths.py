"""Filesystem-safe path helpers. Windows + POSIX safe, deterministic."""

import hashlib
import os
import re
import unicodedata
from urllib.parse import unquote, urlparse

# Characters illegal on Windows plus control chars.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows reserved device names (case-insensitive, extension-insensitive).
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
_WS = re.compile(r"\s+")

# Conservative budget: keeps full path comfortably under MAX_PATH on Windows.
MAX_SEGMENT = 80


def sanitize(name, fallback="untitled", max_len=MAX_SEGMENT):
    """Turn arbitrary Blackboard text into one safe path segment.

    Unicode is preserved (NFC) so Chinese course titles stay readable; only
    characters that are illegal in a path are replaced. When `fallback` is None
    an empty result returns None instead of the fallback text.
    """
    if name is None:
        name = ""
    name = unicodedata.normalize("NFC", str(name))
    name = _ILLEGAL.sub("_", name)
    name = name.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    name = _WS.sub(" ", name).strip()
    # Windows dislikes trailing dots/spaces.
    name = name.rstrip(". ")
    if not name:
        if fallback is None:
            return None
        name = _ILLEGAL.sub("_", str(fallback)).strip().rstrip(". ") or "untitled"
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        keep = max_len - len(digest) - 2
        # Prefer keeping the extension if there is one.
        root, ext = split_extension(name)
        if ext and len(ext) <= 12:
            root = root[: max(1, keep - len(ext))]
            name = f"{root}~{digest}{ext}"
        else:
            name = f"{name[:keep]}~{digest}"
    return name


def dedupe(existing, candidate):
    """Return `candidate` adjusted so it does not collide with `existing` (a set).

    Mutates nothing; caller adds the result to the set.
    """
    if candidate not in existing:
        return candidate
    root, ext = split_extension(candidate)
    for i in range(2, 10000):
        trial = f"{root} ({i}){ext}"
        if trial not in existing:
            return trial
    return f"{root}~{os.getpid()}{ext}"


def long_path(path):
    """Prefix with the Windows extended-length marker when needed."""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    if len(p) >= 240:
        return "\\\\?\\" + p
    return p


def filename_from_url(url, fallback="download.bin"):
    """Best-effort filename from a URL path (handles bbcswebdav xid links)."""
    try:
        path = urlparse(url).path
    except ValueError:
        return fallback
    base = unquote(path.rsplit("/", 1)[-1]) or ""
    base = base.strip()
    if not base or base in {".", ".."}:
        return fallback
    return sanitize(base, fallback=fallback, max_len=120)


# Extensions we trust a Blackboard-provided display name to carry. This is how
# we tell "notes.pdf" (a real name from data-bbfile) apart from an opaque
# value such as "xid-202_1?t=1", which we must not use verbatim.
_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".tsv",
    ".txt", ".md", ".rtf", ".odt", ".ods", ".odp", ".pages", ".key", ".numbers",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".iso",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".webp",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v",
    ".py", ".ipynb", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".r", ".m", ".sql",
    ".json", ".xml", ".html", ".htm", ".css", ".yaml", ".yml",
    ".sav", ".dta", ".xmind", ".drawio", ".vsdx", ".epub", ".tex",
}


def _plausible_extension(ext):
    """True for `.pdf`, `.tar.gz`, `.cpp` - false for `._1`, `.1`, `.xid202a1b`."""
    if not ext or len(ext) < 2 or len(ext) > 13:
        return False
    if not re.fullmatch(r"\.[A-Za-z][A-Za-z0-9]{0,11}", ext):
        return False
    return ext.lower() in _EXTS


def split_extension(name):
    """Split `name` into (root, ext) using the LAST dot only.

    Deliberately does not use `os.path.splitext`: some Windows Python builds
    treat `_` as an extension separator (measured: `splitext('xid-202_1')`
    returned `('xid-202_1', '')` instead of `('xid-202', '_1')`), which would
    make every naming decision environment-dependent.
    """
    if not name:
        return name or "", ""
    dot = name.rfind(".")
    # A leading dot means a hidden file, not an extension.
    if dot <= 0 or dot == len(name) - 1:
        return name, ""
    return name[:dot], name[dot:]


def _basename(text):
    """Last path component, treating both separators as separators."""
    return re.split(r"[\\/]", text)[-1]


def _looks_like_url(text):
    return "://" in text or text.lower().startswith(("http:", "https:", "//"))


# Blackboard's opaque stored-content identifiers, which are NOT display names:
#   xid-21916118_1            (content-store item)
#   pid-435737-dt-content-rid-21916118_1
#   _12529_1                  (course/content primary key)
_OPAQUE_ID = re.compile(
    r"^(?:xid-\d+_\d+|pid-\d+-dt-content-rid-\d+_\d+|_\d+_\d+)$", re.I
)


def readable_name(hint):
    """Return a sanitized file name from a display-name hint, or None.

    A hint is usable only when it looks like a real file name: a path basename,
    no leftover query string, no Blackboard xid/pid identifier, and either no
    extension or an extension we recognise. Blackboard's `linkName` metadata
    satisfies this; URL fragments such as "xid-202_1?t=1" do not.
    """
    if not hint:
        return None
    text = str(hint).strip()
    if not text or "?" in text or "#" in text:
        return None
    if _looks_like_url(text):
        try:
            text = unquote(urlparse(text).path)
        except ValueError:
            return None
    else:
        text = unquote(text)
    text = _basename(text.replace("\\", "/")).strip()
    if not text or text in {".", ".."} or text.endswith("."):
        return None
    if _OPAQUE_ID.match(text):
        return None
    root, ext = split_extension(text)
    if ext and not _plausible_extension(ext):
        return None
    if not root.strip():
        return None
    clean = sanitize(text, fallback=None, max_len=120)
    return clean or None
