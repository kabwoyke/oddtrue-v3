# PROGRESS

Update at the end of every work session. Claude Code reads this at the start of every session.

**Current phase:** 1 — Pure math core
**Phase status:** AWAITING APPROVAL (exit criteria met, see session log; Phase 0 approved by owner 2026-10-22)
**Last updated:** 2026-10-22
**Real-money activity so far:** none

## Phase tracker

| Phase | Name | Status | Approved by owner | Notes |
|---|---|---|---|---|
| 0 | Foundations and decisions | approved | Owner, 2026-10-22 | Decisions 1–3 recorded (D-019..D-021) |
| 1 | Pure math core | awaiting approval | – | D-022, D-023; 100% branch coverage |
| 2 | Exchange interface, fake exchange, ledger | not started | – | |
| 3 | Execution + recovery state machines (fake exchange only) | not started | – | Most important phase |
| 4 | Reconciliation, freeze logic, observability | not started | – | |
| 5 | Real adapters, read-only (5a Binance, 5b Bitget) | not started | – | No order/borrow endpoints reachable |
| 6 | Testnet, then minimum-size live (6a, 6b) | not started | – | Needs explicit go-ahead for real money |
| 7 | Paper/shadow trading at scale | not started | – | Several days minimum |
| 8 | Onboarding, dashboard, billing (8a API keys/onboarding, 8b dashboard API, 8c dashboard UI, 8d M-Pesa) | not started | – | |
| 9 | Inventory, rebalancing, transfers | not started | – | Manual first, automatic last |
| 10 | Limited live rollout | not started | – | Runbooks required first |

Status values: `not started` · `in progress` · `blocked` · `awaiting approval` · `approved`.

## Current session log

Newest first. One entry per session.

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
| 8 | Qualifying opportunity executes | [ ] |
| 9 | Both legs succeed → settle | [ ] |
| 10 | Sell fails after borrowing → recovery, loan = 0 | [ ] |
| 11 | Rebalance (automatic / manual) | [ ] |
| 12 | Dashboard + ROI presentation | [ ] |
| 13 | Pause / resume | [ ] |
| – | Client isolation | [ ] |
| – | Key hygiene (no withdrawal permission) | [ ] |
| – | Paid access / lapse mid-trade | [ ] |
