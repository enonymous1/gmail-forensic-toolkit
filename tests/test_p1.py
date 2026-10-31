"""Tests for the P1/architecture work in 2.2.0."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from gmail_toolkit import __version__
from gmail_toolkit.cli import get_digest, organize_sender
from gmail_toolkit.cli import labels as labels_module
from gmail_toolkit.cli.main import app
from gmail_toolkit.cli.scrape_senders import discover_senders
from gmail_toolkit.gmail_api import (
    batch_get_messages,
    fetch_label_details,
    list_message_ids,
)
from gmail_toolkit.paths import write_text_atomic
from gmail_toolkit.process_email import (
    generate_forensic_markdown,
    headers_dict,
    parse_gmail_payload,
    save_source_files,
)
from gmail_toolkit.settings import Settings
from tests.conftest import FakeService, make_payload

runner = CliRunner()


def test_headers_dict_joins_duplicates_and_lowercases():
    payload = make_payload(
        extra_headers=[
            {"name": "Received", "value": "from a; x -0500"},
            {"name": "Received", "value": "from b; y +0200"},
        ]
    )
    headers = headers_dict(payload["payload"])
    assert headers["subject"] == "Security alert: meeting invite"
    assert "-0500" in headers["received"] and "+0200" in headers["received"]


def test_batch_get_messages_preserves_order_and_chunks():
    # FakeService only has msg-1; build a three-message service instead.
    payloads = {
        f"msg-{index}": make_payload(message_id=f"msg-{index}") for index in (1, 2, 3)
    }
    service = FakeService(
        pages={None: {"messages": [{"id": f"msg-{index}"} for index in (1, 2, 3)]}},
        messages=payloads,
    )
    results = batch_get_messages(
        service, ["msg-1", "msg-2", "msg-3"], settings=Settings(batch_request_size=2)
    )
    assert [result["id"] for result in results] == ["msg-1", "msg-2", "msg-3"]


def test_batch_falls_back_without_batch_support(monkeypatch, fake_service):
    monkeypatch.setattr(fake_service, "new_batch_http_request", None)
    results = batch_get_messages(fake_service, ["msg-1"])
    assert [result["id"] for result in results] == ["msg-1"]


def test_fetch_label_details_skips_transport_errors_and_keeps_order():
    labels = [
        {"id": "B", "name": "B"},
        {"id": "A", "name": "A"},
        {"id": "Broken", "name": "Broken"},
    ]
    service = FakeService(
        labels=labels,
        label_details={
            "B": {"id": "B", "name": "B"},
            "A": {"id": "A", "name": "A"},
            "Broken": OSError("connection dropped"),
        },
    )
    assert [detail["id"] for detail in fetch_label_details(service, labels)] == [
        "B",
        "A",
    ]


def test_write_text_atomic_leaves_no_temp_file(tmp_path):
    target = tmp_path / "report.md"
    write_text_atomic(target, "one")
    write_text_atomic(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_negative_options_are_rejected():
    assert (
        runner.invoke(app, ["scrape", "discovery", "--max-results", "-5"]).exit_code
        != 0
    )
    assert runner.invoke(app, ["digest", "summarize", "--days", "0"]).exit_code != 0
    assert runner.invoke(app, ["batch", "process", "--skip", "-1"]).exit_code != 0


def test_settings_override_page_size():
    service = FakeService(
        pages={
            None: {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
            "p2": {"messages": [{"id": "c"}]},
        }
    )
    assert list_message_ids(
        service, max_results=1, settings=Settings(gmail_page_size=1)
    ) == ["a"]


def test_exports_carry_toolkit_version(payload):
    data = parse_gmail_payload(payload)
    assert f"ToolkitVersion: {__version__}" in generate_forensic_markdown(
        data, "Example"
    )


def test_cached_json_carries_toolkit_version(tmp_path, payload):
    data = parse_gmail_payload(payload)
    save_source_files(data, tmp_path, "2026-07-21_1000_Subject")
    cached = json.loads(
        (tmp_path / "SourceFiles" / "2026-07-21_1000_Subject.json").read_text(
            encoding="utf-8"
        )
    )
    assert cached["id"] == "msg-1"
    assert cached["toolkit_version"] == __version__


def test_version_matches_pyproject():
    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match is not None
    assert match.group(1) == __version__


def test_core_functions_run_without_monkeypatching(fake_service):
    assert discover_senders(fake_service, max_results=500) == [
        ("Fictional Sender <sender@example.com>", 1)
    ]


def test_organize_core_dry_run_makes_no_changes(fake_service):
    label_path, count = organize_sender.organize_sender(
        fake_service, "sender@example.com", dry_run=True
    )
    assert label_path == "Archive-Brands/Individual/sender@example.com"
    assert count == 1
    assert fake_service.messages_api.batch_modify_calls == []


def test_digest_core_returns_rows(fake_service):
    rows = get_digest.summarize_activity(fake_service, days=1, limit=5)
    assert rows[0]["Subject"] == "Security alert: meeting invite"


def test_labels_core_writes_inventory(tmp_path):
    service = FakeService(labels=[{"id": "INBOX", "name": "INBOX", "type": "system"}])
    output = tmp_path / "labels.json"
    assert labels_module.export_inventory(service, output) == 1
    assert json.loads(output.read_text(encoding="utf-8"))[0]["name"] == "INBOX"
