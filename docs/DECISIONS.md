# DECISIONS

Record every decision here: date, decision, reason, who decided. Claude Code must read this at the start of every
session and must **ask** (not guess) about any item still OPEN that blocks the current phase.

## Open decisions

| # | Decision | Blocks | Options / default assumption | Status |
|---|---|---|---|---|
| 1 | Language / stack | Phase 0 | SETTLED → D-019 | SETTLED |
| 2 | Loan settlement method after a successful trade (where does the repayment coin come from?) | Phase 3 | SETTLED → D-027 (supersedes D-020) | SETTLED |
| 3 | Margin mode per exchange (cross vs isolated) | Phase 3/5 | PROVISIONAL → D-021, verify in Phase 5 | SETTLED (provisional) |
| 4 | ROI presentation (positive-only "Completed profitable trades" list + mandatory full history and net totals, see CLAUDE.md §9) | Phase 8c | Implemented as in CLAUDE.md §9; owner + legal to confirm | OPEN |
| 5 | Legal/custody model (clients' own keys on their own accounts; licensing, KYC/AML, data protection in Kenya) | Phase 10 (before any real clients) | Get legal advice; record outcome | OPEN |
| 6 | Initial pair universe and enabled directions | Phase 5/6 | Start small: a few liquid, borrowable pairs; one direction first | OPEN |
| 7 | Pricing model: fixed subscription vs profit share; tiers; KES prices; grace period | Phase 8d | Default: fixed monthly subscription | OPEN |
| 8 | M-Pesa setup: Paybill vs Till, shortcode owner, STK Push only vs + C2B, refund policy | Phase 8d | Default: STK Push, Paybill/Till TBD | OPEN |
| 9 | Hosting and fixed egress IP(s) for client key whitelisting | Phase 5/8 | | OPEN |
| 10 | Definition of "usable capital U" and per-exchange split (buy-side USDT vs borrow-side collateral) | Phase 1 | SETTLED → D-022 | SETTLED |
| 11 | Readiness thresholds (min margin health, min capital, min collateral) and ACTION REQUIRED messages | Phase 8a | | OPEN |
| 12 | Numeric limits: max_slippage, min_net_roi, timeouts, max exposure, max concurrent trades | Phase 1/3 | Start conservative; tune in Phases 6–7 | OPEN |
| 13 | Rotation algorithm (fairness rule, tie-breaks, queue persistence) | Phase 1 | SETTLED → D-023 (queue persistence still to do in Phase 2) | SETTLED |
| 14 | Naked-position handling when A buy fails after B sold | Phase 3 | SETTLED → D-026 | SETTLED |
| 15 | Notification channels (email, SMS, push, Telegram) | Phase 8 | | OPEN |
| 16 | Mobile app later, or responsive web only | Phase 8c | Default: responsive web | OPEN |
| 17 | Scale targets: launch 50 clients, grow to 200, design/test for 300 (CLAUDE.md §4A); confirm SLOs and the ramp 5→20→50→100→200 | Phase 3/7/10 | Default as in §4A | OPEN |
| 18 | Number of fixed egress IPs (more IPs spread per-IP rate limits but every client must whitelist all of them); hosting region close to the exchanges' API endpoints | Phase 5/8 | Start with 2 IPs (primary + failover) | OPEN |
| 19 | Waitlist and capacity policy when `max_active_clients` is reached | Phase 8/10 | Waitlist, first-come, owner-controlled cap | OPEN |

## Settled decisions

| Date | Decision | Reason | Decided by |
|---|---|---|---|
| (date) | Starting exchanges are Binance and Bitget behind a common adapter interface | Owner choice | Owner |
| (date) | Clients supply their own API keys; funds stay on their own exchange accounts | Owner choice (user journey) | Owner |
| (date) | Access is paid; payment via M-Pesa | Owner choice | Owner |
| (date) | Work is built in gated phases (CLAUDE.md §13) | Owner choice | Owner |
| (date) | Withdrawal permission disabled on keys unless an authorized automatic-transfer feature requires it | Security | Owner |

### D-019 — Stack
- Date: 2026-10-22
- Decision: Python (>=3.12; dev machine runs 3.14, CI pins 3.12) + asyncio, Postgres, Redis, strict typing (mypy strict, ruff, pytest, hypothesis). Web UI in TypeScript (React/Next) in Phase 8c. Pydantic v2 for config schema.
- Decided by: Owner

### D-020 — Loan settlement method
- Date: 2026-10-22
- Decision: Settlement is configurable (`settlement_method` in risk.yaml). Default `buy_on_borrow_side`: buy the coin on B to repay, no transfer, shortest loan window. `preheld_inventory` and `transfer_to_borrow_side` are supported by the interface and selectable later.
- Consequences: Phase 3 SETTLING must model interest and price exposure while the loan is open for all three methods.
- Decided by: Owner

### D-021 — Margin mode (provisional)
- Date: 2026-10-22
- Decision: Cross margin by default (`margin_mode: cross`). Provisional: must be verified against Binance and Bitget docs/testnet in Phase 5 and re-confirmed before Phase 6.
- Decided by: Owner

### D-022 — Usable capital U is per exchange
- Date: 2026-10-22
- Decision: Each exchange has its own usable capital (`U_buy` = USDT usable on the buy side, `U_sell` = collateral usable on the borrow side). `trade_capacity = 0.95 × min(U_buy, U_sell)`, implemented once in `allocation.capacity.client_trade_capacity`. The 5% reserve is held per exchange (`0.05 × U_x`); the borrow side's reserve funds the interest purchase in recovery. The final allocation also caps by actual available buy-side USDT and actual borrowable quantity.
- Consequences: Conservative on purpose; it never lets the sum of balances stand in for what one exchange can deploy. Revisit with Phase 6/7 data.
- Decided by: Owner (recommended option accepted)

### D-023 — Rotation: longest-waiting-first with size fit
- Date: 2026-10-22
- Decision: Order candidates by last participation time (never-participated first), tie-break by client_id. Walk the queue and include each client whose allocation fits the remaining 90% market capacity; a client that does not fit is skipped, stays eligible and keeps its place. Each decision carries a reason. The function is pure (time is an input); persisting the queue and reasons is Phase 2 ledger work.
- Decided by: Owner (recommended option accepted)

### D-025 — Ledger database is Postgres (Docker for dev/test)
- Date: 2026-10-22
- Context: an earlier Phase 2 draft used SQLite. CLAUDE.md §4A and D-019 make Postgres the source of truth; SQLite differs in exactly the places a money ledger cares about (decimal storage, concurrency, trigger semantics).
- Decision: Postgres 16 via `docker-compose.yml` (port 55432, throwaway credentials, tmpfs data) for dev and tests; CI uses a Postgres service container. Money columns are `NUMERIC`. Driver: psycopg 3 (async). Append-only enforced by triggers (UPDATE/DELETE/TRUNCATE rejected) on `ledger_entries`, `trade_state_log`, `rotation_log`. Trade state changes are compare-and-set. DB tests skip locally if Postgres is down but fail in CI.
- Consequences: connection pooling, partitioning and Redis hot-state are deferred to Phase 3/7. Production credentials come from the secret store, never this compose file.
- Decided by: Owner (Docker provided for this purpose)

### D-026 — Naked short: escalating protected cover
- Date: 2026-10-22
- Decision: When the A buy fails (or under-fills) after B has sold, cover the short on B at once with a protected IOC buy. If it does not fully fill, retry on each rung of `recovery_slippage_ladder` (default 0.2%, 0.5%, 1%; the last rung is the hard cap), each rung with a fresh book read and a new client order id. If still not covered at the cap or past `recovery_timeout_s`, stop, keep the trade in RECOVERY, raise a page-worthy alert and accept no new trade for that client. Never a market order. The same ladder is used to unwind surplus coin on A.
- Decided by: Owner (recommended option accepted)

### D-027 — Settlement: pre-held inventory on B (supersedes D-020)
- Date: 2026-10-22
- Context: D-020's default (buy the coin back on B) repays the loan at about the price B sold at, so it captures none of the arbitrage profit (A holds coin worth what was paid; B nets minus its spread and fees). Profit needs the repayment coin to come from the cheap side.
- Decision: Default `settlement_method: preheld_inventory`. The loan is repaid with coin the client already holds in B's margin wallet; the coin bought on A becomes A-side inventory and is moved back by rebalancing (Phase 9). Pre-check skips the opportunity if B's margin wallet lacks principal plus estimated interest. `transfer_to_borrow_side` stays an interface option (needs withdrawal permission and a longer loan window). `buy_on_borrow_side` remains only as the fallback for a repay shortfall (e.g. interest), recorded as a recovery cost.
- Consequences: capital is split between USDT and per-coin inventory on both exchanges. If B holds enough coin, a direct sale of that coin would give the same proceeds without borrowing (the borrow adds only seconds of interest); to be revisited with Phase 6/7 data. Net-ROI estimation excludes any B buy-back cost because none is planned.
- Decided by: Owner (recommended option accepted)

### D-028 — Ledger conventions for execution (Phase 3)
- Date: 2026-10-22
- Decision: Every fill is booked from the exchange result: BUY_COST / SELL_PROCEEDS (USDT notional, coin qty), coin-denominated fees converted at the fill price into FEE. Gross profit of a trade = sell proceeds - buy cost over all its fills, so unwinds and covers show up as losses automatically. Slippage is NOT booked separately (it is already inside actual fill prices; booking it would double count). Interest repaid from wallet coin is booked as INTEREST at the B sell price; coin bought only to cover a repay shortfall is booked as RECOVERY_COST (notional) and is excluded from INTEREST. Entries are written once per (leg, kind) so a crash-and-resume never double-books. Coin inventory moving between exchanges is not P&L.
- Decided by: Claude (implementation convention; owner may revise)

## Template for new entries

```
### D-NNN — Title
- Date:
- Context:
- Options considered:
- Decision:
- Consequences / follow-ups:
- Decided by:
```
