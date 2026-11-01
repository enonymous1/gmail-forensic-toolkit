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
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import typer

from ..branding import resolve_brand
from ..mbox_parser import AttachmentMeta, ParsedMboxMessage, iter_mbox, parse_message
from ..paths import (
    get_base_filename,
    get_sender_directory,
    next_available_path,
    write_text_atomic,
)
from ..process_email import EmailData, generate_forensic_markdown
from .validators import positive_int_callback

app = typer.Typer(help="Import messages from a Google Takeout .mbox file.")

_MESSAGE_ID_RE = re.compile(r"^MessageID:\s*['\"]?(\S+?)['\"]?\s*$", re.MULTILINE)


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
) -> dict[str, int]:
    """Import *mbox_path*; return counts of total/imported/duplicates/errors."""
    if brands_path is not None and not Path(brands_path).exists():
        # Same contract as archive_contact: say so, then fall back to
        # the built-in brands (load_brands' behaviour) instead of failing.
        typer.echo(
            f"Warning: brands file not found: {brands_path}; using built-in defaults."
        )
    counts = {"total": 0, "imported": 0, "duplicates": 0, "errors": 0}
    seen: set[str] = set()
    existing_by_dir: dict[Path, set[str]] = {}
    messages = iter_mbox(mbox_path)
    try:
        for index, msg in enumerate(messages):
            if limit is not None and counts["total"] >= limit:
                break
            counts["total"] += 1
            try:
                parsed = parse_message(msg)
                message_id = parsed.message_id or _fallback_id(parsed)
                if message_id in seen:
                    counts["duplicates"] += 1
                    continue
                seen.add(message_id)
                brand = resolve_brand(parsed.sender, brands_path)
                sender_dir = get_sender_directory(
                    exports_dir, brand, parsed.sender_address or parsed.sender
                )
                existing = existing_by_dir.setdefault(
                    sender_dir, _existing_message_ids(sender_dir)
                )
                if message_id in existing:
                    counts["duplicates"] += 1
                    continue
                email_data = _to_email_data(parsed, message_id)
                stem = get_base_filename(email_data["date"], email_data["subject"])
                md_path = next_available_path(sender_dir / f"{stem}.md")
                report = generate_forensic_markdown(email_data, brand)
                if parsed.attachments:
                    report += _attachments_section(parsed.attachments)
                write_text_atomic(md_path, report)
                existing.add(message_id)
                counts["imported"] += 1
                typer.echo(f"  Imported: {md_path.name}")
            except Exception as exc:  # noqa: BLE001 - resilience is the contract
                counts["errors"] += 1
                typer.echo(f"Warning: skipping message {index}: {exc}; continuing.")
    finally:
        messages.close()
    return counts


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
) -> None:
    """Import a Takeout .mbox file as forensic Markdown reports.

    Offline: no Gmail API call, no OAuth. Duplicates are skipped, a
    malformed message is counted and skipped, and attachment bytes are
    never extracted (only metadata is recorded in the report).
    """
    counts = import_mbox(mbox_path, exports_dir, brands_path, limit)
    typer.echo(
        f"\n{counts['imported']} imported, {counts['duplicates']} duplicate(s) "
        f"skipped, {counts['errors']} error(s) skipped, "
        f"{counts['total']} read."
    )
