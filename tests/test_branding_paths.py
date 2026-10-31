"""Tests for branding resolution and path helpers."""

from __future__ import annotations

from datetime import datetime

import pytest

from gmail_toolkit.branding import (
    extract_domain,
    load_brands,
    resolve_brand,
    resolve_brand_from_map,
)
from gmail_toolkit.paths import (
    get_base_filename,
    next_available_path,
    sanitize_filename,
)


def test_extract_domain_handles_display_names_and_case():
    assert extract_domain("Fictional <Sender@Example.COM>") == "example.com"
    assert extract_domain("sender@example.com") == "example.com"
    assert extract_domain("not-an-address") == ""


def test_resolve_brand_subdomain_and_default():
    brands = {"example.com": "Example"}
    assert resolve_brand_from_map("a@mail.example.com", brands) == "Example"
    assert resolve_brand("a@mail.example.com", None) == "Individual"
    assert resolve_brand("a@example.com") == "Individual"
    assert resolve_brand("a@google.com") == "Google"
    assert resolve_brand("a@mail.google.com") == "Google"
    assert resolve_brand("not-an-address") == "Individual"


def test_load_brands_missing_file_uses_defaults_and_invalid_raises(tmp_path):
    assert load_brands(tmp_path / "missing.json")["google.com"] == "Google"
    assert load_brands(None)["github.com"] == "GitHub"
    bad = tmp_path / "brands.json"
    bad.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(ValueError):
        load_brands(bad)
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_brands(malformed)


def test_sanitize_filename_and_base_filename():
    assert sanitize_filename('a/b:c"d*e?f<g>h|i') == "abcdefghi"
    assert sanitize_filename("  name.  ") == "name"
    assert sanitize_filename("///") == "untitled"
    stem = get_base_filename(datetime(2026, 7, 21, 10, 0), "Hello: World")
    assert stem == "2026-07-21_1000_Hello World"


def test_next_available_path_avoids_overwriting(tmp_path):
    first = tmp_path / "report.md"
    first.write_text("one", encoding="utf-8")
    second = next_available_path(first)
    assert second.name == "report_2.md"
    assert next_available_path(tmp_path / "free.md").name == "free.md"
