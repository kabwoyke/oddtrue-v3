"""ExchangeAdapter: the only interface the engine sees (CLAUDE.md section 5).

One adapter instance is bound to ONE client's credentials on ONE exchange. Public market data is shared:
implementations must not make per-client order-book calls in the hot path (CLAUDE.md 4A).

Contract for every method: raise only the types in ``oddtrue.exchanges.errors``. Anything ambiguous
(timeout, 5xx, dropped connection) is ``UnknownOutcome`` and the caller reconciles by client id.
Order and margin actions are idempotent on their client id: a repeated id never executes twice, it raises
``Rejected(code="duplicate_client_id")`` and the caller queries the original.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from decimal import Decimal

from oddtrue.exchanges.models import (
    AccountType,
    Balance,
    Borrowable,
    BorrowRate,
    BorrowResult,
    DepositAddress,
    Fees,
    Loan,
    MarginAccount,
    OrderBook,
    OrderRequest,
    OrderResult,
    Permissions,
    RepayResult,
    TransferStatus,
    WithdrawLimits,
)
from oddtrue.pricing.types import SymbolRules

DUPLICATE_CLIENT_ID = "duplicate_client_id"
ORDER_NOT_FOUND = "order_not_found"  # a query for an id the exchange never saw: the action did NOT happen


class ExchangeAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    # ---- market data (shared across clients) ----
    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int) -> OrderBook: ...

    @abstractmethod
    def stream_orderbook(self, symbol: str, depth: int) -> AsyncIterator[OrderBook]:
        """Snapshot first, then updates. Raises ExchangeDown if the stream drops; the caller resyncs."""

    @abstractmethod
    async def get_fees(self, symbol: str) -> Fees: ...

    @abstractmethod
    async def get_symbol_rules(self, symbol: str) -> SymbolRules: ...

    # ---- account ----
    @abstractmethod
    async def get_balances(self, account: AccountType) -> list[Balance]: ...

    @abstractmethod
    async def get_margin_account(self) -> MarginAccount: ...

    @abstractmethod
    async def get_borrowable(self, asset: str) -> Borrowable: ...

    @abstractmethod
    async def get_liabilities(self) -> list[Loan]: ...

    @abstractmethod
    async def get_borrow_rate(self, asset: str) -> BorrowRate: ...

    # ---- orders (spot buy side and margin borrow/sell side share one entry point; ``req.account`` selects) ----
    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def get_order(self, client_order_id: str, symbol: str, account: AccountType) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, client_order_id: str, symbol: str, account: AccountType) -> OrderResult:
        """IOC/FOK orders are never resting, so this returns the terminal state of the order."""

    # ---- margin ----
    @abstractmethod
    async def borrow(self, asset: str, qty: Decimal, client_id: str) -> BorrowResult: ...

    @abstractmethod
    async def repay(self, asset: str, qty: Decimal, client_id: str) -> RepayResult: ...

    @abstractmethod
    async def get_loan(self, asset: str) -> Loan:
        """Principal plus accrued interest, as reported by the exchange (the source of truth)."""

    # ---- transfers (used from Phase 9) ----
    @abstractmethod
    async def get_deposit_address(self, asset: str, network: str) -> DepositAddress: ...

    @abstractmethod
    async def withdraw(
        self, asset: str, qty: Decimal, network: str, address: str, client_id: str
    ) -> TransferStatus: ...

    @abstractmethod
    async def get_transfer_status(self, client_id: str) -> TransferStatus: ...

    @abstractmethod
    async def get_withdraw_fee_and_min(self, asset: str, network: str) -> WithdrawLimits: ...

    # ---- permissions ----
    @abstractmethod
    async def check_permissions(self) -> Permissions: ...
