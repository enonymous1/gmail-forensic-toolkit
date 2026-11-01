"""Index tests: build/update/search/stats over generated reports.

Reports are built with the real ``generate_forensic_markdown`` so the
index's report parser stays pinned to the generator's format: if the
format changes, these tests fail loudly instead of silently indexing
nothing.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gmail_toolkit import index_db
from gmail_toolkit.cli.main import app
from gmail_toolkit.paths import ensure_directory
from gmail_toolkit.process_email import EmailData, generate_forensic_markdown

runner = CliRunner()


def _make_report(
    *,
    sender: str,
    subject: str,
    date: datetime | None,
    brand: str,
    body_html: str = "",
    body_plain: str = "",
    message_id: str,
) -> str:
    data: EmailData = {
        "id": message_id,
        "subject": subject,
        "sender": sender,
        "date": date,
        "body_html": body_html,
        "body_plain": body_plain,
        "headers": {"subject": subject, "from": sender, "to": "recipient@example.com"},
        "raw_json": {},
    }
    return generate_forensic_markdown(data, brand)


@pytest.fixture
def exports_dir(tmp_path: Path) -> Path:
    root = tmp_path / "exports"
    sender_dir = ensure_directory(root / "Example" / "sender_at_example_com")
    (sender_dir / "2026-07-21_1000_Meeting.md").write_text(
        _make_report(
            sender="Fictional Sender <sender@example.com>",
            subject="Security alert: meeting invite",
            date=datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
            brand="Example",
            body_html=(
                "<h1>Hello</h1><p>quarterly budgets and forecasts</p>"
                "<img src='https://tracker.example/pixel.gif' width='1' height='1'>"
            ),
            message_id="msg-1",
        ),
        encoding="utf-8",
    )
    (sender_dir / "2026-07-22_0900_Newsletter.md").write_text(
        _make_report(
            sender="Fictional Sender <sender@example.com>",
            subject="Weekly newsletter",
            date=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc),
            brand="Example",
            body_plain="garden news",
            message_id="msg-2",
        ),
        encoding="utf-8",
    )
    other_dir = ensure_directory(root / "Other" / "other_at_example_com")
    (other_dir / "nodate_Note.md").write_text(
        _make_report(
            sender="Other Sender <other@example.com>",
            subject="Undated note",
            date=None,
            brand="Other",
            body_plain="something about budgets",
            message_id="msg-3",
        ),
        encoding="utf-8",
    )
    labels_dir = ensure_directory(root / "labels")
    (labels_dir / "labels.json").write_text(
        json.dumps(
            [
                {"id": "INBOX", "name": "INBOX", "type": "system", "messagesTotal": 42},
                {"id": "Label_1", "name": "Example", "type": "user"},
            ]
        ),
        encoding="utf-8",
    )
    ensure_directory(root / "calendar")
    with (root / "calendar" / "calendars.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=["Calendar Name", "Calendar ID"])
        writer.writeheader()
        writer.writerow(
            {"Calendar Name": "Fictional Calendar", "Calendar ID": "primary"}
        )
    events_dir = ensure_directory(root / "calendar" / "events" / "primary")
    with (events_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["Event Name", "Start Time", "End Time", "Description"]
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


def test_build_search_and_stats(exports_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    counts = index_db.build_index(exports_dir, db)
    assert counts == {"messages": 3, "labels": 2, "events": 1}
    assert db.exists()

    found = index_db.search_index(db, "budgets")
    assert {row["report_path"] for row in found} == {
        "Example/sender_at_example_com/2026-07-21_1000_Meeting.md",
        "Other/other_at_example_com/nodate_Note.md",
    }
    assert all("budgets" in row["snippet"].lower() for row in found)
    assert index_db.search_index(db, "budgets", brand="Other")[0]["subject"] == (
        "Undated note"
    )
    assert index_db.search_index(db, "nosuchterm") == []
    # FTS operators in the query are dropped, not interpreted: this
    # neither crashes nor returns everything.
    assert index_db.search_index(db, '" OR 1=1 --') == []

    stats = index_db.index_stats(db)
    assert stats["messages"] == 3
    assert stats["labels"] == 2
    assert stats["events"] == 1
    assert dict(stats["by_brand"]) == {"Example": 2, "Other": 1}
    assert dict(stats["by_type"])["Meeting"] == 1
    assert sum(count for _name, count in stats["by_type"]) == 3
    assert stats["tracking_pixels"] == 1
    assert stats["tracking_links"] == 0
    assert stats["top_senders"][0] == (
        "Fictional Sender <sender@example.com>",
        2,
    )
    assert stats["date_span"] == ("2026-07-21 10:00:00", "2026-07-22 09:00:00")


def test_update_is_incremental(exports_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    index_db.build_index(exports_dir, db)
    sender_dir = exports_dir / "Example" / "sender_at_example_com"
    (sender_dir / "2026-07-23_0800_Extra.md").write_text(
        _make_report(
            sender="Fictional Sender <sender@example.com>",
            subject="Extra report",
            date=datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc),
            brand="Example",
            body_plain="zebra stripes",
            message_id="msg-4",
        ),
        encoding="utf-8",
    )
    counts = index_db.update_index(exports_dir, db)
    assert counts["added"] == 1
    assert counts["updated"] == 0
    assert counts["removed"] == 0
    assert counts["messages"] == 4
    assert index_db.search_index(db, "zebra")[0]["subject"] == "Extra report"

    meeting = sender_dir / "2026-07-21_1000_Meeting.md"
    meeting.write_text(
        meeting.read_text(encoding="utf-8") + "\nAdditional budget notes.\n",
        encoding="utf-8",
    )
    future = time.time() + 60
    os.utime(meeting, (future, future))
    counts = index_db.update_index(exports_dir, db)
    assert counts["updated"] == 1
    assert counts["added"] == 0
    assert counts["removed"] == 0

    (exports_dir / "Other" / "other_at_example_com" / "nodate_Note.md").unlink()
    counts = index_db.update_index(exports_dir, db)
    assert counts["removed"] == 1
    assert counts["messages"] == 3
    assert index_db.search_index(db, "something about budgets") == []


def test_update_without_db_builds(exports_dir: Path, tmp_path: Path) -> None:
    counts = index_db.update_index(exports_dir, tmp_path / "fresh.db")
    assert counts["added"] == 3
    assert counts["messages"] == 3


def test_missing_paths_raise(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        index_db.build_index(tmp_path / "no-exports", tmp_path / "x.db")
    with pytest.raises(FileNotFoundError):
        index_db.search_index(tmp_path / "no.db", "budgets")
    with pytest.raises(FileNotFoundError):
        index_db.index_stats(tmp_path / "no.db")


def _all_output(result) -> str:
    """Stdout + stderr, tolerating Click versions that separate them."""
    err = getattr(result, "stderr", "")
    if isinstance(err, bytes):
        err = err.decode("utf-8", errors="replace")
    return result.output + err


def test_index_cli(
    exports_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(app, ["index", "build", "--help"]).exit_code == 0
    assert runner.invoke(app, ["index", "search", "--help"]).exit_code == 0
    assert runner.invoke(app, ["index", "stats", "--help"]).exit_code == 0
    missing = runner.invoke(
        app, ["index", "search", "budgets", "--db", str(tmp_path / "no.db")]
    )
    assert missing.exit_code != 0
    assert "No index found" in _all_output(missing)

    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["index", "build", "--exports-dir", "exports"])
    assert result.exit_code == 0, result.output
    assert "Indexed 3 message(s)" in result.output
    db = tmp_path / "exports" / "index.db"
    assert db.exists()
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["index", "search", "budgets"])
    assert result.exit_code == 0, result.output
    assert "Security alert: meeting invite" in result.output
    assert "Undated note" in result.output
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["index", "stats"])
    assert result.exit_code == 0, result.output
    assert "Messages: 3" in result.output
    assert "Example: 2" in result.output


def test_schema_v2_columns(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    sender_dir = ensure_directory(root / "Example" / "sender_at_example_com")
    data: EmailData = {
        "id": "msg-v2",
        "subject": "Threaded message",
        "sender": "Fictional Sender <sender@example.com>",
        "date": datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
        "body_html": "",
        "body_plain": "thread body",
        "headers": {
            "subject": "Threaded message",
            "from": "Fictional Sender <sender@example.com>",
            "to": "recipient@example.com",
            "cc": "copy@example.com",
            "labelids": ["INBOX", "IMPORTANT"],
            "in-reply-to": "<parent@example.com>",
            "references": "<a@example.com> <b@example.com>",
            "reply-to": "Reply Name <reply@example.com>",
            "authentication-results": "example.com; spf=pass",
        },
        "raw_json": {},
    }
    report = generate_forensic_markdown(
        data,
        "Example",
        extra_frontmatter={
            "SourceFile": "All Mail.mbox",
            "Attachments": ["report.pdf (application/pdf, 1234 bytes)"],
        },
    )
    (sender_dir / "2026-07-21_1000_Threaded.md").write_text(report, encoding="utf-8")
    index_db.build_index(root)
    connection = sqlite3.connect(root / "index.db")
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM messages").fetchone()
    assert row["recipients_to"] == "recipient@example.com"
    assert row["recipients_cc"] == "copy@example.com"
    assert json.loads(row["labels"]) == ["INBOX", "IMPORTANT"]
    assert row["attachment_count"] == 1
    assert json.loads(row["attachment_names"]) == [
        "report.pdf (application/pdf, 1234 bytes)"
    ]
    assert row["source_file"] == "All Mail.mbox"
    assert row["in_reply_to"] == "<parent@example.com>"
    assert row["references_header"] == "<a@example.com> <b@example.com>"
    assert row["reply_to"] == "Reply Name <reply@example.com>"
    assert row["auth_results"] == "example.com; spf=pass"
    version = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "2"
    connection.close()


def test_old_format_report_gets_v2_defaults(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    sender_dir = ensure_directory(root / "Example" / "sender_at_example_com")
    (sender_dir / "2026-07-21_1000_Old.md").write_text(
        "---\n"
        "Brand: Example\n"
        "Sender: Fictional Sender <sender@example.com>\n"
        "ToolkitVersion: 2.5.0\n"
        "Date: '2026-07-21 10:00:00'\n"
        "Timezone: '-0400'\n"
        "Subject: 'Old report'\n"
        "MessageID: msg-old\n"
        "Type: Meeting\n"
        "Scope: Direct (1:1)\n"
        "---\n\n"
        "> [!INFO] **Forensic Classification**\n"
        "> - **Tracking**: Pixels: 0, Links: 0\n"
        "---\n\n"
        "old body\n",
        encoding="utf-8",
    )
    index_db.build_index(root)
    connection = sqlite3.connect(root / "index.db")
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM messages").fetchone()
    assert row["recipients_to"] == ""
    assert row["labels"] == "[]"
    assert row["attachment_count"] == 0
    assert row["source_file"] == ""
    connection.close()


def test_update_rebuilds_v1_database(tmp_path: Path, exports_dir: Path) -> None:
    db = exports_dir / "index.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    connection.execute(
        "CREATE TABLE messages ("
        "report_path TEXT PRIMARY KEY, message_id TEXT, brand TEXT, "
        "sender TEXT, subject TEXT, date TEXT, type TEXT, scope TEXT, "
        "timezone TEXT, tracking_pixels INTEGER, tracking_links INTEGER, "
        "mtime REAL, size INTEGER)"
    )
    connection.commit()
    connection.close()
    counts = index_db.update_index(exports_dir)
    assert counts["messages"] == 3
    connection = sqlite3.connect(db)
    version = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "2"
    columns = [row[1] for row in connection.execute("PRAGMA table_info(messages)")]
    assert "labels" in columns
    assert "source_file" in columns
    connection.close()
