# Binance — verified API findings

**Status: Phase 5a read-only findings VERIFIED from official docs (2026-10-22); public endpoints and the depth
stream also confirmed live (no key). Signed endpoints are confirmed from docs only: no API key has been used yet.**
Trading, borrow, repay and transfer findings are still UNVERIFIED (Phase 6 / 9). Docs base:
developers.binance.com (spot REST + websocket, margin_trading, wallet).

### Phase 5a verified findings (read 2026-10-22)
| Topic | Finding |
|---|---|
| Credentials | key + secret (HMAC-SHA256 of the query string, `signature` last; `X-MBX-APIKEY` header). No passphrase. |
| Signed requests | `timestamp` ms + `recvWindow` (default 5000, max 60000). Rejected if timestamp >1 s ahead of server or older than recvWindow, so keep the clock synced (adapter uses 5000). |
| Order book REST | `GET /api/v3/depth` limit 5/10/20/50/100 = weight 5, 500 = 25, 1000 = 50, 5000 = 250. Returns `lastUpdateId`, string `[price, qty]` pairs. No exchange timestamp: staleness uses local receive time. Weight 5 confirmed live. |
| Order book stream | `wss://stream.binance.com:9443/ws/<symbol>@depth@100ms`; fields `U`, `u`, `b`, `a`. Sync per Binance rules (drop `u <= lastUpdateId`, first event must straddle, then `U == prev u + 1`). Confirmed live: snapshot + 8 updates applied with no gap. Connection caps (streams per connection, connections per IP) NOT yet read: do before Phase 7. |
| Symbol rules | `GET /api/v3/exchangeInfo?symbol=` weight 20. `LOT_SIZE` (`minQty`,`maxQty`,`stepSize`), `PRICE_FILTER` (`tickSize`), `NOTIONAL` (`minNotional`; legacy `MIN_NOTIONAL`). **Lot and tick are checked as `x % step == 0`, from zero, not from `minQty`** (Filters page): this resolves the Phase 1 note, `allocation` rounding from zero is right. Status must be `TRADING`. Live BTCUSDT: step 0.00001, tick 0.01, min notional 5. |
| Fees | `GET /api/v3/account/commission?symbol=` weight 20 (signed): `standardCommission` + `taxCommission`, plus a `discount` (BNB). Adapter ignores the discount (conservative). Margin fee equality with spot is ASSUMED, verify Phase 6. |
| Spot balances | `GET /api/v3/account?omitZeroBalances=true` weight 20: `balances[].asset/free/locked`. |
| Cross margin account | `GET /sapi/v1/margin/account` weight 10: `userAssets[].asset/borrowed/free/interest/locked/netAsset`, `marginLevel`, `totalNetAssetOfBtc`, `totalLiabilityOfBtc`, `TotalCollateralValueInUSDT`. Values are in BTC, so USDT figures use `BTCUSDT` from `/api/v3/ticker/price` (weight assumed 4, conservative). `marginLevel` is 999 with no borrow (adapter maps to None, an assumption to confirm on a real account). |
| Loan and interest | From `userAssets[].borrowed` + `interest` (interest is in the borrowed asset). No separate loan endpoint needed for reads. |
| Max borrowable | `GET /sapi/v1/margin/maxBorrowable?asset=` weight 50: `amount` (system availability) and `borrowLimit` (account level). Adapter takes the smaller. Cross margin when `isolatedSymbol` is omitted. |
| Borrow rate | `GET /sapi/v1/margin/next-hourly-interest-rate?assets=&isIsolated=FALSE` weight 100: `nextHourlyInterestRate` per hour. History endpoint is daily. |
| Permissions | `GET /sapi/v1/account/apiRestrictions` weight 1: `enableReading`, `enableSpotAndMarginTrading`, `enableMargin`, `enableWithdrawals`, `ipRestrict`. There is NO separate borrow flag: `borrow` is reported as `enableMargin`. Real borrow ability must be proven by `maxBorrowable` (spec section 15 step 3). |
| Rate limits | Per IP, not per key. `X-MBX-USED-WEIGHT-1M` header (confirmed live). 429 = back off (`Retry-After`), 418 = IP ban 2 min to 3 days, 403 = WAF. The IP weight limit value itself (6000/min is the usual spot figure) was NOT read from the docs: configure from `exchangeInfo.rateLimits` in Phase 7. |
| Errors | `{code (negative int), msg}`. 5xx = outcome UNKNOWN for writes (never retry blind); `-1007` matching-engine timeout. For reads the adapter maps 5xx to `ExchangeDown`. |
| Margin pair flags | `GET /sapi/v1/margin/allPairs` (`isMarginTrade`, `isBuyAllowed`, `isSellAllowed`, `delistTime`) and `/allAssets` (`isBorrowable`, `userMinBorrow`): documented, not yet used by the adapter (add with the pair universe, decision #6). |

Phase 5a code: `src/oddtrue/exchanges/binance/`. The HTTP layer sends only GETs to a fixed allowlist
(`READ_ONLY_PATHS`); order, borrow, repay and transfer methods raise `ReadOnlyViolation` before any network code.
Owner smoke test: `scripts/exchange_readonly_smoke.py` (read-only key).

## Docs consulted
| Topic | URL | Date read |
|---|---|---|
| Spot REST / WebSocket | developers.binance.com/docs/binance-spot-api-docs/rest-api/{market-data-endpoints,general-endpoints,account-endpoints,general-api-information}, /filters, /web-socket-streams | 2026-10-22 |
| Margin (cross / isolated) | developers.binance.com/docs/margin_trading/{account,borrow-and-repay,market-data} | 2026-10-22 |
| Rate limits | general-api-information (limit value and websocket caps not read) | 2026-10-22 |
| Error codes | general-api-information (code format only; full list not read) | 2026-10-22 |
| Wallet / withdrawals | developers.binance.com/docs/wallet/account/api-key-permission (withdraw endpoints: Phase 9) | 2026-10-22 |
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
