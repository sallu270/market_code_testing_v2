# Strategy for the LLM: OI Build-up Entry from 09:18 (NIFTY options)

## How to use this file

1. Paste everything under "THE STRATEGY" into a coding LLM, with your project files attached.
2. Items marked **ASSUMPTION** were not stated by the client. The LLM must build them as settings, so they can be changed without code. Confirm them with the client later (section 13).
3. Ask for one phase at a time, and run the tests after each phase.
4. If the LLM wants to change a rule, tell it: "Do not change the rules. Report the problem and wait."

---

# THE STRATEGY

## 1. Goal

Paper-trade NIFTY 50 options using the **Upstox API**. The bot reads the open interest (OI) and price movement of the **CE side and the PE side**, labels each side with a build-up type, and uses a **16-condition table** to decide: CE Buy, PE Buy or No trade. **No entry is allowed before 09:18 and none after 14:45.**

This **replaces** the earlier "first positive tick at 09:15:00" entry. The 09:15:00 pre-open values are stale (yesterday's), so a decision cannot be made then.

## 2. Daily timeline

| Time | What happens |
|---|---|
| Before 09:00 | User sets lots (section 6) and the settings. Settings freeze at 09:00:00. |
| 09:00 to 09:14:55 | Read the NIFTY index from the WebSocket. Compute and show the 8 strikes (4 ITM CE, 4 ITM PE) around spot. Show the gap and % change against the **previous-day close** (section 9). |
| 09:14:55 | Lock the 8 contracts. |
| **09:15:00** | **Take the snapshot** of the 8 contracts: LTP, OI, bid, ask. No trading. |
| 09:15 to 09:18 | Collect data only. **No entry.** |
| **From 09:18** | Wait for the first fresh OI update (section 5), then evaluate the table and trade. |
| 14:45 | No new entries after this time. |
| 15:15:00 | Automatic square-off by the clock, at the live bid. |

## 3. Definitions

- **Locked contracts:** the 4 largest strikes below spot (ITM CE) and the 4 smallest strikes above spot (ITM PE). Strike step and lot size come from the Upstox instrument file, never hardcoded. Nearest expiry.
- **Snapshot:** the values at 09:15:00 for each locked contract.
- **ΔCE and ΔPE (OI change):** sum over the side's 4 contracts of (OI now minus OI in the snapshot). **ASSUMPTION:** aggregate over the 4 ITM contracts per side.
- **Price change per side:** (sum of LTP now minus sum of LTP in the snapshot) divided by the sum of LTP in the snapshot, in %. **ASSUMPTION:** same aggregation.
- **Direction (UP, DOWN or FLAT):** UP if the change is above +threshold, DOWN if below minus threshold, otherwise FLAT. **ASSUMPTION:** price threshold 0.5 %, OI threshold 0.1 %. Both are settings.
- **Build-up type for a side** (needs both price and OI direction, neither FLAT):

| Price | OI | Type |
|---|---|---|
| UP | UP | **LB** long build-up |
| DOWN | UP | **SB** short build-up |
| UP | DOWN | **SC** short covering |
| DOWN | DOWN | **LU** long unwinding |

If any of the four directions (CE price, CE OI, PE price, PE OI) is FLAT, the result is **WAIT** (no decision this cycle).

## 4. The 16 conditions (client's table)

| # | CE | PE | Decision | Stars | Size |
|---|---|---|---|---|---|
| 1 | SC | SB | **CE Buy** | ⭐⭐⭐ | full lots |
| 2 | LB | SB | **CE Buy** | ⭐⭐ | normal lots |
| 3 | LB | LU | **CE Buy** | ⭐⭐ | normal lots |
| 4 | SC | LU | **CE Buy** | ⭐⭐ | quick scalp |
| 5 | SB | SC | **PE Buy** | ⭐⭐⭐ | full lots |
| 6 | SB | LB | **PE Buy** | ⭐⭐ | normal lots |
| 7 | LU | LB | **PE Buy** | ⭐⭐ | normal lots |
| 8 | LU | SC | **PE Buy** | ⭐⭐ | quick scalp |
| 9 | LB | LB | No trade (both premiums rising, IV spike) | | |
| 10 | LB | SC | No trade (both rising) | | |
| 11 | SC | LB | No trade (both rising) | | |
| 12 | SC | SC | No trade (both rising) | | |
| 13 | SB | SB | No trade (both falling, range market) | | |
| 14 | SB | LU | No trade (both falling) | | |
| 15 | LU | SB | No trade (both falling) | | |
| 16 | LU | LU | No trade (both falling, dead market) | | |

Implement it as a lookup table. Pattern for tests: **CE price UP and PE price DOWN gives CE Buy; PE price UP and CE price DOWN gives PE Buy; both the same direction gives No trade.** OI decides only the stars and the size.

## 5. When to evaluate

- Take the 09:15:00 snapshot. Before 09:18 only collect data.
- At or after 09:18:00, evaluate when OI is **fresh**: at least one locked contract on **each side** has an OI value different from its snapshot value. **ASSUMPTION** (the feed gives no OI timestamp).
- After that, re-evaluate on every OI change of a locked contract, at most once every 5 seconds, until 14:45. **ASSUMPTION.**
- The bot must **never enter on pre-open data**: no entry before 09:18, and none while the OI is still equal to the snapshot.
- OI update speed on the Upstox WebSocket has **not been measured**. Record every OI change with its time (section 12), so the real cadence can be measured after the first mornings.

## 6. Entry

1. A decision of CE Buy or PE Buy, while no position is open, trades today are below the limit (**ASSUMPTION:** 1 per day, a setting) and the time is between 09:18:00 and 14:45:00 triggers the entry.
2. **Contract:** among the 4 locked ITM contracts of the chosen side, keep those with a valid live quote (bid > 0, ask > 0, ask not below bid) and take the **tightest spread** (spread % = (ask minus bid) / ask; tie: larger ask quantity, then lower ask).
3. **Lots:** the user enters three numbers before 09:00: lots for ⭐⭐⭐ (full), for ⭐⭐ normal, and for quick scalp. Quantity = lots x lot size. **ASSUMPTION:** example defaults 3, 2 and 1; the client must set the real numbers.
4. **Order:** limit BUY at the live best ask plus a buffer, rounded to the 0.05 tick. If not filled within 2 seconds, modify or cancel and retry once at the fresh ask; then skip and log. The wait and the retries are settings.
5. Save with the trade: table rule number, stars, ΔCE, ΔPE, the build-up type of each side, and all timestamps.

## 7. Exit

- **Exit now** button and command, or the automatic square-off at **15:15:00 by the clock** (not by a tick).
- Exit = limit SELL at the live best bid minus buffer, retried at the fresh bid until filled.
- **ASSUMPTION:** no stop-loss, target or reversal. How a "quick scalp" exits is **not defined by the client** and must be asked.
- Emergency stop: always available. It cancels open orders, exits the position and blocks new entries.

## 8. Paper-trading prices (client correction)

- **Entry fills at the live ask. Exit fills at the live bid.** Never the last price or the previous close.
- If no live bid is available at exit, fetch a quote once. If that fails, keep retrying and alert. **Never use the entry price as the exit price** (this caused a ₹0 P&L bug).

## 9. Previous close, gap and % change (display columns)

- Use the true **previous-day close** from the feed (`cp`) or the quote API (`prev_close_price`).
- Before the market opens, the index shows yesterday's close, so the change must be **0**. If the change at 08:57 is bigger than 2 %, flag "previous close suspicious" and stop.
- Gap % and % change use the previous close. The build-up types use the **09:15:00 snapshot**. Do not mix the two references.

## 10. What the screen shows

- The chart first. Below it, only the **8 dynamic strikes** (they move as spot moves): strike, CE and PE LTP, build-up type (LB, SB, SC or LU), volume, bid and ask.
- A summary line: ΔCE, ΔPE, the CE and PE types, the table rule number, the stars, and the decision (CE Buy, PE Buy, No trade or Wait).
- Buttons: **Exit position** and **Emergency stop**. **Remove the ARM NOW button and its logic.**
- The stock list (section 11) with a top-N filter.

## 11. Stocks module (Release 2)

- List the NIFTY 50 stocks, with a filter to show only the top N (5, 6 or 10). No blank first row.
- Show only stocks whose price change is at least ±0.5 % (client rule).
- Rank by **I = (ΔPE minus ΔCE) / (|ΔPE| + |ΔCE|)** and by the **Power Score**, **not** by volume. **ASSUMPTION:** ΔPE and ΔCE are the change in total PE OI and total CE OI of the stock's options since the 09:15:00 snapshot. **The Power Score is not defined yet and must be requested from the client.**
- Add the ⭐ rank column (stars from the table) and a **futures OI confirmation** column. **ASSUMPTION:** CE Buy is confirmed when the stock's current-month future shows LB or SC; PE Buy when it shows SB or LU. The client's final spec must replace this.
- Stocks have no OI of their own: use their futures and options. One call to the Full Market Quotes V3 API takes up to 500 keys. Strike step, lot size and expiry come from the instrument file per stock. The user sets the quantity per stock.

## 12. Recording

- Every evaluation: time, ΔCE, ΔPE, price changes, the 4 directions, the types, the rule number, stars, decision and the reason for any WAIT.
- Every OI change of a locked contract with the time it was received.
- Every tick of the locked contracts and the index from 09:00.
- Every order attempt, fill and exit, with: tick time (`t_exch`), Upstox server time (`t_server`, from `currentTs`), receive time (`t_recv`), decision time, send time, response time and Upstox `metadata.latency`, all in milliseconds.
- Keep the clock synchronised (NTP) and log the clock offset at 08:55 and 09:14.

## 13. ASSUMPTIONS to confirm with the client

| # | Question | Default used |
|---|---|---|
| 1 | Which CE and PE does the table use? | Aggregate of the 4 ITM contracts per side |
| 2 | Is the price change measured from the 09:15:00 snapshot (stale pre-open price, so it also includes the gap) or from the first trade after 09:15:00? | 09:15:00 snapshot |
| 3 | Up/down thresholds for price and OI | 0.5 % and 0.1 % |
| 4 | How is "fresh OI" detected, and how often to re-evaluate? | OI differs from snapshot on both sides; re-check on each change, at most every 5 s |
| 5 | Lots for full, normal and quick scalp | 3, 2, 1 as examples |
| 6 | What is a quick scalp and how does it exit? | Same as other trades: manual or 15:15 |
| 7 | One trade a day, or several? Reversal when the signal flips? | One trade, no reversal |
| 8 | Δ and Power Score for stocks; futures OI confirmation | As in section 11; Power Score unknown |

## 14. Acceptance tests (write them and make them pass)

- All 16 combinations of CE type x PE type give exactly the decision, stars and size in the table.
- Any FLAT direction gives WAIT.
- No entry before 09:18:00, none while OI equals the snapshot, none after 14:45:00.
- With CE LB/SC and PE SB, the bot never buys a PE.
- Paper entry fills at the live ask, paper exit at the live bid; an exit never uses the entry price.
- The 15:15 square-off happens even if no ticks arrive.
- The previous-close change is 0 before the open.
- The Exit button closes the position; there is no ARM NOW button.
- Every trade record has the rule number, stars, ΔCE, ΔPE and timestamps.

## 15. What not to do

- No entry on pre-open data. No machine learning. No REST polling for prices (use the WebSocket; REST only for batched quotes, through the rate limiter).
- No live orders. No hardcoded strike step, lot size or expiry.
- Do not put the access token in code, logs or zip files.
