"""Tests for charlotte.core.pinning_transport — connect-time SSRF pinning.

These deliberately do NOT use respx: respx intercepts at the transport level, before
connect_tcp, so it would never exercise the pinning backend. Instead we stub DNS
resolution (socket.getaddrinfo, which loop.getaddrinfo delegates to) and the wrapped
backend.
"""

import socket
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from charlotte.core.pinning_transport import (
    _PinningBackend,
    _resolve_and_validate,
    build_pinned_transport,
)
from charlotte.exceptions import CharlotteSSRFError


def _gai(*ips):
    """Build a getaddrinfo-shaped result for the given IP strings."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


# --- _resolve_and_validate -----------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_public_host_returns_resolved_ip():
    with patch("socket.getaddrinfo", return_value=_gai("93.184.216.34")):
        assert await _resolve_and_validate("example.com") == "93.184.216.34"


@pytest.mark.asyncio
async def test_resolve_host_to_private_ip_raises():
    """The static-A-record attack: a public-looking hostname whose record is internal."""
    with patch("socket.getaddrinfo", return_value=_gai("127.0.0.1")):
        with pytest.raises(CharlotteSSRFError, match="non-public"):
            await _resolve_and_validate("internal.evil.test")


@pytest.mark.asyncio
async def test_resolve_any_private_among_many_raises():
    """If any A-record is private, the whole host is refused — DNS round-robin can't
    slip one internal address through."""
    with patch("socket.getaddrinfo", return_value=_gai("93.184.216.34", "10.0.0.5")):
        with pytest.raises(CharlotteSSRFError):
            await _resolve_and_validate("roundrobin.test")


@pytest.mark.asyncio
async def test_resolve_unresolvable_host_raises():
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
        with pytest.raises(CharlotteSSRFError, match="could not resolve"):
            await _resolve_and_validate("nope.invalid")


@pytest.mark.asyncio
async def test_ip_literal_validated_without_dns():
    # A public literal connects as-is, no DNS lookup performed.
    with patch("socket.getaddrinfo", side_effect=AssertionError("must not resolve a literal")):
        assert await _resolve_and_validate("93.184.216.34") == "93.184.216.34"


@pytest.mark.asyncio
async def test_private_and_encoded_literals_raise_without_dns():
    with patch("socket.getaddrinfo", side_effect=AssertionError("must not resolve a literal")):
        with pytest.raises(CharlotteSSRFError):
            await _resolve_and_validate("127.0.0.1")
        with pytest.raises(CharlotteSSRFError):
            await _resolve_and_validate("2130706433")  # decimal-encoded 127.0.0.1


# --- _PinningBackend -----------------------------------------------------------

@pytest.mark.asyncio
async def test_backend_connects_to_validated_ip_not_hostname():
    wrapped = AsyncMock()
    backend = _PinningBackend(wrapped)
    with patch("socket.getaddrinfo", return_value=_gai("93.184.216.34")):
        await backend.connect_tcp("example.com", 443, timeout=5.0)
    # The kernel dials the validated IP, never the unresolved hostname.
    args, kwargs = wrapped.connect_tcp.call_args
    assert args[0] == "93.184.216.34"
    assert args[1] == 443


@pytest.mark.asyncio
async def test_backend_refuses_private_before_connecting():
    wrapped = AsyncMock()
    backend = _PinningBackend(wrapped)
    with patch("socket.getaddrinfo", return_value=_gai("10.0.0.1")):
        with pytest.raises(CharlotteSSRFError):
            await backend.connect_tcp("internal.test", 80)
    wrapped.connect_tcp.assert_not_called()   # never dialed


# --- build_pinned_transport + end-to-end through a client ----------------------

def test_build_pinned_transport_installs_backend():
    transport = build_pinned_transport()
    assert isinstance(transport._pool._network_backend, _PinningBackend)


@pytest.mark.asyncio
async def test_client_with_pinned_transport_blocks_private_resolving_host():
    """End-to-end: a real httpx client over the pinned transport refuses a host that
    resolves to private space, and CharlotteSSRFError propagates unwrapped."""
    with patch("socket.getaddrinfo", return_value=_gai("169.254.169.254")):
        async with httpx.AsyncClient(transport=build_pinned_transport()) as client:
            with pytest.raises(CharlotteSSRFError):
                await client.get("http://metadata.evil.test/latest/")
