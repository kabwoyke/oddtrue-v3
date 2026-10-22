# Bitget — verified API findings

**Status: Phase 5b read-only findings read from the official docs (2026-10-22); public endpoints confirmed live (no
key), and the error envelope confirmed with a deliberately invalid key. Signed endpoints are confirmed from docs only:
no real API key has been used yet.** Trading, borrow, repay and transfer findings are UNVERIFIED (Phase 6 / 9).

How the docs were read: bitget.com/api-doc pages are client-rendered and returned only an empty shell, but the
server-rendered catalog pages `bitget.com/docs/catalog/<name>/<name>` (classic-margin-cross-account,
classic-spot-market, classic-spot-account, classic-margin-common) carry the full endpoint reference. The signing
rules were read from the legacy doc (bitgetlimited.github.io/apidoc/en/margin) and match the headers in the v2
Cross-Repay example; v2 has no separately readable signing page. Bitget marks the "Classic" API as maintenance mode in
favour of the Unified Trading Account (UTA) API: **decision #3/#6 follow-up for the owner** (see PROGRESS).

### Phase 5b verified findings (2026-10-22)
| Topic | Finding |
|---|---|
| Credentials | key + secret + **passphrase** (all three; the onboarding form must ask for it). |
| Signing | Headers `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-TIMESTAMP` (ms), `ACCESS-PASSPHRASE`, `Content-Type: application/json`, `locale`. `ACCESS-SIGN` = Base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + ("?" + query if any) + body)). Timestamp must be within 30 s of server time (legacy doc). Public endpoints need no headers. |
| Response envelope | `{"code":"00000","msg":"success","requestTime":..,"data":..}`. Errors come as HTTP 400 with `{"code":"40037","msg":"Apikey does not exist","data":null}` (observed live with a fake key) and `40034` for an unknown symbol (observed). The adapter also treats a non-`00000` code on HTTP 200 as an error. Auth codes 40006/40009/40012/40014/40018 are from Bitget's public code list and NOT yet seen; only 40037 is confirmed. Unknown codes map to `Rejected` (a read just fails). |
| Order book | `GET /api/v2/spot/market/orderbook?symbol=&type=step0&limit=` max 150, IP limit 20/s. Returns `asks`, `bids` (string `[price, qty]`) and `ts` = matching-engine ms (confirmed live), so staleness is measured against the exchange. |
| Order book stream | **Not built.** `stream_orderbook` polls the REST book once per second and skips unchanged `ts`. The websocket depth channel (snapshot/update, checksum) has not been read: do before Phase 7 (polling at 200+ clients is not an option for the shared IP). |
| Symbol rules | `GET /api/v2/spot/public/symbols?symbol=` IP 20/s. `pricePrecision` / `quantityPrecision` are decimal places, so tick = 10^-pricePrecision and step = 10^-quantityPrecision (BTCUSDT: 0.01 / 0.000001, confirmed live). `minTradeAmount` and `maxTradeAmount` are marked obsolete (0 and ~1e21): min notional is `minTradeUSDT` (1 USDT for BTCUSDT). `status` must be `online`. Not modelled: `maxLimitOrderValue` (USDT cap per limit order), `buyLimitPriceRatio`/`sellLimitPriceRatio` (price band around the market): the protected price must be checked against the band in Phase 6. |
| Fees | Only the symbol default `takerFeeRate`/`makerFeeRate` (0.002 for BTCUSDT) is readable, documented as "can be overridden by individual fee". The adapter uses the default, which is conservative for most accounts. |
| Spot balances | `GET /api/v2/spot/account/assets?assetType=hold_only` UID 10/s: `coin`, `available`, `frozen`, `locked`. Coin case varies ("usdt" in the example): the adapter upper-cases. |
| Cross margin balances / loans | `GET /api/v2/margin/crossed/account/assets` UID 10/s: `coin`, `totalAmount`, `available`, `frozen`, `borrow`, `interest`, `net` (= available + frozen - borrow - interest), `coupon`. Loan principal = `borrow`, interest = `interest` (in the coin; "interest-only payments with a minimum payment of interest"). |
| Margin level | `GET /api/v2/margin/crossed/account/risk-rate` UID 10/s: `riskRateRatio` = total assets / total liabilities. The doc example shows "0" with no debt; the adapter only calls it when liabilities exist. Valuation in USDT: coin balances x `lastPr` from `/api/v2/spot/market/tickers?symbol=XUSDT`; a missing price raises instead of guessing. |
| Max borrowable | `GET /api/v2/margin/crossed/account/max-borrowable-amount?coin=` UID 10/s: `maxBorrowableAmount` ("changes in real time"). A GET (it was a POST in v1), so the GET-only guarantee holds. |
| Borrowable flag and rate | `GET /api/v2/margin/crossed/interest-rate-and-limit?coin=` IP 10/s: `borrowable`, `transferable`, `leverage`, `dailyInterestRate` (non-VIP), `annualInterestRate`, `maxBorrowableAmount`, `vipList`. Sent signed because the exchange resolves the VIP tier from the caller. The adapter returns borrowable = 0 when `borrowable` is false, and uses the non-VIP (highest) daily rate / 24 as the hourly rate. **Open: interest accrual timing (hourly vs daily) and billing asset are not stated: confirm with a minimum real borrow in Phase 6.** |
| Permissions | `GET /api/v2/spot/account/info` **1 call/s per user**: `authorities` codes (read: `stor`, `smor`, ...; write: `stow` spot trade, `smow` margin trade, `wwow` withdraw, `wtow` wallet transfer ...) and `ips` (whitelist). No separate borrow flag: `borrow` = `smow`. The adapter enforces the 1 s spacing locally. |
| Margin pair flags | `GET /api/v2/margin/currencies` (IP 10/s): `isCrossBorrowable` (the plain `isBorrowable` is marked ignore), `userMinBorrow`, `status` 1 = tradable. Observed live: the flag differs per coin (BRETT: cross not borrowable). Not yet used by the adapter (pair universe, decision #6). |
| Rate limits | Per endpoint, per IP or per UID (values above); no usage headers observed. The adapter keeps a per-IP budget shared by all clients and a per-client UID budget, plus a per-path minimum interval. A 429 triggers a local ban for `Retry-After` if given. The overall IP ban behaviour is NOT yet documented here. |

Phase 5b code: `src/oddtrue/exchanges/bitget/`. Same read-only construction as Binance: GET-only, fixed path
allowlist (a test rejects trade/borrow/repay/withdraw/transfer paths), every money-moving method raises
`ReadOnlyViolation` first. Owner smoke test: `scripts/exchange_readonly_smoke.py bitget`.

## Docs consulted
| Topic | URL | Date read |
|---|---|---|
| Spot API (current version) | bitget.com/docs/catalog/classic-spot-market, classic-spot-account (server-rendered catalog pages) | 2026-10-22 |
| Margin API (cross / isolated) | bitget.com/docs/catalog/classic-margin-cross-account, classic-margin-common (isolated not read) | 2026-10-22 |
| Rate limits | per-endpoint limits on the pages above; global/ban policy not read | 2026-10-22 |
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
