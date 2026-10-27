"""Organise a sender's inbox messages under a hierarchical Gmail label."""

from __future__ import annotations

from pathlib import Path

import typer

from ..auth import get_gmail_service
from ..branding import resolve_brand
from ..gmail_api import get_or_create_label, list_message_ids, modify_messages

app = typer.Typer(help="Create labels and organise by sender.")


@app.command()
def labels(
    email: str = typer.Argument(..., help="Sender email address to organise."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would move without changing any labels.",
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Maximum messages to move (default: all in inbox)."
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
    brand = resolve_brand(email, brands_path)
    label_path = f"Archive-Brands/{brand}/{email}"
    typer.echo(f"Organising {email} into label: {label_path}")

    message_ids = list_message_ids(
        service, query=f"from:{email} in:inbox", max_results=limit
    )
    if not message_ids:
        typer.echo("No messages found in Inbox.")
        return

    if dry_run:
        typer.echo(
            f"Dry run: would move {len(message_ids)} message(s) to {label_path}."
        )
        return

    label_id = get_or_create_label(service, label_path)
    modified = modify_messages(
        service, message_ids, add_label_ids=[label_id], remove_label_ids=["INBOX"]
    )
    typer.echo(f"Moved {modified} message(s) to {label_path}.")


if __name__ == "__main__":
    app()
