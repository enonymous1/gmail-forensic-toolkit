"""Structural types for the Google API services.

The googleapiclient client is dynamically built, so its real type is
opaque to mypy and every wrapper used to take ``service: Any`` — which
made the type checker pass vacuously on the API boundary. These
``Protocol`` classes describe only the surface the toolkit actually calls:
the chained ``users().messages().list/get/batchModify`` and
``users().labels().list/get/create`` calls for Gmail, ``calendarList`` /
``events`` for Calendar, and the batch-request builder both services share.

They are structural, not nominal: the real discovery client and the test
fakes both satisfy them without inheriting anything, and mypy now checks
call sites against these shapes instead of ``Any``.
"""

from __future__ import annotations

from typing import Any, Protocol


class HttpRequest(Protocol):
    """A single Google API request, executed for its response."""

    def execute(self) -> Any: ...


class BatchRequest(Protocol):
    """A Gmail/Calendar batch request: add requests, then execute once."""

    def add(
        self,
        request: HttpRequest,
        callback: Any = None,
        request_id: str | None = None,
    ) -> None: ...

    def execute(self) -> Any: ...


class GmailMessages(Protocol):
    def list(self, **kwargs: Any) -> HttpRequest: ...
    def get(self, **kwargs: Any) -> HttpRequest: ...
    def batchModify(self, **kwargs: Any) -> HttpRequest: ...


class GmailLabels(Protocol):
    def list(self, **kwargs: Any) -> HttpRequest: ...
    def get(self, **kwargs: Any) -> HttpRequest: ...
    def create(self, **kwargs: Any) -> HttpRequest: ...


class GmailUsers(Protocol):
    def messages(self) -> GmailMessages: ...
    def labels(self) -> GmailLabels: ...


class GmailService(Protocol):
    """The Gmail API surface the toolkit calls."""

    def users(self) -> GmailUsers: ...

    def new_batch_http_request(self, callback: Any = None) -> BatchRequest: ...


class CalendarList(Protocol):
    def list(self, **kwargs: Any) -> HttpRequest: ...


class CalendarEvents(Protocol):
    def list(self, **kwargs: Any) -> HttpRequest: ...


class CalendarService(Protocol):
    """The Calendar API surface the toolkit calls."""

    def calendarList(self) -> CalendarList: ...
    def events(self) -> CalendarEvents: ...
