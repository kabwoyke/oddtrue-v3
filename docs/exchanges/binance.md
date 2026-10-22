# Binance — verified API findings

**Status: UNVERIFIED.** Nothing here may be trusted until read from the current official documentation and
confirmed on testnet or with minimum-size live calls. Record the date and source for every finding.
Filled during Phase 5a (read-only) and Phase 6 (trading).

## Docs consulted
| Topic | URL | Date read |
|---|---|---|
| Spot REST / WebSocket | | |
| Margin (cross / isolated) | | |
| Rate limits | | |
| Error codes | | |
| Wallet / withdrawals | | |
| Testnet availability | | |

## Questions to answer (and cite the source)
### Credentials and permissions
- Credential fields required (key, secret, others?):
- How to read the key's permissions (trade, margin, withdraw flags)? Endpoint:
- IP whitelisting support and behaviour when IP not whitelisted:

### Market data
- Order book REST/WebSocket endpoints, depth options, update semantics (snapshot + diff, sequence ids):
- Symbol rules (min/max qty, step, tick, min notional, filters) endpoint and field names:
- Fee schedule endpoint, fee tiers, fee-discount assets, which asset fees are charged in:

### Spot trading (buy side)
- Place order endpoint; order types supported; time-in-force (IOC/FOK) behaviour:
- Client order id field and uniqueness/idempotency rules:
- Order status query by client id:

### Margin (borrow/sell side, if Binance is used on that side)
- Cross vs isolated: which to use and why (see DECISIONS #3):
- Borrowable assets/pairs, per-asset borrow limits, max borrowable endpoint:
- Borrow endpoint, repay endpoint, parameters, idempotency:
- Margin sell order: IOC/FOK support on margin; borrow-and-sell in one call or separate?
- Interest: rate source, accrual period, currency of interest, how accrued interest is reported:
- Can interest coin be bought and repaid within the same margin account without transfer?
- Margin level / health fields and liquidation thresholds:
- Loan query (principal + interest) endpoint:

### Transfers
- Deposit address, networks, minimum withdrawal, withdrawal fee, confirmations needed:
- Address whitelist requirement and API behaviour:
- Internal transfer between spot and margin wallets:

### Operational
- Scale (50–200 clients, shared egress IP): per-IP vs per-UID limits, websocket connection caps per IP, user-data stream behaviour and limits, ban/penalty rules:
- Rate limits (weights, per-IP, per-UID), ban behaviour, headers to monitor:
- Error codes mapped to our error taxonomy (`RateLimited`, `InsufficientFunds`, `InsufficientLiquidity`,
  `InvalidQuantity`, `UnknownOutcome`, `Rejected`, `AuthError`, `ExchangeDown`):
- Time sync requirements (timestamp / recvWindow):
- Maintenance windows and status endpoint:
- Testnet/demo coverage (which of the above can be tested without real funds):

## Quirks and gotchas (add as discovered)
-

## Adapter method mapping
| Adapter method | Endpoint | Notes | Verified (date, how) |
|---|---|---|---|
| get_orderbook | | | |
| get_symbol_rules | | | |
| get_fees | | | |
| get_balances | | | |
| get_margin_account | | | |
| get_borrowable | | | |
| get_borrow_rate | | | |
| place_order | | | |
| get_order | | | |
| cancel_order | | | |
| borrow | | | |
| margin_sell | | | |
| repay | | | |
| get_loan | | | |
| check_permissions | | | |
| withdraw / get_transfer_status | | | |
