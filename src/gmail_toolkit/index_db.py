"""SQLite search index derived from ``exports/``.

This is a cache, never a second store. ``exports/`` remains the only
source of truth: every row here is parsed from a report file, a labels
JSON, or a calendar CSV, and deleting the database is always a valid
recovery because rebuilding it just re-reads those files.

Staleness is handled by a manifest, not by trusting the caller: each
message row records its report's mtime and size, and ``update_index``
re-scans the directory listing (cheap, no parsing) and re-parses only
files that are new, changed, or gone. Nothing in the toolkit writes to
this database at archive/import time — there is exactly one writer
(the index commands) and one reader direction (files -> database) —
so the two can never disagree the way two writers can.

Schema (version 1):
- ``messages``: one row per ``*.md`` report, keyed by report path.
  Frontmatter fields plus tracking counts from the report's INFO
  block, plus the manifest columns (``mtime``, ``size``).
- ``messages_fts``: FTS5 over brand, sender, subject, and body, with
  the report path as an UNINDEXED column to join back to ``messages``.
- ``labels``: the label inventory (``labels.json``), not per-message
  membership — report frontmatter does not carry label membership.
- ``events``: every exported calendar ``events.csv`` row.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = "1"

_TRACKING_RE = re.compile(r"Pixels:\s*(\d+),\s*Links:\s*(\d+)")


def default_db_path(exports_dir: Path | str) -> Path:
    """Default index location: ``<exports-dir>/index.db``.

    ``exports/`` is git-ignored, so the database never lands in a
    commit, and it sits next to the files it indexes.
    """
    return Path(exports_dir) / "index.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            report_path TEXT PRIMARY KEY,
            message_id TEXT,
            brand TEXT NOT NULL DEFAULT '',
            sender TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL DEFAULT 'Unknown',
            type TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT '',
            tracking_pixels INTEGER NOT NULL DEFAULT 0,
            tracking_links INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            brand, sender, subject, body, report_path UNINDEXED
        );
        CREATE TABLE IF NOT EXISTS labels (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT '',
            messages_total INTEGER,
            messages_unread INTEGER
        );
        CREATE TABLE IF NOT EXISTS events (
            calendar TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            start TEXT NOT NULL DEFAULT '',
            end TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            creator TEXT NOT NULL DEFAULT '',
            organizer TEXT NOT NULL DEFAULT '',
            attendees TEXT NOT NULL DEFAULT ''
        );
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


def _schema_ok(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and row["value"] == SCHEMA_VERSION


def _split_report(text: str) -> tuple[dict[str, Any], str, tuple[int, int]]:
    """Split a generated report into frontmatter, body, tracking counts.

    Mirrors the layout ``generate_forensic_markdown`` writes: a YAML
    block, an ``[!INFO]`` blockquote, a ``---`` separator, then the
    body. Anything that does not match degrades gracefully: no
    frontmatter means an empty dict and the whole text as body, no
    INFO block means zero tracking counts. Never raises on odd input.
    """
    if not text.startswith("---\n"):
        return {}, text, (0, 0)
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return {}, text, (0, 0)
    try:
        data = yaml.safe_load(text[4:closing]) or {}
    except yaml.YAMLError:
        return {}, text, (0, 0)
    if not isinstance(data, dict):
        return {}, text, (0, 0)
    rest = text[closing + 5 :]
    # The generator puts a blank line after the closing ---, so rest
    # starts with "\n" before the [!INFO] blockquote, not with ">".
    # Strip it first or the blockquote strip below never fires — and the
    # INFO block's "Direct (1:1)" would pollute the FTS body with a "1"
    # term matching every report.
    rest = rest.lstrip("\n")
    info_lines: list[str] = []
    while rest.startswith(">"):
        line, _, rest = rest.partition("\n")
        info_lines.append(line)
    tracking_match = _TRACKING_RE.search("\n".join(info_lines))
    tracking = (
        (int(tracking_match.group(1)), int(tracking_match.group(2)))
        if tracking_match
        else (0, 0)
    )
    if rest.startswith("---\n"):
        rest = rest[4:].lstrip("\n")
    return data, rest, tracking


def _parse_report_file(path: Path, root: Path) -> tuple[dict[str, Any], str]:
    """Parse one report file into a messages row plus its FTS body."""
    text = path.read_text(encoding="utf-8")
    frontmatter, body, (pixels, links) = _split_report(text)
    stat = path.stat()

    def _text(key: str, default: str = "") -> str:
        value = frontmatter.get(key, default)
        return "" if value is None else str(value)

    message_id = frontmatter.get("MessageID")
    row = {
        "report_path": path.relative_to(root).as_posix(),
        "message_id": None if message_id is None else str(message_id),
        "brand": _text("Brand"),
        "sender": _text("Sender"),
        "subject": _text("Subject"),
        "date": _text("Date", "Unknown"),
        "type": _text("Type"),
        "scope": _text("Scope"),
        "timezone": _text("Timezone"),
        "tracking_pixels": pixels,
        "tracking_links": links,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }
    return row, body


def _write_message(
    connection: sqlite3.Connection, row: dict[str, Any], body: str
) -> None:
    """Insert (or re-insert) one message row and its FTS row."""
    connection.execute(
        "DELETE FROM messages WHERE report_path = ?", (row["report_path"],)
    )
    connection.execute(
        "DELETE FROM messages_fts WHERE report_path = ?", (row["report_path"],)
    )
    connection.execute(
        """
        INSERT INTO messages (
            report_path, message_id, brand, sender, subject, date, type,
            scope, timezone, tracking_pixels, tracking_links, mtime, size
        ) VALUES (
            :report_path, :message_id, :brand, :sender, :subject, :date,
            :type, :scope, :timezone, :tracking_pixels, :tracking_links,
            :mtime, :size
        )
        """,
        row,
    )
    connection.execute(
        "INSERT INTO messages_fts (brand, sender, subject, body, report_path)"
        " VALUES (?, ?, ?, ?, ?)",
        (row["brand"], row["sender"], row["subject"], body, row["report_path"]),
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_labels(root: Path) -> list[dict[str, Any]]:
    """Normalise ``labels.json`` rows, or [] when there is no inventory."""
    import json

    path = root / "labels" / "labels.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "id": str(item.get("id", "")),
                "name": str(item.get("name", "")),
                "type": str(item.get("type", "")),
                "messages_total": _as_int(item.get("messagesTotal")),
                "messages_unread": _as_int(item.get("messagesUnread")),
            }
        )
    return rows


def _read_events(root: Path) -> list[dict[str, str]]:
    """Read every ``events.csv`` under ``calendar/events/``."""
    import csv

    events: list[dict[str, str]] = []
    events_root = root / "calendar" / "events"
    if not events_root.is_dir():
        return events
    for path in sorted(events_root.rglob("events.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                events.append(
                    {
                        "calendar": path.parent.name,
                        "name": row.get("Event Name", "") or "",
                        "start": row.get("Start Time", "") or "",
                        "end": row.get("End Time", "") or "",
                        "description": row.get("Description", "") or "",
                        "location": row.get("Location", "") or "",
                        "status": row.get("Status", "") or "",
                        "creator": row.get("Creator Email", "") or "",
                        "organizer": row.get("Organizer Email", "") or "",
                        "attendees": row.get("Attendees", "") or "",
                    }
                )
    return events


def _refresh_labels_events(
    connection: sqlite3.Connection, root: Path
) -> tuple[int, int]:
    """Replace the labels/events tables from the current export files.

    These files are small, so a full refresh is cheaper than a manifest
    diff — and it means a deleted inventory row actually disappears.
    """
    connection.execute("DELETE FROM labels")
    connection.execute("DELETE FROM events")
    labels = _read_labels(root)
    connection.executemany(
        "INSERT INTO labels (id, name, type, messages_total, messages_unread)"
        " VALUES (:id, :name, :type, :messages_total, :messages_unread)",
        labels,
    )
    events = _read_events(root)
    connection.executemany(
        "INSERT INTO events (calendar, name, start, end, description,"
        " location, status, creator, organizer, attendees)"
        " VALUES (:calendar, :name, :start, :end, :description, :location,"
        " :status, :creator, :organizer, :attendees)",
        events,
    )
    return len(labels), len(events)


def _require_exports_dir(exports_dir: Path | str) -> Path:
    root = Path(exports_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"No exports directory: {root}")
    return root


def build_index(
    exports_dir: Path | str, db_path: Path | str | None = None
) -> dict[str, int]:
    """Full rebuild: drop every table and re-index all of ``exports/``."""
    root = _require_exports_dir(exports_dir)
    db = Path(db_path) if db_path is not None else default_db_path(root)
    connection = _connect(db)
    try:
        for table in ("messages", "messages_fts", "labels", "events", "meta"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        _create_schema(connection)
        messages = 0
        for path in sorted(root.rglob("*.md")):
            row, body = _parse_report_file(path, root)
            _write_message(connection, row, body)
            messages += 1
        labels, events = _refresh_labels_events(connection, root)
        connection.commit()
    finally:
        connection.close()
    return {"messages": messages, "labels": labels, "events": events}


def update_index(
    exports_dir: Path | str, db_path: Path | str | None = None
) -> dict[str, int]:
    """Manifest-checked update: re-parse only new, changed, or gone files.

    With no database (or a schema mismatch) this is just a full build.
    ``mtime`` + ``size`` is the change signal: a report edited in place
    with identical size and mtime would be missed until a full rebuild,
    which is the documented trade-off of a manifest over hashing.
    """
    root = _require_exports_dir(exports_dir)
    db = Path(db_path) if db_path is not None else default_db_path(root)
    if not db.exists():
        counts = build_index(root, db)
        return {"added": counts["messages"], "updated": 0, "removed": 0, **counts}
    connection = _connect(db)
    try:
        if not _schema_ok(connection):
            connection.close()
            counts = build_index(root, db)
            return {
                "added": counts["messages"],
                "updated": 0,
                "removed": 0,
                **counts,
            }
        stored = {
            row["report_path"]: (row["mtime"], row["size"])
            for row in connection.execute(
                "SELECT report_path, mtime, size FROM messages"
            )
        }
        current: dict[str, tuple[float, int]] = {}
        for path in sorted(root.rglob("*.md")):
            stat = path.stat()
            current[path.relative_to(root).as_posix()] = (stat.st_mtime, stat.st_size)
        removed = 0
        for report_path in stored:
            if report_path not in current:
                connection.execute(
                    "DELETE FROM messages WHERE report_path = ?", (report_path,)
                )
                connection.execute(
                    "DELETE FROM messages_fts WHERE report_path = ?",
                    (report_path,),
                )
                removed += 1
        added = updated = 0
        for report_path, (mtime, size) in current.items():
            if report_path not in stored:
                row, body = _parse_report_file(root / report_path, root)
                _write_message(connection, row, body)
                added += 1
            elif stored[report_path] != (mtime, size):
                row, body = _parse_report_file(root / report_path, root)
                _write_message(connection, row, body)
                updated += 1
        labels, events = _refresh_labels_events(connection, root)
        connection.commit()
        total = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        connection.close()
    return {
        "added": added,
        "updated": updated,
        "removed": removed,
        "messages": total,
        "labels": labels,
        "events": events,
    }


def _fts_query(user_query: str) -> str | None:
    """Turn free text into a safe FTS5 query, or None when it has no terms.

    Only word characters survive, so FTS5 operators, quotes, and
    injection attempts in the search box are dropped rather than
    interpreted — including the bare-word operators AND, OR, NOT, and
    NEAR, which ``\\w+`` alone would have let through. Surviving terms
    are ANDed (FTS5's default for bare terms); phrase search is not
    offered in this version.
    """
    tokens = [
        token
        for token in re.findall(r"\w+", user_query, re.UNICODE)
        if token.upper() not in ("AND", "OR", "NOT", "NEAR")
    ]
    if not tokens:
        return None
    return " ".join(tokens)


def _require_db(db_path: Path) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(
            f"No index found: {db_path} (run `gmail-toolkit index build` first)"
        )
    return db_path


def search_index(
    db_path: Path | str,
    query: str,
    *,
    brand: str | None = None,
    sender: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """FTS5 search over brand/sender/subject/body, best matches first."""
    db = _require_db(Path(db_path))
    fts = _fts_query(query)
    if fts is None:
        return []
    sql = (
        "SELECT m.report_path, m.message_id, m.brand, m.sender, m.subject,"
        " m.date, m.type, m.scope,"
        " snippet(messages_fts, 3, '«', '»', '…', 24) AS snippet"
        " FROM messages_fts"
        " JOIN messages m ON m.report_path = messages_fts.report_path"
        " WHERE messages_fts MATCH ?"
    )
    params: list[Any] = [fts]
    if brand:
        sql += " AND m.brand = ?"
        params.append(brand)
    if sender:
        escaped = sender.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND m.sender LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")
    sql += " ORDER BY bm25(messages_fts) LIMIT ?"
    params.append(limit)
    connection = _connect(db)
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def index_stats(db_path: Path | str) -> dict[str, Any]:
    """Totals and breakdowns for the ``index stats`` command."""
    db = _require_db(Path(db_path))
    connection = _connect(db)
    try:
        messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        labels = connection.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        def _breakdown(column: str) -> list[tuple[str, int]]:
            return [
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    f"SELECT {column}, COUNT(*) FROM messages"
                    f" GROUP BY {column} ORDER BY COUNT(*) DESC"
                )
            ]

        tracking = connection.execute(
            "SELECT COALESCE(SUM(tracking_pixels), 0),"
            " COALESCE(SUM(tracking_links), 0) FROM messages"
        ).fetchone()
        top_senders = [
            (str(row[0]), int(row[1]))
            for row in connection.execute(
                "SELECT sender, COUNT(*) FROM messages WHERE sender != ''"
                " GROUP BY sender ORDER BY COUNT(*) DESC LIMIT 10"
            )
        ]
        span = connection.execute(
            "SELECT MIN(date), MAX(date) FROM messages WHERE date != 'Unknown'"
        ).fetchone()
        return {
            "messages": messages,
            "labels": labels,
            "events": events,
            "by_brand": _breakdown("brand"),
            "by_type": _breakdown("type"),
            "by_scope": _breakdown("scope"),
            "tracking_pixels": int(tracking[0]),
            "tracking_links": int(tracking[1]),
            "top_senders": top_senders,
            "date_span": (span[0], span[1]),
        }
    finally:
        connection.close()
