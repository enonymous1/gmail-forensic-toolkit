"""Filename and directory helpers."""

from __future__ import annotations

import os
import re
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
