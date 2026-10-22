# PROGRESS

Update at the end of every work session. Claude Code reads this at the start of every session.

**Current phase:** 5 — Real exchange adapters, read-only (5a Binance, 5b Bitget)
**Phase status:** IN PROGRESS: 5a Binance and 5b Bitget adapters built (read-only); market-data/detection layer still to do
**Last updated:** 2026-10-22
**Real-money activity so far:** none

## Phase tracker

| Phase | Name | Status | Approved by owner | Notes |
|---|---|---|---|---|
| 0 | Foundations and decisions | approved | Owner, 2026-10-22 | Decisions 1–3 recorded (D-019..D-021) |
| 1 | Pure math core | approved | Owner, 2026-10-22 | D-022, D-023; 100% branch coverage |
| 2 | Exchange interface, fake exchange, ledger | approved | Owner, 2026-10-22 | D-025 Postgres; 99% coverage on exchanges+ledger |
| 3 | Execution + recovery state machines (fake exchange only) | approved | Owner, 2026-10-22 | D-026, D-027, D-028; ~300 kill points swept; 99% coverage |
| 4 | Reconciliation, freeze logic, observability | approved | Owner, 2026-10-22 | D-029..D-031; 336 tests; 100% on pure math, 99% elsewhere |
| 5 | Real adapters, read-only (5a Binance, 5b Bitget) | in progress | – | No order/borrow endpoints reachable |
| 6 | Testnet, then minimum-size live (6a, 6b) | not started | – | Needs explicit go-ahead for real money |
| 7 | Paper/shadow trading at scale | not started | – | Several days minimum |
| 8 | Onboarding, dashboard, billing (8a API keys/onboarding, 8b dashboard API, 8c dashboard UI, 8d M-Pesa) | not started | – | |
| 9 | Inventory, rebalancing, transfers | not started | – | Manual first, automatic last |
| 10 | Limited live rollout | not started | – | Runbooks required first |

Status values: `not started` · `in progress` · `blocked` · `awaiting approval` · `approved`.

## Current session log

Newest first. One entry per session.

### 2026-10-22 — Phase 5a signed run (owner, read-only Binance key)
- Result: every signed Binance read worked live: permissions (read only, trade/margin/borrow/withdraw all False, as intended), fees 0.001/0.001, spot and margin balances, margin account (no debt), loans (none), borrowable and hourly borrow rate. No error, no remapping needed. Recorded in `docs/exchanges/binance.md`. The Bitget signed run has not been reported yet.
- Finding for the owner: this account's spot wallet holds Simple Earn tokens (`LDUSDT`, whose balance grows with interest) and dust coins (PEOPLE, TRY, BTC at 5e-8). D-029 treats any balance change the engine did not make as a mismatch, so a client holding earn products would freeze every time interest accrues. **Decision needed (propose D-035):** reconcile only USDT and the coins in the pair universe and ignore everything else, with the readiness check telling the client that earn tokens and other assets are outside the engine's scope. Until you decide, nothing changes in code. (Balances in this account are far below any usable capital, so readiness would currently say ACTION REQUIRED: minimum capital, decision #11.)
- Next: Bitget signed run, then `detect_live.py`.

### 2026-10-22 — Phase 5 market-data and detection layer
- Approval: owner said "continue" after Phase 5b. This is the last Phase 5 piece: detection on live data with no trading.
- Done: `marketdata/` (`BookCache`: freshness, never-older-replaces-newer, invalidation; `BookFeed`: per-symbol resync with backoff/`retry_after`, survives unexpected errors). `detection/` (`direct.evaluate`: pure, same cost model as revalidation, both directions; `AdapterCosts`: TTL-cached rules/fees/rates with explicit, logged overrides; `Scanner`: log-only, open/close transitions with lifetimes, stale-book and cost-failure handling). `exchanges/factory.py` (adapters from env/.env), Binance websocket connector error normalisation, `scripts/detect_live.py`. New metrics (book updates/resyncs, detection scans/rejects, opportunity lifetime). D-034.
- Tests: 36 new across detection (hand-computed figures, property test on the 90% cap), cache, feed, scanner, a two-fake-exchange end-to-end run with a websocket drop and resync, connector errors, factory, and the boundary test (detection/marketdata cannot import or call anything that trades). 100% branch coverage on `detection/direct.py` and `marketdata/cache.py`; gates added to `verify.sh` and CI.
- Found live: with the sandbox's DNS failing for the Binance stream host, the connector leaked a raw socket error and the feed task died silently (books stale, so nothing was invented, but nothing was reported either). Fixed and regression-tested. Detection against live data therefore has NOT yet produced a result: Binance's stream host did not resolve here (and later nothing did). Bitget polling worked (35 updates in 30 s). Re-run `scripts/detect_live.py` from your machine.
- Limits: Bitget books come from 1 s REST polling, not a websocket (not acceptable at Phase 7 scale). Binance stream caps (streams per connection, connections per IP) not read yet. Detection is market-level (no client balances, borrow limits or per-order caps such as Bitget `maxLimitOrderValue`). No triangular detection (a separate module, later). Fees/borrow rate need keys or `--assume-*` flags.
- Phase 5 exit status: adapter contract tests for both exchanges + the fake (the two real adapters have their own suites; a single shared contract suite run identically against all three is NOT written yet), "detection logs would-be opportunities with net ROI" (built, awaiting a live run from the owner), "zero order/borrow endpoints reachable" (enforced by allowlist + boundary tests). Remaining before "phase 5 approved": (a) one live detection run and signed smoke runs from your machine, (b) the shared adapter contract suite, which I can write next, (c) your Classic-vs-UTA answer (D-033).
- Next (on your word): the shared contract suite, then Phase 5 approval; Phase 6 needs your explicit go-ahead for any real-money step.

### 2026-10-22 — Phase 5b (Bitget, read-only)
- Approval: owner said "continue with phase 5b".
- Docs: bitget.com/api-doc pages render only in a browser, so the v2 reference was read from the server-rendered catalog pages and recorded in `docs/exchanges/bitget.md` with the method used. Signing came from the legacy doc and the v2 example headers. Public endpoints and the error envelope were confirmed live (a deliberately fake key returned `40037`).
- Done: `exchanges/bitget/` (`http.py`: GET-only allowlist, signed with key/secret/**passphrase**, envelope handling, per-IP + per-UID budgets and a per-path minimum interval; `adapter.py`: order book with matching-engine timestamps, symbol rules from precisions, fees, spot and cross-margin balances, margin account valued in USDT, borrowable (flag + live max), loans, hourly borrow rate, permissions from `authorities`). Money-moving methods raise the shared `ReadOnlyViolation` (`exchanges/readonly.py`; Binance now uses it too). `scripts/exchange_readonly_smoke.py binance|bitget` replaces the Binance-only script. `verify.sh` and CI cover both exchanges. D-033.
- Tests: 33 new (tests/test_bitget_readonly.py): signature against an independent HMAC, no secrets in URLs/repr, every write method refused with zero requests, allowlist guard, error envelope on HTTP 200, local ban, per-second limit, USDT valuation and the refusal to guess a price, polling stream. ~99% coverage on the Bitget code. The design flaw found while testing: a "1 call/second" limit cannot be expressed as budget weight (the priority floors forbid it), so it is a per-path interval.
- Open / limits: **Bitget Classic vs UTA question for the owner (D-033).** Signed endpoints are verified from docs only (no key used); interest accrual timing, auth error codes other than 40037, and the global ban policy are unconfirmed. Websocket depth is not built (REST polling stands in). `maxLimitOrderValue` and the limit-price band are not modelled yet (Phase 6). `margin/currencies` pair flags not used yet.
- Blocked / needs owner: (1) answer the Classic-vs-UTA question; (2) optionally run `scripts/exchange_readonly_smoke.py bitget` with a READ-ONLY key (withdrawals off) and the Binance one, and send the output; (3) say what comes next: the Phase 5 market-data/detection layer on live data (websocket books, staleness, opportunity detection that only logs) is the remaining Phase 5 work before approval.

### 2026-10-22 — Phase 5a (Binance, read-only)
- Approval: owner said "continue" after Phase 4 was approved. Phase 5a only; 5b (Bitget) and the opportunity-detection layer are not started.
- Done: docs read first and recorded with dates in `docs/exchanges/binance.md`. `exchanges/binance/`: signed GET transport with a fixed read-only path allowlist and error normalisation (`http.py`); pure diff-depth book sync with gap detection (`depth.py`); `BinanceReadOnlyAdapter` (order book REST + stream, symbol rules, fees, spot/margin balances, margin account, borrowable, liabilities/loan, hourly borrow rate, permissions); every order/borrow/repay/transfer method raises `ReadOnlyViolation`. `ratelimit/WeightBudget` (priority classes, reserved floors, usage-header sync, ban honoured locally). `scripts/exchange_readonly_smoke.py` for the owner. D-032.
- Verified live without a key: depth REST (weight 5 matches docs), exchangeInfo filters (BTCUSDT step 0.00001, tick 0.01, min notional 5), the depth stream synced from snapshot and applied updates with no gap, and the adapter returned a book and rules from real Binance. Signed endpoints are verified from docs only: no API key has been used.
- Tests: 40 new (tests/test_binance_readonly.py): parsing, weight tiers, signature correctness, secrets absent from URLs/repr, every write method refused with zero requests, allowlist guard, error mapping, 429 local ban, budget priorities, depth gap handling, stream. Coverage binance ~99%, ratelimit 100% (gates updated).
- Open / limits: signed endpoints need a read-only key to confirm (run the smoke script); `marginLevel` 999 handling, margin fee = spot fee, ticker weight and the IP weight limit are assumptions flagged in binance.md; websocket connection caps not yet read; no detection layer yet; margin pair/borrowable universe flags (`allPairs`, `allAssets`) not used yet.
- Blocked / needs owner: (1) optionally run `BINANCE_API_KEY=... BINANCE_API_SECRET=... .venv/bin/python scripts/exchange_readonly_smoke.py` with a READ-ONLY key (withdrawals off) and tell me the output, or say to go on without it; (2) say "continue" for Phase 5b (Bitget docs, adapter).
- Next: Phase 5b Bitget, then the market-data/detection layer on live data with no trading.

### 2026-10-22 — Phase 4
- Approval: owner said "phase 3 approved, move to phase 4". No open decision blocked this phase (notification channels, #15, are Phase 8, so alerts go through a channel interface).
- Done: **Reconciliation** (`reconciliation/`): pure snapshot/expected/diff modules; `Reconciler` compares the exchanges with the last known-good snapshot plus settled legs, freezes only the affected client on a mismatch, defers while trades are in flight or a leg is unknown, reports "unavailable" without guessing, and lifts a freeze only by a clean recheck or an operator accepting the exchange state with a note. The engine reconciles before a trade (cancelling a drifted client's trade before money is at risk) and after it (parking the trade, not DONE, until resolved), and a leg-level mismatch also freezes. **Kill switches** (`safety/`): global, per-exchange and per-client halts, audit-logged, readiness-gated resume, authoritative database check inside trade creation; per-exchange auto-halt from consecutive infrastructure failures; an `InstrumentedAdapter` decorator that measures every call. **Observability** (`observability/`): JSON logs with trade context and redaction of keys, text and tracebacks, an in-process metrics registry with Prometheus output, and an alert hub that de-duplicates, persists, counts and pages. **Supervisor**: a sweep that resumes parked trades, reconciles idle clients, and raises loan-age, reserve and exchange-down alerts. Migration 0003 (audit log, halts, reconciliations, freezes, alerts).
- Exit criteria, each with tests: induced mismatches freeze only the affected client (2 drifted among 21 clients -> exactly 2 frozen; a manual deposit, an unknown asset, a surprise loan, drift mid-trade, a leg-level mismatch); halts stop new trades but in-flight trades finish or recover (halt set mid-EXECUTING, halt during a parked recovery, exchange halt blocks only trades touching it, client pause isolated); all section 12 alerts demonstrably fire, page and persist (loan not zero after recovery timeout, reconciliation mismatch, unknown outcome unresolved, naked position, exchange down with in-flight trades, reserve below minimum, plus loan open too long).
- Tests: 336 passing. 100% branch coverage on pricing, allocation and the three pure reconciliation modules; 99% on exchanges, ledger, engine, recovery, reconciliation, safety and observability (CI gates 100% / 98%, read from a single coverage run). A second crash-and-resume sweep (4 scenarios x every kill point, ~300 more) runs with the full safety stack and proves a crash anywhere never leaves a client falsely frozen and the books always reconcile. `scripts/verify.sh` runs everything in about 4 minutes.
- Found and fixed along the way: the reserve-minimum alert paged for a healthy client with no margin wallet on its buy-side exchange (now only where a margin account exists); a bare `key=<value>` in free text slipped through redaction (now caught; short ordinary values are left alone); `StartRefused` now carries a reason; the entitlement/halt gate runs at `start()` as well as at revalidation.
- Notes / limits: reconciliation costs about six private reads per client and runs before and after each trade; user-data streams are the planned optimisation (Phase 5/7). Any balance change not made by the engine (a deposit, a withdrawal, a manual trade) is a mismatch by design. Alerts reach a channel interface only; real channels (email, SMS, Telegram) are Phase 8. There is no operator console yet: operators use the Python API in docs/RUNBOOKS.md from an authenticated shell until the admin API (Phase 8). The client directory the supervisor needs is an interface until Phase 8a. Metrics are in-process with a Prometheus text renderer; no HTTP endpoint is exposed yet.
- Blocked / needs owner: say "phase 4 approved" to begin Phase 5 (real Binance/Bitget adapters, read-only first; nothing in it may reach an order or borrow endpoint). Phase 5 needs API access to read the official docs and, for 5a/5b, read-only API keys for a test account - none have been requested or used so far. Numeric limits (#12) are still provisional.
- Next: Phase 5a - Binance market data and account reads only, documented in docs/exchanges/binance.md.

### 2026-10-22 — Phase 3
- Approval: owner said "move to phase 3" (treated as "phase 2 approved"). Two owner decisions taken during the phase: D-026 (naked short: escalating protected cover) and D-027 (settlement: pre-held inventory on B).
- Found before building: D-020's default (buy the coin back on B to repay) captures no arbitrage profit; owner chose pre-held inventory (D-027). Caveat recorded: with enough B inventory a direct sale would match the proceeds without borrowing, so the borrow adds only a billed hour of interest. Revisit with Phase 6/7 data.
- Done: `execution/` (states, persisted `TradePlan`, injected deps/clock/alerts/gate/rebalance hook, `Gateway` with hard timeouts and query-by-client-id resolution, leg runners, engine with pre-checks, revalidation through the single `validate_trade`, concurrent A-buy and B borrow->protected-sell under one time limit, reconciling) and `recovery/` (exposure derived from persisted legs, escalating protected ladder, naked-short cover, surplus unwind, shortfall purchase within the 5% reserve, full repay with API-verified loan == 0, recovery deadline alerts). Migration 0002 (plan, leg meta, loans, once-per-leg entries, DB guard against finishing with an unverified loan). `validate_recovery` shares the recovery terms with `validate_trade`. Fake exchange: hang faults, hourly interest billing, duplicate counter.
- Tests: 235 passing, 99% branch coverage on pricing, allocation, exchanges, ledger, execution, recovery (CI gates: 100% pure math, 98% the rest). Scenarios: success with hand-computed figures; 15 pre-check cancellations; sell rejected / protected sell unfilled / borrow rejected / everything fails / neither order fills; A buy fails after B sold (naked short); A and B partial fills; escalating cover and exhausted cover; interest purchase from the reserve and reserve too small; timeouts that fill and that do not (buy, sell, borrow, repay) never re-sent; hangs under the hard limit; execution time limit; unresolvable outcomes hold and resume; exchange down, rate limit, auth failure; reconciliation mismatch; late-appearing loan; 3 clients at once. Crash-and-resume sweep: 6 scenarios x every checkpoint (~300 kill points), each resumed on a new connection and engine, asserting loan == 0 from the exchange, no duplicate sends, ledger == exchange leg by leg, no unhedged coin, bounded loss. Mutation-checked: a re-send bug is caught at `sent:A_BUY:1`.
- Bugs found and fixed by the tests: concurrent legs corrupted one shared DB connection (ledger now serialises per task); interrupted repay was resolved against a fabricated "before" figure (now persisted on the leg); resolve used the caller's quantity instead of the leg's; an unexpected rejection of a status query escaped as a crash (now unresolved/held); a resumed trade could send a fresh entry order from a stale revalidation (now it never does).
- Notes / limits: interest is billed per started hour in the fake (assumption, verify in Phase 5). One DB connection with a per-task lock is fine here; a pool comes with Phase 7. RECONCILING compares legs and the loan only; balance-level reconciliation, client freeze and kill switches are Phase 4. Alerts go to an in-memory sink until Phase 4. Entitlement gate and rebalance evaluator are hooks (Phases 8d, 9). `buy_on_borrow_side` and `transfer_to_borrow_side` settlement are not implemented (config accepts them; only pre-held inventory runs).
- Blocked / needs owner: say "phase 3 approved" to begin Phase 4. Numeric limits (#12) still provisional.
- Next: Phase 4 - reconciliation, freeze logic, kill switches, structured logs, metrics, alerts.

### 2026-10-22 — Phase 2
- Approval: owner said "continue with the next phase" after Phase 1; treated as "phase 1 approved".
- Found: Phase 2 code (adapter ABC, errors, models, FakeExchange, SQLite ledger) already sat in the initial commit with no tests and no PROGRESS entry. Reviewed it, kept the exchange side, replaced SQLite with Postgres (D-025).
- Done: `docker-compose.yml` (Postgres 16); Postgres migration + checksum-verified migrator with advisory lock; `ledger.repository.Ledger` (leg intent persisted before sending, idempotent per trade+leg+attempt, compare-and-set state transitions with log, append-only entries, trade results derived from entries only, per-client filtered reads, rotation audit + last-participation for `plan_rotation`). psycopg dependency; CI Postgres service and coverage gate.
- Tests: 135 passing (61 new), incl. `tests/test_multi_coin.py`: USDT is the only constant asset, the traded coin is any base (ETH, SOL, DOGE, 1000SHIB, USDC...) with its own step/tick/min-notional, separate per-coin loans, full borrow/sell/repay cycle per coin. FakeExchange 100% branch coverage: full borrow/sell/buy-back/repay cycle, timeout-that-fills and timeout-not-applied (query by client id, duplicate id never executes twice), partial fill, reject, rate limit, exchange down, auth error, stale book, stream drop and resync, latency on fake clock, weighted per-IP/per-client limits with ban and expiry, 300 clients on one book with no leakage. Ledger: append-only triggers block UPDATE/DELETE/TRUNCATE, spec §32/§33 figures, recovered trade keeps losses in net, sum of trade results == client total, cross-client access denied, migration tamper detection, exact decimal round trip. ruff, mypy strict, secret scan clean.
- Notes: `get_borrowable` returns unrounded Decimals; the engine must floor to step. Fake borrow-pool/collateral model is simplified (single mid price, fixed leverage); real limits come from Phase 5 docs. Not yet built (Phase 3+): connection pool, Redis, balance-drift injection beyond `drift()`, worker-sharding.
- Blocked / needs owner: say "phase 2 approved" to begin Phase 3. Run `docker compose up -d --wait` before `pytest` locally (DB tests skip otherwise). Nothing committed.
- Next: Phase 3 — trade state machine and recovery against FakeExchange.

### 2026-10-22 — Phase 1
- Done: `pricing/` (types, VWAP walk, executable liquidity within slippage, profit/fees/interest/net ROI, protected limit price, rounding) and `allocation/` (95/5 split, `client_trade_capacity`, 90% `market_cap`, `final_allocation` = min of caps floored to a step valid on both exchanges then re-checked, rotation, single `validate_trade()` covering spec §41). All pure Decimal, no I/O or clock.
- Tests: 74 passing; 100% branch coverage on both packages (enforced in CI); hypothesis property tests for VWAP bounds, floor/ceil rounding, allocation never exceeding any cap, rotation capacity/determinism, and "anything allocated passes validate_trade". Spec examples reproduced: $500→$475/$25, $50,000→$45,000, 0.123456→0.12345 BTC, 0.003000+0.000020=0.003020 BTC, $2 interest purchase, §8 $100k demand vs $45k capacity. ruff, mypy strict, secret scan clean.
- Decisions: D-022 (U is per exchange; trade capacity = 0.95 × min), D-023 (longest-waiting-first with size fit).
- Notes: rounding lives in `pricing/rounding.py` (re-exported by `allocation`) to avoid a circular import. Lot step is treated as a multiple from zero; Binance's LOT_SIZE is offset from min_qty, so verify per exchange in Phase 5. `ABOVE_CAP_AFTER_ROUNDING` is a guard unreachable by construction (pragma no cover). Interest rate input is per hour; adapters must normalize. Rotation persistence is Phase 2.
- Blocked / needs owner: say "phase 1 approved" to begin Phase 2. Open numeric limits (#12) are still provisional.
- Next: Phase 2 — ExchangeAdapter ABC + error taxonomy, FakeExchange with fault injection, append-only ledger and migrations.

### 2026-10-22 — Phase 0
- Done: repo skeleton (`src/oddtrue/*` per CLAUDE.md §4), `pyproject.toml` (ruff, mypy strict, pytest, hypothesis), `.gitignore`, `.env.example` (names only), `git init`, `.pre-commit-config.yaml`, GitHub Actions CI, `scripts/scan_secrets.py`, typed config schema + loader (`oddtrue.config`), `config/risk.yaml`, `scale.yaml`, `pairs.yaml`. Floats rejected in YAML; live mode needs env var + config flag + allowlist; default `paper`.
- Tests added / passing: 14 passed (config, mode gating, secret scanner). ruff clean, mypy strict clean, secret scan clean on all tracked files, and proven to block a fake key.
- Decisions made: D-019 stack, D-020 settlement (configurable, default buy on B), D-021 cross margin (provisional).
- Blocked / needs owner: say "phase 0 approved" to begin Phase 1. Numeric limits in risk.yaml are PROVISIONAL placeholders (decision #12). Nothing committed yet; run `pre-commit install` in the venv if you want the local hook active. CI not yet run (no remote).
- Next: Phase 1 (pricing/ and allocation/ pure math). Needs decisions #10 (usable capital U per exchange) and #13 (rotation algorithm) answered first.

## Known issues / risks

| ID | Description | Severity | Phase found | Status |
|---|---|---|---|---|
| | | | | |

## Acceptance scenarios (from CLAUDE.md §15)

Mark each when its automated test passes.

| # | Scenario | Test passing |
|---|---|---|
| 1 | Register / isolated dashboard | [ ] |
| 2 | Connect Binance | [ ] |
| 3 | Connect Bitget (with passphrase) | [ ] |
| 4 | Readiness check → READY / ACTION REQUIRED | [ ] |
| 5 | Preferences | [ ] |
| 6 | Capital rules (95/5, 90%) | [x] (Phase 1: tests/test_spec_examples.py) |
| 7 | Scan and net ROI | [ ] |
| 8 | Qualifying opportunity executes | [x] (Phase 3, FakeExchange: tests/test_engine_happy.py) |
| 9 | Both legs succeed → settle | [x] (Phase 3, FakeExchange) |
| 10 | Sell fails after borrowing → recovery, loan = 0 | [x] (Phase 3, FakeExchange: tests/test_engine_recovery.py) |
| 11 | Rebalance (automatic / manual) | [ ] |
| 12 | Dashboard + ROI presentation | [ ] |
| 13 | Pause / resume | [~] (Phase 4: halts/pause stop new trades only and are audit-logged; readiness checks on resume are Phase 8a) |
| – | Client isolation | [ ] |
| – | Key hygiene (no withdrawal permission) | [ ] |
| – | Paid access / lapse mid-trade | [ ] |
