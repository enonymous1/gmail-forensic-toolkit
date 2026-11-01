"""Import a Google Takeout ``.mbox`` file as forensic Markdown reports.

The offline counterpart to ``archive contact``: instead of the Gmail
API it reads a Takeout archive, parses each message with
``mbox_parser`` (adapted from the companion mbox-explorer project), and
writes the same forensic Markdown reports into the same
``exports/<Brand>/<Sender>/`` layout, so the read-only viewer browses
them without knowing which source produced them.

Import contract, mirroring the explorer's ingester:
- Duplicates are skipped: the Message-ID header is the dedupe key, and
  a message already present in the destination sender directory (from
  an earlier import) is skipped too, so re-running an import is safe.
  Messages without a Message-ID get a deterministic ``mbox-<hash>`` id
  derived from sender, subject, date, and body, so re-runs dedupe them
  as well.
- A malformed message is counted and skipped, never fatal: one bad
  message must not abort a multi-gigabyte import.
- Attachments are metadata only (filename, type, size, appended to the
  report). Attachment bytes are never extracted.
- No ``SourceFiles/`` cache is written: the ``.mbox`` file itself is
  the source of truth, so re-importing is the rebuild path and
  ``restore rebuild`` does not apply to mbox imports.
- ``preview`` scans a file without writing anything (counts, date
  span, top senders, new-vs-duplicate breakdown), and ``import
  --dry-run`` runs the import logic with writes disabled, so a large
  Takeout can be scoped before committing to it.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import typer

from ..branding import resolve_brand
from ..mbox_parser import AttachmentMeta, ParsedMboxMessage, iter_mbox, parse_message
from ..paths import (
    get_base_filename,
    get_sender_directory,
    next_available_path,
    sender_directory_path,
    write_text_atomic,
)
from ..process_email import EmailData, generate_forensic_markdown
from .validators import positive_int_callback

app = typer.Typer(help="Import messages from a Google Takeout .mbox file.")

_MESSAGE_ID_RE = re.compile(r"^MessageID:\s*['\"]?(\S+?)['\"]?\s*$", re.MULTILINE)


def _warn_missing_brands(brands_path: Path | None) -> None:
    if brands_path is not None and not Path(brands_path).exists():
        # Same contract as archive_contact: say so, then fall back to
        # the built-in brands (load_brands' behaviour) instead of failing.
        typer.echo(
            f"Warning: brands file not found: {brands_path}; using built-in defaults."
        )


class _DedupeTracker:
    """One dedupe implementation shared by import and preview.

    A message is a duplicate when its id was already seen in this run
    or is already present in its destination sender directory (from an
    earlier import). The existing-directory scan is cached per directory
    for the run, so a large file does not re-read every report per
    message.
    """

    def __init__(self, exports_dir: Path) -> None:
        self.exports_dir = exports_dir
        self.seen: set[str] = set()
        self.existing_by_dir: dict[Path, set[str]] = {}

    def is_duplicate(self, message_id: str, sender_dir: Path) -> bool:
        if message_id in self.seen:
            return True
        self.seen.add(message_id)
        existing = self.existing_by_dir.setdefault(
            sender_dir, _existing_message_ids(sender_dir)
        )
        return message_id in existing


def _fallback_id(parsed: ParsedMboxMessage) -> str:
    """Deterministic id for a message without a Message-ID header."""
    material = "\n".join(
        [parsed.sender, parsed.subject, parsed.date_header, parsed.body_plain[:1000]]
    )
    return f"mbox-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def _existing_message_ids(sender_dir: Path) -> set[str]:
    """MessageIDs already present in a sender directory's reports."""
    ids: set[str] = set()
    if not sender_dir.is_dir():
        return ids
    for path in sender_dir.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---\n"):
            continue
        closing = text.find("\n---\n", 4)
        if closing == -1:
            continue
        match = _MESSAGE_ID_RE.search(text[4:closing])
        if match:
            ids.add(match.group(1))
    return ids


def _to_email_data(parsed: ParsedMboxMessage, message_id: str) -> EmailData:
    """Map a parsed mbox message onto the forensic pipeline's shape."""
    return {
        "id": message_id,
        "subject": parsed.subject or "(No Subject)",
        "sender": parsed.sender or "Unknown Sender",
        "date": parsed.date,
        "body_html": parsed.body_html,
        "body_plain": parsed.body_plain,
        "headers": parsed.headers,
        # No API payload exists for an mbox message, and no SourceFiles
        # cache is written for imports (see the module docstring), so
        # this stays empty rather than pretending to be a payload.
        "raw_json": {},
    }


def _attachments_section(attachments: list[AttachmentMeta]) -> str:
    """Markdown section recording attachment metadata (bytes not kept)."""
    lines = [
        "",
        "## Attachments",
        "",
        "Attachment files were not extracted; only metadata is recorded.",
        "",
    ]
    for attachment in attachments:
        name = attachment.filename or "(unnamed)"
        lines.append(
            f"- {name} ({attachment.content_type}, {attachment.size_bytes} bytes)"
        )
    return "\n".join(lines) + "\n"


def import_mbox(
    mbox_path: Path,
    exports_dir: Path = Path("exports"),
    brands_path: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> dict[str, int]:
    """Import *mbox_path*; return counts of total/imported/duplicates/errors.

    With ``dry_run`` set, the same parse/dedupe/filename logic runs but
    nothing is written: no report files and no directories are created,
    and ``imported`` counts messages that *would* be imported. With
    ``quiet`` set, the per-message lines are suppressed; warnings and
    the final summary still print.
    """
    _warn_missing_brands(brands_path)
    counts = {"total": 0, "imported": 0, "duplicates": 0, "errors": 0}
    tracker = _DedupeTracker(exports_dir)
    messages = iter_mbox(mbox_path)
    try:
        for index, msg in enumerate(messages):
            if limit is not None and counts["total"] >= limit:
                break
            counts["total"] += 1
            try:
                parsed = parse_message(msg)
                message_id = parsed.message_id or _fallback_id(parsed)
                brand = resolve_brand(parsed.sender, brands_path)
                sender_email = parsed.sender_address or parsed.sender
                # Dry-run must not create directories as a side effect,
                # so compute the would-be path instead of ensuring it.
                sender_dir = (
                    sender_directory_path(exports_dir, brand, sender_email)
                    if dry_run
                    else get_sender_directory(exports_dir, brand, sender_email)
                )
                if tracker.is_duplicate(message_id, sender_dir):
                    counts["duplicates"] += 1
                    continue
                email_data = _to_email_data(parsed, message_id)
                stem = get_base_filename(email_data["date"], email_data["subject"])
                md_path = next_available_path(sender_dir / f"{stem}.md")
                report = generate_forensic_markdown(email_data, brand)
                if parsed.attachments:
                    report += _attachments_section(parsed.attachments)
                if not dry_run:
                    write_text_atomic(md_path, report)
                counts["imported"] += 1
                if not quiet:
                    verb = "Would import" if dry_run else "Imported"
                    typer.echo(f"  {verb}: {md_path.name}")
            except Exception as exc:  # noqa: BLE001 - resilience is the contract
                counts["errors"] += 1
                typer.echo(f"Warning: skipping message {index}: {exc}; continuing.")
    finally:
        messages.close()
    return counts


def preview_mbox(
    mbox_path: Path,
    exports_dir: Path = Path("exports"),
    brands_path: Path | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    """Read-only scan of *mbox_path*; nothing is written.

    Parse-only (no Markdown is generated), so this is the fast way to
    scope a large Takeout: message count, date span, top senders,
    shares without a Message-ID or a parseable date, the error rate,
    and how many messages would be new versus duplicates against the
    existing exports.
    """
    _warn_missing_brands(brands_path)
    sender_counts: Counter[str] = Counter()
    brand_counts: Counter[str] = Counter()
    date_min: datetime | None = None
    date_max: datetime | None = None
    counts = {
        "total": 0,
        "errors": 0,
        "with_message_id": 0,
        "without_message_id": 0,
        "with_date": 0,
        "without_date": 0,
        "new": 0,
        "duplicates": 0,
        "with_attachments": 0,
    }
    tracker = _DedupeTracker(exports_dir)
    messages = iter_mbox(mbox_path)
    try:
        for index, msg in enumerate(messages):
            if limit is not None and counts["total"] >= limit:
                break
            counts["total"] += 1
            try:
                parsed = parse_message(msg)
            except Exception as exc:  # noqa: BLE001 - resilience is the contract
                counts["errors"] += 1
                typer.echo(f"Warning: skipping message {index}: {exc}; continuing.")
                continue
            message_id = parsed.message_id or _fallback_id(parsed)
            if parsed.message_id:
                counts["with_message_id"] += 1
            else:
                counts["without_message_id"] += 1
            if parsed.date is not None:
                counts["with_date"] += 1
                # Headers mix offsets (and sometimes omit one); compare
                # in UTC so a naive and an aware date never collide.
                utc_date = parsed.date
                if utc_date.tzinfo is None:
                    utc_date = utc_date.replace(tzinfo=timezone.utc)
                else:
                    utc_date = utc_date.astimezone(timezone.utc)
                if date_min is None or utc_date < date_min:
                    date_min = utc_date
                if date_max is None or utc_date > date_max:
                    date_max = utc_date
            else:
                counts["without_date"] += 1
            sender_counts[parsed.sender or "Unknown Sender"] += 1
            brand = resolve_brand(parsed.sender, brands_path)
            brand_counts[brand] += 1
            if parsed.attachments:
                counts["with_attachments"] += 1
            sender_dir = sender_directory_path(
                exports_dir, brand, parsed.sender_address or parsed.sender
            )
            if tracker.is_duplicate(message_id, sender_dir):
                counts["duplicates"] += 1
            else:
                counts["new"] += 1
    finally:
        messages.close()
    return {
        **counts,
        "date_min": date_min,
        "date_max": date_max,
        "sender_count": len(sender_counts),
        "top_senders": sender_counts.most_common(10),
        "by_brand": brand_counts.most_common(),
    }


@app.command(name="import")
def import_command(
    mbox_path: Path = typer.Argument(
        ..., exists=True, help="Path to the .mbox file to import."
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        callback=positive_int_callback,
        help="Maximum messages to read from the file.",
    ),
    brands_path: Path | None = typer.Option(
        None, "--brands", help="Brands JSON file (defaults to built-in brands)."
    ),
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be imported without writing anything.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Print only the summary, not one line per message."
    ),
) -> None:
    """Import a Takeout .mbox file as forensic Markdown reports.

    Offline: no Gmail API call, no OAuth. Duplicates are skipped, a
    malformed message is counted and skipped, and attachment bytes are
    never extracted (only metadata is recorded in the report).
    """
    counts = import_mbox(mbox_path, exports_dir, brands_path, limit, dry_run, quiet)
    verb = "would be imported" if dry_run else "imported"
    typer.echo(
        f"\n{counts['imported']} {verb}, {counts['duplicates']} duplicate(s) "
        f"skipped, {counts['errors']} error(s) skipped, "
        f"{counts['total']} read."
    )


@app.command()
def preview(
    mbox_path: Path = typer.Argument(
        ..., exists=True, help="Path to the .mbox file to scan."
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        callback=positive_int_callback,
        help="Maximum messages to scan from the file.",
    ),
    brands_path: Path | None = typer.Option(
        None, "--brands", help="Brands JSON file (defaults to built-in brands)."
    ),
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
) -> None:
    """Scan a Takeout .mbox file without writing anything.

    Reports how many messages the file holds, what date span they
    cover, who the top senders are, and how many would be new versus
    duplicates against the existing exports, so a large Takeout can
    be scoped before running a real import.
    """
    data = preview_mbox(mbox_path, exports_dir, brands_path, limit)
    date_min = data["date_min"]
    date_max = data["date_max"]
    if isinstance(date_min, datetime) and isinstance(date_max, datetime):
        date_span = f"{date_min.isoformat()} to {date_max.isoformat()}"
    else:
        date_span = "unknown (no parseable dates)"
    typer.echo(f"Preview: {mbox_path}")
    typer.echo(f"Messages: {data['total']} ({data['errors']} error(s) skipped)")
    typer.echo(f"Date span: {date_span} ({data['without_date']} without a date)")
    typer.echo(
        f"Message-ID: {data['with_message_id']} with, "
        f"{data['without_message_id']} without (fallback ids)"
    )
    typer.echo(
        f"Against {exports_dir}: {data['new']} new, "
        f"{data['duplicates']} duplicate(s) would be skipped"
    )
    typer.echo(f"Messages with attachments: {data['with_attachments']}")
    typer.echo(f"Unique senders: {data['sender_count']}")
    typer.echo("By brand:")
    for brand, count in cast(list[tuple[str, int]], data["by_brand"]):
        typer.echo(f"  {brand}: {count}")
    typer.echo("Top senders:")
    for sender, count in cast(list[tuple[str, int]], data["top_senders"]):
        typer.echo(f"  {sender}: {count}")
