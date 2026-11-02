"""Organise a sender's inbox messages under a hierarchical Gmail label.

Two ways to organise:

- ``organize labels <email>`` moves one sender's inbox messages under
  ``Archive-Brands/<Brand>/<email>``.
- ``organize plan`` builds a JSON plan from the analysed reports (one
  entry per sender: email, brand, label path, report count), which you
  can review and edit by hand — the plan file *is* the structure
  definition. ``organize apply`` then applies the whole plan to the
  live inbox, creating labels as needed, one batchModify per sender.

The plan's brands come from the *current* brands file, not from the
reports' frontmatter Brand, so editing the brands file after analysis
reshapes the plan. The live inbox is queried per sender at apply time
(``from:<email> in:inbox``), so messages that arrived after the
Takeout are covered and messages deleted or archived since are
skipped; plan counts from the Takeout will not match live counts
exactly. Run apply with ``--dry-run`` first: like ``labels``, this is
a mutating command (it removes ``INBOX`` unless ``--keep-inbox`` is
given) and there is deliberately no interactive confirmation prompt —
a prompt would break non-interactive use, so the dry run is the guard.
Re-running apply is safe: messages already moved no longer match
``in:inbox``, so they are found zero times.
"""

from __future__ import annotations

import json
from email.utils import getaddresses
from pathlib import Path
from typing import Any

import typer
import yaml

from ..auth import get_gmail_service
from ..branding import load_brands, resolve_brand, resolve_brand_from_map
from ..gmail_api import (
    create_label,
    get_or_create_label,
    list_labels,
    list_message_ids,
    modify_messages,
)
from ..paths import iter_report_paths
from ..services import GmailService
from .validators import positive_int_callback

app = typer.Typer(help="Create labels and organise by sender, or apply a plan.")

PLAN_VERSION = 1
LABEL_SCHEME = "Archive-Brands/{brand}/{email}"


def organize_sender(
    service: GmailService,
    email: str,
    dry_run: bool = False,
    limit: int | None = None,
    brands_path: Path | None = None,
) -> tuple[str, int]:
    """Move a sender's inbox messages under ``Archive-Brands/<Brand>/<email>``.

    Returns ``(label_path, moved_count)``; with ``dry_run`` nothing is
    changed and the count is how many messages *would* move. This is the
    testable core; the Typer command below only builds the service and
    prints the result. Run with ``--dry-run`` first: this is the one
    command that mutates labels (it removes ``INBOX``), and there is
    deliberately no interactive confirmation prompt — a prompt would
    break non-interactive use, so the dry run is the guard.
    """
    if brands_path is not None and not brands_path.exists():
        typer.echo(
            f"Warning: brands file not found: {brands_path}; using built-in defaults."
        )
    brand = resolve_brand(email, brands_path)
    label_path = f"Archive-Brands/{brand}/{email}"
    typer.echo(f"Organising {email} into label: {label_path}")

    message_ids = list_message_ids(
        service, query=f"from:{email} in:inbox", max_results=limit
    )
    if not message_ids:
        typer.echo("No messages found in Inbox.")
        return label_path, 0

    if dry_run:
        typer.echo(
            f"Dry run: would move {len(message_ids)} message(s) to {label_path}."
        )
        return label_path, len(message_ids)

    label_id = get_or_create_label(service, label_path)
    modified = modify_messages(
        service, message_ids, add_label_ids=[label_id], remove_label_ids=["INBOX"]
    )
    typer.echo(f"Moved {modified} message(s) to {label_path}.")
    return label_path, modified


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Return a report's YAML frontmatter dict, or {} when unparseable."""
    if not text.startswith("---\n"):
        return {}
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return {}
    try:
        data = yaml.safe_load(text[4:closing]) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def build_plan(
    exports_dir: Path,
    brands_path: Path | None = None,
    min_count: int = 1,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Build an organise plan from the reports under *exports_dir*.

    Returns ``(plan, stats)``. Senders are grouped by bare email address
    parsed from each report's frontmatter Sender (display-name variants
    of one address collapse to one entry); reports with no parseable
    address are counted in ``stats["skipped"]``. Brands resolve from the
    current brands file, and senders below *min_count* reports are left
    out — useful for keeping a large sender set inside Gmail's
    recommended label budget (see README). Entries are sorted by email
    so the plan file is deterministic.
    """
    brands = load_brands(brands_path)
    counts: dict[str, int] = {}
    reports = 0
    skipped = 0
    for path in iter_report_paths(Path(exports_dir)):
        reports += 1
        frontmatter = _parse_frontmatter(path.read_text(encoding="utf-8"))
        sender = str(frontmatter.get("Sender", ""))
        # getaddresses() accepts bare words as addresses ("Unknown Sender"
        # -> "Unknown"), so require "@": only a real address makes a
        # usable from: query, and "Unknown Sender" reports are unorganisable.
        addresses = [addr for _name, addr in getaddresses([sender]) if "@" in addr]
        if not addresses:
            skipped += 1
            continue
        email = addresses[0]
        counts[email] = counts.get(email, 0) + 1
    entries = []
    for email in sorted(counts):
        if counts[email] < min_count:
            continue
        brand = resolve_brand_from_map(email, brands)
        entries.append(
            {
                "email": email,
                "brand": brand,
                "label_path": LABEL_SCHEME.format(brand=brand, email=email),
                "takeout_count": counts[email],
            }
        )
    plan = {
        "version": PLAN_VERSION,
        "label_scheme": LABEL_SCHEME,
        "entries": entries,
    }
    stats = {"reports": reports, "senders": len(entries), "skipped": skipped}
    return plan, stats


class _LabelCache:
    """Label-id lookup that lists labels once, then creates as needed.

    ``get_or_create_label`` re-lists on every call; a plan with
    hundreds of senders would pay one list call per sender. The cache
    lists once and serves every entry from the map (case-insensitive,
    like ``get_or_create_label``), creating only the missing labels.
    """

    def __init__(self, service: GmailService) -> None:
        self._service = service
        self._ids = {
            str(label.get("name", "")).lower(): str(label["id"])
            for label in list_labels(service)
            if label.get("id")
        }

    def get_or_create(self, name: str) -> str:
        key = name.lower()
        if key in self._ids:
            return self._ids[key]
        label_id = create_label(self._service, name)
        self._ids[key] = label_id
        return label_id


def apply_plan(
    service: GmailService,
    plan: dict[str, Any],
    dry_run: bool = False,
    limit: int | None = None,
    keep_inbox: bool = False,
    journal_path: Path | None = None,
) -> dict[str, Any]:
    """Apply a plan dict to the live inbox. Returns a summary dict.

    One entry at a time: list that sender's inbox messages, then (unless
    dry-running) create the label if missing and batchModify the batch.
    A failing entry is recorded and skipped, never fatal to the rest of
    the plan. With *journal_path*, one JSON record per entry plus a
    summary record is appended, so the outcome survives the terminal.
    """
    entries = plan.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Plan has no entries list")
    cache: _LabelCache | None = None
    handle = None
    if journal_path is not None:
        journal_path = Path(journal_path)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        handle = journal_path.open("a", encoding="utf-8")  # noqa: SIM115
    summary: dict[str, Any] = {
        "entries": len(entries),
        "applied": 0,
        "no_messages": 0,
        "errors": 0,
        "found": 0,
        "moved": 0,
        "dry_run": dry_run,
        "keep_inbox": keep_inbox,
    }
    try:
        for entry in entries:
            record: dict[str, Any] = {
                "email": None,
                "label_path": None,
                "outcome": "error",
                "found": 0,
                "moved": 0,
                "error": None,
            }
            if not isinstance(entry, dict):
                record["error"] = "entry is not an object"
                summary["errors"] += 1
                typer.echo("Skipping entry: not an object.")
                if handle is not None:
                    handle.write(json.dumps(record) + "\n")
                continue
            email = entry.get("email")
            label_path = entry.get("label_path")
            if not label_path and entry.get("brand") and email:
                label_path = LABEL_SCHEME.format(brand=entry["brand"], email=email)
            record["email"] = email
            record["label_path"] = label_path
            if not email or not label_path:
                record["error"] = "entry is missing email or label_path"
                summary["errors"] += 1
                typer.echo(f"Skipping entry: missing email or label_path: {entry}.")
                if handle is not None:
                    handle.write(json.dumps(record) + "\n")
                continue
            try:
                message_ids = list_message_ids(
                    service, query=f"from:{email} in:inbox", max_results=limit
                )
            except Exception as exc:  # noqa: BLE001 - recorded, plan continues
                record["error"] = str(exc)
                summary["errors"] += 1
                typer.echo(f"{email}: error listing messages: {exc}.")
                if handle is not None:
                    handle.write(json.dumps(record) + "\n")
                continue
            record["found"] = len(message_ids)
            summary["found"] += len(message_ids)
            if not message_ids:
                record["outcome"] = "dry-run" if dry_run else "no-messages"
                summary["no_messages"] += 1
                typer.echo(f"{email}: no messages in Inbox.")
            elif dry_run:
                record["outcome"] = "dry-run"
                typer.echo(
                    f"Dry run: would move {len(message_ids)} message(s) "
                    f"to {label_path}."
                )
            else:
                try:
                    if cache is None:
                        cache = _LabelCache(service)
                    label_id = cache.get_or_create(str(label_path))
                    moved = modify_messages(
                        service,
                        message_ids,
                        add_label_ids=[label_id],
                        remove_label_ids=[] if keep_inbox else ["INBOX"],
                    )
                except Exception as exc:  # noqa: BLE001 - recorded, plan continues
                    record["error"] = str(exc)
                    summary["errors"] += 1
                    typer.echo(f"{email}: error applying label: {exc}.")
                    if handle is not None:
                        handle.write(json.dumps(record) + "\n")
                    continue
                record["outcome"] = "applied"
                record["moved"] = moved
                summary["moved"] += moved
                summary["applied"] += 1
                typer.echo(f"{email} -> {label_path}: moved {moved} message(s).")
            if handle is not None:
                handle.write(json.dumps(record) + "\n")
    finally:
        if handle is not None:
            handle.write(json.dumps({"type": "summary", **summary}) + "\n")
            handle.close()
    return summary


@app.command()
def labels(
    email: str = typer.Argument(..., help="Sender email address to organise."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would move without changing any labels.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        callback=positive_int_callback,
        help="Maximum messages to move (default: all in inbox).",
    ),
    brands_path: Path | None = typer.Option(
        None, "--brands", help="Brands JSON file (defaults to built-in brands)."
    ),
) -> None:
    """Organise a sender's emails into Gmail labels (Archive-Brands/Brand/Sender).

    This removes ``INBOX`` from the matched messages. Run with ``--dry-run``
    first to see how many messages would move.
    """
    service = get_gmail_service()
    organize_sender(
        service, email, dry_run=dry_run, limit=limit, brands_path=brands_path
    )


@app.command()
def plan(
    exports_dir: Path = typer.Option(
        Path("exports"),
        "--exports-dir",
        help="Reports directory to analyse (default: exports).",
    ),
    output: Path = typer.Option(
        Path("organize-plan.json"),
        "--output",
        help="Where to write the plan JSON (default: organize-plan.json).",
    ),
    brands_path: Path | None = typer.Option(
        None, "--brands", help="Brands JSON file (defaults to built-in brands)."
    ),
    min_count: int = typer.Option(
        1,
        "--min-count",
        help="Only include senders with at least this many reports.",
    ),
) -> None:
    """Build an organise plan from the analysed reports.

    Writes a JSON file mapping each sender to a label path, with its
    report count. Review and edit the file before applying it: rename
    a branch, merge two brands, drop a sender — the edited file is the
    structure ``organize apply`` will use.
    """
    if brands_path is not None and not brands_path.exists():
        typer.echo(
            f"Warning: brands file not found: {brands_path}; using built-in defaults."
        )
    plan_data, stats = build_plan(exports_dir, brands_path, min_count)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan_data, indent=2) + "\n", encoding="utf-8")
    typer.echo(
        f"Plan written to {output}: {stats['senders']} sender(s) from "
        f"{stats['reports']} report(s), {stats['skipped']} skipped "
        "(no parseable address)."
    )


@app.command()
def apply(
    plan_path: Path = typer.Argument(
        ..., help="Plan JSON file from `organize plan` (edit it first)."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would move without changing any labels.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        callback=positive_int_callback,
        help="Maximum messages to move per sender (default: all in inbox).",
    ),
    keep_inbox: bool = typer.Option(
        False,
        "--keep-inbox",
        help="Add the label without removing INBOX.",
    ),
    log_path: Path | None = typer.Option(
        None,
        "--log",
        help="Append one JSON record per entry plus a summary record.",
    ),
) -> None:
    """Apply an organise plan to the live inbox.

    Creates any missing labels, then moves each sender's inbox messages
    under its planned label. Run with ``--dry-run`` first. Re-running is
    safe: already-moved messages no longer match ``in:inbox``.
    """
    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        typer.echo(f"Error: cannot read plan file {plan_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: plan file is not valid JSON: {plan_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not isinstance(plan_data, dict):
        typer.echo(
            f"Error: plan file must contain a JSON object: {plan_path}", err=True
        )
        raise typer.Exit(code=1)
    service = get_gmail_service()
    try:
        summary = apply_plan(
            service,
            plan_data,
            dry_run=dry_run,
            limit=limit,
            keep_inbox=keep_inbox,
            journal_path=log_path,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if dry_run:
        typer.echo(
            f"Dry run: {summary['found']} message(s) would move; "
            f"{summary['no_messages']} entries had no messages; "
            f"{summary['errors']} error(s)."
        )
    else:
        typer.echo(
            f"Moved {summary['moved']} message(s) across {summary['applied']} "
            f"entries; {summary['no_messages']} entries had no messages; "
            f"{summary['errors']} error(s)."
        )


if __name__ == "__main__":
    app()
