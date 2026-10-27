"""Summarise recent mailbox activity."""

from __future__ import annotations

import typer

from ..auth import get_gmail_service
from ..gmail_api import get_message_metadata, list_message_ids

app = typer.Typer(help="Generate an email digest.")


@app.command()
def summarize(
    days: int = typer.Option(1, "--days", help="Days of activity to summarise."),
    limit: int = typer.Option(20, "--limit", help="Maximum messages to show."),
) -> None:
    """Summarise recent mailbox activity using Gmail search."""
    service = get_gmail_service()
    query = f"newer_than:{days}d"

    typer.echo(f"Fetching digest for the last {days} day(s)...")

    message_ids = list_message_ids(service, query=query, max_results=limit)
    if not message_ids:
        typer.echo("No recent activity found.")
        return

    typer.echo(f"{'Date':<20} | {'From':<30} | {'Subject'}")
    typer.echo("-" * 80)

    for message_id in message_ids:
        msg_data = get_message_metadata(
            service, message_id, ["From", "Subject", "Date"]
        )
        headers = {
            str(header.get("name", "")).lower(): str(header.get("value", ""))
            for header in (msg_data.get("payload", {}) or {}).get("headers", []) or []
        }
        date = headers.get("date", "Unknown")
        sender = headers.get("from", "Unknown")[:30]
        subject = headers.get("subject", "(No Subject)")
        typer.echo(f"{date[:20]:<20} | {sender:<30} | {subject}")


if __name__ == "__main__":
    app()
