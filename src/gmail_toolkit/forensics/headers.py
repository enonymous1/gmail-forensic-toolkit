"""Sender timezone extraction from email headers."""

from __future__ import annotations

import re
from datetime import datetime

import dateutil.parser

_TZ_OFFSET = re.compile(r"([+-]\d{4})")


def _offset_string(moment: datetime) -> str:
    tz_name = moment.tzname()
    if tz_name:
        return tz_name
    offset = moment.utcoffset()
    if offset is None:
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}{total_minutes % 60:02d}"


def get_sender_timezone(header_dict: dict) -> str:
    """Extract the sender's timezone from the Date header, then Received.

    Returns a tz name (for example ``EDT``) or a numeric offset (``-0400``),
    and ``UTC`` when neither header yields a timezone. Duplicate ``Received``
    headers are joined by the parser, so every offset is considered.
    """
    headers = {str(key).lower(): value for key, value in header_dict.items()}

    date_header = headers.get("date")
    if isinstance(date_header, str) and date_header.strip():
        try:
            parsed = dateutil.parser.parse(date_header)
        except (ValueError, OverflowError, TypeError):
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return _offset_string(parsed)

    received = headers.get("received", "")
    if isinstance(received, list):
        received = " ".join(str(item) for item in received)
    match = _TZ_OFFSET.search(str(received))
    if match:
        return match.group(1)

    return "UTC"
