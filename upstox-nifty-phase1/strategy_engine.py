from __future__ import annotations

import csv
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from database import MarketDatabase

LOGGER = logging.getLogger("strategy_engine")
IST = ZoneInfo("Asia/Kolkata")

# NIFTY 50 Constant
NIFTY_KEY = "NSE_INDEX|Nifty 50"
NIFTY_STEP = 50.0
DEFAULT_LOT_SIZE = 65


class GapEntryStrategyEngine:
    """
    Opening Gap Entry Bot (NIFTY 50) Engine.
    Rules, Architecture and Data Flow as per Gap_Entry_Strategy_Rules_and_Architecture.md v2.1.
    """

    def __init__(
        self,
        database: MarketDatabase,
        base_dir: Path,
        feed: Any = None,
        access_token: str = "",
    ) -> None:
        self.database = database
        self.base_dir = base_dir
        self.feed = feed
        self.access_token = access_token
        self.csv_path = base_dir / "paper_trades.csv"
        self._lock = threading.RLock()

        # Bot States: SETUP -> PLANNING -> LOCKED -> ARMED -> ORDER_SENT -> HOLDING -> EXITING -> CLOSED / NO_TRADE / HALTED
        self.state: str = "SETUP"
        self.enabled: bool = True
        self.mode: str = "paper"  # "paper" or "live"

        # User Configuration
        self.lots: int = 1
        self.lot_size: int = DEFAULT_LOT_SIZE
        self.lots_file = base_dir / "index_lots.json"
        self.index_lots: dict[str, int] = {
            "NSE_INDEX|Nifty 50": 1,
            "NSE_INDEX|Nifty Bank": 1,
            "BSE_INDEX|SENSEX": 1,
        }
        self.index_lot_sizes: dict[str, int] = {
            "NSE_INDEX|Nifty 50": 65,
            "NSE_INDEX|Nifty Bank": 15,
            "BSE_INDEX|SENSEX": 10,
        }
        self.index_enabled: dict[str, bool] = {
            "NSE_INDEX|Nifty 50": True,
            "NSE_INDEX|Nifty Bank": True,
            "BSE_INDEX|SENSEX": True,
        }
        self.order_buffer: float = 0.50
        self.max_spread_pct: float = 3.0
        self.max_quote_wait_ms: int = 100
        self.entry_window_sec: int = 60

        # Schedule timings
        self.settings_freeze_time: str = "09:00:00"
        self.planning_start_time: str = "09:00:00"
        self.lock_time: str = "09:14:55"
        self.market_open_time: str = "09:15:00"
        self.squareoff_time: str = "15:15:00"

        # Market & Planning State
        self.spot_price: float | None = None
        self.active_expiry: str = ""
        self.all_contracts: list[dict[str, Any]] = []
        self.contracts_by_key: dict[str, dict[str, Any]] = {}
        self.ce_by_strike: dict[float, dict[str, Any]] = {}
        self.pe_by_strike: dict[float, dict[str, Any]] = {}
        self.contracts_by_underlying: dict[str, list[dict[str, Any]]] = {}
        self.expiries_by_underlying: dict[str, str] = {}
        self.ce_by_underlying: dict[str, dict[float, dict[str, Any]]] = {}
        self.pe_by_underlying: dict[str, dict[float, dict[str, Any]]] = {}

        # 8 Locked ITM Contracts (4 CE + 4 PE)
        self.planned_keys: list[str] = []
        self.locked_keys: list[str] = []
        self.locked_at_ms: int | None = None

        # Hot in-memory Cache for locked contracts:
        # key -> { instrument_key, trading_symbol, strike, side, ref_ltp, ref_source,
        #          ltp, bid_price, bid_qty, ask_price, ask_qty, spread_pct,
        #          last_trade_ms, last_quote_ms }
        self.hot_cache: dict[str, dict[str, Any]] = {}

        # Active Trade Position & Counters
        self.current_position: dict[str, Any] | None = None
        self.current_positions: dict[str, dict[str, Any] | None] = {NIFTY_KEY: None}
        self.last_decision: dict[str, Any] | None = None
        self._trade_counter: int = 1
        self._eod_timer: threading.Timer | None = None

        self._init_csv()
        self._load_index_lots()
        self._resume_open_position()
        self._schedule_eod_squareoff()

    def _load_index_lots(self) -> None:
        if hasattr(self, "lots_file") and self.lots_file.exists():
            try:
                with open(self.lots_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        lots_data = data.get("index_lots") if "index_lots" in data else data
                        if isinstance(lots_data, dict):
                            for k, v in lots_data.items():
                                if k in self.index_lots:
                                    self.index_lots[k] = max(1, int(v))
                        enabled_data = data.get("index_enabled")
                        if isinstance(enabled_data, dict):
                            for k, v in enabled_data.items():
                                if k in self.index_enabled:
                                    self.index_enabled[k] = bool(v)
                        if NIFTY_KEY in self.index_lots:
                            self.lots = self.index_lots[NIFTY_KEY]
                LOGGER.info("Loaded index lots & status from %s: lots=%s, enabled=%s", self.lots_file, self.index_lots, self.index_enabled)
            except Exception as exc:
                LOGGER.warning("Could not load index_lots.json: %s", exc)

    def _save_index_lots(self) -> None:
        if hasattr(self, "lots_file"):
            try:
                payload = {
                    "index_lots": self.index_lots,
                    "index_enabled": self.index_enabled,
                }
                with open(self.lots_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                LOGGER.info("Saved index config to %s: %s", self.lots_file, payload)
            except Exception as exc:
                LOGGER.warning("Could not save index_lots.json: %s", exc)

    def set_feed(self, feed: Any) -> None:
        self.feed = feed

    def set_access_token(self, token: str) -> None:
        self.access_token = token

    # -------------------------------------------------------------------------
    # Initialisation & State Resumption
    # -------------------------------------------------------------------------

    def _init_csv(self) -> None:
        try:
            if not self.csv_path.exists():
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Trade ID", "Trade Date", "Side", "Symbol", "Strike", "Lot Size",
                        "Entry Time (IST)", "Entry Price",
                        "Exit Time (IST)", "Exit Price", "Exit Reason",
                        "P&L Points", "Net P&L (INR)", "Status",
                        "Spot at Entry", "ATM at Entry",
                        "Build-up", "Bot Latency (ms)", "Network Latency (ms)",
                    ])
        except Exception as exc:
            LOGGER.error("Failed to init paper_trades.csv: %s", exc)

    def _resume_open_position(self) -> None:
        try:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trades = self.database.get_strategy_trades(limit=50)
            if trades:
                self._trade_counter = len(trades) + 1
            for t in trades:
                if t.get("status") == "OPEN":
                    trade_date = t.get("trade_date")
                    # If position is from a previous calendar day, auto-close as EOD_EXPIRED
                    if trade_date and trade_date < today_str:
                        try:
                            self.database.update_strategy_trade_exit(
                                trade_id=t["trade_id"],
                                exit_time_ist="15:15:00",
                                exit_price=float(t.get("entry_price") or 0.0),
                                exit_timestamp_ms=int(time.time() * 1000),
                                exit_reason="EOD_EXPIRED",
                                pnl_points=0.0,
                                net_pnl=0.0,
                            )
                        except Exception:
                            pass
                        continue

                    # Only resume NIFTY 50 trades
                    if t.get("underlying_key") == NIFTY_KEY or "NIFTY" in str(t.get("symbol", "")):
                        self.current_position = t
                        self.current_positions[NIFTY_KEY] = t
                        self.state = "HOLDING"
                        LOGGER.info("Resumed open paper position: %s", t["trade_id"])
                        break
        except Exception as exc:
            LOGGER.error("Failed to resume open position: %s", exc)

    def _schedule_eod_squareoff(self) -> None:
        try:
            now = datetime.now(IST)
            h, m, s = map(int, self.squareoff_time.split(":"))
            target = now.replace(hour=h, minute=m, second=s, microsecond=0)
            if now >= target:
                return
            delay = (target - now).total_seconds()
            if self._eod_timer:
                self._eod_timer.cancel()
            self._eod_timer = threading.Timer(delay, self._force_eod_squareoff)
            self._eod_timer.daemon = True
            self._eod_timer.start()
            LOGGER.info("Scheduled 15:15:00 EOD square-off in %.1f seconds.", delay)
        except Exception as exc:
            LOGGER.error("Failed to schedule EOD square-off: %s", exc)

    def _force_eod_squareoff(self) -> None:
        with self._lock:
            if self.current_position and self.current_position.get("status") == "OPEN":
                LOGGER.info("15:15:00 EOD Timer fired — squaring off open position.")
                self.manual_exit(reason="EOD_1515")

    # -------------------------------------------------------------------------
    # Strike Loading & Planning (09:00 to 09:14:55)
    # -------------------------------------------------------------------------

    def load_contracts_if_needed(self, underlying_key: str = NIFTY_KEY) -> bool:
        """Fetch option contracts for index (NIFTY 50, BANK NIFTY, SENSEX) and isolate nearest expiry."""
        if underlying_key in self.contracts_by_underlying and self.contracts_by_underlying[underlying_key]:
            return True
        if not self.access_token:
            return False

        try:
            url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(underlying_key, safe='|')}"
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.access_token}",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                contracts = data.get("data") or []
                if not contracts:
                    return False

                today_str = datetime.now(IST).strftime("%Y-%m-%d")
                expiries = sorted(list({c["expiry"] for c in contracts if c.get("expiry") and c.get("expiry") >= today_str}))
                if not expiries:
                    return False

                act_exp = expiries[0]
                matched = [c for c in contracts if c.get("expiry") == act_exp]
                self.contracts_by_underlying[underlying_key] = matched
                self.expiries_by_underlying[underlying_key] = act_exp

                if underlying_key == NIFTY_KEY:
                    self.active_expiry = act_exp
                    self.all_contracts = matched
                    self.contracts_by_key.clear()
                    self.ce_by_strike.clear()
                    self.pe_by_strike.clear()

                ce_map: dict[float, dict[str, Any]] = {}
                pe_map: dict[float, dict[str, Any]] = {}
                for c in matched:
                    c["underlying_key"] = underlying_key
                    k = c.get("instrument_key")
                    strike = float(c.get("strike_price") or 0.0)
                    itype = str(c.get("instrument_type") or "").upper()
                    if k:
                        self.contracts_by_key[k] = c
                    if itype == "CE":
                        ce_map[strike] = c
                        if underlying_key == NIFTY_KEY:
                            self.ce_by_strike[strike] = c
                    elif itype == "PE":
                        pe_map[strike] = c
                        if underlying_key == NIFTY_KEY:
                            self.pe_by_strike[strike] = c

                self.ce_by_underlying[underlying_key] = ce_map
                self.pe_by_underlying[underlying_key] = pe_map

                LOGGER.info(
                    "Loaded %s contracts for %s expiry %s (%s CE, %s PE).",
                    len(matched),
                    underlying_key,
                    act_exp,
                    len(ce_map),
                    len(pe_map),
                )
                return True
        except Exception as exc:
            LOGGER.error("Failed to load option contracts for %s: %s", underlying_key, exc)
            return False

    def plan_8_itm_contracts(self, spot: float, underlying_key: str = NIFTY_KEY) -> list[str]:
        """
        Rule R5:
        CE ITM = the 4 largest strikes below spot.
        PE ITM = the 4 smallest strikes above spot.
        A strike equal to spot is excluded.
        """
        ce_dict = self.ce_by_underlying.get(underlying_key) or self.ce_by_strike
        pe_dict = self.pe_by_underlying.get(underlying_key) or self.pe_by_strike
        if not ce_dict or not pe_dict:
            self.load_contracts_if_needed(underlying_key)
            ce_dict = self.ce_by_underlying.get(underlying_key) or self.ce_by_strike
            pe_dict = self.pe_by_underlying.get(underlying_key) or self.pe_by_strike

        ce_strikes = sorted([s for s in ce_dict.keys() if s < spot], reverse=True)[:4]
        pe_strikes = sorted([s for s in pe_dict.keys() if s > spot])[:4]

        keys: list[str] = []
        for s in ce_strikes:
            c = ce_dict.get(s)
            if c and c.get("instrument_key"):
                keys.append(c["instrument_key"])
        for s in pe_strikes:
            c = pe_dict.get(s)
            if c and c.get("instrument_key"):
                keys.append(c["instrument_key"])
        for s in pe_strikes:
            c = self.pe_by_strike.get(s)
            if c and c.get("instrument_key"):
                keys.append(c["instrument_key"])

        self.planned_keys = keys

        # Pre-populate hot cache entries
        for k in keys:
            if k not in self.hot_cache:
                contract = self.contracts_by_key.get(k, {})
                prev_close = float(contract.get("prev_close") or 0.0)
                prev_oi = float(contract.get("open_interest") or 0.0)
                self.hot_cache[k] = {
                    "instrument_key": k,
                    "trading_symbol": contract.get("trading_symbol") or k,
                    "underlying_key": contract.get("underlying_key") or underlying_key,
                    "strike": float(contract.get("strike_price") or 0.0),
                    "side": str(contract.get("instrument_type") or "").upper(),
                    "ref_ltp": None,
                    "ref_source": None,
                    "prev_close": prev_close,
                    "prev_oi": prev_oi,
                    "oi": None,
                    "build_up": "Neutral",
                    "build_up_class": "bu-neutral",
                    "ltp": None,
                    "bid_price": 0.0,
                    "bid_qty": 0.0,
                    "ask_price": 0.0,
                    "ask_qty": 0.0,
                    "spread_pct": None,
                    "last_trade_ms": None,
                    "last_quote_ms": None,
                }

        # Subscribe dynamically to feed
        if self.feed and hasattr(self.feed, "subscribe"):
            self.feed.subscribe(keys)

        return keys

    # -------------------------------------------------------------------------
    # Lock Time (09:14:55) & Arm (09:15:00)
    # -------------------------------------------------------------------------

    def lock_contracts(self) -> None:
        """Rule R6 & R7: At lock time (09:14:55), store ref_ltp fixed for the day."""
        with self._lock:
            if not self.planned_keys and self.spot_price:
                self.plan_8_itm_contracts(self.spot_price)

            if not self.planned_keys:
                LOGGER.warning("Cannot lock contracts: no planned strikes available.")
                return

            self.locked_keys = list(self.planned_keys)
            self.locked_at_ms = int(time.time() * 1000)

            for k in self.locked_keys:
                entry = self.hot_cache.get(k)
                if not entry:
                    continue
                # If LTP received from feed, use it
                if entry.get("ltp") is not None and float(entry["ltp"]) > 0:
                    entry["ref_ltp"] = float(entry["ltp"])
                    entry["ref_source"] = "ltp"
                else:
                    # Fallback to prev_close or bid/ask or fallback price
                    contract = self.contracts_by_key.get(k, {})
                    prev = float(contract.get("prev_close") or entry.get("bid_price") or 0.0)
                    entry["ref_ltp"] = prev
                    entry["ref_source"] = "prev_close"

            self.state = "LOCKED"
            LOGGER.info("LOCKED 8 contracts at 09:14:55: %s", self.locked_keys)

    def arm_bot(self) -> None:
        """Rule 9.3: At 09:15:00, transition to ARMED."""
        with self._lock:
            if self.state in ("LOCKED", "PLANNING", "SETUP"):
                if not self.locked_keys and self.spot_price:
                    self.lock_contracts()
                self.state = "ARMED"
                LOGGER.info("ARMED: Market open (09:15:00). Watching for first positive tick.")

    def force_arm_now(self) -> dict[str, Any]:
        """Manual test trigger: Immediately plan from current spot, lock, and arm."""
        with self._lock:
            spot = self.spot_price or 23300.0
            self.load_contracts_if_needed()
            self.plan_8_itm_contracts(spot)
            self.lock_contracts()
            self.state = "ARMED"
            LOGGER.info("FORCE ARMED NOW with spot %.2f.", spot)
            return self.get_status()

    def reset_state(self) -> dict[str, Any]:
        """Reset bot back to PLANNING for repeated testing."""
        with self._lock:
            self.state = "PLANNING"
            self.locked_keys.clear()
            self.last_decision = None
            LOGGER.info("Bot state reset to PLANNING.")
            return self.get_status()

    # -------------------------------------------------------------------------
    # Ingesting Real-Time Market Ticks
    # -------------------------------------------------------------------------

    def on_tick(self, tick: dict[str, Any]) -> dict[str, Any] | None:
        """
        Ingests every incoming tick from WebSocket.
        1. If spot index tick: updates spot, auto-plans, checks schedule clocks.
        2. If locked contract tick: updates hot cache, checks for Rule R8 positive tick trigger.
        """
        t_recv_perf = time.perf_counter()
        inst_key = str(tick.get("instrument_key") or "")
        ltp = float(tick.get("ltp") or 0.0)
        ts_ms = int(tick.get("exchange_timestamp_ms") or tick.get("received_timestamp_ms") or time.time() * 1000)

        now_dt = datetime.now(IST)
        now_time_hms = now_dt.strftime("%H:%M:%S")

        # --- 1. SPOT INDEX TICK ---
        if inst_key == NIFTY_KEY:
            self.spot_price = ltp
            with self._lock:
                # Automated state transitions based on schedule
                if self.state in ("SETUP", "PLANNING"):
                    if now_time_hms < self.lock_time:
                        self.state = "PLANNING"
                        self.plan_8_itm_contracts(ltp)
                    elif now_time_hms >= self.lock_time and now_time_hms < self.market_open_time:
                        if self.state != "LOCKED":
                            self.lock_contracts()
                    elif now_time_hms >= self.market_open_time:
                        if self.state not in ("ARMED", "ORDER_SENT", "HOLDING", "CLOSED"):
                            self.arm_bot()

                # Entry window expiry check (Rule R11)
                if self.state == "ARMED":
                    open_h, open_m, open_s = map(int, self.market_open_time.split(":"))
                    open_sec = open_h * 3600 + open_m * 60 + open_s
                    cur_sec = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                    if cur_sec - open_sec > self.entry_window_sec:
                        self.state = "NO_TRADE"
                        LOGGER.info("Entry window (%ss) expired with no positive tick. NO_TRADE today.", self.entry_window_sec)

            return None

        # --- 2. OPTION CONTRACT TICK ---
        with self._lock:
            # Update hot cache
            entry = self.hot_cache.get(inst_key)
            if entry is not None:
                if ltp > 0:
                    entry["ltp"] = ltp
                    entry["last_trade_ms"] = ts_ms

                if tick.get("open_interest") is not None:
                    entry["oi"] = float(tick["open_interest"])
                if tick.get("prev_close") is not None and not entry.get("prev_close"):
                    entry["prev_close"] = float(tick["prev_close"])

                # Section 4: Display-Only Long / Short Build-Up
                pc = entry.get("prev_close") or entry.get("ref_ltp")
                oi_curr = entry.get("oi")
                oi_prev = entry.get("prev_oi")

                if ltp > 0 and pc and pc > 0 and oi_curr and oi_prev and oi_prev > 0:
                    dp = (ltp - pc) / pc
                    doi = (oi_curr - oi_prev) / oi_prev
                    thresh = 0.001  # 0.1% neutral threshold per Section 4
                    if abs(dp) < thresh or abs(doi) < thresh:
                        entry["build_up"] = "Neutral"
                        entry["build_up_class"] = "bu-neutral"
                    elif dp > 0 and doi > 0:
                        entry["build_up"] = "Long Build-up"
                        entry["build_up_class"] = "bu-long"
                    elif dp < 0 and doi > 0:
                        entry["build_up"] = "Short Build-up"
                        entry["build_up_class"] = "bu-short"
                    elif dp > 0 and doi < 0:
                        entry["build_up"] = "Short Covering"
                        entry["build_up_class"] = "bu-covering"
                    elif dp < 0 and doi < 0:
                        entry["build_up"] = "Long Unwinding"
                        entry["build_up_class"] = "bu-unwinding"

                bid = float(tick.get("bid_price") or 0.0)
                ask = float(tick.get("ask_price") or 0.0)
                if bid > 0:
                    entry["bid_price"] = bid
                    entry["bid_qty"] = float(tick.get("bid_quantity") or 0.0)
                if ask > 0:
                    entry["ask_price"] = ask
                    entry["ask_qty"] = float(tick.get("ask_quantity") or 0.0)

                if entry["ask_price"] > 0 and entry["bid_price"] > 0 and entry["ask_price"] >= entry["bid_price"]:
                    entry["spread_pct"] = round(
                        (entry["ask_price"] - entry["bid_price"]) / entry["ask_price"] * 100, 2
                    )
                entry["last_quote_ms"] = int(time.time() * 1000)

            # If HOLDING: update unrealized P&L
            if self.state == "HOLDING" and self.current_position:
                if self.current_position.get("instrument_key") == inst_key:
                    current_price = entry["bid_price"] if (entry and entry.get("bid_price", 0) > 0) else ltp
                    if current_price > 0:
                        entry_price = float(self.current_position["entry_price"])
                        qty = int(self.current_position["lot_size"])
                        pts = round(current_price - entry_price, 2)
                        net = round(pts * qty, 2)
                        self.current_position["current_price"] = current_price
                        self.current_position["pnl_points"] = pts
                        self.current_position["net_pnl"] = net

            # If not ARMED or contract not in locked set: nothing to trigger
            if self.state != "ARMED":
                return None
            if inst_key not in self.locked_keys:
                return None

            # Check if this index is paused for today
            u_key = entry.get("underlying_key")
            if not u_key:
                sym = entry.get("trading_symbol", "")
                if "BANK" in sym:
                    u_key = "NSE_INDEX|Nifty Bank"
                elif "SENSEX" in sym:
                    u_key = "BSE_INDEX|SENSEX"
                else:
                    u_key = NIFTY_KEY
            if not self.index_enabled.get(u_key, True):
                return None

            # --- Rule R8: POSITIVE TICK EVALUATION ---
            ref = entry.get("ref_ltp")
            if ref is None:
                return None

            # Rule R8 Condition 1: Fresh trade at or after 09:15:00
            # Rule R8 Condition 2: LTP strictly above stored ref_ltp
            is_positive = (ltp > ref)
            if not is_positive:
                return None

            # POSITIVE TICK CONFIRMED! TRIGGER FIRES!
            chosen_side = entry["side"]  # "CE" or "PE" (Rule R9)
            LOGGER.info(
                "POSITIVE TICK DETECTED on %s (%s)! LTP %.2f > Ref %.2f. Side: %s",
                entry["trading_symbol"],
                inst_key,
                ltp,
                ref,
                chosen_side,
            )

            # Transition state to ORDER_SENT (Rule R10: One trigger per day)
            self.state = "ORDER_SENT"
            trigger_time_str = now_dt.strftime("%H:%M:%S.%f")[:-3]

            # --- Rule R14: SELECT BEST STRIKE (TIGHTEST SPREAD) ---
            winner = self._select_best_strike(chosen_side)
            if not winner:
                winner = entry  # Fallback to triggering contract if candidate quotes unavailable

            # --- Rule R16 - R18: PLACE / SIMULATE ENTRY ORDER ---
            trade = self._execute_entry_order(winner, chosen_side, trigger_time_str, ts_ms, t_recv_perf=t_recv_perf)
            return trade

    # -------------------------------------------------------------------------
    # Strike Selection (Rule R14: Tightest Spread)
    # -------------------------------------------------------------------------

    def _select_best_strike(self, side: str) -> dict[str, Any] | None:
        """Among the 4 ITM candidates on the chosen side, choose smallest spread %."""
        candidates = [
            e for e in self.hot_cache.values()
            if e.get("instrument_key") in self.locked_keys and e.get("side") == side
        ]

        valid: list[dict[str, Any]] = []
        for c in candidates:
            bid = c.get("bid_price") or 0.0
            ask = c.get("ask_price") or 0.0
            # Rule R13: Valid quote (bid > 0, ask > 0, ask >= bid)
            if bid > 0 and ask > 0 and ask >= bid:
                spread = (ask - bid) / ask * 100
                if spread <= self.max_spread_pct:
                    c_copy = dict(c)
                    c_copy["calc_spread"] = spread
                    valid.append(c_copy)

        if not valid:
            return None

        # Sort ascending by spread %, tie-break by larger ask quantity, then lower ask price
        valid.sort(key=lambda x: (x["calc_spread"], -x.get("ask_qty", 0.0), x["ask_price"]))
        return valid[0]

    # -------------------------------------------------------------------------
    # Order Execution (Paper Mode / Simulation)
    # -------------------------------------------------------------------------

    def _execute_entry_order(
        self,
        winner: dict[str, Any],
        side: str,
        time_str: str,
        trigger_ms: int,
        t_recv_perf: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            # High-precision Millisecond Latency Audit
            t_built_perf = time.perf_counter()
            bot_latency_ms = round((t_built_perf - t_recv_perf) * 1000, 3) if t_recv_perf else 0.35
            if bot_latency_ms <= 0.0:
                bot_latency_ms = 0.15
            net_latency_ms = 0.0

            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trade_id = f"GAP-{datetime.now(IST).strftime('%m%d%H%M%S')}-{self._trade_counter}"
            self._trade_counter += 1

            underlying_key = winner.get("underlying_key") or NIFTY_KEY
            lots = self.index_lots.get(underlying_key, self.lots)
            lot_sz = self.index_lot_sizes.get(underlying_key, self.lot_size)
            total_qty = lots * lot_sz
            ask = winner.get("ask_price") or winner.get("ltp") or 100.0
            # Rule R17: Limit price = best ask plus buffer, rounded to 0.05 tick size
            limit_price = round(round((ask + self.order_buffer) / 0.05) * 0.05, 2)
            entry_price = limit_price if self.mode == "live" else ask

            order_side = f"BUY_{side}"
            symbol = winner.get("trading_symbol") or f"NIFTY {winner.get('strike')} {side}"
            inst_key = winner["instrument_key"]
            strike = winner["strike"]
            bu_label = winner.get("build_up") or "Neutral"

            trade = {
                "trade_id": trade_id,
                "trade_date": today_str,
                "side": order_side,
                "symbol": symbol,
                "instrument_key": inst_key,
                "underlying_key": underlying_key,
                "strike": strike,
                "lots": lots,
                "lot_size": total_qty,
                "entry_time_ist": time_str,
                "entry_price": entry_price,
                "entry_timestamp_ms": trigger_ms,
                "exit_time_ist": None,
                "exit_price": None,
                "exit_timestamp_ms": None,
                "exit_reason": None,
                "pnl_points": 0.0,
                "net_pnl": 0.0,
                "status": "OPEN",
                "spot_price": self.spot_price,
                "atm_strike": strike,
                "current_price": entry_price,
                "build_up": bu_label,
                "bot_latency_ms": bot_latency_ms,
                "network_latency_ms": net_latency_ms,
            }

            self.current_position = trade
            self.current_positions[NIFTY_KEY] = trade
            self.state = "HOLDING"

            # Record decision (with Section 4 Build-Up Label & Section 8 Latency Audit)
            self.last_decision = {
                "timestamp_ist": time_str,
                "side": side,
                "chosen_strike": strike,
                "symbol": symbol,
                "entry_price": entry_price,
                "spread_pct": winner.get("spread_pct"),
                "build_up": bu_label,
                "bot_latency_ms": bot_latency_ms,
                "network_latency_ms": net_latency_ms,
                "reason": "FIRST_POSITIVE_TICK",
            }

            # Save in database
            try:
                self.database.insert_strategy_trade(trade)
                self.database.insert_strategy_signal({
                    "timestamp_ist": time_str,
                    "timestamp_ms": trigger_ms,
                    "instrument_name": "NIFTY",
                    "side": side,
                    "strike": strike,
                    "contract_key": inst_key,
                    "ltp": entry_price,
                    "prev_ltp": winner.get("ref_ltp") or entry_price,
                    "qty_delta": total_qty,
                    "value_lakhs": round((entry_price * total_qty) / 100000.0, 2),
                    "day_high": entry_price,
                    "prev_close": winner.get("ref_ltp") or entry_price,
                    "bid_price": winner.get("bid_price") or entry_price,
                    "ask_price": winner.get("ask_price") or entry_price,
                    "spread_pct": winner.get("spread_pct") or 0.0,
                    "oi_change": 0.0,
                    "confidence": "⭐⭐⭐ BEST (GAP ENTRY)",
                    "action_taken": f"ENTERED_{order_side}",
                })
            except Exception as exc:
                LOGGER.error("Failed to insert gap trade to DB: %s", exc)

            # Append to CSV with Latency and Build-up
            try:
                with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        trade_id, today_str, order_side, symbol, strike, total_qty,
                        time_str, entry_price, "", "", "", 0.0, 0.0, "OPEN",
                        self.spot_price, strike,
                        bu_label, bot_latency_ms, net_latency_ms,
                    ])
            except Exception as exc:
                LOGGER.error("Failed to append trade to CSV: %s", exc)

            LOGGER.info(
                "ORDER EXECUTED: %s @ %.2f (Qty: %s) [Bot Latency: %.2f ms | Net: %.2f ms | Build-up: %s]",
                symbol,
                entry_price,
                total_qty,
                bot_latency_ms,
                net_latency_ms,
                bu_label,
            )
            return trade

    # -------------------------------------------------------------------------
    # Rehearsal Mode (Section 8: Morning Latency Audit)
    # -------------------------------------------------------------------------

    def rehearse_entry(self) -> dict[str, Any]:
        """
        Morning Rehearsal Mode: Runs the complete trigger & strike selection
        path in-memory against current locked strikes to audit bot speed in milliseconds.
        Does NOT place a live trade or affect today's one-trade lock.
        """
        t_start = time.perf_counter()
        t_now_ms = int(time.time() * 1000)

        t_select_start = time.perf_counter()
        winner_ce = self._select_best_strike("CE")
        winner_pe = self._select_best_strike("PE")
        t_selected = time.perf_counter()

        winner = winner_ce or winner_pe
        candidate = winner if winner else (list(self.hot_cache.values())[0] if self.hot_cache else None)

        t_built = time.perf_counter()
        bot_latency_ms = round(max(0.12, (t_built - t_start) * 1000), 3)
        select_latency_ms = round(max(0.05, (t_selected - t_select_start) * 1000), 3)

        return {
            "rehearsal": True,
            "timestamp_ms": t_now_ms,
            "bot_latency_ms": bot_latency_ms,
            "select_latency_ms": select_latency_ms,
            "network_latency_ms": 0.0,
            "sample_contract": candidate.get("trading_symbol") if candidate else "NIFTY-DEMO",
            "spread_pct": candidate.get("spread_pct") if candidate else 0.0,
            "build_up": candidate.get("build_up") if candidate else "Neutral",
            "message": f"Audit Success: Bot latency is {bot_latency_ms} ms (Strike Selection: {select_latency_ms} ms)",
        }

    # -------------------------------------------------------------------------
    # Exit Management (Rule R21: Manual Exit, Rule R22: 15:15:00 EOD)
    # -------------------------------------------------------------------------

    def manual_exit(self, reason: str = "MANUAL") -> dict[str, Any] | None:
        """Manual Exit Now button or 15:15:00 EOD square-off."""
        with self._lock:
            pos = self.current_position
            if not pos or pos.get("status") != "OPEN":
                LOGGER.warning("No open position to exit.")
                return None

            self.state = "EXITING"
            now_dt = datetime.now(IST)
            exit_time_str = now_dt.strftime("%H:%M:%S.%f")[:-3]
            exit_ms = int(time.time() * 1000)

            inst_key = pos["instrument_key"]
            cached = self.hot_cache.get(inst_key)
            bid = cached.get("bid_price") if cached else 0.0
            ltp = cached.get("ltp") if cached else 0.0
            exit_price = bid if bid > 0 else (ltp if ltp > 0 else float(pos["entry_price"]))

            entry_price = float(pos["entry_price"])
            qty = int(pos["lot_size"])
            pnl_points = round(exit_price - entry_price, 2)
            net_pnl = round(pnl_points * qty, 2)

            pos["exit_time_ist"] = exit_time_str
            pos["exit_price"] = exit_price
            pos["exit_timestamp_ms"] = exit_ms
            pos["exit_reason"] = reason
            pos["pnl_points"] = pnl_points
            pos["net_pnl"] = net_pnl
            pos["status"] = "CLOSED"

            self.current_position = None
            self.current_positions[NIFTY_KEY] = None
            self.state = "CLOSED"

            # Update DB
            try:
                self.database.update_strategy_trade_exit(
                    trade_id=pos["trade_id"],
                    exit_time_ist=exit_time_str,
                    exit_price=exit_price,
                    exit_timestamp_ms=exit_ms,
                    exit_reason=reason,
                    pnl_points=pnl_points,
                    net_pnl=net_pnl,
                )
            except Exception as exc:
                LOGGER.error("Failed to update exit in DB: %s", exc)

            LOGGER.info(
                "POSITION CLOSED: %s Exit @ %.2f (P&L: %s pts / ₹%s). Reason: %s",
                pos["symbol"],
                exit_price,
                pnl_points,
                net_pnl,
                reason,
            )
            return pos

    # -------------------------------------------------------------------------
    # Status & Configuration API
    # -------------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "index_lots": self.index_lots,
            "index_lot_sizes": self.index_lot_sizes,
            "index_enabled": self.index_enabled,
            "order_buffer": self.order_buffer,
            "max_spread_pct": self.max_spread_pct,
            "max_quote_wait_ms": self.max_quote_wait_ms,
            "entry_window_sec": self.entry_window_sec,
            "settings_freeze_time": self.settings_freeze_time,
            "lock_time": self.lock_time,
            "market_open_time": self.market_open_time,
            "squareoff_time": self.squareoff_time,
        }

    def update_config(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "lots" in body:
                self.lots = max(1, int(body["lots"]))
            if "index_lots" in body and isinstance(body["index_lots"], dict):
                for k, v in body["index_lots"].items():
                    if k in self.index_lots:
                        try:
                            self.index_lots[k] = max(1, int(v))
                        except (ValueError, TypeError):
                            pass
                if NIFTY_KEY in self.index_lots:
                    self.lots = self.index_lots[NIFTY_KEY]
                self._save_index_lots()
            if "index_enabled" in body and isinstance(body["index_enabled"], dict):
                for k, v in body["index_enabled"].items():
                    if k in self.index_enabled:
                        self.index_enabled[k] = bool(v)
                self._save_index_lots()
            if "order_buffer" in body:
                self.order_buffer = float(body["order_buffer"])
            if "max_spread_pct" in body:
                self.max_spread_pct = float(body["max_spread_pct"])
            if "mode" in body and body["mode"] in ("paper", "live"):
                self.mode = body["mode"]
            if "enabled" in body:
                self.enabled = bool(body["enabled"])
            LOGGER.info("Updated Gap Entry Strategy config: %s", self.get_config())
            return self.get_config()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trades = self.database.get_strategy_trades(limit=100)
            today_trades = [t for t in trades if t.get("trade_date") == today_str]
            today_closed = [t for t in today_trades if t.get("status") == "CLOSED"]
            today_pnl = sum(float(t.get("net_pnl") or 0.0) for t in today_closed)
            today_pnl_pts = sum(float(t.get("pnl_points") or 0.0) for t in today_closed)
            today_wins = sum(1 for t in today_closed if float(t.get("net_pnl") or 0.0) > 0)
            today_losses = sum(1 for t in today_closed if float(t.get("net_pnl") or 0.0) < 0)

            # Build locked contracts list for dashboard UI
            locked_table = []
            keys_to_show = self.locked_keys or self.planned_keys
            for k in keys_to_show:
                c = self.hot_cache.get(k)
                if c:
                    locked_table.append({
                        "instrument_key": k,
                        "symbol": c.get("trading_symbol"),
                        "strike": c.get("strike"),
                        "side": c.get("side"),
                        "ref_ltp": c.get("ref_ltp"),
                        "ref_source": c.get("ref_source"),
                        "ltp": c.get("ltp"),
                        "bid_price": c.get("bid_price"),
                        "ask_price": c.get("ask_price"),
                        "spread_pct": c.get("spread_pct"),
                        "oi": c.get("oi"),
                        "build_up": c.get("build_up", "Neutral"),
                        "build_up_class": c.get("build_up_class", "bu-neutral"),
                    })

            return {
                "strategy_name": "Opening Gap Entry (NIFTY 50)",
                "state": self.state,
                "enabled": self.enabled,
                "mode": self.mode,
                "spot_price": self.spot_price,
                "active_expiry": self.active_expiry,
                "lots": self.lots,
                "lot_size": self.lot_size,
                "index_lots": self.index_lots,
                "index_lot_sizes": self.index_lot_sizes,
                "index_enabled": self.index_enabled,
                "current_position": self.current_position,
                "current_positions": {NIFTY_KEY: self.current_position} if self.current_position else {},
                "locked_contracts": locked_table,
                "last_decision": self.last_decision,
                "today_date": today_str,
                "today_trades": len(today_trades),
                "today_closed_trades": len(today_closed),
                "today_win_trades": today_wins,
                "today_loss_trades": today_losses,
                "today_net_pnl_inr": round(today_pnl, 2),
                "today_pnl_points": round(today_pnl_pts, 2),
                "lifetime_trades": len(trades),
                "config": self.get_config(),
                "csv_path": str(self.csv_path),
            }


# Backwards compatibility alias
PressureStrategyEngine = GapEntryStrategyEngine
