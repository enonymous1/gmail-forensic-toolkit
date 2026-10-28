"""Offline tests for the folded-in label inventory and Calendar export."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from types import SimpleNamespace

from googleapiclient.errors import HttpError
from typer.testing import CliRunner

from gmail_toolkit import auth, calendar_api
from gmail_toolkit.cli import calendar as calendar_module
from gmail_toolkit.cli import labels as labels_module
from gmail_toolkit.cli.main import app
from gmail_toolkit.gmail_api import (
    fetch_label_details,
    get_label,
    list_labels,
)
from tests.conftest import FakeCalendarService, FakeService

runner = CliRunner()


def _http_error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason="error"), b"error")


_LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "Label_1", "name": "Archive-Brands/Example", "type": "user"},
]

_DETAILS = {
    "INBOX": {
        "id": "INBOX",
        "name": "INBOX",
        "type": "system",
        "messagesTotal": 42,
        "messagesUnread": 3,
    },
    "Label_1": {
        "id": "Label_1",
        "name": "Archive-Brands/Example",
        "type": "user",
        "messagesTotal": 7,
        "messagesUnread": 0,
        "color": {"backgroundColor": "#fff", "textColor": "#000"},
    },
}


def _label_service() -> FakeService:
    return FakeService(labels=_LABELS, label_details=dict(_DETAILS))


def test_list_labels_and_get_label():
    service = _label_service()
    assert list_labels(service) == _LABELS
    assert get_label(service, "Label_1") == _DETAILS["Label_1"]
    assert service.labels_api.get_calls == ["Label_1"]


def test_fetch_label_details_skips_failed_label():
    service = FakeService(
        labels=_LABELS,
        label_details={"INBOX": _DETAILS["INBOX"], "Label_1": _http_error(404)},
    )
    assert fetch_label_details(service, _LABELS) == [_DETAILS["INBOX"]]


def test_labels_inventory_writes_json(monkeypatch, tmp_path):
    monkeypatch.setattr(labels_module, "get_gmail_service", lambda: _label_service())
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["labels", "inventory"])
    assert result.exit_code == 0, result.output
    assert "Archive-Brands/Example" in result.output
    saved = json.loads(
        (tmp_path / "exports" / "labels" / "labels.json").read_text(encoding="utf-8")
    )
    assert saved == _LABELS


def test_labels_details_writes_per_label_and_joined(monkeypatch, tmp_path):
    service = FakeService(
        labels=_LABELS,
        label_details={"INBOX": _DETAILS["INBOX"], "Label_1": _http_error(404)},
    )
    monkeypatch.setattr(labels_module, "get_gmail_service", lambda: service)
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["labels", "details"])
    assert result.exit_code == 0, result.output
    assert "1 skipped" in result.output
    details_dir = tmp_path / "exports" / "labels" / "details"
    assert (
        json.loads((details_dir / "INBOX.json").read_text(encoding="utf-8"))
        == (_DETAILS["INBOX"])
    )
    assert not (details_dir / "Label_1.json").exists()
    joined = json.loads(
        (details_dir / "joined_label_details.json").read_text(encoding="utf-8")
    )
    assert joined == [_DETAILS["INBOX"]]


def test_list_calendars(fake_calendar_service):
    calendars = calendar_api.list_calendars(fake_calendar_service)
    assert [calendar["id"] for calendar in calendars] == [
        "primary",
        "team@example.com",
    ]


def test_list_events_pages_and_truncates(fake_calendar_service):
    events = calendar_api.list_events(fake_calendar_service, "primary")
    assert [event["summary"] for event in events] == [
        "Fictional Standup",
        "All-day Fictional Holiday",
    ]
    assert (
        calendar_api.list_events(fake_calendar_service, "primary", max_results=1)[0][
            "summary"
        ]
        == "Fictional Standup"
    )
    assert (
        calendar_api.list_events(fake_calendar_service, "primary", max_results=0) == []
    )


def test_list_upcoming_events_bounds_from_now(fake_calendar_service):
    calendar_api.list_upcoming_events(fake_calendar_service, "primary", 1)
    call = fake_calendar_service.events_api.list_calls[-1]
    assert call["calendarId"] == "primary"
    assert call["singleEvents"] is True
    # A parseable RFC 3339 timestamp: the bound is "now", not the past.
    assert datetime.fromisoformat(call["timeMin"])


def test_event_row_handles_all_day_and_missing_fields():
    row = calendar_api.event_row(
        {"summary": "All-day Fictional Holiday", "start": {"date": "2026-10-29"}}
    )
    assert row["Start Time"] == "2026-10-29"
    assert row["End Time"] == ""
    assert row["Attendees"] == ""
    assert row["Creator Email"] == ""


def test_event_row_flattens_full_event(fake_calendar_service):
    event = calendar_api.list_events(fake_calendar_service, "primary")[0]
    row = calendar_api.event_row(event)
    assert row["Event Name"] == "Fictional Standup"
    assert row["Start Time"] == "2026-10-28T09:00:00-04:00"
    assert row["Attendees"] == "attendee@example.com"
    assert row["Creator Email"] == "organizer@example.com"


def test_calendar_command_writes_calendars_csv(
    monkeypatch, tmp_path, fake_calendar_service
):
    monkeypatch.setattr(
        calendar_module, "get_calendar_service", lambda: fake_calendar_service
    )
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["calendar", "calendars"])
    assert result.exit_code == 0, result.output
    rows = list(
        csv.DictReader((tmp_path / "exports" / "calendar" / "calendars.csv").open())
    )
    assert rows == [
        {"Calendar Name": "Fictional Calendar", "Calendar ID": "primary"},
        {"Calendar Name": "Team Calendar", "Calendar ID": "team@example.com"},
    ]


def test_calendar_upcoming_writes_events_csv(
    monkeypatch, tmp_path, fake_calendar_service
):
    monkeypatch.setattr(
        calendar_module, "get_calendar_service", lambda: fake_calendar_service
    )
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["calendar", "upcoming", "--max-results", "1"])
    assert result.exit_code == 0, result.output
    rows = list(
        csv.DictReader(
            (
                tmp_path / "exports" / "calendar" / "events" / "primary" / "events.csv"
            ).open()
        )
    )
    assert rows == [
        {
            "Event Name": "Fictional Standup",
            "Start Time": "2026-10-28T09:00:00-04:00",
            "End Time": "2026-10-28T09:30:00-04:00",
            "Description": "Daily sync",
        }
    ]


def test_calendar_events_all_writes_one_csv_per_calendar(
    monkeypatch, tmp_path, fake_calendar_service
):
    monkeypatch.setattr(
        calendar_module, "get_calendar_service", lambda: fake_calendar_service
    )
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["calendar", "events", "--all"])
    assert result.exit_code == 0, result.output
    base = tmp_path / "exports" / "calendar" / "events"
    assert (base / "primary" / "events.csv").exists()
    assert (base / "team@example.com" / "events.csv").exists()


def test_get_calendar_service_missing_client_secrets_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        auth,
        "credential_paths",
        lambda *args, **kwargs: (tmp_path / "c.json", tmp_path / "t.json"),
    )
    try:
        auth.get_calendar_service()
    except FileNotFoundError as exc:
        assert "Missing client secrets" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_calendar_service_with_no_events_writes_header_only(monkeypatch, tmp_path):
    empty = FakeCalendarService(
        calendars=[{"id": "primary", "summary": "Empty Calendar"}],
        event_pages={None: {"items": []}},
    )
    monkeypatch.setattr(calendar_module, "get_calendar_service", lambda: empty)
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["calendar", "events"])
    assert result.exit_code == 0, result.output
    rows = list(
        csv.DictReader(
            (
                tmp_path / "exports" / "calendar" / "events" / "primary" / "events.csv"
            ).open()
        )
    )
    assert rows == []
