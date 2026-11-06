"""Archive every message from one sender as forensic Markdown."""

from __future__ import annotations

import hashlib
from pathlib import Path

import typer

from ..auth import get_gmail_service
from ..branding import resolve_brand
from ..gmail_api import batch_get_messages, decode_raw_message, list_message_ids
from ..paths import (
    get_base_filename,
    get_sender_directory,
    next_available_path,
    write_bytes_atomic,
    write_text_atomic,
)
from ..process_email import (
    generate_forensic_markdown,
    parse_gmail_payload,
    save_source_files,
)
from ..services import GmailService
from .validators import positive_int_callback

app = typer.Typer(help="Archive emails from a specific sender.")


def archive_contact(
    service: GmailService,
    email: str,
    limit: int = 50,
    brands_path: Path | None = None,
    exports_dir: Path = Path("exports"),
    save_eml: bool = False,
) -> int:
    """Archive messages from *email*; return the number archived.

    With ``save_eml`` set, each message is also fetched in ``raw``
    format (one extra batched fetch for the run, in the same order)
    and its bytes saved beside its report as ``<stem>.eml``, with the
    filename and SHA-256 recorded in frontmatter. Off by default:
    the raw fetch doubles the messages.get calls for the run.
    """
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
    sender_dir = get_sender_directory(exports_dir, brand, email)

    full_messages = batch_get_messages(service, message_ids)
    raw_messages = (
        batch_get_messages(service, message_ids, message_format="raw")
        if save_eml
        else [None] * len(full_messages)
    )
    if len(raw_messages) != len(full_messages):
        raise ValueError(
            f"Raw fetch returned {len(raw_messages)} messages for "
            f"{len(full_messages)} archived; refusing to pair them."
        )

    archived = 0
    # One batch call per chunk of messages, not one get per message.
    for raw_msg, raw_payload in zip(full_messages, raw_messages, strict=True):
        email_data = parse_gmail_payload(raw_msg)
        base_name = get_base_filename(email_data["date"], email_data["subject"])

        md_path = next_available_path(sender_dir / f"{base_name}.md")
        extras: dict[str, object] | None = None
        if save_eml:
            if not isinstance(raw_payload, dict) or not raw_payload.get("raw"):
                raise ValueError(
                    f"Raw payload missing for message {raw_msg.get('id')}; "
                    "refusing to write a report without its .eml."
                )
            raw_bytes = decode_raw_message(str(raw_payload["raw"]))
            eml_name = f"{md_path.stem}.eml"
            # Write the evidence before the report that references it.
            write_bytes_atomic(sender_dir / eml_name, raw_bytes)
            extras = {
                "RawFile": eml_name,
                "RawSha256": hashlib.sha256(raw_bytes).hexdigest(),
            }
        write_text_atomic(
            md_path, generate_forensic_markdown(email_data, brand, extras)
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
    limit: int = typer.Option(
        50,
        "--limit",
        callback=positive_int_callback,
        help="Maximum messages to archive.",
    ),
    brands_path: Path | None = typer.Option(
        None, "--brands", help="Brands JSON file (defaults to built-in brands)."
    ),
    exports_dir: Path = typer.Option(
        Path("exports"), "--exports-dir", help="Root directory for exports."
    ),
    save_eml: bool = typer.Option(
        False,
        "--save-eml",
        help="Fetch each message raw too; save <stem>.eml beside its report.",
    ),
) -> None:
    """Archive all messages from a specific sender."""
    service = get_gmail_service()
    archive_contact(
        service,
        email,
        limit=limit,
        brands_path=brands_path,
        exports_dir=exports_dir,
        save_eml=save_eml,
    )


if __name__ == "__main__":
    app()
