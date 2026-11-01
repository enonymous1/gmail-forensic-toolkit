"""Parse Google Takeout ``.mbox`` messages for the forensic pipeline.

Adapted from the companion mbox-explorer project's ``parser.py`` (same
author). The decoding rules are unchanged — RFC 2047 headers, charset
fallbacks, sanitized HTML — but the output is the toolkit's message
shape instead of a database row, so an imported message flows through
the same classification, tracking scan, branding, and Markdown
generation as an API-archived one.

Deliberate differences from the explorer:
- No attachment extraction and no database: attachment bytes are never
  written. Only filename/type/size metadata is kept, and the importer
  appends it to the report as an Attachments section.
- Dates stay as datetimes (or ``None``), matching ``EmailData``: a
  missing or unparseable Date is never replaced with "now".
- This module never raises on bad input if it can help it: a corrupt
  charset, a missing header, or a malformed date degrades to a safe
  fallback, so the importer can log the message and keep going.
"""

from __future__ import annotations

import mailbox
import re
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses
from pathlib import Path
from typing import Any

import dateutil.parser
from bs4 import BeautifulSoup

# Charsets to try, in order, when the declared charset fails or is absent.
_FALLBACK_CHARSETS = ("utf-8", "windows-1252", "iso-8859-1")

_UNSAFE_ATTRS = re.compile(r"^on", re.IGNORECASE)


@dataclass
class AttachmentMeta:
    """Metadata for one attachment part; bytes are never retained."""

    filename: str | None
    content_type: str
    size_bytes: int


@dataclass
class ParsedMboxMessage:
    """One parsed mbox message, ready to map onto ``EmailData``."""

    message_id: str | None
    subject: str
    sender: str
    sender_address: str
    date: datetime | None
    date_header: str
    body_plain: str
    body_html: str
    headers: dict[str, Any]
    raw_headers: str
    attachments: list[AttachmentMeta] = field(default_factory=list)


def decode_header_value(value: str | None) -> str:
    """Decode RFC 2047 encoded-words (``=?UTF-8?B?...?=``) to str."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _format_addresses(value: str | None) -> str:
    """Normalise an address header to 'Name <a@b>, Name2 <c@d>'."""
    decoded = decode_header_value(value)
    if not decoded:
        return ""
    formatted = []
    for name, addr in getaddresses([decoded]):
        if addr and name:
            formatted.append(f"{name} <{addr}>")
        elif addr:
            formatted.append(addr)
        elif name:
            formatted.append(name)
    return ", ".join(formatted)


def _first_address(value: str | None) -> str:
    """Return the first bare address in *value*, or "" when there is none."""
    decoded = decode_header_value(value)
    for _name, addr in getaddresses([decoded]) if decoded else []:
        if addr:
            return addr
    return ""


def decode_payload(part: Message) -> bytes:
    """Return a part's decoded bytes, or b'' for a container/no payload."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return b""
    if isinstance(payload, bytes):
        return payload
    if payload is None:
        # Non-encoded single-part body: re-encode the str payload.
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
    return b""


def decode_text(payload: bytes, charset: str | None) -> str:
    """Decode bytes, falling back through common charsets, never raising."""
    candidates = []
    if charset:
        candidates.append(charset)
    candidates.extend(c for c in _FALLBACK_CHARSETS if c not in candidates)
    for name in candidates:
        try:
            return payload.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def sanitize_html(html: str) -> str:
    """Strip active content from HTML so it is safe to render and convert.

    Removes script/style/iframe/object/embed/form/base/link/meta/title
    tags, ``on*`` event attributes, and ``javascript:`` / ``data:``
    URLs. This is defence in depth: the report generator converts HTML
    to Markdown and never emits raw HTML, but a report must not carry
    active content in the first place.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(
        [
            "script",
            "style",
            "iframe",
            "object",
            "embed",
            "form",
            "base",
            "link",
            "meta",
            "title",
        ]
    ):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if _UNSAFE_ATTRS.match(attr):
                del tag.attrs[attr]
            elif attr in ("href", "src"):
                val = str(tag.attrs[attr]).strip().lower()
                if val.startswith(("javascript:", "data:text/html", "vbscript:")):
                    del tag.attrs[attr]
    return str(soup)


def html_to_text(html: str) -> str:
    """Best-effort plain text from HTML, collapsing all whitespace.

    This text is a fallback for HTML-only messages; the rendered HTML
    body is kept separately in ``body_html``.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    return " ".join(text.split())


def parse_message(msg: Message) -> ParsedMboxMessage:
    """Parse one ``email.message.Message`` into a ``ParsedMboxMessage``."""
    raw_headers = "\n".join(f"{k}: {v}" for k, v in msg.items())
    labels_raw = decode_header_value(msg.get("X-Gmail-Labels", ""))
    labels = [p.strip() for p in labels_raw.split(",") if p.strip()]
    date_header = msg.get("Date", "") or ""
    try:
        date = dateutil.parser.parse(date_header) if date_header else None
    except (ValueError, OverflowError, TypeError):
        date = None
    # No fallback to "now": a missing or unparseable Date stays unknown
    # (see EmailData). Callers that need a filename use a "nodate" stem.

    sender = _format_addresses(msg.get("From"))
    subject = decode_header_value(msg.get("Subject", ""))
    headers: dict[str, Any] = {
        "subject": subject,
        "from": sender,
        "date": date_header,
        "to": _format_addresses(msg.get("To")),
        "cc": _format_addresses(msg.get("Cc")),
        "bcc": _format_addresses(msg.get("Bcc")),
        # Duplicate Received headers are joined with a space, the same
        # rule as headers_dict in process_email.py, so the timezone
        # scan sees every hop.
        "received": " ".join(msg.get_all("Received", [])),
        "list-id": msg.get("List-ID", "") or "",
        "precedence": msg.get("Precedence", "") or "",
        # Threading and authentication headers are evidence, not noise:
        # the generator emits them into frontmatter, so keep them here
        # rather than dropping them at parse time.
        "in-reply-to": msg.get("In-Reply-To", "") or "",
        "references": msg.get("References", "") or "",
        "reply-to": _format_addresses(msg.get("Reply-To")),
        "authentication-results": msg.get("Authentication-Results", "") or "",
        "labelids": labels,
    }

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentMeta] = []

    # Walk every leaf part. Containers (multipart/*) have no payload.
    leaves = list(msg.walk()) if msg.is_multipart() else [msg]
    for part in leaves:
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if filename:
            filename = decode_header_value(filename)

        if disposition == "attachment" or filename:
            # Metadata only: the bytes are decoded once for the size
            # and then dropped, never written to disk.
            attachments.append(
                AttachmentMeta(
                    filename=filename,
                    content_type=content_type,
                    size_bytes=len(decode_payload(part)),
                )
            )
            continue

        if content_type == "text/plain":
            text_parts.append(
                decode_text(decode_payload(part), part.get_content_charset())
            )
        elif content_type == "text/html":
            html_parts.append(
                decode_text(decode_payload(part), part.get_content_charset())
            )
        # Anything else without a filename (e.g. inline images with a
        # Content-ID) is intentionally ignored: it is not readable body
        # text and is not a user attachment.

    body_html = sanitize_html(html_parts[0]) if html_parts else ""
    if text_parts:
        body_plain = "\n\n".join(p for p in text_parts if p.strip())
    elif body_html:
        # HTML-only message: derive plain text from it.
        body_plain = html_to_text(body_html)
    else:
        body_plain = ""

    return ParsedMboxMessage(
        message_id=decode_header_value(msg.get("Message-ID", "")).strip() or None,
        subject=subject,
        sender=sender,
        sender_address=_first_address(msg.get("From")),
        date=date,
        date_header=date_header,
        body_plain=body_plain,
        body_html=body_html,
        headers=headers,
        raw_headers=raw_headers,
        attachments=attachments,
    )


def iter_mbox(path: str | Path) -> Generator[Message, None, None]:
    """Yield messages from the mbox file at *path*, in file order.

    The caller owns closing the generator (``gen.close()``) when it
    stops early, e.g. on ``--limit``; closing releases the file.
    Streaming matters: a Takeout archive can be multi-gigabyte, so the
    file is never loaded wholesale.
    """
    box = mailbox.mbox(path, create=False)
    try:
        yield from box
    finally:
        box.close()
