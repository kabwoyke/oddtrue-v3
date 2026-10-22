# DECISIONS

Record every decision here: date, decision, reason, who decided. Claude Code must read this at the start of every
session and must **ask** (not guess) about any item still OPEN that blocks the current phase.

## Open decisions

| # | Decision | Blocks | Options / default assumption | Status |
|---|---|---|---|---|
| 1 | Language / stack | Phase 0 | SETTLED → D-019 | SETTLED |
| 2 | Loan settlement method after a successful trade (where does the repayment coin come from?) | Phase 3 | SETTLED (default, configurable) → D-020 | SETTLED |
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
| 14 | Naked-position handling when A buy fails after B sold | Phase 3 | Immediate cover on B; define max acceptable loss and escalation | OPEN |
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
