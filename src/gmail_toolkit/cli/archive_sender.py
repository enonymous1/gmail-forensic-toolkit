"""Archive every message from one sender as forensic Markdown."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from ..auth import get_gmail_service
from ..branding import resolve_brand
from ..gmail_api import get_message, list_message_ids
from ..paths import (
    ensure_directory,
    get_base_filename,
    next_available_path,
    sanitize_filename,
)
from ..process_email import (
    generate_forensic_markdown,
    parse_gmail_payload,
    save_source_files,
)

app = typer.Typer(help="Archive emails from a specific sender.")


def archive_contact(
    service: Any,
    email: str,
    limit: int = 50,
    brands_path: Path | None = None,
    exports_dir: Path = Path("exports"),
) -> int:
    """Archive messages from *email*; return the number archived."""
    if brands_path is not None and not Path(brands_path).exists():
        # A mistyped --brands path must not silently fall back to the
        # built-in brands: say so, then fall back (load_brands' contract).
        typer.echo(
            f"Warning: brands file not found: {brands_path}; using built-in defaults."
        )
    query = f"from:{email}"
    typer.echo(f"Archiving messages from: {email}")

    message_ids = list_message_ids(service, query=query, max_results=limit)
    if not message_ids:
        typer.echo("No messages found.")
        return 0

    brand = resolve_brand(email, brands_path)
    sender_clean = sanitize_filename(email.replace("@", "_at_").replace(".", "_"))
    sender_dir = ensure_directory(exports_dir / brand / sender_clean)

    archived = 0
    for message_id in message_ids:
        raw_msg = get_message(service, message_id, message_format="full")
        email_data = parse_gmail_payload(raw_msg)
        base_name = get_base_filename(email_data["date"], email_data["subject"])

        md_path = next_available_path(sender_dir / f"{base_name}.md")
        md_path.write_text(
            generate_forensic_markdown(email_data, brand), encoding="utf-8"
        )
        # Source files share the stem of the Markdown file actually written,
        # so a collision-suffixed report still pairs with its sources.
        save_source_files(email_data, sender_dir, md_path.stem)
        typer.echo(f"  Archived: {md_path.name}")
        archived += 1
    return archived


@app.command()
def contact(
    email: str = typer.Argument(..., help="Sender email address to archive."),
    limit: int = typer.Option(50, "--limit", help="Maximum messages to archive."),
    brands_path: Path | None = typer.Option(
        None, "--brands", help="Brands JSON file (defaults to built-in brands)."
    ),
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
) -> None:
    """Archive all messages from a specific sender."""
    service = get_gmail_service()
    archive_contact(
        service, email, limit=limit, brands_path=brands_path, exports_dir=exports_dir
    )


if __name__ == "__main__":
    app()
