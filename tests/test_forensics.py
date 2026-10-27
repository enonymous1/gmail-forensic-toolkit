"""Tests for classifier, timezone, and tracking detection."""

from __future__ import annotations

from gmail_toolkit.forensics.classifier import classify_message
from gmail_toolkit.forensics.headers import get_sender_timezone
from gmail_toolkit.forensics.scanners import detect_tracking


def test_classifier_meeting_and_notification_keywords():
    assert classify_message("Meeting invite tomorrow", "a@b.com", {})[0] == "Meeting"
    assert classify_message("Security alert", "a@b.com", {})[0] == "Notification"
    assert classify_message("Hello", "a@b.com", {}) == ("Email", "Direct (1:1)")


def test_classifier_draft_label_and_group_scope():
    headers = {"labelids": ["INBOX", "DRAFT"]}
    assert classify_message("Hello", "a@b.com", headers)[0] == "Draft"
    headers = {
        "to": "one@example.com, two@example.com",
        "cc": "three@example.com",
    }
    assert classify_message("Hello", "a@b.com", headers)[1] == "Group"
    headers = {"list-id": "<list.example.com>", "to": "one@example.com"}
    assert classify_message("Hello", "a@b.com", headers)[1] == "Broadcast"
    headers = {"precedence": "bulk"}
    assert classify_message("Hello", "a@b.com", headers)[1] == "Broadcast"


def test_sender_timezone_prefers_date_header():
    headers = {"date": "Mon, 21 Jul 2026 10:00:00 -0400"}
    assert get_sender_timezone(headers) == "-0400"
    headers = {"date": "Mon, 21 Jul 2026 10:00:00 +0000 (UTC)"}
    assert get_sender_timezone(headers) == "UTC"
    headers = {"received": "from x by y; 21 Jul 2026 10:00:00 +0530"}
    assert get_sender_timezone(headers) == "+0530"
    assert get_sender_timezone({}) == "UTC"


def test_detect_tracking_pixels_and_links():
    html = (
        "<img src='https://example.com/pixel.gif' width='1px' height='1px'>"
        "<img src='https://example.com/logo.png' width='100' height='40'>"
        "<img src='https://example.com/b.png' style='display:none'>"
        "<a href='https://bit.ly/abc'>short</a>"
        "<a href='https://example.com/page'>normal</a>"
    )
    pixels, links = detect_tracking(html)
    assert pixels == 2
    assert links == 1
    assert detect_tracking("") == (0, 0)
    assert detect_tracking("<p>no tracking</p>") == (0, 0)
