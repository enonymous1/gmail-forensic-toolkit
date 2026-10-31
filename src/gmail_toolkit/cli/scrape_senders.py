"""Discover unique senders and message counts."""

from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path

import typer

from ..auth import get_gmail_service
from ..gmail_api import batch_get_message_metadata, list_message_ids
from ..paths import write_text_atomic
from ..process_email import headers_dict
from ..services import GmailService
from .validators import positive_int_callback

app = typer.Typer(help="Discover unique senders and message counts.")


def discover_senders(service: GmailService, max_results: int) -> list[tuple[str, int]]:
    """Return ``(sender, count)`` pairs ranked by count, then name.

    Metadata fetches go through the batch helper, not one call per
    message. This is the testable core; the Typer command below only
    builds the service and prints/writes the result.
    """
    message_ids = list_message_ids(service, max_results=max_results)
    senders: list[str] = []
    for msg_data in batch_get_message_metadata(service, message_ids, ["From"]):
        sender = headers_dict(msg_data.get("payload", {}) or {}).get("from")
        if sender:
            senders.append(sender)
    counts = Counter(senders)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


@app.command()
def discovery(
    max_results: int = typer.Option(
        500,
        "--max-results",
        callback=positive_int_callback,
        help="Maximum messages to scan.",
    ),
    output: Path = typer.Option(
        Path("exports") / "Contact_Directory.csv",
        "--output",
        help="Where to write the sender directory CSV.",
    ),
) -> None:
    """Scrape the mailbox to identify unique senders and message counts."""
    service = get_gmail_service()
    typer.echo(f"Scanning last {max_results} messages for unique senders...")

    ranked = discover_senders(service, max_results)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Sender", "Count"])
    writer.writerows(ranked)
    write_text_atomic(output, buffer.getvalue())

    typer.echo(f"{'Sender':<50} | Count")
    typer.echo("-" * 60)
    for sender, count in ranked[:20]:
        typer.echo(f"{sender[:50]:<50} | {count}")
    typer.echo(f"\nFull directory saved to {output}")


if __name__ == "__main__":
    app()
