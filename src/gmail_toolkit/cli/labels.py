"""Export the Gmail label inventory as JSON.

Carried over from ``ss-gmail``'s label utilities, which wrote one JSON
file per label under a hardcoded macOS path and joined them with a
separate script. Here the list response and the per-label details are
both plain JSON under ``exports/labels/`` in the working directory, and
no sender, message, or calendar data is involved: this is read-only.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..auth import get_gmail_service
from ..gmail_api import fetch_label_details, list_labels
from ..paths import ensure_directory, sanitize_filename

app = typer.Typer(help="Export the Gmail label inventory as JSON.")

_LABEL_COLUMNS = ("Name", "ID", "Type", "Messages")


def _print_table(labels: list[dict], details: dict[str, dict] | None = None) -> None:
    typer.echo(f"{'Name':<40} | {'ID':<20} | {'Type':<10} | Messages")
    typer.echo("-" * 80)
    for label in labels:
        detail = (details or {}).get(str(label.get("id", "")), label)
        total = detail.get("messagesTotal")
        typer.echo(
            f"{str(label.get('name', ''))[:40]:<40} | "
            f"{str(label.get('id', ''))[:20]:<20} | "
            f"{str(label.get('type', ''))[:10]:<10} | "
            f"{total if total is not None else '-'}"
        )


@app.command()
def inventory(
    output: Path = typer.Option(
        Path("exports") / "labels" / "labels.json",
        "--output",
        help="Where to write the label list JSON.",
    ),
) -> None:
    """List every label and write the list response to JSON."""
    service = get_gmail_service()
    labels = list_labels(service)
    _print_table(labels)
    ensure_directory(output.parent)
    output.write_text(json.dumps(labels, indent=2), encoding="utf-8")
    typer.echo(f"\n{len(labels)} label(s) saved to {output}")


@app.command()
def details(
    output_dir: Path = typer.Option(
        Path("exports") / "labels" / "details",
        "--output-dir",
        help="Directory for one JSON file per label plus the joined file.",
    ),
) -> None:
    """Fetch every label's detail and write one JSON file per label.

    Also writes ``joined_label_details.json`` in the same directory: the
    combined array, matching the join step the old project ran separately.
    Labels whose detail request fails are skipped and counted.
    """
    service = get_gmail_service()
    labels = list_labels(service)
    ensure_directory(output_dir)

    fetched = fetch_label_details(service, labels)
    by_id = {str(detail.get("id", "")): detail for detail in fetched}
    for detail in fetched:
        filename = f"{sanitize_filename(str(detail.get('id', 'label')))}.json"
        (output_dir / filename).write_text(
            json.dumps(detail, indent=2), encoding="utf-8"
        )
    joined_path = output_dir / "joined_label_details.json"
    joined_path.write_text(json.dumps(fetched, indent=2), encoding="utf-8")

    _print_table(labels, by_id)
    skipped = len(labels) - len(fetched)
    typer.echo(
        f"\n{len(fetched)} label detail(s) saved to {output_dir}"
        + (f" ({skipped} skipped)" if skipped else "")
        + f"; joined file: {joined_path}"
    )


if __name__ == "__main__":
    app()
