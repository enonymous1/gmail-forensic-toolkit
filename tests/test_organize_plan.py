"""Tests for `organize plan` / `organize apply` (v2.9.0).

Plan generation reads report frontmatter only (no Gmail calls). Apply
tests use a stateful fake service: its list() filters by the query's
sender and INBOX membership, and batchModify() really moves the labels,
so idempotent re-runs and keep-inbox behaviour are exercised for real.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gmail_toolkit.cli import organize_sender
from gmail_toolkit.cli.main import app

runner = CliRunner()


class _Req:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class _StatefulMessages:
    """Messages keyed by id; list() honours from:/in:inbox, batchModify moves labels."""

    def __init__(self, messages: dict[str, dict[str, Any]]) -> None:
        self.messages = messages
        self.batch_modify_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Req:
        query = kwargs.get("q") or ""
        match = re.search(r"from:(\S+)", query)
        sender = match.group(1) if match else None
        inbox_only = "in:inbox" in query
        ids = [
            mid
            for mid, msg in self.messages.items()
            if (sender is None or msg["sender"] == sender)
            and (not inbox_only or "INBOX" in msg["labels"])
        ]
        max_results = kwargs.get("maxResults")
        if max_results is not None:
            ids = ids[:max_results]
        return _Req({"messages": [{"id": mid} for mid in ids]})

    def batchModify(self, **kwargs: Any) -> _Req:
        body = kwargs["body"]
        self.batch_modify_calls.append(body)
        for mid in body["ids"]:
            labels = self.messages[mid]["labels"]
            for added in body.get("addLabelIds", []):
                if added not in labels:
                    labels.append(added)
            for removed in body.get("removeLabelIds", []):
                if removed in labels:
                    labels.remove(removed)
        return _Req({})


class _StatefulLabels:
    def __init__(self, labels: list[dict[str, str]] | None = None) -> None:
        self.labels = list(labels or [])
        self.created: list[dict[str, Any]] = []
        self.list_calls = 0
        self._next = 0

    def list(self, **kwargs: Any) -> _Req:
        self.list_calls += 1
        return _Req({"labels": list(self.labels)})

    def create(self, **kwargs: Any) -> _Req:
        body = kwargs["body"]
        self._next += 1
        created = {"id": f"label-{self._next}", "name": body["name"]}
        self.created.append(body)
        self.labels.append(created)
        return _Req(created)


class _StatefulUsers:
    def __init__(self, messages: _StatefulMessages, labels: _StatefulLabels) -> None:
        self._messages = messages
        self._labels = labels

    def messages(self) -> _StatefulMessages:
        return self._messages

    def labels(self) -> _StatefulLabels:
        return self._labels


class _StatefulService:
    def __init__(
        self,
        messages: dict[str, dict[str, Any]],
        labels: list[dict[str, str]] | None = None,
    ) -> None:
        self.messages_api = _StatefulMessages(messages)
        self.labels_api = _StatefulLabels(labels)

    def users(self) -> _StatefulUsers:
        return _StatefulUsers(self.messages_api, self.labels_api)


def _inbox_messages() -> dict[str, dict[str, Any]]:
    return {
        "m1": {"sender": "a@example.com", "labels": ["INBOX"]},
        "m2": {"sender": "a@example.com", "labels": ["INBOX"]},
        "m3": {"sender": "b@other.com", "labels": ["INBOX"]},
        "m4": {"sender": "c@other.com", "labels": ["INBOX"]},
    }


def _plan() -> dict[str, Any]:
    return {
        "version": 1,
        "label_scheme": "Archive-Brands/{brand}/{email}",
        "entries": [
            {
                "email": "a@example.com",
                "brand": "ExampleBrand",
                "label_path": "Archive-Brands/ExampleBrand/a@example.com",
                "takeout_count": 3,
            },
            {
                "email": "b@other.com",
                "brand": "OtherBrand",
                "label_path": "Archive-Brands/OtherBrand/b@other.com",
                "takeout_count": 1,
            },
        ],
    }


def _write_report(path: Path, sender: str, brand: str = "Example") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nBrand: {brand}\nSender: {sender}\nSubject: x\n---\n\nBody\n",
        encoding="utf-8",
    )


def _exports_tree(tmp_path: Path) -> tuple[Path, Path]:
    exports = tmp_path / "exports"
    alice_dir = exports / "Example" / "a_at_example_com"
    _write_report(alice_dir / "r1.md", "Alice A <a@example.com>")
    _write_report(alice_dir / "r2.md", "a@example.com")
    _write_report(alice_dir / "r3.md", "A. Different <a@example.com>")
    _write_report(
        exports / "Other" / "b_at_other_com" / "r1.md",
        "Bob <b@other.com>",
        brand="Other",
    )
    _write_report(
        exports / "Other" / "unknown" / "r1.md",
        "Unknown Sender",
        brand="Other",
    )
    # An .md attachment must never become a plan entry.
    _write_report(
        exports / "Example" / "a_at_example_com" / "r1.attachments" / "note.md",
        "Carol <c@other.com>",
    )
    brands = tmp_path / "brands.json"
    brands.write_text(
        json.dumps({"example.com": "ExampleBrand", "other.com": "OtherBrand"}),
        encoding="utf-8",
    )
    return exports, brands


def test_plan_groups_by_email_and_resolves_current_brands(tmp_path):
    exports, brands = _exports_tree(tmp_path)
    plan, stats = organize_sender.build_plan(exports, brands)
    assert plan["version"] == 1
    assert [e["email"] for e in plan["entries"]] == ["a@example.com", "b@other.com"]
    first, second = plan["entries"]
    # Display-name variants collapse; brand comes from the brands file,
    # not the reports' frontmatter Brand.
    assert first["takeout_count"] == 3
    assert first["brand"] == "ExampleBrand"
    assert first["label_path"] == "Archive-Brands/ExampleBrand/a@example.com"
    assert second["takeout_count"] == 1
    assert second["label_path"] == "Archive-Brands/OtherBrand/b@other.com"
    assert stats == {"reports": 5, "senders": 2, "skipped": 1}


def test_plan_min_count_filters_small_senders(tmp_path):
    exports, brands = _exports_tree(tmp_path)
    plan, _stats = organize_sender.build_plan(exports, brands, min_count=3)
    assert [e["email"] for e in plan["entries"]] == ["a@example.com"]


def test_plan_empty_exports(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    plan, stats = organize_sender.build_plan(exports)
    assert plan["entries"] == []
    assert stats == {"reports": 0, "senders": 0, "skipped": 0}


def test_apply_dry_run_changes_nothing(tmp_path):
    service = _StatefulService(_inbox_messages())
    summary = organize_sender.apply_plan(
        service, _plan(), dry_run=True, journal_path=tmp_path / "apply.jsonl"
    )
    assert summary["found"] == 3
    assert summary["moved"] == 0
    assert service.labels_api.created == []
    assert service.messages_api.batch_modify_calls == []
    assert all("INBOX" in m["labels"] for m in service.messages_api.messages.values())
    records = [
        json.loads(line)
        for line in (tmp_path / "apply.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["type"] == "summary"
    assert records[-1]["found"] == 3
    assert all(r["outcome"] == "dry-run" for r in records[:-1])


def test_apply_moves_and_lists_labels_once():
    service = _StatefulService(_inbox_messages())
    summary = organize_sender.apply_plan(service, _plan())
    assert summary == {
        "entries": 2,
        "applied": 2,
        "no_messages": 0,
        "errors": 0,
        "found": 3,
        "moved": 3,
        "dry_run": False,
        "keep_inbox": False,
    }
    # One list call total (the cache), not one per sender.
    assert service.labels_api.list_calls == 1
    assert len(service.labels_api.created) == 2
    calls = service.messages_api.batch_modify_calls
    assert len(calls) == 2
    assert calls[0]["ids"] == ["m1", "m2"]
    assert calls[0]["removeLabelIds"] == ["INBOX"]
    assert calls[1]["ids"] == ["m3"]
    # Untouched sender keeps INBOX; moved ones lose it.
    assert service.messages_api.messages["m4"]["labels"] == ["INBOX"]
    assert "INBOX" not in service.messages_api.messages["m1"]["labels"]


def test_apply_reuses_existing_label():
    service = _StatefulService(
        _inbox_messages(),
        labels=[
            {"id": "label-pre", "name": "Archive-Brands/ExampleBrand/a@example.com"}
        ],
    )
    organize_sender.apply_plan(service, _plan())
    assert len(service.labels_api.created) == 1
    assert service.labels_api.created[0]["name"] == (
        "Archive-Brands/OtherBrand/b@other.com"
    )
    assert "label-pre" in service.messages_api.messages["m1"]["labels"]


def test_apply_keep_inbox_and_limit():
    service = _StatefulService(_inbox_messages())
    summary = organize_sender.apply_plan(service, _plan(), keep_inbox=True, limit=1)
    assert summary["moved"] == 2
    calls = service.messages_api.batch_modify_calls
    assert [call["ids"] for call in calls] == [["m1"], ["m3"]]
    assert all(call["removeLabelIds"] == [] for call in calls)
    # Limit hit: m1 labelled but kept in Inbox; m2 never touched.
    assert "INBOX" in service.messages_api.messages["m1"]["labels"]
    assert service.messages_api.messages["m2"]["labels"] == ["INBOX"]


def test_apply_rerun_is_noop():
    service = _StatefulService(_inbox_messages())
    organize_sender.apply_plan(service, _plan())
    second = organize_sender.apply_plan(service, _plan())
    assert second["moved"] == 0
    assert second["no_messages"] == 2
    assert len(service.labels_api.created) == 2
    assert len(service.messages_api.batch_modify_calls) == 2


def test_apply_skips_bad_entries_and_derives_missing_path():
    service = _StatefulService(_inbox_messages())
    plan = {
        "version": 1,
        "entries": [
            {"email": "a@example.com", "brand": "ExampleBrand"},
            {"email": "no-label@example.com"},
            "not-an-object",
            {
                "email": "b@other.com",
                "label_path": "Archive-Brands/OtherBrand/b@other.com",
            },
        ],
    }
    summary = organize_sender.apply_plan(service, plan)
    assert summary["applied"] == 2
    assert summary["errors"] == 2
    assert summary["moved"] == 3
    created = [c["name"] for c in service.labels_api.created]
    assert "Archive-Brands/ExampleBrand/a@example.com" in created


def test_apply_rejects_plan_without_entries():
    with pytest.raises(ValueError, match="no entries list"):
        organize_sender.apply_plan(_StatefulService(_inbox_messages()), {"version": 1})


def test_plan_command_writes_file(tmp_path):
    exports, brands = _exports_tree(tmp_path)
    output = tmp_path / "plan.json"
    result = runner.invoke(
        app,
        [
            "organize",
            "plan",
            "--exports-dir",
            str(exports),
            "--output",
            str(output),
            "--brands",
            str(brands),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2 sender(s) from 5 report(s), 1 skipped" in result.output
    data = json.loads(output.read_text(encoding="utf-8"))
    assert [e["email"] for e in data["entries"]] == ["a@example.com", "b@other.com"]


def test_apply_command_missing_file_is_clean_error(tmp_path):
    result = runner.invoke(app, ["organize", "apply", str(tmp_path / "nope.json")])
    assert result.exit_code == 1
    assert "cannot read plan file" in result.output


def test_apply_command_dry_run_via_cli(monkeypatch, tmp_path):
    service = _StatefulService(_inbox_messages())
    monkeypatch.setattr(organize_sender, "get_gmail_service", lambda: service)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    result = runner.invoke(app, ["organize", "apply", str(plan_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run: 3 message(s) would move" in result.output
    assert service.messages_api.batch_modify_calls == []


def test_organize_help_lists_plan_and_apply():
    result = runner.invoke(app, ["organize", "--help"])
    assert result.exit_code == 0
    assert "plan" in result.output
    assert "apply" in result.output
