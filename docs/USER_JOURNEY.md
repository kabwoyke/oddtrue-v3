# Oddtrue — Complete User Journey Example (Binance + Bitget)

> Worked scenario supplied by the owner. Used as the end-to-end acceptance reference. In this example Binance = Exchange A (buy side) and Bitget = Exchange B (borrow-and-sell side); in the engine these roles are assigned per opportunity (see `CLAUDE.md` §1). Dollar figures are illustrative.

The following example explains how a user interacts with Oddtrue from registration to automated arbitrage execution and account management.

## 1. User registers and logs in

Example: John creates an Oddtrue account, logs in, completes the required verification, and opens the Exchange Connections page.

Oddtrue system: Creates John's account and prepares his private trading dashboard.

## 2. John connects Binance (Exchange A)

John selects Binance and enters:

- Binance API Key
- Binance API Secret
- Any additional credentials required by the exchange

Oddtrue system:

- Securely encrypts and stores the credentials.
- Tests the API connection.
- Checks the API permissions.
- Retrieves John's balances and trading availability.

John must create the API key through Binance himself. He does not share his exchange password or two-factor authentication codes with Oddtrue.

## 3. John connects Bitget (Exchange B)

John selects Bitget and enters:

- Bitget API Key
- Bitget API Secret
- Any additional credentials required by Bitget, such as an API passphrase.

Oddtrue system:

- Securely stores the credentials.
- Tests connectivity.
- Checks spot trading, margin trading and borrowing permissions.
- Retrieves balances, collateral, borrowing capacity, existing liabilities and margin health.

The system must confirm which permissions and borrowing functions are actually available on John's account before marking it ready.

## 4. Oddtrue checks account readiness

Suppose John has \$500 of usable capital across his connected accounts.

The system checks:

- API connections are working.
- Required trading balances are available.
- Bitget supports the required borrowing operation.
- Margin health and borrowing capacity are sufficient.
- Exchange restrictions and minimum order sizes are satisfied.

If the checks pass, John's account becomes READY. Otherwise, Oddtrue displays ACTION REQUIRED and explains the problem.

## 5. John configures his trading preferences

John can:

- Enable or disable his participation in arbitrage.
- Select automatic or manual cross-exchange asset transfers.
- View his account balances and trading history.
- Pause or resume trading.
- Monitor transfer and recovery statuses.

John does not select individual arbitrage opportunities or manually calculate borrowing quantities. Those tasks belong to the engine.

## 6. Oddtrue applies the capital and liquidity rules

For John's \$500 usable capacity:

- Main allocation: \$500 × 95% = \$475
- Reserved capacity: \$500 × 5% = \$25

The \$25 reserve is not automatically spent. Its use is restricted to permitted purposes, including qualifying recovery requirements.

The engine also applies the 90% executable market-liquidity limit and client rotation. John's actual allocation is the lowest amount permitted by his capital, available market liquidity, borrowing capacity and exchange restrictions.

## 7. Oddtrue scans Binance and Bitget

The engine scans eligible USDT trading pairs, reads the order books and calculates:

- Buy VWAP on the buying exchange.
- Sell VWAP on the selling exchange.
- Trading fees and estimated borrowing interest.
- Slippage and other applicable costs.
- Expected net profit and net ROI.

It rejects opportunities that fail the configured profitability threshold or any liquidity, borrowing, lot-size or safety restriction.

## 8. An opportunity qualifies

Example:

Binance has the lower executable buying cost, while Bitget offers the higher executable selling price.

The intended sequence is:

Binance: Use USDT to buy the coin.

Bitget: Borrow the same coin and immediately submit a protected sell order.

The engine revalidates the order books, balances, borrowing capacity and expected net ROI immediately before execution. Where supported, the operations are submitted as tightly concurrently as technically possible.

The system records actual fills and costs rather than treating an estimated ROI as earned profit.

## 9. If both legs succeed

Oddtrue:

- Records the actual buy and sell fills.
- Calculates actual trading fees, borrowing interest and other applicable costs.
- Repays the borrowed coin and interest.
- Verifies that the liability is zero.
- Reconciles both exchanges.
- Calculates whether rebalancing is required.
- Updates John's account ledger and dashboard.

## 10. If the Bitget sell fails after borrowing

The system must not assume that the original trade succeeded.

It checks the actual order status, balances and outstanding loan.

Binance: Sells the BTC purchased for the failed opportunity back into USDT using a protected executable order, subject to exchange availability and recovery controls.

Bitget: Checks the borrowed BTC and actual accrued interest. Where permitted, the system uses the reserved capacity to acquire the additional BTC needed for interest, then repays the full principal plus interest.

The system verifies that the loan is fully closed before marking recovery complete. If recovery cannot be completed, the account enters a controlled recovery state instead of accepting another trade.

The actual unwind loss and recovery costs remain in internal accounting.

## 11. Oddtrue rebalances the accounts

After a successful trade or recovery, the engine checks the inventory on both exchanges.

If Binance needs additional operational inventory, Oddtrue calculates whether a transfer from Bitget is required. It transfers only the necessary amount, taking account of network fees, minimum transfer limits, expected future opportunities and available balances.

John chooses either:

Automatic transfer: Oddtrue initiates permitted transfers and monitors confirmation.

Manual transfer: Oddtrue displays the sending exchange, receiving exchange, asset, amount, approved network, estimated fee and reason. John initiates the transfer himself.

The system reconciles the receiving balance before treating the inventory as restored.

## 12. John's dashboard and ROI history

John sees:

- Binance balances and transfer status.
- Bitget balances, margin, borrowed assets, interest and borrowing availability.
- His 95% main allocation and 5% reserve.
- Current allocation and rotation status.
- Active opportunity and current operational status.
- Transfer mode and rebalancing status.
- Realized ROI, total realized profit and ROI earnings history.

ROI publication rule: Oddtrue calculates realized ROI only after a trade has completed and all applicable costs have been deducted.

Realized ROI = Net Realized Profit ÷ Capital Used × 100

Only positive realized ROI from successfully completed trades is published in the ROI earnings list. Losses are not published in that list, but all losses, failed trades, recovery costs and underlying transactions must remain recorded internally and reflected accurately in the user's balance.

An estimated opportunity ROI must never be presented as earned profit.

## 13. John pauses and resumes trading

If John pauses trading, Oddtrue stops assigning new opportunities to him while safely resolving active orders, liabilities and recovery operations.

When John resumes, the engine rechecks API connectivity, balances, margin health, borrowing capacity, inventory and transfer status before returning him to the eligible client rotation pool.

## Final responsibility split

John's responsibility: Register, connect his own Binance and Bitget API credentials, maintain sufficient capital and collateral, select transfer preferences, and monitor his dashboard.

Oddtrue's responsibility: Scan opportunities, calculate VWAP and net ROI, enforce allocation and liquidity limits, rotate clients, execute permitted trades, manage borrowing and repayment, recover failed executions, reconcile balances, calculate transfers and maintain accurate records.

Important implementation requirement: API credentials must be encrypted, access-controlled and never exposed in logs or to other users. API keys should have only the permissions needed for the selected functionality. Withdrawal permissions should be disabled unless a specifically authorized transfer feature genuinely requires them, and any enabled transfer functionality must have explicit user authorization, destination/network controls and appropriate safeguards.

The user journey must work independently for each client. One user's balances, credentials, trades, borrowing liabilities, transfers and ROI must never be mixed with another user's account.
