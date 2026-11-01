"""Viewer tests: Flask test client against synthetic exports (no server)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gmail_toolkit.cli.main import app
from gmail_toolkit.viewer import create_app

runner = CliRunner()

_REPORT_1 = """---
Brand: Example
Sender: Fictional Sender <sender@example.com>
ToolkitVersion: 2.3.0
Date: '2026-07-21 10:00:00'
Timezone: '-0400'
Subject: 'Security alert: meeting invite'
MessageID: msg-1
Type: Meeting
Scope: Direct (1:1)
---

> [!INFO] **Forensic Classification**
> - **Brand**: Example

# Hello

Some body text.
"""

_REPORT_2 = """---
Brand: Other
Sender: Another Sender <other@example.com>
ToolkitVersion: 2.3.0
Date: '2026-07-20 09:00:00'
Timezone: '-0400'
Subject: 'Weekly newsletter'
MessageID: msg-2
Type: Newsletter
Scope: Bulk
---

# Newsletter

Weekly content.
"""


@pytest.fixture
def exports_dir(tmp_path: Path) -> Path:
    root = tmp_path / "exports"
    sender_dir = root / "Example" / "sender_at_example_com"
    sender_dir.mkdir(parents=True)
    (sender_dir / "2026-07-21_1000_Security.md").write_text(_REPORT_1, encoding="utf-8")
    other_dir = root / "Other" / "other_at_example_com"
    other_dir.mkdir(parents=True)
    (other_dir / "2026-07-20_0900_Newsletter.md").write_text(
        _REPORT_2, encoding="utf-8"
    )
    (root / "labels").mkdir()
    (root / "labels" / "labels.json").write_text(
        json.dumps(
            [
                {
                    "id": "INBOX",
                    "name": "INBOX",
                    "type": "system",
                    "messagesTotal": 42,
                    "messagesUnread": 3,
                },
                {"id": "Label_1", "name": "Example", "type": "user"},
            ]
        ),
        encoding="utf-8",
    )
    (root / "calendar").mkdir()
    with (root / "calendar" / "calendars.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=["Calendar Name", "Calendar ID"])
        writer.writeheader()
        writer.writerow(
            {"Calendar Name": "Fictional Calendar", "Calendar ID": "primary"}
        )
    events_dir = root / "calendar" / "events" / "primary"
    events_dir.mkdir(parents=True)
    with (events_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Event Name", "Start Time", "End Time", "Description"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "Event Name": "Fictional Standup",
                "Start Time": "2026-10-28T09:00:00-04:00",
                "End Time": "2026-10-28T09:30:00-04:00",
                "Description": "Daily sync",
            }
        )
    return root


def test_home_shows_counts(exports_dir: Path) -> None:
    client = create_app(exports_dir).test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"2 report(s)" in response.data
    assert b"2 sender(s)" in response.data
    assert b"2 label(s)" in response.data
    assert b"1 calendar event(s)" in response.data


def test_reports_list_search_and_brand_filter(exports_dir: Path) -> None:
    client = create_app(exports_dir).test_client()
    response = client.get("/reports")
    assert response.status_code == 200
    assert b"Security alert" in response.data
    assert b"Weekly newsletter" in response.data
    # Newest first.
    assert response.data.index(b"Security alert") < response.data.index(
        b"Weekly newsletter"
    )
    response = client.get("/reports", query_string={"q": "newsletter"})
    assert b"Weekly newsletter" in response.data
    assert b"Security alert" not in response.data
    response = client.get("/reports", query_string={"brand": "Other"})
    assert b"Weekly newsletter" in response.data
    assert b"Security alert" not in response.data


def test_report_renders_markdown_and_raw(exports_dir: Path) -> None:
    client = create_app(exports_dir).test_client()
    rel = "Example/sender_at_example_com/2026-07-21_1000_Security.md"
    response = client.get(f"/report/{rel}")
    assert response.status_code == 200
    assert b"Security alert: meeting invite" in response.data
    assert b"<h1>Hello</h1>" in response.data
    assert b"View raw Markdown" in response.data
    response = client.get(f"/raw/{rel}")
    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert b"# Hello" in response.data


def test_report_404_and_traversal_blocked(exports_dir: Path) -> None:
    client = create_app(exports_dir).test_client()
    assert client.get("/report/Example/nope.md").status_code == 404
    # A non-Markdown file inside exports is not served as a report.
    assert client.get("/report/labels/labels.json").status_code == 404
    # Dot-dot segments must not escape the exports directory.
    assert client.get("/report/%2E%2E%2Flabels%2Flabels.json").status_code == 404
    assert client.get("/report/..%2F..%2Fsecret.md").status_code == 404


def test_labels_page_and_missing_inventory(tmp_path: Path, exports_dir: Path) -> None:
    client = create_app(exports_dir).test_client()
    response = client.get("/labels")
    assert response.status_code == 200
    assert b"INBOX" in response.data
    assert b"42" in response.data
    empty = create_app(tmp_path / "no-exports").test_client()
    response = empty.get("/labels")
    assert response.status_code == 200
    assert b"No label inventory found" in response.data


def test_calendar_page(exports_dir: Path) -> None:
    client = create_app(exports_dir).test_client()
    response = client.get("/calendar")
    assert response.status_code == 200
    assert b"Fictional Calendar" in response.data
    assert b"Fictional Standup" in response.data
    assert b"Daily sync" in response.data


def test_empty_exports_dir_home(tmp_path: Path) -> None:
    client = create_app(tmp_path / "no-exports").test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"No exports found yet" in response.data


def test_serve_cli_help_and_bad_port() -> None:
    assert runner.invoke(app, ["serve", "--help"]).exit_code == 0
    assert runner.invoke(app, ["serve", "--port", "0"]).exit_code != 0
    assert runner.invoke(app, ["serve", "--port", "70000"]).exit_code != 0
