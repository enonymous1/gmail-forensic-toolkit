"""Gmail OAuth2 authentication.

Credentials live outside the repository, under ``credentials/`` in the
working directory by default (that directory is git-ignored). Override the
locations with ``GMAIL_CREDENTIALS_FILE`` / ``GMAIL_TOKEN_FILE`` or the
``credentials_path`` / ``token_path`` arguments. Nothing in this module
reads a ``.env`` file: the original README advertised ``GMAIL_SCOPES`` and
``DEBUG`` variables that no code ever honoured.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# If modifying these SCOPES, delete the token file: a stored token only
# carries the scopes granted when it was created.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

_DEFAULT_CREDENTIALS = Path("credentials") / "credentials.json"
_DEFAULT_TOKEN = Path("credentials") / "token.json"


def credential_paths(
    credentials_path: str | Path | None = None,
    token_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Resolve the client-secrets and token paths, honouring env overrides."""
    credentials = credentials_path or os.environ.get("GMAIL_CREDENTIALS_FILE")
    token = token_path or os.environ.get("GMAIL_TOKEN_FILE")
    return (
        Path(credentials) if credentials else _DEFAULT_CREDENTIALS,
        Path(token) if token else _DEFAULT_TOKEN,
    )


def _load_token(token_path: Path, scopes: list[str]) -> Credentials | None:
    if not token_path.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_path), scopes)
    except (ValueError, OSError):
        # A corrupt or hand-edited token file must trigger re-authentication,
        # not crash every command.
        return None


def _save_token(token_path: Path, creds: Credentials) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(token_path, 0o600)


def get_gmail_service(
    credentials_path: str | Path | None = None,
    token_path: str | Path | None = None,
    scopes: list[str] | None = None,
) -> Any:
    """Authenticate the user and return a Gmail API service instance.

    Handles token refreshing and the initial OAuth2 flow, and saves the
    resulting token for the next run. A refresh failure falls through to a
    fresh login instead of surfacing a raw :class:`RefreshError`.
    """
    active_scopes = list(scopes) if scopes is not None else list(SCOPES)
    client_secrets_path, resolved_token_path = credential_paths(
        credentials_path, token_path
    )

    creds = _load_token(resolved_token_path, active_scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except (RefreshError, OSError, ValueError):
                creds = None
        if not creds or not creds.valid:
            if not client_secrets_path.exists():
                raise FileNotFoundError(
                    f"Missing client secrets file at {client_secrets_path}. "
                    "Please download it from the Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secrets_path), active_scopes
            )
            creds = flow.run_local_server(port=0)
        _save_token(resolved_token_path, creds)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)
