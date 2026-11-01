"""Serve the read-only local viewer (localhost only)."""

from __future__ import annotations

from pathlib import Path

import typer

from ..viewer import create_app
from .validators import port_callback


def serve(
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
    port: int = typer.Option(
        5000,
        "--port",
        callback=port_callback,
        help="Local port to listen on (localhost only).",
    ),
) -> None:
    """Start the read-only viewer at http://127.0.0.1:<port>.

    There is deliberately no ``--host`` flag: the viewer serves real
    mailbox content, so it must never be exposed beyond this machine.
    """
    app = create_app(exports_dir)
    typer.echo(f"Viewer: http://127.0.0.1:{port} (read-only, localhost only)")
    typer.echo("Press Ctrl+C to stop.")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
