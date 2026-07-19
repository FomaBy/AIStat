"""Strict validation shared by every deployable outbound endpoint path."""

import ipaddress
import re
from typing import Optional
from urllib.parse import urlsplit

_HOST_LABEL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def _hostname_ok(hostname: Optional[str]) -> bool:
    """Validate an IP literal or DNS/IDNA hostname without resolving it."""
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass

    dns_name = hostname[:-1] if hostname.endswith(".") else hostname
    if not dns_name:
        return False
    try:
        ascii_name = dns_name.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return (
        len(ascii_name) <= 253
        and all(_HOST_LABEL_RE.match(label) for label in ascii_name.split("."))
    )


def https_endpoint_error(name: str, url: Optional[str]) -> Optional[str]:
    """Return a safe validation error, or ``None`` for an absolute HTTPS URL."""
    if not isinstance(url, str) or not url:
        return "{} is not configured".format(name)

    malformed = "must be an absolute URL with a valid host and port"
    if "\\" in url or any(
        ch.isspace() or ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f
        for ch in url
    ):
        return malformed
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        return malformed

    authority = parsed.netloc
    invalid_bracket_suffix = False
    if authority.startswith("["):
        closing = authority.find("]")
        suffix = authority[closing + 1:] if closing >= 0 else ""
        invalid_bracket_suffix = (
            closing < 0
            or (suffix and not suffix.startswith(":"))
            or suffix == ":"
        )
    if (
        not authority
        or "@" in authority
        or authority.endswith(":")
        or invalid_bracket_suffix
        or not _hostname_ok(hostname)
        or port == 0
    ):
        return malformed
    if parsed.scheme.lower() != "https":
        return "must use HTTPS"
    return None
