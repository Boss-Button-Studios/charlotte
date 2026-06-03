"""
URL normalization for Charlotte (spec §9.5).

Normalization makes identical-but-differently-encoded URLs compare equal,
which is the foundation for visited-set deduplication and the URL provenance
check. All eight rules from the spec are applied in order every time.

Public function: normalize_url(url, base_url=None) -> str
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
from urllib.parse import (
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from charlotte.exceptions import CharlotteConfigError, CharlotteSSRFError

# RFC 3986 §2.3 — characters that are never percent-encoded in a well-formed URL.
# Decoding percent-encoded sequences for these characters produces a canonical form.
_UNRESERVED: frozenset[str] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "-._~"
)

# Scheme → default port. Ports equal to these are stripped from the netloc.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ftp": 21}

# Cloud metadata endpoints that must never be reached, regardless of IP classification.
# These hostnames resolve to link-local addresses but string-matching is faster and
# catches cases where ipaddress() wouldn't apply (hostname rather than IP in the URL).
_CLOUD_METADATA_HOSTS: frozenset[str] = frozenset({
    "metadata.google.internal",
    "metadata.azure.com",
    "metadata.azure.internal",
    "169.254.169.254",
    "metadata.ec2.internal",
})


def _decode_unreserved(component: str) -> str:
    """Decode percent-encoded unreserved characters per RFC 3986 §2.3.

    Only decodes sequences whose byte value maps to an ASCII unreserved
    character (A-Z, a-z, 0-9, -, ., _, ~). Reserved characters (%2F, %3A,
    etc.) and non-ASCII sequences are left percent-encoded.
    """
    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        byte_val = int(m.group(1), 16)
        if byte_val < 128:
            char = chr(byte_val)
            if char in _UNRESERVED:
                return char
        return m.group(0)

    return re.sub(r"%([0-9A-Fa-f]{2})", _replace, component)


def _normalize_path(path: str) -> str:
    """Collapse consecutive slashes and resolve . and .. path segments.

    posixpath.normpath handles both, but preserves a leading // as a POSIX
    special case. We treat // the same as / for HTTP URLs.
    """
    if not path:
        return "/"
    normalized = posixpath.normpath(path)
    if normalized == ".":
        return "/"
    # posixpath preserves a leading // (POSIX implementation-defined behaviour).
    # For HTTP URLs it has no meaning, so collapse it to /.
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def _sort_query(query: str) -> str:
    """Sort query parameters alphabetically by key for canonical comparison.

    Uses standard percent-encoding (%20, not +) on re-encoding, consistent
    with RFC 3986. Sort is stable so duplicate keys preserve relative order.
    """
    if not query:
        return ""
    params = parse_qsl(query, keep_blank_values=True)
    params.sort(key=lambda kv: kv[0])
    return urlencode(params, quote_via=quote)


def validate_url_safety(url: str) -> None:
    """Reject URLs that could trigger SSRF against internal infrastructure.

    Checks performed (all unconditional — there is no bypass):
    - Scheme must be http or https (blocks file://, gopher://, etc.)
    - Hostname must not be in the cloud metadata denylist
    - Hostname must not resolve to a private, loopback, link-local, multicast,
      or reserved IP address
    - Bare "localhost" is rejected regardless of its OS resolution

    DNS rebinding is a known partial gap: this check is purely static on the URL
    string and does not re-validate after DNS resolution. See SECURITY.md.

    Args:
        url: Absolute URL to validate. Should be normalized first.

    Raises:
        CharlotteSSRFError: URL scheme is not http/https, or the hostname
            resolves to a disallowed address range.
    """
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise CharlotteSSRFError(f"Could not parse URL for safety check: {url!r}") from exc

    if parsed.scheme not in {"http", "https"}:
        raise CharlotteSSRFError(
            f"URL scheme {parsed.scheme!r} is not allowed — only http and https are permitted"
        )

    # Strip the FQDN trailing dot (e.g. "localhost." == "localhost") before any
    # string-based checks so that forms like http://localhost./ are not bypassed.
    hostname = (parsed.hostname or "").lower().rstrip(".")

    if hostname in _CLOUD_METADATA_HOSTS:
        raise CharlotteSSRFError(
            f"URL targets a cloud metadata endpoint that is never reachable: {hostname!r}"
        )

    if hostname == "localhost":
        raise CharlotteSSRFError(
            "URL targets 'localhost' — Charlotte only crawls publicly routable addresses"
        )

    # Try to parse as a bare IP address (works for both IPv4 and IPv6 literals).
    # urlsplit().hostname strips brackets from IPv6 literals, giving a parseable address.
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return  # hostname is a DNS name — static SSRF check passes (see DNS rebinding note)

    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
    ):
        raise CharlotteSSRFError(
            f"URL targets a non-public IP address ({hostname!r}) — "
            "Charlotte only crawls publicly routable addresses"
        )


def normalize_url(url: str, base_url: str | None = None) -> str:
    """Normalize a URL to a canonical form for deduplication and comparison.

    Applies all eight normalization rules from spec §9.5 in order:

    1. Lowercase scheme and host
    2. Remove default ports (80 for http, 443 for https, 21 for ftp)
    3. Resolve relative URLs against base_url
    4. Decode percent-encoded unreserved characters (e.g. %41 -> A)
    5. Strip URL fragment (#section)
    6. Normalize path: collapse // and resolve . / .. segments
    7. Sort query parameters alphabetically by key (stable — duplicate keys
       preserve their relative order)
    8. Remove trailing slash from non-root paths

    Note: result URLs returned to callers are intentionally NOT normalized --
    this function is for internal deduplication only. See spec §9.5.

    Args:
        url:      The URL to normalize. May be relative if base_url is given.
        base_url: Current page URL used to resolve relative references (rule 3).

    Returns:
        Normalized absolute URL string.

    Raises:
        CharlotteConfigError: If the URL is empty, unparseable, or is relative
                              with no base_url provided to resolve it.
    """
    if not url:
        raise CharlotteConfigError("URL must not be empty")

    # Rule 3: resolve relative URLs before any other parsing
    if base_url:
        try:
            url = urljoin(base_url, url)
        except ValueError as exc:
            raise CharlotteConfigError(
                f"Could not resolve {url!r} against base {base_url!r}: {exc}"
            ) from exc

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise CharlotteConfigError(f"Could not parse URL {url!r}: {exc}") from exc

    if not parsed.scheme:
        raise CharlotteConfigError(
            f"URL has no scheme and no base_url was provided to resolve it: {url!r}"
        )

    # Rule 1: lowercase scheme and host (not userinfo — passwords are case-sensitive)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()

    # Reconstruct netloc with lowercased host; preserve userinfo as-is
    username = parsed.username or ""
    password = parsed.password or ""
    port = parsed.port

    if username:
        userinfo = f"{username}:{password}@" if password else f"{username}@"
    else:
        userinfo = ""

    # Rule 2: strip default ports.
    # RFC 3986 §3.2.2: IPv6 literals must be enclosed in brackets in the netloc.
    # urlsplit().hostname strips them; we restore them before reassembly.
    host_part = f"[{hostname}]" if ":" in hostname else hostname

    if port and port == _DEFAULT_PORTS.get(scheme):
        netloc = f"{userinfo}{host_part}"
    elif port:
        netloc = f"{userinfo}{host_part}:{port}"
    else:
        netloc = f"{userinfo}{host_part}"

    # Rule 4: decode safe percent-encoding in the path
    path = _decode_unreserved(parsed.path)

    # Rule 5: fragment is simply omitted from urlunsplit (empty string below)

    # Rule 6: normalize path separators
    path = _normalize_path(path)

    # Rule 7: sort query parameters
    query = _sort_query(parsed.query)

    # Rule 8: remove trailing slash from non-root paths
    # (posixpath.normpath already handles this; kept as a safety net)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    try:
        return urlunsplit((scheme, netloc, path, query, ""))
    except ValueError as exc:
        raise CharlotteConfigError(
            f"Could not reassemble normalized URL from {url!r}: {exc}"
        ) from exc
