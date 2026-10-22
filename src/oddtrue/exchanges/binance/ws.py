"""Default websocket connector for the depth stream (thin shim over the ``websockets`` package)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def connect(url: str) -> AsyncIterator[AsyncIterator[str | bytes]]:  # pragma: no cover - live-only shim
    import websockets  # imported lazily so unit tests and tooling never need a socket

    async with websockets.connect(url, ping_interval=20, open_timeout=10) as sock:
        yield aiter(sock)
