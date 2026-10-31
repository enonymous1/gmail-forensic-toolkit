"""Batch-archive the top senders from the contact directory."""

from __future__ import annotations

import csv
from email.utils import parseaddr
from pathlib import Path

import typer

from ..auth import get_gmail_service
from ..services import GmailService
from .archive_sender import archive_contact
from .validators import non_negative_int_callback, positive_int_callback

app = typer.Typer(help="Batch process multiple senders.")


def process_batch(
    service: GmailService,
    directory: Path,
    batch_size: int = 10,
    skip: int = 0,
    limit: int = 100,
    brands_path: Path | None = None,
    exports_dir: Path = Path("exports"),
) -> int:
    """Archive *batch_size* senders from the contact directory, after *skip*.

    Raises ``ValueError`` for a missing directory or a directory without
    a ``Sender`` column; the Typer command turns that into exit code 1.
    Returns the number of senders processed.
    """
    if not directory.exists():
        raise ValueError("Contact directory not found. Please run 'scrape' first.")

    with directory.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "Sender" not in reader.fieldnames:
            raise ValueError("Contact directory is missing a 'Sender' column.")
        rows = list(reader)

    targets = rows[skip : skip + batch_size]
    typer.echo(f"Starting batch process for {len(targets)} senders...")

    for row in targets:
        sender_value = row.get("Sender", "")
        _name, sender_email = parseaddr(sender_value)
        sender_email = sender_email or sender_value.strip()
        count = row.get("Count", "?")
        typer.echo(f"\nProcessing: {sender_email} ({count} msgs)")
        archive_contact(
            service,
            sender_email,
            limit=limit,
            brands_path=brands_path,
            exports_dir=exports_dir,
        )
    return len(targets)


@app.command()
def process(
    batch_size: int = typer.Option(
        10,
        "--batch-size",
        callback=positive_int_callback,
        help="Senders per batch.",
    ),
    skip: int = typer.Option(
        0,
        "--skip",
        callback=non_negative_int_callback,
        help="Senders to skip from the top.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        callback=positive_int_callback,
        help="Maximum messages to archive per sender.",
    ),
    directory: Path = typer.Option(
        Path("exports") / "Contact_Directory.csv",
        "--directory",
        help="Contact directory CSV produced by the scrape command.",
    ),
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
    brands_path: Path | None = typer.Option(
        None, "--brands", help="Brands JSON file (defaults to built-in brands)."
    ),
) -> None:
    """Loop through senders from the Contact_Directory.csv in batches."""
    try:
        process_batch(
            get_gmail_service(),
            directory,
            batch_size=batch_size,
            skip=skip,
            limit=limit,
            brands_path=brands_path,
            exports_dir=exports_dir,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
