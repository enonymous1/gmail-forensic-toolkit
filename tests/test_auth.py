"""Tests for credential path resolution and token handling."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from gmail_toolkit import auth


def test_credential_paths_honour_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(tmp_path / "c.json"))
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "t.json"))
    credentials, token = auth.credential_paths()
    assert credentials == tmp_path / "c.json"
    assert token == tmp_path / "t.json"


def test_credential_paths_default_to_working_directory_credentials():
    credentials, token = auth.credential_paths()
    assert credentials == Path("credentials") / "credentials.json"
    assert token == Path("credentials") / "token.json"


def test_load_token_missing_and_corrupt_return_none(tmp_path):
    assert auth._load_token(tmp_path / "missing.json", auth.SCOPES) is None
    corrupt = tmp_path / "token.json"
    corrupt.write_text("not json", encoding="utf-8")
    assert auth._load_token(corrupt, auth.SCOPES) is None


def test_save_token_writes_restricted_file(tmp_path):
    token_path = tmp_path / "credentials" / "token.json"
    fake_creds = SimpleNamespace(to_json=lambda: '{"token": "x"}')
    auth._save_token(token_path, fake_creds)
    assert token_path.read_text(encoding="utf-8") == '{"token": "x"}'
    if os.name != "nt":
        # Windows has no POSIX mode bits: os.chmod there only toggles
        # read-only, so a 0600 assertion cannot hold (see _save_token).
        # The POSIX restriction itself is still asserted on every
        # POSIX platform, including the Ubuntu CI leg.
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_get_gmail_service_missing_client_secrets_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        auth,
        "credential_paths",
        lambda *args, **kwargs: (tmp_path / "c.json", tmp_path / "t.json"),
    )
    with pytest.raises(FileNotFoundError, match="Missing client secrets"):
        auth.get_gmail_service()
