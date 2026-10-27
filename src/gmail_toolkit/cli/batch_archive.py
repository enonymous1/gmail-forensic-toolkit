"""Batch-archive the top senders from the contact directory."""

from __future__ import annotations

import csv
from email.utils import parseaddr
from pathlib import Path

import typer

from ..auth import get_gmail_service
from .archive_sender import archive_contact

app = typer.Typer(help="Batch process multiple senders.")


@app.command()
def process(
    batch_size: int = typer.Option(10, "--batch-size", help="Senders per batch."),
    skip: int = typer.Option(0, "--skip", help="Senders to skip from the top."),
    limit: int = typer.Option(
        100, "--limit", help="Maximum messages to archive per sender."
    ),
    directory: Path = typer.Option(
        Path("exports") / "Contact_Directory.csv",
        "--directory",
        help="Contact directory CSV produced by the scrape command.",
    ),
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
) -> None:
    """Loop through senders from the Contact_Directory.csv in batches."""
    if not directory.exists():
        typer.echo("Contact directory not found. Please run 'scrape' first.")
        raise typer.Exit(code=1)

    with directory.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "Sender" not in reader.fieldnames:
            typer.echo("Contact directory is missing a 'Sender' column.")
            raise typer.Exit(code=1)
        rows = list(reader)

    targets = rows[skip : skip + batch_size]
    typer.echo(f"Starting batch process for {len(targets)} senders...")

    service = get_gmail_service()
    for row in targets:
        sender_value = row.get("Sender", "")
        _name, sender_email = parseaddr(sender_value)
        sender_email = sender_email or sender_value.strip()
        count = row.get("Count", "?")
        typer.echo(f"\nProcessing: {sender_email} ({count} msgs)")
        archive_contact(service, sender_email, limit=limit, exports_dir=exports_dir)


if __name__ == "__main__":
    app()
