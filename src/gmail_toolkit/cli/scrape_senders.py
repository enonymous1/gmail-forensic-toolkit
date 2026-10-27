"""Discover unique senders and message counts."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import typer

from ..auth import get_gmail_service
from ..gmail_api import get_message_metadata, list_message_ids

app = typer.Typer(help="Discover unique senders and message counts.")


@app.command()
def discovery(
    max_results: int = typer.Option(
        500, "--max-results", help="Maximum messages to scan."
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

    message_ids = list_message_ids(service, max_results=max_results)
    senders: list[str] = []
    for message_id in message_ids:
        msg_data = get_message_metadata(service, message_id, ["From"])
        for header in (msg_data.get("payload", {}) or {}).get("headers", []) or []:
            if header.get("name") == "From":
                senders.append(str(header.get("value", "")))

    counts = Counter(senders)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Sender", "Count"])
        writer.writerows(ranked)

    typer.echo(f"{'Sender':<50} | Count")
    typer.echo("-" * 60)
    for sender, count in ranked[:20]:
        typer.echo(f"{sender[:50]:<50} | {count}")
    typer.echo(f"\nFull directory saved to {output}")


if __name__ == "__main__":
    app()
