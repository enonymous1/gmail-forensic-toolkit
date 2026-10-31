"""Gmail Forensic Toolkit CLI entry point."""

from __future__ import annotations

import logging

import typer

from .. import __version__
from . import (
    archive_sender,
    batch_archive,
    calendar,
    get_digest,
    labels,
    organize_sender,
    restore_metadata,
    scrape_senders,
)

app = typer.Typer(help="Gmail Forensic Toolkit CLI")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"gmail-toolkit {__version__}")
        raise typer.Exit()


@app.callback()
def _callback(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        is_eager=True,
        help="Enable debug logging (retry warnings, conversion fallbacks).",
    ),
) -> None:
    """Gmail Forensic Toolkit CLI."""
    # Runs before every subcommand (pass it before the subcommand name:
    # ``gmail-toolkit --verbose scrape ...``). Without --verbose only
    # warnings and errors are logged; with it, debug records surface too.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )


app.add_typer(scrape_senders.app, name="scrape")
app.add_typer(archive_sender.app, name="archive")
app.add_typer(organize_sender.app, name="organize")
app.add_typer(batch_archive.app, name="batch")
app.add_typer(get_digest.app, name="digest")
app.add_typer(restore_metadata.app, name="restore")
app.add_typer(labels.app, name="labels")
app.add_typer(calendar.app, name="calendar")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
