"""Tests for payload parsing and forensic Markdown generation."""

from __future__ import annotations

import json

from gmail_toolkit.process_email import (
    convert_html_to_markdown,
    generate_forensic_markdown,
    parse_gmail_payload,
    save_source_files,
)
from tests.conftest import encode_body, make_payload


def test_parse_multipart_first_body_wins(payload):
    payload["payload"]["parts"].append(
        {
            "mimeType": "text/html",
            "filename": "attachment.html",
            "body": {"data": encode_body("<p>attachment</p>")},
        }
    )
    data = parse_gmail_payload(payload)
    assert "Hello" in data["body_html"]
    assert "attachment" not in data["body_html"]
    assert data["body_plain"] == "Hello"
    assert data["id"] == "msg-1"
    assert data["subject"] == "Security alert: meeting invite"


def test_parse_duplicate_received_headers_are_joined():
    payload = make_payload(
        extra_headers=[
            {"name": "Received", "value": "from a by b with ESMTP; x -0500"},
            {"name": "Received", "value": "from c by d; y +0200"},
        ]
    )
    # Remove the Date header so the Received fallback is exercised.
    payload["payload"]["headers"] = [
        header for header in payload["payload"]["headers"] if header["name"] != "Date"
    ]
    data = parse_gmail_payload(payload)
    assert "-0500" in str(data["headers"]["received"])
    assert "+0200" in str(data["headers"]["received"])


def test_parse_bad_base64_and_missing_date_do_not_crash():
    payload = make_payload()
    payload["payload"]["parts"][1]["body"]["data"] = "!!!not-base64!!!"
    payload["payload"]["headers"] = [
        header for header in payload["payload"]["headers"] if header["name"] != "Date"
    ]
    data = parse_gmail_payload(payload)
    assert data["body_html"] == ""
    # No Date header must stay unknown, never be replaced with "now".
    assert data["date"] is None


def test_missing_date_reports_unknown_and_uses_nodate_stem():
    payload = make_payload()
    payload["payload"]["headers"] = [
        header for header in payload["payload"]["headers"] if header["name"] != "Date"
    ]
    data = parse_gmail_payload(payload)
    output = generate_forensic_markdown(data, "Example")
    assert "Date: Unknown" in output
    from gmail_toolkit.paths import get_base_filename

    assert get_base_filename(data["date"], "Hello: World") == "nodate_Hello World"


def test_forensic_markdown_uses_the_real_engine(payload):
    data = parse_gmail_payload(payload)
    output = generate_forensic_markdown(data, "Example")
    # The 1.0 placeholder output was hardcoded to these values; the fixed
    # engine must classify the fixture's meeting/security subject, detect
    # the 1x1 pixel and t.co link, and report the -0400 timezone.
    assert "Category**: Meeting" in output
    assert "Tracking**: Pixels: 1, Links: 1" in output
    assert "Timezone**: -0400" in output
    assert "Type: Meeting" in output
    assert "Scope: Direct (1:1)" in output
    assert "<img" not in output
    assert "# Hello" in output


def test_forensic_markdown_plain_only_uses_plain_text():
    payload = make_payload()
    payload["payload"] = {
        "mimeType": "text/plain",
        "headers": payload["payload"]["headers"],
        "body": {"data": encode_body("plain body only")},
    }
    data = parse_gmail_payload(payload)
    output = generate_forensic_markdown(data, "Example")
    assert "plain body only" in output


def test_convert_html_to_markdown_never_returns_raw_html():
    assert convert_html_to_markdown("") == ""
    converted = convert_html_to_markdown("<h1>Title</h1><p>Body <b>bold</b></p>")
    assert "<h1>" not in converted
    assert "Title" in converted
    assert "**bold**" in converted


def test_save_source_files_writes_json_html_and_text(tmp_path, payload):
    data = parse_gmail_payload(payload)
    save_source_files(data, tmp_path, "2026-07-21_1000_Subject")
    source_dir = tmp_path / "SourceFiles"
    assert (
        json.loads((source_dir / "2026-07-21_1000_Subject.json").read_text())["id"]
        == "msg-1"
    )
    assert "Hello" in (source_dir / "2026-07-21_1000_Subject.html").read_text()
    assert (source_dir / "2026-07-21_1000_Subject.txt").read_text() == "Hello"
