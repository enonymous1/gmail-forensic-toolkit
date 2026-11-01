"""mbox import tests: synthetic .mbox files, no Gmail API, no live data."""

from __future__ import annotations

from pathlib import Path

import pytest
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
    assert counts == {"total": 5, "imported": 4, "duplicates": 1, "errors": 0}
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
    assert counts == {"total": 5, "imported": 0, "duplicates": 5, "errors": 0}
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
    assert counts == {"total": 5, "imported": 3, "duplicates": 1, "errors": 1}


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
