# SECURITY

Oddtrue holds API keys that can trade (and borrow) on clients' real accounts. Treat a compromise as a financial
incident, not just a data incident.

## Assets to protect
1. Client exchange API credentials (key, secret, passphrase).
2. Client funds reachable through those keys (trade, margin, borrow; withdrawal if ever enabled).
3. Client personal data (email, phone, M-Pesa numbers, trade history).
4. Payment integrity (M-Pesa credentials, callbacks, entitlements).
5. Engine integrity (config, risk limits, kill switches).

## Credentials (exchange keys)
- Clients create keys themselves; Oddtrue never asks for exchange passwords or 2FA codes.
- Required permissions: read + spot trade + margin borrow/trade. **Withdrawal disabled** unless the client
  explicitly enables Automatic transfers, then only to pre-whitelisted own-exchange addresses with caps.
- Reject keys with unsafe permissions at onboarding; re-check periodically; alert on permission change.
- Recommend/require IP whitelisting on the exchange using the platform's fixed egress IP(s).
- Envelope encryption (per-client data key wrapped by a KMS/master key). Decrypt only inside the execution
  service, in memory, for the shortest time. Never in logs, error traces, metrics, dashboards or API responses.
- Support revoke/rotate; delete on account closure; document key-compromise procedure (RUNBOOKS.md).

## Application security
- MFA for all clients and mandatory for operators; session expiry; CSRF protection; rate limiting; lockout/backoff.
- Row-level authorization on every client-data query; automated cross-client access tests.
- Operator/admin app is separate (separate deployment, auth and permissions); least privilege; all actions audit-logged.
- Input validation everywhere; parameterized queries only; dependency scanning; pinned dependencies; SBOM.
- Secrets in a secret manager, never in the repo; pre-commit and CI secret scanning; separate dev/test/prod secrets.
- Logs: structured, redacting formatter, no PII beyond what is necessary (mask phones/emails), retention policy.
- Backups encrypted; restore tested; point-in-time recovery for the ledger database.

## Execution safety (money)
- Kill switches (global, per exchange, per client) with audit trail.
- Hard per-client and global exposure limits, max concurrent trades, max trade size; `live` mode gated by env var +
  config flag + allowlist.
- Idempotency keys and unknown-outcome handling to prevent duplicate orders/borrows/repays.
- Reconciliation freezes a client on any exchange/ledger mismatch.
- Time limits on every operation; recovery state machine; "loan == 0" verified from the exchange.

## Payments (M-Pesa)
- Callback endpoint is untrusted input: verify via transaction-status query, match amount/shortcode/phone/reference,
  restrict to expected IPs/paths where supported, idempotent on receipt number, never grant access on a client-side claim.
- Daraja credentials in the secret manager; rotate on exposure; sandbox and production fully separated.
- Reconciliation job against M-Pesa records; admin queue for mismatches; refunds audited and never altering trading ledger entries.

## Data protection and compliance (owner/legal to confirm)
- Kenya data protection obligations (registration, consent, subject rights, breach notification), KYC/AML position,
  and licensing for operating an automated trading service for third-party accounts: decision #5 in DECISIONS.md.
- Terms of service and risk disclosure with recorded consent (version + timestamp).
- No guaranteed-return claims; dashboard shows real results including losses (CLAUDE.md §9).

## Threats and mitigations (starter list; extend)
| Threat | Mitigation |
|---|---|
| Database or backup leak exposes keys | Envelope encryption; KMS; keys useless without withdrawal rights + IP whitelist |
| Insider/operator abuse | Separate admin app, least privilege, MFA, audit log, no plaintext key access for operators |
| Forged/replayed payment callback | Status-query verification, receipt-number idempotency, strict matching |
| Stolen client session | MFA, session expiry, re-auth for key changes and pause/resume/transfer-mode changes |
| Malicious dashboard input (XSS/CSRF/SQLi) | Output encoding, CSP, CSRF tokens, parameterized queries |
| Exchange outage during an open loan | Recovery state machine, timeouts, alerts, runbook, manual intervention path |
| Runaway bug trades repeatedly | Exposure limits, kill switches, per-client freeze, circuit breakers on loss/error rate |
| Cross-client data mix-up | Row-level security, client_id on all rows, isolation tests, no shared mutable state in the engine |

## Logging and redaction (Phase 4)
- Logs are JSON; redaction is applied in the formatter, so it covers messages, structured fields and formatted tracebacks.
  Secret-looking field names (api key, secret, passphrase, password, token, authorization, signature, X-MBX-APIKEY,
  OK-ACCESS-*, private key, otp, mfa, dek) lose their value; `name=value` text, bearer tokens and long key-shaped values are
  scrubbed; any decrypted credential held in memory must be passed to `register_secret` so it is masked wherever it appears.
- Alert text is redacted before it is stored or paged. Metrics labels never contain trade or client identifiers.
- Tests (`tests/test_observability.py`) prove a secret carried in an exchange error never reaches a log line.
