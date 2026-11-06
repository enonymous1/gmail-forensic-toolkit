"""Rebuild forensic Markdown from locally saved Gmail JSON payloads."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..paths import write_text_atomic
from ..process_email import (
    generate_forensic_markdown,
    parse_gmail_payload,
    read_report_frontmatter,
)

app = typer.Typer(help="Rebuild metadata from local JSON.")


def _preserved_raw_link(parent: Path, stem: str) -> dict[str, object]:
    """Carry a report's ``RawFile``/``RawSha256`` link across a rebuild.

    Regenerating from the JSON payload cannot know about the saved
    ``.eml`` beside the old report, so without this the rebuilt report
    would drop the link (and the index would drop the catalog row for
    the file, even though the file is still there). Only preserved
    when the ``.eml`` still exists beside the report.
    """
    old = read_report_frontmatter(parent / f"{stem}.md")
    raw_file = old.get("RawFile")
    if not isinstance(raw_file, str) or not raw_file:
        return {}
    if not (parent / raw_file).is_file():
        return {}
    extras: dict[str, object] = {"RawFile": raw_file}
    raw_sha = old.get("RawSha256")
    if isinstance(raw_sha, str) and raw_sha:
        extras["RawSha256"] = raw_sha
    return extras


@app.command()
def rebuild(
    target_path: Path = typer.Argument(
        Path("exports"), help="Exports directory to scan for SourceFiles."
    ),
) -> None:
    """Offline engine: re-parse local JSON payloads into updated Markdown.

    The brand is inferred from the directory layout
    ``exports/<Brand>/<Sender>/SourceFiles``. A payload that fails to parse
    is reported and skipped instead of aborting the whole rebuild.
    """
    if not target_path.exists():
        typer.echo(f"Path not found: {target_path}")
        raise typer.Exit(code=1)

    typer.echo(f"Scanning {target_path} for SourceFiles...")

    restored = 0
    for source_dir in sorted(target_path.rglob("SourceFiles")):
        if not source_dir.is_dir():
            continue
        parent = source_dir.parent
        try:
            relative_parts = parent.relative_to(target_path).parts
        except ValueError:
            relative_parts = ()
        brand = relative_parts[0] if relative_parts else "Unknown"

        for json_path in sorted(source_dir.glob("*.json")):
            try:
                with json_path.open(encoding="utf-8") as handle:
                    raw_json = json.load(handle)
                email_data = parse_gmail_payload(raw_json)
            except (json.JSONDecodeError, OSError, AttributeError, TypeError) as exc:
                typer.echo(f"  Skipping {json_path.name}: {exc}")
                continue
            try:
                md_content = generate_forensic_markdown(
                    email_data,
                    brand,
                    extra_frontmatter=_preserved_raw_link(parent, json_path.stem),
                )
                md_path = parent / f"{json_path.stem}.md"
                write_text_atomic(md_path, md_content)
            except Exception as exc:
                # Generation can fail on a payload that parses cleanly
                # (for example an unserialisable frontmatter value); that
                # must skip one report, not abort the whole rebuild.
                typer.echo(f"  Skipping {json_path.name}: {exc}")
                continue
            typer.echo(f"  Restored: {md_path.name}")
            restored += 1

    if restored == 0:
        typer.echo("No SourceFiles JSON payloads found.")


if __name__ == "__main__":
    app()
