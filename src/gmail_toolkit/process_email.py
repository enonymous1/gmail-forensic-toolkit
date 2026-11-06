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
import logging
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import dateutil.parser
import yaml
from bs4 import BeautifulSoup
from markdownify import markdownify

from . import __version__
from .forensics.classifier import classify_message
from .forensics.headers import get_sender_timezone
from .forensics.scanners import detect_tracking
from .paths import ensure_directory, write_text_atomic

logger = logging.getLogger(__name__)


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


def headers_dict(payload: Mapping[str, Any]) -> dict[str, str]:
    """Flatten a Gmail payload's header list into a lowercase dict.

    This is the one canonical header-flattening rule: names are lowercased
    and duplicate headers (notably ``Received``) are joined with a space
    instead of silently dropping all but the last. ``parse_gmail_payload``,
    scrape, and digest all use it, so a header means the same thing in
    every command.
    """
    headers: dict[str, str] = {}
    for header in payload.get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        value = str(header.get("value", ""))
        if not name:
            continue
        if name in headers:
            headers[name] = f"{headers[name]} {value}"
        else:
            headers[name] = value
    return headers


def parse_gmail_payload(message_json: Mapping[str, Any]) -> EmailData:
    """Parse a Gmail API message object into a structured forensic dict.

    Duplicate headers (notably ``Received``) are joined with a space instead
    of silently dropping all but the last, and the first text body of each
    MIME type wins instead of the last part overwriting earlier ones.
    """
    payload = message_json.get("payload", {}) or {}
    headers: dict[str, Any] = headers_dict(payload)
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
    """Cache the raw JSON, HTML, and plain-text sources for offline re-parsing.

    The cached JSON is the API payload plus a namespaced ``toolkit_version``
    key recording which toolkit version wrote it, so a future format change
    can tell old caches from new ones. The key is not a Gmail field and
    ``parse_gmail_payload`` ignores it on restore.
    """
    source_dir = ensure_directory(Path(target_dir) / "SourceFiles")

    json_path = source_dir / f"{base_filename}.json"
    cached = dict(email_data["raw_json"])
    cached["toolkit_version"] = __version__
    write_text_atomic(json_path, json.dumps(cached, indent=4))

    if email_data["body_html"]:
        html_path = source_dir / f"{base_filename}.html"
        write_text_atomic(html_path, email_data["body_html"])

    if email_data["body_plain"]:
        text_path = source_dir / f"{base_filename}.txt"
        write_text_atomic(text_path, email_data["body_plain"])


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
        # The fallback to plain text is deliberate (a report must never
        # contain raw HTML), but swallowing the error silently hid real
        # bugs. Log it so --verbose surfaces the cause.
        logger.warning("markdownify failed; falling back to plain text", exc_info=True)
        converted = ""
    if converted.strip():
        return converted
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text()


def read_report_frontmatter(report_path: Path) -> dict[str, Any]:
    """Return a report's YAML frontmatter dict, or {} when unreadable.

    The reader side of ``generate_forensic_markdown``: restore and
    verify need the frontmatter a report already carries (e.g. its
    ``RawFile`` link) without re-parsing the message.
    """
    try:
        text = Path(report_path).read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return {}
    try:
        data = yaml.safe_load(text[4:closing]) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def generate_forensic_markdown(
    email_data: EmailData,
    brand: str,
    extra_frontmatter: dict[str, object] | None = None,
) -> str:
    """Assemble YAML frontmatter, an ``[!INFO]`` block, and the body.

    The frontmatter carries the message's metadata, not just the
    forensic verdict: recipients, Gmail labels, and threading and
    authentication headers, so a report (and the index built from it)
    does not silently drop what the source message carried. API
    reports carry label IDs here (from the API's ``labelIds``) while
    Takeout reports carry label names (from ``X-Gmail-Labels``).
    ``extra_frontmatter`` adds caller-specific keys (the importer
    passes ``SourceFile`` and ``Attachments``); it never overrides a
    standard key.
    """
    msg_type, msg_scope = classify_message(email_data["subject"], email_data["headers"])
    pixels, links = detect_tracking(email_data["body_html"])
    timezone_name = get_sender_timezone(email_data["headers"])
    headers = email_data["headers"]

    def _header(key: str) -> str:
        value = headers.get(key, "")
        return "" if value is None else str(value)

    def _labels() -> list[str]:
        raw = headers.get("labelids", []) or []
        if isinstance(raw, str):
            return [raw]
        return [str(item) for item in raw]

    frontmatter: dict[str, object] = {
        "Brand": brand,
        "Sender": email_data["sender"],
        "ToolkitVersion": __version__,
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
        "To": _header("to"),
        "Cc": _header("cc"),
        "Labels": _labels(),
        "InReplyTo": _header("in-reply-to"),
        "References": _header("references"),
        "ReplyTo": _header("reply-to"),
        "AuthenticationResults": _header("authentication-results"),
    }
    if extra_frontmatter:
        for key, value in extra_frontmatter.items():
            frontmatter.setdefault(key, value)

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
