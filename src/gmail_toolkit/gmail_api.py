"""Thin, paginated, retrying wrappers over the Gmail API.

The original commands called ``messages().list()`` once and processed only
the first page, and made one ``get`` / ``modify`` call per message with no
retry. These helpers page through ``nextPageToken``, retry transient
(429 / 5xx) failures with backoff, and use ``batchModify`` for labelling so
organising a sender is one call per 1,000 messages instead of one per message.

Only the shapes the Gmail API actually returns are assumed here: ``list``
responses carry ``messages`` and optionally ``nextPageToken``; a message
carries ``id``. Anything else is treated as absent, never as an error.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any, TypeVar

from googleapiclient.errors import HttpError

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_DEFAULT_PAGE_SIZE = 500
_BATCH_MODIFY_CHUNK = 1000

T = TypeVar("T")


def execute_with_retry(
    execute: Callable[[], T],
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Execute *execute*, retrying transient Gmail failures with backoff."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return execute()
        except HttpError as exc:
            status = getattr(exc, "resp", None)
            status_code = getattr(status, "status", None)
            if status_code in RETRYABLE_STATUSES and attempt < max_attempts:
                sleep(base_delay_seconds * (2 ** (attempt - 1)))
                continue
            raise


def _list_messages(
    service: Any, query: str | None, request_size: int, page_token: str | None
) -> Any:
    return (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=request_size, pageToken=page_token)
        .execute()
    )


def list_message_ids(
    service: Any,
    query: str | None = None,
    max_results: int | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> list[str]:
    """Return message IDs for *query*, following every page of results."""
    if max_results is not None and max_results <= 0:
        return []
    message_ids: list[str] = []
    page_token: str | None = None
    while True:
        remaining = None
        if max_results is not None:
            remaining = max_results - len(message_ids)
            if remaining <= 0:
                break
        request_size = page_size if remaining is None else min(page_size, remaining)
        response = execute_with_retry(
            partial(_list_messages, service, query, request_size, page_token)
        )
        for message in response.get("messages", []) or []:
            message_id = message.get("id")
            if message_id:
                message_ids.append(message_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return message_ids


def _get_message(service: Any, message_id: str, message_format: str) -> Any:
    return (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format=message_format)
        .execute()
    )


def get_message(service: Any, message_id: str, message_format: str = "full") -> Any:
    """Fetch one message, retrying transient failures."""
    return execute_with_retry(
        partial(_get_message, service, message_id, message_format)
    )


def _get_metadata(service: Any, message_id: str, headers: list[str]) -> Any:
    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=headers,
        )
        .execute()
    )


def get_message_metadata(
    service: Any, message_id: str, metadata_headers: Iterable[str]
) -> Any:
    """Fetch one message's metadata headers, retrying transient failures."""
    headers = list(metadata_headers)
    return execute_with_retry(partial(_get_metadata, service, message_id, headers))


def _get_label_page(service: Any, page_token: str | None) -> Any:
    return service.users().labels().list(userId="me", pageToken=page_token).execute()


def list_labels(service: Any) -> list[dict[str, Any]]:
    """Return every label, following every page of the list response.

    The list response carries only each label's ``id`` and ``name``; use
    :func:`get_label` for the full detail (counts, visibility, colour).
    """
    labels: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        response = execute_with_retry(partial(_get_label_page, service, page_token))
        labels.extend(response.get("labels", []) or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return labels


def _fetch_label(service: Any, label_id: str) -> Any:
    return service.users().labels().get(userId="me", id=label_id).execute()


def get_label(service: Any, label_id: str) -> dict[str, Any]:
    """Fetch one label's full detail, retrying transient failures."""
    return execute_with_retry(partial(_fetch_label, service, label_id))


def fetch_label_details(
    service: Any, labels: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fetch the detail of every label in *labels*, skipping failures.

    A label whose detail request fails even after retries (for example a
    label deleted between the list and the get) is skipped rather than
    aborting the whole inventory: the caller can compare the result length
    with the input length to see how many were skipped.
    """
    details: list[dict[str, Any]] = []
    for label in labels:
        label_id = label.get("id")
        if not label_id:
            continue
        try:
            details.append(get_label(service, str(label_id)))
        except HttpError:
            continue
    return details


def _list_labels(service: Any, page_token: str | None) -> Any:
    return service.users().labels().list(userId="me", pageToken=page_token).execute()


def _create_label(service: Any, body: dict[str, Any]) -> Any:
    return service.users().labels().create(userId="me", body=body).execute()


def get_or_create_label(service: Any, label_name: str) -> str:
    """Return the ID of *label_name*, creating the label if it is missing."""
    page_token: str | None = None
    while True:
        response = execute_with_retry(partial(_list_labels, service, page_token))
        for label in response.get("labels", []) or []:
            if str(label.get("name", "")).lower() == label_name.lower():
                return str(label["id"])
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    body = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = execute_with_retry(partial(_create_label, service, body))
    return str(created["id"])


def _batch_modify(service: Any, chunk_body: dict[str, Any]) -> Any:
    return (
        service.users().messages().batchModify(userId="me", body=chunk_body).execute()
    )


def modify_messages(
    service: Any,
    message_ids: Iterable[str],
    add_label_ids: Iterable[str] | None = None,
    remove_label_ids: Iterable[str] | None = None,
    chunk_size: int = _BATCH_MODIFY_CHUNK,
) -> int:
    """Apply label changes with ``batchModify``; return messages modified."""
    ids = list(message_ids)
    if not ids:
        return 0
    body = {
        "ids": [],
        "addLabelIds": list(add_label_ids or []),
        "removeLabelIds": list(remove_label_ids or []),
    }
    modified = 0
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        chunk_body = dict(body, ids=chunk)
        execute_with_retry(partial(_batch_modify, service, chunk_body))
        modified += len(chunk)
    return modified
