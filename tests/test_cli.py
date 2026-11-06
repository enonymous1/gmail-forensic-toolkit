"""End-to-end CLI tests against the fake Gmail service (no live Gmail)."""

from __future__ import annotations

import csv
import json

from typer.testing import CliRunner

from gmail_toolkit.cli import archive_sender as archive_module
from gmail_toolkit.cli import batch_archive, get_digest, organize_sender, scrape_senders
from gmail_toolkit.cli.main import app
from tests.conftest import FakeService, make_payload

runner = CliRunner()


def _patch_service(monkeypatch, module, service):
    monkeypatch.setattr(module, "get_gmail_service", lambda: service)


def test_cli_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["scrape", "--help"]).exit_code == 0
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "gmail-toolkit 2.10.0" in result.output


def test_scrape_writes_sorted_csv(monkeypatch, tmp_path, fake_service):
    _patch_service(monkeypatch, scrape_senders, fake_service)
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["scrape", "discovery", "--max-results", "10"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader((tmp_path / "exports" / "Contact_Directory.csv").open()))
    assert rows == [{"Sender": "Fictional Sender <sender@example.com>", "Count": "1"}]


def test_archive_writes_markdown_and_sources(monkeypatch, tmp_path, fake_service):
    _patch_service(monkeypatch, archive_module, fake_service)
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["archive", "contact", "sender@example.com"])
    assert result.exit_code == 0, result.output
    sender_dir = tmp_path / "exports" / "Individual" / "sender_at_example_com"
    reports = list(sender_dir.glob("*.md"))
    assert len(reports) == 1
    assert "Category**: Meeting" in reports[0].read_text(encoding="utf-8")
    assert list((sender_dir / "SourceFiles").glob("*.json"))


def test_archive_no_messages(monkeypatch, tmp_path):
    service = FakeService(pages={None: {"messages": []}})
    _patch_service(monkeypatch, archive_module, service)
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["archive", "contact", "nobody@example.com"])
    assert result.exit_code == 0
    assert "No messages found." in result.output


def test_organize_dry_run_makes_no_changes(monkeypatch, fake_service):
    _patch_service(monkeypatch, organize_sender, fake_service)
    result = runner.invoke(
        app, ["organize", "labels", "sender@example.com", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Dry run: would move 1 message(s)" in result.output
    assert fake_service.messages_api.batch_modify_calls == []
    assert fake_service.labels_api.created == []


def test_organize_apply_uses_batch_modify(monkeypatch, fake_service):
    _patch_service(monkeypatch, organize_sender, fake_service)
    result = runner.invoke(app, ["organize", "labels", "sender@example.com"])
    assert result.exit_code == 0, result.output
    assert "Moved 1 message(s)" in result.output
    assert len(fake_service.messages_api.batch_modify_calls) == 1
    body = fake_service.messages_api.batch_modify_calls[0]["body"]
    assert body["removeLabelIds"] == ["INBOX"]
    assert body["ids"] == ["msg-1"]


def test_digest_prints_table(monkeypatch, fake_service):
    _patch_service(monkeypatch, get_digest, fake_service)
    result = runner.invoke(app, ["digest", "summarize", "--days", "1", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "Security alert: meeting invite" in result.output


def test_restore_rebuilds_from_saved_json(tmp_path):
    payload = make_payload()
    target = tmp_path / "exports" / "Example" / "sender_at_example_com" / "SourceFiles"
    target.mkdir(parents=True)
    (target / "2026-07-21_1000_Subject.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    result = runner.invoke(app, ["restore", "rebuild", str(tmp_path / "exports")])
    assert result.exit_code == 0, result.output
    report = target.parent / "2026-07-21_1000_Subject.md"
    assert report.exists()
    assert "Category**: Meeting" in report.read_text(encoding="utf-8")


def test_restore_skips_payload_that_breaks_generation(tmp_path, monkeypatch):
    # A payload that parses but breaks Markdown generation must skip one
    # report, not abort the whole rebuild.
    import gmail_toolkit.cli.restore_metadata as restore_module

    payload = make_payload()
    target = tmp_path / "exports" / "Example" / "sender_at_example_com" / "SourceFiles"
    target.mkdir(parents=True)
    (target / "2026-07-21_1000_Subject.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    def _failing_generate(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(restore_module, "generate_forensic_markdown", _failing_generate)
    result = runner.invoke(app, ["restore", "rebuild", str(tmp_path / "exports")])
    assert result.exit_code == 0, result.output
    assert "Skipping" in result.output
    assert not (target.parent / "2026-07-21_1000_Subject.md").exists()


def test_restore_missing_path_exits_nonzero():
    result = runner.invoke(app, ["restore", "rebuild", "/definitely/not/here"])
    assert result.exit_code == 1


def test_batch_process_reads_csv_and_archives(monkeypatch, tmp_path, fake_service):
    _patch_service(monkeypatch, batch_archive, fake_service)
    exports = tmp_path / "exports"
    exports.mkdir()
    with (exports / "Contact_Directory.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Sender", "Count"])
        writer.writerow(["Fictional Sender <sender@example.com>", "1"])
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["batch", "process", "--batch-size", "1"])
    assert result.exit_code == 0, result.output
    assert list((tmp_path / "exports" / "Individual").rglob("*.md"))


def test_batch_process_accepts_brands_file(monkeypatch, tmp_path, fake_service):
    _patch_service(monkeypatch, batch_archive, fake_service)
    exports = tmp_path / "exports"
    exports.mkdir()
    brands = tmp_path / "brands.json"
    brands.write_text('{"example.com": "CustomBrand"}', encoding="utf-8")
    with (exports / "Contact_Directory.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Sender", "Count"])
        writer.writerow(["Fictional Sender <sender@example.com>", "1"])
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(
            app, ["batch", "process", "--batch-size", "1", "--brands", str(brands)]
        )
    assert result.exit_code == 0, result.output
    assert list((tmp_path / "exports" / "CustomBrand").rglob("*.md"))


def test_batch_process_missing_csv_exits_nonzero(monkeypatch, tmp_path):
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        result = runner.invoke(app, ["batch", "process"])
    assert result.exit_code == 1
