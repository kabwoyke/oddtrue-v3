# RUNBOOKS

Operational procedures. Must be written and rehearsed (tabletop, then in testnet/live-min) **before Phase 10**.
Each runbook: trigger → immediate actions → verification → follow-up. Keep steps short and unambiguous.

> Status: TEMPLATES, with the Phase 4 mechanics filled in (halts, freezes, alerts). The operator-only admin API and
> console arrive in Phase 8; until then operators use the Python API shown below from an authenticated shell. Contacts,
> dashboards and on-call rota are still to be filled in.

## General rules
- Safety first: stop *new* trades, then resolve open positions. Never abandon an open loan.
- Prefer the engine's own recovery state machine over manual exchange actions; if manual action is needed, record it
  in the ledger as an adjustment with a reason.
- After any incident: reconcile affected clients, write a short post-mortem, add a regression test.

## Engine alert reference (Phase 3)
What each alert from `oddtrue.execution.deps.AlertKind` means, what the engine has already done, and the runbook to use.
In every case the trade stays in its persisted state and the client cannot start another trade (one unfinished trade
per client); nothing is retried blindly. `advance(client, trade)` safely resumes a parked trade.

| Alert | Engine state | Already done | Next |
|---|---|---|---|
| `naked_position` | RECOVERY | Short on B detected; covering with protected buys (escalating) | RB-3 if it escalates |
| `cover_escalation_exhausted` | RECOVERY (held) | All ladder rungs tried (D-026), no market order sent | RB-3: owner decides, then resume |
| `loan_not_zero_after_recovery_timeout` | RECOVERY (held) | Past `recovery_timeout_s`, still retrying each pass | RB-2 |
| `reserve_insufficient` | RECOVERY (held) | Interest/shortfall purchase would exceed the 5% reserve | RB-2: owner tops up reserve (plan `reserve_5`) or funds the coin, then resume |
| `unknown_outcome_unresolved` | EXECUTING/SETTLING/RECOVERY (held) | Status queries by client id kept failing; nothing re-sent | Check exchange status, then resume; RB-4 if outage |
| `reconciliation_mismatch` | RECONCILING (held) | Ledger fill differs from the exchange's order record | RB-5: compensating entry, never edit |
| `exchange_down_in_flight` | current (held) | Calls failed after retries | RB-4 |
| `auth_incident` | cancelled (pre-trade) or held | Key rejected/revoked | RB-6 |
| `loan_present_before_trade` | cancelled | A loan for the coin already existed | RB-5 (unexplained borrow) |
| `dust_left` (not a page) | continues | Coin below the exchange minimum order left on A | none; sweep during rebalancing |

## Operator commands (Phase 4, from an authenticated Python shell)
```python
halts = HaltController(ledger, clock, alerts, metrics)  # oddtrue.safety.halts
await halts.halt_global("reason", "ops:alice")  # stop NEW trades everywhere
await halts.halt_exchange("bitget", "reason", "ops:alice")  # stop new trades touching one exchange
await halts.halt_client("john", "reason", "ops:alice")  # pause one client
await halts.resume_global("reason", "ops:alice")  # likewise resume_exchange / resume_client
await halts.active()  # what is halted now
safety = SafetyLedger(ledger)  # oddtrue.ledger.safety
await safety.frozen_clients()
await safety.active_freeze("john")  # who is frozen and why
await safety.audit_entries(client_id="john")  # the append-only audit trail
await reconciler.recheck("john", venues, "ops:alice")  # lifts a freeze only if the books now match
await reconciler.accept_exchange_state("john", venues, "ops:alice", "client deposited 50 USDT by hand")
await engine.advance("john", "<trade_id>")  # resume one parked trade
await supervisor.sweep()  # resume all parked trades + reconcile idle clients
```
Every halt, resume, freeze and unfreeze is audit-logged with actor and reason; reasons are mandatory.

## RB-1 Global halt / exchange halt / client pause
- Trigger: runaway behaviour, exchange incident, security concern, owner decision.
- Actions: set halt (scope GLOBAL / EXCHANGE / CLIENT) → confirm no new trades start → list in-flight trades.
- Verify: in-flight trades complete or recover; open loans reach 0; no open orders.
- Resume: fresh readiness checks (API, balances, margin, borrow, inventory, transfers) before rejoining rotation.
- A halt never stops in-flight trades or their recovery; do not try to "force" one by editing rows. An exchange that fails
  repeatedly is halted automatically (actor `system:health`); lifting that halt is a human decision.

## RB-2 Stuck loan (loan not zero after recovery timeout)
- Trigger: alert "loan not zero after recovery timeout".
- Actions: freeze client → query exchange for principal, interest, coin balances on both exchanges →
  determine missing amount → acquire/transfer the minimum needed → repay → verify loan == 0 via API.
- If repayment impossible (insufficient funds/collateral): escalate to owner; consider notifying client; document.
- Follow-up: ledger adjustment for costs; reconciliation; root cause.

## RB-3 Naked position (one leg filled, the other did not)
- Trigger: A buy failed after B sold (short exposure), or A holds coin after B failure and unwind failed.
- Actions: freeze client → determine exact exposure from exchange APIs → cover/unwind with protected orders →
  verify flat → repay loans → reconcile.

## RB-4 Exchange outage or degraded API
- Trigger: repeated `ExchangeDown`/`RateLimited`/timeouts.
- Actions: EXCHANGE halt → identify clients with open trades/loans → monitor exchange status → recover when back
  (never assume state; query first) → reconcile all affected clients before resuming.

## RB-5 Reconciliation mismatch
- Trigger: exchange state differs from ledger expectation.
- Actions: client auto-frozen (RECONCILING) → capture both snapshots → classify (missed fill, duplicate,
  manual client action on exchange, fee/interest rounding, bug) → fix via compensating entries → unfreeze.
- Escalate if unexplained; do not unfreeze on guesses.
- Look at the stored reconciliation first: `safety.reconciliations(client)[-1]` holds `expected`, `actual` and `diff`.
  A positive delta means the exchange holds MORE than the ledger accounts for. Resolve with `recheck` (books match again)
  or `accept_exchange_state` with a note (e.g. a deposit the client made). A parked trade then finishes via `engine.advance`.

## RB-6 API key compromise or revocation
- Trigger: client reports compromise; unexpected permission change; auth errors.
- Actions: stop new trades for the client → recover any open loan if keys still work → ask client to revoke/rotate
  keys on the exchange → mark credentials revoked → re-onboard with new keys → review audit log and access.
- If Oddtrue's own storage may be compromised: GLOBAL halt, rotate KMS/master keys, notify all clients, follow legal obligations.

## RB-7 Payment incident (M-Pesa)
- Trigger: callback failures, duplicate/forged callbacks, reconciliation differences, client paid but not activated.
- Actions: check payments queue → verify via transaction-status query → apply entitlement manually only after
  verification, with audit entry → fix root cause.
- Lapse during open trade: complete/recover first, then pause (never abandon positions).

## RB-8 Automatic transfer anomaly
- Trigger: transfer stuck, wrong network, unexpected amount, whitelist mismatch.
- Actions: disable automatic transfers for the client (and globally if systemic) → trace txid → contact exchange
  support if needed → reconcile balances → update inventory plan.

## RB-9 Deploy and rollback
- Deploy only when all clients are idle or in a safe state, or via draining (stop new trades, wait for in-flight completion).
- Rollback plan documented per release; database migrations backward compatible where possible.

## RB-10 Backup and restore
- Scheduled encrypted backups; periodic restore drill; verify ledger integrity after restore (invariants in DATA_MODEL.md).

## Contacts and escalation (fill in)
| Role | Name | Contact |
|---|---|---|
| Owner | | |
| On-call engineer | | |
| Legal counsel | | |
| Binance support | | |
| Bitget support | | |
| Safaricom Daraja support | | |
