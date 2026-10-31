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

import logging
import random
import time
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any, TypeVar

from googleapiclient.errors import HttpError

from .services import GmailService
from .settings import DEFAULT_SETTINGS, Settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_DEFAULT_PAGE_SIZE = DEFAULT_SETTINGS.gmail_page_size

T = TypeVar("T")


def execute_with_retry(
    execute: Callable[[], T],
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    settings: Settings | None = None,
) -> T:
    """Execute *execute*, retrying transient Gmail failures with backoff.

    The delay doubles each attempt, plus a small random jitter (up to 25%
    of the delay). Without jitter, every client that hit the same quota
    wall retries in lockstep and hits it again together; the jitter
    spreads retries out. When *settings* is given it supplies the attempt
    budget and base delay, overriding the explicit arguments.
    """
    if settings is not None:
        max_attempts = settings.max_attempts
        base_delay_seconds = settings.retry_base_delay_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            return execute()
        except HttpError as exc:
            status = getattr(exc, "resp", None)
            status_code = getattr(status, "status", None)
            if status_code in RETRYABLE_STATUSES and attempt < max_attempts:
                delay = base_delay_seconds * (2 ** (attempt - 1))
                jittered = delay + random.uniform(0, delay * 0.25)
                logger.warning(
                    "Transient Gmail error %s; retrying in %.2fs (attempt %s/%s)",
                    status_code,
                    jittered,
                    attempt,
                    max_attempts,
                )
                sleep(jittered)
                continue
            raise


def _list_messages(
    service: GmailService,
    query: str | None,
    request_size: int,
    page_token: str | None,
) -> Any:
    return (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=request_size, pageToken=page_token)
        .execute()
    )


def iter_pages(
    fetch_page: Callable[[int, str | None], Any],
    items_key: str,
    max_results: int | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
    settings: Settings | None = None,
) -> Iterable[list[Any]]:
    """Yield one page of items at a time, following ``nextPageToken``.

    A page token that repeats means the server is not advancing, so it
    ends the iteration instead of looping forever. A page larger than the
    remaining budget is truncated, so callers never get more than
    *max_results* items even if the server ignores the requested size.
    """
    seen_tokens: set[str | None] = set()
    page_token: str | None = None
    yielded = 0
    while True:
        if page_token in seen_tokens:
            break
        seen_tokens.add(page_token)
        remaining = None if max_results is None else max_results - yielded
        if remaining is not None and remaining <= 0:
            break
        request_size = page_size if remaining is None else min(page_size, remaining)
        response = execute_with_retry(
            partial(fetch_page, request_size, page_token), settings=settings
        )
        items = response.get(items_key, []) or []
        if remaining is not None:
            items = items[:remaining]
        yield items
        yielded += len(items)
        page_token = response.get("nextPageToken")
        if not page_token:
            break


def list_message_ids(
    service: GmailService,
    query: str | None = None,
    max_results: int | None = None,
    page_size: int | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Return message IDs for *query*, following every page of results."""
    if max_results is not None and max_results <= 0:
        return []
    effective_page_size = (
        page_size
        if page_size is not None
        else (settings or DEFAULT_SETTINGS).gmail_page_size
    )
    message_ids: list[str] = []
    for page in iter_pages(
        partial(_list_messages, service, query),
        "messages",
        max_results=max_results,
        page_size=effective_page_size,
        settings=settings,
    ):
        for message in page:
            message_id = message.get("id")
            if message_id:
                message_ids.append(message_id)
    return message_ids


def _message_request(
    service: GmailService, message_id: str, message_format: str
) -> Any:
    return (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format=message_format)
    )


def _get_message(service: GmailService, message_id: str, message_format: str) -> Any:
    return _message_request(service, message_id, message_format).execute()


def get_message(
    service: GmailService,
    message_id: str,
    message_format: str = "full",
    settings: Settings | None = None,
) -> Any:
    """Fetch one message, retrying transient failures."""
    return execute_with_retry(
        partial(_get_message, service, message_id, message_format),
        settings=settings,
    )


def _metadata_request(
    service: GmailService, message_id: str, headers: list[str]
) -> Any:
    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=headers,
        )
    )


def _get_metadata(service: GmailService, message_id: str, headers: list[str]) -> Any:
    return _metadata_request(service, message_id, headers).execute()


def get_message_metadata(
    service: GmailService,
    message_id: str,
    metadata_headers: Iterable[str],
    settings: Settings | None = None,
) -> Any:
    """Fetch one message's metadata headers, retrying transient failures."""
    headers = list(metadata_headers)
    return execute_with_retry(
        partial(_get_metadata, service, message_id, headers), settings=settings
    )


def _batch_execute(
    service: GmailService, requests: list[Any], settings: Settings | None = None
) -> list[tuple[Any, Exception | None]]:
    """Run *requests* through the service's batch support, in order.

    Returns ``(response, exception)`` pairs in request order, so callers
    decide per request whether an error aborts or is skipped. The batch
    call itself is retried on transient failures; a per-request failure
    inside an otherwise successful batch is *not* individually retried —
    the caller sees it as that request's exception. Services without
    batch support (older test fakes) fall back to sequential executes,
    each retried the usual way.
    """
    builder = getattr(service, "new_batch_http_request", None)
    if builder is None:
        results: list[tuple[Any, Exception | None]] = []
        for request in requests:
            try:
                results.append(
                    (execute_with_retry(request.execute, settings=settings), None)
                )
            except Exception as exc:  # noqa: BLE001 - reported per request
                results.append((None, exc))
        return results

    collected: dict[str, tuple[Any, Exception | None]] = {}

    def _callback(request_id: str, response: Any, exception: Exception | None) -> None:
        collected[str(request_id)] = (response, exception)

    batch = builder()
    for index, request in enumerate(requests):
        batch.add(request, callback=_callback, request_id=str(index))
    execute_with_retry(batch.execute, settings=settings)
    return [
        collected.get(
            str(index), (None, RuntimeError("Batch request produced no result"))
        )
        for index in range(len(requests))
    ]


def _chunked_batch(
    service: GmailService,
    requests: list[Any],
    settings: Settings | None = None,
) -> list[tuple[Any, Exception | None]]:
    """Run *requests* in batches of ``Settings.batch_request_size``."""
    chunk_size = (settings or DEFAULT_SETTINGS).batch_request_size
    results: list[tuple[Any, Exception | None]] = []
    for start in range(0, len(requests), chunk_size):
        results.extend(
            _batch_execute(service, requests[start : start + chunk_size], settings)
        )
    return results


def batch_get_messages(
    service: GmailService,
    message_ids: Iterable[str],
    message_format: str = "full",
    settings: Settings | None = None,
) -> list[Any]:
    """Fetch many messages with batch requests, in the given order.

    This replaces one HTTP call per message (the old N+1 pattern in
    scrape, digest, and archive) with one batch call per
    ``Settings.batch_request_size`` messages. The first per-request error
    aborts, matching what sequential ``get_message`` calls did.
    """
    ids = list(message_ids)
    requests = [_message_request(service, mid, message_format) for mid in ids]
    responses: list[Any] = []
    for response, exc in _chunked_batch(service, requests, settings):
        if exc is not None:
            raise exc
        responses.append(response)
    return responses


def batch_get_message_metadata(
    service: GmailService,
    message_ids: Iterable[str],
    metadata_headers: Iterable[str],
    settings: Settings | None = None,
) -> list[Any]:
    """Fetch many messages' metadata headers with batch requests, in order."""
    headers = list(metadata_headers)
    ids = list(message_ids)
    requests = [_metadata_request(service, mid, headers) for mid in ids]
    responses: list[Any] = []
    for response, exc in _chunked_batch(service, requests, settings):
        if exc is not None:
            raise exc
        responses.append(response)
    return responses


def _get_label_page(
    service: GmailService, _request_size: int, page_token: str | None
) -> Any:
    # The labels list takes no page size; the parameter only exists so this
    # fits the shared pagination helper's fetch signature.
    return service.users().labels().list(userId="me", pageToken=page_token).execute()


def list_labels(
    service: GmailService, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Return every label, following every page of the list response.

    The list response carries only each label's ``id`` and ``name``; use
    :func:`get_label` for the full detail (counts, visibility, colour).
    """
    labels: list[dict[str, Any]] = []
    for page in iter_pages(
        partial(_get_label_page, service), "labels", settings=settings
    ):
        labels.extend(page)
    return labels


def _fetch_label(service: GmailService, label_id: str) -> Any:
    return service.users().labels().get(userId="me", id=label_id).execute()


def get_label(
    service: GmailService, label_id: str, settings: Settings | None = None
) -> dict[str, Any]:
    """Fetch one label's full detail, retrying transient failures."""
    return execute_with_retry(
        partial(_fetch_label, service, label_id), settings=settings
    )


def fetch_label_details(
    service: GmailService,
    labels: Iterable[dict[str, Any]],
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Fetch the detail of every label in *labels*, skipping failures.

    A label whose detail request fails even after retries (for example a
    label deleted between the list and the get) is skipped rather than
    aborting the whole inventory: the caller can compare the result length
    with the input length to see how many were skipped. Transport failures
    (``OSError``: dropped connections, DNS errors) are skipped the same
    way as API errors — the old code caught only ``HttpError`` despite
    this skip-tolerant claim, so a network blip aborted the inventory.
    Detail requests go through the batch helper, not one call per label.
    """
    label_ids = [str(label["id"]) for label in labels if label.get("id")]
    requests = [_fetch_label_request(service, label_id) for label_id in label_ids]
    by_id: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    for label_id, (response, exc) in zip(
        label_ids, _chunked_batch(service, requests, settings), strict=False
    ):
        if exc is None:
            by_id[label_id] = response
        else:
            failed.append(label_id)
    # A per-request failure inside a batch is not itself retried, so a
    # transient error on one label gets a second chance the old way
    # before it is skipped for good.
    for label_id in failed:
        try:
            by_id[label_id] = get_label(service, label_id, settings)
        except (HttpError, OSError):
            continue
    return [by_id[label_id] for label_id in label_ids if label_id in by_id]


def _fetch_label_request(service: GmailService, label_id: str) -> Any:
    return service.users().labels().get(userId="me", id=label_id)


def _create_label(service: GmailService, body: dict[str, Any]) -> Any:
    return service.users().labels().create(userId="me", body=body).execute()


def get_or_create_label(
    service: GmailService, label_name: str, settings: Settings | None = None
) -> str:
    """Return the ID of *label_name*, creating the label if it is missing."""
    for label in list_labels(service, settings):
        if str(label.get("name", "")).lower() == label_name.lower():
            return str(label["id"])

    body = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = execute_with_retry(
        partial(_create_label, service, body), settings=settings
    )
    return str(created["id"])


def _batch_modify(service: GmailService, chunk_body: dict[str, Any]) -> Any:
    return (
        service.users().messages().batchModify(userId="me", body=chunk_body).execute()
    )


def modify_messages(
    service: GmailService,
    message_ids: Iterable[str],
    add_label_ids: Iterable[str] | None = None,
    remove_label_ids: Iterable[str] | None = None,
    chunk_size: int | None = None,
    settings: Settings | None = None,
) -> int:
    """Apply label changes with ``batchModify``; return messages modified."""
    ids = list(message_ids)
    if not ids:
        return 0
    effective_chunk = (
        chunk_size
        if chunk_size is not None
        else (settings or DEFAULT_SETTINGS).batch_modify_chunk_size
    )
    body = {
        "ids": [],
        "addLabelIds": list(add_label_ids or []),
        "removeLabelIds": list(remove_label_ids or []),
    }
    modified = 0
    for start in range(0, len(ids), effective_chunk):
        chunk = ids[start : start + effective_chunk]
        chunk_body = dict(body, ids=chunk)
        execute_with_retry(
            partial(_batch_modify, service, chunk_body), settings=settings
        )
        modified += len(chunk)
    return modified
