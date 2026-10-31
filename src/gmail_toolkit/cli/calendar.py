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
import io
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
from ..paths import sanitize_filename, write_text_atomic
from ..services import CalendarService
from .validators import positive_int_callback

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
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    write_text_atomic(path, buffer.getvalue())


def export_calendars(service: CalendarService, output: Path) -> int:
    """Write the calendars CSV to *output*; return the calendar count."""
    rows = [calendar_row(calendar) for calendar in list_calendars(service)]
    _write_csv(output, _CALENDAR_FIELDS, rows)
    for row in rows:
        typer.echo(f"{row['Calendar Name']} ({row['Calendar ID']})")
    typer.echo(f"\n{len(rows)} calendar(s) saved to {output}")
    return len(rows)


def export_upcoming(
    service: CalendarService,
    calendar_id: str,
    max_results: int,
    output: Path | None,
) -> tuple[Path, int]:
    """Export the next events on one calendar, from now onwards.

    Without *output*, the path is derived from the calendar id:
    ``exports/calendar/events/<calendar-id>/events.csv``.
    """
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
    return resolved_output, len(rows)


def export_events(
    service: CalendarService,
    calendar_id: str,
    all_calendars: bool,
    output_dir: Path,
) -> list[Path]:
    """Export every event of one calendar, or of every calendar with *all_calendars*."""
    if all_calendars:
        targets = [
            str(calendar.get("id", ""))
            for calendar in list_calendars(service)
            if calendar.get("id")
        ]
    else:
        targets = [calendar_id]

    paths: list[Path] = []
    for target in targets:
        found = list_events(service, target)
        rows = [event_row(event) for event in found]
        path = output_dir / sanitize_filename(target) / "events.csv"
        _write_csv(path, _EVENT_FIELDS, rows)
        typer.echo(f"{len(rows)} event(s) for calendar '{target}' saved to {path}")
        paths.append(path)
    return paths


@app.command()
def calendars(
    output: Path = typer.Option(
        Path("exports") / "calendar" / "calendars.csv",
        "--output",
        help="Where to write the calendars CSV.",
    ),
) -> None:
    """List every calendar and write the calendars CSV."""
    export_calendars(get_calendar_service(), output)


@app.command()
def upcoming(
    calendar_id: str = typer.Option(
        "primary", "--calendar-id", help="Calendar ID to read events from."
    ),
    max_results: int = typer.Option(
        10,
        "--max-results",
        callback=positive_int_callback,
        help="Maximum upcoming events to export.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Where to write the upcoming-events CSV. Defaults to "
        "exports/calendar/events/<calendar-id>/events.csv.",
    ),
) -> None:
    """Export the next events on one calendar, from now onwards."""
    export_upcoming(get_calendar_service(), calendar_id, max_results, output)


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
    export_events(get_calendar_service(), calendar_id, all_calendars, output_dir)


if __name__ == "__main__":
    app()
