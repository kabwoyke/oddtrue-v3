"""Bitget v2 REST transport for Phase 5b: read-only by construction (same guarantee as the Binance one).

Every request is a GET to a path in ``READ_ONLY_PATHS``; there is no write method. Signing, headers and the
response envelope are recorded in docs/exchanges/bitget.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import urllib.parse
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from oddtrue.exchanges.binance.http import RawResponse, Transport, UrllibTransport
from oddtrue.exchanges.errors import (
    AuthError,
    ExchangeDown,
    ExchangeError,
    RateLimited,
    Rejected,
)
from oddtrue.exchanges.readonly import ReadOnlyViolation as _ReadOnlyViolation
from oddtrue.observability.logging import register_secret
from oddtrue.ratelimit import Priority, WeightBudget

__all__ = ["BitgetHttp", "RawResponse", "ReadOnlyViolation", "Transport", "UrllibTransport", "map_error"]

NAME = "bitget"
BASE_URL = "https://api.bitget.com"
OK = "00000"

READ_ONLY_PATHS: frozenset[str] = frozenset(
    {
        "/api/v2/spot/market/orderbook",
        "/api/v2/spot/market/tickers",
        "/api/v2/spot/public/symbols",
        "/api/v2/spot/account/info",
        "/api/v2/spot/account/assets",
        "/api/v2/margin/crossed/account/assets",
        "/api/v2/margin/crossed/account/risk-rate",
        "/api/v2/margin/crossed/account/max-borrowable-amount",
        "/api/v2/margin/crossed/interest-rate-and-limit",
    }
)

# 40037 "Apikey does not exist" was observed live; the rest of this set comes from Bitget's public error-code list
# and is confirmed by the owner's first signed run (docs/exchanges/bitget.md). Unlisted codes become Rejected.
_AUTH_CODES = {"40006", "40009", "40012", "40014", "40018", "40037"}


class ReadOnlyViolation(_ReadOnlyViolation):
    def __init__(self, what: str) -> None:
        super().__init__(what, exchange=NAME)


def map_error(status: int, headers: Mapping[str, str], body: bytes) -> ExchangeError:
    """Normalise a failed response. Pure. Reads have no outcome to be unsure about, so 5xx is ExchangeDown."""
    retry = headers.get("retry-after")
    retry_s = Decimal(retry) if retry and retry.replace(".", "", 1).isdigit() else None
    if status == 429:
        return RateLimited("bitget 429", retry_after_s=retry_s, exchange=NAME, code="429")
    if status >= 500:
        return ExchangeDown(f"bitget HTTP {status}", exchange=NAME, code=str(status))
    code, msg = "", ""
    try:
        data = json.loads(body)
        code, msg = str(data["code"]), str(data.get("msg", ""))
    except (ValueError, KeyError, TypeError):
        pass
    if status == 401 or code in _AUTH_CODES:
        return AuthError(f"bitget auth failure ({code or status})", exchange=NAME, code=code or str(status))
    return Rejected(f"bitget rejected the request ({code or status}): {msg}", exchange=NAME, code=code or str(status))


class BitgetHttp:
    def __init__(
        self,
        *,
        api_key: str,
        secret: str,
        passphrase: str,
        transport: Transport,
        clock: Callable[[], float],
        ip_budget: WeightBudget,
        uid_budget: WeightBudget | None = None,
        base_url: str = BASE_URL,
        timeout_s: float = 5.0,
    ) -> None:
        self._key = api_key
        self._secret = secret.encode()
        self._passphrase = passphrase
        for value in (api_key, secret, passphrase):
            register_secret(value)
        self._transport = transport
        self._clock = clock
        self._ip = ip_budget  # shared by every client on this egress IP
        self._uid = uid_budget  # this client's own account limit
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._last_call: dict[str, float] = {}

    def __repr__(self) -> str:
        return f"BitgetHttp(base={self._base}, key=***)"

    def sign(self, timestamp_ms: str, path: str, query: str) -> str:
        prehash = f"{timestamp_ms}GET{path}" + (f"?{query}" if query else "")
        return base64.b64encode(hmac.new(self._secret, prehash.encode(), hashlib.sha256).digest()).decode()

    async def get(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        ip_weight: int,
        priority: Priority,
        signed: bool = False,
        min_interval_s: float = 0.0,
    ) -> Any:
        """Return the ``data`` member of a successful response."""
        if path not in READ_ONLY_PATHS:
            raise ReadOnlyViolation(path)
        query = urllib.parse.urlencode(dict(params or {}))
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if signed:
            ts = str(int(self._clock() * 1000))
            headers |= {
                "ACCESS-KEY": self._key,
                "ACCESS-SIGN": self.sign(ts, path, query),
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self._passphrase,
            }
        self._ip.acquire(ip_weight, priority)
        if signed and self._uid is not None:
            self._uid.acquire(1, priority)
        now = self._clock()
        if min_interval_s and now - self._last_call.get(path, -1e18) < min_interval_s:
            # Some endpoints allow only one call per second per user; refuse locally rather than earn a block.
            wait = Decimal(str(min_interval_s - (now - self._last_call[path])))
            raise RateLimited(
                f"{path} is limited to one call per {min_interval_s}s",
                retry_after_s=wait,
                exchange=NAME,
                code="local_interval",
            )
        if min_interval_s:
            self._last_call[path] = now
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        try:
            resp = await asyncio.wait_for(self._transport.get(url, headers, self._timeout), self._timeout + 1)
        except (TimeoutError, OSError) as exc:
            raise ExchangeDown(f"bitget request failed: {type(exc).__name__}", exchange=NAME, code="network") from None
        if resp.status >= 400:
            err = map_error(resp.status, resp.headers, resp.body)
            if isinstance(err, RateLimited) and err.retry_after_s is not None:
                self._ip.ban(err.retry_after_s)
            raise err
        try:
            envelope = json.loads(resp.body)
        except ValueError:
            raise ExchangeDown("bitget returned a non-JSON body", exchange=NAME, code="bad_body") from None
        if not isinstance(envelope, dict) or "code" not in envelope:
            raise ExchangeDown("bitget returned an unexpected body", exchange=NAME, code="bad_body")
        if str(envelope["code"]) != OK:  # a 200 can still carry an error envelope
            raise map_error(200, {}, resp.body)
        return envelope.get("data")
