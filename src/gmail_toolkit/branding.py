"""Brand resolution from a sender's domain."""

from __future__ import annotations

import json
from email.utils import parseaddr
from pathlib import Path

DEFAULT_BRANDS: dict[str, str] = {
    "google.com": "Google",
    "microsoft.com": "Microsoft",
    "github.com": "GitHub",
    "apple.com": "Apple",
    "amazon.com": "Amazon",
}


def load_brands(brands_config_path: str | Path | None) -> dict[str, str]:
    """Load domain -> brand mappings.

    Returns the built-in defaults when no path is given or the file does not
    exist, so the CLI works from an installed package without a config file
    in the current directory. A file that exists but is not a JSON object of
    strings raises :class:`ValueError` instead of silently falling back.
    """
    if brands_config_path is None:
        return dict(DEFAULT_BRANDS)
    path = Path(brands_config_path)
    if not path.exists():
        return dict(DEFAULT_BRANDS)
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ValueError(f"Brands file must be a JSON object of strings: {path}")
    return {key.lower(): value for key, value in data.items()}


def extract_domain(email_address: str) -> str:
    """Extract the lower-cased domain from a bare address or ``Name <addr>``."""
    _name, address = parseaddr(email_address)
    candidate = address or email_address.strip()
    if "@" not in candidate:
        return ""
    return candidate.rsplit("@", 1)[-1].strip().strip(">").lower()


def resolve_brand_from_map(email_address: str, brands: dict[str, str]) -> str:
    """Resolve a brand from *brands*, matching subdomains to parent domains."""
    domain = extract_domain(email_address)
    if not domain:
        return "Individual"
    if domain in brands:
        return brands[domain]
    for key, value in brands.items():
        if domain.endswith(f".{key}"):
            return value
    return "Individual"


def resolve_brand(
    email_address: str, brands_config_path: str | Path | None = None
) -> str:
    """Resolve a brand name from an email address using the brands config.

    Defaults to ``'Individual'`` when no brand is found. When
    *brands_config_path* is ``None`` or the file is missing, the built-in
    defaults in :data:`DEFAULT_BRANDS` are used.
    """
    return resolve_brand_from_map(email_address, load_brands(brands_config_path))
