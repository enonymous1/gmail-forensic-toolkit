"""Gmail Forensic Toolkit CLI entry point."""

from __future__ import annotations

import typer

from .. import __version__
from . import (
    archive_sender,
    batch_archive,
    get_digest,
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
) -> None:
    """Gmail Forensic Toolkit CLI."""


app.add_typer(scrape_senders.app, name="scrape")
app.add_typer(archive_sender.app, name="archive")
app.add_typer(organize_sender.app, name="organize")
app.add_typer(batch_archive.app, name="batch")
app.add_typer(get_digest.app, name="digest")
app.add_typer(restore_metadata.app, name="restore")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
