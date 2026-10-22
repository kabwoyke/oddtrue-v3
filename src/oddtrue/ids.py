"""Deterministic idempotency keys (CLAUDE.md rule 3): trade_id + leg + attempt, persisted BEFORE sending."""

from __future__ import annotations

from hashlib import sha256

PREFIX = "ot-"
_DIGEST_CHARS = 24  # 27 chars total; exchange length limits are verified per exchange in Phase 5


def client_order_id(trade_id: str, leg: str, attempt: int) -> str:
    if not trade_id or not leg or attempt < 1:
        raise ValueError("trade_id and leg are required and attempt starts at 1")
    if "|" in trade_id or "|" in leg:
        raise ValueError("'|' is reserved as the separator")
    digest = sha256(f"{trade_id}|{leg}|{attempt}".encode()).hexdigest()
    return PREFIX + digest[:_DIGEST_CHARS]
