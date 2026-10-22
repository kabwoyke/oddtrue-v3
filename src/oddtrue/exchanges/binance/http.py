"""Binance REST transport for Phase 5a: read-only by construction.

There is no ``post``/``delete`` and no method parameter: every request is a GET, and only paths in
``READ_ONLY_PATHS`` can be sent. An order, borrow, repay or withdraw path is therefore unreachable from here,
whatever a caller passes (CLAUDE.md section 13, Phase 5 exit criterion). Sources: docs/exchanges/binance.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from oddtrue.exchanges.errors import (
    AuthError,
    ExchangeDown,
    ExchangeError,
    InvalidQuantity,
    RateLimited,
    Rejected,
)
from oddtrue.exchanges.readonly import READ_ONLY as READ_ONLY
from oddtrue.exchanges.readonly import ReadOnlyViolation as _ReadOnlyViolation
from oddtrue.observability.logging import register_secret
from oddtrue.ratelimit import Priority, WeightBudget

NAME = "binance"
BASE_URL = "https://api.binance.com"

# Every endpoint this phase may touch. All are GET. Do not add a path without recording it in binance.md.
READ_ONLY_PATHS: frozenset[str] = frozenset(
    {
        "/api/v3/depth",
        "/api/v3/exchangeInfo",
        "/api/v3/ticker/price",
        "/api/v3/account",
        "/api/v3/account/commission",
        "/sapi/v1/account/apiRestrictions",
        "/sapi/v1/margin/account",
        "/sapi/v1/margin/maxBorrowable",
        "/sapi/v1/margin/next-hourly-interest-rate",
    }
)

# Documented error codes (docs/exchanges/binance.md). Anything else is a definitive Rejected on a read.
_AUTH_CODES = {-2014, -2015, -1022, -1002, -2008}
_QTY_CODES = {-1013, -1100, -1101, -1102, -1103, -1104, -1105, -1106, -1111, -1116}


class ReadOnlyViolation(_ReadOnlyViolation):
    def __init__(self, what: str) -> None:
        super().__init__(what, exchange=NAME)


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    async def get(self, url: str, headers: Mapping[str, str], timeout_s: float) -> RawResponse: ...


class UrllibTransport:  # pragma: no cover - thin stdlib shim, exercised only by scripts/exchange_readonly_smoke.py
    async def get(self, url: str, headers: Mapping[str, str], timeout_s: float) -> RawResponse:
        def call() -> RawResponse:
            req = urllib.request.Request(url, headers=dict(headers), method="GET")  # noqa: S310 - https base only
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310
                    return RawResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
            except urllib.error.HTTPError as e:
                return RawResponse(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())

        return await asyncio.to_thread(call)


def map_error(status: int, headers: Mapping[str, str], body: bytes) -> ExchangeError:
    """Normalise a non-2xx response. Pure. Reads have no outcome to be unsure of, so 5xx is ExchangeDown."""
    retry = headers.get("retry-after")
    retry_s = Decimal(retry) if retry and retry.replace(".", "", 1).isdigit() else None
    if status == 429:
        return RateLimited("binance 429", retry_after_s=retry_s, exchange=NAME, code="429")
    if status == 418:
        return RateLimited("binance IP ban", retry_after_s=retry_s, banned=True, exchange=NAME, code="418")
    if status == 403:
        return RateLimited("binance WAF block", retry_after_s=retry_s, banned=True, exchange=NAME, code="403")
    if status >= 500:
        return ExchangeDown(f"binance HTTP {status}", exchange=NAME, code=str(status))
    code: int | None = None
    msg = ""
    try:
        data = json.loads(body)
        code, msg = int(data["code"]), str(data.get("msg", ""))
    except (ValueError, KeyError, TypeError):
        pass
    if status == 401 or code in _AUTH_CODES:
        return AuthError(f"binance auth failure ({code})", exchange=NAME, code=str(code or status))
    if code in _QTY_CODES:
        return InvalidQuantity(f"binance rejected quantity/price ({code}): {msg}", exchange=NAME, code=str(code))
    if code == -1003:
        return RateLimited("binance -1003", retry_after_s=retry_s, exchange=NAME, code="-1003")
    return Rejected(f"binance rejected the request ({code or status}): {msg}", exchange=NAME, code=str(code or status))


class BinanceHttp:
    def __init__(
        self,
        *,
        api_key: str,
        secret: str,
        transport: Transport,
        clock: Callable[[], float],
        budget: WeightBudget,
        base_url: str = BASE_URL,
        recv_window_ms: int = 5000,
        timeout_s: float = 5.0,
    ) -> None:
        self._key = api_key
        self._secret = secret.encode()
        register_secret(api_key)
        register_secret(secret)
        self._transport = transport
        self._clock = clock
        self._budget = budget
        self._base = base_url.rstrip("/")
        self._recv = recv_window_ms
        self._timeout = timeout_s

    def __repr__(self) -> str:
        return f"BinanceHttp(base={self._base}, key=***)"

    def sign(self, query: str) -> str:
        return hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()

    async def get(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        weight: int,
        priority: Priority,
        signed: bool = False,
    ) -> Any:
        if path not in READ_ONLY_PATHS:
            raise ReadOnlyViolation(path)
        q = dict(params or {})
        if signed:
            q["timestamp"] = str(int(self._clock() * 1000))
            q["recvWindow"] = str(self._recv)
        query = urllib.parse.urlencode(q)
        if signed:
            query += "&signature=" + self.sign(query)
        self._budget.acquire(weight, priority)
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        try:
            resp = await asyncio.wait_for(
                self._transport.get(url, {"X-MBX-APIKEY": self._key}, self._timeout), self._timeout + 1
            )
        except (TimeoutError, OSError) as exc:
            raise ExchangeDown(f"binance request failed: {type(exc).__name__}", exchange=NAME, code="network") from None
        used = resp.headers.get("x-mbx-used-weight-1m")
        if used and used.isdigit():
            self._budget.observe_used(int(used))
        if resp.status >= 400:
            err = map_error(resp.status, resp.headers, resp.body)
            if isinstance(err, RateLimited) and err.retry_after_s is not None:
                self._budget.ban(err.retry_after_s)
            raise err
        try:
            return json.loads(resp.body, parse_float=Decimal, parse_int=int)
        except ValueError:
            raise ExchangeDown("binance returned a non-JSON body", exchange=NAME, code="bad_body") from None
