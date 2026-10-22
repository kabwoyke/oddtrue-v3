"""Structured JSON logs with secret redaction (CLAUDE.md rule 9, section 12).

Every record is one JSON object carrying the trade context (trade_id, client_id, exchange, leg, state,
client_order_id) taken from a context variable, so call sites just log and the engine binds context once.
Redaction happens in the formatter, so it also covers messages, extra fields AND formatted tracebacks.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

REDACTED = "[REDACTED]"
CONTEXT_FIELDS = ("trade_id", "client_id", "exchange", "leg", "state", "client_order_id")

# Field names whose values are never logged, matched on the lower-cased name with separators removed.
_SECRET_KEYS = (
    "apikey", "secret", "passphrase", "password", "token", "authorization", "signature", "privatekey",
    "otp", "mfa", "dek", "kek", "accesskey", "accesssign", "accesspassphrase", "mbxapikey", "cookie",
)  # fmt: skip
# "name=value", "name: value" and JSON-ish "name": "value" in free text.
_KV = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|passphrase|password|token|authorization|signature|private[_-]?key|x-mbx-apikey|ok-access-[a-z]+)"
    r"(['\"]?\s*[:=]\s*['\"]?)(?:Bearer\s+|Basic\s+)?([^\s,;'\"&}]+)"
)
# A bare "key=..." is only treated as a secret when the value is long and key-shaped (avoids hiding "key=BTCUSDT").
_BARE_KEY = re.compile(r"(?i)\b(key)(['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9+/_-]{16,}")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_known_secrets: set[str] = set()

_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("oddtrue_log_context", default=None)


def register_secret(value: str) -> None:
    """Mask this exact value wherever it appears in any log line (e.g. a decrypted key held in memory)."""
    if len(value) >= 6:
        _known_secrets.add(value)


def forget_secrets() -> None:
    _known_secrets.clear()


def _is_secret_key(key: str) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(s in flat for s in _SECRET_KEYS)


def redact_text(text: str) -> str:
    for secret in sorted(_known_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _BARE_KEY.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def redact(value: Any) -> Any:
    """Recursively redact: secret-looking keys lose their value, strings are scrubbed, containers are walked."""
    if isinstance(value, Mapping):
        return {k: (REDACTED if _is_secret_key(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind fields (trade_id, client_id, ...) to every log record emitted inside the block, nesting-safe."""
    token = _context.set({**(_context.get() or {}), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_context.get() or {})


_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(current_context())
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str, separators=(",", ":"), sort_keys=False)


def configure_logging(level: int = logging.INFO, stream: TextIO | None = None) -> logging.Handler:
    """Send all ``oddtrue.*`` logs to one JSON handler on ``stream`` (stderr by default)."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("oddtrue")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    return handler


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"oddtrue.{name}")
