# DATA MODEL (initial design)

Starting point for Phase 2 and Phase 8. Refine through migrations. Database: Postgres (assumed). All money,
price and quantity columns are `NUMERIC` (never float). All tables carry `created_at`; mutable tables also `updated_at`.
Every client-owned row has `client_id`, and every query on client data must be filtered by it (row-level security
recommended).

## Principles
- **Ledger is append-only.** Corrections are new compensating entries; never UPDATE or DELETE ledger rows.
- Exchange state is truth; the ledger is the expectation; reconciliation compares them.
- Every external action has a persisted idempotency key *before* it is sent.
- Raw exchange payloads are stored (access-controlled) for audit and dispute handling.
- Secrets are encrypted (envelope encryption); the database never holds plaintext keys.

## Identity and access
| Table | Key columns |
|---|---|
| `clients` | id, email, phone (masked in UI), status (`ONBOARDING`,`READY`,`ACTION_REQUIRED`,`TRADING`,`RECOVERY`,`RECONCILING`,`PAUSED`,`SUSPENDED`), transfer_mode (`AUTOMATIC`/`MANUAL`), participation_enabled, mfa_enabled, consent_version, consent_at |
| `exchange_credentials` | id, client_id, exchange (`binance`/`bitget`), key_id (public part, masked in UI), encrypted_secret, encrypted_extra (e.g. passphrase), dek_ref, permissions_json, withdrawal_enabled, last_checked_at, status |
| `audit_log` | id, actor, client_id?, action, detail_json, ip, at |

## Billing and entitlement
| Table | Key columns |
|---|---|
| `plans` | id, name, price_kes, period_days, active |
| `entitlements` | client_id, state (`PENDING_PAYMENT`,`ACTIVE`,`GRACE`,`LAPSED`,`SUSPENDED`), plan_id, valid_until, grace_until |
| `invoices` | id, client_id, plan_id, amount_kes, status, expires_at |
| `payments` | id, invoice_id, provider (`mpesa`), checkout_request_id, mpesa_receipt_number (UNIQUE), amount_kes, phone_masked, status, raw_callback_json, verified_via_query (bool), received_at |
| `payment_reconciliations` | id, run_at, differences_json |

## Trading
| Table | Key columns |
|---|---|
| `opportunities` | id, detected_at, symbol, buy_exchange, sell_exchange, class (`DIRECT`/`TRIANGULAR`), est_buy_vwap, est_sell_vwap, est_net_roi, executable_liquidity, market_cap_90, decision, reject_reason |
| `rotation_queue` | client_id, opportunity_id?, position, last_participated_at, last_skip_reason |
| `trades` | id, client_id, opportunity_id, state, role_buy_exchange, role_borrow_exchange, symbol, capital_used, allocated_95, reserve_5, outcome (`SUCCESS`,`RECOVERED`,`FAILED`,`CANCELLED`), started_at, finished_at |
| `trade_legs` | id, trade_id, leg (`A_BUY`,`B_BORROW`,`B_SELL`,`A_UNWIND`,`B_INTEREST_BUY`,`B_REPAY`,`COVER`), client_order_id (UNIQUE), exchange_order_id, requested_qty, filled_qty, avg_price, fee, fee_asset, status, raw_response_json |
| `loans` | id, trade_id, exchange, asset, principal, accrued_interest, outstanding, verified_zero_at |
| `trade_state_log` | trade_id, from_state, to_state, reason, at (supports crash-resume and audit) |

## Ledger and reporting
| Table | Key columns |
|---|---|
| `ledger_entries` | id, client_id, trade_id?, kind (`BUY_COST`,`SELL_PROCEEDS`,`FEE`,`INTEREST`,`SLIPPAGE`,`RECOVERY_COST`,`TRANSFER_COST`,`REBALANCE_COST`,`ADJUSTMENT`), amount_usdt, asset, qty, ref_leg_id, at |
| `trade_results` | trade_id, gross, fees, interest, slippage, recovery, transfer, net_profit, realized_roi, published_in_roi_list (bool) — derived from ledger, reconciled by test |
| `balance_snapshots` | client_id, exchange, taken_at, balances_json, margin_json, liabilities_json |
| `reconciliations` | id, client_id, at, expected_json, actual_json, diff_json, resolved_at, action |

## Transfers and inventory
| Table | Key columns |
|---|---|
| `inventory_targets` | client_id, exchange, asset, target_qty, updated_at |
| `transfers` | id, client_id, mode, asset, amount, from_exchange, to_exchange, network, est_fee, expected_received, status (see CLAUDE.md §8), whitelist_address_ref, txid, reason |
| `withdrawal_whitelist` | client_id, exchange, asset, network, address, added_by, approved_at |

## Operations
| Table | Key columns |
|---|---|
| `halts` | id, scope (`GLOBAL`/`EXCHANGE`/`CLIENT`), target, reason, set_by, set_at, cleared_at |
| `alerts` | id, severity, kind, client_id?, trade_id?, detail, at, acknowledged_by |

## Required invariants (each gets an automated test)
1. Sum of `trade_results` per client equals the sum of that client's `ledger_entries` (net).
2. A `trade` can reach `DONE` only if a `loans.verified_zero_at` exists for every loan it opened.
3. `payments.mpesa_receipt_number` is unique; replaying a callback changes nothing.
4. No trade is created for a client whose entitlement is not `ACTIVE` (or `GRACE` if allowed) or whose status is not eligible.
5. Every `trade_legs.client_order_id` is unique and written before the exchange call.
6. No row for client X is readable via any client-API call authenticated as client Y.
