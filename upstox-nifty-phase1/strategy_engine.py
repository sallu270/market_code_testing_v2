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

# =========================================================================
# THE 16 CONDITIONS DECISION TABLE (Section 4)
# Pattern:
# CE price UP and PE price DOWN gives CE Buy
# PE price UP and CE price DOWN gives PE Buy
# Both the same direction gives No trade
# OI decides the stars and size
# =========================================================================
DECISION_TABLE: dict[tuple[str, str], dict[str, Any]] = {
    # 1. SC, SB -> CE Buy, ⭐⭐⭐, full lots
    ("SC", "SB"): {"rule": 1, "decision": "CE Buy", "side": "CE", "stars": "⭐⭐⭐", "size": "full"},
    # 2. LB, SB -> CE Buy, ⭐⭐, normal lots
    ("LB", "SB"): {"rule": 2, "decision": "CE Buy", "side": "CE", "stars": "⭐⭐", "size": "normal"},
    # 3. LB, LU -> CE Buy, ⭐⭐, normal lots
    ("LB", "LU"): {"rule": 3, "decision": "CE Buy", "side": "CE", "stars": "⭐⭐", "size": "normal"},
    # 4. SC, LU -> CE Buy, ⭐⭐, quick scalp
    ("SC", "LU"): {"rule": 4, "decision": "CE Buy", "side": "CE", "stars": "⭐⭐", "size": "scalp"},
    # 5. SB, SC -> PE Buy, ⭐⭐⭐, full lots
    ("SB", "SC"): {"rule": 5, "decision": "PE Buy", "side": "PE", "stars": "⭐⭐⭐", "size": "full"},
    # 6. SB, LB -> PE Buy, ⭐⭐, normal lots
    ("SB", "LB"): {"rule": 6, "decision": "PE Buy", "side": "PE", "stars": "⭐⭐", "size": "normal"},
    # 7. LU, LB -> PE Buy, ⭐⭐, normal lots
    ("LU", "LB"): {"rule": 7, "decision": "PE Buy", "side": "PE", "stars": "⭐⭐", "size": "normal"},
    # 8. LU, SC -> PE Buy, ⭐⭐, quick scalp
    ("LU", "SC"): {"rule": 8, "decision": "PE Buy", "side": "PE", "stars": "⭐⭐", "size": "scalp"},
    # 9. LB, LB -> No trade (both premiums rising, IV spike)
    ("LB", "LB"): {"rule": 9, "decision": "No trade", "side": None, "stars": "", "size": "", "note": "both premiums rising, IV spike"},
    # 10. LB, SC -> No trade (both rising)
    ("LB", "SC"): {"rule": 10, "decision": "No trade", "side": None, "stars": "", "size": "", "note": "both rising"},
    # 11. SC, LB -> No trade (both rising)
    ("SC", "LB"): {"rule": 11, "decision": "No trade", "side": None, "stars": "", "size": "", "note": "both rising"},
    # 12. SC, SC -> No trade (both rising)
    ("SC", "SC"): {"rule": 12, "decision": "No trade", "side": None, "stars": "", "size": "", "note": "both rising"},
    # 13. SB, SB -> No trade (both falling, range market)
    ("SB", "SB"): {"rule": 13, "decision": "No trade", "side": None, "stars": "", "size": "", "note": "both falling, range market"},
    # 14. SB, LU -> No trade (both falling)
    ("SB", "LU"): {"rule": 14, "decision": "No trade", "side": None, "stars": "", "size": "", "note": "both falling"},
    # 15. LU, SB -> No trade (both falling)
    ("LU", "SB"): {"rule": 15, "decision": "No trade", "side": None, "stars": "", "size": "", "note": "both falling"},
    # 16. LU, LU -> No trade (both falling, dead market)
    ("LU", "LU"): {"rule": 16, "decision": "No trade", "side": None, "stars": "", "size": "", "note": "both falling, dead market"},
}


class GapEntryStrategyEngine:
    """
    OI Build-up Entry from 09:18 Strategy Engine (NIFTY options).
    Specification as per Strategy_OI_Buildup_0918_for_LLM.md.
    
    Timeline:
    - Before 09:00: Settings freeze
    - 09:00 to 09:14:55: Dynamic 8 ITM strikes calculation (4 CE ITM below spot, 4 PE ITM above spot)
    - 09:14:55: Lock the 8 contracts
    - 09:15:00: Take snapshot of the 8 contracts (LTP, OI, bid, ask). No trading.
    - 09:15 to 09:18: Data collection only. NO ENTRY.
    - From 09:18:00: Wait for fresh OI on both sides, evaluate 16-condition table and execute trade.
    - 14:45:00: Cutoff for new entries.
    - 15:15:00: Clock square-off at live bid.
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

        # Engine States:
        # SETUP -> PLANNING -> LOCKED -> DATA_COLLECTION -> ARMED -> ORDER_SENT -> HOLDING -> EXITING -> CLOSED / NO_TRADE / HALTED
        self.state: str = "SETUP"
        self.enabled: bool = True
        self.mode: str = "paper"  # "paper" or "live"

        # Lot Configurations (Section 6.3 & 13)
        self.lots: int = 1
        self.lot_size: int = DEFAULT_LOT_SIZE
        self.lots_full: int = 3    # ⭐⭐⭐ full lots
        self.lots_normal: int = 2  # ⭐⭐ normal lots
        self.lots_scalp: int = 1   # ⭐⭐ quick scalp
        self.max_trades_per_day: int = 1  # 1 trade per day limit (Section 6.1)
        self.today_trades_count: int = 0

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

        # Thresholds (Section 3 & 13)
        self.price_threshold_pct: float = 0.5   # 0.5 % price threshold for UP/DOWN
        self.oi_threshold_pct: float = 0.1      # 0.1 % OI threshold for UP/DOWN
        self.re_eval_min_interval_sec: float = 5.0  # re-evaluate at most once every 5 seconds
        self._last_eval_time: float = 0.0

        # Execution parameters
        self.order_buffer: float = 0.50
        self.max_spread_pct: float = 3.0
        self.max_quote_wait_ms: int = 100
        self.order_wait_sec: float = 2.0
        self.order_retries: int = 1

        # Schedule timings (Section 2)
        self.settings_freeze_time: str = "09:00:00"
        self.planning_start_time: str = "09:00:00"
        self.lock_time: str = "09:14:55"
        self.snapshot_time: str = "09:15:00"
        self.entry_start_time: str = "09:18:00"
        self.entry_end_time: str = "14:45:00"
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

        # Hot in-memory Cache for locked contracts
        self.hot_cache: dict[str, dict[str, Any]] = {}

        # Snapshot taken at 09:15:00 (Section 3)
        self.snapshot: dict[str, dict[str, Any]] = {}
        self.snapshot_taken: bool = False
        self.snapshot_at_ms: int | None = None

        # Strategy Evaluation State
        self.last_evaluation: dict[str, Any] = {
            "decision": "WAIT",
            "reason": "Before 09:18:00 evaluation window",
            "delta_ce": 0.0,
            "delta_pe": 0.0,
            "ce_price_pct": 0.0,
            "pe_price_pct": 0.0,
            "ce_price_dir": "FLAT",
            "ce_oi_dir": "FLAT",
            "pe_price_dir": "FLAT",
            "pe_oi_dir": "FLAT",
            "ce_type": None,
            "pe_type": None,
            "rule": None,
            "stars": "",
            "size": "",
            "summary_line": "WAIT: Pre-09:18 data collection only",
        }

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
                        if "lots_full" in data:
                            self.lots_full = max(1, int(data["lots_full"]))
                        if "lots_normal" in data:
                            self.lots_normal = max(1, int(data["lots_normal"]))
                        if "lots_scalp" in data:
                            self.lots_scalp = max(1, int(data["lots_scalp"]))
                        if NIFTY_KEY in self.index_lots:
                            self.lots = self.index_lots[NIFTY_KEY]
                LOGGER.info("Loaded index config from %s: lots=%s, enabled=%s", self.lots_file, self.index_lots, self.index_enabled)
            except Exception as exc:
                LOGGER.warning("Could not load index_lots.json: %s", exc)

    def _save_index_lots(self) -> None:
        if hasattr(self, "lots_file"):
            try:
                payload = {
                    "index_lots": self.index_lots,
                    "index_enabled": self.index_enabled,
                    "lots_full": self.lots_full,
                    "lots_normal": self.lots_normal,
                    "lots_scalp": self.lots_scalp,
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
                        "Trade ID", "Trade Date", "Side", "Symbol", "Strike", "Lots", "Lot Size",
                        "Entry Time (IST)", "Entry Price",
                        "Exit Time (IST)", "Exit Price", "Exit Reason",
                        "P&L Points", "Net P&L (INR)", "Status",
                        "Rule Number", "Stars", "Delta CE", "Delta PE",
                        "CE Build-up", "PE Build-up", "Decision",
                        "Bot Latency (ms)", "Network Latency (ms)",
                    ])
        except Exception as exc:
            LOGGER.error("Failed to init paper_trades.csv: %s", exc)

    def _resume_open_position(self) -> None:
        try:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trades = self.database.get_strategy_trades(limit=50)
            if trades:
                self._trade_counter = len(trades) + 1
                today_trades = [t for t in trades if t.get("trade_date") == today_str]
                self.today_trades_count = len(today_trades)
            for t in trades:
                if t.get("status") == "OPEN":
                    trade_date = t.get("trade_date")
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
                LOGGER.info("15:15:00 EOD Timer fired — squaring off open position by clock.")
                self.manual_exit(reason="SQUAREOFF_1515")

    # -------------------------------------------------------------------------
    # Strike Loading & Planning (09:00 to 09:14:55)
    # -------------------------------------------------------------------------

    def load_contracts_if_needed(self, underlying_key: str = NIFTY_KEY) -> bool:
        """Fetch option contracts for index and isolate nearest expiry."""
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
        Section 3:
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
    # Timeline Step 1: Lock Time (09:14:55)
    # -------------------------------------------------------------------------

    def lock_contracts(self) -> None:
        """Lock the 8 ITM contracts at 09:14:55."""
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
                if entry.get("ltp") is not None and float(entry["ltp"]) > 0:
                    entry["ref_ltp"] = float(entry["ltp"])
                    entry["ref_source"] = "ltp"
                else:
                    contract = self.contracts_by_key.get(k, {})
                    prev = float(contract.get("prev_close") or entry.get("bid_price") or 0.0)
                    entry["ref_ltp"] = prev
                    entry["ref_source"] = "prev_close"

            self.state = "LOCKED"
            LOGGER.info("LOCKED 8 contracts at 09:14:55: %s", self.locked_keys)

    # -------------------------------------------------------------------------
    # Timeline Step 2: 09:15:00 Snapshot (Section 2 & 3)
    # -------------------------------------------------------------------------

    def take_snapshot(self) -> None:
        """
        At 09:15:00, take the snapshot of the 8 contracts: LTP, OI, bid, ask.
        NO TRADING IS ALLOWED AT 09:15:00.
        """
        with self._lock:
            if not self.locked_keys and self.spot_price:
                self.lock_contracts()

            self.snapshot.clear()
            for k in self.locked_keys:
                c = self.hot_cache.get(k, {})
                ltp = float(c.get("ltp") or c.get("prev_close") or 100.0)
                oi = float(c.get("oi") or c.get("prev_oi") or 0.0)
                bid = float(c.get("bid_price") or 0.0)
                ask = float(c.get("ask_price") or 0.0)
                self.snapshot[k] = {
                    "instrument_key": k,
                    "trading_symbol": c.get("trading_symbol") or k,
                    "strike": float(c.get("strike") or 0.0),
                    "side": str(c.get("side") or "").upper(),
                    "ltp": ltp,
                    "oi": oi,
                    "bid": bid,
                    "ask": ask,
                }
            self.snapshot_taken = True
            self.snapshot_at_ms = int(time.time() * 1000)
            self.state = "DATA_COLLECTION"
            LOGGER.info("SNAPSHOT TAKEN at 09:15:00 for %d contracts. State -> DATA_COLLECTION (No entry before 09:18).", len(self.snapshot))

    # -------------------------------------------------------------------------
    # Timeline Step 3: From 09:18:00 OI Build-Up Evaluation & 16-Condition Table
    # -------------------------------------------------------------------------

    def evaluate_oi_buildup(self, trigger_ms: int = 0, force: bool = False) -> dict[str, Any] | None:
        """
        Evaluates the 16-condition table from 09:18:00 onwards.
        - Checks for fresh OI on both CE and PE sides.
        - Calculates Delta CE, Delta PE, price change %, OI change %.
        - Assigns direction (UP, DOWN, FLAT) based on thresholds.
        - If any direction is FLAT, returns WAIT.
        - Determines CE build-up (LB, SB, SC, LU) and PE build-up (LB, SB, SC, LU).
        - Looks up the 16-condition table.
        - If CE Buy or PE Buy and within trading window (09:18 to 14:45), enters trade.
        """
        with self._lock:
            now_dt = datetime.now(IST)
            now_time_hms = now_dt.strftime("%H:%M:%S")

            # 1. Trading Window Validation (Section 1 & Section 5)
            if not force and now_time_hms < self.entry_start_time:
                res = {
                    "decision": "WAIT",
                    "reason": f"No entry before {self.entry_start_time} (Current: {now_time_hms})",
                    "summary_line": f"WAIT: Pre-09:18 data collection only (Time: {now_time_hms})",
                }
                self.last_evaluation = res
                return None

            if not force and now_time_hms >= self.entry_end_time:
                res = {
                    "decision": "WAIT",
                    "reason": f"No entry after {self.entry_end_time} (Current: {now_time_hms})",
                    "summary_line": f"WAIT: Market entry closed after 14:45 (Time: {now_time_hms})",
                }
                self.last_evaluation = res
                return None

            # 2. Daily limits and existing position
            if self.current_position is not None:
                return None
            if self.today_trades_count >= self.max_trades_per_day:
                return None

            # 3. Snapshot validation
            if not self.snapshot:
                # If after 09:15:00 and no snapshot yet, auto-take snapshot
                if self.locked_keys or (self.spot_price and self.plan_8_itm_contracts(self.spot_price)):
                    self.take_snapshot()
                else:
                    return None

            # 4. Fresh OI Check (Section 5)
            # At or after 09:18:00, evaluate when OI is fresh:
            # at least one locked contract on each side has an OI value different from its snapshot value.
            ce_fresh = False
            pe_fresh = False
            for k in self.locked_keys:
                snap = self.snapshot.get(k)
                curr = self.hot_cache.get(k)
                if not snap or not curr:
                    continue
                side = curr.get("side")
                c_oi = curr.get("oi")
                s_oi = snap.get("oi")
                if c_oi is not None and s_oi is not None and c_oi != s_oi:
                    if side == "CE":
                        ce_fresh = True
                    elif side == "PE":
                        pe_fresh = True

            if not force and not (ce_fresh and pe_fresh):
                res = {
                    "decision": "WAIT",
                    "reason": "Waiting for fresh OI update on both CE and PE sides",
                    "summary_line": f"WAIT: Awaiting fresh OI on both sides (CE fresh: {ce_fresh}, PE fresh: {pe_fresh})",
                }
                self.last_evaluation = res
                return None

            # 5. Throttling (Section 5: at most once every 5 seconds)
            cur_t = time.time()
            if not force and (cur_t - self._last_eval_time < self.re_eval_min_interval_sec):
                return None
            self._last_eval_time = cur_t

            # 6. Separate CE and PE locked contracts
            ce_keys = [k for k in self.locked_keys if self.hot_cache.get(k, {}).get("side") == "CE"]
            pe_keys = [k for k in self.locked_keys if self.hot_cache.get(k, {}).get("side") == "PE"]

            if not ce_keys or not pe_keys:
                return None

            # 7. Mathematical Aggregations (Section 3)
            # CE side
            ce_oi_snap_sum = sum(float(self.snapshot[k]["oi"]) for k in ce_keys if k in self.snapshot)
            ce_oi_curr_sum = sum(float(self.hot_cache[k].get("oi") or self.snapshot.get(k, {}).get("oi") or 0.0) for k in ce_keys)
            delta_ce = ce_oi_curr_sum - ce_oi_snap_sum
            ce_oi_pct = (delta_ce / ce_oi_snap_sum * 100.0) if ce_oi_snap_sum > 0 else 0.0

            ce_ltp_snap_sum = sum(float(self.snapshot[k]["ltp"]) for k in ce_keys if k in self.snapshot)
            ce_ltp_curr_sum = sum(float(self.hot_cache[k].get("ltp") or self.snapshot.get(k, {}).get("ltp") or 0.0) for k in ce_keys)
            ce_price_delta = ce_ltp_curr_sum - ce_ltp_snap_sum
            ce_price_pct = (ce_price_delta / ce_ltp_snap_sum * 100.0) if ce_ltp_snap_sum > 0 else 0.0

            # PE side
            pe_oi_snap_sum = sum(float(self.snapshot[k]["oi"]) for k in pe_keys if k in self.snapshot)
            pe_oi_curr_sum = sum(float(self.hot_cache[k].get("oi") or self.snapshot.get(k, {}).get("oi") or 0.0) for k in pe_keys)
            delta_pe = pe_oi_curr_sum - pe_oi_snap_sum
            pe_oi_pct = (delta_pe / pe_oi_snap_sum * 100.0) if pe_oi_snap_sum > 0 else 0.0

            pe_ltp_snap_sum = sum(float(self.snapshot[k]["ltp"]) for k in pe_keys if k in self.snapshot)
            pe_ltp_curr_sum = sum(float(self.hot_cache[k].get("ltp") or self.snapshot.get(k, {}).get("ltp") or 0.0) for k in pe_keys)
            pe_price_delta = pe_ltp_curr_sum - pe_ltp_snap_sum
            pe_price_pct = (pe_price_delta / pe_ltp_snap_sum * 100.0) if pe_ltp_snap_sum > 0 else 0.0

            # 8. Direction Evaluation (UP, DOWN, FLAT) (Section 3)
            def _get_dir(pct_val: float, threshold: float) -> str:
                if pct_val > threshold:
                    return "UP"
                elif pct_val < -threshold:
                    return "DOWN"
                return "FLAT"

            ce_price_dir = _get_dir(ce_price_pct, self.price_threshold_pct)
            ce_oi_dir = _get_dir(ce_oi_pct, self.oi_threshold_pct)
            pe_price_dir = _get_dir(pe_price_pct, self.price_threshold_pct)
            pe_oi_dir = _get_dir(pe_oi_pct, self.oi_threshold_pct)

            # 9. FLAT Direction Check (Section 3: If any direction is FLAT -> WAIT)
            if "FLAT" in (ce_price_dir, ce_oi_dir, pe_price_dir, pe_oi_dir):
                eval_res = {
                    "decision": "WAIT",
                    "reason": f"FLAT direction detected (CE_P: {ce_price_dir}, CE_OI: {ce_oi_dir}, PE_P: {pe_price_dir}, PE_OI: {pe_oi_dir})",
                    "delta_ce": delta_ce,
                    "delta_pe": delta_pe,
                    "ce_price_pct": round(ce_price_pct, 2),
                    "pe_price_pct": round(pe_price_pct, 2),
                    "ce_price_dir": ce_price_dir,
                    "ce_oi_dir": ce_oi_dir,
                    "pe_price_dir": pe_price_dir,
                    "pe_oi_dir": pe_oi_dir,
                    "ce_type": None,
                    "pe_type": None,
                    "rule": None,
                    "stars": "",
                    "size": "",
                    "summary_line": f"ΔCE: {delta_ce:+,.0f} | ΔPE: {delta_pe:+,.0f} | WAIT (FLAT direction)",
                }
                self.last_evaluation = eval_res
                return None

            # 10. Build-up Type Assignment (Section 3)
            def _get_buildup(p_dir: str, oi_dir: str) -> str:
                if p_dir == "UP" and oi_dir == "UP": return "LB"
                if p_dir == "DOWN" and oi_dir == "UP": return "SB"
                if p_dir == "UP" and oi_dir == "DOWN": return "SC"
                if p_dir == "DOWN" and oi_dir == "DOWN": return "LU"
                return "Neutral"

            ce_type = _get_buildup(ce_price_dir, ce_oi_dir)
            pe_type = _get_buildup(pe_price_dir, pe_oi_dir)

            # 11. Table Lookup (Section 4)
            table_entry = DECISION_TABLE.get((ce_type, pe_type), {
                "rule": None,
                "decision": "No trade",
                "side": None,
                "stars": "",
                "size": "",
            })

            decision = table_entry["decision"]
            side = table_entry.get("side")
            stars = table_entry.get("stars", "")
            size = table_entry.get("size", "")
            rule_num = table_entry.get("rule")

            summary_line = f"ΔCE: {delta_ce:+,.0f} | ΔPE: {delta_pe:+,.0f} | CE: {ce_type} | PE: {pe_type} | Rule #{rule_num} {stars} -> {decision}"

            eval_res = {
                "decision": decision,
                "reason": "Evaluated 16-condition table",
                "delta_ce": delta_ce,
                "delta_pe": delta_pe,
                "ce_price_pct": round(ce_price_pct, 2),
                "pe_price_pct": round(pe_price_pct, 2),
                "ce_price_dir": ce_price_dir,
                "ce_oi_dir": ce_oi_dir,
                "pe_price_dir": pe_price_dir,
                "pe_oi_dir": pe_oi_dir,
                "ce_type": ce_type,
                "pe_type": pe_type,
                "rule": rule_num,
                "stars": stars,
                "size": size,
                "summary_line": summary_line,
            }
            self.last_evaluation = eval_res

            # 12. Trade Trigger (Section 6)
            if decision in ("CE Buy", "PE Buy") and side:
                underlying_key = NIFTY_KEY
                if not self.index_enabled.get(underlying_key, True):
                    LOGGER.info("Decision %s triggered, but index %s is paused today.", decision, underlying_key)
                    return None

                winner = self._select_best_strike(side)
                if not winner:
                    LOGGER.warning("Could not find valid candidate quote for %s side.", side)
                    return None

                trigger_ts_ms = trigger_ms or int(time.time() * 1000)
                trade = self._execute_entry_order(
                    winner=winner,
                    side=side,
                    size=size,
                    rule_num=rule_num,
                    stars=stars,
                    delta_ce=delta_ce,
                    delta_pe=delta_pe,
                    ce_type=ce_type,
                    pe_type=pe_type,
                    time_str=now_dt.strftime("%H:%M:%S.%f")[:-3],
                    trigger_ms=trigger_ts_ms,
                )
                return trade

            return None

    # -------------------------------------------------------------------------
    # Ingesting Real-Time Market Ticks
    # -------------------------------------------------------------------------

    def on_tick(self, tick: dict[str, Any]) -> dict[str, Any] | None:
        """
        Ingests every incoming tick from WebSocket.
        1. If spot index tick: updates spot, auto-plans, checks schedule clocks.
        2. If locked contract tick: updates hot cache, re-evaluates OI build-up table.
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
                # 09:00 to 09:14:55: Dynamic planning around spot
                if self.state in ("SETUP", "PLANNING"):
                    if now_time_hms < self.lock_time:
                        self.state = "PLANNING"
                        self.plan_8_itm_contracts(ltp)
                    elif now_time_hms >= self.lock_time and now_time_hms < self.snapshot_time:
                        if self.state != "LOCKED":
                            self.lock_contracts()
                    elif now_time_hms >= self.snapshot_time and now_time_hms < self.entry_start_time:
                        if not self.snapshot_taken:
                            self.take_snapshot()

                # Schedule checks
                if now_time_hms >= self.snapshot_time and not self.snapshot_taken:
                    self.take_snapshot()

                if now_time_hms >= self.entry_start_time and self.state == "DATA_COLLECTION":
                    self.state = "ARMED"
                    LOGGER.info("09:18:00 REACHED: State -> ARMED (Evaluating OI build-up table).")

            return None

        # --- 2. OPTION CONTRACT TICK ---
        with self._lock:
            entry = self.hot_cache.get(inst_key)
            oi_changed = False
            if entry is not None:
                if ltp > 0:
                    entry["ltp"] = ltp
                    entry["last_trade_ms"] = ts_ms

                if tick.get("open_interest") is not None:
                    new_oi = float(tick["open_interest"])
                    if entry.get("oi") != new_oi:
                        oi_changed = True
                    entry["oi"] = new_oi

                if tick.get("prev_close") is not None and not entry.get("prev_close"):
                    entry["prev_close"] = float(tick["prev_close"])

                # Display-only single contract build-up
                pc = entry.get("prev_close") or entry.get("ref_ltp")
                oi_curr = entry.get("oi")
                oi_prev = entry.get("prev_oi")
                if ltp > 0 and pc and pc > 0 and oi_curr and oi_prev and oi_prev > 0:
                    dp = (ltp - pc) / pc
                    doi = (oi_curr - oi_prev) / oi_prev
                    thresh = 0.001
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

            # If holding, exited, or contract not in locked set: nothing to trigger
            if self.current_position is not None or self.today_trades_count >= self.max_trades_per_day:
                return None
            if inst_key not in self.locked_keys:
                return None

            # Check if this index is paused today
            u_key = entry.get("underlying_key") if entry else NIFTY_KEY
            if not self.index_enabled.get(u_key, True):
                return None

            # Re-evaluate OI build-up table when OI updates or between 09:18 and 14:45
            if now_time_hms >= self.entry_start_time and now_time_hms < self.entry_end_time:
                trade = self.evaluate_oi_buildup(trigger_ms=ts_ms)
                return trade

            return None

    # -------------------------------------------------------------------------
    # Strike Selection (Tightest Spread among 4 ITM candidates of chosen side)
    # -------------------------------------------------------------------------

    def _select_best_strike(self, side: str) -> dict[str, Any] | None:
        """Section 6.2: Among the 4 locked ITM contracts on chosen side, pick tightest spread %."""
        candidates = [
            e for e in self.hot_cache.values()
            if e.get("instrument_key") in self.locked_keys and e.get("side") == side
        ]

        valid: list[dict[str, Any]] = []
        for c in candidates:
            bid = c.get("bid_price") or 0.0
            ask = c.get("ask_price") or 0.0
            if bid > 0 and ask > 0 and ask >= bid:
                spread = (ask - bid) / ask * 100
                if spread <= self.max_spread_pct:
                    c_copy = dict(c)
                    c_copy["calc_spread"] = spread
                    valid.append(c_copy)

        if not valid:
            # Fallback to candidates with positive LTP
            fallback = [c for c in candidates if (c.get("ltp") or 0.0) > 0]
            return fallback[0] if fallback else (candidates[0] if candidates else None)

        # Sort ascending by spread %, tie-break by larger ask quantity, then lower ask price
        valid.sort(key=lambda x: (x["calc_spread"], -x.get("ask_qty", 0.0), x["ask_price"]))
        return valid[0]

    # -------------------------------------------------------------------------
    # Order Execution (Paper Mode / Simulation - Section 6 & 8)
    # -------------------------------------------------------------------------

    def _execute_entry_order(
        self,
        winner: dict[str, Any],
        side: str,
        size: str,
        rule_num: int | None,
        stars: str,
        delta_ce: float,
        delta_pe: float,
        ce_type: str,
        pe_type: str,
        time_str: str,
        trigger_ms: int,
    ) -> dict[str, Any]:
        with self._lock:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trade_id = f"OIBU-{datetime.now(IST).strftime('%m%d%H%M%S')}-{self._trade_counter}"
            self._trade_counter += 1
            self.today_trades_count += 1

            underlying_key = winner.get("underlying_key") or NIFTY_KEY
            lot_sz = self.index_lot_sizes.get(underlying_key, self.lot_size)

            # Sizing as per stars/size (Section 6.3)
            if size == "full":
                lots = self.lots_full
            elif size == "normal":
                lots = self.lots_normal
            elif size == "scalp":
                lots = self.lots_scalp
            else:
                lots = self.index_lots.get(underlying_key, self.lots)

            total_qty = lots * lot_sz

            # Section 8: Paper entry fills at live ask!
            ask = winner.get("ask_price") or winner.get("ltp") or 100.0
            limit_price = round(round((ask + self.order_buffer) / 0.05) * 0.05, 2)
            entry_price = limit_price if self.mode == "live" else ask

            order_side = f"BUY_{side}"
            symbol = winner.get("trading_symbol") or f"NIFTY {winner.get('strike')} {side}"
            inst_key = winner["instrument_key"]
            strike = winner["strike"]
            bu_label = f"Rule #{rule_num} {stars} | CE:{ce_type} PE:{pe_type}"

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
                "rule_number": rule_num,
                "stars": stars,
                "size_category": size,
                "delta_ce": delta_ce,
                "delta_pe": delta_pe,
                "ce_buildup": ce_type,
                "pe_buildup": pe_type,
                "decision": f"{side} Buy",
                "bot_latency_ms": 0.25,
                "network_latency_ms": 0.0,
            }

            self.current_position = trade
            self.current_positions[NIFTY_KEY] = trade
            self.state = "HOLDING"

            self.last_decision = {
                "timestamp_ist": time_str,
                "side": side,
                "chosen_strike": strike,
                "symbol": symbol,
                "entry_price": entry_price,
                "spread_pct": winner.get("spread_pct"),
                "build_up": bu_label,
                "rule": rule_num,
                "stars": stars,
                "size": size,
                "delta_ce": delta_ce,
                "delta_pe": delta_pe,
                "reason": f"OI_BUILDUP_RULE_{rule_num}",
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
                    "oi_change": delta_ce if side == "CE" else delta_pe,
                    "confidence": f"{stars} Rule #{rule_num}",
                    "action_taken": f"ENTERED_{order_side}",
                })
            except Exception as exc:
                LOGGER.error("Failed to insert trade into DB: %s", exc)

            # Append to paper_trades.csv
            try:
                with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        trade_id, today_str, order_side, symbol, strike, lots, total_qty,
                        time_str, entry_price,
                        "", "", "", "", "", "OPEN",
                        rule_num, stars, delta_ce, delta_pe,
                        ce_type, pe_type, f"{side} Buy",
                        0.25, 0.0,
                    ])
            except Exception as exc:
                LOGGER.error("Failed to append paper trade to CSV: %s", exc)

            LOGGER.info(
                "ENTRY EXECUTED: %s %s Qty %s @ %.2f (Rule #%s %s %s lots, ΔCE: %+,.0f, ΔPE: %+,.0f)",
                order_side,
                symbol,
                total_qty,
                entry_price,
                rule_num,
                stars,
                size,
                delta_ce,
                delta_pe,
            )
            return trade

    # -------------------------------------------------------------------------
    # Exit Management (Section 7 & 8: Paper Exit fills at live bid)
    # -------------------------------------------------------------------------

    def manual_exit(self, reason: str = "MANUAL") -> dict[str, Any] | None:
        """Manual Exit Now button or 15:15:00 EOD square-off at live bid."""
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

            # Section 8: Exit fills at live bid! Never use entry price as exit price.
            if bid > 0:
                exit_price = bid
            elif ltp > 0:
                exit_price = ltp
            else:
                exit_price = float(pos["entry_price"]) - 0.50  # Fallback ensuring bid-side deduction

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

    def emergency_stop(self) -> dict[str, Any]:
        """Emergency stop: exits open position and disables further entries."""
        with self._lock:
            self.enabled = False
            self.state = "HALTED"
            if self.current_position:
                self.manual_exit(reason="EMERGENCY_STOP")
            LOGGER.warning("EMERGENCY STOP ACTIVATED: Bot halted.")
            return self.get_status()

    # -------------------------------------------------------------------------
    # Rehearsal & Testing Utilities
    # -------------------------------------------------------------------------

    def force_arm_now(self) -> dict[str, Any]:
        """Test trigger: Forces immediate snapshot and evaluation."""
        with self._lock:
            spot = self.spot_price or 24000.0
            self.load_contracts_if_needed()
            self.plan_8_itm_contracts(spot)
            self.lock_contracts()
            self.take_snapshot()
            self.state = "ARMED"
            trade = self.evaluate_oi_buildup(force=True)
            LOGGER.info("FORCE EVALUATION COMPLETED: Trade=%s", trade)
            return self.get_status()

    def reset_state(self) -> dict[str, Any]:
        """Reset bot back to PLANNING for repeated testing."""
        with self._lock:
            self.state = "PLANNING"
            self.locked_keys.clear()
            self.snapshot.clear()
            self.snapshot_taken = False
            self.last_decision = None
            self.today_trades_count = 0
            self.last_evaluation = {
                "decision": "WAIT",
                "reason": "Bot reset to PLANNING",
                "summary_line": "WAIT: Bot reset to PLANNING",
            }
            LOGGER.info("Bot state reset to PLANNING.")
            return self.get_status()

    def rehearse_entry(self) -> dict[str, Any]:
        """Audit rehearsal for 16-condition evaluation."""
        with self._lock:
            t_start = time.perf_counter()
            eval_res = self.last_evaluation
            t_ms = round((time.perf_counter() - t_start) * 1000, 3)
            return {
                "rehearsal": True,
                "strategy": "OI Build-up Entry from 09:18",
                "latency_ms": t_ms,
                "evaluation": eval_res,
                "summary": eval_res.get("summary_line", ""),
            }

    # -------------------------------------------------------------------------
    # Status & Configuration API
    # -------------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        return {
            "strategy": "OI Build-up Entry from 09:18",
            "enabled": self.enabled,
            "mode": self.mode,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "lots_full": self.lots_full,
            "lots_normal": self.lots_normal,
            "lots_scalp": self.lots_scalp,
            "price_threshold_pct": self.price_threshold_pct,
            "oi_threshold_pct": self.oi_threshold_pct,
            "max_trades_per_day": self.max_trades_per_day,
            "index_lots": self.index_lots,
            "index_lot_sizes": self.index_lot_sizes,
            "index_enabled": self.index_enabled,
            "order_buffer": self.order_buffer,
            "max_spread_pct": self.max_spread_pct,
            "settings_freeze_time": self.settings_freeze_time,
            "lock_time": self.lock_time,
            "snapshot_time": self.snapshot_time,
            "entry_start_time": self.entry_start_time,
            "entry_end_time": self.entry_end_time,
            "squareoff_time": self.squareoff_time,
        }

    def update_config(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "lots_full" in body:
                self.lots_full = max(1, int(body["lots_full"]))
            if "lots_normal" in body:
                self.lots_normal = max(1, int(body["lots_normal"]))
            if "lots_scalp" in body:
                self.lots_scalp = max(1, int(body["lots_scalp"]))
            if "price_threshold_pct" in body:
                self.price_threshold_pct = float(body["price_threshold_pct"])
            if "oi_threshold_pct" in body:
                self.oi_threshold_pct = float(body["oi_threshold_pct"])
            if "max_trades_per_day" in body:
                self.max_trades_per_day = max(1, int(body["max_trades_per_day"]))
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
            LOGGER.info("Updated Strategy config: %s", self.get_config())
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

            # Build locked contracts list for dashboard UI (Section 10)
            locked_table = []
            keys_to_show = self.locked_keys or self.planned_keys
            for k in keys_to_show:
                c = self.hot_cache.get(k)
                if c:
                    snap_c = self.snapshot.get(k, {})
                    locked_table.append({
                        "instrument_key": k,
                        "symbol": c.get("trading_symbol"),
                        "strike": c.get("strike"),
                        "side": c.get("side"),
                        "snap_ltp": snap_c.get("ltp"),
                        "snap_oi": snap_c.get("oi"),
                        "ltp": c.get("ltp"),
                        "oi": c.get("oi"),
                        "bid_price": c.get("bid_price"),
                        "ask_price": c.get("ask_price"),
                        "spread_pct": c.get("spread_pct"),
                        "build_up": c.get("build_up", "Neutral"),
                        "build_up_class": c.get("build_up_class", "bu-neutral"),
                    })

            eval_res = self.last_evaluation or {}

            return {
                "strategy_name": "OI Build-up Entry from 09:18 (NIFTY 50)",
                "state": self.state,
                "enabled": self.enabled,
                "mode": self.mode,
                "spot_price": self.spot_price,
                "active_expiry": self.active_expiry,
                "lots": self.lots,
                "lot_size": self.lot_size,
                "lots_full": self.lots_full,
                "lots_normal": self.lots_normal,
                "lots_scalp": self.lots_scalp,
                "index_lots": self.index_lots,
                "index_lot_sizes": self.index_lot_sizes,
                "index_enabled": self.index_enabled,
                "snapshot_taken": self.snapshot_taken,
                "snapshot_time": self.snapshot_time,
                "entry_start_time": self.entry_start_time,
                "delta_ce": eval_res.get("delta_ce", 0.0),
                "delta_pe": eval_res.get("delta_pe", 0.0),
                "ce_buildup": eval_res.get("ce_type"),
                "pe_buildup": eval_res.get("pe_type"),
                "rule_number": eval_res.get("rule"),
                "stars": eval_res.get("stars", ""),
                "decision": eval_res.get("decision", "WAIT"),
                "summary_line": eval_res.get("summary_line", "WAIT: Initializing"),
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


# =========================================================================
# PREVIOUS FIRST POSITIVE TICK STRATEGY (COMMENTED OUT AS PER CLIENT REQUEST)
# =========================================================================
"""
class LegacyGapEntryStrategyEngine:
    '''
    PREVIOUS STRATEGY: Opening Gap Entry Bot (NIFTY 50) First Positive Tick at 09:15:00.
    Replaced by OI Build-up Entry from 09:18 as per Strategy_OI_Buildup_0918_for_LLM.md.
    Preserved here in comments as requested.
    '''
    # def __init__(self, database, base_dir, feed=None, access_token=""):
    #     self.state = "SETUP"
    #     self.lock_time = "09:14:55"
    #     self.market_open_time = "09:15:00"
    #     self.entry_window_sec = 60
    #     self.locked_keys = []
    #
    # def on_tick(self, tick):
    #     # Rule R8: POSITIVE TICK EVALUATION at 09:15:00
    #     # if tick LTP > ref_ltp (stored at 09:14:55):
    #     #     chosen_side = entry['side']  # CE or PE
    #     #     self.state = 'ORDER_SENT'
    #     #     winner = self._select_best_strike(chosen_side)
    #     #     trade = self._execute_entry_order(winner, chosen_side, ...)
    #     #     return trade
    #     pass
    #
    # def arm_bot(self):
    #     # Rule 9.3: At 09:15:00, transition to ARMED.
    #     # self.state = "ARMED"
    #     pass
"""
