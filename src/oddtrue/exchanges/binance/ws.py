"""Default websocket connector for the depth stream (thin shim over the ``websockets`` package).

Transport failures never escape as library or socket exceptions: a failure to connect is ``ExchangeDown`` and a
connection that closes or breaks mid-stream is ``StreamDropped``, so the feed's resync path always sees our types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from oddtrue.exchanges.errors import ExchangeDown, StreamDropped

NAME = "binance"


@asynccontextmanager
async def connect(url: str) -> AsyncIterator[AsyncIterator[str | bytes]]:
    import websockets  # imported lazily so unit tests and tooling never need a socket
    from websockets.exceptions import WebSocketException

    try:
        async with websockets.connect(url, ping_interval=20, open_timeout=10) as sock:
            try:
                yield aiter(sock)
            except WebSocketException:
                raise StreamDropped("binance websocket closed", exchange=NAME, code="ws_closed") from None
    except (OSError, TimeoutError) as exc:
        raise ExchangeDown(
            f"binance websocket unreachable: {type(exc).__name__}", exchange=NAME, code="ws_connect"
        ) from None
    except WebSocketException:
        raise ExchangeDown("binance websocket handshake failed", exchange=NAME, code="ws_connect") from None
