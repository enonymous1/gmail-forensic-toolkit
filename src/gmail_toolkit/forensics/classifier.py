"""Message type and scope classification."""

from __future__ import annotations

from email.utils import getaddresses

_MEETING_KEYWORDS = ("meeting", "invite", "calendar", "accepted:")
_NOTIFICATION_KEYWORDS = ("alert", "notification", "update:", "security")
_BULK_PRECEDENCE = {"list", "bulk", "junk"}


def _normalised_headers(headers: dict) -> dict[str, object]:
    return {str(key).lower(): value for key, value in headers.items()}


def _recipient_count(headers: dict[str, object]) -> int:
    """Count distinct recipients across To/Cc/Bcc headers."""
    values: list[str] = []
    for field in ("to", "cc", "bcc"):
        value = headers.get(field)
        if isinstance(value, str) and value.strip():
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value)
    addresses = {address.lower() for _name, address in getaddresses(values) if address}
    return len(addresses)


def classify_message(subject: str, sender: str, headers: dict) -> tuple[str, str]:
    """Return ``(message_type, message_scope)`` for a message.

    Type is keyword-based on the subject, with Gmail's ``DRAFT`` label as a
    fallback. Scope is ``Broadcast`` for list/bulk mail, ``Group`` when more
    than one recipient is addressed, and ``Direct (1:1)`` otherwise. The
    *sender* argument is accepted for call-site compatibility; classification
    does not depend on it.
    """
    _ = sender
    normalised = _normalised_headers(headers)

    message_type = "Email"
    subject_lower = subject.lower()
    if any(keyword in subject_lower for keyword in _MEETING_KEYWORDS):
        message_type = "Meeting"
    elif any(keyword in subject_lower for keyword in _NOTIFICATION_KEYWORDS):
        message_type = "Notification"
    else:
        label_ids = normalised.get("labelids", [])
        if isinstance(label_ids, list) and any(
            str(label).lower() == "draft" for label in label_ids
        ):
            message_type = "Draft"

    message_scope = "Direct (1:1)"
    list_id = normalised.get("list-id", "")
    precedence = normalised.get("precedence", "")
    if list_id or (
        isinstance(precedence, str) and precedence.lower() in _BULK_PRECEDENCE
    ):
        message_scope = "Broadcast"
    elif _recipient_count(normalised) > 1:
        message_scope = "Group"

    return message_type, message_scope
