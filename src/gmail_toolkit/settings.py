"""Runtime settings for the toolkit.

One dataclass holds the numbers that used to be scattered magic constants
(page sizes, batch chunk sizes, retry budgets). Library and embedding
callers can pass a ``Settings`` instance to the API wrappers; the CLI uses
the defaults, so no new flags are needed to get the current behaviour.
Adding a user-facing setting later means adding a field here and a CLI
option that builds one, not hunting constants across modules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Tunable behaviour for the Gmail/Calendar wrappers."""

    gmail_page_size: int = 500
    calendar_page_size: int = 250
    batch_modify_chunk_size: int = 1000
    batch_request_size: int = 100
    max_attempts: int = 3
    retry_base_delay_seconds: float = 1.0


DEFAULT_SETTINGS = Settings()
