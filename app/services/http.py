"""Shared, lazily created HTTPX async clients (connection pooling per upstream)."""

from __future__ import annotations

import httpx

_clients: dict[str, httpx.AsyncClient] = {}


def get_http_client(name: str, timeout: httpx.Timeout | float = 10.0) -> httpx.AsyncClient:
    """Return a pooled client for the named upstream (created on first use)."""
    client = _clients.get(name)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
        )
        _clients[name] = client
    return client


def set_http_client(name: str, client: httpx.AsyncClient) -> None:
    """Inject a client (tests use ``httpx.MockTransport``)."""
    _clients[name] = client


async def close_http_clients() -> None:
    for client in list(_clients.values()):
        if not client.is_closed:
            await client.aclose()
    _clients.clear()
