"""Shared fixtures: a fake Gmail service and synthetic message payloads."""

from __future__ import annotations

import base64
from typing import Any

import pytest


def encode_body(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def make_payload(
    *,
    message_id: str = "msg-1",
    subject: str = "Security alert: meeting invite",
    sender: str = "Fictional Sender <sender@example.com>",
    date: str = "Mon, 21 Jul 2026 10:00:00 -0400",
    to: str = "recipient@example.com",
    extra_headers: list[dict[str, str]] | None = None,
    html: str = "<html><body><h1>Hello</h1>"
    "<img src='https://tracker.example/pixel.gif' width='1' height='1'>"
    "<a href='https://t.co/abc'>link</a></body></html>",
    plain: str = "Hello",
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
        {"name": "Date", "value": date},
        {"name": "To", "value": to},
    ]
    headers.extend(extra_headers or [])
    return {
        "id": message_id,
        "labelIds": label_ids or ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": encode_body(plain)},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": encode_body(html)},
                },
            ],
        },
    }


class _Request:
    def __init__(self, result: Any, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


class FakeMessages:
    """Records list/get/batchModify calls; pages are keyed by pageToken."""

    def __init__(
        self,
        pages: dict[str | None, dict[str, Any]] | None = None,
        messages: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.pages = pages or {None: {"messages": []}}
        self.messages = messages or {}
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.batch_modify_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Request:
        self.list_calls.append(kwargs)
        page = self.pages.get(kwargs.get("pageToken"))
        if page is None:
            page = {"messages": []}
        return _Request(page)

    def get(self, **kwargs: Any) -> _Request:
        self.get_calls.append(kwargs["id"])
        return _Request(self.messages[kwargs["id"]])

    def batchModify(self, **kwargs: Any) -> _Request:
        self.batch_modify_calls.append(kwargs)
        return _Request({})


class FakeLabels:
    def __init__(
        self,
        labels: list[dict[str, str]] | None = None,
        details: dict[str, dict[str, Any] | Exception] | None = None,
    ) -> None:
        self.labels = labels or []
        self.details = details or {}
        self.created: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    def list(self, **kwargs: Any) -> _Request:
        return _Request({"labels": self.labels})

    def get(self, **kwargs: Any) -> _Request:
        label_id = kwargs["id"]
        self.get_calls.append(label_id)
        detail = self.details.get(label_id)
        if isinstance(detail, Exception):
            return _Request(None, error=detail)
        if detail is not None:
            return _Request(detail)
        return _Request({"id": label_id, "name": label_id})

    def create(self, **kwargs: Any) -> _Request:
        body = kwargs["body"]
        created = {"id": "label-new", "name": body["name"]}
        self.created.append(body)
        self.labels.append(created)
        return _Request(created)


class FakeUsers:
    def __init__(self, messages: FakeMessages, labels: FakeLabels) -> None:
        self._messages = messages
        self._labels = labels

    def messages(self) -> FakeMessages:
        return self._messages

    def labels(self) -> FakeLabels:
        return self._labels


class FakeCalendarList:
    def __init__(self, calendars: list[dict[str, Any]] | None = None) -> None:
        self.calendars = calendars or []
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Request:
        self.list_calls.append(kwargs)
        return _Request({"items": self.calendars})


class FakeEvents:
    """Records events().list calls; pages are keyed by pageToken."""

    def __init__(self, pages: dict[str | None, dict[str, Any]] | None = None) -> None:
        self.pages = pages or {None: {"items": []}}
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Request:
        self.list_calls.append(kwargs)
        page = self.pages.get(kwargs.get("pageToken"))
        if page is None:
            page = {"items": []}
        items = list(page.get("items", []) or [])
        if kwargs.get("maxResults") is not None:
            items = items[: kwargs["maxResults"]]
        result = {"items": items}
        if page.get("nextPageToken"):
            result["nextPageToken"] = page["nextPageToken"]
        return _Request(result)


class FakeCalendarService:
    def __init__(
        self,
        calendars: list[dict[str, Any]] | None = None,
        event_pages: dict[str | None, dict[str, Any]] | None = None,
    ) -> None:
        self.calendar_list_api = FakeCalendarList(calendars)
        self.events_api = FakeEvents(event_pages)

    def calendarList(self) -> FakeCalendarList:
        return self.calendar_list_api

    def events(self) -> FakeEvents:
        return self.events_api


class FakeService:
    def __init__(
        self,
        pages: dict[str | None, dict[str, Any]] | None = None,
        messages: dict[str, dict[str, Any]] | None = None,
        labels: list[dict[str, str]] | None = None,
        label_details: dict[str, dict[str, Any] | Exception] | None = None,
    ) -> None:
        self.messages_api = FakeMessages(pages, messages)
        self.labels_api = FakeLabels(labels, label_details)

    def users(self) -> FakeUsers:
        return FakeUsers(self.messages_api, self.labels_api)


@pytest.fixture
def payload() -> dict[str, Any]:
    return make_payload()


@pytest.fixture
def fake_service(payload: dict[str, Any]) -> FakeService:
    return FakeService(
        pages={None: {"messages": [{"id": "msg-1"}]}},
        messages={"msg-1": payload},
    )


@pytest.fixture
def fake_calendar_service() -> FakeCalendarService:
    return FakeCalendarService(
        calendars=[
            {"id": "primary", "summary": "Fictional Calendar"},
            {"id": "team@example.com", "summary": "Team Calendar"},
        ],
        event_pages={
            None: {
                "items": [
                    {
                        "summary": "Fictional Standup",
                        "start": {"dateTime": "2026-10-28T09:00:00-04:00"},
                        "end": {"dateTime": "2026-10-28T09:30:00-04:00"},
                        "description": "Daily sync",
                        "location": "Room 1",
                        "status": "confirmed",
                        "creator": {"email": "organizer@example.com"},
                        "organizer": {"email": "organizer@example.com"},
                        "attendees": [{"email": "attendee@example.com"}],
                    },
                    {
                        "summary": "All-day Fictional Holiday",
                        "start": {"date": "2026-10-29"},
                        "end": {"date": "2026-10-30"},
                    },
                ],
                "nextPageToken": "p2",
            },
            "p2": {"items": []},
        },
    )
