# ODDTRUE — ARBITRAGE ENGINE SPECIFICATION

> Source of truth for the Oddtrue engine. Converted from `PART_I_1.docx` (the original file contained the full text twice; the duplicate copy was removed). Section numbering (1–62) is unchanged so that references such as "spec §41" in `CLAUDE.md` stay valid.

> **Terminology:** *Exchange A* = buy side (spot BUY). *Exchange B* = borrow-and-sell side (margin BORROW → SELL). Roles are assigned per opportunity; first exchanges: Binance and Bitget.

---

# PART I — FINAL ARBITRAGE ENGINE FLOW + MATHEMATICS

## 1. CLIENT ACCOUNT INITIALIZATION

For every client:

- Connect Exchange A API.
- Connect Exchange B API.
- Verify API permissions.
- Verify spot trading.
- Verify margin trading.
- Verify borrowing.
- Retrieve balances.
- Retrieve collateral.
- Retrieve available margin.
- Retrieve existing liabilities.
- Retrieve maximum borrowable amount.
- Retrieve account/margin health.
- Only eligible accounts enter the execution pool.

## 2. CLIENT CAPACITY — 95% + 5%

For every client:

- Usable Client Capacity = U

Main trading allocation:

- Trade Capacity = U × 95%

Reserved capacity:

- Reserve = U × 5%

Therefore:

- 95% + 5% = 100%

Example:

- U = \$500

Main execution:

- \$500 × 95% = \$475

Reserve:

- \$500 × 5% = \$25
- The 5% reserve is not automatically spent.

It remains available for:

- Interest acquisition during loan recovery.
- Small account requirements.
- Permitted recovery costs.
- Maintaining account headroom.

## 3. MARKET DATA COLLECTION

Continuously retrieve from A and B:

- Full order books.
- Bid prices.
- Ask prices.
- Quantities at every price level.
- Trading fees.
- Borrowing rates.
- Available-to-borrow quantity.
- Margin requirements.
- Available balances.
- Minimum order quantity.
- Quantity step.
- Minimum notional.
- Price tick size.
- Transfer/withdrawal requirements.
- Scan the configured universe of hundreds of USDT pairs.

## 4. OPPORTUNITY DETECTION

Primary strategy:

- Exchange A
- USDT → BUY Coin
- Exchange B
- BORROW same Coin → SELL Coin → USDT

Evaluate both directions:

- A cheaper → B more expensive
- and
- B cheaper → A more expensive
- Triangular/multi-route arbitrage is treated as a separate opportunity class, not forced into every trade.

## 5. FULL ORDER-BOOK CALCULATION

- Do not use only the first displayed price.
- Walk through the order book until the required quantity is reached.

For each level:

- Level Value = Price × Executable Quantity

Calculate VWAP:

- VWAP = Σ(Price × Executed Quantity) / Σ Executed Quantity

Calculate separately:

- Buy VWAP
- and
- Sell VWAP

Only liquidity that can execute within the configured maximum slippage boundary is considered executable liquidity.

## 6. 90% MARKET-LIQUIDITY RULE

After determining executable liquidity:

- Maximum Market Execution = Executable Liquidity × 90%

Example:

Executable liquidity:

- \$50,000

Maximum market execution:

- \$50,000 × 90% = \$45,000
- The engine must not intentionally consume more than the 90% limit.

## 7. CLIENT ALLOCATION

For each client:

- Maximum Client Allocation = Usable Capacity × 95%
- Then compare it against the opportunity.

Final client allocation:

- MIN(95% Client Capacity, 90% Market Capacity, Available Capital, Borrowable Quantity, Exchange Limits)
- Every order must also pass lot-size and minimum-notional validation.

## 8. CLIENT ROTATION

If total client demand exceeds the available opportunity:

Example:

Eligible client demand:

- \$100,000

Available 90% market capacity:

- \$45,000
- Only clients whose allocations fit within the \$45,000 capacity participate.

Remaining clients:

- Stay eligible.
- Enter/continue in the rotation queue.
- Receive future qualifying opportunities.
- A client who does not execute is not considered a failed client.

## 9. LOT-SIZE AND EXCHANGE LIMIT VALIDATION

Every order quantity must satisfy:

- Minimum quantity.
- Maximum quantity.
- Quantity step.
- Minimum notional.
- Price tick.
- Pair-specific restrictions.

Example:

Calculated:

- 0.123456 BTC

Allowed step:

- 0.00001 BTC

Valid order:

- 0.12345 BTC

The adjustment must remain within:

- 95% client limit
- and
- 90% market-liquidity limit.

## 10. PRE-CHECK B SELL CAPACITY

Before borrowing:

- Read B's current order book.
- Calculate executable sell quantity.
- Calculate sell VWAP.
- Confirm the intended quantity can be sold.
- Confirm it can be sold within the maximum permitted slippage.
- Calculate expected sell proceeds.

If insufficient executable liquidity exists:

- DO NOT BORROW.
- This is a critical failure-prevention mechanism.

## 11. PRE-CHECK BORROWING CAPACITY

Before borrowing:

Retrieve actual:

- Available-to-borrow quantity.
- Borrowing limit.
- Margin health.
- Current borrowing rate.
- Existing liabilities.

Calculate:

- Required Coin Quantity = Trade USDT Value ÷ Executable Coin Price
- Then adjust to exchange quantity rules.

If borrowing capacity is insufficient:

- DO NOT EXECUTE.

## 12. FINAL REVALIDATION

Immediately before execution, re-query:

- A order book.
- B order book.
- A available balance.
- B available balance.
- B available-to-borrow.
- Prices.
- Fees.
- Borrowing rate.
- Margin capacity.

Recalculate:

- VWAP
- Executable liquidity
- Client allocation
- Expected gross profit
- Expected fees
- Expected interest
- Expected net ROI

If the opportunity no longer meets the required threshold:

- CANCEL BEFORE EXECUTION.

## 13. PROFITABILITY MATHEMATICS

- Buy cost
- Buy Cost = Buy Quantity × Buy VWAP
- Sell proceeds
- Sell Proceeds = Sell Quantity × Sell VWAP
- Gross profit
- Gross Profit = Sell Proceeds − Buy Cost
- Trading fees
- Trading Fees = Σ(Executed Value × Applicable Fee Rate)
- Borrowing interest

For estimation:

- Interest ≈ Borrowed Amount × Borrow Rate × Borrowing Duration

For final accounting:

- Interest = Actual Accrued Interest reported by Exchange B
- Net profit
- Net Profit = Gross Profit − Trading Fees − Borrowing Interest − Slippage Costs − Other Applicable Costs
- Net ROI
- Net ROI = Net Profit ÷ Capital Used × 100
- Execute only if expected net ROI ≥ configured minimum.

## 14. CONCURRENT EXECUTION

Where APIs permit:

- Exchange A
- BUY Coin with USDT
- Exchange B
- BORROW Coin → immediately SELL Coin
- The engine should submit the operations as tightly concurrently as technically possible.

Objective:

- Borrow → immediately protected sell

Not:

- Borrow → wait → decide later.

## 15. PROTECTED SELL ORDER

Where supported and appropriate:

- Aggressive IOC limit order
- or
- FOK limit order
- The engine calculates the maximum acceptable execution price.
- The order must not accept a materially worse price than the profitability calculation allows.

## 16. SUCCESSFUL TRADE

If:

- A BUY = successful
- B BORROW = successful
- B SELL = successful

then:

- Record actual fills.
- Record actual prices.
- Record actual quantities.
- Calculate actual fees.
- Calculate actual borrowing interest.
- Calculate actual net profit.
- Repay BTC/coin loan + interest.
- Verify loan = 0.
- Reconcile balances.
- Evaluate rebalancing.
- Update client ledger.
- Return to opportunity scanning.

## 17. FAILED B SELL — CRITICAL RECOVERY

Scenario:

- A BUY = successful
- B BORROW = successful
- B SELL = failed

The engine must immediately:

- Stop/reject further attempts at the failed order.
- Check the actual coin balance on B.
- Check the actual outstanding loan.
- Check accrued interest.
- Check A's purchased coin.
- Start recovery.
- The borrowed coin on B must not simply remain unmanaged.

## 18. IMMEDIATE UNWIND OF A

- Because B's sell failed, the arbitrage is no longer complete.

Therefore:

- Exchange A: Coin → SELL → USDT
- The engine immediately sells the coin purchased on A using a protected executable order.

The objective is:

- Return Exchange A's trading capital to USDT.
- It must not leave the A-side coin position waiting for another opportunity.

## 19. B-SIDE BORROWED COIN

- The borrowed coin remains on B.

Example:

- B borrowed = 0.003 BTC
- B sell failed.

Therefore:

- BTC remaining on B = 0.003 BTC

The loan remains:

- 0.003 BTC + accrued interest
- The engine calculates the exact liability.

## 20. INTEREST CALCULATION

Let:

- P = BTC principal borrowed
- I = accrued BTC interest

Then:

- Total Liability = P + I

Example:

Principal:

- 0.003000 BTC

Interest:

- 0.000020 BTC

Therefore:

- Total Liability = 0.003020 BTC

## 21. USE B'S RESERVED 5% FOR INTEREST

Because B already holds the borrowed principal:

- 0.003000 BTC
- only the interest amount needs to be acquired.

Required:

- 0.000020 BTC
- The engine checks the client's reserved 5% capacity.

If permitted by B's margin rules:

- Interest BTC × Current Executable BTC/USDT Price = USDT Required

Example:

- 0.000020 BTC × \$100,000 = \$2

The engine uses approximately the required \$2 from the reserved capacity to purchase:

- 0.000020 BTC
- plus the necessary adjustment for actual execution fees/quantity rules.

## 22. FULL LOAN REPAYMENT

After acquiring the interest BTC:

B has:

- Principal BTC + Interest BTC

Example:

- 0.003000 + 0.000020 = 0.003020 BTC

Repay:

- 0.003020 BTC

Then verify through API:

- Outstanding BTC Liability = 0

If the API does not report zero:

- Do not mark recovery complete.

## 23. IMPORTANT — NO A → B TRANSFER FOR INTEREST

- The interest should be handled inside B using the reserved 5%, where exchange rules permit it.

Therefore:

- B borrowed principal → already on B
- Interest → acquired on B
- Repay → full principal + interest
- This avoids making a separate A → B transfer solely for the tiny interest amount.

## 24. WHAT HAPPENS TO A AFTER THE FAILED TRADE?

A's purchased BTC is immediately sold:

- BTC → USDT
- Therefore A returns to a USDT position.

Example:

A:

- \$500 USDT → buys BTC → B sell fails

Recovery:

- BTC → sell → approximately USDT value

The actual result reflects:

- Buy execution price.
- Sell execution price.
- Trading fees.
- Slippage.
- This actual recovery cost is recorded against the trade/client.

## 25. FAILED-TRADE LOSS MATHEMATICS

If A buys BTC for:

- \$500

and immediately unwinds it for:

- \$498.50

then:

- A unwind loss = \$500 − \$498.50 = \$1.50

Add:

- B borrowing interest.
- B interest-purchase fee.
- Any other applicable execution costs.

Therefore:

- Total Recovery Cost = A Unwind Loss + B Interest + Recovery Fees + Other Costs
- The system records the actual result.

## 26. IF THE ORIGINAL OPPORTUNITY DISAPPEARS

Once B's original sell has failed:

- Terminate the original arbitrage.

Do not keep trying to force the original trade unless a fresh profitability calculation independently shows that continuation is still valid.

The default recovery is:

- A BTC → USDT
- B borrowed BTC → repay principal + interest

## 27. ACCOUNT RECONCILIATION AFTER RECOVERY

Verify through APIs:

- Exchange A
- USDT balance.
- BTC balance.
- Available trading balance.
- Exchange B
- USDT balance.
- BTC balance.
- Margin balance.
- Borrowed BTC.
- Accrued interest.
- Outstanding liability.

Final requirement:

- Outstanding Loan = 0

## 28. ACCOUNT REBALANCING

- After recovery or a successful trade, evaluate the inventory distribution between A and B.

Monitor:

- BTC.
- USDT.
- Margin collateral.
- Available borrowing.
- Available trading capital.
- Expected future trading demand.
- The objective is not necessarily 50/50 balances.

The objective is:

- Maintain enough operational inventory on both exchanges to execute future opportunities.

## 29. REBALANCING TRANSFER

If Exchange A has insufficient operational inventory and B has excess inventory:

- B → A
- Transfer only the required amount.

Calculate:

- Required Rebalance = Target Inventory − Current Inventory

Before transferring, consider:

- Transfer fee.
- Network fee.
- Minimum transfer.
- Expected future arbitrage.
- Whether another future trade can naturally rebalance the accounts.
- Do not perform unnecessary transfers.

## 30. TWO-TRANSFER CONCEPT

- A successful/recovery cycle may involve two different transfers when required.
- Transfer 1 — Settlement/recovery
- Used only when necessary to resolve a remaining asset/liability imbalance.
- Transfer 2 — Rebalancing
- Used to restore operational inventory for future trading.
- They are not the same operation.
- The engine must calculate each independently.

## 31. IMPORTANT: NO AUTOMATIC EQUALIZATION

The engine must not blindly do:

- B → A = same amount that A → B transferred

Instead:

- Calculate current inventory → calculate target operational inventory → transfer only required amount.
- This minimizes unnecessary network/withdrawal costs.

## 32. CLIENT LEDGER

For every client and every trade record:

- Starting USDT.
- 95% allocated amount.
- 5% reserved amount.
- Actual executed amount.
- BTC purchased.
- BTC borrowed.
- BTC sold.
- Buy price/VWAP.
- Sell price/VWAP.
- Trading fees.
- Borrowing interest.
- Recovery costs.
- Transfer costs.
- Rebalancing costs.
- Gross profit/loss.
- Net profit/loss.
- Ending balance.

## 33. ACTUAL PROFIT MATHEMATICS

Expected:

- Expected Net Profit = Expected Gross Profit − Expected Costs

After execution:

Actual Net Profit = Actual Sell Proceeds − Actual Buy Cost − Actual Trading Fees − Actual Interest − Actual Slippage − Actual Recovery/Transfer Costs

- Client balance is updated using actual realized figures, not estimates.

## 34. MULTI-ROUTE / TRIANGULAR OPPORTUNITY

- This is a separate opportunity class.

Example:

- USDT → BTC → SOL → USDT

For each leg:

- Output = Input × Executable Conversion Rate × (1 − Fee Rate)

After all legs:

- Final USDT = Output of final conversion

Then:

- Net Profit = Final USDT − Starting USDT − Other Costs
- Compare the resulting net ROI against the direct cross-exchange opportunity.

Do not use the triangular route simply because its gross percentage is higher; compare actual executable net profit, liquidity, number of legs, fees and execution risk.

## 35. API FAILURE HANDLING

If an API call fails:

- Do not assume the transaction failed.
- Query the exchange again.
- Check actual order status.
- Check actual balance.
- Check actual loan.
- Reconcile state before issuing another transaction.
- This prevents duplicate orders or duplicate borrowing.

## 36. PARTIAL-FILL HANDLING

If an order partially fills:

Calculate:

- Actual Filled Quantity

Then immediately recalculate:

- Remaining quantity.
- Remaining liquidity.
- Remaining profitability.
- Outstanding loan.
- Required hedge/recovery.
- Do not assume the original quantity was filled.

## 37. MAXIMUM EXECUTION TIME

- Every operation has a configured maximum execution/recovery time.

If exceeded:

- Stop normal execution → reconcile actual state → activate recovery.
- The engine must never remain indefinitely waiting for an order response.

## 38. DUPLICATE-ORDER PROTECTION

Before submitting another order after an uncertain API response:

- Query existing order status first.

The engine must prevent:

- Duplicate buys.
- Duplicate sells.
- Duplicate borrowing.
- Duplicate repayments.

## 39. CONTINUOUS BALANCE CONTROL

After every trade:

- Exchange State → API Reconciliation → Internal Ledger → Inventory Calculation

The engine must know:

- What the exchange says exists
- versus
- What Oddtrue expects to exist.
- Any discrepancy triggers reconciliation.

## 40. FINAL CONTINUOUS ENGINE LOOP

```text
CLIENT ACCOUNTS
↓
95% CLIENT CAPACITY + 5% RESERVED CAPACITY
↓
COLLECT MARKET DATA
↓
SCAN HUNDREDS OF PAIRS
↓
READ FULL ORDER BOOKS
↓
CALCULATE VWAP
↓
CALCULATE EXECUTABLE LIQUIDITY
↓
APPLY 90% MARKET-LIQUIDITY LIMIT
↓
CALCULATE CLIENT ALLOCATIONS
↓
APPLY CLIENT ROTATION
↓
CHECK B SELL LIQUIDITY
↓
CHECK B BORROWING CAPACITY
↓
FINAL API REVALIDATION
↓
CALCULATE NET ROI
↓
EXECUTE A BUY + B BORROW/SELL CONCURRENTLY
↓
SUCCESS?
── SUCCESS PATH ──
YES
↓
RECORD ACTUAL FILLS
↓
REPAY LOAN + INTEREST
↓
VERIFY LOAN = 0
↓
RECONCILE
↓
REBALANCE IF REQUIRED
↓
UPDATE CLIENT
↓
NEXT OPPORTUNITY

── FAILURE PATH ──
B SELL FAILED?
YES
↓
A BTC → IMMEDIATE SELL → USDT
↓
B BORROWED BTC REMAINS ON B
↓
QUERY ACTUAL PRINCIPAL + ACCRUED INTEREST
↓
CALCULATE INTEREST BTC
↓
USE B'S RESERVED 5% TO BUY ONLY INTEREST BTC
↓
ADD INTEREST BTC TO BORROWED BTC
↓
REPAY FULL PRINCIPAL + INTEREST
↓
VERIFY LOAN = 0
↓
RECONCILE A + B
↓
CALCULATE INVENTORY IMBALANCE
↓
REBALANCE ONLY IF REQUIRED
↓
UPDATE CLIENT LEDGER
↓
RETURN TO OPPORTUNITY ROTATION
```

## 41. MASTER MATHEMATICAL CONSTRAINT

Every trade must satisfy all of these simultaneously:

- Client Allocation ≤ 95% of usable client capacity
- Market Execution ≤ 90% of executable liquidity
- Borrow Quantity ≤ Actual Available-to-Borrow
- Order Quantity ≤ Exchange Maximum
- Order Quantity ≥ Exchange Minimum
- Order Quantity conforms to Lot Size
- Order Notional ≥ Minimum Notional
- Expected Net ROI ≥ Minimum Required ROI

And during recovery:

- BTC Interest Requirement ≤ 5% Reserved Capacity
- where permitted.

Finally:

- Outstanding Loan After Recovery = 0

## 42. THE CORE ODDTRUE PRINCIPLE

The system is not simply an arbitrage calculator. It is an:

- execution + recovery + liquidity + inventory management engine.

The four major controls are:

- 95% → controls each client's main execution capacity
- 5% → reserved capacity for protection/recovery, including small interest acquisition
- 90% → controls total market liquidity consumption
- Rotation → distributes opportunities among clients when liquidity is insufficient for everyone

And the failed-sell recovery is specifically:

```text
A BUY BTC → B BORROW BTC → B SELL FAILS
↓
A SELL BTC → USDT immediately
↓
B retains borrowed BTC
↓
B uses reserved 5% to obtain interest BTC
↓
B repays principal + interest
↓
Loan = 0
↓
Reconcile
↓
Rebalance inventories
↓
Continue trading
```

---

# PART II — USER SETUP & ASSET TRANSFER CONTROL FLOW

## 43. USER CREATES ODDTRUE ACCOUNT

User:

- Registers/logs in to Oddtrue.
- Completes the required account verification.

- Opens Exchange Connections.

## 44. USER CONNECTS EXCHANGE A

User manually enters:

- Exchange A API Key
- Exchange A API Secret
- Any additional required API credentials

Oddtrue:

- Encrypts/stores credentials securely.
- Tests API connectivity.
- Checks required permissions.

- Reads balances and account status.

## 45. USER CONNECTS EXCHANGE B

User manually enters:

- Exchange B API Key
- Exchange B API Secret
- Any additional required API credentials.

Oddtrue:

- Tests API connectivity.
- Checks trading permissions.
- Checks margin permissions.
- Checks borrowing permissions.
- Reads balances.
- Reads available collateral.

- Reads borrowing capacity.

## 46. ACCOUNT READINESS CHECK

Oddtrue verifies:

- Exchange A
- API working
- Trading available
- Required asset/USDT balance available
- Exchange B
- API working
- Margin available
- Borrowing available
- Required asset borrowable
- Margin health sufficient

If everything passes:

- Account Status = READY

Otherwise:

- Account Status = ACTION REQUIRED
- The user is shown exactly what needs to be corrected.

## 47. USER TRANSFER PREFERENCE

Inside Oddtrue create:

- Cross-Exchange Asset Transfer Mode
- The user chooses one of two modes.
- OPTION A — AUTOMATIC TRANSFER
- Enable Automatic Cross-Exchange Asset Transfer

If enabled, Oddtrue's engine can initiate permitted cross-exchange transfers according to the configured recovery/rebalancing rules.

The system handles:

- Transfer amount calculation.
- Destination exchange.
- Asset.
- Network.
- Transfer monitoring.
- Deposit confirmation.
- Balance reconciliation.
- The user does not need to manually initiate each transfer.
- OPTION B — MANUAL TRANSFER
- Manual Cross-Exchange Asset Transfer

If selected:

Oddtrue calculates and displays:

- Asset to transfer.
- Amount.
- Sending exchange.
- Receiving exchange.
- Network.
- Estimated fee.
- Reason for transfer.
- Required deadline/urgency.
- The user manually performs the transfer.
- Oddtrue then waits for confirmation and reconciles the balances.

## 48. IMPORTANT DIFFERENCE BETWEEN THE TWO MODES

- Automatic mode
- Trade → transfer instruction → transfer → receive → reconcile → continue
- This is faster and better suited to high-frequency arbitrage because the system does not wait for the user.
- Manual mode
- Trade → transfer required → notify user → user transfers → confirmation → reconcile → continue

This introduces:

- Human delay.
- Transfer delay.
- Operational hassle.
- Potential missed opportunities.
- Greater risk that the required inventory is not available for the next trade.

Therefore, automatic transfer should be the recommended mode for users who want continuous automated arbitrage, while manual transfer remains available for users who want direct control.

## 49. USER CONTROL BOUNDARY

The user controls:

- API connection.
- Exchange selection.
- Transfer mode.
- Whether their account participates.
- Deposits.
- Withdrawals when permitted.
- Viewing account/trade information.
- Pausing participation.

The user does not manually control:

- Arbitrage opportunity selection.
- Order-book execution.
- Borrow quantity.
- Buy/sell quantities.
- Loan repayment calculations.
- Recovery calculations.
- Automatic rebalancing when automatic transfer is enabled.

## 50. AFTER EVERY SUCCESSFUL TRADE

- Oddtrue calculates the resulting exchange inventory.

Example:

- Exchange A
- BTC ↓
- USDT ↑
- Exchange B
- BTC/USDT distribution changes.

The engine calculates:

- Current inventory
- versus
- Required operational inventory

Then:

- Rebalance Requirement = Target Inventory − Current Inventory

If no rebalance is needed:

- No transfer

If rebalance is required:

- Automatic mode
- Oddtrue initiates the permitted transfer.
- Manual mode
- Oddtrue displays a transfer instruction for the user.

## 51. MANUAL TRANSFER INSTRUCTION

If manual mode is enabled, Oddtrue should clearly show something like:

- Rebalancing Required
- Send: 0.25 BTC
- From: Exchange B
- To: Exchange A
- Network: [approved network]
- Estimated fee: X
- Purpose: Restore Exchange A trading inventory
- Status: Awaiting user transfer
- The user then completes the transfer externally.
- Oddtrue monitors the receiving exchange and automatically detects the deposit when possible.

## 52. TRANSFER CONFIRMATION

After a manual transfer:

Oddtrue checks:

- Transaction status.
- Amount sent.
- Amount received.
- Asset.
- Destination.
- Network.
- Updated exchange balance.

Then:

```text
Transfer Confirmed
↓
Reconcile
↓
Inventory Restored
↓
Account Available
```

## 53. AUTOMATIC TRANSFER SAFETY

- Automatic transfers should not mean unlimited withdrawal authority.

The engine must calculate:

- Required Transfer Amount
- and transfer only what is necessary according to the strategy.

Before initiating an automatic transfer, verify:

- Asset.
- Destination.
- Network.
- Minimum withdrawal.
- Withdrawal fee.
- Expected received amount.
- Sending balance.
- Receiving exchange deposit availability.
- Whether the transfer would impair future trading.

## 54. USER CAN CHANGE TRANSFER MODE

The user may change:

- Automatic ↔ Manual
- but the change should take effect only when the account is in a safe state.

For example:

- Active arbitrage
- → change locked
- Recovery
- → change locked
- Pending transfer
- → change locked
- Reconciliation
- → change locked
- Account idle
- → user may change mode
- This prevents a user from changing the transfer mechanism in the middle of an operation.

## 55. USER PAUSE

If the user selects:

- Pause Trading

Oddtrue should:

- Stop accepting new opportunities for that user

but allow any active operation to:

- Complete, or
- Enter recovery.

Then:

- Loan = 0
- Active orders = resolved
- Pending recovery = none

```text
Pending transfer = resolved or safely handled
↓
Account = PAUSED
```

## 56. USER RESUMES

User selects:

- Resume Trading

Oddtrue performs a fresh:

- Balance check.
- API check.
- Margin check.
- Borrowing check.
- Inventory check.
- Transfer-status check.

Then:

- Account = READY
- and the client returns to the allocation/rotation pool.

## 57. WHAT HAPPENS IF USER CHOOSES MANUAL TRANSFERS?

The developer should understand this clearly:

- Manual transfer mode does NOT disable arbitrage calculations.
- It only changes who initiates the cross-exchange inventory movement.

Automatic:

- Oddtrue initiates transfer

Manual:

- User initiates transfer using Oddtrue's instruction
- Everything else remains controlled by the engine.

## 58. USER DASHBOARD

The dashboard should show:

- Exchange A
- USDT balance
- Crypto balance
- Available trading balance
- Transfer status
- Exchange B
- USDT balance
- Crypto balance
- Margin balance
- Borrowed asset
- Accrued interest
- Available borrowing
- Transfer status
- Oddtrue
- 95% active allocation
- 5% reserved capacity
- Current allocated amount
- Current rotation status
- Active opportunity
- Current P/L
- Transfer mode
- Realized ROI
- ROI earnings history
- Total realized profit
- Realized ROI rule

Realized ROI is calculated only after a trade has been successfully completed and all applicable execution costs have been deducted.

The calculation is:

- Realized ROI = Net Realized Profit ÷ Capital Used × 100

Where:

Net Realized Profit = Gross Profit − applicable trading fees − borrowing interest − execution/slippage costs − applicable recovery costs − applicable transfer/rebalancing costs

- ROI display rule
- Positive realized ROI = Green
- Only positive realized ROI from successfully completed trades is published in the ROI earnings list.
- Losses are NOT published in the ROI earnings list.
- Estimated opportunity ROI must not be presented as earned ROI.
- Failed, cancelled, or recovery operations must not be presented as successful ROI earnings.

Underlying transactions and losses must still remain recorded internally for accurate accounting, reconciliation and client balance calculations.

- Example

Estimated opportunity ROI:

- +4.00%

After successful execution and all costs:

- Realized ROI: +3.50% 🟢

The published ROI list shows:

- +3.50% 🟢
- not the original +4.00% estimate.

## 59. TRANSFER STATUS

Use clear statuses:

- NO TRANSFER REQUIRED
- TRANSFER REQUIRED
- AWAITING USER TRANSFER
- TRANSFER INITIATED
- TRANSFER CONFIRMING
- TRANSFER RECEIVED
- RECONCILING
- REBALANCED
- READY
- This prevents the user from wondering what the system is waiting for.

## 60. USER'S SIMPLE RESPONSIBILITY

The user essentially does five things:

- Connect Exchange A
- Connect Exchange B
- Maintain sufficient capital/collateral
- Choose Automatic or Manual Cross-Exchange Transfer
- Monitor their account
- Everything else is handled by the Oddtrue engine according to the execution rules.

## 61. RECOMMENDED DEFAULT

I would make:

- Automatic Cross-Exchange Asset Transfer = ON by default

with a clear explanation:

Automatic transfer is recommended for continuous arbitrage because manual transfers can introduce human and blockchain/exchange processing delays and may prevent the account from being ready for the next opportunity.

But the user should be able to turn it off and select:

- Manual Transfer
- if they want direct control.

## 62. FINAL USER FLOW

```text
LOGIN
↓
CONNECT EXCHANGE A API
↓
CONNECT EXCHANGE B API
↓
API + BALANCE + MARGIN CHECK
↓
ACCOUNT READY
↓
USER CHOOSES TRANSFER MODE
AUTOMATIC OR MANUAL
↓
USER ENTERS/MAINTAINS CAPITAL
↓
ODDTRUE SCANS OPPORTUNITIES
↓
95% CLIENT ALLOCATION
↓
90% MARKET LIQUIDITY LIMIT
↓
CLIENT ROTATION
↓
CONCURRENT ARBITRAGE EXECUTION
↓
SUCCESS
RECONCILE
↓
CALCULATE INVENTORY
↓
REBALANCE REQUIRED?
NO → CONTINUE TRADING
YES → AUTOMATIC TRANSFER OR MANUAL TRANSFER
↓
TRANSFER CONFIRMED
↓
RECONCILE
↓
CONTINUE TRADING

── B SELL FAILURE ──
A BUY BTC
↓
B BORROW BTC
↓
B SELL FAILS
↓
A BTC → SELL → USDT
↓
B BORROWED BTC REMAINS ON B
↓
CALCULATE PRINCIPAL + INTEREST
↓
USE B 5% RESERVE TO ACQUIRE INTEREST BTC
↓
REPAY FULL B LIABILITY
↓
VERIFY LOAN = 0
↓
RECONCILE
↓
CALCULATE INVENTORY
↓
REBALANCE IF REQUIRED
↓
AUTOMATIC TRANSFER OR USER MANUAL TRANSFER
↓
ACCOUNT READY
↓
NEXT OPPORTUNITY
```

## MASTER END-TO-END PRINCIPLE

The developer should treat all 62 sections above as one connected Oddtrue specification, not as separate systems.

The operating hierarchy is:

```text
CLIENT ELIGIBILITY
↓
95% CLIENT EXECUTION CAPACITY + 5% RESERVED CAPACITY
↓
MARKET DATA
↓
FULL ORDER-BOOK/VWAP ANALYSIS
↓
90% EXECUTABLE MARKET-LIQUIDITY LIMIT
↓
LOT-SIZE + EXCHANGE LIMIT CHECK
↓
CLIENT ROTATION
↓
B SELL-LIQUIDITY PRE-CHECK
↓
B BORROWING-CAPACITY PRE-CHECK
↓
FINAL REVALIDATION
↓
NET ROI CALCULATION
↓
CONCURRENT A BUY + B BORROW/SELL
↓
SUCCESS → SETTLE → RECONCILE → REBALANCE → CONTINUE
OR
B SELL FAILURE → A BTC SELL TO USDT → B RETAINS BORROWED BTC → ACQUIRE ONLY INTEREST BTC FROM B'S 5% RESERVE → REPAY PRINCIPAL + INTEREST → VERIFY LOAN = 0 → RECONCILE → REBALANCE → CONTINUE
↓
USER DASHBOARD / REALIZED ROI
↓
NEXT OPPORTUNITY
```

The fundamental controls remain separate and must not be confused:

- 95% = individual client's active execution allocation
- 5% = individual client's reserved capacity
- 90% = maximum portion of executable market liquidity Oddtrue intentionally consumes
- CLIENT ROTATION = allocation mechanism when eligible client demand exceeds available opportunity liquidity
- REALIZED ROI = calculated only after successful completion and actual costs
- ROI PUBLISHED = positive realized ROI only
- INTERNAL LEDGER = retains complete actual transaction records, including losses and recovery costs
- AUTOMATIC TRANSFER = system-controlled permitted inventory movement
- MANUAL TRANSFER = user-controlled inventory movement following Oddtrue's calculated instruction
- REBALANCING = restore operational inventory, not blindly equalize exchange balances
- RECOVERY = resolve the actual post-execution state and ensure outstanding liabilities are closed
- FINAL RECOVERY REQUIREMENT = Outstanding Loan = 0
