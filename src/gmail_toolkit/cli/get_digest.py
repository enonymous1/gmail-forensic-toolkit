"""Summarise recent mailbox activity."""

from __future__ import annotations

import typer

from ..auth import get_gmail_service
from ..gmail_api import batch_get_message_metadata, list_message_ids
from ..process_email import headers_dict
from ..services import GmailService
from .validators import positive_int_callback

app = typer.Typer(help="Generate an email digest.")


def summarize_activity(
    service: GmailService, days: int, limit: int
) -> list[dict[str, str]]:
    """Return one ``{Date, From, Subject}`` row per recent message.

    Metadata fetches go through the batch helper, not one call per
    message. This is the testable core; the Typer command below only
    builds the service and prints the table.
    """
    query = f"newer_than:{days}d"
    message_ids = list_message_ids(service, query=query, max_results=limit)
    rows: list[dict[str, str]] = []
    for msg_data in batch_get_message_metadata(
        service, message_ids, ["From", "Subject", "Date"]
    ):
        headers = headers_dict(msg_data.get("payload", {}) or {})
        rows.append(
            {
                "Date": headers.get("date", "Unknown"),
                "From": headers.get("from", "Unknown"),
                "Subject": headers.get("subject", "(No Subject)"),
            }
        )
    return rows


@app.command()
def summarize(
    days: int = typer.Option(
        1,
        "--days",
        callback=positive_int_callback,
        help="Days of activity to summarise.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        callback=positive_int_callback,
        help="Maximum messages to show.",
    ),
) -> None:
    """Summarise recent mailbox activity using Gmail search."""
    service = get_gmail_service()
    typer.echo(f"Fetching digest for the last {days} day(s)...")

    rows = summarize_activity(service, days, limit)
    if not rows:
        typer.echo("No recent activity found.")
        return

    typer.echo(f"{'Date':<20} | {'From':<30} | {'Subject'}")
    typer.echo("-" * 80)
    for row in rows:
        typer.echo(
            f"{row['Date'][:20]:<20} | {row['From'][:30]:<30} | {row['Subject']}"
        )


if __name__ == "__main__":
    app()
