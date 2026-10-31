"""Shared Typer option validators.

Numeric options used to be unvalidated: ``--max-results -5`` printed
"Scanning last -5 messages" and wrote an empty CSV, and ``--days -1``
built a ``newer_than:-1d`` query that failed at the API with a traceback.
These callbacks reject nonsense at the CLI boundary with a usage error
instead, before any API call happens.
"""

from __future__ import annotations

from typing import Any

import typer


def positive_int_callback(ctx: Any, param: Any, value: int | None) -> int | None:
    """Require a positive integer (``None`` passes through for optional flags)."""
    if value is not None and value <= 0:
        raise typer.BadParameter("must be a positive integer")
    return value


def non_negative_int_callback(ctx: Any, param: Any, value: int | None) -> int | None:
    """Require a non-negative integer (``None`` passes through)."""
    if value is not None and value < 0:
        raise typer.BadParameter("must not be negative")
    return value
