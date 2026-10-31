"""Organise a sender's inbox messages under a hierarchical Gmail label."""

from __future__ import annotations

from pathlib import Path

import typer

from ..auth import get_gmail_service
from ..branding import resolve_brand
from ..gmail_api import get_or_create_label, list_message_ids, modify_messages
from ..services import GmailService
from .validators import positive_int_callback

app = typer.Typer(help="Create labels and organise by sender.")


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


if __name__ == "__main__":
    app()
