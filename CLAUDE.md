# CLAUDE.md — Oddtrue Cross-Exchange Arbitrage Engine

Project guide for Claude Code. Read this fully before writing or changing code.
The source spec is `docs/ODDTRUE_SPEC.md` (the 62-section Part I + II document). The worked end-to-end example is
`docs/USER_JOURNEY.md` ("John", Binance + Bitget) — it is the acceptance reference (see §15). If this file and the
spec disagree, stop and ask — do not pick silently.

## 1. What we are building

A multi-client engine that captures price differences between two exchanges:

- **Exchange A (buy side):** spend USDT, BUY the coin.
- **Exchange B (sell side):** BORROW the same coin on margin, immediately SELL it for USDT.
- Later: repay the loan (principal + interest), reconcile, rebalance inventory.
- Both directions are evaluated (A→B and B→A), so each exchange can play either role per trade.

**Starting exchanges: Binance and Bitget.** Code must treat them as interchangeable adapters behind one interface,
never hard-code "Binance = A". Role (buy side / borrow-sell side) is decided per opportunity.

It is an **execution + recovery + liquidity + inventory** engine, not a price-spread calculator. Most of the code
and most of the tests live in recovery, reconciliation and safety, not in opportunity detection.

## 2. Non-negotiable rules (money safety)

Violating any of these is a bug, even if tests pass.

1. **Never use floats for money, price or quantity.** Use `Decimal` (Python) / a decimal library. Round only at
   the exchange boundary, using the exchange's step/tick rules, and always round in the direction that is safe
   (quantities down, repay amounts up).
2. **Never assume an order or API call failed.** On timeout/5xx/unknown error: query order status, balance and
   loan by client order id, reconcile, *then* decide. Never blindly retry a borrow, order or repay.
3. **Every order, borrow and repay carries an idempotency key** (`clientOrderId` or equivalent), derived
   deterministically from `trade_id + leg + attempt`. Persist it *before* sending.
4. **Do not borrow unless B's sell liquidity is pre-checked** (executable qty within max slippage). Then borrow,
   then sell immediately with a protected IOC/FOK limit order at a computed max-worse price. Never market-sell.
5. **Every trade must satisfy the master constraint (spec §41) in one place** — a single `validate_trade()` that
   returns pass/fail with the failing reason. No scattered partial checks.
6. **Recovery is a first-class state machine**, not an exception handler. A trade is not finished until
   `outstanding_loan == 0` is confirmed *from the exchange API*, not from our ledger.
7. **Exchange state is the source of truth; our ledger is the expectation.** Any mismatch freezes the affected
   client (`RECONCILING`) until resolved. No new trades for a frozen client.
8. **Hard time limits on every operation** (spec §37). On timeout: stop normal flow → reconcile → recovery.
   Nothing may wait indefinitely.
9. **Secrets:** API keys encrypted at rest (envelope encryption / KMS), never logged, never in tracebacks,
   never in test fixtures. Redact in all log formatters. Require keys **without withdrawal permission** unless
   the client explicitly enables automatic transfers (see §8).
10. **Kill switches:** global halt, per-exchange halt, per-client pause. Halt stops *new* trades only; in-flight
    trades must complete or recover. Every halt/resume is audit-logged.

## 3. Key numbers and where they apply (do not conflate)

| Control | Meaning | Applies to |
|---|---|---|
| 95% | Client's active execution allocation: `U × 0.95` | per client |
| 5% | Client's reserve: `U × 0.05`, **not auto-spent**; used for interest buy / recovery costs / headroom | per client |
| 90% | Max share of *executable* market liquidity we intentionally consume | per opportunity (all clients combined) |
| Rotation | Fair allocation of opportunity among clients when demand > 90% capacity | across clients |

Final allocation (spec §7):
`min(95% client cap, 90% market cap, available capital, borrowable qty, exchange limits)`,
then floor to lot size and re-check min notional and the 95%/90% caps *after* rounding.

All of these live in config (`config/risk.yaml`), never as magic numbers in code.
Reviewer note: spec applies 95% in both §2 and §7 — implement it **once** (as `trade_capacity`) and reference it.

## 4. Architecture

Suggested layout (adjust if repo already differs):

```
src/oddtrue/
  config/            # risk.yaml, pairs.yaml, exchange limits
  exchanges/
    base.py          # ExchangeAdapter ABC (the only interface the engine sees)
    binance.py
    bitget.py
    fake.py          # deterministic simulator used by tests & paper mode
  marketdata/        # order-book streams, snapshots, staleness checks (shared across all clients)
  ratelimit/         # central per-exchange / per-IP / per-key request budgeter with priority classes (§4A)
  pricing/           # VWAP, executable liquidity, slippage, fee/interest math (pure functions)
  detection/         # direct cross-exchange; triangular as a SEPARATE module
  allocation/        # 95/5, 90%, lot-size rounding, rotation queue
  execution/         # trade state machine, concurrent legs, protected orders
  recovery/          # failed-sell recovery, partial-fill handling, timeouts
  reconciliation/    # exchange-vs-ledger diffing, freeze/unfreeze
  inventory/         # target inventory, rebalance decisions, transfer manager
  ledger/            # append-only ledger, per-client accounting
  clients/           # onboarding, readiness checks, pause/resume, transfer mode
  api/               # client dashboard read API + live stream (authz per client)
  admin_api/         # operator-only API (separate permissions)
  observability/     # structured logs, metrics, alerts
web/
  dashboard/         # client dashboard UI (§9A)
  admin/             # operator console (separate app)
tests/
docs/ODDTRUE_SPEC.md
```

Rules of structure:
- `pricing/` and `allocation/` are **pure functions** (no I/O, no clock, no randomness). They must be 100% unit tested.
- The engine talks only to `ExchangeAdapter`. No Binance/Bitget-specific types leak past `exchanges/`.
- Execution and recovery are explicit **state machines** with persisted state. A crash at any point must resume
  correctly on restart by reloading state and reconciling against the exchanges.
- Use `asyncio` (or the language's equivalent) with order books on websockets and REST only for
  actions and reconciliation. Per-exchange rate limiters in the adapter layer.

## 4A. Scale target: 50–200 concurrent clients

The system must run **50 clients at launch and scale to 200** (400 exchange credential sets, hundreds of USDT
pairs, several trades per client per hour) without redesign. Design and test for **300 clients** (50% headroom).
Money safety (§2) is never traded away for throughput: if a limit is hit, the engine slows or skips — it does not cut corners.

**Where the load really is**
1. **Public market data is shared.** Fetch/stream each order book *once* per exchange and share it with all
   clients. Never fetch order books per client.
2. **Private calls are per client** (balances, borrowable, orders, borrow, repay, loan). These scale with clients
   and are the main rate-limit risk. Prefer **user-data websocket streams** for balances/order updates, with REST
   only for the pre-trade freshness check and for reconciliation.
3. **Burst when an opportunity qualifies.** One opportunity can be allocated to many clients at once. 200 clients ×
   (revalidation + 3–6 order/borrow/repay calls) is a burst of well over a thousand private requests, plus all
   clients selling into the *same* order book at the same moment.
4. **Shared egress IP(s).** All clients' keys are whitelisted to the platform's fixed IP(s), so IP-based exchange
   rate limits are shared across every client. Verify each exchange's per-IP, per-UID and per-connection limits
   (and websocket connection caps) and record them in `docs/exchanges/*.md`.

**Required design**
- **Central rate-limit budgeter** per exchange (per IP and per API key/UID): token buckets honoring the exchange's
  weights/headers, priority classes (1: recovery & repay, 2: trade execution, 3: revalidation, 4: reconciliation,
  5: background/dashboard). Recovery and repay calls must never starve behind scanning or dashboard traffic.
- **Single allocation authority.** Opportunity allocation and rotation run in exactly one place at a time
  (leader/lock) so two workers can never double-allocate the same liquidity. Allocation is computed for all
  clients *before* any order is sent; the 90% market cap is shared state.
- **Sharded execution workers.** Per-client trade state machines run in worker processes sharded by `client_id`
  (stateless apart from persisted trade state; any worker can resume any trade after a crash). One active trade
  per client at a time unless config explicitly allows more.
- **Market-impact control for simultaneous clients.** Because many clients hit the same book, the allocation
  step caps *total* size by the 90% executable-liquidity rule, and execution is **batched/staggered** with a fresh
  book read between batches; later batches are re-validated (and dropped if the opportunity no longer pays).
  Never let N clients each assume they have the whole book.
- **Bulkheads per client and per exchange.** A client with bad keys, slow API calls, or a stuck recovery must
  not slow others: per-client timeouts, circuit breakers, concurrency limits, and queues. An exchange outage halts
  only that exchange's work (EXCHANGE halt), not the whole engine.
- **Backpressure, not silent drops.** If the budgeter or queues are saturated, new opportunities are skipped and
  counted (metric + log reason); in-flight recovery always proceeds.
- **Connection management.** Reuse pooled HTTP connections and warm them; maintain websocket reconnect with
  resync (snapshot + diff) and staleness detection; respect connection caps; spread streams if limits require.
- **Database.** Postgres with indexes on `(client_id, created_at)`, partitioning or archiving for ledger/legs if
  growth requires; connection pooling; no long transactions in the hot path; hot trade state may be cached in
  Redis but Postgres is the source of truth. Expected volume is modest at 200 clients; correctness over cleverness.
- **Dashboard fan-out.** Live updates via SSE/websocket backed by pub/sub (e.g. Redis) so 200+ open dashboards
  don't poll the engine or the exchanges; dashboard reads never touch exchange APIs.
- **Horizontal scale path.** Workers, API and market-data service are separately deployable; the engine must
  tolerate 2+ instances of each (except the single allocation authority, which fails over via lock/lease).

**Scale config (in `config/risk.yaml`/`config/scale.yaml`, never magic numbers)**
`max_active_clients`, `max_concurrent_trades_global`, `max_concurrent_trades_per_client`, per-exchange request
budgets, `batch_size`, `batch_stagger_ms`, `queue_max_depth`, per-client timeouts.

**Scale SLOs (initial targets; tune from Phase 7 data and record in DECISIONS)**
- Allocation for 200 clients computed in well under the opportunity's useful lifetime (target < 100 ms).
- No client's recovery delayed by other clients' activity beyond its recovery budget.
- Zero duplicate allocations of the same liquidity; zero cross-client data leakage under load.
- Rate-limit bans/blocks from either exchange: zero (alert and auto-throttle before reaching limits).
- Reconciliation of all clients completes within its interval without blocking trading.

**Required load and fault testing (Phase 3 groundwork, Phase 7 full)**
- `FakeExchange` supports configurable **latency, rate limits (weights, bans), websocket drops, partial outages**,
  and 300 simulated clients with distinct balances/keys.
- Scenarios: all clients allocated to one opportunity; simultaneous sell-failure for many clients (recovery storm);
  exchange 429/ban mid-burst; one slow/hung client among 300; websocket reconnect storm; worker crash mid-burst;
  allocation-authority failover; DB failover/restart.
- Pass criteria: invariants in `docs/DATA_MODEL.md` hold; every loan returns to 0; rate-limit budgets never
  exceeded; recovery priority honored; no starvation of any client over a long run (rotation fairness measured).

## 5. Exchange adapter interface (minimum)

Every adapter implements (all async, all typed, all raising our own error types):

- Market: `get_orderbook(symbol, depth)`, `stream_orderbook`, `get_fees(symbol)`, `get_symbol_rules(symbol)`
  → `min_qty, max_qty, step, tick, min_notional`
- Account: `get_balances()`, `get_margin_account()` (collateral, health), `get_borrowable(asset)`,
  `get_liabilities()`, `get_borrow_rate(asset)`
- Spot (buy side): `place_order(...)`, `get_order(client_id)`, `cancel_order(...)`
- Margin (borrow/sell side): `borrow(asset, qty, client_id)`, `margin_sell(...)`, `repay(asset, qty, client_id)`,
  `get_loan(asset)` (principal + accrued interest)
- Transfers: `get_deposit_address`, `withdraw(...)`, `get_transfer_status(...)`, `get_withdraw_fee_and_min(...)`
- Permissions: `check_permissions()` → trade / margin / borrow / withdraw flags

Normalize all exchange errors into: `RateLimited`, `InsufficientFunds`, `InsufficientLiquidity`,
`InvalidQuantity`, `Timeout/UnknownOutcome`, `Rejected`, `AuthError`, `ExchangeDown`.
**`UnknownOutcome` is its own type** — it must always trigger query-and-reconcile, never a retry.

### Binance and Bitget — verify, don't assume
Do NOT rely on memory for endpoints, margin modes, order types, borrow/repay semantics, interest accrual
timing, rate limits, or symbol filters. Before implementing each adapter method:
1. Read the current official API docs (Binance: spot + margin/cross-margin; Bitget: v2 spot + margin).
2. Record the finding in `docs/exchanges/<exchange>.md` (endpoint, params, error codes, quirks, date checked).
3. Confirm on **testnet/demo or with minimum-size live calls** before trusting it.
Open questions to settle first (write answers into the docs above):
- Which pairs/assets are borrowable on each exchange, in cross vs isolated margin, and the borrow limits?
- Is interest accrued hourly, and in which asset? How is the loan/interest reported by the API?
- Does the exchange support IOC/FOK on margin sell orders? Is borrow-and-sell a single call or two?
- Can the interest be bought and repaid in the same margin account without a transfer?
- Fee tiers/BNB-style fee discounts, and whether fees are deducted from the received asset (affects exact qty).
- Withdrawal networks, minimums, fees, confirmations, and address whitelist requirements.

## 6. Trade lifecycle (state machine)

```
DETECTED → ALLOCATED → PRECHECK_B_LIQUIDITY → PRECHECK_BORROW → REVALIDATED
 → EXECUTING (A buy ∥ B borrow→protected sell)
     ├─ ALL_FILLED → SETTLING (repay principal+interest, verify loan=0) → RECONCILING → REBALANCE_EVAL → DONE
     └─ B_SELL_FAILED → RECOVERY:
            1. stop retrying the failed order
            2. query B coin balance, loan, accrued interest; query A coin balance
            3. sell A's coin → USDT (protected order)
            4. buy ONLY the interest amount on B from the client's 5% reserve (if B rules allow)
            5. repay principal + interest
            6. verify loan == 0 via API (else stay in RECOVERY, alert)
            7. RECONCILING → REBALANCE_EVAL → DONE (recorded as recovered/failed trade, with real cost)
```

Also handle explicitly, each with tests: partial fill on any leg; A buy fails *after* B already sold
(naked short — must buy back/cover on B immediately); borrow succeeds but order never submitted; repay fails;
exchange down mid-trade; duplicate response; process crash at each state; timeout at each state.

**Open design gap — settle with the owner before building SETTLING:** after a *successful* trade you hold coin on A
and owe coin on B. Repayment needs coin on B: either pre-held inventory on B, buying on B, or an A→B transfer
(spec §30 "Transfer 1"). The spec does not say which. Until decided, implement the interface and make the
chosen strategy configurable. Whatever is chosen, model the interest and price exposure while the loan is open.

## 7. Math reference (implement in `pricing/`, test against hand-computed cases)

- VWAP = Σ(price·qty) / Σ qty, walking levels until required qty is filled; compute buy and sell VWAP separately.
- Executable liquidity = depth within `max_slippage` of the best price.
- `gross = sell_qty·sell_vwap − buy_qty·buy_vwap`
- `net = gross − fees − interest − slippage − other`; `net_roi = net / capital_used × 100`
- Execute only if `expected_net_roi ≥ min_roi` **after** the final revalidation re-query.
- Interest estimate = `borrowed × rate × duration`; final accounting uses the exchange-reported accrued interest.
- Recovery cost = A unwind loss + B interest + recovery fees + other costs. Record actuals only.
- Triangular: `out = in × rate × (1 − fee)` per leg; compare *net executable* profit, legs, liquidity and risk
  against direct opportunities. It is a separate opportunity class — never forced into the direct flow.

## 8. Transfers and rebalancing

- Rebalance = `target_inventory − current_inventory`. **Never** mirror the last transfer; never equalize 50/50.
- Before any transfer check: min withdrawal, fee, expected received amount, network, sender balance,
  destination deposit availability, whether it would impair upcoming trades. Skip if a future trade will
  rebalance naturally or fee > benefit.
- **Automatic mode:** engine-initiated; requires withdrawal permission → treat as high risk:
  withdrawal only to **pre-whitelisted own-exchange addresses**, per-transfer and daily caps, amount derived
  strictly from the rebalance calc, full audit log. Never accept a destination address from user/runtime input.
- **Manual mode:** engine produces an instruction (asset, amount, from, to, network, est. fee, reason, urgency)
  and watches for the deposit. Arbitrage math is unchanged.
- Transfer mode can only change when the account is idle (not in trade, recovery, pending transfer, reconciling).
- Statuses: NO TRANSFER REQUIRED → TRANSFER REQUIRED → AWAITING USER TRANSFER → TRANSFER INITIATED →
  TRANSFER CONFIRMING → TRANSFER RECEIVED → RECONCILING → REBALANCED → READY.

## 9. Clients, ledger and reporting

- Per-trade ledger fields: spec §32. **Append-only**; corrections are new entries, never edits.
- Client balances update from **actual** realized figures, never estimates.
- Rotation: clients who don't execute stay eligible; a skipped client is not a failed client. Rotation must be
  deterministic and auditable (persist the queue and the reason each client was skipped/included).
- **ROI list vs. full record (owner's rule, implemented as follows):** spec §58 and the user journey say the
  "ROI earnings list" shows only positive realized ROI from successfully completed trades, while all losses,
  failed trades and recovery costs stay in internal accounting and in the client's balance. Implement exactly that
  list, **but** the same client dashboard must always also provide: (a) the full per-trade history including
  recovered/failed/cancelled trades with their costs, and (b) net realized profit and total losses/recovery costs
  for the selected period. The ROI list is labelled "Completed profitable trades" — it is never the only
  performance figure on screen, and its numbers are never summed or averaged into a headline "performance" figure
  on any public/marketing surface. Client balance always reflects actual results. Owner/legal to confirm this
  presentation (decision #4).
- Estimated ROI is never shown as earned ROI.

## 9B. Access model: paid subscription (M-Pesa) + user-supplied API keys

**Model:** the client signs up, pays via M-Pesa, supplies their own exchange API keys, and the engine trades on
their own exchange accounts. Funds always stay in the client's own Binance/Bitget accounts; Oddtrue never holds
client money and never receives it. Only **active paid** clients can be in the execution pool.

**Entitlement states:** `PENDING_PAYMENT → ACTIVE → GRACE → LAPSED` (plus `SUSPENDED` for admin/compliance).
- Eligibility to trade (spec §1) = account READY **and** entitlement `ACTIVE` (or `GRACE` if the owner allows).
- Entitlement is checked at allocation time and again at final revalidation, from the database, never from a cache.
- **Lapse/suspension behaves like Pause (spec §55):** stop new opportunities; let any active trade complete or
  recover; confirm loan == 0 and no open orders/transfers; then mark the account PAUSED. **Never abandon an open
  loan or position because a payment lapsed.** An unpaid client can still log in read-only to see history,
  statements and balances, and to remove API keys.
- Payment never changes risk rules, allocation limits or rotation fairness (unless the owner defines tiers).

**M-Pesa integration (Safaricom Daraja) — verify against current Daraja docs before building**
- Primary flow: **STK Push** (Lipa na M-Pesa Online) from the dashboard: client enters their phone number,
  confirms the PIN prompt on their phone. Optional fallback: **C2B** (pay to Paybill/Till with an account number)
  with validation/confirmation URLs, for clients whose STK Push fails.
- Needs: Daraja app, shortcode (Paybill/Till), passkey, consumer key/secret, HTTPS callback URLs, and Safaricom
  go-live approval. Sandbox first; production credentials only in the secret store.
- Treat the **callback as untrusted input**: verify by also calling the transaction-status / STK-query API using
  the `CheckoutRequestID` before granting access; match amount, shortcode, phone and account reference to the
  pending invoice; accept only expected source IPs/paths where supported; never trust client-side "I paid".
- **Idempotent:** key every payment on `MpesaReceiptNumber`/transaction id; a replayed or duplicate callback must
  never double-credit. Handle late callbacks, timeouts, user-cancelled, wrong amount, overpay/underpay,
  wrong phone, and callbacks that arrive after the invoice expired (reconcile manually, don't silently drop).
- Reconciliation job: periodically compare our payments table to M-Pesa statements/transaction status; flag
  differences to the admin console.
- Store: invoice, payment attempt, receipt number, amount, phone (masked in UI), timestamps, raw callback payload
  (access-controlled). Issue a receipt/statement the client can download.
- Money values in KES as integers/Decimal; amount, plan, period and expiry computed server-side only.

**Plans and renewal**
- Plans, prices and billing period live in config/DB (not code). Default assumption: fixed periodic subscription
  (e.g. monthly) — see open decision on profit-share. Renewal reminders before expiry (SMS/email/push), a
  configurable grace period, then lapse as above.
- Refund/dispute handling is an admin workflow with an audit trail; refunds never alter trading ledger entries.

**User-supplied API keys**
- Onboarding wizard tells the client exactly which permissions to enable: **read + spot trade + margin
  borrow/trade; withdrawals OFF** (withdrawal only if they explicitly choose Automatic transfer mode, §8).
- Credential sets differ per exchange: key + secret, plus extras where required (e.g. **Bitget needs an API
  passphrase**). The adapter declares its required credential fields; the onboarding form is generated from that.
  The client creates keys on the exchange themselves and **never shares exchange passwords or 2FA codes**.
- Provide the platform's **fixed egress IP(s)** so clients can IP-whitelist their keys on the exchange.
- Reject keys that have withdrawal enabled (unless automatic mode is chosen with whitelisted addresses),
  keys that lack required permissions, or keys that fail the readiness checks. Show the exact fix.
- Encrypt per-client with envelope encryption; decrypt only inside the execution service, in memory, for the
  shortest time; never expose to the dashboard/API/logs; support revoke/rotate; delete on account closure.
- Detect key revocation or permission changes mid-operation (auth errors) → treat as an incident: stop new
  trades, run recovery if a loan is open, notify the client.

**Terms, risk disclosure and compliance (must exist before real clients)**
- Terms of service + clear risk disclosure (arbitrage can lose money; recovery costs; exchange outages; borrow
  rates; no guaranteed returns). Explicit consent recorded with timestamp and version.
- No guaranteed-return marketing; dashboard shows real results (see §9). KYC/AML needs and licensing for
  operating an automated trading service for other people's accounts (Kenya: check Capital Markets Authority and
  the virtual-asset service provider regime, plus data protection/ODPC registration and tax obligations) are
  **owner/legal-counsel decisions** — do not assume; record the outcome in `docs/DECISIONS.md`.

## 9A. User dashboard (web app, per-client)

Each client gets a dashboard to track their own account, trades and profit. It is a **read-mostly view over the
ledger and reconciled exchange state**; the only actions it can trigger are the ones listed under "Controls".

**Data rules**
- Every number comes from the append-only ledger or the latest reconciled exchange snapshot. The UI never
  computes money itself and never calls an exchange directly. Show "last updated" and a stale-data warning.
- Display realized figures only as realized; anything projected is labelled "estimated" and visually distinct.
- A client sees only their own data (row-level authorization on every query; tests that try cross-client access).
- Reported totals must reconcile: sum of trade rows == headline totals. Add a test for this.

**Pages / panels**
1. **Overview:** total equity, total realized profit (net), realized ROI (period + all-time), today / 7d / 30d /
   all-time P&L, win/loss/recovered counts, current status (READY, TRADING, RECOVERY, RECONCILING, PAUSED,
   ACTION REQUIRED), and the 95% active / 5% reserve split with current allocated amount.
2. **Accounts:** per exchange (A/B role shown as Binance/Bitget): USDT and crypto balances, available trading
   balance, margin balance and health, borrowed asset, accrued interest, available borrowing, transfer status.
3. **Live activity:** active opportunity/trade with its lifecycle state, rotation status ("in queue", position,
   last participated), open loans, pending transfers. Updates in near real time (websocket/SSE).
4. **Trade history:** paginated, filterable (date, pair, exchange, outcome, direction) and sortable. Per trade:
   time, pair, direction, quantities, buy/sell VWAP, gross profit, fees, interest, slippage, recovery and
   transfer costs, **net profit and realized ROI**, outcome (SUCCESS / RECOVERED / FAILED / CANCELLED), and a
   detail drawer with the fills and timeline. **Losses and recovery costs are shown**, not hidden (see §9).
5. **Profit & ROI analytics:** equity curve, daily/weekly/monthly profit, ROI over time, profit by pair and by
   exchange direction, cost breakdown (fees vs interest vs slippage vs recovery vs transfers).
6. **Transfers:** history and status; in manual mode, the pending instruction (asset, amount, from, to, network,
   estimated fee, reason, urgency) with a "waiting for your transfer" state and deposit detection.
7. **Exchange connections:** connection health, permission check results, masked key identifiers only
   (never display or return a secret), reconnect/rotate-key flow, READY / ACTION REQUIRED with exact fixes.
8. **Billing:** current plan, status (ACTIVE / GRACE / LAPSED), expiry date, "Pay with M-Pesa" (STK Push) with
   live payment status, payment history and downloadable receipts, renewal reminders. Lapsed users see
   read-only data and a pay button; trading controls are disabled.
9. **Settings:** transfer mode (Automatic / Manual, locked unless account idle), pause/resume, notification
   preferences (email/push/Telegram for trade results, recovery events, action-required, transfer needed).
10. **Statements & export:** CSV/PDF export of trade history and period statements, matching the ledger exactly.

**Controls (the only write actions)**
- Pause / Resume (resume runs the fresh checks in spec §56), change transfer mode (when idle), update/rotate
  API keys, notification settings, perform manual transfers externally and confirm. Nothing else.
- No control for choosing opportunities, sizes or borrow amounts (spec §49).

**Security and quality**
- Strong auth (MFA required), session expiry, CSRF protection, rate limiting, audit log of logins and setting
  changes. Secrets are write-only: accepted on input, never returned by any endpoint.
- Responsive (works on phone), accessible, clear empty/error/loading states, plain-language status text so the
  client always knows what the system is waiting for.
- API: versioned REST/GraphQL read endpoints + SSE/websocket stream, typed contract (OpenAPI), contract-tested.
- Admin/operator views (all clients, global halt, reconciliation queue, rotation audit) are a **separate app and
  permission set**, never mixed into the client dashboard.

## 10. Config and modes

- `MODE=paper|testnet|live-min|live`. Default `paper`. `live` requires an explicit env var AND a config flag AND
  a non-empty allowlist of client ids. Never default to live in any script, test or Docker image.
- `live-min`: real exchanges with hard-capped tiny notional (e.g. ≤ $10/trade) for adapter validation.
- All thresholds in `config/risk.yaml`: `client_trade_pct=0.95`, `client_reserve_pct=0.05`,
  `market_liquidity_pct=0.90`, `max_slippage`, `min_net_roi`, `op_timeout_s`, `recovery_timeout_s`,
  per-client and global max exposure, max concurrent trades, pair allow/deny lists.

## 11. Testing requirements

- Unit tests for every pure function; property tests (hypothesis) for rounding/lot-size/VWAP invariants
  (e.g. rounded qty never exceeds the 95%/90% caps; VWAP lies between best and worst level touched).
- `FakeExchange` adapter with injectable faults: timeouts that actually fill, partial fills, rejects,
  stale order books, borrow success + sell failure, duplicate responses, rate limits, balance drift.
- Scenario tests for **every recovery path** in §6 and for crash-and-resume at each state transition.
- Reconciliation tests: induce ledger/exchange mismatch → client freezes → resolves.
- A change to `pricing/`, `allocation/`, `execution/` or `recovery/` is not done without tests.
- Run before declaring any task complete: lint, type-check (strict), full test suite.

## 12. Observability

- Structured JSON logs with `trade_id`, `client_id`, `exchange`, `leg`, `state`, `client_order_id`. Redact secrets.
- Metrics: opportunities found/executed/cancelled, revalidation drop-off rate, fill latency per leg,
  recovery count and cost, loans open > N seconds, reconciliation mismatches, API error rates per exchange.
- Alerts (page-worthy): loan not zero after recovery timeout; reconciliation mismatch; unknown-outcome unresolved;
  naked position; exchange down with in-flight trades; reserve below minimum.

## 13. Build phases (strict order, gated)

Work **one phase at a time**. A phase is complete only when every exit criterion is met and the owner has said
"phase N approved". Do not start phase N+1 early, do not build ahead "while you're there", and do not skip a
phase because the code looks easy. Keep `docs/PROGRESS.md` updated (phase, status, what's done, what's blocked).
At the start of each session, read `docs/PROGRESS.md` and `docs/DECISIONS.md` first.

### Phase 0 — Foundations and decisions
- Repo skeleton, tooling (lint, strict type-check, test runner, pre-commit secret scanning), CI.
- `docs/DECISIONS.md` filled for §16 open items 1–3 at minimum; config schema and `risk.yaml` stubs.
- **Exit:** CI green on an empty-but-wired project; decisions recorded; secrets scanning proven to block a fake key.

### Phase 1 — Pure math core (no network, no exchanges)
- `pricing/`: VWAP, executable liquidity within slippage, fees, interest estimate, gross/net profit, net ROI.
- `allocation/`: 95/5 split, 90% market cap, lot-size/tick/min-notional rounding, final `min(...)` allocation,
  rotation queue, `validate_trade()` (master constraint §41).
- **Exit:** 100% coverage on these modules; property tests for rounding and VWAP invariants; every worked example
  in the spec (the $500, $50,000/$45,000, 0.123456 BTC, 0.003 BTC cases) reproduced as a test.

### Phase 2 — Exchange interface, fake exchange, ledger
- `ExchangeAdapter` ABC and error taxonomy (incl. `UnknownOutcome`); `FakeExchange` with fault injection
  (timeouts-that-fill, partial fills, rejects, stale books, borrow ok + sell fail, rate limits, balance drift) **plus configurable latency, rate-limit weights/bans,
  websocket drops and N simulated clients (target 300) — required for the scale tests in §4A**.
- Append-only ledger and persistence layer (schema + migrations); per-client accounting.
- **Exit:** the fake exchange can simulate a full margin borrow/sell/repay cycle; ledger reproduces spec §32/§33
  figures exactly; fault injection is switchable per test.

### Phase 3 — Execution and recovery state machines (against FakeExchange only)
- Trade lifecycle (§6), pre-checks, final revalidation, concurrent legs, protected sell, success settlement.
- Failed-sell recovery, naked-short cover (A buy fails after B sold), partial fills, timeouts, duplicate protection.
- Persisted state with crash-and-resume at every transition.
- **Exit:** every scenario in §11 green; kill-the-process-at-each-state tests resume correctly; loan == 0 verified
  in all recovery tests. This is the most important phase — do not rush it.

### Phase 4 — Reconciliation, freeze logic, observability
- Exchange-vs-ledger diffing, client freeze/unfreeze, kill switches, structured logs, metrics, alerts.
- **Exit:** induced mismatches freeze only the affected client; halt switches stop new trades but let in-flight
  trades finish or recover; alert conditions in §12 demonstrably fire.

### Phase 5 — Real exchange adapters, read-only first
- 5a Binance: market data + account reads only (order books, rules, fees, balances, margin account, borrowable,
  borrow rate, loan/interest). Document findings in `docs/exchanges/binance.md`.
- 5b Bitget: same.
- Market-data layer: websocket books, staleness detection, per-exchange rate limiting; opportunity detection
  running on **live data with no trading**.
- **Exit:** adapter contract tests pass for both exchanges and for `FakeExchange` identically; detection logs
  would-be opportunities with net ROI; zero order/borrow endpoints are reachable from this phase's code paths.

### Phase 6 — Trading on testnet/demo, then minimum-size live
- 6a Testnet/demo (where each exchange offers it) for orders, borrow, repay.
- 6b `live-min`: real funds, hard-capped tiny notional, dedicated test account owned by the owner, allowlist of one.
  Exercise: successful trade, deliberately forced sell-failure recovery, partial fill, timeout, repay verification.
- **Exit:** a documented run of real small trades where the ledger matches the exchanges to the cent and every
  loan returns to 0; realized costs vs. estimates compared and the estimate model adjusted.
- Needs the owner's explicit go-ahead for any real-money step.

### Phase 7 — Paper/shadow trading at scale
- Run the full engine on live data without sending orders: multi-client rotation, allocation, throughput, rate
  limits, latency. Compare simulated fills against real order-book movement.
- **Scale test (§4A):** simulate 50, 100, 200 and 300 clients against live market data and a rate-limiting
  fake/replay of private endpoints; run the burst, recovery-storm and failure scenarios; measure the SLOs; tune
  batch size/stagger and request budgets; document results in `docs/SCALE_REPORT.md`.
- **Exit:** extended run (duration agreed with the owner, not less than several days) with no unexplained
  reconciliation mismatch, stable latency, and realistic simulated net ROI after all costs.

### Phase 8 — Client onboarding, readiness, user dashboard
Split into sub-phases, each separately approved:
- **8a Onboarding backend:** registration/verification hooks, MFA, encrypted key storage, permission checks,
  READY / ACTION REQUIRED, pause/resume.
- **8b Dashboard API:** read endpoints + live stream per §9A, row-level authorization, OpenAPI contract,
  statement/CSV export. Built and tested against ledger data produced by Phases 3 and 7.
- **8c Dashboard UI:** Overview, Accounts, Live activity, Trade history, Analytics, Transfers (status only until
  Phase 9), Connections, Settings, Exports, per §9A. Mobile-responsive.
- **8d Subscriptions and M-Pesa billing (§9B):** plans, invoices, entitlement state machine, Daraja sandbox
  integration (STK Push, callback verification via status query, idempotent receipts, reconciliation job),
  entitlement gating in the allocation/execution pool, lapse-as-pause behavior, Billing page, admin payment queue.
  Production Daraja credentials and go-live only after sandbox tests pass and the owner approves.
- **Exit:** onboarding rejects keys with unsafe permissions; secrets never appear in logs or responses;
  pause leaves no open loans/orders; dashboard totals equal ledger totals (automated test); cross-client access
  attempts are denied (automated test); losses and recovery costs appear in trade history and net totals;
  UI tested with seeded data covering success, recovered, failed, cancelled, paused and action-required states;
  an unpaid or lapsed client can never be allocated a trade (automated test); a duplicate/replayed/forged
  M-Pesa callback never grants or extends access (automated tests); lapse during an open trade completes or
  recovers it first, loan == 0, then pauses.

### Phase 9 — Inventory, rebalancing and transfers
- Target inventory model and rebalance decisions first (no transfers); then **manual** transfer instructions and
  deposit detection; **automatic** transfers last, with whitelisted addresses and caps (§8).
- Wire the dashboard Transfers page and transfer-status statuses to real data (manual instruction card,
  deposit detection, history).
- **Exit:** rebalance decisions match hand-computed cases; transfer-status machine covered by tests; automatic
  mode cannot send to any non-whitelisted address under any input.

### Phase 10 — Limited live rollout
- Few real clients, small caps, owner-supervised, then widen gradually (client count, size, pairs, directions).
- **Client ramp: 5 → 20 → 50 → 100 → 200.** Each step needs a stable stretch (agreed duration) with no unresolved
  reconciliation mismatch, no rate-limit ban, SLOs in §4A met, and owner approval before raising
  `max_active_clients`. The cap is enforced in config, so onboarding beyond the current step is waitlisted.
- Runbooks written before rollout: incident response, stuck loan, exchange outage, key compromise, manual halt.
- **Exit per step:** a stretch with no unreconciled discrepancy and no open loan past its timeout before raising limits.

### Gate rules (apply to every phase)
- Never merge a phase with failing tests, lint or type errors.
- Never carry a known money-handling bug forward "to fix later"; fix it or stop and report.
- If a later phase reveals a flaw in an earlier one, go back and fix it there, with a regression test.
- Rollback plan for any phase that touches real funds is written before it runs.

## 14. How Claude should work in this repo

- Follow the phases in §13. State which phase you are in at the start of every task, and refuse (politely) to
  implement items from a later phase until the current one is approved.
- Never run anything that touches real funds or real API keys without the user's explicit instruction in that turn.
- Never log, print, commit or paste secrets. Never add keys to code, tests or `.env.example` values.
- When the spec is ambiguous or contradictory, list the options and ask; record the decision in
  `docs/DECISIONS.md` (date, decision, reason).
- Prefer small, reviewable changes. Explain *why* for any change in money-handling code.
- Do not "simplify" safety logic (revalidation, pre-checks, reconciliation, timeouts) to make tests pass or speed up code.

## 15. Acceptance scenarios (from `docs/USER_JOURNEY.md`, "John", Binance + Bitget)

Each journey step becomes an automated end-to-end test (first against `FakeExchange`, later against testnet /
`live-min`). A phase's exit criteria include the scenarios assigned to it. Use the journey's numbers where given.

| # | Journey step | Scenario test | Phase |
|---|---|---|---|
| 1 | Register, verify, open Exchange Connections | New account gets an isolated, empty dashboard | 8a |
| 2 | Connect Binance (key/secret) | Credentials encrypted; connectivity + permissions checked; balances read; secret never in logs/responses | 5a, 8a |
| 3 | Connect Bitget (key/secret/**passphrase**) | Spot, margin and borrow permissions + collateral/limits/liabilities/health read; account not READY until borrowing is confirmed *actually available* | 5b, 8a |
| 4 | Readiness check ($500 usable) | All checks pass → READY; any failure → ACTION REQUIRED with a specific, fixable message (min order sizes, restrictions, margin health) | 8a |
| 5 | Preferences | Participate on/off, transfer mode, pause/resume; client cannot pick opportunities or borrow sizes | 8a/8c |
| 6 | Capital rules | $500 → $475 trade / $25 reserve; allocation = min(capital, 90% liquidity, borrow capacity, exchange limits); reserve untouched unless a permitted use | 1 |
| 7 | Scan | VWAP both sides, fees, interest, slippage → net ROI; reject below threshold or on any liquidity/borrow/lot-size/safety failure | 1, 5 |
| 8 | Opportunity qualifies | Buy on the cheaper exchange, borrow+protected sell on the dearer one; full revalidation immediately before; concurrent submission; actuals recorded, estimates never booked as profit | 3 |
| 9 | Both legs succeed | Actual fills/fees/interest recorded; repay principal+interest; liability verified 0; both exchanges reconciled; rebalance evaluated; ledger + dashboard updated | 3, 4 |
| 10 | Sell fails after borrowing | No assumption of success; check order/balances/loan; unwind bought BTC via protected order; buy only interest BTC from reserve; repay; loan verified 0 else **controlled recovery state** that accepts no new trade; unwind loss + costs kept in ledger | 3 |
| 11 | Rebalance | Needs-inventory calc; transfer only the necessary amount after fees/minimums/future demand; automatic vs manual instruction card; receiving balance reconciled before "restored" | 9 |
| 12 | Dashboard + ROI | Balances, margin, borrowed, interest, 95/5 split, rotation, active opportunity, realized ROI list, total realized profit; estimated ROI never shown as earned; presentation per §9 | 8b/8c |
| 13 | Pause / resume | Pause: no new assignments, active orders/loans/recovery resolved; resume: fresh API/balance/margin/borrow/inventory/transfer checks before rejoining rotation | 4, 8a |
| – | Isolation | Two clients run concurrently; balances, credentials, trades, loans, transfers, ROI never mix (cross-client access test; ledger keyed by client) | 2, 8b |
| – | Key hygiene | Withdrawal permission disabled unless an authorized transfer feature needs it; destination/network controls | 8a, 9 |
| – | Paid access | The same journey, with entitlement ACTIVE required; lapse mid-trade completes/recovers first (§9B) | 8d |

## 16. Open decisions (owner to answer; track in docs/DECISIONS.md)

1. Language/stack (default assumption: Python 3.12 + asyncio + Postgres + Redis, strict typing).
2. Settlement method for repaying the loan after a successful trade (see §6 gap).
3. Margin mode (cross vs isolated) on each exchange.
4. Wins-only ROI publication (see §9) — recommended: do not ship as written.
5. Custody/legal model: are clients' own API keys used under their own accounts (assumed), and what licensing or
   disclosure applies to operating this for third parties in your jurisdiction?
6. Initial pair universe and which direction(s) to enable first.
7. Pricing model: fixed subscription (assumed) vs profit-share/performance fee (needs exact-ledger fee
   calculation, high-water-mark rules and stronger legal review), plan tiers, prices (KES), grace period length.
8. M-Pesa setup: Paybill vs Till, who owns the shortcode, STK Push only vs STK + C2B fallback, refund policy.
9. Licensing/KYC/data-protection position in Kenya (see §9B) and where the platform is hosted (fixed egress IP).
10. Definition of "usable capital U" (journey: "$500 across his connected accounts"): the buy side needs USDT on
    one exchange while the borrow side needs collateral on the other. Decide how U is measured and split per
    exchange, and how per-exchange balances cap the allocation (`min(...)` in §3 must use per-exchange figures).
11. Exact readiness thresholds (min margin health, min capital, min collateral) and the "ACTION REQUIRED" messages.
