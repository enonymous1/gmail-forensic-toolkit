"""Tracking-pixel and redirect-link detection in HTML bodies."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

_REDIRECT_MARKERS = (
    "click.",
    "lnk.",
    "t.co",
    "bit.ly",
    "urlgenie",
    "mandrillapp",
    "sendgrid.net",
    "marketo.com",
    "tracking.",
    "redirect.",
)
_PIXEL_SRC_MARKERS = ("pixel", "transparent", "beacon", "1x1")
# _STYLE_HIDE is a heuristic, not proof of tracking: legitimate spacer
# images and hidden layout elements match it too. Counts from this module
# are therefore indicative ("this message looks tracked"), never a verdict
# on the sender's intent.
_STYLE_HIDE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0"
    r"|width\s*:\s*0(?:px)?\s*(?:;|$)|height\s*:\s*0(?:px)?\s*(?:;|$)"
    r"|width\s*:\s*1px|height\s*:\s*1px"
)


def _dimension(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip().removesuffix("px").strip()
    try:
        return int(text)
    except ValueError:
        return None


def _is_tracking_pixel(img: object) -> bool:
    get = getattr(img, "get", None)
    if get is None:
        return False
    width = _dimension(get("width"))
    height = _dimension(get("height"))
    if (width is not None and width <= 1) or (height is not None and height <= 1):
        return True
    style = str(get("style", "") or "")
    if _STYLE_HIDE.search(style.lower()):
        return True
    src = str(get("src", "") or "").lower()
    return any(marker in src for marker in _PIXEL_SRC_MARKERS)


def detect_tracking(html_content: str) -> tuple[int, int]:
    """Scan HTML for tracking pixels and redirect links; return both counts."""
    if not html_content:
        return 0, 0

    soup = BeautifulSoup(html_content, "html.parser")

    pixels = sum(1 for img in soup.find_all("img") if _is_tracking_pixel(img))

    links = 0
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).lower()
        host = urlparse(href).netloc
        if any(marker in host or marker in href for marker in _REDIRECT_MARKERS):
            links += 1

    return pixels, links
