# M-Pesa (Safaricom Daraja) — verified findings

**Status: UNVERIFIED.** Read the current Daraja documentation and test in the sandbox before implementing.
Filled during Phase 8d. Record dates and sources.

## Docs consulted
| Topic | URL | Date read |
|---|---|---|
| Authorization (OAuth) | | |
| M-Pesa Express / STK Push | | |
| STK Push query | | |
| C2B (register URLs, validation, confirmation) | | |
| Transaction status | | |
| Go-live process | | |

## Setup checklist
- [ ] Daraja developer account and app created
- [ ] Shortcode type decided (Paybill / Till) and owner confirmed (DECISIONS #8)
- [ ] Sandbox credentials stored in secret manager (never in repo)
- [ ] Public HTTPS callback URLs reachable from Safaricom
- [ ] Passkey / consumer key / consumer secret handling documented
- [ ] Safaricom go-live requirements understood and submitted
- [ ] Production credentials stored separately from sandbox

## Questions to answer (cite the source)
- Access-token endpoint, expiry, caching rules:
- STK Push request fields (shortcode, passkey/password timestamp, transaction type, amount rules, phone format, callback URL, account reference):
- STK Push callback payload: success vs failure fields (`ResultCode`, `CheckoutRequestID`, `MpesaReceiptNumber`, amount, phone, transaction date):
- STK query: request/response, rate limits, when it is safe to call:
- C2B: URL registration, validation vs confirmation flow, payload fields, account reference handling:
- Source IPs / security guidance for callbacks:
- Behaviour on user cancel, timeout, insufficient funds, wrong PIN; the resulting result codes:
- Maximum/minimum amounts, daily limits relevant to subscriptions:
- Reversal/refund API availability and process:
- Reconciliation: statements/reports/transaction-status options:

## Implementation rules (from CLAUDE.md §9B)
1. Callback is **untrusted**: verify with the query API before granting access; match amount, shortcode, phone and reference to the invoice.
2. Idempotent on `MpesaReceiptNumber`; replays never double-credit.
3. Handle late callbacks, expired invoices, wrong amounts, overpay/underpay, cancelled prompts, wrong phone.
4. Entitlement/amount/expiry computed server-side only.
5. Store raw callbacks (access-controlled); mask phone numbers in UI and logs.
6. Reconciliation job and admin queue for differences.

## Test cases (must be automated)
| Case | Expected |
|---|---|
| Successful payment, valid callback | Entitlement ACTIVE until period end; receipt issued |
| Duplicate callback (same receipt) | No change, no double credit |
| Forged callback (no matching checkout / failed query) | Rejected; alert |
| Wrong amount | Not activated; flagged for admin |
| User cancels / timeout | Invoice remains pending; user can retry |
| Late callback after invoice expiry | Held for manual review; not silently dropped |
| Renewal before expiry | Period extended without gap or overlap |
| Lapse during open trade | Trade completes/recovers first, then account pauses |

## Quirks and gotchas (add as discovered)
-
