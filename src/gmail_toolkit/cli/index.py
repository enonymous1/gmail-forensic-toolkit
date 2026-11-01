"""Build and query a SQLite search index over ``exports/``.

Phase 1 of the index: CLI only. The viewer is deliberately not wired
to it yet — the file-based viewer keeps working untouched, and the
index proves its value standalone first.

The index is a derived cache, not a store (see ``index_db``): rebuild
it any time with ``index build``, refresh it incrementally with
``index update`` after archiving or importing more reports.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from ..index_db import (
    build_index,
    default_db_path,
    index_stats,
    search_index,
    update_index,
)
from .validators import positive_int_callback

app = typer.Typer(help="Build and query a SQLite search index over exports/.")


def _resolve_db(exports_dir: Path, db: Path | None) -> Path:
    return db if db is not None else default_db_path(exports_dir)


def _handle_db_error(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1)


@app.command()
def build(
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Index path (default: <exports-dir>/index.db)."
    ),
) -> None:
    """Full rebuild of the index from all of ``exports/``."""
    db_path = _resolve_db(exports_dir, db)
    try:
        counts = build_index(exports_dir, db_path)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        _handle_db_error(exc)
        return
    typer.echo(
        f"Indexed {counts['messages']} message(s), {counts['labels']} label(s),"
        f" {counts['events']} event(s) into {db_path}."
    )


@app.command()
def update(
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Index path (default: <exports-dir>/index.db)."
    ),
) -> None:
    """Incremental update: re-parse only new, changed, or gone reports.

    With no database yet this is a full build.
    """
    db_path = _resolve_db(exports_dir, db)
    try:
        counts = update_index(exports_dir, db_path)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        _handle_db_error(exc)
        return
    typer.echo(
        f"Index updated: {counts['added']} added, {counts['updated']} updated,"
        f" {counts['removed']} removed ({counts['messages']} total),"
        f" {counts['labels']} label(s), {counts['events']} event(s)."
    )


@app.command()
def search(
    query: str = typer.Argument(
        ..., help="Search terms, ANDed (matched against brand, sender, subject, body)."
    ),
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Index path (default: <exports-dir>/index.db)."
    ),
    brand: str | None = typer.Option(None, "--brand", help="Only this brand."),
    sender: str | None = typer.Option(None, "--sender", help="Sender substring match."),
    limit: int = typer.Option(
        20,
        "--limit",
        callback=positive_int_callback,
        help="Maximum results.",
    ),
) -> None:
    """Full-text search over indexed reports, best matches first."""
    db_path = _resolve_db(exports_dir, db)
    try:
        results = search_index(db_path, query, brand=brand, sender=sender, limit=limit)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        _handle_db_error(exc)
        return
    if not results:
        typer.echo("No matches.")
        return
    for result in results:
        typer.echo(
            f"{result['date']} | {result['brand']} | {result['sender']} |"
            f" {result['subject']}"
        )
        typer.echo(f"  {result['report_path']}")
        if result.get("snippet"):
            typer.echo(f"  …{result['snippet']}…")


@app.command()
def stats(
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Index path (default: <exports-dir>/index.db)."
    ),
) -> None:
    """Totals and breakdowns from the index."""
    db_path = _resolve_db(exports_dir, db)
    try:
        data = index_stats(db_path)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        _handle_db_error(exc)
        return
    typer.echo(
        f"Messages: {data['messages']} | Labels: {data['labels']}"
        f" | Events: {data['events']}"
    )
    for heading, rows in (
        ("By brand", data["by_brand"]),
        ("By type", data["by_type"]),
        ("By scope", data["by_scope"]),
        ("Top senders", data["top_senders"]),
    ):
        typer.echo(f"{heading}:")
        if not rows:
            typer.echo("  (none)")
        for name, count in rows:
            typer.echo(f"  {name or '(blank)'}: {count}")
    typer.echo(
        f"Tracking: {data['tracking_pixels']} pixel(s),"
        f" {data['tracking_links']} link(s)"
    )
    start, end = data["date_span"]
    if start is None:
        typer.echo("Date span: unknown (no dated reports)")
    else:
        typer.echo(f"Date span: {start} to {end}")
