"""mbox import tests: synthetic .mbox files, no Gmail API, no live data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from email import message_from_string
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from gmail_toolkit.cli import mbox as mbox_module
from gmail_toolkit.cli.main import app

runner = CliRunner()

_MBOX = """From sender@example.com Mon Jul 21 10:00:00 2026
Message-ID: <msg1@example.com>
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:00:00 -0400
Subject: Security alert: meeting invite
To: recipient@example.com
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="B"

--B
Content-Type: text/plain; charset="utf-8"

Hello plain.
--B
Content-Type: text/html; charset="utf-8"

<html><body><h1>Hello</h1>
<img src="https://tracker.example/pixel.gif" width="1" height="1">
</body></html>
--B--

From sender@example.com Mon Jul 21 10:01:00 2026
Message-ID: <msg1@example.com>
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:01:00 -0400
Subject: Duplicate should be skipped
To: recipient@example.com
Content-Type: text/plain; charset="utf-8"

Duplicate body.

From other@example.com Mon Jul 21 10:02:00 2026
From: Other Sender <other@example.com>
Date: not-a-date
Subject: No date note
To: recipient@example.com
Content-Type: text/plain; charset="utf-8"

No date and no Message-ID.

From sender@example.com Mon Jul 21 10:03:00 2026
Message-ID: <msg4@example.com>
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:03:00 -0400
Subject: With attachment
To: recipient@example.com
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="M"

--M
Content-Type: text/plain; charset="utf-8"

See attached.
--M
Content-Type: application/pdf; name="report.pdf"
Content-Transfer-Encoding: base64
Content-Disposition: attachment; filename="report.pdf"

JVBERi0xLjQK
--M--

From sender@example.com Mon Jul 21 10:04:00 2026
Message-ID: <msg5@example.com>
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:04:00 -0400
Subject: =?UTF-8?B?U2VjdXJpdHkgYWxlcnQ=?=
To: recipient@example.com
Content-Type: text/plain; charset="utf-8"

Encoded subject body.
"""


@pytest.fixture
def mbox_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.mbox"
    path.write_text(_MBOX, encoding="utf-8")
    return path


def _read_reports(exports: Path) -> list[str]:
    return [path.read_text(encoding="utf-8") for path in exports.rglob("*.md")]


def test_import_writes_reports_duplicates_and_fallback_ids(
    tmp_path: Path, mbox_file: Path
) -> None:
    exports = tmp_path / "exports"
    counts = mbox_module.import_mbox(mbox_file, exports)
    assert counts == {
        "total": 5,
        "imported": 4,
        "duplicates": 1,
        "errors": 0,
        "attachments": 0,
        "attachment_bytes": 0,
        "eml_files": 0,
        "eml_bytes": 0,
    }
    reports = _read_reports(exports)
    assert len(reports) == 4
    joined = "\n".join(reports)
    # Message 1: classified, tracking counted, HTML converted.
    assert "Subject: 'Security alert: meeting invite'" in joined
    assert "Category**: Meeting" in joined
    assert "Pixels: 1" in joined
    assert "# Hello" in joined
    assert "Duplicate should be skipped" not in joined
    # Message 3: no Message-ID and no parseable date.
    # (Subjects without a colon dump unquoted; the colon in message 1's
    # subject is what forces quotes there.)
    assert "Subject: No date note" in joined
    assert "MessageID: mbox-" in joined
    nodate = list((exports / "Individual" / "other_at_example_com").glob("nodate_*.md"))
    assert len(nodate) == 1
    # Message 4: attachment metadata recorded, bytes not extracted.
    assert "## Attachments" in joined
    assert "report.pdf (application/pdf," in joined
    assert not list(exports.rglob("*.pdf"))
    # Message 5: RFC 2047 subject decoded.
    assert "Subject: Security alert\n" in joined


def test_rerun_skips_everything(tmp_path: Path, mbox_file: Path) -> None:
    exports = tmp_path / "exports"
    mbox_module.import_mbox(mbox_file, exports)
    counts = mbox_module.import_mbox(mbox_file, exports)
    assert counts == {
        "total": 5,
        "imported": 0,
        "duplicates": 5,
        "errors": 0,
        "attachments": 0,
        "attachment_bytes": 0,
        "eml_files": 0,
        "eml_bytes": 0,
    }
    assert len(_read_reports(exports)) == 4


def test_limit_caps_messages_read(tmp_path: Path, mbox_file: Path) -> None:
    counts = mbox_module.import_mbox(mbox_file, tmp_path / "exports", limit=2)
    assert counts["total"] == 2
    assert counts["imported"] == 1
    assert counts["duplicates"] == 1


def test_one_bad_message_does_not_abort_import(
    tmp_path: Path, mbox_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = mbox_module.generate_forensic_markdown
    calls = {"n": 0}

    def flaky(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(mbox_module, "generate_forensic_markdown", flaky)
    counts = mbox_module.import_mbox(mbox_file, tmp_path / "exports")
    assert counts == {
        "total": 5,
        "imported": 3,
        "duplicates": 1,
        "errors": 1,
        "attachments": 0,
        "attachment_bytes": 0,
        "eml_files": 0,
        "eml_bytes": 0,
    }


def test_mbox_cli(
    tmp_path: Path, mbox_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(app, ["mbox", "import", "--help"]).exit_code == 0
    missing = runner.invoke(app, ["mbox", "import", str(tmp_path / "missing.mbox")])
    assert missing.exit_code != 0
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(
            app, ["mbox", "import", str(mbox_file), "--exports-dir", "exports"]
        )
    assert result.exit_code == 0, result.output
    assert "4 imported" in result.output


def test_preview_reports_counts(tmp_path: Path, mbox_file: Path) -> None:
    exports = tmp_path / "exports"
    data = mbox_module.preview_mbox(mbox_file, exports)
    assert data["total"] == 5
    assert data["errors"] == 0
    assert data["with_message_id"] == 4
    assert data["without_message_id"] == 1
    assert data["with_date"] == 4
    assert data["without_date"] == 1
    assert data["new"] == 4
    assert data["duplicates"] == 1
    assert data["with_attachments"] == 1
    assert data["sender_count"] == 2
    assert data["date_min"] == datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)
    assert data["date_max"] == datetime(2026, 7, 21, 14, 4, tzinfo=timezone.utc)
    assert data["by_brand"] == [("Individual", 5)]
    assert data["top_senders"][0] == ("Fictional Sender <sender@example.com>", 4)
    # Preview writes nothing, not even the exports directory itself.
    assert not exports.exists()


def test_preview_after_import_shows_all_duplicates(
    tmp_path: Path, mbox_file: Path
) -> None:
    exports = tmp_path / "exports"
    mbox_module.import_mbox(mbox_file, exports, limit=None)
    data = mbox_module.preview_mbox(mbox_file, exports)
    assert data["new"] == 0
    assert data["duplicates"] == 5


def test_preview_limit(tmp_path: Path, mbox_file: Path) -> None:
    data = mbox_module.preview_mbox(mbox_file, tmp_path / "exports", limit=2)
    assert data["total"] == 2


def test_dry_run_writes_nothing(tmp_path: Path, mbox_file: Path) -> None:
    exports = tmp_path / "exports"
    counts = mbox_module.import_mbox(mbox_file, exports, dry_run=True)
    assert counts["imported"] == 4
    assert counts["duplicates"] == 1
    # Not a single report file, and not even the exports directory.
    assert not exports.exists()


def test_preview_and_dry_run_cli(
    tmp_path: Path, mbox_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(app, ["mbox", "preview", "--help"]).exit_code == 0
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        preview = runner.invoke(
            app, ["mbox", "preview", str(mbox_file), "--exports-dir", "exports"]
        )
        assert preview.exit_code == 0, preview.output
        assert "Messages: 5" in preview.output
        assert "4 new" in preview.output
        dry = runner.invoke(
            app,
            [
                "mbox",
                "import",
                str(mbox_file),
                "--exports-dir",
                "exports",
                "--dry-run",
            ],
        )
        assert dry.exit_code == 0, dry.output
        assert "4 would be imported" in dry.output
        assert "Would import:" in dry.output
    assert not (tmp_path / "exports").exists()


def test_import_quiet_suppresses_per_message_lines(
    tmp_path: Path, mbox_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "mbox",
                "import",
                str(mbox_file),
                "--exports-dir",
                "exports",
                "--quiet",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Imported:" not in result.output
    assert "4 imported" in result.output


_LABELED_MBOX = """From sender@example.com Mon Jul 21 10:00:00 2026
Message-ID: <lab1@example.com>
From: Fictional Sender <sender@example.com>
To: recipient@example.com
Cc: copy@example.com
X-Gmail-Labels: INBOX, IMPORTANT
Date: Mon, 21 Jul 2026 10:00:00 -0400
Subject: Labeled message
Content-Type: text/plain; charset="utf-8"

Labeled body.
"""

# Two distinct messages that collide on one fallback id: same sender,
# subject, and Date header, and identical first 1,000 plain characters,
# but different full bodies. Only the full-body hash tells them apart.
_COLLISION_MBOX = (
    """From sender@example.com Mon Jul 21 10:00:00 2026
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:00:00 -0400
Subject: Identical alert
Content-Type: text/plain; charset="utf-8"

"""
    + "A" * 1000
    + """ one.
From sender@example.com Mon Jul 21 10:00:00 2026
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:00:00 -0400
Subject: Identical alert
Content-Type: text/plain; charset="utf-8"

"""
    + "A" * 1000
    + """ two.
"""
)


def _frontmatter_of(report_path: Path) -> dict:
    text = report_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    closing = text.find("\n---\n", 4)
    return yaml.safe_load(text[4:closing])


def test_parser_keeps_threading_and_auth_headers() -> None:
    msg = message_from_string(
        "From: Fictional Sender <sender@example.com>\n"
        "Subject: Re: hello\n"
        "In-Reply-To: <parent@example.com>\n"
        "References: <a@example.com> <b@example.com>\n"
        "Reply-To: Reply Name <reply@example.com>\n"
        "Authentication-Results: example.com; spf=pass dkim=pass\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "\n"
        "Body.\n"
    )
    parsed = mbox_module.parse_message(msg)
    assert parsed.headers["in-reply-to"] == "<parent@example.com>"
    assert parsed.headers["references"] == "<a@example.com> <b@example.com>"
    assert parsed.headers["reply-to"] == "Reply Name <reply@example.com>"
    assert parsed.headers["authentication-results"] == "example.com; spf=pass dkim=pass"


def test_imported_report_carries_full_fidelity_frontmatter(
    tmp_path: Path,
) -> None:
    mbox_file = tmp_path / "labeled.mbox"
    mbox_file.write_text(_LABELED_MBOX, encoding="utf-8")
    exports = tmp_path / "exports"
    counts = mbox_module.import_mbox(mbox_file, exports)
    assert counts["imported"] == 1
    reports = list(exports.rglob("*.md"))
    assert len(reports) == 1
    data = _frontmatter_of(reports[0])
    assert data["To"] == "recipient@example.com"
    assert data["Cc"] == "copy@example.com"
    assert data["Labels"] == ["INBOX", "IMPORTANT"]
    assert data["SourceFile"] == str(mbox_file)
    assert data["Attachments"] == []
    preview = mbox_module.preview_mbox(mbox_file, exports)
    assert preview["by_label"] == [("INBOX", 1), ("IMPORTANT", 1)]


def test_import_journal(tmp_path: Path, mbox_file: Path) -> None:
    exports = tmp_path / "exports"
    log = tmp_path / "import.jsonl"
    mbox_module.import_mbox(mbox_file, exports, log_path=log)
    records = [json.loads(line) for line in log.read_text().splitlines()]
    messages = [r for r in records if r["event"] == "message"]
    summaries = [r for r in records if r["event"] == "summary"]
    assert len(messages) == 5
    assert len(summaries) == 1
    outcomes = sorted(r["outcome"] for r in messages)
    assert outcomes == ["duplicate", "imported", "imported", "imported", "imported"]
    assert all(r["body_sha256"] for r in messages)
    assert all(r["report"] for r in messages if r["outcome"] == "imported")
    summary = summaries[0]
    assert summary["imported"] == 4
    assert summary["duplicates"] == 1
    assert summary["dry_run"] is False
    assert summary["source"] == str(mbox_file)


def test_verify_before_and_after_import(tmp_path: Path, mbox_file: Path) -> None:
    exports = tmp_path / "exports"
    before = mbox_module.verify_mbox(mbox_file, exports)
    assert before["verified"] == 0
    assert before["missing"] == 4
    assert before["duplicates"] == 1
    # The fixture's "duplicate" reuses msg1's id with a different
    # subject, date, and body, so it is a message-id collision too —
    # exactly the case plain dedupe cannot see.
    assert before["collisions"] == 1
    assert before["collision_details"][0]["kind"] == "message-id"
    assert len(before["missing_details"]) == 4
    # Verify writes nothing, not even the exports directory.
    assert not exports.exists()
    mbox_module.import_mbox(mbox_file, exports)
    after = mbox_module.verify_mbox(mbox_file, exports)
    assert after["verified"] == 4
    assert after["missing"] == 0
    assert after["duplicates"] == 1
    assert after["collisions"] == 1
    assert after["missing_details"] == []


def test_verify_flags_fallback_collision(tmp_path: Path) -> None:
    mbox_file = tmp_path / "collision.mbox"
    mbox_file.write_text(_COLLISION_MBOX, encoding="utf-8")
    exports = tmp_path / "exports"
    data = mbox_module.verify_mbox(mbox_file, exports)
    assert data["total"] == 2
    assert data["missing"] == 1
    assert data["duplicates"] == 1
    assert data["collisions"] == 1
    collision = data["collision_details"][0]
    assert collision["kind"] == "fallback"
    assert collision["first_index"] == 0
    assert collision["index"] == 1
    # The import itself still dedupes the second message; verify is
    # what makes that silent loss visible.
    counts = mbox_module.import_mbox(mbox_file, exports)
    assert counts["imported"] == 1
    assert counts["duplicates"] == 1


def test_verify_cli(
    tmp_path: Path, mbox_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(app, ["mbox", "verify", "--help"]).exit_code == 0
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(
            app, ["mbox", "verify", str(mbox_file), "--exports-dir", "exports"]
        )
    assert result.exit_code == 0, result.output
    assert "missing: 4" in result.output
    assert "Missing reports:" in result.output


def test_parser_keeps_attachment_bytes(mbox_file: Path) -> None:
    from gmail_toolkit.mbox_parser import iter_mbox, parse_message

    for msg in iter_mbox(mbox_file):
        parsed = parse_message(msg)
        if parsed.attachments:
            assert len(parsed.attachments) == 1
            assert parsed.attachments[0].filename == "report.pdf"
            assert parsed.attachments[0].size_bytes == 9
            assert parsed.attachments[0].data == b"%PDF-1.4\n"
            return
    raise AssertionError("no attachment found in fixture")


def test_safe_attachment_name() -> None:
    safe = mbox_module._safe_attachment_name
    assert safe("../../evil.exe") == "evil.exe"
    assert safe("/tmp/absolute.pdf") == "absolute.pdf"
    assert safe("CON.txt") == "_CON.txt"
    assert safe("nul") == "_nul"
    assert safe(None) == "(unnamed)"
    assert safe("") == "(unnamed)"


def test_import_extracts_attachments(tmp_path: Path, mbox_file: Path) -> None:
    exports = tmp_path / "exports"
    log = tmp_path / "import.jsonl"
    counts = mbox_module.import_mbox(
        mbox_file, exports, log_path=log, extract_attachments=True
    )
    assert counts["attachments"] == 1
    assert counts["attachment_bytes"] == 9
    stem = "2026-07-21_1003_With attachment"
    extracted = exports / "Individual" / "sender_at_example_com" / f"{stem}.attachments"
    pdf = extracted / "report.pdf"
    assert pdf.read_bytes() == b"%PDF-1.4\n"
    report = exports / "Individual" / "sender_at_example_com" / f"{stem}.md"
    data = _frontmatter_of(report)
    assert data["AttachmentPaths"] == [f"{stem}.attachments/report.pdf"]
    assert "extracted to" in report.read_text(encoding="utf-8")
    records = [json.loads(line) for line in log.read_text().splitlines()]
    attached = next(r for r in records if r["event"] == "message" and r["attachments"])
    assert attached["attachments"][0]["extracted"] is True
    assert attached["attachments"][0]["path"].endswith("report.pdf")


def test_import_without_flag_extracts_nothing(tmp_path: Path, mbox_file: Path) -> None:
    exports = tmp_path / "exports"
    counts = mbox_module.import_mbox(mbox_file, exports)
    assert counts["attachments"] == 0
    assert list(exports.rglob("*.attachments")) == []
    report = (
        exports
        / "Individual"
        / "sender_at_example_com"
        / "2026-07-21_1003_With attachment.md"
    )
    assert _frontmatter_of(report)["AttachmentPaths"] == []
    assert "were not extracted" in report.read_text(encoding="utf-8")


def test_dry_run_with_flag_extracts_nothing(tmp_path: Path, mbox_file: Path) -> None:
    exports = tmp_path / "exports"
    log = tmp_path / "import.jsonl"
    counts = mbox_module.import_mbox(
        mbox_file, exports, dry_run=True, log_path=log, extract_attachments=True
    )
    assert counts["imported"] == 4
    assert counts["attachments"] == 0
    assert not exports.exists()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    attached = next(r for r in records if r["event"] == "message" and r["attachments"])
    assert attached["attachments"][0]["extracted"] is False
    assert ".attachments" in attached["attachments"][0]["path"]


def test_extraction_collision_and_traversal(tmp_path: Path) -> None:
    mbox_text = """From sender@example.com Mon Jul 21 10:00:00 2026
Message-ID: <multi@example.com>
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:00:00 -0400
Subject: Many attachments
Content-Type: multipart/mixed; boundary="M"

--M
Content-Type: text/plain; charset="utf-8"

See attached.
--M
Content-Type: application/pdf; name="report.pdf"
Content-Disposition: attachment; filename="report.pdf"
Content-Transfer-Encoding: base64

Zmlyc3Q=
--M
Content-Type: application/pdf; name="report.pdf"
Content-Disposition: attachment; filename="report.pdf"
Content-Transfer-Encoding: base64

c2Vjb25k
--M
Content-Type: application/octet-stream; name="evil.exe"
Content-Disposition: attachment; filename="../../evil.exe"
Content-Transfer-Encoding: base64

ZXZpbA==
--M--
"""
    mbox_file = tmp_path / "multi.mbox"
    mbox_file.write_text(mbox_text, encoding="utf-8")
    exports = tmp_path / "exports"
    counts = mbox_module.import_mbox(mbox_file, exports, extract_attachments=True)
    assert counts["attachments"] == 3
    attach_dir = next(exports.rglob("*.attachments"))
    names = sorted(p.name for p in attach_dir.iterdir())
    assert names == ["evil.exe", "report.pdf", "report_2.pdf"]
    assert (attach_dir / "report.pdf").read_bytes() == b"first"
    assert (attach_dir / "report_2.pdf").read_bytes() == b"second"
    # Nothing escaped the attachments directory.
    assert not (tmp_path / "evil.exe").exists()
    assert not (exports / "evil.exe").exists()


def test_md_attachment_is_not_a_report(tmp_path: Path) -> None:
    from gmail_toolkit import index_db
    from gmail_toolkit.viewer import _list_reports

    mbox_text = """From sender@example.com Mon Jul 21 10:00:00 2026
Message-ID: <mdattach@example.com>
From: Fictional Sender <sender@example.com>
Date: Mon, 21 Jul 2026 10:00:00 -0400
Subject: Markdown attachment
Content-Type: multipart/mixed; boundary="M"

--M
Content-Type: text/plain; charset="utf-8"

See attached.
--M
Content-Type: text/markdown; name="note.md"
Content-Disposition: attachment; filename="note.md"
Content-Transfer-Encoding: base64

IyBOb3QgYSByZXBvcnQK
--M--
"""
    mbox_file = tmp_path / "mdattach.mbox"
    mbox_file.write_text(mbox_text, encoding="utf-8")
    exports = tmp_path / "exports"
    mbox_module.import_mbox(mbox_file, exports, extract_attachments=True)
    attach_dir = next(exports.rglob("*.attachments"))
    assert (attach_dir / "note.md").read_text() == "# Not a report\n"
    counts = index_db.build_index(exports)
    assert counts["messages"] == 1
    assert len(_list_reports(exports)) == 1


def test_preview_attachment_totals(tmp_path: Path, mbox_file: Path) -> None:
    data = mbox_module.preview_mbox(mbox_file, tmp_path / "exports")
    assert data["attachment_files"] == 1
    assert data["attachment_bytes"] == 9
