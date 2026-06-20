"""
DNS-pinning httpx transport — validate the connection target, not just the URL string.

`validate_url_safety()` is a static check on the URL string. It cannot catch a hostname
whose DNS record points at internal space (a static A-record → 10.x / 127.0.0.1), nor a
DNS-rebinding attack where the name resolves public at check time and private at connect
time. Both are closed here, at the only place that knows the real destination: the
socket connect.

A custom network backend resolves the host once, validates **every** resolved address
against the SSRF policy, and connects to a validated IP — so the address the kernel
actually dials is the one that passed the check, with no second resolution to rebind.
Because the request's host stays the hostname, TLS SNI, certificate hostname
verification, and the Host header are unaffected; only the TCP connect target is pinned.

Wire it in with ``httpx.AsyncClient(transport=build_pinned_transport(...), ...)``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpcore
import httpx

from charlotte.core.normalizer import _is_non_public_address, _parse_ip_literal
from charlotte.exceptions import CharlotteConfigError, CharlotteSSRFError


async def _resolve_and_validate(host: str) -> str:
    """Resolve ``host`` and return a validated, publicly-routable IP to connect to.

    A bare IP literal (including the alternate encodings normalizer handles) is validated
    directly with no DNS lookup. A hostname is resolved; **every** returned address must
    be public — if any is private/loopback/link-local/CGN/reserved the whole host is
    refused, so DNS round-robin cannot slip one internal address through.

    Raises:
        CharlotteSSRFError: the host (or any of its addresses) is non-public, or it
            cannot be resolved to a usable address.
    """
    literal = _parse_ip_literal(host)
    if literal is not None:
        if _is_non_public_address(literal):
            raise CharlotteSSRFError(f"{host!r} is a non-public address")
        return host

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CharlotteSSRFError(f"could not resolve host {host!r}") from exc

    validated: list[str] = []
    for info in infos:
        ip_str = info[4][0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_non_public_address(addr):
            raise CharlotteSSRFError(
                f"host {host!r} resolves to a non-public address ({ip_str})"
            )
        validated.append(ip_str)

    if not validated:
        raise CharlotteSSRFError(f"host {host!r} did not resolve to a usable address")
    return validated[0]


class _PinningBackend(httpcore.AsyncNetworkBackend):
    """Wraps httpcore's network backend, pinning every TCP connect to a validated IP."""

    def __init__(self, wrapped: httpcore.AsyncNetworkBackend) -> None:
        self._wrapped = wrapped

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        # `host` is the request's hostname; resolve+validate, then dial the validated IP.
        # SNI / cert verification happen later against the original host, so pinning the
        # connect target does not weaken TLS.
        ip = await _resolve_and_validate(host)
        return await self._wrapped.connect_tcp(
            ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(self, *args, **kwargs):  # pragma: no cover - unused
        return await self._wrapped.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - passthrough
        await self._wrapped.sleep(seconds)


def build_pinned_transport(**kwargs) -> httpx.AsyncHTTPTransport:
    """An ``httpx.AsyncHTTPTransport`` whose every TCP connect is SSRF-validated and pinned.

    ``kwargs`` are forwarded to ``httpx.AsyncHTTPTransport`` (timeout, verify, limits, …).

    Raises:
        CharlotteConfigError: httpx's transport internals changed and the pinning seam is
            no longer where we expect. Failing loudly here is deliberate — silently losing
            the pin would reopen the SSRF hole.
    """
    transport = httpx.AsyncHTTPTransport(**kwargs)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise CharlotteConfigError(
            "httpx transport internals changed — the DNS-pinning seam is unavailable. "
            "Update charlotte.core.pinning_transport."
        )
    pool._network_backend = _PinningBackend(pool._network_backend)
    return transport
