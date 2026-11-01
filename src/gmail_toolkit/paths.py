"""Filename and directory helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

_ILLEGAL = re.compile(r'[\\/*?:"<>|\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Remove characters that are illegal in filenames on Windows/macOS/Linux.

    Trailing dots and spaces are also stripped: Windows refuses to create
    files whose names end in them, even though other platforms allow it.
    """
    cleaned = _ILLEGAL.sub("", name).strip().rstrip(". ")
    return cleaned or "untitled"


def get_base_filename(date_dt: datetime | None, subject: str) -> str:
    """Return the ``yyyy-MM-dd_HHmm_Subject`` filename stem used for exports.

    A message with no known date gets a ``nodate`` stem instead of an
    invented timestamp: the filename must never imply a date the payload
    did not carry.
    """
    timestamp = date_dt.strftime("%Y-%m-%d_%H%M") if date_dt is not None else "nodate"
    clean_subject = sanitize_filename(subject)
    return f"{timestamp}_{clean_subject[:100]}"


def sender_directory_path(exports_dir: str | Path, brand: str, email: str) -> Path:
    """Return the ``exports/<Brand>/<Sender>/`` path for *email* (no mkdir).

    The dry-run and preview paths use this: computing where a report
    would land must not create directories as a side effect.
    """
    sender_clean = sanitize_filename(email.replace("@", "_at_").replace(".", "_"))
    return Path(exports_dir) / brand / sender_clean


def get_sender_directory(exports_dir: str | Path, brand: str, email: str) -> Path:
    """Return the ensured ``exports/<Brand>/<Sender>/`` directory for *email*.

    Shared by the API archive path and the mbox importer so both land in
    the same layout the viewer browses: ``@`` becomes ``_at_`` and dots
    become underscores, then illegal filename characters are stripped.
    """
    return ensure_directory(sender_directory_path(exports_dir, brand, email))


def ensure_directory(path: str | Path) -> Path:
    """Create *path* (including parents) if needed and return it as a Path."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_text_atomic(path: str | Path, text: str) -> None:
    """Write *text* to *path* via a temp file in the same directory + rename.

    A crash mid-write used to leave a truncated export that ``restore``
    would then choke on. The temp file lives beside the destination so
    the rename stays on one filesystem (and therefore atomic); on
    Windows ``os.replace`` overwrites an existing destination, which is
    safe here because callers pick a collision-free path first.
    """
    target = Path(path)
    ensure_directory(target.parent)
    tmp = target.with_name(f".{target.name}.tmp")
    # newline="": write the text verbatim (no platform translation), so a
    # CSV's CRLF rows are not doubled on Windows.
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, target)


def write_bytes_atomic(path: str | Path, data: bytes) -> None:
    """Write *data* to *path* via a temp file in the same directory + rename.

    The bytes twin of ``write_text_atomic``: attachment extraction uses
    it so a crash mid-write cannot leave a truncated attachment that
    looks complete.
    """
    target = Path(path)
    ensure_directory(target.parent)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def iter_report_paths(root: Path) -> Iterator[Path]:
    """Every ``*.md`` under *root* except files inside ``*.attachments`` dirs.

    Extracted attachments live beside their report in a
    ``<stem>.attachments`` directory, so a ``.md`` attachment would
    otherwise be indexed and listed as a report. Sender directories
    never end in ``.attachments`` (dots become underscores there), so
    the only paths this skips are extracted attachments.
    """
    for path in sorted(root.rglob("*.md")):
        if any(part.endswith(".attachments") for part in path.parts[:-1]):
            continue
        yield path


def next_available_path(path: str | Path) -> Path:
    """Return *path* if free, otherwise ``stem_2.ext``, ``stem_3.ext``, ...

    Two messages can share a timestamp and subject, and silently overwriting
    the first export would lose evidence. Collisions get a numeric suffix.
    """
    candidate = Path(path)
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while True:
        numbered = candidate.with_name(f"{stem}_{counter}{suffix}")
        if not numbered.exists():
            return numbered
        counter += 1
