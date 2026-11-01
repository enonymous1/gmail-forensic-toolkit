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
