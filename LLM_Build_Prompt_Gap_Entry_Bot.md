# Prompt to give an LLM: build the Opening Gap Entry Bot

## How to use this file

1. Start a new chat with a coding LLM. Paste everything under "THE PROMPT" below.
2. Attach your current project files (`main.py`, `upstox_feed.py`, `live_chain.py`, `rest_guard.py`, `db_writer.py`, `strategy_engine.py`) and `Gap_Entry_Strategy_Rules_and_Architecture.md`.
3. Ask for **one phase at a time** (section 9 of the prompt). Do not ask for everything at once.
4. After each phase, run the tests, then paste any error or log back into the chat.
5. If the LLM changes a rule, say: "Do not change the rules. Report the problem and wait."

---

# THE PROMPT

## 1. Role and goal

You are a senior Python engineer. Build an **Opening Gap Entry Bot** for Indian index and stock options using the **Upstox API**. The bot runs in **paper mode first**. Speed at the 09:15 open matters most, and every step must be timed in milliseconds.

Work in small phases. Write tests with simulated ticks for each phase. If a rule is unclear, state your assumption in one line and continue. Never change a rule silently. Never invent Upstox fields: if you are not sure, print the raw message and ask me.

## 2. What exists already

A Python 3.12 + Flask project with: an Upstox WebSocket feed handler (MarketDataStreamerV3, full mode), a live option chain kept current by ticks (`live_chain.py`), a REST rate limiter (`rest_guard.py`), a background database batch writer (`db_writer.py`) and SQLite storage. Reuse these. Do not poll REST for prices.

## 3. The strategy (Release 1: NIFTY 50 options)

1. **Before 09:00** the user enters the number of lots. Quantity = lots x lot size (lot size and strike step come from the Upstox instrument file, never hardcoded). Settings freeze at 09:00:00.
2. **From 09:00** read the NIFTY index value from the WebSocket (it moves during pre-open and settles around 09:08 to 09:12).
3. **At 09:14:55 lock 8 contracts** of the nearest expiry:
   - 4 ITM calls = the 4 largest strikes **below** spot.
   - 4 ITM puts = the 4 smallest strikes **above** spot.
   - A strike equal to spot is excluded.
   - Example, spot 23000: CE 22950, 22900, 22850, 22800; PE 23050, 23100, 23150, 23200.
4. **At lock time store `ref_ltp`** (the current LTP) of every locked contract. It is fixed for the whole day and never updated. If the feed has no LTP, use the previous close and record the source. (Options do not trade before 09:15, so this is yesterday's last price.)
5. **Trigger (from 09:15:00):** a tick on any locked contract is *positive* when (a) its last-trade time is at or after 09:15:00 today (a fresh trade) and (b) its LTP is strictly above that contract's `ref_ltp`. The **first** positive tick decides the side (CE or PE). Ties: earliest exchange time, then the larger percentage gain.
6. **Choose the strike:** among the 4 ITM contracts on that side, keep those with a valid quote (bid > 0, ask > 0, ask not below bid, quote updated at or after 09:15:00) and pick the **tightest spread** (spread % = (ask - bid) / ask; tie: larger ask quantity, then lower ask). If none is valid, wait up to 100 ms and re-check, then skip and log.
7. **Order:** one limit BUY at best ask + buffer, rounded to the 0.05 tick. Orders are pre-built before 09:15 for all locked contracts; only the price is filled in at trigger time.
8. **If not filled in 2 seconds:** modify or cancel, retry once at the fresh ask + buffer, then skip and log. Wait time and retries are settings.
9. **One trade per day.** After the entry the bot never enters again.
10. **Exit:** manual **Exit now** button (and a command), or an automatic square-off at **15:15:00 triggered by the clock** (not by a tick). Exit = limit sell at best bid - buffer, retried at the fresh bid until filled. No stop-loss, no target, no reversal for now.
11. **Safety:** an emergency stop that is always available (cancels orders, exits the position, blocks entries). Optional daily loss limit and max trades, off by default. Paper mode only until I say otherwise.

## 4. Display-only feature: long and short build-up

For each of the 8 locked strikes show a label. It is **display only and never affects the entry**.

| Price vs previous close | OI vs previous OI | Label |
|---|---|---|
| Up | Up | Long build-up |
| Down | Up | Short build-up |
| Up | Down | Short covering |
| Down | Down | Long unwinding |

Use a small neutral threshold (setting). Previous values come from one batched quote call at about 09:14:50; live values from the WebSocket. Save the label with every trade record.

## 5. Release 2: NIFTY 50 stocks

- A screen listing all 50 stocks: price, volume, OI, OI change, OI change %, with sortable columns, a checkbox to select 5 or 6 stocks, and a **quantity (lots) box per stock**.
- Stocks themselves have **no OI**, so rank them using their **current-month futures** (OI and volume). One call to the Full Market Quotes V3 API (`GET /v3/market-quote/quotes`, up to 500 instrument keys) gives volume, `oi`, `previous_oi`, `prev_close_price` and `indicative_equilibrium_price`.
- Before 09:00 "today's OI change" does not exist: rank by yesterday's OI change and volume, and the pre-open indicative price. Live values take over after the open.
- Each selected stock runs the **same strategy on its options** (steps 3 to 10), independently. Strike step, lot size and expiry come from the instrument file per stock (stock options are monthly).
- Before 09:15 check that the account has enough funds for all selected stocks (live mode).

## 6. Upstox facts to respect

- Market data: WebSocket `MarketDataStreamerV3`, full mode. Messages carry a server timestamp `currentTs` (milliseconds). Each message has the exchange last-trade time, volume, OI, 5-level bid/ask and the previous close.
- Orders: `https://api-hft.upstox.com/v3/order/place` (and modify and cancel). Every reply has `metadata.latency` (Upstox processing time in ms). The Place Order V3 docs mention a sandbox.
- **No option order can be placed before 09:15:00** (options have no pre-open session).
- REST limits: keep a client-side limiter (5 calls per second, 800 per 30 minutes), pause on HTTP 429 and back off. Never poll REST in a loop. The access token expires daily.
- Live trading later needs a registered static IP and the current SEBI/exchange algo rules. This is out of scope now.

## 7. Performance rules for the trigger path

- From the first tick of a locked contract to the order being sent: **everything in memory**. No REST call, no database write, no logging, no disk access, no printing.
- Send orders from a dedicated sender thread through a persistent, pre-warmed HTTP connection.
- Pre-build order payloads before 09:15. Disable and freeze Python garbage collection from 09:14:50 to 09:16.
- Database and CSV writes happen on a background thread with batching.
- Use `time.perf_counter_ns()` for gaps inside the bot and `time.time_ns()` for comparison with Upstox. Require NTP time sync and log the clock offset at 08:55 and 09:14.

## 8. Recording (all in milliseconds)

Record for every entry: `t_exch` (feed last-trade time), `t_server` (`currentTs`), `t_recv` (first moment our code sees the message), `t_decoded`, `t_trigger`, `t_selected`, `t_built`, `t_send`, `t_response`, `upstox_processing_ms` (`metadata.latency`), `t_open_ack`, `t_filled`.

Derived gaps: Upstox to bot = `t_recv - t_server`; bot work = `t_send - t_recv`; order round trip = `t_response - t_send`; network = round trip - `upstox_processing_ms`; time from the open = `t_send - 09:15:00.000`.

Also store every tick of the locked contracts and the index from 09:00, every decision (candidate quotes, spreads, chosen contract, reason), every order attempt and every exit. Add a **morning rehearsal mode**: use index ticks between 09:00 and 09:15 as fake triggers and run the full path (decode, select, build, send to the sandbox) to collect timing samples before the real open.

## 9. Phases (do one per request)

1. Data capture and screen: index feed, instrument file, strike planner, lock at 09:14:55, reference LTP, hot cache. Read-only.
2. Trigger and strike selector with a paper broker (simulated fill against the live bid/ask). Tests with simulated ticks.
3. Timing recorder, daily CSV, rehearsal mode.
4. Manual exit, 15:15 clock square-off, emergency stop.
5. Build-up labels on screen and in the trade record.
6. NIFTY 50 stocks screen and ranking (read-only).
7. Per-stock quantity and the same strategy on the selected stocks' options.

## 10. Defaults to use (do not ask me about these)

Lock time 09:14:55. Settings freeze 09:00:00. Ask buffer: a setting, default 0.50. Max spread 3 %. Entry window 60 seconds (no trade after that). Neutral threshold for build-up 0.1 % price and 0.1 % OI. Standby strikes: 0.

## 11. Acceptance tests (write them and make them pass)

- Spot 23000 locks exactly the 8 contracts in the example above; spot exactly on a strike excludes that strike.
- A tick before 09:15:00, or with an old last-trade time, never triggers.
- A tick with LTP equal to `ref_ltp` does not trigger; one above it does.
- The first positive tick on a PE contract trades a PE, never a CE.
- The tightest valid spread wins; contracts without a fresh two-sided quote are ignored; no valid quote leads to a skip.
- No second entry in the same day.
- Exit now and the 15:15 timer both close the position, even if no ticks arrive.
- The trigger path makes no REST call and no database write (assert this in a test).
- A 429 response pauses REST calls and the bot keeps running.
- All timestamps in section 8 are present on every recorded entry.

## 12. What not to do

- No machine learning, no extra frameworks, no REST polling for prices.
- No live orders. No stop-loss, target or reversal logic.
- No hardcoded strike step, lot size or expiry.
- Do not put the access token in code, logs or zip files.
