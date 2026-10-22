"""One error type for "this adapter is read-only" so callers and tests can treat every real adapter alike."""

from __future__ import annotations

from oddtrue.exchanges.errors import Rejected

READ_ONLY = "read_only"


class ReadOnlyViolation(Rejected):
    """A caller asked a read-only adapter to do something that moves money or is not on its allowlist."""

    def __init__(self, what: str, *, exchange: str | None = None) -> None:
        super().__init__(
            f"{exchange or 'exchange'} adapter is read-only in this phase: {what}", exchange=exchange, code=READ_ONLY
        )
