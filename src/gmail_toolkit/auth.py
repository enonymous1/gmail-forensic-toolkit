"""Gmail (and Google Calendar) OAuth2 authentication.

Credentials live outside the repository, under ``credentials/`` in the
working directory by default (that directory is git-ignored). Override the
locations with ``GMAIL_CREDENTIALS_FILE`` / ``GMAIL_TOKEN_FILE`` or the
``credentials_path`` / ``token_path`` arguments. Nothing in this module
reads a ``.env`` file: the original README advertised ``GMAIL_SCOPES`` and
``DEBUG`` variables that no code ever honoured.

Gmail and Calendar share one token file. A stored token only carries the
scopes granted when it was created, so the first Calendar command after
only Gmail use needs a fresh grant: delete the token file and sign in
again when prompted.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .services import CalendarService, GmailService

# If modifying these SCOPES, delete the token file: a stored token only
# carries the scopes granted when it was created.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
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


def _authenticate(
    credentials_path: str | Path | None,
    token_path: str | Path | None,
    scopes: list[str],
) -> Credentials:
    """Load, refresh, or freshly grant OAuth credentials for *scopes*."""
    client_secrets_path, resolved_token_path = credential_paths(
        credentials_path, token_path
    )

    creds = _load_token(resolved_token_path, scopes)

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
                str(client_secrets_path), scopes
            )
            creds = flow.run_local_server(port=0)
        _save_token(resolved_token_path, creds)

    return creds


def get_gmail_service(
    credentials_path: str | Path | None = None,
    token_path: str | Path | None = None,
    scopes: list[str] | None = None,
) -> GmailService:
    """Authenticate the user and return a Gmail API service instance.

    Handles token refreshing and the initial OAuth2 flow, and saves the
    resulting token for the next run. A refresh failure falls through to a
    fresh login instead of surfacing a raw :class:`RefreshError`.
    """
    active_scopes = list(scopes) if scopes is not None else list(SCOPES)
    creds = _authenticate(credentials_path, token_path, active_scopes)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_calendar_service(
    credentials_path: str | Path | None = None,
    token_path: str | Path | None = None,
    scopes: list[str] | None = None,
) -> CalendarService:
    """Authenticate the user and return a Calendar API service instance.

    Shares the Gmail token file (see the module docstring): a token granted
    for Gmail scopes alone does not carry ``calendar.readonly``, so the
    first Calendar command may need the token file deleted and a fresh
    sign-in. The Calendar API must be enabled for the Cloud project,
    separately from the Gmail API.
    """
    active_scopes = list(scopes) if scopes is not None else list(CALENDAR_SCOPES)
    creds = _authenticate(credentials_path, token_path, active_scopes)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
