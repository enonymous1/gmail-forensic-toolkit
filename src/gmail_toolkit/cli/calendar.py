"""Export Google Calendar calendars and events as CSV.

Carried over from ``ss-gmail``'s Calendar utilities, which wrote CSVs to
hardcoded macOS paths and treated a missing Calendar service as an empty
result. Here the exports land under ``exports/calendar/`` in the working
directory, failures surface instead of vanishing, and fetching a whole
calendar follows every page. Read-only: the Calendar scope requested is
``calendar.readonly``.
"""

from __future__ import annotations

import csv
from pathlib import Path

import typer

from ..auth import get_calendar_service
from ..calendar_api import (
    calendar_row,
    event_row,
    list_calendars,
    list_events,
    list_upcoming_events,
)
from ..paths import ensure_directory, sanitize_filename

app = typer.Typer(help="Export Google Calendar calendars and events as CSV.")

_CALENDAR_FIELDS = ["Calendar Name", "Calendar ID"]
_EVENT_FIELDS = [
    "Event Name",
    "Start Time",
    "End Time",
    "Description",
    "Location",
    "Status",
    "Creator Email",
    "Organizer Email",
    "Attendees",
]
_UPCOMING_FIELDS = ["Event Name", "Start Time", "End Time", "Description"]


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    ensure_directory(path.parent)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@app.command()
def calendars(
    output: Path = typer.Option(
        Path("exports") / "calendar" / "calendars.csv",
        "--output",
        help="Where to write the calendars CSV.",
    ),
) -> None:
    """List every calendar and write the calendars CSV."""
    service = get_calendar_service()
    found = list_calendars(service)
    rows = [calendar_row(calendar) for calendar in found]
    _write_csv(output, _CALENDAR_FIELDS, rows)
    for row in rows:
        typer.echo(f"{row['Calendar Name']} ({row['Calendar ID']})")
    typer.echo(f"\n{len(rows)} calendar(s) saved to {output}")


@app.command()
def upcoming(
    calendar_id: str = typer.Option(
        "primary", "--calendar-id", help="Calendar ID to read events from."
    ),
    max_results: int = typer.Option(
        10, "--max-results", help="Maximum upcoming events to export."
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Where to write the upcoming-events CSV. Defaults to "
        "exports/calendar/events/<calendar-id>/events.csv.",
    ),
) -> None:
    """Export the next events on one calendar, from now onwards."""
    service = get_calendar_service()
    events = list_upcoming_events(service, calendar_id, max_results=max_results)
    rows = [
        {key: event_row(event)[key] for key in _UPCOMING_FIELDS} for event in events
    ]
    resolved_output = (
        output
        or Path("exports")
        / "calendar"
        / "events"
        / sanitize_filename(calendar_id)
        / "events.csv"
    )
    _write_csv(resolved_output, _UPCOMING_FIELDS, rows)
    for row in rows:
        typer.echo(f"{row['Event Name']} at {row['Start Time']}")
    typer.echo(f"\n{len(rows)} upcoming event(s) saved to {resolved_output}")


@app.command()
def events(
    calendar_id: str = typer.Option(
        "primary", "--calendar-id", help="Calendar ID to export (ignored with --all)."
    ),
    all_calendars: bool = typer.Option(
        False, "--all", help="Export every calendar instead of one."
    ),
    output_dir: Path = typer.Option(
        Path("exports") / "calendar" / "events",
        "--output-dir",
        help="Directory holding one events.csv per calendar.",
    ),
) -> None:
    """Export every event of one calendar, or of every calendar with --all."""
    service = get_calendar_service()
    if all_calendars:
        targets = [
            str(calendar.get("id", ""))
            for calendar in list_calendars(service)
            if calendar.get("id")
        ]
    else:
        targets = [calendar_id]

    for target in targets:
        found = list_events(service, target)
        rows = [event_row(event) for event in found]
        path = output_dir / sanitize_filename(target) / "events.csv"
        _write_csv(path, _EVENT_FIELDS, rows)
        typer.echo(f"{len(rows)} event(s) for calendar '{target}' saved to {path}")


if __name__ == "__main__":
    app()
