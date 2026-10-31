"""Gmail payload parsing and forensic Markdown generation.

This module previously defined ``generate_forensic_markdown`` and
``convert_html_to_markdown`` twice: the second, placeholder definitions
shadowed the forensic ones, so every report was hardcoded to
``Email / Direct (1:1) / 0 tracking``. There is exactly one definition of
each function here, and the forensic engine is actually called.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import dateutil.parser
import yaml
from bs4 import BeautifulSoup
from markdownify import markdownify

from .forensics.classifier import classify_message
from .forensics.headers import get_sender_timezone
from .forensics.scanners import detect_tracking
from .paths import ensure_directory


class EmailData(TypedDict):
    """Structured form of one Gmail API message.

    ``date`` is ``None`` when the message carries no parseable Date header.
    It is deliberately *not* replaced with the current time: inventing a
    timestamp would misdate evidence in a forensic report.
    """

    id: str | None
    subject: str
    sender: str
    date: datetime | None
    body_html: str
    body_plain: str
    headers: dict[str, Any]
    raw_json: Mapping[str, Any]


def _decode_body(data: str) -> str:
    """Decode a Gmail body ``data`` field, tolerating missing padding."""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="ignore")


def parse_gmail_payload(message_json: Mapping[str, Any]) -> EmailData:
    """Parse a Gmail API message object into a structured forensic dict.

    Duplicate headers (notably ``Received``) are joined with a space instead
    of silently dropping all but the last, and the first text body of each
    MIME type wins instead of the last part overwriting earlier ones.
    """
    payload = message_json.get("payload", {}) or {}
    headers: dict[str, Any] = {}
    for header in payload.get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        value = header.get("value", "")
        if not name:
            continue
        if name in headers:
            headers[name] = f"{headers[name]} {value}"
        else:
            headers[name] = value
    headers["labelids"] = message_json.get("labelIds", [])

    subject = str(headers.get("subject", "(No Subject)"))
    sender = str(headers.get("from", "Unknown Sender"))
    date_str = str(headers.get("date", ""))

    try:
        date_dt = dateutil.parser.parse(date_str) if date_str else None
    except (ValueError, OverflowError, TypeError):
        date_dt = None
    # No fallback to "now": a missing or unparseable Date stays unknown
    # (see EmailData). Callers that need a filename use a "nodate" stem.

    body_html = ""
    body_plain = ""
    queue: deque[Mapping[str, Any]] = deque([payload])
    while queue:
        part = queue.popleft()
        mime_type = part.get("mimeType")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if isinstance(data, str) and data and not part.get("filename"):
            # Parts with a filename are attachments, not the message body.
            decoded = _decode_body(data)
            if mime_type == "text/html" and not body_html:
                body_html = decoded
            elif mime_type == "text/plain" and not body_plain:
                body_plain = decoded
        subparts = part.get("parts")
        if isinstance(subparts, list):
            queue.extend(subpart for subpart in subparts if isinstance(subpart, dict))

    return {
        "id": message_json.get("id"),
        "subject": subject,
        "sender": sender,
        "date": date_dt,
        "body_html": body_html,
        "body_plain": body_plain,
        "headers": headers,
        "raw_json": message_json,
    }


def save_source_files(
    email_data: EmailData, target_dir: str | Path, base_filename: str
) -> None:
    """Cache the raw JSON, HTML, and plain-text sources for offline re-parsing."""
    source_dir = ensure_directory(Path(target_dir) / "SourceFiles")

    json_path = source_dir / f"{base_filename}.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(email_data["raw_json"], handle, indent=4)

    if email_data["body_html"]:
        html_path = source_dir / f"{base_filename}.html"
        html_path.write_text(email_data["body_html"], encoding="utf-8")

    if email_data["body_plain"]:
        text_path = source_dir / f"{base_filename}.txt"
        text_path.write_text(email_data["body_plain"], encoding="utf-8")


def convert_html_to_markdown(html_content: str) -> str:
    """Convert HTML to Markdown with markdownify; fall back to plain text.

    The original code shelled out to an external ``pandoc`` binary and, on a
    non-zero exit, returned the raw HTML into the Markdown report. This
    version is pure Python and never returns raw HTML.
    """
    if not html_content:
        return ""
    try:
        converted = markdownify(html_content, heading_style="ATX")
    except Exception:
        converted = ""
    if converted.strip():
        return converted
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text()


def generate_forensic_markdown(email_data: EmailData, brand: str) -> str:
    """Assemble YAML frontmatter, an ``[!INFO]`` block, and the body."""
    msg_type, msg_scope = classify_message(
        email_data["subject"], email_data["sender"], email_data["headers"]
    )
    pixels, links = detect_tracking(email_data["body_html"])
    timezone_name = get_sender_timezone(email_data["headers"])

    frontmatter = {
        "Brand": brand,
        "Sender": email_data["sender"],
        "Date": (
            email_data["date"].strftime("%Y-%m-%d %H:%M:%S")
            if email_data["date"] is not None
            else "Unknown"
        ),
        "Timezone": timezone_name,
        "Subject": email_data["subject"],
        "MessageID": email_data["id"],
        "Type": msg_type,
        "Scope": msg_scope,
    }

    yaml_block = f"---\n{yaml.safe_dump(frontmatter, sort_keys=False)}---\n\n"

    info_block = (
        "> [!INFO] **Forensic Classification**\n"
        f"> - **Brand**: {brand}\n"
        f"> - **Category**: {msg_type}\n"
        f"> - **Focus**: {msg_scope}\n"
        f"> - **Timezone**: {timezone_name}\n"
        f"> - **Tracking**: Pixels: {pixels}, Links: {links}\n"
        "---\n\n"
    )

    if email_data["body_html"]:
        md_content = convert_html_to_markdown(email_data["body_html"])
    else:
        md_content = email_data["body_plain"]

    return yaml_block + info_block + md_content
