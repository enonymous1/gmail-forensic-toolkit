"""Thin, paginated, retrying wrappers over the Google Calendar API.

Carried over from the ``ss-gmail`` project's ``google_calendar.py``, with
its defects fixed: that module authenticated separately (its own token
file), wrote to hardcoded ``/Users/...`` paths, created its output
directory at import time, swallowed every failure by returning ``None``,
and fetched upcoming events without a ``timeMin`` bound. Here auth is
shared with Gmail (see :mod:`gmail_toolkit.auth`), paths are caller-chosen,
and failures surface as exceptions after the same retry policy the Gmail
wrappers use.

Only the shapes the Calendar API actually returns are assumed: a
calendar-list response carries ``items``; an event carries ``summary``,
``start`` / ``end`` dicts with ``dateTime`` or ``date``, and optionally
``description``, ``location``, ``status``, ``creator``, ``organizer`` and
``attendees``. Anything else is treated as absent, never as an error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from typing import Any

from .gmail_api import execute_with_retry, iter_pages

_DEFAULT_PAGE_SIZE = 250


def _list_calendars(service: Any) -> Any:
    return service.calendarList().list().execute()


def list_calendars(service: Any) -> list[dict[str, Any]]:
    """Return every calendar on the calendar list."""
    response = execute_with_retry(partial(_list_calendars, service))
    return list(response.get("items", []) or [])


def _list_events_page(
    service: Any,
    calendar_id: str,
    page_token: str | None,
    request_size: int,
    time_min: str | None,
) -> Any:
    kwargs: dict[str, Any] = {
        "calendarId": calendar_id,
        "pageToken": page_token,
        "maxResults": request_size,
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if time_min is not None:
        kwargs["timeMin"] = time_min
    return service.events().list(**kwargs).execute()


def list_events(
    service: Any,
    calendar_id: str,
    max_results: int | None = None,
    time_min: str | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Return events for *calendar_id*, following every page of results.

    With *time_min* set, only events starting at or after it are returned.
    Without it, the whole event history is returned, oldest first.
    """
    if max_results is not None and max_results <= 0:
        return []
    events: list[dict[str, Any]] = []
    for page in iter_pages(
        lambda size, token: _list_events_page(
            service, calendar_id, token, size, time_min
        ),
        "items",
        max_results=max_results,
        page_size=page_size,
    ):
        events.extend(page)
    return events


def list_upcoming_events(
    service: Any,
    calendar_id: str = "primary",
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Return the next *max_results* events from now on *calendar_id*.

    The original code ordered by start time with no lower bound, so a
    mailbox whose primary calendar holds old events could return the past
    instead of the future. ``timeMin`` is now the current UTC time.
    """
    time_min = datetime.now(timezone.utc).isoformat()
    return list_events(service, calendar_id, max_results=max_results, time_min=time_min)


def _event_when(event: dict[str, Any], key: str) -> str:
    when = event.get(key) or {}
    return str(when.get("dateTime") or when.get("date") or "")


def event_row(event: dict[str, Any]) -> dict[str, str]:
    """Flatten one Calendar event into the full CSV row shape."""
    creator = event.get("creator") or {}
    organizer = event.get("organizer") or {}
    attendees = ", ".join(
        str(attendee.get("email", ""))
        for attendee in event.get("attendees", []) or []
        if attendee.get("email")
    )
    return {
        "Event Name": str(event.get("summary") or ""),
        "Start Time": _event_when(event, "start"),
        "End Time": _event_when(event, "end"),
        "Description": str(event.get("description") or ""),
        "Location": str(event.get("location") or ""),
        "Status": str(event.get("status") or ""),
        "Creator Email": str(creator.get("email") or ""),
        "Organizer Email": str(organizer.get("email") or ""),
        "Attendees": attendees,
    }


def calendar_row(calendar: dict[str, Any]) -> dict[str, str]:
    """Flatten one calendar-list entry into the calendars CSV row shape."""
    return {
        "Calendar Name": str(calendar.get("summary") or ""),
        "Calendar ID": str(calendar.get("id") or ""),
    }
