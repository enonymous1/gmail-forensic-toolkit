"""Tests for the raw .eml archive (v2.10.0): Takeout import and live archive."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path

from typer.testing import CliRunner

from gmail_toolkit import index_db
from gmail_toolkit.cli import archive_sender, batch_archive
from gmail_toolkit.cli import mbox as mbox_module
from gmail_toolkit.cli.main import app
from gmail_toolkit.gmail_api import decode_raw_message
from gmail_toolkit.paths import ensure_directory
from gmail_toolkit.process_email import (
    EmailData,
    generate_forensic_markdown,
    read_report_frontmatter,
)
from tests.conftest import FakeService, make_payload

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Strip ANSI colour codes from CLI output.

    When colour is on (e.g. FORCE_COLOR=1 in the environment), Rich
    renders an option as separate styled spans — ``-``, ``-save``,
    ``-eml`` — so the literal ``--save-eml`` never appears in the raw
    output even though the flag is there and works.
    """
    return _ANSI_RE.sub("", text)


_MBOX = """From sender@example.com Mon Jul 21 10:00:00 2026
Message-ID: <eml1@example.com>
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:00:00 -0400
Subject: First message
To: recipient@example.com
Content-Type: text/plain; charset="utf-8"

First body.

From sender@example.com Mon Jul 21 10:01:00 2026
Message-ID: <eml2@example.com>
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:01:00 -0400
Subject: Second message
To: recipient@example.com
Content-Type: text/plain; charset="utf-8"

Second body.
"""

_RAW = (
    b"From: Fictional Sender <sender@example.com>\n"
    b"Subject: Security alert: meeting invite\n\nRaw body here.\n"
)


def _mbox_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.mbox"
    path.write_text(_MBOX, encoding="utf-8")
    return path


def _raw_b64(data: bytes) -> str:
    # Gmail's raw payloads are base64url without padding.
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _fake_archive_service() -> FakeService:
    return FakeService(
        pages={None: {"messages": [{"id": "msg-1"}]}},
        messages={"msg-1": make_payload()},
        raw_messages={"msg-1": _raw_b64(_RAW)},
    )


def test_import_save_eml_writes_files_frontmatter_and_journal(tmp_path):
    mbox = _mbox_file(tmp_path)
    exports = tmp_path / "exports"
    log = tmp_path / "import.jsonl"
    counts = mbox_module.import_mbox(mbox, exports, log_path=log, save_eml=True)
    assert counts["imported"] == 2
    assert counts["eml_files"] == 2
    eml_files = sorted(exports.rglob("*.eml"))
    assert len(eml_files) == 2
    assert counts["eml_bytes"] == sum(p.stat().st_size for p in eml_files)
    for eml in eml_files:
        report = eml.with_suffix(".md")
        assert report.is_file()
        parsed = BytesParser().parsebytes(eml.read_bytes())
        assert parsed["Subject"] in ("First message", "Second message")
        frontmatter = read_report_frontmatter(report)
        assert frontmatter["RawFile"] == eml.name
        assert frontmatter["RawSha256"] == hashlib.sha256(eml.read_bytes()).hexdigest()
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    messages = [r for r in records if r["event"] == "message"]
    assert all(r["eml"]["saved"] is True for r in messages)
    assert all(r["eml"]["path"].endswith(".eml") for r in messages)
    assert all(
        r["eml"]["sha256"]
        == hashlib.sha256(Path(r["eml"]["path"]).read_bytes()).hexdigest()
        for r in messages
    )
    assert records[-1]["eml_files"] == 2


def test_import_without_flag_writes_no_eml(tmp_path):
    counts = mbox_module.import_mbox(_mbox_file(tmp_path), tmp_path / "exports")
    assert counts["eml_files"] == 0
    assert counts["eml_bytes"] == 0
    assert list((tmp_path / "exports").rglob("*.eml")) == []


def test_import_save_eml_dry_run_writes_nothing(tmp_path):
    mbox = _mbox_file(tmp_path)
    exports = tmp_path / "exports"
    log = tmp_path / "import.jsonl"
    counts = mbox_module.import_mbox(
        mbox, exports, dry_run=True, log_path=log, save_eml=True
    )
    assert counts["imported"] == 2
    assert not exports.exists()
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    messages = [r for r in records if r["event"] == "message"]
    assert all(r["eml"]["saved"] is False for r in messages)
    assert all(r["eml"]["path"].endswith(".eml") for r in messages)


def test_import_save_eml_rerun_writes_no_new_eml(tmp_path):
    mbox = _mbox_file(tmp_path)
    exports = tmp_path / "exports"
    mbox_module.import_mbox(mbox, exports, save_eml=True)
    counts = mbox_module.import_mbox(mbox, exports, save_eml=True)
    assert counts["imported"] == 0
    assert counts["duplicates"] == 2
    assert counts["eml_files"] == 0
    assert len(list(exports.rglob("*.eml"))) == 2


def test_preview_reports_eml_totals(tmp_path):
    data = mbox_module.preview_mbox(_mbox_file(tmp_path), tmp_path / "exports")
    assert data["eml_files"] == 2
    assert data["eml_bytes"] > 0


def test_verify_eml_ok_then_missing_then_mismatch(tmp_path):
    mbox = _mbox_file(tmp_path)
    exports = tmp_path / "exports"
    mbox_module.import_mbox(mbox, exports, save_eml=True)
    data = mbox_module.verify_mbox(mbox, exports)
    assert data["verified"] == 2
    assert data["eml_problems"] == 0
    eml_files = sorted(exports.rglob("*.eml"))
    first, second = eml_files
    first.write_bytes(first.read_bytes() + b"tampered")
    data = mbox_module.verify_mbox(mbox, exports)
    assert data["eml_problems"] == 1
    assert data["eml_details"][0]["issue"] == "hash mismatch"
    second.unlink()
    data = mbox_module.verify_mbox(mbox, exports)
    assert data["eml_problems"] == 2
    assert sorted(d["issue"] for d in data["eml_details"]) == [
        "hash mismatch",
        "missing .eml file",
    ]


def test_verify_ignores_reports_without_raw_file(tmp_path):
    mbox = _mbox_file(tmp_path)
    exports = tmp_path / "exports"
    mbox_module.import_mbox(mbox, exports)
    data = mbox_module.verify_mbox(mbox, exports)
    assert data["verified"] == 2
    assert data["eml_problems"] == 0


def test_decode_raw_message_handles_missing_padding():
    assert decode_raw_message(_raw_b64(_RAW)) == _RAW
    padded = base64.urlsafe_b64encode(_RAW).decode("ascii")
    assert decode_raw_message(padded) == _RAW


def test_archive_save_eml_writes_decoded_raw_bytes(tmp_path):
    service = _fake_archive_service()
    exports = tmp_path / "exports"
    count = archive_sender.archive_contact(
        service, "sender@example.com", exports_dir=exports, save_eml=True
    )
    assert count == 1
    eml_files = list(exports.rglob("*.eml"))
    assert len(eml_files) == 1
    assert eml_files[0].read_bytes() == _RAW
    report = eml_files[0].with_suffix(".md")
    frontmatter = read_report_frontmatter(report)
    assert frontmatter["RawFile"] == eml_files[0].name
    assert frontmatter["RawSha256"] == hashlib.sha256(_RAW).hexdigest()
    # One full fetch and one raw fetch for the same message.
    assert service.messages_api.get_calls == ["msg-1", "msg-1"]


def test_archive_without_flag_fetches_full_only(tmp_path):
    service = _fake_archive_service()
    exports = tmp_path / "exports"
    archive_sender.archive_contact(service, "sender@example.com", exports_dir=exports)
    assert list(exports.rglob("*.eml")) == []
    assert service.messages_api.get_calls == ["msg-1"]


def test_batch_save_eml_passthrough(tmp_path):
    service = _fake_archive_service()
    directory = tmp_path / "Contact_Directory.csv"
    directory.write_text(
        "Sender,Count\nFictional Sender <sender@example.com>,1\n",
        encoding="utf-8",
    )
    exports = tmp_path / "exports"
    processed = batch_archive.process_batch(
        service, directory, batch_size=1, limit=1, exports_dir=exports, save_eml=True
    )
    assert processed == 1
    eml_files = list(exports.rglob("*.eml"))
    assert len(eml_files) == 1
    assert eml_files[0].read_bytes() == _RAW


def _report_data() -> EmailData:
    return {
        "id": "msg-eml",
        "subject": "With raw file",
        "sender": "Fictional Sender <sender@example.com>",
        "date": datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
        "body_html": "",
        "body_plain": "body",
        "headers": {"subject": "With raw file", "from": "x"},
        "raw_json": {},
    }


def test_index_schema_v4_eml_columns(tmp_path):
    root = tmp_path / "exports"
    sender_dir = ensure_directory(root / "Example" / "sender_at_example_com")
    eml = sender_dir / "2026-07-21_1000_With raw file.eml"
    eml.write_bytes(b"raw bytes")
    report = generate_forensic_markdown(
        _report_data(),
        "Example",
        extra_frontmatter={
            "RawFile": eml.name,
            "RawSha256": hashlib.sha256(b"raw bytes").hexdigest(),
        },
    )
    (sender_dir / "2026-07-21_1000_With raw file.md").write_text(
        report, encoding="utf-8"
    )
    plain_data = _report_data()
    plain_data["id"] = "msg-plain"
    plain = generate_forensic_markdown(plain_data, "Example")
    (sender_dir / "2026-07-21_1000_Plain.md").write_text(plain, encoding="utf-8")
    index_db.build_index(root)
    connection = sqlite3.connect(root / "index.db")
    connection.row_factory = sqlite3.Row
    rows = {
        row["message_id"]: row for row in connection.execute("SELECT * FROM messages")
    }
    assert rows["msg-eml"]["eml_path"] == eml.name
    assert rows["msg-eml"]["eml_sha256"] == hashlib.sha256(b"raw bytes").hexdigest()
    assert rows["msg-eml"]["eml_bytes"] == len(b"raw bytes")
    assert rows["msg-plain"]["eml_path"] == ""
    assert rows["msg-plain"]["eml_sha256"] == ""
    assert rows["msg-plain"]["eml_bytes"] == 0
    version = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "4"
    connection.close()


def test_update_rebuilds_v3_database(tmp_path):
    root = tmp_path / "exports"
    sender_dir = ensure_directory(root / "Example" / "sender_at_example_com")
    report = generate_forensic_markdown(_report_data(), "Example")
    (sender_dir / "2026-07-21_1000_With raw file.md").write_text(
        report, encoding="utf-8"
    )
    db = root / "index.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    connection.execute(
        "CREATE TABLE messages ("
        "report_path TEXT PRIMARY KEY, message_id TEXT, brand TEXT, "
        "sender TEXT, subject TEXT, date TEXT, type TEXT, scope TEXT, "
        "timezone TEXT, tracking_pixels INTEGER, tracking_links INTEGER, "
        "recipients_to TEXT, recipients_cc TEXT, labels TEXT, "
        "attachment_count INTEGER, attachment_names TEXT, "
        "attachment_paths TEXT, source_file TEXT, in_reply_to TEXT, "
        "references_header TEXT, reply_to TEXT, auth_results TEXT, "
        "mtime REAL, size INTEGER)"
    )
    connection.commit()
    connection.close()
    counts = index_db.update_index(root)
    assert counts["messages"] == 1
    connection = sqlite3.connect(db)
    version = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "4"
    columns = [row[1] for row in connection.execute("PRAGMA table_info(messages)")]
    assert "eml_path" in columns
    assert "eml_sha256" in columns
    assert "eml_bytes" in columns
    connection.close()


def test_restore_preserves_raw_link(tmp_path):
    exports = tmp_path / "exports"
    sender_dir = ensure_directory(exports / "Example" / "sender_at_example_com")
    source_dir = ensure_directory(sender_dir / "SourceFiles")
    payload = make_payload()
    (source_dir / "2026-07-21_1000_Test.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    eml = sender_dir / "2026-07-21_1000_Test.eml"
    eml.write_bytes(b"raw bytes")
    raw_sha = hashlib.sha256(b"raw bytes").hexdigest()
    (sender_dir / "2026-07-21_1000_Test.md").write_text(
        "---\n"
        "Brand: Example\n"
        "Sender: Fictional Sender <sender@example.com>\n"
        f"RawFile: {eml.name}\n"
        f"RawSha256: {raw_sha}\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["restore", "rebuild", str(exports)])
    assert result.exit_code == 0, result.output
    frontmatter = read_report_frontmatter(sender_dir / "2026-07-21_1000_Test.md")
    assert frontmatter["RawFile"] == eml.name
    assert frontmatter["RawSha256"] == raw_sha


def test_restore_drops_raw_link_when_eml_gone(tmp_path):
    exports = tmp_path / "exports"
    sender_dir = ensure_directory(exports / "Example" / "sender_at_example_com")
    source_dir = ensure_directory(sender_dir / "SourceFiles")
    (source_dir / "2026-07-21_1000_Test.json").write_text(
        json.dumps(make_payload()), encoding="utf-8"
    )
    (sender_dir / "2026-07-21_1000_Test.md").write_text(
        "---\n"
        "Brand: Example\n"
        "Sender: Fictional Sender <sender@example.com>\n"
        "RawFile: 2026-07-21_1000_Test.eml\n"
        "RawSha256: deadbeef\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["restore", "rebuild", str(exports)])
    assert result.exit_code == 0, result.output
    frontmatter = read_report_frontmatter(sender_dir / "2026-07-21_1000_Test.md")
    assert "RawFile" not in frontmatter


def test_import_save_eml_cli(tmp_path):
    mbox = _mbox_file(tmp_path)
    exports = tmp_path / "exports"
    result = runner.invoke(
        app,
        [
            "mbox",
            "import",
            str(mbox),
            "--exports-dir",
            str(exports),
            "--save-eml",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2 .eml file(s) saved" in result.output
    assert len(list(exports.rglob("*.eml"))) == 2


def test_archive_save_eml_cli(monkeypatch, tmp_path):
    service = _fake_archive_service()
    monkeypatch.setattr(archive_sender, "get_gmail_service", lambda: service)
    exports = tmp_path / "exports"
    result = runner.invoke(
        app,
        [
            "archive",
            "contact",
            "sender@example.com",
            "--exports-dir",
            str(exports),
            "--save-eml",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(list(exports.rglob("*.eml"))) == 1


def test_save_eml_flags_in_help():
    assert "--save-eml" in _strip_ansi(
        runner.invoke(app, ["mbox", "import", "--help"]).output
    )
    assert "--save-eml" in _strip_ansi(
        runner.invoke(app, ["archive", "contact", "--help"]).output
    )
    assert "--save-eml" in _strip_ansi(
        runner.invoke(app, ["batch", "process", "--help"]).output
    )


def test_save_eml_flags_in_help_with_color(monkeypatch):
    """Regression: with colour forced on, the raw help output splits
    ``--save-eml`` across styled spans, so the plain substring check
    fails even though the flag is present."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    raw = runner.invoke(app, ["mbox", "import", "--help"]).output
    assert "\x1b[" in raw  # colour really was on for this render
    assert "--save-eml" not in raw
    assert "--save-eml" in _strip_ansi(raw)
