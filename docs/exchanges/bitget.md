# Bitget — verified API findings

**Status: UNVERIFIED.** Nothing here may be trusted until read from the current official documentation and
confirmed on demo/testnet (if offered) or with minimum-size live calls. Record the date and source for every finding.
Filled during Phase 5b (read-only) and Phase 6 (trading).

## Docs consulted
| Topic | URL | Date read |
|---|---|---|
| Spot API (current version) | | |
| Margin API (cross / isolated) | | |
| Rate limits | | |
| Error codes | | |
| Wallet / withdrawals | | |
| Demo / testnet availability | | |

## Questions to answer (and cite the source)
### Credentials and permissions
- Credential fields required (key, secret, **passphrase**, others?):
- How to read the key's permissions (trade, margin, withdraw flags)?
- IP whitelisting support and behaviour:

### Market data
- Order book endpoints/WebSocket, depth, update semantics:
- Symbol rules (min/max qty, precision, min notional) and field names:
- Fee schedule, fee tiers, which asset fees are charged in:

### Spot trading
- Place order; order types; time-in-force (IOC/FOK):
- Client order id and idempotency:
- Order status by client id:

### Margin (the borrow/sell side in the user journey)
- **Does the account actually support the required borrow operation?** (readiness check in journey step 3/4)
- Cross vs isolated margin (see DECISIONS #3); which pairs/assets are borrowable; limits; max borrowable:
- Borrow and repay endpoints, parameters, idempotency, one-call borrow-and-sell or separate:
- Margin sell order types; IOC/FOK support on margin:
- Interest: rate, accrual period, interest currency, how accrued interest is reported:
- Buying the interest coin within the margin account and repaying without a transfer:
- Margin level / risk ratio fields and liquidation thresholds:
- Loan / liability query (principal + interest):

### Transfers
- Deposit address, networks, minimum withdrawal, fees, confirmations:
- Address whitelist behaviour:
- Spot ↔ margin internal transfer:

### Operational
- Scale (50–200 clients, shared egress IP): per-IP vs per-UID limits, websocket connection caps per IP, user-data stream behaviour and limits, ban/penalty rules:
- Rate limits and ban behaviour:
- Error codes mapped to our error taxonomy:
- Timestamp/signature requirements:
- Maintenance windows / status:
- Demo/testnet coverage:

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
