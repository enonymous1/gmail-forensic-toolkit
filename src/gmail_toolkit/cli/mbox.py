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
- ``import --log`` appends one JSON record per message outcome (plus
  a summary record) to a journal file, so errors and skips survive
  the terminal. ``verify`` re-streams a file and reconciles it
  against the reports, listing Message-IDs in the file with no
  report — the back-check that proves an import completed.
- With ``--save-eml``, each newly imported message's raw bytes are
  saved beside its report as ``<stem>.eml``, with the filename and
  SHA-256 recorded in frontmatter and in the journal; without it, no
  raw bytes are kept. ``verify`` checks a saved ``.eml`` exists and
  its hash matches. ``preview`` reports the total raw size, so the
  job can be sized first.
- Imported reports carry the metadata the source message carried:
  recipients, Gmail labels, threading and authentication headers,
  the source Takeout file, and attachment names, in frontmatter.
  With ``--extract-attachments``, attachment bytes are extracted
  beside their report (see below); without it, only metadata is
  recorded and bytes are never written.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import typer

from ..branding import resolve_brand
from ..mbox_parser import AttachmentMeta, ParsedMboxMessage, iter_mbox, parse_message
from ..paths import (
    ensure_directory,
    get_base_filename,
    get_sender_directory,
    next_available_path,
    sanitize_filename,
    sender_directory_path,
    write_bytes_atomic,
    write_text_atomic,
)
from ..process_email import (
    EmailData,
    generate_forensic_markdown,
    read_report_frontmatter,
)
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
    message. The same scan backs ``report_path`` for verify's .eml
    checks.
    """

    def __init__(self, exports_dir: Path) -> None:
        self.exports_dir = exports_dir
        self.seen: set[str] = set()
        self.paths_by_dir: dict[Path, dict[str, Path]] = {}

    def is_duplicate(self, message_id: str, sender_dir: Path) -> bool:
        return self.status(message_id, sender_dir) != "missing"

    def status(self, message_id: str, sender_dir: Path) -> str:
        """Classify *message_id*: ``seen``, ``imported``, or ``missing``.

        ``seen`` means already encountered in this run, ``imported``
        means already present in the destination directory from an
        earlier run, ``missing`` means neither. Import and preview
        only need the duplicate question; verify needs all three.
        """
        if message_id in self.seen:
            return "seen"
        self.seen.add(message_id)
        existing = self.paths_by_dir.setdefault(
            sender_dir, _report_paths_by_id(sender_dir)
        )
        return "imported" if message_id in existing else "missing"

    def report_path(self, message_id: str, sender_dir: Path) -> Path | None:
        """Report file holding *message_id* in *sender_dir*, if any."""
        return self.paths_by_dir.setdefault(
            sender_dir, _report_paths_by_id(sender_dir)
        ).get(message_id)


def _body_hash(parsed: ParsedMboxMessage) -> str:
    """Full-body hash, for telling true duplicates from collisions.

    The fallback id hashes only sender, subject, date header, and the
    first 1,000 plain characters, so two different messages can share
    one. Comparing full-body hashes separates a true duplicate
    (identical body) from a collision (same id, different body).
    """
    material = f"{parsed.body_plain}\n{parsed.body_html}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _fallback_id(parsed: ParsedMboxMessage) -> str:
    """Deterministic id for a message without a Message-ID header."""
    material = "\n".join(
        [parsed.sender, parsed.subject, parsed.date_header, parsed.body_plain[:1000]]
    )
    return f"mbox-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def _report_paths_by_id(sender_dir: Path) -> dict[str, Path]:
    """Map MessageID -> report file for a sender directory's reports."""
    paths: dict[str, Path] = {}
    if not sender_dir.is_dir():
        return paths
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
            paths.setdefault(match.group(1), path)
    return paths


def _sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed so a large .eml stays cheap."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _attachments_section(
    attachments: list[AttachmentMeta],
    extracted: list[str | None] | None = None,
) -> str:
    """Markdown section recording attachment metadata.

    With ``extracted`` (one relative path per attachment, or None),
    each line also says where the bytes went; without it the section
    states plainly that files were not extracted.
    """
    lines = [
        "",
        "## Attachments",
        "",
    ]
    if extracted is None:
        lines.append("Attachment files were not extracted; only metadata is recorded.")
    else:
        lines.append("Attachment files were extracted alongside this report.")
    lines.append("")
    for index, attachment in enumerate(attachments):
        name = attachment.filename or "(unnamed)"
        line = f"- {name} ({attachment.content_type}, {attachment.size_bytes} bytes)"
        if extracted is not None and extracted[index]:
            line += f" — extracted to {extracted[index]}"
        lines.append(line)
    return "\n".join(lines) + "\n"


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
}


def _plan_attachment_targets(
    attach_dir: Path, attachments: list[AttachmentMeta]
) -> list[tuple[AttachmentMeta, Path]]:
    """One target path per attachment, collision-free within the message.

    ``next_available_path`` only sees files already on disk, but all
    of a message's targets are planned before any of them is written,
    so two attachments with the same name would otherwise plan the
    same path and the second write would overwrite the first. Track
    planned targets too, and bump the numeric suffix past them.
    """
    planned: list[tuple[AttachmentMeta, Path]] = []
    used: set[Path] = set()
    for attachment in attachments:
        target = next_available_path(
            attach_dir / _safe_attachment_name(attachment.filename)
        )
        while target in used:
            match = re.match(r"^(.*)_(\d+)$", target.stem)
            if match:
                stem = f"{match.group(1)}_{int(match.group(2)) + 1}"
            else:
                stem = f"{target.stem}_2"
            target = target.with_name(f"{stem}{target.suffix}")
        used.add(target)
        planned.append((attachment, target))
    return planned


def _safe_attachment_name(filename: str | None) -> str:
    """A filesystem-safe basename for an attachment filename.

    Filenames come from the message, so they can carry directory
    traversal (``../../evil.exe``), absolute paths, or
    Windows-reserved stems. Take the basename, strip illegal
    characters, and prefix reserved stems, so the extracted file
    always lands inside its ``.attachments`` directory.
    """
    if not filename:
        return "(unnamed)"
    cleaned = sanitize_filename(Path(filename).name)
    if Path(cleaned).stem.upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def import_mbox(
    mbox_path: Path,
    exports_dir: Path = Path("exports"),
    brands_path: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    quiet: bool = False,
    log_path: Path | None = None,
    extract_attachments: bool = False,
    save_eml: bool = False,
) -> dict[str, int]:
    """Import *mbox_path*; return counts of total/imported/duplicates/errors.

    With ``dry_run`` set, the same parse/dedupe/filename logic runs but
    nothing is written: no report files, no directories, and no
    extracted attachments, and ``imported`` counts messages that
    *would* be imported. With ``quiet`` set, the per-message lines
    are suppressed; warnings and the final summary still print. With
    ``log_path`` set, one JSON record per message outcome plus a
    summary record is appended to that file, so errors and skips
    survive the terminal. With ``extract_attachments`` set, each
    newly imported message's attachments are written beside its
    report in a ``<stem>.attachments`` directory (never by default:
    attachments can carry malware, and a large Takeout can hold
    gigabytes of them). Extraction happens only for newly imported
    messages, not for duplicates skipped by dedupe. With ``save_eml``
    set, each newly imported message's raw bytes are written beside
    its report as ``<stem>.eml`` (never by default: it duplicates the
    .mbox on disk, roughly 1x its size), with the filename and SHA-256
    recorded in frontmatter and in the journal.
    """
    _warn_missing_brands(brands_path)
    counts = {
        "total": 0,
        "imported": 0,
        "duplicates": 0,
        "errors": 0,
        "attachments": 0,
        "attachment_bytes": 0,
        "eml_files": 0,
        "eml_bytes": 0,
    }
    tracker = _DedupeTracker(exports_dir)
    log_handle = None
    if log_path is not None:
        ensure_directory(Path(log_path).parent)
        log_handle = Path(log_path).open("a", encoding="utf-8")  # noqa: SIM115 - closed in finally below
    messages = iter_mbox(mbox_path)
    try:
        for index, msg in enumerate(messages):
            if limit is not None and counts["total"] >= limit:
                break
            counts["total"] += 1
            record_id: str | None = None
            written: list[Path] = []
            attach_dir: Path | None = None
            eml_record: dict[str, object] = {
                "path": None,
                "sha256": None,
                "saved": False,
            }
            try:
                parsed = parse_message(msg)
                message_id = parsed.message_id or _fallback_id(parsed)
                record_id = message_id
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
                    outcome = "duplicate"
                    report_name: str | None = None
                    journal_attachments = [
                        {
                            "filename": a.filename,
                            "content_type": a.content_type,
                            "size_bytes": a.size_bytes,
                            "path": None,
                            "extracted": False,
                        }
                        for a in parsed.attachments
                    ]
                else:
                    email_data = _to_email_data(parsed, message_id)
                    stem = get_base_filename(email_data["date"], email_data["subject"])
                    md_path = next_available_path(sender_dir / f"{stem}.md")
                    attach_dir = sender_dir / f"{stem}.attachments"
                    raw_bytes: bytes | None = None
                    raw_sha: str | None = None
                    eml_path: Path | None = None
                    if save_eml:
                        # The .eml is the message re-serialized from the
                        # parsed mailbox object (no mbox "From " separator
                        # line); the recorded hash is of the bytes
                        # actually written. Write the evidence before the
                        # report that references it; the failure path
                        # below removes it if the report then fails, so
                        # a message ends up with both or neither.
                        raw_bytes = msg.as_bytes()
                        raw_sha = hashlib.sha256(raw_bytes).hexdigest()
                        eml_path = sender_dir / f"{md_path.stem}.eml"
                        eml_record = {
                            "path": str(eml_path),
                            "sha256": raw_sha,
                            "saved": False,
                        }
                        if not dry_run:
                            write_bytes_atomic(eml_path, raw_bytes)
                            written.append(eml_path)
                            counts["eml_files"] += 1
                            counts["eml_bytes"] += len(raw_bytes)
                            eml_record["saved"] = True
                    # Plan extraction targets first: frontmatter and
                    # the journal need the paths whether or not bytes
                    # are written, and the report needs them before it
                    # is generated.
                    planned = _plan_attachment_targets(attach_dir, parsed.attachments)
                    extracted = extract_attachments and not dry_run
                    if extracted and planned:
                        ensure_directory(attach_dir)
                        for attachment, target in planned:
                            write_bytes_atomic(target, attachment.data)
                            written.append(target)
                            counts["attachments"] += 1
                            counts["attachment_bytes"] += attachment.size_bytes
                    journal_attachments = [
                        {
                            "filename": attachment.filename,
                            "content_type": attachment.content_type,
                            "size_bytes": attachment.size_bytes,
                            "path": str(target) if extract_attachments else None,
                            "extracted": bool(extracted),
                        }
                        for attachment, target in planned
                    ]
                    extras: dict[str, object] = {
                        "SourceFile": str(mbox_path),
                        "Attachments": [
                            f"{a.filename or '(unnamed)'} "
                            f"({a.content_type}, {a.size_bytes} bytes)"
                            for a in parsed.attachments
                        ],
                        "AttachmentPaths": (
                            [
                                f"{attach_dir.name}/{target.name}"
                                for _, target in planned
                            ]
                            if extract_attachments
                            else []
                        ),
                    }
                    if save_eml and eml_path is not None and raw_sha is not None:
                        extras["RawFile"] = eml_path.name
                        extras["RawSha256"] = raw_sha
                    report = generate_forensic_markdown(
                        email_data,
                        brand,
                        extra_frontmatter=extras,
                    )
                    if parsed.attachments:
                        report += _attachments_section(
                            parsed.attachments,
                            extracted=(
                                [
                                    f"{attach_dir.name}/{target.name}"
                                    for _, target in planned
                                ]
                                if extracted
                                else None
                            ),
                        )
                    if not dry_run:
                        write_text_atomic(md_path, report)
                    counts["imported"] += 1
                    outcome = "imported"
                    report_name = str(md_path)
                    if not quiet:
                        verb = "Would import" if dry_run else "Imported"
                        typer.echo(f"  {verb}: {md_path.name}")
                if log_handle is not None:
                    log_handle.write(
                        json.dumps(
                            {
                                "event": "message",
                                "index": index,
                                "message_id": message_id,
                                "subject": parsed.subject,
                                "sender": parsed.sender,
                                "date": (
                                    parsed.date.isoformat()
                                    if parsed.date is not None
                                    else None
                                ),
                                "outcome": outcome,
                                "report": report_name,
                                "body_sha256": _body_hash(parsed),
                                "attachments": journal_attachments,
                                "eml": eml_record,
                                "error": None,
                            }
                        )
                        + "\n"
                    )
            except Exception as exc:  # noqa: BLE001 - resilience is the contract
                # A failed message must not leave orphaned files: drop
                # any attachments written before the failure, and the
                # directory if it is left empty.
                for target in written:
                    target.unlink(missing_ok=True)
                if attach_dir is not None and attach_dir.is_dir():
                    with contextlib.suppress(OSError):
                        attach_dir.rmdir()
                # The .eml above was unlinked with the rest, so the
                # error record must not claim it was saved.
                eml_record = {"path": None, "sha256": None, "saved": False}
                counts["errors"] += 1
                typer.echo(f"Warning: skipping message {index}: {exc}; continuing.")
                if log_handle is not None:
                    log_handle.write(
                        json.dumps(
                            {
                                "event": "message",
                                "index": index,
                                "message_id": record_id,
                                "subject": None,
                                "sender": None,
                                "date": None,
                                "outcome": "error",
                                "report": None,
                                "body_sha256": None,
                                "attachments": [],
                                "eml": eml_record,
                                "error": str(exc),
                            }
                        )
                        + "\n"
                    )
    finally:
        messages.close()
        if log_handle is not None:
            log_handle.write(
                json.dumps(
                    {
                        "event": "summary",
                        "source": str(mbox_path),
                        "dry_run": dry_run,
                        **counts,
                    }
                )
                + "\n"
            )
            log_handle.close()
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
    label_counts: Counter[str] = Counter()
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
        "attachment_files": 0,
        "attachment_bytes": 0,
        "eml_files": 0,
        "eml_bytes": 0,
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
                raw_len = len(msg.as_bytes())
            except Exception as exc:  # noqa: BLE001 - resilience is the contract
                counts["errors"] += 1
                typer.echo(f"Warning: skipping message {index}: {exc}; continuing.")
                continue
            message_id = parsed.message_id or _fallback_id(parsed)
            counts["eml_files"] += 1
            counts["eml_bytes"] += raw_len
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
            for label in parsed.headers.get("labelids", []) or []:
                label_counts[str(label)] += 1
            if parsed.attachments:
                counts["with_attachments"] += 1
                counts["attachment_files"] += len(parsed.attachments)
                counts["attachment_bytes"] += sum(
                    a.size_bytes for a in parsed.attachments
                )
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
        "by_label": label_counts.most_common(),
    }


def verify_mbox(
    mbox_path: Path,
    exports_dir: Path = Path("exports"),
    brands_path: Path | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    """Reconcile *mbox_path* against the reports in *exports_dir*.

    Writes nothing. Every message in the file is classified: present
    in the exports (``verified``), an in-file repeat (``duplicates``),
    or absent from the exports (``missing``, with details). Messages
    sharing an id but with different bodies are reported as
    ``collisions`` — the case plain dedupe cannot see, including two
    distinct no-ID messages that collided on one fallback id.
    Body hashes are truncated to 64 bits in memory: plenty to tell
    bodies apart, without holding a full hash per message for a huge
    file. For a verified message whose report carries ``RawFile``,
    the ``.eml`` beside the report is checked too: it must exist, and
    its SHA-256 must match the frontmatter ``RawSha256``; failures
    are reported as ``eml_problems``. Brand resolution uses the
    current brands settings, so run verify with the same ``--brands``
    the import used.
    """
    _warn_missing_brands(brands_path)
    counts = {
        "total": 0,
        "errors": 0,
        "verified": 0,
        "duplicates": 0,
        "missing": 0,
        "collisions": 0,
        "eml_problems": 0,
    }
    missing_details: list[dict[str, object]] = []
    collision_details: list[dict[str, object]] = []
    error_details: list[dict[str, object]] = []
    eml_details: list[dict[str, object]] = []
    tracker = _DedupeTracker(exports_dir)
    body_hashes: dict[str, tuple[str, int]] = {}
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
                error_details.append({"index": index, "error": str(exc)})
                continue
            message_id = parsed.message_id or _fallback_id(parsed)
            short_hash = _body_hash(parsed)[:16]
            previous = body_hashes.get(message_id)
            if previous is not None and previous[0] != short_hash:
                counts["collisions"] += 1
                collision_details.append(
                    {
                        "message_id": message_id,
                        "kind": (
                            "fallback" if parsed.message_id is None else "message-id"
                        ),
                        "first_index": previous[1],
                        "index": index,
                        "sender": parsed.sender,
                        "subject": parsed.subject,
                    }
                )
            else:
                body_hashes.setdefault(message_id, (short_hash, index))
            brand = resolve_brand(parsed.sender, brands_path)
            sender_dir = sender_directory_path(
                exports_dir, brand, parsed.sender_address or parsed.sender
            )
            status = tracker.status(message_id, sender_dir)
            if status == "seen":
                counts["duplicates"] += 1
            elif status == "imported":
                counts["verified"] += 1
                report = tracker.report_path(message_id, sender_dir)
                if report is not None:
                    frontmatter = read_report_frontmatter(report)
                    raw_file = frontmatter.get("RawFile")
                    if isinstance(raw_file, str) and raw_file:
                        eml_path = report.parent / raw_file
                        expected = frontmatter.get("RawSha256")
                        if not eml_path.is_file():
                            issue = "missing .eml file"
                        elif (
                            isinstance(expected, str)
                            and expected
                            and _sha256_file(eml_path) != expected
                        ):
                            issue = "hash mismatch"
                        else:
                            issue = ""
                        if issue:
                            counts["eml_problems"] += 1
                            eml_details.append(
                                {
                                    "message_id": message_id,
                                    "sender": parsed.sender,
                                    "subject": parsed.subject,
                                    "issue": issue,
                                    "report": str(report),
                                    "index": index,
                                }
                            )
            else:
                counts["missing"] += 1
                missing_details.append(
                    {
                        "message_id": message_id,
                        "sender": parsed.sender,
                        "subject": parsed.subject,
                        "date": (
                            parsed.date.isoformat() if parsed.date is not None else None
                        ),
                        "index": index,
                    }
                )
    finally:
        messages.close()
    return {
        **counts,
        "missing_details": missing_details,
        "collision_details": collision_details,
        "error_details": error_details,
        "eml_details": eml_details,
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
    log_path: Path | None = typer.Option(
        None,
        "--log",
        help="Append one JSON record per message outcome, plus a summary, to PATH.",
    ),
    extract_attachments: bool = typer.Option(
        False,
        "--extract-attachments",
        help="Extract attachment bytes beside each new report (off by default).",
    ),
    save_eml: bool = typer.Option(
        False,
        "--save-eml",
        help="Save each new message's raw bytes as <stem>.eml beside its report.",
    ),
) -> None:
    """Import a Takeout .mbox file as forensic Markdown reports.

    Offline: no Gmail API call, no OAuth. Duplicates are skipped, a
    malformed message is counted and skipped, and attachment bytes are
    extracted only with ``--extract-attachments`` (otherwise only
    metadata is recorded in the report). Raw message bytes are saved
    only with ``--save-eml`` (otherwise only the report is written).
    """
    counts = import_mbox(
        mbox_path,
        exports_dir,
        brands_path,
        limit,
        dry_run,
        quiet,
        log_path,
        extract_attachments,
        save_eml,
    )
    verb = "would be imported" if dry_run else "imported"
    summary = (
        f"\n{counts['imported']} {verb}, {counts['duplicates']} duplicate(s) "
        f"skipped, {counts['errors']} error(s) skipped, "
        f"{counts['total']} read."
    )
    if extract_attachments:
        summary += (
            f" {counts['attachments']} attachment(s) extracted "
            f"({counts['attachment_bytes']} bytes)."
        )
    if save_eml:
        summary += (
            f" {counts['eml_files']} .eml file(s) saved ({counts['eml_bytes']} bytes)."
        )
    typer.echo(summary)


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
    typer.echo(
        f"Messages with attachments: {data['with_attachments']} "
        f"({data['attachment_files']} files, {data['attachment_bytes']} bytes)"
    )
    typer.echo(
        f"Raw messages (.eml): {data['eml_files']} files, {data['eml_bytes']} bytes"
    )
    typer.echo(f"Unique senders: {data['sender_count']}")
    typer.echo("By brand:")
    for brand, count in cast(list[tuple[str, int]], data["by_brand"]):
        typer.echo(f"  {brand}: {count}")
    typer.echo("By label:")
    for label, count in cast(list[tuple[str, int]], data["by_label"]):
        typer.echo(f"  {label}: {count}")
    typer.echo("Top senders:")
    for sender, count in cast(list[tuple[str, int]], data["top_senders"]):
        typer.echo(f"  {sender}: {count}")


@app.command()
def verify(
    mbox_path: Path = typer.Argument(
        ..., exists=True, help="Path to the .mbox file to reconcile."
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        callback=positive_int_callback,
        help="Maximum messages to check from the file.",
    ),
    brands_path: Path | None = typer.Option(
        None, "--brands", help="Brands JSON file (defaults to built-in brands)."
    ),
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
) -> None:
    """Reconcile a Takeout .mbox file against the exports, writing nothing.

    Reports how many messages in the file already have a report, how
    many are in-file duplicates, and — the actionable part — which
    messages have no report, plus any id collisions (same id, different
    body) and unparseable messages. Run it after an import to prove
    the import completed; use the same ``--brands`` the import used.
    """
    data = verify_mbox(mbox_path, exports_dir, brands_path, limit)
    typer.echo(f"Verify: {mbox_path}")
    typer.echo(f"Messages: {data['total']} ({data['errors']} error(s) unparseable)")
    typer.echo(
        f"Verified present: {data['verified']}, "
        f"in-file duplicates: {data['duplicates']}, "
        f"missing: {data['missing']}, collisions: {data['collisions']}, "
        f".eml problems: {data['eml_problems']}"
    )
    missing = cast(list[dict[str, object]], data["missing_details"])
    if missing:
        typer.echo("Missing reports:")
        for item in missing:
            typer.echo(
                f"  {item['message_id']} — {item['sender']} — "
                f"{str(item['subject'])[:80]}"
            )
    collisions = cast(list[dict[str, object]], data["collision_details"])
    if collisions:
        typer.echo("Collisions (same id, different body):")
        for item in collisions:
            typer.echo(
                f"  {item['message_id']} ({item['kind']}) — first at "
                f"#{item['first_index']}, again at #{item['index']} — "
                f"{item['sender']} — {str(item['subject'])[:80]}"
            )
    errors = cast(list[dict[str, object]], data["error_details"])
    if errors:
        typer.echo("Unparseable messages:")
        for item in errors:
            typer.echo(f"  #{item['index']}: {item['error']}")
    eml_problems = cast(list[dict[str, object]], data["eml_details"])
    if eml_problems:
        typer.echo("Raw .eml problems:")
        for item in eml_problems:
            typer.echo(
                f"  {item['message_id']} ({item['issue']}) — {item['sender']} — "
                f"{str(item['subject'])[:80]}"
            )
