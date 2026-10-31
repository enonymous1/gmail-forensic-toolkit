"""Tests for pagination, retries, labels, and batchModify."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from gmail_toolkit.gmail_api import (
    execute_with_retry,
    get_or_create_label,
    list_message_ids,
    modify_messages,
)
from tests.conftest import FakeService


def _http_error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason="error"), b"error")


def test_list_message_ids_pages_and_truncates():
    service = FakeService(
        pages={
            None: {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
            "p2": {"messages": [{"id": "c"}]},
        }
    )
    assert list_message_ids(service) == ["a", "b", "c"]
    assert list_message_ids(service, max_results=2) == ["a", "b"]
    assert list_message_ids(service, max_results=0) == []


def test_list_message_ids_truncates_oversized_page_and_stops_on_repeat():
    # A page larger than the budget must not overshoot max_results, and a
    # page token that repeats must end the listing instead of looping.
    oversized = FakeService(
        pages={None: {"messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}}
    )
    assert list_message_ids(oversized, max_results=2) == ["a", "b"]
    looping = FakeService(
        pages={
            None: {"messages": [{"id": "a"}], "nextPageToken": "p"},
            "p": {"messages": [{"id": "b"}], "nextPageToken": "p"},
        }
    )
    assert list_message_ids(looping) == ["a", "b"]


def test_execute_with_retry_retries_transient_then_succeeds():
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _http_error(503)
        return "ok"

    assert execute_with_retry(flaky, sleep=lambda _seconds: None) == "ok"
    assert attempts["count"] == 3


def test_execute_with_retry_does_not_retry_client_errors():
    def failing():
        raise _http_error(404)

    with pytest.raises(HttpError):
        execute_with_retry(failing, sleep=lambda _seconds: None)


def test_get_or_create_label_reuses_existing_and_creates_missing():
    service = FakeService(labels=[{"id": "label-1", "name": "Archive-Brands/X"}])
    assert get_or_create_label(service, "archive-brands/x") == "label-1"
    assert service.labels_api.created == []
    assert get_or_create_label(service, "Archive-Brands/Y") == "label-new"
    assert service.labels_api.created[0]["name"] == "Archive-Brands/Y"


def test_modify_messages_chunks_and_counts():
    service = FakeService()
    ids = [f"msg-{index}" for index in range(5)]
    assert modify_messages(service, ids, ["L1"], ["INBOX"], chunk_size=2) == 5
    assert [
        call["body"]["ids"] for call in service.messages_api.batch_modify_calls
    ] == [
        ["msg-0", "msg-1"],
        ["msg-2", "msg-3"],
        ["msg-4"],
    ]
    assert modify_messages(service, []) == 0
    assert service.messages_api.batch_modify_calls.__len__() == 3
