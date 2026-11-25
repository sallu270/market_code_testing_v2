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
from stock_screener import StockScreener, NIFTY_50_STOCKS

LOGGER = logging.getLogger("strategy_engine")
IST = ZoneInfo("Asia/Kolkata")

# Index Constants
NIFTY_KEY = "NSE_INDEX|Nifty 50"
BANKNIFTY_KEY = "NSE_INDEX|Nifty Bank"
SENSEX_KEY = "BSE_INDEX|SENSEX"
NIFTY_STEP = 50.0
DEFAULT_LOT_SIZE = 65

INDEX_CONFIG: dict[str, dict[str, Any]] = {
    NIFTY_KEY: {"name": "NIFTY 50", "step": 50.0, "lot_size": 65, "symbol_prefix": "NIFTY", "futures_key": "NSE_FO|61471"},
    BANKNIFTY_KEY: {"name": "BANK NIFTY", "step": 100.0, "lot_size": 15, "symbol_prefix": "BANKNIFTY", "futures_key": "NSE_FO|61466"},
    SENSEX_KEY: {"name": "SENSEX", "step": 100.0, "lot_size": 10, "symbol_prefix": "SENSEX", "futures_key": ""},
}

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
    Opening Gap Entry Bot - First Positive Tick Strategy (NIFTY 50, BANK NIFTY, SENSEX).
    Rules & Data Flow as per Gap_Entry_Strategy_Rules_and_Architecture.md v2.1.
    
    Active Strategy (Indices):
    - 09:00 to 09:14:55: Dynamic 8 ITM strikes calculation (4 CE ITM below spot, 4 PE ITM above spot)
    - 09:14:55: Lock the 8 contracts and store ref_ltp for each contract (fixed for the day)
    - 09:15:00: Market open -> State transitions to ARMED
    - From 09:15:00: First positive tick (LTP > ref_ltp) triggers immediate entry order
      on the winning ITM strike with tightest bid-ask spread
    - Exit: Manual exit, or automatic square-off at 15:15:00
    
    Preserved Strategy:
    - The 09:18 OI Build-up 16-condition table strategy is preserved in comments/backup as requested.
    - Constituent stock options evaluate via the 09:18 OI buildup table.
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

        # Lot Configurations (Phase 2 & 3: Configurable lots per rank, default 1)
        self.lots: int = 1
        self.lot_size: int = DEFAULT_LOT_SIZE
        self.lots_full: int = 1    # ⭐⭐⭐ full lots (user-configurable, default 1)
        self.lots_normal: int = 1  # ⭐⭐ normal lots (user-configurable, default 1)
        self.lots_scalp: int = 1   # ⭐⭐ quick scalp (user-configurable, default 1)
        self.max_trades_per_day: int = 3  # Phase 3: Limit raised from 1 to 3 trades per index per day
        self.daily_loss_limit_inr: float = 10000.0  # Phase 3: Daily loss limit per index
        self.stock_max_trades_per_day: int = 3  # Max 3 trades per stock per day
        self.max_bullish_stocks: int = 5  # Pre-defined count of bullish stocks to trade today
        self.max_bearish_stocks: int = 5  # Pre-defined count of bearish stocks to trade today
        self.today_trades_count: int = 0

        # Phase 3: Live Alerts system
        self.alerts: list[dict[str, Any]] = []
        self._alerts_lock = threading.Lock()
        self._alert_flags: set[str] = set()
        self.last_tick_time: float = time.time()
        self._active_stock_option_keys: set[str] = set()

        # Priority 1: Stock order pacing (50ms interval = max 20 orders/sec to prevent broker HTTP 429)
        # Priority 0: Indices bypass pacing completely with zero delay
        self._stock_pace_lock = threading.Lock()
        self._last_stock_order_time: float = 0.0
        self.stock_order_interval_sec: float = 0.05

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
        self.market_open_time: str = "09:15:00"
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
        self.current_positions: dict[str, dict[str, Any] | None] = {NIFTY_KEY: None, BANKNIFTY_KEY: None, SENSEX_KEY: None}
        self.last_decision: dict[str, Any] | None = None
        self._trade_counter: int = 1
        self._eod_timer: threading.Timer | None = None

        # Multi-Index Underlyings (NIFTY 50, BANK NIFTY, SENSEX)
        self.index_underlyings: dict[str, dict[str, Any]] = {}
        for ukey, cfg in INDEX_CONFIG.items():
            fut_k = cfg.get("futures_key", "")
            self.index_underlyings[ukey] = {
                "key": ukey,
                "name": cfg["name"],
                "step": cfg["step"],
                "lot_size": self.index_lot_sizes.get(ukey, cfg["lot_size"]),
                "symbol_prefix": cfg["symbol_prefix"],
                "futures_key": fut_k,
                "futures_ltp": None,
                "futures_oi": None,
                "futures_prev_close": None,
                "futures_snap_ltp": None,
                "futures_snap_oi": None,
                "futures_snap_vol": None,
                "futures_buildup": "Neutral",
                "open_price": None,
                "prev_close": None,
                "pre_open_error": False,
                "path1_done": False,
                "consecutive_signal": None,
                "consecutive_count": 0,
                "max_trades_per_day": self.max_trades_per_day,
                "today_trades_count": 0,
                "realized_pnl": 0.0,
                "daily_loss_limit_inr": self.daily_loss_limit_inr,
                "lots": self.index_lots.get(ukey, 1),
                "enabled": self.index_enabled.get(ukey, True),
                "spot_price": None,
                "state": "SETUP",
                "planned_keys": [],
                "locked_keys": [],
                "atm_plus_minus_keys": [],
                "snapshot": {},
                "snapshot_taken": False,
                "last_evaluation": {
                    "decision": "WAIT",
                    "reason": f"Before 09:18:00 evaluation window for {cfg['name']}",
                    "delta_ce": 0.0,
                    "delta_pe": 0.0,
                    "ce_type": None,
                    "pe_type": None,
                    "rule": None,
                    "stars": "",
                    "size": "",
                    "summary_line": f"{cfg['name']} WAIT: Pre-09:18",
                },
                "current_position": None,
                "_last_eval_time": 0.0,
                "ce_basket_keys": [],
                "pe_basket_keys": [],
                "baseline_prices": {},
                "ce_baseline_sum": 0.0,
                "pe_baseline_sum": 0.0,
                "baseline_locked": False,
                "ce_current_sum": 0.0,
                "pe_current_sum": 0.0,
                "ce_pct": 0.0,
                "pe_pct": 0.0,
                "score": 0.0,
                "raw_signal": "NEUTRAL",
                "candidate_signal": "NEUTRAL",
                "candidate_start_time": 0.0,
                "hold_elapsed_sec": 0.0,
                "confirmed_signal": "NEUTRAL",
                "cooldown_until": 0.0,
                "whipsaw_sec": 300.0,
            }

            # Initialize futures in hot_cache if available
            if fut_k and fut_k not in self.hot_cache:
                self.hot_cache[fut_k] = {
                    "instrument_key": fut_k,
                    "trading_symbol": f"{cfg['symbol_prefix']} FUT",
                    "underlying_key": ukey,
                    "side": "FUT",
                    "ltp": None,
                    "oi": None,
                    "prev_close": None,
                    "build_up": "Neutral",
                    "bid_price": 0.0,
                    "ask_price": 0.0,
                }

        # Stock Options Engines (Top N stocks from selected_stocks.json)
        self.stock_underlyings: dict[str, dict[str, Any]] = {}
        self.stock_screener = StockScreener(base_dir=base_dir, access_token=access_token)

        self._init_csv()
        self._load_index_lots()
        self.load_stock_configs()
        self._seed_index_spots_from_db()
        self._resume_open_position()
        self._schedule_eod_squareoff()

    def _seed_index_spots_from_db(self) -> None:
        if not hasattr(self, "database") or not self.database:
            return
        now_dt = datetime.now(IST)
        now_time_hms = now_dt.strftime("%H:%M:%S")

        for ukey, idx_st in self.index_underlyings.items():
            if idx_st.get("spot_price") is None:
                try:
                    ticks = self.database.latest_ticks(instrument_key=ukey, limit=1)
                    if ticks and float(ticks[0].get("ltp") or 0.0) > 0:
                        idx_st["spot_price"] = float(ticks[0]["ltp"])
                        if ukey == NIFTY_KEY and self.spot_price is None:
                            self.spot_price = idx_st["spot_price"]
                        LOGGER.info("Seeded spot price for %s from DB: %.2f", ukey, idx_st["spot_price"])
                except Exception as exc:
                    LOGGER.debug("Could not seed spot for %s: %s", ukey, exc)

        for sym, st in self.stock_underlyings.items():
            if st.get("spot_price") is None:
                key = st.get("key") or f"NSE_EQ|{sym}"
                try:
                    ticks = self.database.latest_ticks(instrument_key=key, limit=1)
                    if ticks and float(ticks[0].get("ltp") or 0.0) > 0:
                        st["spot_price"] = float(ticks[0]["ltp"])
                        LOGGER.info("Seeded spot price for stock %s from DB: %.2f", sym, st["spot_price"])
                    else:
                        meta = next((s for s in NIFTY_50_STOCKS if s["symbol"] == sym.upper()), None)
                        fallback_price = float(st.get("prev_close") or (meta.get("strike_step", 10) * 100 if meta else 1000.0))
                        st["spot_price"] = fallback_price
                        LOGGER.info("Seeded spot price for stock %s from baseline: %.2f", sym, fallback_price)
                except Exception as exc:
                    LOGGER.debug("Could not seed spot for stock %s: %s", sym, exc)

        # If restarted/initialized during trading hours (>= 09:18), auto-arm enabled instruments with spot
        if now_time_hms >= self.entry_start_time:
            for ukey, idx_st in self.index_underlyings.items():
                if idx_st.get("enabled", True) and idx_st.get("state") in ("SETUP", "PLANNING"):
                    spot = idx_st.get("spot_price")
                    if spot and spot > 0:
                        self.load_contracts_if_needed(ukey)
                        self.plan_8_itm_contracts(spot, underlying_key=ukey)
                        self.lock_contracts(underlying_key=ukey)
                        self.take_snapshot(underlying_key=ukey)
                        idx_st["state"] = "ARMED"
                        idx_st["path1_done"] = True
                        if ukey == NIFTY_KEY:
                            self.path1_done = True
                        LOGGER.info("Auto-armed index %s on startup at spot %.2f (Path 1 marked completed for mid-day start)", ukey, spot)

            for sym, st in self.stock_underlyings.items():
                if st.get("enabled", True) and st.get("state") in ("SETUP", "PLANNING"):
                    spot = st.get("spot_price")
                    if spot and spot > 0:
                        self.plan_stock_contracts(sym, spot)
                        self.lock_stock_contracts(sym)
                        self.take_stock_snapshot(sym)
                        st["state"] = "ARMED"
                        st["path1_done"] = True
                        LOGGER.info("Auto-armed stock %s on startup at spot %.2f (Path 1 marked completed for mid-day start)", sym, spot)

            # Pre-subscribe the 40 stock option contracts for Top 10 stocks if started during market hours before 15:15:00
            if now_time_hms < "15:15:00":
                try:
                    self.lock_all_stocks()
                except Exception as ex:
                    LOGGER.debug("Could not pre-subscribe stocks on mid-day start: %s", ex)

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
                        if "max_bullish_stocks" in data:
                            self.max_bullish_stocks = max(1, min(5, int(data["max_bullish_stocks"])))
                        if "max_bearish_stocks" in data:
                            self.max_bearish_stocks = max(1, min(5, int(data["max_bearish_stocks"])))
                        if NIFTY_KEY in self.index_lots:
                            self.lots = self.index_lots[NIFTY_KEY]
                        if hasattr(self, "index_underlyings"):
                            for ukey, idx_st in self.index_underlyings.items():
                                if ukey in self.index_lots:
                                    idx_st["lots"] = self.index_lots[ukey]
                                if ukey in self.index_enabled:
                                    idx_st["enabled"] = self.index_enabled[ukey]
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
                    "max_bullish_stocks": getattr(self, "max_bullish_stocks", 5),
                    "max_bearish_stocks": getattr(self, "max_bearish_stocks", 5),
                }
                with open(self.lots_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                LOGGER.info("Saved index config to %s: %s", self.lots_file, payload)
            except Exception as exc:
                LOGGER.warning("Could not save index_lots.json: %s", exc)

    def add_alert(self, alert_type: str, message: str, level: str = "INFO", details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Phase 3 Alerts: stale data, pre-open anomaly, feed lost, signals, entries, exits with P&L, 14:45 cutoff, 15:10 warning."""
        with self._alerts_lock:
            now_dt = datetime.now(IST)
            alert = {
                "id": len(self.alerts) + 1,
                "type": alert_type,
                "message": message,
                "level": level,
                "timestamp_ist": now_dt.strftime("%H:%M:%S"),
                "timestamp_ms": int(time.time() * 1000),
                "details": details or {},
            }
            self.alerts.append(alert)
            if len(self.alerts) > 200:
                self.alerts.pop(0)

            # Persist alert to database audit trail if available
            if hasattr(self, "database") and self.database:
                try:
                    sym = (details or {}).get("underlying") or (details or {}).get("symbol") or ""
                    self.database.insert_strategy_alert({
                        "alert_date": now_dt.strftime("%Y-%m-%d"),
                        "timestamp_ist": alert["timestamp_ist"],
                        "timestamp_ms": alert["timestamp_ms"],
                        "level": level,
                        "alert_type": alert_type,
                        "symbol": str(sym),
                        "message": message,
                        "details_json": json.dumps(details or {}),
                    })
                except Exception as ex:
                    LOGGER.debug("Could not persist alert to database: %s", ex)

            LOGGER.info("[ALERT] [%s] %s: %s", level, alert_type, message)
            return alert

    def get_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._alerts_lock:
            return list(reversed(self.alerts[-limit:]))

    def _log_signal_audit(
        self,
        underlying_key: str,
        side: str,
        strike: float,
        contract_key: str,
        ltp: float,
        confidence: str,
        action_taken: str,
        prev_ltp: float = 0.0,
        qty_delta: float = 0.0,
        val_lakhs: float = 0.0,
        day_high: float = 0.0,
        prev_close: float = 0.0,
        bid_price: float = 0.0,
        ask_price: float = 0.0,
        spread_pct: float = 0.0,
        oi_change: float = 0.0,
        delta_ce: float = 0.0,
        delta_pe: float = 0.0,
        **kwargs: Any,
    ) -> None:
        """Phase 3: Log every signal (taken or blocked) with its rank, condition number, and reason."""
        now_dt = datetime.now(IST)
        sig_record = {
            "timestamp_ist": now_dt.strftime("%H:%M:%S.%f")[:-3],
            "timestamp_ms": int(time.time() * 1000),
            "instrument_name": self.index_underlyings.get(underlying_key, {}).get("name", underlying_key),
            "side": side,
            "strike": strike,
            "contract_key": contract_key,
            "ltp": ltp,
            "prev_ltp": prev_ltp,
            "qty_delta": qty_delta,
            "val_lakhs": val_lakhs,
            "day_high": day_high,
            "prev_close": prev_close,
            "action_taken": action_taken,
            "bid_price": bid_price,
            "ask_price": ask_price,
            "spread_pct": spread_pct,
            "oi_change": oi_change,
            "confidence": confidence,
        }
        try:
            self.database.insert_strategy_signal(sig_record)
        except Exception as exc:
            LOGGER.debug("Could not insert signal audit: %s", exc)

    def classify_instrument(self, key: str) -> str:
        """
        Dynamically categorizes an instrument key into 'indices' or 'stocks'.
        Used by DualUpstoxFeed to route incoming subscriptions to the correct WebSocket pipe.
        """
        key_str = str(key).strip()
        if not key_str:
            return "indices"

        # 1. Check index underlyings (spots, futures, and their options)
        if key_str in getattr(self, "index_underlyings", {}):
            return "indices"
        for d in getattr(self, "index_underlyings", {}).values():
            if key_str == d.get("futures_key"):
                return "indices"
            if key_str in (d.get("locked_keys") or []):
                return "indices"
            if key_str in (d.get("planned_keys") or []):
                return "indices"

        # 2. Check stock underlyings (cash stocks, futures, and their options)
        if key_str in getattr(self, "stock_underlyings", {}):
            return "stocks"
        for sym, st in getattr(self, "stock_underlyings", {}).items():
            if key_str == st.get("key") or key_str == st.get("futures_key"):
                return "stocks"
            if key_str in (st.get("locked_keys") or []):
                return "stocks"
            if key_str in (st.get("planned_keys") or []):
                return "stocks"

        # 3. Check contracts_by_key cache
        contract = getattr(self, "contracts_by_key", {}).get(key_str)
        if contract:
            u = str(contract.get("underlying_key") or "")
            if u in getattr(self, "index_underlyings", {}) or "INDEX" in u.upper():
                return "indices"
            return "stocks"

        # 4. Exchange segment prefixes
        if key_str.startswith("NSE_INDEX|") or key_str.startswith("BSE_INDEX|") or "INDEX" in key_str.upper():
            return "indices"
        if key_str.startswith("NSE_EQ|") or key_str.startswith("BSE_EQ|") or "|INE" in key_str.upper():
            return "stocks"

        return "indices"

    def set_feed(self, feed: Any) -> None:
        self.feed = feed
        idx_keys: list[str] = []
        stk_keys: list[str] = []

        # 1. Futures keys for indices and their planned/locked keys
        for d in self.index_underlyings.values():
            if d.get("futures_key"):
                idx_keys.append(d["futures_key"])
            idx_keys.extend(d.get("locked_keys") or d.get("planned_keys") or [])

        # 2. Stock keys, futures and option contracts
        for d in self.stock_underlyings.values():
            if d.get("key"):
                stk_keys.append(d["key"])
            if d.get("futures_key"):
                stk_keys.append(d["futures_key"])
            stk_keys.extend(d.get("locked_keys") or d.get("planned_keys") or [])

        # 3. Legacy locked keys
        idx_keys.extend(self.locked_keys or self.planned_keys or [])

        idx_uniq = list(dict.fromkeys([k for k in idx_keys if k]))
        stk_uniq = list(dict.fromkeys([k for k in stk_keys if k]))

        if hasattr(feed, "subscribe"):
            try:
                if idx_uniq:
                    feed.subscribe(idx_uniq, target="indices")
                if stk_uniq:
                    feed.subscribe(stk_uniq, target="stocks")
                LOGGER.info("Feed assigned: Subscribed %d index contracts and %d stock contracts.", len(idx_uniq), len(stk_uniq))
            except Exception as exc:
                LOGGER.warning("Could not auto-subscribe initial keys on feed assign: %s", exc)

    def set_access_token(self, token: str) -> None:
        self.access_token = token
        if hasattr(self, "stock_screener"):
            self.stock_screener.set_access_token(token)
        # Pre-load option contracts for all indices immediately so strike mapping is isolated
        for ukey in getattr(self, "index_underlyings", {}):
            try:
                self.load_contracts_if_needed(ukey)
            except Exception as e:
                LOGGER.debug("Could not pre-load contracts for %s: %s", ukey, e)

    def load_stock_configs(self) -> None:
        """Loads selected stocks from selected_stocks.json and tomorrow_watchlist.json."""
        with self._lock:
            if not hasattr(self, "stock_screener"):
                self.stock_screener = StockScreener(base_dir=self.base_dir, access_token=self.access_token)

            # Load tomorrow watchlist for Dr. Vijay Bhilwade's EOD scoring & ranks
            wl_dict: dict[str, dict[str, Any]] = {}
            try:
                wl = self.stock_screener.get_tomorrow_watchlist()
                for item in wl.get("top_bullish", []):
                    sym_u = item.get("symbol", "").upper()
                    if sym_u:
                        d_item = dict(item)
                        d_item["direction"] = "BULLISH"
                        wl_dict[sym_u] = d_item
                for item in wl.get("top_bearish", []):
                    sym_u = item.get("symbol", "").upper()
                    if sym_u and sym_u not in wl_dict:
                        d_item = dict(item)
                        d_item["direction"] = "BEARISH"
                        wl_dict[sym_u] = d_item
            except Exception as e:
                LOGGER.debug("Could not get tomorrow watchlist: %s", e)

            # Arm all 50 constituent stocks from NIFTY_50_STOCKS
            cfg = self.stock_screener.get_top_stocks_config()
            custom_map = {s.get("symbol", "").upper(): s for s in (cfg.get("stocks") or [])}

            current_syms = set()
            for s in NIFTY_50_STOCKS:
                sym = s["symbol"].upper()
                current_syms.add(sym)
                c_conf = custom_map.get(sym, {})
                lots = max(1, int(c_conf.get("lots", 1)))
                enabled = bool(c_conf.get("enabled", True))
                lot_size = int(s.get("lot_size", 100))
                key = s.get("key", "")
                strike_step = float(s.get("strike_step", 10.0))

                wl_item = wl_dict.get(sym, {})
                watchlist_rank = wl_item.get("rank") or c_conf.get("rank") or "★★★"
                watchlist_dir = wl_item.get("direction") or c_conf.get("direction") or "BULLISH"
                futures_key = wl_item.get("futures_key") or c_conf.get("futures_key") or ""
                futures_sym = wl_item.get("futures_symbol") or c_conf.get("futures_symbol") or ""
                prev_close = float(wl_item.get("prev_close") or c_conf.get("prev_close") or 0.0)

                if sym not in self.stock_underlyings:
                    self.stock_underlyings[sym] = {
                        "symbol": sym,
                        "key": key,
                        "lot_size": lot_size,
                        "strike_step": strike_step,
                        "lots": lots,
                        "enabled": enabled,
                        "spot_price": None,
                        "open_price": None,
                        "prev_close": prev_close,
                        "gap_pct": 0.0,
                        "gap_against": False,
                        "gap_reason": "",
                        "watchlist_rank": watchlist_rank,
                        "watchlist_direction": watchlist_dir,
                        "futures_key": futures_key,
                        "futures_symbol": futures_sym,
                        "futures_ltp": 0.0,
                        "futures_oi": 0.0,
                        "futures_prev_close": prev_close,
                        "futures_prev_oi": 0.0,
                        "futures_buildup": "Neutral",
                        "path1_done": False,
                        "consecutive_signal": None,
                        "consecutive_count": 0,
                        "today_trades_count": 0,
                        "state": "SETUP",
                        "planned_keys": [],
                        "locked_keys": [],
                        "snapshot": {},
                        "snapshot_taken": False,
                        "last_evaluation": {
                            "decision": "WAIT",
                            "reason": f"Before 09:18:00 evaluation window for {sym}",
                            "delta_ce": 0.0,
                            "delta_pe": 0.0,
                            "ce_type": None,
                            "pe_type": None,
                            "rule": None,
                            "stars": "",
                            "size": "",
                            "summary_line": f"{sym} WAIT: Pre-09:18",
                        },
                        "current_position": None,
                        "_last_eval_time": 0.0,
                    }
                else:
                    self.stock_underlyings[sym]["lots"] = lots
                    self.stock_underlyings[sym]["enabled"] = enabled
                    if key:
                        self.stock_underlyings[sym]["key"] = key
                    if lot_size > 0:
                        self.stock_underlyings[sym]["lot_size"] = lot_size
                    if strike_step > 0:
                        self.stock_underlyings[sym]["strike_step"] = strike_step
                    if watchlist_rank:
                        self.stock_underlyings[sym]["watchlist_rank"] = watchlist_rank
                    if watchlist_dir:
                        self.stock_underlyings[sym]["watchlist_direction"] = watchlist_dir
                    if futures_key:
                        self.stock_underlyings[sym]["futures_key"] = futures_key
                    if futures_sym:
                        self.stock_underlyings[sym]["futures_symbol"] = futures_sym
                    if prev_close > 0:
                        self.stock_underlyings[sym]["prev_close"] = prev_close

                # Ensure equity and futures are tracked in hot cache
                if key and key not in self.hot_cache:
                    self.hot_cache[key] = {
                        "instrument_key": key,
                        "trading_symbol": sym,
                        "underlying_key": sym,
                        "ltp": None,
                        "prev_close": prev_close,
                    }
                if futures_key and futures_key not in self.hot_cache:
                    self.hot_cache[futures_key] = {
                        "instrument_key": futures_key,
                        "trading_symbol": futures_sym or f"{sym} FUT",
                        "underlying_key": sym,
                        "ltp": None,
                        "oi": None,
                        "prev_close": prev_close,
                    }

            # Remove any stocks not in the master list
            for sym in list(self.stock_underlyings.keys()):
                if sym not in current_syms:
                    del self.stock_underlyings[sym]

            LOGGER.info("Armed all %d NIFTY 50 stock underlyings for trading.", len(self.stock_underlyings))

    def plan_stock_contracts(self, stock_sym: str, spot: float | None = None) -> list[str]:
        """Plans 8 ITM contracts for a constituent stock and subscribes to feed."""
        stock_sym = stock_sym.upper()
        stock_state = self.stock_underlyings.get(stock_sym)
        if not stock_state or not stock_state.get("enabled", True):
            return []

        if spot is not None and spot > 0:
            stock_state["spot_price"] = spot
        elif stock_state.get("spot_price") and float(stock_state["spot_price"]) > 0:
            spot = float(stock_state["spot_price"])
        elif stock_state.get("prev_close") and float(stock_state["prev_close"]) > 0:
            spot = float(stock_state["prev_close"])
        elif stock_state.get("open_price") and float(stock_state["open_price"]) > 0:
            spot = float(stock_state["open_price"])

        res = self.stock_screener.plan_stock_itm_contracts(stock_sym, spot)
        if not res or "error" in res:
            LOGGER.warning("Could not plan ITM contracts for %s: %s", stock_sym, res.get("error") if res else "None")
            return []

        planned_list = res.get("planned_contracts") or []
        keys = []
        for c in planned_list:
            k = c.get("instrument_key")
            if not k:
                continue
            keys.append(k)
            self.contracts_by_key[k] = {
                "instrument_key": k,
                "trading_symbol": c.get("symbol") or k,
                "strike_price": c.get("strike"),
                "instrument_type": c.get("side"),
                "lot_size": c.get("lot_size", stock_state["lot_size"]),
                "underlying_key": stock_sym,
            }
            if k not in self.hot_cache:
                self.hot_cache[k] = {
                    "instrument_key": k,
                    "trading_symbol": c.get("symbol") or k,
                    "underlying_key": stock_sym,
                    "strike": float(c.get("strike") or 0.0),
                    "side": str(c.get("side") or "").upper(),
                    "ref_ltp": None,
                    "ref_source": None,
                    "prev_close": 0.0,
                    "prev_oi": 0.0,
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

        stock_state["planned_keys"] = keys
        if stock_state.get("state") == "SETUP":
            stock_state["state"] = "PLANNING"

        extra_keys = []
        if stock_state.get("key"):
            extra_keys.append(stock_state["key"])
        if stock_state.get("futures_key"):
            extra_keys.append(stock_state["futures_key"])

        # JIT: Subscribe only cash equity and futures upfront, preventing 400 option saturation
        if self.feed and hasattr(self.feed, "subscribe") and extra_keys:
            self.feed.subscribe(extra_keys, target="stocks")

        LOGGER.info("Planned 8 ITM contracts for %s: %s", stock_sym, keys)
        return keys

    def lock_stock_contracts(self, stock_sym: str) -> None:
        stock_sym = stock_sym.upper()
        st = self.stock_underlyings.get(stock_sym)
        if not st or not st.get("enabled", True):
            return
        if not st.get("planned_keys") and st.get("spot_price"):
            self.plan_stock_contracts(stock_sym, st["spot_price"])
        if not st.get("planned_keys"):
            return
        st["locked_keys"] = list(st["planned_keys"])
        for k in st["locked_keys"]:
            entry = self.hot_cache.get(k)
            if entry:
                if entry.get("ltp") is not None and float(entry["ltp"]) > 0:
                    entry["ref_ltp"] = float(entry["ltp"])
                    entry["ref_source"] = "ltp"
                else:
                    entry["ref_ltp"] = float(entry.get("bid_price") or 0.0)
                    entry["ref_source"] = "bid"
        st["state"] = "LOCKED"
        LOGGER.info("LOCKED 8 contracts for %s: %s", stock_sym, st["locked_keys"])

    def take_stock_snapshot(self, stock_sym: str) -> None:
        """
        09:15:00 Snapshot for stock:
        - Snapshots 09:15 Open Price from live cash tick.
        - Checks Gap filter against yesterday's direction (Skip stock if gap > 0.3% against).
        - Checks Exhaustion Gap Cap (Skip stock if abs(gap) > 2.5%).
        - Transitions state to ARMED for Path 1.
        """
        stock_sym = stock_sym.upper()
        st = self.stock_underlyings.get(stock_sym)
        if not st or not st.get("enabled", True):
            return

        # 09:15 Open Price
        open_p = float(st.get("spot_price") or self.hot_cache.get(st.get("key", ""), {}).get("ltp") or 0.0)
        st["open_price"] = open_p

        # Gap filter against yesterday's direction (Rules 4 & 5) + 2.5% Exhaustion Cap
        prev_c = float(st.get("prev_close") or self.hot_cache.get(st.get("key", ""), {}).get("prev_close") or 0.0)
        if prev_c > 0 and open_p > 0:
            gap_pct = ((open_p - prev_c) / prev_c) * 100.0
            st["gap_pct"] = round(gap_pct, 2)
            dir_pref = str(st.get("watchlist_direction") or "BULLISH").upper()
            if abs(gap_pct) > 2.5:
                st["gap_against"] = True
                st["gap_reason"] = f"Exhaustion gap {gap_pct:+.2f}% exceeds 2.5% cap"
                LOGGER.info("Stock %s - SKIPPED for Path 1: %s", stock_sym, st["gap_reason"])
            elif dir_pref == "BULLISH" and gap_pct < -0.3:
                st["gap_against"] = True
                st["gap_reason"] = f"Gapped down {gap_pct:.2f}% (> 0.3%) against BULLISH direction"
                LOGGER.info("Stock %s BULLISH - SKIPPED for Path 1: %s", stock_sym, st["gap_reason"])
            elif dir_pref == "BEARISH" and gap_pct > 0.3:
                st["gap_against"] = True
                st["gap_reason"] = f"Gapped up +{gap_pct:.2f}% (> 0.3%) against BEARISH direction"
                LOGGER.info("Stock %s BEARISH - SKIPPED for Path 1: %s", stock_sym, st["gap_reason"])
            else:
                st["gap_against"] = False
                st["gap_reason"] = ""

        # Option snapshot if contracts already planned
        keys = st.get("locked_keys") or st.get("planned_keys") or []
        snap = {}
        for k in keys:
            entry = self.hot_cache.get(k, {})
            ltp = float(entry.get("ltp") or entry.get("ref_ltp") or 0.0)
            oi = float(entry.get("oi") or entry.get("prev_oi") or 0.0)
            bid = float(entry.get("bid_price") or 0.0)
            ask = float(entry.get("ask_price") or 0.0)
            snap[k] = {
                "ltp": ltp,
                "oi": oi,
                "bid": bid,
                "ask": ask,
                "strike": float(entry.get("strike") or 0.0),
                "side": entry.get("side"),
            }
        st["snapshot"] = snap

        # Futures snapshot
        fut_k = st.get("futures_key")
        if fut_k and fut_k in self.hot_cache:
            st["futures_snap_ltp"] = float(self.hot_cache[fut_k].get("ltp") or 0.0)
            st["futures_snap_oi"] = float(self.hot_cache[fut_k].get("oi") or 0.0)

        st["snapshot_taken"] = True
        st["state"] = "ARMED"
        LOGGER.info(
            "SNAPSHOT taken at 09:15:00 for %s (Open=%.2f, Gap=%.2f%%, GapAgainst=%s)",
            stock_sym,
            open_p,
            st.get("gap_pct", 0.0),
            st.get("gap_against", False),
        )

    def _pace_stock_order_dispatch(self) -> float:
        """
        Enforces micro-pacing (50ms spacing / max 20 orders/sec) for Priority 1 stock orders.
        Indices are Priority 0 and bypass this completely with zero delay.
        Thread-safe: Uses _stock_pace_lock so index order processing is never blocked.
        """
        with self._stock_pace_lock:
            now = time.time()
            elapsed = now - self._last_stock_order_time
            if elapsed < self.stock_order_interval_sec:
                sleep_needed = self.stock_order_interval_sec - elapsed
                time.sleep(sleep_needed)
            self._last_stock_order_time = time.time()
            return self._last_stock_order_time

    def _execute_stock_path1_entry(
        self,
        stock_sym: str,
        side: str,
        trigger_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Path 1: 09:15 First-Tick Entry (only for watchlist stocks ranked ★★★ or ★★★★).
        - Skip if opened > 0.3% against yesterday's direction.
        - Buy ATM CE on first tick above open (Bullish).
        - Buy ATM PE on first tick below open (Bearish).
        - Rule 9 Futures Gate: Re-confirm futures direction (CE requires not SB/LU; PE requires not LB/SC).
        - Difference 4 Spread Guard: Reject if bid-ask spread > 3.0%.
        - Priority 1 Micro-Pacing: 50ms spacing (max 20 orders/sec) to prevent Upstox HTTP 429.
        - Fill at live ask. Stop-loss 25%, target 40%, cutoff 09:45. Max 1 09:15 entry per stock.
        """
        stock_sym = stock_sym.upper()
        st = self.stock_underlyings.get(stock_sym)
        if not st or not st.get("enabled", True):
            return None

        now_dt = datetime.now(IST)
        now_time_hms = now_dt.strftime("%H:%M:%S")
        if self.re_eval_min_interval_sec > 0 and now_time_hms >= self.entry_start_time:
            st["path1_done"] = True
            st["path1_evaluating"] = False
            return None

        if st.get("gap_against"):
            st["path1_done"] = True
            st["path1_evaluating"] = False
            return None
        rank = st.get("watchlist_rank", "★★★")
        if rank not in ("★★★", "★★★★"):
            st["path1_done"] = True
            st["path1_evaluating"] = False
            return None
        if st.get("current_position") is not None:
            st["path1_done"] = True
            st["path1_evaluating"] = False
            return None

        # Rule 9: Re-confirm futures direction check (Futures Gate)
        fut_bu = st.get("futures_buildup", "Neutral")
        if side == "CE" and fut_bu in ("SB", "LU"):
            LOGGER.info("Stock Path 1 BLOCKED for %s (CE): Futures buildup is %s (must not be SB/LU)", stock_sym, fut_bu)
            st["path1_done"] = True
            st["path1_evaluating"] = False
            return None
        elif side == "PE" and fut_bu in ("LB", "SC"):
            LOGGER.info("Stock Path 1 BLOCKED for %s (PE): Futures buildup is %s (must not be LB/SC)", stock_sym, fut_bu)
            st["path1_done"] = True
            st["path1_evaluating"] = False
            return None

        # JIT Strike Planning & Locking
        spot_p = float(st.get("spot_price") or st.get("open_price") or st.get("prev_close") or 0.0)
        if not st.get("locked_keys"):
            self.plan_stock_contracts(stock_sym, spot=spot_p)
            self.lock_stock_contracts(stock_sym)

        winner = self._select_best_strike(side, locked_keys=st.get("locked_keys"), spot=spot_p)
        if not winner:
            LOGGER.warning("Path 1 for %s (%s): No valid strike found.", stock_sym, side)
            st["path1_evaluating"] = False
            return None

        # JIT: Subscribes ONLY this specific winning contract to WebSocket 2
        w_key = winner.get("instrument_key")
        if self.feed and hasattr(self.feed, "subscribe") and w_key:
            self.feed.subscribe([w_key], target="stocks")

        ask = float(winner.get("ask_price") or winner.get("ltp") or 0.0)
        bid = float(winner.get("bid_price") or 0.0)
        if ask <= 0.0 and w_key in self.hot_cache:
            ask = float(self.hot_cache[w_key].get("ask_price") or self.hot_cache[w_key].get("ltp") or 0.0)
            if bid <= 0.0:
                bid = float(self.hot_cache[w_key].get("bid_price") or 0.0)

        # Fast quote wait buffer: Wait up to 300ms (6 x 50ms) for the WebSocket quote to arrive in hot_cache
        if ask <= 0.0 and w_key:
            for _ in range(6):
                time.sleep(0.05)
                cached = self.hot_cache.get(w_key, {})
                ask = float(cached.get("ask_price") or cached.get("ltp") or 0.0)
                bid = float(cached.get("bid_price") or 0.0)
                if ask > 0.0:
                    break

        if ask <= 0.0:
            LOGGER.info("Path 1 for %s (%s): Strike %s waiting for live quote (ask/ltp=0). Retrying on next tick.",
                        stock_sym, side, winner.get("trading_symbol"))
            # Crucial: Do NOT mark path1_done=True; allow next tick to retry once quote arrives!
            st["path1_evaluating"] = False
            return None

        # Difference 4: Stock Safety Guard - Bid-Ask spread > 3.0% rejection
        if bid > 0.0 and ask > 0.0:
            spread_pct = ((ask - bid) / ask) * 100.0
            if spread_pct > self.max_spread_pct:
                LOGGER.warning("Path 1 for %s (%s): Strike %s spread %.2f%% exceeds %.1f%% guard. Order rejected.",
                               stock_sym, side, winner.get("trading_symbol"), spread_pct, self.max_spread_pct)
                st["path1_done"] = True
                st["path1_evaluating"] = False
                return None

        # Priority 1: Enforce micro-pacing for stock order (50ms rate limit buffer)
        self._pace_stock_order_dispatch()

        st["path1_done"] = True
        st["path1_evaluating"] = False
        base_lots = max(1, int(st.get("lots", 1)))
        lots = max(1, base_lots * 2) if "★★★★" in rank else base_lots
        lot_sz = int(st.get("lot_size", self.lot_size))
        total_qty = lots * lot_sz

        entry_price = ask
        stop_loss_price = round(entry_price * 0.75, 2)
        target_price = round(entry_price * 1.40, 2)

        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        now_dt = datetime.now(IST)
        time_str = now_dt.strftime("%H:%M:%S.%f")[:-3]
        trade_id = f"STK-P1-{datetime.now(IST).strftime('%m%d%H%M%S')}-{self._trade_counter}"
        self._trade_counter += 1

        order_side = f"BUY_{side}"
        symbol = winner.get("trading_symbol") or f"{stock_sym} {winner.get('strike')} {side}"
        inst_key = winner["instrument_key"]
        strike = winner.get("strike", 0.0)

        trade = {
            "trade_id": trade_id,
            "trade_date": today_str,
            "side": order_side,
            "symbol": symbol,
            "instrument_key": inst_key,
            "underlying_key": stock_sym,
            "strike": strike,
            "lots": lots,
            "lot_size": total_qty,
            "entry_time_ist": time_str,
            "entry_price": entry_price,
            "entry_timestamp_ms": trigger_ms or int(time.time() * 1000),
            "exit_time_ist": None,
            "exit_price": None,
            "exit_timestamp_ms": None,
            "exit_reason": None,
            "pnl_points": 0.0,
            "net_pnl": 0.0,
            "status": "OPEN",
            "spot_price": st.get("spot_price"),
            "atm_strike": strike,
            "current_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "target_price": target_price,
            "entry_path": "PATH_1_0915",
            "timeout_time": "09:45:00",
            "build_up": f"Path 1 (09:15) First-Tick {side} Buy | Rank: {rank}",
            "stars": rank,
            "decision": f"09:15 {side} Buy",
        }

        st["current_position"] = trade
        st["today_trades_count"] = st.get("today_trades_count", 0) + 1
        st["state"] = "HOLDING"
        self.current_positions[stock_sym] = trade

        try:
            self.database.insert_strategy_trade(trade)
            if self.csv_path.exists():
                with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        trade_id, today_str, order_side, symbol, strike, lots, total_qty,
                        time_str, entry_price, "", "", "", 0.0, 0.0, "OPEN",
                        st.get("spot_price"), strike, trade["build_up"], 0.25, 0.0
                    ])
        except Exception as exc:
            LOGGER.error("DB error recording Path 1 trade for %s: %s", stock_sym, exc)

        LOGGER.info(
            "PATH 1 (09:15) ENTRY EXECUTED: %s %s @ %.2f (Qty: %d, SL: %.2f, Target: %.2f, Cutoff: 09:45)",
            stock_sym,
            symbol,
            entry_price,
            total_qty,
            stop_loss_price,
            target_price,
        )
        return trade

    @staticmethod
    def _classify_buildup(p_dir: str, oi_dir: str) -> str:
        if p_dir == "UP" and oi_dir == "UP":
            return "LB"
        if p_dir == "DOWN" and oi_dir == "UP":
            return "SB"
        if p_dir == "UP" and oi_dir == "DOWN":
            return "SC"
        if p_dir == "DOWN" and oi_dir == "DOWN":
            return "LU"
        return "Neutral"

    def evaluate_stock_oi_buildup(
        self,
        stock_sym: str,
        trigger_ms: int | None = None,
        force: bool = False,
        require_consecutive: bool = False,
    ) -> dict[str, Any] | None:
        """
        Path 2: From 09:18 OI-based entry, all day (until 14:45).
        - 16-condition table applied to stock ATM +- 1 options vs 09:15 snapshot.
        - Futures check (Futures Gate): CE Buy needs LB/SC. PE Buy needs SB/LU. Otherwise block it!
        - Signal must hold for 2 OI updates in a row.
        - Rank decides lot size (Sizing matrix).
        - Live ask fill, SL 25%, Target 40%, live bid exit. Max 3 trades per stock per day.
        """
        stock_sym = stock_sym.upper()
        st = self.stock_underlyings.get(stock_sym)
        if not st or not st.get("enabled", True):
            return None
        if st.get("current_position") is not None:
            return None

        if not force and self.re_eval_min_interval_sec > 0:
            now_ts = time.time()
            if now_ts - st.get("_last_eval_time", 0.0) < self.re_eval_min_interval_sec:
                return None
            st["_last_eval_time"] = now_ts

            now_dt = datetime.now(IST)
            now_time_hms = now_dt.strftime("%H:%M:%S")
            if now_time_hms >= self.entry_end_time or now_time_hms < self.entry_start_time:
                return None

        snap = st.get("snapshot") or {}
        locked_keys = st.get("locked_keys") or []
        if len(locked_keys) < 8 or len(snap) < 8:
            return None

        ce_keys = [k for k in locked_keys if self.hot_cache.get(k, {}).get("side") == "CE"]
        pe_keys = [k for k in locked_keys if self.hot_cache.get(k, {}).get("side") == "PE"]
        if len(ce_keys) != 4 or len(pe_keys) != 4:
            return None

        ce_oi_fresh = any(
            float(self.hot_cache.get(k, {}).get("oi") or 0.0) != float(snap.get(k, {}).get("oi") or 0.0)
            for k in ce_keys
        )
        pe_oi_fresh = any(
            float(self.hot_cache.get(k, {}).get("oi") or 0.0) != float(snap.get(k, {}).get("oi") or 0.0)
            for k in pe_keys
        )
        if not (ce_oi_fresh and pe_oi_fresh):
            return None

        ce_curr_oi_sum = sum(float(self.hot_cache.get(k, {}).get("oi") or 0.0) for k in ce_keys)
        ce_snap_oi_sum = sum(float(snap.get(k, {}).get("oi") or 0.0) for k in ce_keys)
        delta_ce = ce_curr_oi_sum - ce_snap_oi_sum

        pe_curr_oi_sum = sum(float(self.hot_cache.get(k, {}).get("oi") or 0.0) for k in pe_keys)
        pe_snap_oi_sum = sum(float(snap.get(k, {}).get("oi") or 0.0) for k in pe_keys)
        delta_pe = pe_curr_oi_sum - pe_snap_oi_sum

        ce_curr_ltp_sum = sum(float(self.hot_cache.get(k, {}).get("ltp") or snap.get(k, {}).get("ltp") or 0.0) for k in ce_keys)
        ce_snap_ltp_sum = sum(float(snap.get(k, {}).get("ltp") or 0.0) for k in ce_keys)
        ce_price_pct = ((ce_curr_ltp_sum - ce_snap_ltp_sum) / ce_snap_ltp_sum * 100.0) if ce_snap_ltp_sum > 0 else 0.0

        pe_curr_ltp_sum = sum(float(self.hot_cache.get(k, {}).get("ltp") or snap.get(k, {}).get("ltp") or 0.0) for k in pe_keys)
        pe_snap_ltp_sum = sum(float(snap.get(k, {}).get("ltp") or 0.0) for k in pe_keys)
        pe_price_pct = ((pe_curr_ltp_sum - pe_snap_ltp_sum) / pe_snap_ltp_sum * 100.0) if pe_snap_ltp_sum > 0 else 0.0

        ce_oi_pct = (delta_ce / ce_snap_oi_sum * 100.0) if ce_snap_oi_sum > 0 else 0.0
        pe_oi_pct = (delta_pe / pe_snap_oi_sum * 100.0) if pe_snap_oi_sum > 0 else 0.0

        ce_price_dir = "UP" if ce_price_pct > self.price_threshold_pct else "DOWN" if ce_price_pct < -self.price_threshold_pct else "FLAT"
        ce_oi_dir = "UP" if ce_oi_pct > self.oi_threshold_pct else "DOWN" if ce_oi_pct < -self.oi_threshold_pct else "FLAT"
        pe_price_dir = "UP" if pe_price_pct > self.price_threshold_pct else "DOWN" if pe_price_pct < -self.price_threshold_pct else "FLAT"
        pe_oi_dir = "UP" if pe_oi_pct > self.oi_threshold_pct else "DOWN" if pe_oi_pct < -self.oi_threshold_pct else "FLAT"

        if "FLAT" in (ce_price_dir, ce_oi_dir, pe_price_dir, pe_oi_dir):
            st["last_evaluation"] = {
                "decision": "WAIT",
                "reason": "At least one direction is FLAT",
                "delta_ce": delta_ce,
                "delta_pe": delta_pe,
                "summary_line": f"{stock_sym} WAIT: FLAT direction",
            }
            st["consecutive_signal"] = None
            st["consecutive_count"] = 0
            return None

        ce_type = self._classify_buildup(ce_price_dir, ce_oi_dir)
        pe_type = self._classify_buildup(pe_price_dir, pe_oi_dir)

        table_entry = DECISION_TABLE.get((ce_type, pe_type), {"rule": None, "decision": "No trade", "side": None, "stars": "", "size": ""})
        decision = table_entry["decision"]
        side = table_entry.get("side")
        stars = table_entry.get("stars", "")
        size = table_entry.get("size", "")
        rule_num = table_entry.get("rule")

        st["last_evaluation"] = {
            "decision": decision,
            "rule": rule_num,
            "stars": stars,
            "size": size,
            "delta_ce": delta_ce,
            "delta_pe": delta_pe,
            "ce_type": ce_type,
            "pe_type": pe_type,
            "summary_line": f"{stock_sym} ΔCE: {delta_ce:+,.0f} | ΔPE: {delta_pe:+,.0f} | {ce_type}+{pe_type} -> {decision} {stars}",
        }

        if decision in ("CE Buy", "PE Buy") and side:
            # 1. Futures Check (Rule 9: Gate)
            fut_bu = st.get("futures_buildup", "Neutral")
            if decision == "CE Buy" and fut_bu in ("SB", "LU"):
                LOGGER.info("Futures Gate BLOCKED CE Buy for %s: Futures is %s (must be LB or SC)", stock_sym, fut_bu)
                st["consecutive_signal"] = None
                st["consecutive_count"] = 0
                return None
            elif decision == "PE Buy" and fut_bu in ("LB", "SC"):
                LOGGER.info("Futures Gate BLOCKED PE Buy for %s: Futures is %s (must be SB or LU)", stock_sym, fut_bu)
                st["consecutive_signal"] = None
                st["consecutive_count"] = 0
                return None

            # 2. Two Consecutive OI Updates Check (Rule 11)
            if require_consecutive and not force:
                prev_sig = st.get("consecutive_signal")
                if prev_sig == decision:
                    st["consecutive_count"] = st.get("consecutive_count", 0) + 1
                else:
                    st["consecutive_signal"] = decision
                    st["consecutive_count"] = 1
                    LOGGER.info("%s signal %s held for 1 fresh OI update. Awaiting 2nd consecutive update...", stock_sym, decision)
                    return None

                if st["consecutive_count"] < 2:
                    return None

                # Confirmed for 2 updates! Reset counter
                st["consecutive_count"] = 0

            # 3. Strike Selection
            winner = self._select_best_strike(side, locked_keys=locked_keys)
            if not winner:
                LOGGER.warning("%s %s trigger: No valid contract with spread <= %.1f%%", stock_sym, side, self.max_spread_pct)
                return None

            # JIT: Subscribes ONLY this specific winning contract to WebSocket 2
            w_key = winner.get("instrument_key")
            if self.feed and hasattr(self.feed, "subscribe") and w_key:
                self.feed.subscribe([w_key], target="stocks")

            # Difference 4: Stock Safety Guard - Bid-Ask spread > 3.0% rejection
            w_bid = float(winner.get("bid_price") or 0.0)
            w_ask = float(winner.get("ask_price") or winner.get("ltp") or 0.0)
            if w_bid > 0.0 and w_ask > 0.0:
                w_spread = ((w_ask - w_bid) / w_ask) * 100.0
                if w_spread > self.max_spread_pct:
                    LOGGER.warning("Stock %s %s trigger: Winner %s spread %.2f%% exceeds max %.1f%% guard. Order rejected.",
                                   stock_sym, side, winner.get("trading_symbol"), w_spread, self.max_spread_pct)
                    return None

            # Priority 1: Enforce micro-pacing for stock order (50ms rate limit buffer)
            self._pace_stock_order_dispatch()

            # 4. Sizing Matrix (Rule 10)
            size_cat = size or "normal"
            wl_rank = st.get("watchlist_rank") or stars
            if "★★★★" in wl_rank:
                size_cat = "full"

            trade = self._execute_entry_order(
                winner=winner,
                side=side,
                size=size_cat,
                rule_num=rule_num,
                stars=stars or wl_rank,
                delta_ce=delta_ce,
                delta_pe=delta_pe,
                ce_type=ce_type,
                pe_type=pe_type,
                time_str=datetime.now(IST).strftime("%H:%M:%S.%f")[:-3],
                trigger_ms=trigger_ms or int(time.time() * 1000),
                underlying_key=stock_sym,
            )
            # Attach SL 25% and Target 40%
            entry_p = float(trade["entry_price"])
            trade["stop_loss_price"] = round(entry_p * 0.75, 2)
            trade["target_price"] = round(entry_p * 1.40, 2)
            trade["entry_path"] = "PATH_2_OI"

            st["current_position"] = trade
            st["state"] = "HOLDING"
            return trade

        else:
            st["consecutive_signal"] = None
            st["consecutive_count"] = 0

        return None

    def plan_all_stocks(self) -> None:
        """In JIT mode, option contracts are planned dynamically when a stock triggers."""
        pass

    def lock_all_stocks(self) -> None:
        """
        At 09:14:55 Lock Checkpoint:
        Pre-plans and locks contracts for the Top 10 stocks (e.g. 5 Bullish + 5 Bearish).
        Subscribes ATM +- 2 options (4 contracts per stock: 2 CE + 2 PE = 40 instruments total)
        to WebSocket 2 (stocks-feed) so quotes are active for the 09:15 open.
        """
        with self._lock:
            # 1. Determine priority watchlist symbols (Top Bullish + Top Bearish)
            max_bull = getattr(self, "max_bullish_stocks", 5)
            max_bear = getattr(self, "max_bearish_stocks", 5)
            top_syms: list[str] = []
            try:
                wl = self.stock_screener.get_tomorrow_watchlist()
                for item in (wl.get("top_bullish") or [])[:max_bull]:
                    s = str(item.get("symbol", "")).upper().strip()
                    if s and s not in top_syms:
                        top_syms.append(s)
                for item in (wl.get("top_bearish") or [])[:max_bear]:
                    s = str(item.get("symbol", "")).upper().strip()
                    if s and s not in top_syms:
                        top_syms.append(s)
            except Exception as e:
                LOGGER.debug("Could not read tomorrow watchlist: %s", e)

            # Fallback if watchlist not yet generated
            if not top_syms:
                try:
                    cfg = self.stock_screener.get_top_stocks_config()
                    for s in (cfg.get("stocks") or []):
                        sym_u = str(s.get("symbol", "")).upper().strip()
                        if sym_u and sym_u not in top_syms:
                            top_syms.append(sym_u)
                        if len(top_syms) >= (max_bull + max_bear):
                            break
                except Exception:
                    pass

            # 2. Pre-plan & subscribe ATM +- 2 options (4 contracts per stock = 40 instruments)
            atm_keys_to_sub: list[str] = []
            for sym in top_syms:
                st = self.stock_underlyings.get(sym)
                if not st or not st.get("enabled", True):
                    continue

                spot = float(st.get("spot_price") or st.get("prev_close") or 0.0)
                if spot > 0 and not st.get("locked_keys"):
                    self.plan_stock_contracts(sym, spot)
                    self.lock_stock_contracts(sym)

                l_keys = st.get("locked_keys") or []
                if l_keys:
                    ce_keys = [k for k in l_keys if self.hot_cache.get(k, {}).get("side") == "CE"]
                    pe_keys = [k for k in l_keys if self.hot_cache.get(k, {}).get("side") == "PE"]
                    # 1 ATM CE + 1 ATM PE per stock (20 options total for 10 stocks)
                    if ce_keys:
                        atm_keys_to_sub.append(ce_keys[0])
                    if pe_keys:
                        atm_keys_to_sub.append(pe_keys[0])

            if not hasattr(self, "_active_stock_option_keys"):
                self._active_stock_option_keys = set()
            self._active_stock_option_keys.update(atm_keys_to_sub)

            if atm_keys_to_sub and self.feed and hasattr(self.feed, "subscribe"):
                try:
                    self.feed.subscribe(atm_keys_to_sub)
                    LOGGER.info(
                        "Pre-subscribed %d ATM option contracts for Top %d stocks on WebSocket: %s",
                        len(atm_keys_to_sub), len(top_syms), atm_keys_to_sub
                    )
                except Exception as exc:
                    LOGGER.debug("Could not pre-subscribe ATM stock keys: %s", exc)

    def snapshot_all_stocks(self) -> None:
        for sym, st in self.stock_underlyings.items():
            if st.get("enabled", True) and not st.get("snapshot_taken", False):
                self.take_stock_snapshot(sym)

    def arm_all_stocks(self) -> None:
        for sym, st in self.stock_underlyings.items():
            if st.get("enabled", True) and st.get("state") in ("LOCKED", "DATA_COLLECTION"):
                st["state"] = "ARMED"

    def squareoff_all_stocks_1515(self) -> None:
        """
        15:15:00 EOD clock square-off across all active stock positions,
        and dynamically unsubscribes all 40 stock option contracts from the stocks WebSocket.
        """
        with self._lock:
            # 1. Close open stock positions at live bid
            for sym, st in self.stock_underlyings.items():
                if st.get("state") == "HOLDING" and st.get("current_position"):
                    pos = st["current_position"]
                    if pos.get("status") == "OPEN":
                        inst_key = pos["instrument_key"]
                        cached = self.hot_cache.get(inst_key)
                        bid = float(cached.get("bid_price") or 0.0) if cached else 0.0
                        ltp = float(cached.get("ltp") or 0.0) if cached else 0.0
                        ref_p = float(cached.get("ref_ltp") or 0.0) if cached else 0.0
                        exit_price = bid if bid > 0 else (ltp if ltp > 0 else (ref_p if ref_p > 0 else float(pos.get("entry_price") or 0.0)))
                        qty = int(pos.get("lot_size") or 1)
                        pts = round(exit_price - float(pos["entry_price"]), 2)
                        pos["exit_time_ist"] = datetime.now(IST).strftime("%H:%M:%S.%f")[:-3]
                        pos["exit_price"] = exit_price
                        pos["exit_reason"] = "SQUAREOFF_1515"
                        pos["pnl_points"] = pts
                        pos["net_pnl"] = round(pts * qty, 2)
                        pos["status"] = "CLOSED"
                        st["state"] = "CLOSED"
                        st["current_position"] = None
                        self.current_positions[sym] = None
                        LOGGER.info("15:15:00 EOD Square-Off executed for stock %s @ %.2f", sym, exit_price)

            # 2. Stop and unsubscribe all 40 active stock option contracts from stocks WebSocket
            if hasattr(self, "_active_stock_option_keys") and self._active_stock_option_keys:
                keys_to_unsub = list(self._active_stock_option_keys)
                if self.feed and hasattr(self.feed, "unsubscribe"):
                    try:
                        self.feed.unsubscribe(keys_to_unsub)
                        LOGGER.info("15:15:00 Stopped and unsubscribed all %d stock option instruments from stocks WebSocket.", len(keys_to_unsub))
                    except Exception as exc:
                        LOGGER.warning("Could not unsubscribe stock option instruments: %s", exc)
                self._active_stock_option_keys.clear()

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
                for tr in today_trades:
                    ukey = tr.get("underlying_key") or NIFTY_KEY
                    if ukey in self.index_underlyings:
                        self.index_underlyings[ukey]["today_trades_count"] += 1
                    elif ukey in self.stock_underlyings:
                        self.stock_underlyings[ukey]["today_trades_count"] += 1

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

                    u_key = t.get("underlying_key") or NIFTY_KEY
                    if u_key in self.index_underlyings:
                        self.index_underlyings[u_key]["current_position"] = t
                        self.index_underlyings[u_key]["state"] = "HOLDING"
                    elif u_key in self.stock_underlyings:
                        self.stock_underlyings[u_key]["current_position"] = t
                        self.stock_underlyings[u_key]["state"] = "HOLDING"

                    self.current_positions[u_key] = t
                    if u_key == NIFTY_KEY:
                        self.current_position = t
                        self.state = "HOLDING"
                    LOGGER.info("Resumed open paper position for %s: %s", u_key, t["trade_id"])
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
    # -------------------------------------------------------------------------
    # Strike Loading & Planning (09:00 to 09:14:55)
    # -------------------------------------------------------------------------

    def _load_index_contracts_from_master(self, underlying_key: str) -> bool:
        """Loads index option contracts directly from local NSE.json master file."""
        sym_map = {
            NIFTY_KEY: "NIFTY",
            BANKNIFTY_KEY: "BANKNIFTY",
            "NSE_INDEX|Nifty 50": "NIFTY",
            "NSE_INDEX|Nifty Bank": "BANKNIFTY",
        }
        target_sym = sym_map.get(underlying_key)
        if not target_sym:
            return False

        search_paths = [
            Path("NSE.json"),
            Path(__file__).parent / "NSE.json",
            Path(__file__).parent.parent / "NSE.json",
        ]
        nse_file = next((p for p in search_paths if p.is_file()), None)
        if not nse_file:
            return False

        try:
            if not hasattr(self, "_master_instruments") or not self._master_instruments:
                with open(nse_file, "r", encoding="utf-8") as f:
                    try:
                        self._master_instruments = json.load(f)
                    except (json.JSONDecodeError, KeyboardInterrupt, Exception) as load_err:
                        LOGGER.error("Failed to load NSE.json (interrupted or corrupted): %s", load_err)
                        return False

            opts = [
                x for x in self._master_instruments
                if x.get("instrument_type") in ("CE", "PE")
                and (x.get("underlying_symbol") == target_sym or x.get("asset_symbol") == target_sym)
            ]
            if not opts:
                return False

            # Find active expiry >= today in IST
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            from datetime import timezone
            exp_dict: dict[str, list[dict[str, Any]]] = {}
            for x in opts:
                exp_ts = int(x.get("expiry") or 0)
                if exp_ts > 0:
                    d_str = datetime.fromtimestamp(exp_ts / 1000.0, timezone.utc).strftime("%Y-%m-%d")
                    if d_str >= today_str:
                        exp_dict.setdefault(d_str, []).append(x)

            if not exp_dict:
                return False

            act_exp = sorted(exp_dict.keys())[0]
            matched = exp_dict[act_exp]

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
                "Loaded %s contracts for %s expiry %s from NSE.json (%s CE, %s PE).",
                len(matched),
                underlying_key,
                act_exp,
                len(ce_map),
                len(pe_map),
            )
            return True
        except Exception as exc:
            LOGGER.error("Failed to load option contracts from NSE.json for %s: %s", underlying_key, exc)
            return False

    def load_contracts_if_needed(self, underlying_key: str = NIFTY_KEY) -> bool:
        """Fetch option contracts for index and isolate nearest expiry."""
        if underlying_key in self.contracts_by_underlying and self.contracts_by_underlying[underlying_key]:
            return True

        # 1. Prefer fast local master file (NSE.json) for NSE indices to eliminate 429 rate limits
        if self._load_index_contracts_from_master(underlying_key):
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
            return self._load_index_contracts_from_master(underlying_key)

    def plan_8_itm_contracts(self, spot: float, underlying_key: str = NIFTY_KEY) -> list[str]:
        """
        Plans ATM +- 5 Strikes (11 CE and 11 PE strikes, 22 contracts total per index)
        for both the 09:15 Positive Tick and the new Sum Signal (Score = CE % - PE %) strategy.
        """
        if underlying_key not in self.ce_by_underlying or not self.ce_by_underlying[underlying_key]:
            self.load_contracts_if_needed(underlying_key)

        ce_dict = self.ce_by_underlying.get(underlying_key) or (self.ce_by_strike if underlying_key == NIFTY_KEY else {})
        pe_dict = self.pe_by_underlying.get(underlying_key) or (self.pe_by_strike if underlying_key == NIFTY_KEY else {})

        step = float(self.index_underlyings.get(underlying_key, {}).get("step") or 50.0)
        atm_strike = round(spot / step) * step

        # ATM +- 5: 11 strikes total (5 below ATM, ATM, 5 above ATM)
        all_ce_strikes = sorted(ce_dict.keys())
        all_pe_strikes = sorted(pe_dict.keys())

        ce_basket_keys: list[str] = []
        pe_basket_keys: list[str] = []
        keys: list[str] = []

        if all_ce_strikes:
            nearest_idx = min(range(len(all_ce_strikes)), key=lambda i: abs(all_ce_strikes[i] - atm_strike))
            start_i = max(0, nearest_idx - 5)
            end_i = min(len(all_ce_strikes), nearest_idx + 6)
            for s in all_ce_strikes[start_i:end_i]:
                c = ce_dict.get(s)
                if c and c.get("instrument_key"):
                    keys.append(c["instrument_key"])
                    ce_basket_keys.append(c["instrument_key"])

        if all_pe_strikes:
            nearest_idx = min(range(len(all_pe_strikes)), key=lambda i: abs(all_pe_strikes[i] - atm_strike))
            start_i = max(0, nearest_idx - 5)
            end_i = min(len(all_pe_strikes), nearest_idx + 6)
            for s in all_pe_strikes[start_i:end_i]:
                p = pe_dict.get(s)
                if p and p.get("instrument_key"):
                    keys.append(p["instrument_key"])
                    pe_basket_keys.append(p["instrument_key"])

        if underlying_key in self.index_underlyings:
            self.index_underlyings[underlying_key]["planned_keys"] = keys
            self.index_underlyings[underlying_key]["ce_basket_keys"] = ce_basket_keys
            self.index_underlyings[underlying_key]["pe_basket_keys"] = pe_basket_keys
            self.index_underlyings[underlying_key]["atm_strike"] = atm_strike
            if self.index_underlyings[underlying_key]["state"] == "SETUP":
                self.index_underlyings[underlying_key]["state"] = "PLANNING"

            self.index_underlyings[underlying_key]["atm_plus_minus_keys"] = list(keys)

        if underlying_key == NIFTY_KEY:
            self.planned_keys = keys
            self.ce_basket_keys = ce_basket_keys
            self.pe_basket_keys = pe_basket_keys
            self.atm_strike = atm_strike
            if self.state == "SETUP":
                self.state = "PLANNING"

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
            self.feed.subscribe(keys, target="indices")

        return keys

    def plan_all_indices(self) -> None:
        """Plans 8 ITM contracts for all enabled indices if spot price is known."""
        for ukey, idx_st in self.index_underlyings.items():
            if idx_st.get("enabled", True):
                spot = idx_st.get("spot_price")
                if spot and spot > 0 and not idx_st.get("planned_keys"):
                    self.plan_8_itm_contracts(spot, underlying_key=ukey)

    # -------------------------------------------------------------------------
    # Timeline Step 1: Lock Time (09:14:55)
    # -------------------------------------------------------------------------

    def lock_contracts(self, underlying_key: str | None = None) -> None:
        """Lock the 8 ITM contracts at 09:14:55 for specified index or all indices."""
        with self._lock:
            targets = [underlying_key] if underlying_key else list(self.index_underlyings.keys())
            for ukey in targets:
                idx_state = self.index_underlyings.get(ukey)
                if not idx_state or not idx_state.get("enabled", True):
                    continue
                if idx_state.get("snapshot_taken") and idx_state.get("locked_keys"):
                    continue

                if not idx_state.get("planned_keys") and idx_state.get("spot_price"):
                    self.plan_8_itm_contracts(idx_state["spot_price"], underlying_key=ukey)

                p_keys = idx_state.get("planned_keys") or []
                if not p_keys and ukey == NIFTY_KEY and self.planned_keys:
                    p_keys = list(self.planned_keys)
                    idx_state["planned_keys"] = p_keys

                if not p_keys:
                    LOGGER.warning("Cannot lock contracts for %s: no planned strikes available.", ukey)
                    continue

                idx_state["locked_keys"] = list(p_keys)
                for k in idx_state["locked_keys"]:
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

                idx_state["state"] = "LOCKED"
                if ukey == NIFTY_KEY:
                    self.locked_keys = list(idx_state["locked_keys"])
                    self.locked_at_ms = int(time.time() * 1000)
                    self.state = "LOCKED"
                if self.feed and hasattr(self.feed, "subscribe") and idx_state["locked_keys"]:
                    try:
                        self.feed.subscribe(idx_state["locked_keys"], target="indices")
                    except Exception as exc:
                        LOGGER.debug("Could not subscribe locked keys for %s: %s", ukey, exc)
                LOGGER.info("LOCKED 6 contracts (ATM +-3) for %s at 09:14:55: %s", ukey, idx_state["locked_keys"])

    def lock_all_indices(self) -> None:
        self.lock_contracts()

    # -------------------------------------------------------------------------
    # Timeline Step 2: 09:15:00 Snapshot (Section 2 & 3)
    # -------------------------------------------------------------------------

    def take_snapshot(self, underlying_key: str | None = None) -> None:
        """
        At 09:15:00, take the snapshot of the 6 contracts (ATM +-3): LTP, OI, bid, ask.
        NO TRADING IS ALLOWED AT 09:15:00.
        """
        with self._lock:
            targets = [underlying_key] if underlying_key else list(self.index_underlyings.keys())
            for ukey in targets:
                idx_state = self.index_underlyings.get(ukey)
                if not idx_state or not idx_state.get("enabled", True):
                    continue
                if idx_state.get("snapshot_taken") and idx_state.get("locked_keys"):
                    continue

                if not idx_state.get("locked_keys"):
                    if idx_state.get("spot_price"):
                        self.lock_contracts(underlying_key=ukey)
                    elif ukey == NIFTY_KEY and self.locked_keys:
                        idx_state["locked_keys"] = list(self.locked_keys)

                l_keys = list(idx_state.get("locked_keys") or [])
                for ak in (idx_state.get("atm_plus_minus_keys") or []):
                    if ak not in l_keys:
                        l_keys.append(ak)
                if not l_keys:
                    continue

                idx_snap = {}
                for k in l_keys:
                    c = self.hot_cache.get(k, {})
                    ltp = float(c.get("ltp") or c.get("prev_close") or 100.0)
                    oi = float(c.get("oi") or c.get("prev_oi") or 0.0)
                    bid = float(c.get("bid_price") or 0.0)
                    ask = float(c.get("ask_price") or 0.0)
                    if oi == 0.0 or ltp == 100.0:
                        try:
                            db_tick = self.database.get_latest_tick(k)
                            if db_tick:
                                ltp = float(db_tick.get("ltp") or ltp)
                                oi = float(db_tick.get("oi") or oi)
                                bid = float(db_tick.get("bid_price") or bid)
                                ask = float(db_tick.get("ask_price") or ask)
                        except Exception:
                            pass
                    idx_snap[k] = {
                        "instrument_key": k,
                        "trading_symbol": c.get("trading_symbol") or k,
                        "strike": float(c.get("strike") or 0.0),
                        "side": str(c.get("side") or "").upper(),
                        "ltp": ltp,
                        "oi": oi,
                        "bid": bid,
                        "ask": ask,
                    }

                # Phase 2: 09:15 Futures Snapshot (price, OI, volume)
                fut_k = idx_state.get("futures_key")
                if fut_k:
                    fut_c = self.hot_cache.get(fut_k, {})
                    fut_ltp = float(fut_c.get("ltp") or fut_c.get("prev_close") or idx_state.get("futures_ltp") or 0.0)
                    fut_oi = float(fut_c.get("oi") or fut_c.get("prev_oi") or idx_state.get("futures_oi") or 0.0)
                    idx_state["futures_snap_ltp"] = fut_ltp
                    idx_state["futures_snap_oi"] = fut_oi
                    idx_state["futures_snap_vol"] = float(fut_c.get("volume") or 0.0)
                    LOGGER.info("SNAPSHOT FUTURES for %s: LTP=%.2f, OI=%.0f", ukey, fut_ltp, fut_oi)

                idx_state["snapshot"] = idx_snap
                idx_state["snapshot_taken"] = True
                idx_state["state"] = "ARMED"

                # Lock the 09:15:00 Sum Signal Baseline (11 CE and 11 PE strikes)
                idx_state["baseline_prices"] = {}
                for k in l_keys:
                    c = self.hot_cache.get(k, {})
                    snap_p = float(c.get("ltp") or c.get("ref_ltp") or 0.0)
                    if snap_p <= 0.0:
                        bid_p = float(c.get("bid_price") or 0.0)
                        ask_p = float(c.get("ask_price") or 0.0)
                        if bid_p > 0 and ask_p > 0:
                            snap_p = round((bid_p + ask_p) / 2.0, 2)
                        else:
                            snap_p = float(c.get("prev_close") or 100.0)
                    idx_state["baseline_prices"][k] = snap_p

                ce_b_keys = idx_state.get("ce_basket_keys") or [k for k in l_keys if self.hot_cache.get(k, {}).get("side") == "CE"]
                pe_b_keys = idx_state.get("pe_basket_keys") or [k for k in l_keys if self.hot_cache.get(k, {}).get("side") == "PE"]

                ce_base_sum = sum(idx_state["baseline_prices"].get(k, 0.0) for k in ce_b_keys)
                pe_base_sum = sum(idx_state["baseline_prices"].get(k, 0.0) for k in pe_b_keys)
                idx_state["ce_baseline_sum"] = round(ce_base_sum, 2)
                idx_state["pe_baseline_sum"] = round(pe_base_sum, 2)
                idx_state["baseline_locked"] = True
                LOGGER.info("SUM SIGNAL BASELINE LOCKED at 09:15 for %s: CE Sum = %.2f, PE Sum = %.2f across %d strikes.", ukey, ce_base_sum, pe_base_sum, len(ce_b_keys) + len(pe_b_keys))

                if ukey == NIFTY_KEY:
                    self.snapshot = dict(idx_snap)
                    self.snapshot_taken = True
                    self.snapshot_at_ms = int(time.time() * 1000)
                    self.state = "ARMED"

                LOGGER.info("SNAPSHOT TAKEN at 09:15:00 for %s (%d contracts). State -> ARMED (Watching for first positive tick).", ukey, len(idx_snap))

    def snapshot_all_indices(self) -> None:
        self.take_snapshot()

    def arm_all_indices(self) -> None:
        """Arms all enabled indices at 09:15:00 for First Positive Tick strategy."""
        with self._lock:
            any_armed = False
            for ukey, idx_state in self.index_underlyings.items():
                if idx_state.get("enabled", True) and idx_state.get("state") in ("LOCKED", "DATA_COLLECTION", "PLANNING", "SETUP"):
                    if not idx_state.get("locked_keys") and idx_state.get("spot_price"):
                        self.lock_contracts(underlying_key=ukey)
                    idx_state["state"] = "ARMED"
                    any_armed = True
            if self.state in ("LOCKED", "DATA_COLLECTION", "PLANNING", "SETUP"):
                self.state = "ARMED"
                any_armed = True
            if any_armed:
                LOGGER.info("ARMED ALL INDICES for First Positive Tick strategy at 09:15:00.")

    # -------------------------------------------------------------------------
    # SUM SIGNAL STRATEGY (Dr. Vijay Bhilwade Specification)
    # Score = CE % - PE % across ATM +- 5 strikes (11 CE and 11 PE strikes)
    # Baseline locked at 09:15:00. Confirmation requires 5-minute continuous hold.
    # -------------------------------------------------------------------------

    def evaluate_sum_signal(
        self,
        underlying_key: str = NIFTY_KEY,
        trigger_ms: int = 0,
        force: bool = False,
        require_consecutive: bool = False,
    ) -> dict[str, Any] | None:
        """
        Calculates the real-time Sum Signal for an index using WebSocket ticks:
        - Basket: ATM +- 5 strikes (11 CE and 11 PE contracts).
        - CE % = ((Sum of CE LTPs - Baseline CE Sum) / Baseline CE Sum) * 100
        - PE % = ((Sum of PE LTPs - Baseline PE Sum) / Baseline PE Sum) * 100
        - Score = CE % - PE %
        - Signal:
            Bullish: CE % > +2.0% AND PE % < -2.0% -> 'CE Buy'
            Bearish: PE % > +2.0% AND CE % < -2.0% -> 'PE Buy'
            Neutral: within +-2.0% buffer or mixed -> 'Neutral'
        - Whipsaw Control: Signal must hold continuously for 300 seconds (5 minutes).
          If it breaks to Neutral or opposite, timer resets to 0.
        - Execution: On confirmation, enters tightest spread strike on signal side
          with -20% Stop-Loss and +40% Target, with Stepped Trailing Stop-Loss.
        """
        with self._lock:
            now_dt = datetime.now(IST)
            now_time_hms = now_dt.strftime("%H:%M:%S")
            now_ts = time.time()

            idx_state = self.index_underlyings.get(underlying_key)
            if not idx_state:
                idx_state = self.index_underlyings.get(NIFTY_KEY)
                underlying_key = NIFTY_KEY
            if not idx_state or not idx_state.get("enabled", True):
                return None

            # Get or auto-plan CE and PE baskets (11 strikes each)
            ce_keys = idx_state.get("ce_basket_keys") or []
            pe_keys = idx_state.get("pe_basket_keys") or []

            if not ce_keys or not pe_keys:
                spot = float(idx_state.get("spot_price") or self.spot_price or 0.0)
                if spot > 0:
                    self.plan_8_itm_contracts(spot, underlying_key=underlying_key)
                    ce_keys = idx_state.get("ce_basket_keys") or []
                    pe_keys = idx_state.get("pe_basket_keys") or []

            if not ce_keys or not pe_keys:
                # Fallback to locked keys if baskets still empty
                l_keys = idx_state.get("locked_keys") or []
                ce_keys = [k for k in l_keys if "CE" in self.hot_cache.get(k, {}).get("trading_symbol", "") or self.hot_cache.get(k, {}).get("side") == "CE"]
                pe_keys = [k for k in l_keys if "PE" in self.hot_cache.get(k, {}).get("trading_symbol", "") or self.hot_cache.get(k, {}).get("side") == "PE"]

            # Compute current sums from live WebSocket hot cache
            ce_curr_sum = 0.0
            for k in ce_keys:
                c = self.hot_cache.get(k)
                if c:
                    p = float(c.get("ltp") or c.get("bid_price") or c.get("ref_ltp") or 0.0)
                    ce_curr_sum += p

            pe_curr_sum = 0.0
            for k in pe_keys:
                c = self.hot_cache.get(k)
                if c:
                    p = float(c.get("ltp") or c.get("bid_price") or c.get("ref_ltp") or 0.0)
                    pe_curr_sum += p

            idx_state["ce_current_sum"] = round(ce_curr_sum, 2)
            idx_state["pe_current_sum"] = round(pe_curr_sum, 2)

            # Retrieve baseline sums locked at 09:15:00
            ce_base = float(idx_state.get("ce_baseline_sum") or 0.0)
            pe_base = float(idx_state.get("pe_baseline_sum") or 0.0)

            # Auto-lock baseline if not locked yet and market is open or forced
            market_open_hms = getattr(self, "market_open_time", getattr(self, "snapshot_time", "09:15:00"))
            if not idx_state.get("baseline_locked") and (now_time_hms >= market_open_hms or force):
                if ce_curr_sum > 0 or pe_curr_sum > 0:
                    idx_state["ce_baseline_sum"] = round(ce_curr_sum, 2)
                    idx_state["pe_baseline_sum"] = round(pe_curr_sum, 2)
                    idx_state["baseline_locked"] = True
                    ce_base = round(ce_curr_sum, 2)
                    pe_base = round(pe_curr_sum, 2)
                    LOGGER.info("Sum Signal baseline auto-locked for %s: CE=%.2f, PE=%.2f", underlying_key, ce_base, pe_base)

            # Compute percentage changes and Sum Signal Score
            if ce_base > 0:
                ce_pct = round(((ce_curr_sum - ce_base) / ce_base) * 100.0, 2)
            else:
                ce_pct = 0.0

            if pe_base > 0:
                pe_pct = round(((pe_curr_sum - pe_base) / pe_base) * 100.0, 2)
            else:
                pe_pct = 0.0

            score = round(ce_pct - pe_pct, 2)

            idx_state["ce_pct"] = ce_pct
            idx_state["pe_pct"] = pe_pct
            idx_state["score"] = score

            # Signal classification as per Dr. Bhilwade's document
            if ce_pct > 2.0 and pe_pct < -2.0:
                raw_signal = "CE Buy"
            elif pe_pct > 2.0 and ce_pct < -2.0:
                raw_signal = "PE Buy"
            else:
                raw_signal = "Neutral"

            idx_state["raw_signal"] = raw_signal

            # Whipsaw / 5-Minute Continuous Hold Tracking
            whipsaw_sec = float(idx_state.get("whipsaw_sec", 300.0))
            if raw_signal == "Neutral":
                idx_state["candidate_signal"] = None
                idx_state["candidate_start_time"] = None
                idx_state["hold_elapsed_sec"] = 0.0
                idx_state["confirmed_signal"] = None
            elif raw_signal in ("CE Buy", "PE Buy"):
                if idx_state.get("candidate_signal") == raw_signal:
                    start_t = idx_state.get("candidate_start_time") or now_ts
                    elapsed = round(now_ts - start_t, 1)
                    idx_state["hold_elapsed_sec"] = elapsed
                    if elapsed >= whipsaw_sec:
                        idx_state["confirmed_signal"] = raw_signal
                else:
                    # Raw signal flipped or started fresh
                    idx_state["candidate_signal"] = raw_signal
                    idx_state["candidate_start_time"] = now_ts
                    idx_state["hold_elapsed_sec"] = 0.0
                    idx_state["confirmed_signal"] = None

            confirmed = idx_state.get("confirmed_signal")
            elapsed_sec = idx_state.get("hold_elapsed_sec", 0.0)

            res = {
                "decision": confirmed or ("WAIT" if raw_signal != "Neutral" else "NEUTRAL"),
                "raw_signal": raw_signal,
                "confirmed_signal": confirmed,
                "score": score,
                "ce_pct": ce_pct,
                "pe_pct": pe_pct,
                "ce_current_sum": round(ce_curr_sum, 2),
                "pe_current_sum": round(pe_curr_sum, 2),
                "ce_baseline_sum": round(ce_base, 2),
                "pe_baseline_sum": round(pe_base, 2),
                "hold_elapsed_sec": elapsed_sec,
                "whipsaw_sec": whipsaw_sec,
                "reason": f"Score {score:+0.2f}% (CE: {ce_pct:+0.2f}%, PE: {pe_pct:+0.2f}%). Hold: {int(elapsed_sec)}/{int(whipsaw_sec)}s",
                "summary_line": f"{idx_state['name']} SUM SIGNAL: Score {score:+0.2f}% | CE {ce_pct:+0.2f}% vs PE {pe_pct:+0.2f}% -> {confirmed or raw_signal}",
            }
            idx_state["last_evaluation"] = res
            if underlying_key == NIFTY_KEY:
                self.last_evaluation = res

            # Check if active position needs reversal
            if idx_state.get("current_position"):
                pos = idx_state["current_position"]
                if "CE" in pos.get("side", "") and confirmed == "PE Buy":
                    LOGGER.info("CONFIRMED REVERSAL to PE Buy for %s. Closing CE position at live bid.", underlying_key)
                    self._exit_single_trade(pos, "SIGNAL_REVERSAL_PE", underlying_key)
                    return self._enter_sum_signal_trade(underlying_key=underlying_key, side="PE")
                elif "PE" in pos.get("side", "") and confirmed == "CE Buy":
                    LOGGER.info("CONFIRMED REVERSAL to CE Buy for %s. Closing PE position at live bid.", underlying_key)
                    self._exit_single_trade(pos, "SIGNAL_REVERSAL_CE", underlying_key)
                    return self._enter_sum_signal_trade(underlying_key=underlying_key, side="CE")
                return None

            # Fresh Entry Check
            if confirmed in ("CE Buy", "PE Buy"):
                side = "CE" if confirmed == "CE Buy" else "PE"

                # Check 15-minute cooldown for same direction
                cooldown_until = idx_state.get("cooldown_until")
                if cooldown_until and now_dt < cooldown_until:
                    if idx_state.get("last_closed_side") == side:
                        LOGGER.debug("Underlying %s in 15-min cooldown for %s until %s", underlying_key, side, cooldown_until)
                        return None

                # Check daily trade limits
                max_t = idx_state.get("max_trades_per_day", self.max_trades_per_day)
                if idx_state.get("today_trades_count", 0) >= max_t:
                    return None

                # Check daily loss limit
                loss_lim = idx_state.get("daily_loss_limit_inr", self.daily_loss_limit_inr)
                if idx_state.get("realized_pnl", 0.0) <= -loss_lim:
                    return None

                # Check trading hours (09:18 to 14:45)
                if not force and (now_time_hms < self.entry_start_time or now_time_hms >= self.entry_end_time):
                    return None

                trade = self._enter_sum_signal_trade(underlying_key=underlying_key, side=side)
                return trade

            return None

    def _enter_sum_signal_trade(self, underlying_key: str, side: str) -> dict[str, Any] | None:
        """Executes a market entry on confirmed Sum Signal for the selected index."""
        idx_st = self.index_underlyings.get(underlying_key)
        if not idx_st:
            return None

        # Select strike: tightest spread candidate among ITM / ATM basket
        basket_keys = idx_st.get("ce_basket_keys" if side == "CE" else "pe_basket_keys") or idx_st.get("locked_keys")
        winner = self._select_best_strike(side, locked_keys=basket_keys)
        if not winner:
            winner = self._select_atm_strike(side, underlying_key=underlying_key)
        if not winner:
            LOGGER.warning("No candidate strike found for %s %s trade", underlying_key, side)
            return None

        ask = float(winner.get("ask_price") or 0.0)
        ltp = float(winner.get("ltp") or 0.0)
        bid = float(winner.get("bid_price") or 0.0)
        ref_price = ask if ask > 0 else (ltp if ltp > 0 else 0.0)
        if ref_price <= 0.0:
            LOGGER.warning("ORDER REJECTED: No live quotes for %s (%s)", winner.get("trading_symbol"), winner.get("instrument_key"))
            return None

        limit_price = round(round((ref_price + self.order_buffer) / 0.05) * 0.05, 2)
        entry_price = ref_price if self.mode == "paper" else limit_price
        sl_price = round(entry_price * 0.80, 2)   # -20% SL as per Dr. Bhilwade's document
        tgt_price = round(entry_price * 1.40, 2)  # +40% Target

        lots = self.index_lots.get(underlying_key, idx_st.get("lots", 1))
        lot_sz = idx_st.get("lot_size") or self.index_lot_sizes.get(underlying_key, self.lot_size)
        total_qty = lots * lot_sz

        now_dt = datetime.now(IST)
        trade_id = f"SUM-{now_dt.strftime('%m%d%H%M%S')}-{self._trade_counter}"
        self._trade_counter += 1

        trade = {
            "trade_id": trade_id,
            "trade_date": now_dt.strftime("%Y-%m-%d"),
            "entry_path": "SUM_SIGNAL",
            "stop_loss_price": sl_price,
            "target_price": tgt_price,
            "side": f"BUY_{side}",
            "symbol": winner.get("trading_symbol") or f"{idx_st['name']} {winner.get('strike')} {side}",
            "instrument_key": winner["instrument_key"],
            "underlying_key": underlying_key,
            "strike": winner.get("strike"),
            "lots": lots,
            "lot_size": total_qty,
            "entry_time_ist": now_dt.strftime("%H:%M:%S.%f")[:-3],
            "entry_price": entry_price,
            "entry_timestamp_ms": int(time.time() * 1000),
            "exit_time_ist": None,
            "exit_price": None,
            "exit_timestamp_ms": None,
            "exit_reason": None,
            "pnl_points": 0.0,
            "net_pnl": 0.0,
            "status": "OPEN",
            "spot_price": idx_st.get("spot_price") or self.spot_price,
            "atm_strike": winner.get("strike"),
            "current_price": entry_price,
            "score": idx_st.get("score", 0.0),
            "ce_pct": idx_st.get("ce_pct", 0.0),
            "pe_pct": idx_st.get("pe_pct", 0.0),
            "decision": f"{side} Buy",
            "highest_price": entry_price,
            "trailing_active": False,
            "bot_latency_ms": 0.25,
            "network_latency_ms": 0.0,
        }

        idx_st["current_position"] = trade
        idx_st["today_trades_count"] = idx_st.get("today_trades_count", 0) + 1
        idx_st["state"] = "HOLDING"
        if underlying_key == NIFTY_KEY:
            self.current_position = trade
            self.state = "HOLDING"
            self.today_trades_count += 1

        LOGGER.info(
            "SUM SIGNAL TRADE EXECUTED for %s: %s at %.2f (SL: %.2f [-20%%], TGT: %.2f [+40%%], Score: %s)",
            underlying_key, trade["symbol"], entry_price, sl_price, tgt_price, idx_st.get("score")
        )
        return trade

    def evaluate_oi_buildup(
        self,
        underlying_key: str = NIFTY_KEY,
        trigger_ms: int = 0,
        force: bool = False,
        require_consecutive: bool = False,
    ) -> dict[str, Any] | None:
        """Backwards compatibility wrapper forwarding to evaluate_sum_signal."""
        return self.evaluate_sum_signal(underlying_key=underlying_key, trigger_ms=trigger_ms, force=force)

    def _select_atm_strike(self, side: str, underlying_key: str = NIFTY_KEY) -> dict[str, Any] | None:
        """Selects the ATM option contract of the chosen side closest to spot price."""
        idx_st = self.index_underlyings.get(underlying_key)
        spot = idx_st.get("spot_price") if idx_st else self.spot_price
        if not spot or spot <= 0:
            return self._select_best_strike(side)

        target_keys = (
            (idx_st.get("atm_plus_minus_keys") or [])
            + (idx_st.get("locked_keys") or [])
            + (idx_st.get("ce_basket_keys" if side == "CE" else "pe_basket_keys") or [])
        )
        candidates = [
            e for e in self.hot_cache.values()
            if (not target_keys or e.get("instrument_key") in target_keys)
            and e.get("side") == side
            and e.get("underlying_key") == underlying_key
        ]
        if not candidates:
            candidates = [e for e in self.hot_cache.values() if e.get("side") == side and e.get("underlying_key") == underlying_key]

        if not candidates:
            return None

        candidates.sort(key=lambda x: (abs(float(x.get("strike") or 0.0) - spot), x.get("spread_pct") or 999.0))
        return candidates[0]

    def evaluate_all_active_underlyings(self) -> list[dict[str, Any]]:
        """Evaluates all enabled indices for Sum Signal trades."""
        trades = []
        now_dt = datetime.now(IST)
        now_hms = now_dt.strftime("%H:%M:%S")
        if not (self.entry_start_time <= now_hms < self.entry_end_time):
            return trades

        with self._lock:
            for ukey, idx_st in self.index_underlyings.items():
                if idx_st.get("enabled", True):
                    max_t = idx_st.get("max_trades_per_day", self.max_trades_per_day)
                    if idx_st.get("current_position") is None and idx_st.get("today_trades_count", 0) < max_t:
                        t = self.evaluate_sum_signal(underlying_key=ukey)
                        if t and isinstance(t, dict) and t.get("status") == "OPEN":
                            trades.append(t)
        return trades

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
        if inst_key in self.index_underlyings:
            self.last_tick_time = time.time()
            idx_st = self.index_underlyings[inst_key]
            idx_st["spot_price"] = ltp
            if tick.get("prev_close") is not None and not idx_st.get("prev_close"):
                idx_st["prev_close"] = float(tick["prev_close"])
            if tick.get("open") is not None and not idx_st.get("open_price"):
                idx_st["open_price"] = float(tick["open"])
            if tick.get("average_price"):
                idx_st["vwap"] = float(tick["average_price"])

            # Phase 1: Pre-open previous-close check before 09:15:00
            if self.re_eval_min_interval_sec > 0 and now_time_hms < "09:15:00":
                pc = idx_st.get("prev_close")
                if pc and pc > 0 and ltp > 0:
                    chg = round(ltp - pc, 2)
                    if chg != 0.0 and not idx_st.get("pre_open_error"):
                        idx_st["pre_open_error"] = True
                        msg = f"PRE-OPEN ANOMALY on {idx_st['name']}: LTP {ltp} != Prev Close {pc} (Change: {chg:+0.2f})."
                        LOGGER.warning(msg)
                        self.add_alert("PRE_OPEN_ANOMALY", msg, level="WARNING", details={"underlying": inst_key, "ltp": ltp, "prev_close": pc, "change": chg})

            if inst_key == NIFTY_KEY:
                self.spot_price = ltp
            with self._lock:
                market_open_hms = getattr(self, "market_open_time", getattr(self, "snapshot_time", "09:15:00"))
                # 09:00 to 09:14:55: Dynamic planning around spot
                if idx_st.get("state") in ("SETUP", "PLANNING"):
                    if now_time_hms < self.lock_time:
                        idx_st["state"] = "PLANNING"
                        self.plan_8_itm_contracts(ltp, underlying_key=inst_key)
                    elif self.lock_time <= now_time_hms < market_open_hms:
                        if idx_st.get("state") != "LOCKED":
                            self.lock_contracts(underlying_key=inst_key)
                    elif now_time_hms >= market_open_hms:
                        if idx_st.get("state") not in ("ARMED", "ORDER_SENT", "HOLDING", "CLOSED"):
                            if not idx_st.get("locked_keys"):
                                self.lock_contracts(underlying_key=inst_key)
                            self.take_snapshot(underlying_key=inst_key)
                            idx_st["state"] = "ARMED"
                            if inst_key == NIFTY_KEY:
                                self.state = "ARMED"
                            LOGGER.info("09:15:00 MARKET OPEN for %s: State -> ARMED (Watching for first positive tick).", inst_key)

                # Schedule checks
                if now_time_hms >= market_open_hms and idx_st.get("state") in ("LOCKED", "PLANNING", "SETUP", "DATA_COLLECTION"):
                    if not idx_st.get("locked_keys"):
                        self.lock_contracts(underlying_key=inst_key)
                    self.take_snapshot(underlying_key=inst_key)
                    idx_st["state"] = "ARMED"
                    if inst_key == NIFTY_KEY:
                        self.state = "ARMED"
                    LOGGER.info("09:15:00 REACHED for %s: State -> ARMED (Watching for first positive tick).", inst_key)

            return None

        # --- 1b. STOCK SPOT TICK ---
        stock_sym_for_spot = next((sym for sym, d in self.stock_underlyings.items() if d.get("key") == inst_key), None)
        if stock_sym_for_spot:
            self.last_tick_time = time.time()
            st = self.stock_underlyings[stock_sym_for_spot]
            st["spot_price"] = ltp
            with self._lock:
                if st.get("state") in ("SETUP", "PLANNING"):
                    if now_time_hms < self.lock_time:
                        last_p = float(st.get("_last_planned_spot") or 0.0)
                        step = float(st.get("strike_step") or 10.0)
                        if not st.get("planned_keys") or abs(ltp - last_p) >= step:
                            st["_last_planned_spot"] = ltp
                            st["state"] = "PLANNING"
                            self.plan_stock_contracts(stock_sym_for_spot, ltp)
                    elif self.lock_time <= now_time_hms < self.snapshot_time:
                        if st.get("state") != "LOCKED":
                            self.lock_stock_contracts(stock_sym_for_spot)
                    elif now_time_hms >= self.snapshot_time:
                        if not st.get("snapshot_taken"):
                            self.take_stock_snapshot(stock_sym_for_spot)

                if now_time_hms >= self.snapshot_time and not st.get("snapshot_taken"):
                    self.take_stock_snapshot(stock_sym_for_spot)

                # Path 1 check: First tick above/below 09:15 open price for ★★★+ stocks
                trigger_stock_side = None
                if (
                    st.get("state") == "ARMED"
                    and not st.get("path1_done")
                    and not st.get("path1_evaluating")
                    and not st.get("gap_against")
                    and st.get("current_position") is None
                    and st.get("today_trades_count", 0) < self.stock_max_trades_per_day
                    and st.get("watchlist_rank") in ("★★★", "★★★★")
                ):
                    open_p = float(st.get("open_price") or 0.0)
                    dir_pref = str(st.get("watchlist_direction") or "BULLISH").upper()
                    if open_p > 0 and ltp > 0:
                        if dir_pref == "BULLISH" and ltp > open_p:
                            trigger_stock_side = "CE"
                            st["path1_evaluating"] = True
                        elif dir_pref == "BEARISH" and ltp < open_p:
                            trigger_stock_side = "PE"
                            st["path1_evaluating"] = True

            if trigger_stock_side:
                # Dispatched outside self._lock so Priority 0 index ticks experience ZERO lock contention!
                return self._execute_stock_path1_entry(stock_sym_for_spot, trigger_stock_side, trigger_ms=ts_ms)
            return None

        # --- 1c. STOCK FUTURES TICK ---
        stock_sym_for_fut = next((sym for sym, d in self.stock_underlyings.items() if d.get("futures_key") == inst_key), None)
        if stock_sym_for_fut:
            self.last_tick_time = time.time()
            st = self.stock_underlyings[stock_sym_for_fut]
            st["futures_ltp"] = ltp
            if tick.get("open_interest") is not None:
                st["futures_oi"] = float(tick["open_interest"])
            if tick.get("prev_close") is not None and not st.get("futures_prev_close"):
                st["futures_prev_close"] = float(tick["prev_close"])

            # Live futures buildup classification
            pc = st.get("futures_prev_close") or st.get("futures_snap_ltp")
            oi_curr = st.get("futures_oi")
            oi_prev = st.get("futures_prev_oi") or st.get("futures_snap_oi")
            if ltp > 0 and pc and pc > 0 and oi_curr and oi_prev and oi_prev > 0:
                dp = (ltp - pc) / pc
                doi = (oi_curr - oi_prev) / oi_prev
                thresh = 0.001
                if abs(dp) < thresh or abs(doi) < thresh:
                    st["futures_buildup"] = "Neutral"
                elif dp > 0 and doi > 0:
                    st["futures_buildup"] = "LB"
                elif dp < 0 and doi > 0:
                    st["futures_buildup"] = "SB"
                elif dp > 0 and doi < 0:
                    st["futures_buildup"] = "SC"
                elif dp < 0 and doi < 0:
                    st["futures_buildup"] = "LU"

            # Check Futures Flip exit if holding position
            with self._lock:
                if st.get("state") == "HOLDING" and st.get("current_position"):
                    pos = st["current_position"]
                    fut_bu = st.get("futures_buildup", "Neutral")
                    if "CE" in pos.get("side", "") and fut_bu in ("SB", "LU"):
                        return self._exit_single_trade(pos, "FUTURES_FLIP_BEARISH", stock_sym_for_fut)
                    elif "PE" in pos.get("side", "") and fut_bu in ("LB", "SC"):
                        return self._exit_single_trade(pos, "FUTURES_FLIP_BULLISH", stock_sym_for_fut)
            return None

        # --- 1d. INDEX FUTURES TICK (Phase 2 & 3) ---
        idx_sym_for_fut = next((ukey for ukey, d in self.index_underlyings.items() if d.get("futures_key") == inst_key), None)
        if idx_sym_for_fut:
            self.last_tick_time = time.time()
            idx_st = self.index_underlyings[idx_sym_for_fut]
            idx_st["futures_ltp"] = ltp
            if tick.get("open_interest") is not None:
                idx_st["futures_oi"] = float(tick["open_interest"])
            if tick.get("prev_close") is not None and not idx_st.get("futures_prev_close"):
                idx_st["futures_prev_close"] = float(tick["prev_close"])
            if tick.get("average_price"):
                idx_st["futures_vwap"] = float(tick["average_price"])

            # Live futures buildup classification
            pc = idx_st.get("futures_prev_close") or idx_st.get("futures_snap_ltp")
            oi_curr = idx_st.get("futures_oi")
            oi_prev = idx_st.get("futures_prev_oi") or idx_st.get("futures_snap_oi")
            if ltp > 0 and pc and pc > 0 and oi_curr and oi_prev and oi_prev > 0:
                dp = (ltp - pc) / pc
                doi = (oi_curr - oi_prev) / oi_prev
                thresh = 0.001
                if abs(dp) < thresh or abs(doi) < thresh:
                    idx_st["futures_buildup"] = "Neutral"
                elif dp > 0 and doi > 0:
                    idx_st["futures_buildup"] = "LB"
                elif dp < 0 and doi > 0:
                    idx_st["futures_buildup"] = "SB"
                elif dp > 0 and doi < 0:
                    idx_st["futures_buildup"] = "SC"
                elif dp < 0 and doi < 0:
                    idx_st["futures_buildup"] = "LU"

            # Check Futures Flip exit if holding position on NIFTY/BANKNIFTY
            with self._lock:
                if idx_st.get("state") == "HOLDING" and idx_st.get("current_position"):
                    pos = idx_st["current_position"]
                    fut_bu = idx_st.get("futures_buildup", "Neutral")
                    if "CE" in pos.get("side", "") and fut_bu in ("SB", "LU"):
                        return self._exit_single_trade(pos, "FUTURES_FLIP_BEARISH", idx_sym_for_fut)
                    elif "PE" in pos.get("side", "") and fut_bu in ("LB", "SC"):
                        return self._exit_single_trade(pos, "FUTURES_FLIP_BULLISH", idx_sym_for_fut)
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

                e_ask = float(entry.get("ask_price") or 0.0)
                e_bid = float(entry.get("bid_price") or 0.0)
                if e_ask > 0 and e_bid > 0 and e_ask >= e_bid:
                    entry["spread_pct"] = round(
                        (e_ask - e_bid) / e_ask * 100, 2
                    )
                entry["last_quote_ms"] = int(time.time() * 1000)

            # If HOLDING: update unrealized P&L
            if self.state == "HOLDING" and self.current_position:
                if self.current_position.get("instrument_key") == inst_key:
                    e_bid = float(entry.get("bid_price") or 0.0) if entry else 0.0
                    current_price = e_bid if e_bid > 0 else float(ltp or 0.0)
                    if current_price > 0:
                        entry_price = float(self.current_position["entry_price"])
                        qty = int(self.current_position["lot_size"])
                        pts = round(current_price - entry_price, 2)
                        net = round(pts * qty, 2)
                        self.current_position["current_price"] = current_price
                        self.current_position["pnl_points"] = pts
                        self.current_position["net_pnl"] = net

            # Check if this contract belongs to a stock option underlying
            stock_sym = next((sym for sym, d in self.stock_underlyings.items() if inst_key in d.get("locked_keys", [])), None)
            if stock_sym:
                st = self.stock_underlyings[stock_sym]
                if not st.get("enabled", True):
                    return None

                # If holding: update unrealized P&L and monitor risk/exit rules
                if st.get("state") == "HOLDING" and st.get("current_position"):
                    pos = st["current_position"]
                    if pos.get("instrument_key") == inst_key:
                        e_bid = float(entry.get("bid_price") or 0.0) if entry else 0.0
                        current_price = e_bid if e_bid > 0 else float(ltp or 0.0)
                        if current_price > 0:
                            entry_price = float(pos["entry_price"])
                            qty = int(pos["lot_size"])
                            pts = round(current_price - entry_price, 2)
                            net = round(pts * qty, 2)
                            pos["current_price"] = current_price
                            pos["pnl_points"] = pts
                            pos["net_pnl"] = net

                            # 1. Stop-Loss Check (25% below entry price)
                            sl_p = float(pos.get("stop_loss_price") or (entry_price * 0.75))
                            if current_price <= sl_p:
                                return self._exit_single_trade(pos, "STOP_LOSS_25PCT", stock_sym)

                            # 2. Target Check (40% above entry price)
                            tgt_p = float(pos.get("target_price") or (entry_price * 1.40))
                            if current_price >= tgt_p:
                                return self._exit_single_trade(pos, "TARGET_40PCT", stock_sym)

                            # 3. Path 1 09:45 Cutoff (only for trades entered before 09:45)
                            entry_time = str(pos.get("entry_time_ist") or "")
                            if self.re_eval_min_interval_sec > 0 and pos.get("entry_path") == "PATH_1_0915" and now_time_hms >= "09:45:00" and entry_time < "09:45:00":
                                return self._exit_single_trade(pos, "TIME_EXIT_0945", stock_sym)

                            # 4. Futures Flip Check
                            fut_bu = st.get("futures_buildup", "Neutral")
                            if "CE" in pos.get("side", "") and fut_bu in ("SB", "LU"):
                                return self._exit_single_trade(pos, "FUTURES_FLIP_BEARISH", stock_sym)
                            elif "PE" in pos.get("side", "") and fut_bu in ("LB", "SC"):
                                return self._exit_single_trade(pos, "FUTURES_FLIP_BULLISH", stock_sym)

                            # 5. Signal Reversal Check
                            last_dec = st.get("last_evaluation", {}).get("decision")
                            if "CE" in pos.get("side", "") and last_dec == "PE Buy":
                                return self._exit_single_trade(pos, "SIGNAL_REVERSAL_PE", stock_sym)
                            elif "PE" in pos.get("side", "") and last_dec == "CE Buy":
                                return self._exit_single_trade(pos, "SIGNAL_REVERSAL_CE", stock_sym)

                if st.get("current_position") is not None or st.get("today_trades_count", 0) >= self.stock_max_trades_per_day:
                    return None

                if now_time_hms >= self.entry_start_time and now_time_hms < self.entry_end_time:
                    trade = self.evaluate_stock_oi_buildup(stock_sym, trigger_ms=ts_ms, require_consecutive=True)
                    return trade
                return None

            # Check if this contract belongs to an index option underlying
            idx_key = next(
                (ukey for ukey, d in self.index_underlyings.items()
                 if inst_key in d.get("locked_keys", [])
                 or inst_key in d.get("atm_plus_minus_keys", [])
                 or inst_key in d.get("ce_basket_keys", [])
                 or inst_key in d.get("pe_basket_keys", [])),
                None
            )
            if not idx_key and inst_key in self.locked_keys:
                idx_key = NIFTY_KEY

            if idx_key:
                idx_st = self.index_underlyings[idx_key]
                if not idx_st.get("enabled", True):
                    return None

                # If holding: update unrealized P&L and evaluate risk/exit rules
                if idx_st.get("state") == "HOLDING" and idx_st.get("current_position"):
                    pos = idx_st["current_position"]
                    if pos.get("instrument_key") == inst_key:
                        e_bid = float(entry.get("bid_price") or 0.0) if entry else 0.0
                        current_price = e_bid if e_bid > 0 else float(ltp or 0.0)
                        if current_price > 0:
                            entry_price = float(pos["entry_price"])
                            qty = int(pos.get("total_qty") or pos.get("lot_size") or 1)
                            pts = round(current_price - entry_price, 2)
                            net = round(pts * qty, 2)
                            pos["current_price"] = current_price
                            pos["pnl_points"] = pts
                            pos["net_pnl"] = net

                            # Stepped Trailing Stop-Loss calculation (Dr. Vijay Bhilwade specification)
                            # - Initial SL: -20% of entry price
                            # - Target: +40% of entry price
                            # - At +5% profit: SL moves to Cost (Break-even, 0% profit)
                            # - For every further +3% profit (at +8%, +11%, +14%...): raise SL by +2%
                            profit_pct = ((current_price - entry_price) / entry_price) * 100.0
                            if profit_pct >= 5.0:
                                steps = int((profit_pct - 5.0) // 3.0)
                                trail_pct = steps * 2.0
                                new_sl = round(entry_price * (1.0 + trail_pct / 100.0), 2)
                                if new_sl > float(pos.get("stop_loss_price") or 0.0):
                                    pos["stop_loss_price"] = new_sl
                                    pos["trailing_active"] = True
                                    LOGGER.info(
                                        "Stepped Trailing SL RAISED for %s: new SL %.2f (+%.1f%% lock) at profit +%.2f%%",
                                        pos.get("symbol"), new_sl, trail_pct, profit_pct
                                    )

                            # 1. Stop-Loss Check (sold at live bid)
                            sl_p = float(pos.get("stop_loss_price") or (entry_price * 0.80))
                            if current_price <= sl_p:
                                if pos.get("trailing_active"):
                                    reason = "TRAILING_STOP_LOSS"
                                elif pos.get("entry_path") == "PATH_1_0915":
                                    reason = "STOP_LOSS_25PCT"
                                else:
                                    reason = "STOP_LOSS_20PCT"
                                return self._exit_single_trade(pos, reason, idx_key)

                            # 2. Target Check (+40% above entry price, sold at live bid)
                            tgt_p = float(pos.get("target_price") or (entry_price * 1.40))
                            if current_price >= tgt_p:
                                return self._exit_single_trade(pos, "TARGET_40PCT", idx_key)

                            # 3. Path 1 (09:15 entry) 09:45:00 Cutoff Exit (only for trades entered under PATH_1_0915 before 09:45)
                            entry_time = str(pos.get("entry_time_ist") or "")
                            if self.re_eval_min_interval_sec > 0 and pos.get("entry_path") == "PATH_1_0915" and now_time_hms >= "09:45:00" and entry_time < "09:45:00":
                                return self._exit_single_trade(pos, "TIME_EXIT_0945", idx_key)

                            # 4. Confirmed Signal Reversal Check (Opposite signal confirmed after 5 minutes hold)
                            confirmed_sig = idx_st.get("confirmed_signal")
                            if "CE" in pos.get("side", "") and confirmed_sig == "PE Buy":
                                exited_trade = self._exit_single_trade(pos, "SIGNAL_REVERSAL_PE", idx_key)
                                self._enter_sum_signal_trade(underlying_key=idx_key, side="PE")
                                return exited_trade
                            elif "PE" in pos.get("side", "") and confirmed_sig == "CE Buy":
                                exited_trade = self._exit_single_trade(pos, "SIGNAL_REVERSAL_CE", idx_key)
                                self._enter_sum_signal_trade(underlying_key=idx_key, side="CE")
                                return exited_trade

                # Check daily limits: max 3 trades per index, daily loss limit, cutoff 14:45
                max_t = idx_st.get("max_trades_per_day", self.max_trades_per_day)
                if idx_st.get("current_position") is not None or idx_st.get("today_trades_count", 0) >= max_t:
                    return None
                if idx_st.get("realized_pnl", 0.0) <= -idx_st.get("daily_loss_limit_inr", self.daily_loss_limit_inr):
                    return None
                if self.re_eval_min_interval_sec > 0 and now_time_hms >= self.entry_end_time:
                    return None

                # =========================================================================
                # PATH 2: SUM SIGNAL EVALUATION (From 09:18:00 onwards)
                # =========================================================================
                if now_time_hms >= self.entry_start_time:
                    trade = self.evaluate_sum_signal(underlying_key=idx_key, trigger_ms=ts_ms)
                    if trade and isinstance(trade, dict) and trade.get("status") == "OPEN":
                        return trade

                # =========================================================================
                # PATH 1: FIRST POSITIVE TICK EVALUATION (Rule R8 strictly 09:15 to 09:18)
                # =========================================================================
                if self.re_eval_min_interval_sec > 0 and now_time_hms >= self.entry_start_time:
                    idx_st["path1_done"] = True
                    if idx_key == NIFTY_KEY:
                        self.path1_done = True
                elif idx_st.get("state") == "ARMED" and idx_st.get("today_trades_count", 0) == 0 and not idx_st.get("path1_done"):
                    ref = entry.get("ref_ltp")
                    if ref is None or float(ref or 0.0) <= 0.0:
                        if ltp > 0:
                            entry["ref_ltp"] = ltp
                            entry["ref_source"] = "initial_tick"
                        return None

                    # Rule R8: Fresh trade strictly above stored ref_ltp
                    if ltp > float(ref):
                        chosen_side = entry.get("side") or ("CE" if "CE" in entry.get("trading_symbol", "") else "PE")
                        LOGGER.info(
                            "FIRST POSITIVE TICK DETECTED on %s (%s) for %s! LTP %.2f > Ref %.2f. Side: %s",
                            entry.get("trading_symbol"),
                            inst_key,
                            idx_key,
                            ltp,
                            ref,
                            chosen_side,
                        )
                        idx_st["state"] = "ORDER_SENT"
                        if idx_key == NIFTY_KEY:
                            self.state = "ORDER_SENT"

                        winner = self._select_best_strike(chosen_side, locked_keys=idx_st.get("locked_keys"))
                        if not winner:
                            winner = entry

                        trigger_time_str = now_dt.strftime("%H:%M:%S.%f")[:-3]
                        trade = self._execute_positive_tick_entry(
                            winner=winner,
                            side=chosen_side,
                            trigger_time_str=trigger_time_str,
                            trigger_ms=ts_ms,
                            underlying_key=idx_key,
                            t_recv_perf=t_recv_perf,
                        )
                        return trade

                return None

            return None

    # -------------------------------------------------------------------------
    # Strike Selection (Tightest Spread among 4 ITM candidates of chosen side)
    # -------------------------------------------------------------------------

    def _select_best_strike(self, side: str, locked_keys: list[str] | None = None, spot: float | None = None) -> dict[str, Any] | None:
        """Section 6.2: Among the 4 locked ITM contracts on chosen side, pick tightest spread %."""
        target_keys = locked_keys if locked_keys is not None else self.locked_keys
        candidates = [
            e for e in self.hot_cache.values()
            if e.get("instrument_key") in target_keys and e.get("side") == side
        ]
        if not candidates:
            return None

        # Sort candidates so the ATM contract (nearest to spot or first in locked_keys) is at index 0
        if spot is not None and spot > 0:
            candidates.sort(key=lambda c: abs(float(c.get("strike") or 0.0) - spot))
        elif target_keys:
            key_order = {k: i for i, k in enumerate(target_keys)}
            candidates.sort(key=lambda c: key_order.get(c.get("instrument_key", ""), 999))

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
            return fallback[0] if fallback else candidates[0]

        # Sort ascending by spread %, tie-break by proximity to spot (ATM), then larger ask quantity, lower ask price
        if spot is not None and spot > 0:
            valid.sort(key=lambda x: (x["calc_spread"], abs(float(x.get("strike") or 0.0) - spot), -x.get("ask_qty", 0.0), x["ask_price"]))
        else:
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
        underlying_key: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trade_id = f"OIBU-{datetime.now(IST).strftime('%m%d%H%M%S')}-{self._trade_counter}"
            self._trade_counter += 1
            self.today_trades_count += 1

            underlying_key = underlying_key or winner.get("underlying_key") or NIFTY_KEY
            if underlying_key in self.stock_underlyings:
                st = self.stock_underlyings[underlying_key]
                lot_sz = st.get("lot_size", self.lot_size)
                base_lots = st.get("lots", 1)
                if size == "full":
                    lots = max(1, base_lots * 2)
                elif size == "normal":
                    lots = base_lots
                elif size == "scalp":
                    lots = max(1, base_lots // 2) if base_lots > 1 else 1
                else:
                    lots = base_lots
            elif underlying_key in self.index_underlyings:
                idx_st = self.index_underlyings[underlying_key]
                lot_sz = idx_st.get("lot_size") or self.index_lot_sizes.get(underlying_key, self.lot_size)
                if size == "full":
                    lots = self.lots_full
                elif size == "normal":
                    lots = self.lots_normal
                elif size == "scalp":
                    lots = self.lots_scalp
                else:
                    lots = self.index_lots.get(underlying_key, idx_st.get("lots", 1))
            else:
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

            # Phase 1 & 2: Paper entry fills at live ask!
            ask = float(winner.get("ask_price") or 0.0)
            ltp = float(winner.get("ltp") or 0.0)
            bid = float(winner.get("bid_price") or 0.0)
            ref_price = ask if ask > 0 else (ltp if ltp > 0 else 0.0)
            if ref_price <= 0.0:
                LOGGER.warning("ORDER REJECTED: No live quotes (ask=%.2f, ltp=%.2f) for %s (%s)", ask, ltp, winner.get("trading_symbol"), winner.get("instrument_key"))
                return None

            # Difference 4: Guard for stocks - Reject if bid-ask spread exceeds max_spread_pct (3.0%)
            if underlying_key in self.stock_underlyings and bid > 0 and ask > 0:
                spread = ((ask - bid) / ask) * 100.0
                if spread > self.max_spread_pct:
                    LOGGER.warning("STOCK ORDER REJECTED: Spread %.2f%% exceeds max %.1f%% guard for %s", spread, self.max_spread_pct, winner.get("trading_symbol"))
                    return None
            limit_price = round(round((ref_price + self.order_buffer) / 0.05) * 0.05, 2)
            entry_price = ref_price if self.mode == "paper" else limit_price
            sl_price = round(entry_price * 0.75, 2)
            tgt_price = round(entry_price * 1.40, 2)

            order_side = f"BUY_{side}"
            symbol = winner.get("trading_symbol") or f"NIFTY {winner.get('strike')} {side}"
            inst_key = winner["instrument_key"]
            strike = winner["strike"]
            bu_label = f"Rule #{rule_num} {stars} | CE:{ce_type} PE:{pe_type}"

            trade = {
                "trade_id": trade_id,
                "trade_date": today_str,
                "entry_path": "PATH_2_0918_OI",
                "stop_loss_price": sl_price,
                "target_price": tgt_price,
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
                "spot_price": self.index_underlyings.get(underlying_key, {}).get("spot_price") or self.spot_price,
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

            if underlying_key in self.index_underlyings:
                idx_st = self.index_underlyings[underlying_key]
                idx_st["current_position"] = trade
                idx_st["today_trades_count"] = idx_st.get("today_trades_count", 0) + 1
                idx_st["state"] = "HOLDING"
                if underlying_key == NIFTY_KEY:
                    self.current_position = trade
                    self.state = "HOLDING"
            elif underlying_key in self.stock_underlyings:
                st = self.stock_underlyings[underlying_key]
                st["current_position"] = trade
                st["today_trades_count"] = st.get("today_trades_count", 0) + 1
                st["state"] = "HOLDING"

            self.current_positions[underlying_key] = trade

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
                    "instrument_name": self.index_underlyings.get(underlying_key, {}).get("name", underlying_key),
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
                self.add_alert(
                    "ENTRY_ORDER_FILLED",
                    f"ENTERED {order_side} {symbol} @ {entry_price:.2f} (09:18 OI Rule #{rule_num} {stars}, Lots: {lots}, SL: {sl_price:.2f}, Tgt: {tgt_price:.2f})",
                    level="INFO",
                    details={"underlying": underlying_key, "trade_id": trade_id, "side": order_side, "entry_price": entry_price, "lots": lots},
                )
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
    # Active First Positive Tick Order Execution (Rule R16-R18)
    # -------------------------------------------------------------------------

    def _execute_positive_tick_entry(
        self,
        winner: dict[str, Any],
        side: str,
        trigger_time_str: str,
        trigger_ms: int,
        underlying_key: str,
        t_recv_perf: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            t_built_perf = time.perf_counter()
            bot_latency_ms = round((t_built_perf - t_recv_perf) * 1000, 3) if t_recv_perf else 0.35
            if bot_latency_ms <= 0.0:
                bot_latency_ms = 0.15
            net_latency_ms = 0.0

            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trade_id = f"GAP-{datetime.now(IST).strftime('%m%d%H%M%S')}-{self._trade_counter}"
            self._trade_counter += 1

            idx_st = self.index_underlyings.get(underlying_key, {})
            lots = idx_st.get("lots") or self.index_lots.get(underlying_key, self.lots)
            lot_sz = idx_st.get("lot_size") or self.index_lot_sizes.get(underlying_key, self.lot_size)
            total_qty = lots * lot_sz

            ask = float(winner.get("ask_price") or 0.0)
            ltp = float(winner.get("ltp") or 0.0)
            ref_price = ask if ask > 0 else (ltp if ltp > 0 else 0.0)
            if ref_price <= 0.0:
                LOGGER.warning("ORDER REJECTED: No live quotes (ask=%.2f, ltp=%.2f) for %s (%s)", ask, ltp, winner.get("trading_symbol"), winner.get("instrument_key"))
                return None
            limit_price = round(round((ref_price + self.order_buffer) / 0.05) * 0.05, 2)
            entry_price = ref_price if self.mode == "paper" else limit_price
            sl_price = round(entry_price * 0.75, 2)
            tgt_price = round(entry_price * 1.40, 2)

            order_side = f"BUY_{side}"
            symbol = winner.get("trading_symbol") or f"{idx_st.get('symbol_prefix', 'INDEX')} {winner.get('strike')} {side}"
            inst_key = winner["instrument_key"]
            strike = winner.get("strike")
            bu_label = winner.get("build_up") or "Neutral"

            trade = {
                "trade_id": trade_id,
                "trade_date": today_str,
                "entry_path": "PATH_1_0915",
                "stop_loss_price": sl_price,
                "target_price": tgt_price,
                "timeout_time": "09:45:00",
                "side": order_side,
                "symbol": symbol,
                "instrument_key": inst_key,
                "underlying_key": underlying_key,
                "strike": strike,
                "lots": lots,
                "lot_size": total_qty,
                "entry_time_ist": trigger_time_str,
                "entry_price": entry_price,
                "entry_timestamp_ms": trigger_ms,
                "exit_time_ist": None,
                "exit_price": None,
                "exit_timestamp_ms": None,
                "exit_reason": None,
                "pnl_points": 0.0,
                "net_pnl": 0.0,
                "status": "OPEN",
                "spot_price": idx_st.get("spot_price") or self.spot_price,
                "atm_strike": strike,
                "current_price": entry_price,
                "build_up": bu_label,
                "rule_number": "R8",
                "stars": "⭐⭐⭐",
                "decision": f"{side} Buy",
                "bot_latency_ms": bot_latency_ms,
                "network_latency_ms": net_latency_ms,
            }

            idx_st["current_position"] = trade
            idx_st["today_trades_count"] = idx_st.get("today_trades_count", 0) + 1
            idx_st["path1_done"] = True
            idx_st["state"] = "HOLDING"

            if underlying_key == NIFTY_KEY:
                self.current_position = trade
                self.today_trades_count += 1
                self.state = "HOLDING"

            self.current_positions[underlying_key] = trade

            decision_record = {
                "timestamp_ist": trigger_time_str,
                "underlying_key": underlying_key,
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
            idx_st["last_decision"] = decision_record
            idx_st["last_evaluation"] = {
                "decision": f"{side} Buy",
                "reason": f"FIRST POSITIVE TICK: LTP {winner.get('ltp')} > Ref {winner.get('ref_ltp')}",
                "delta_ce": 0.0,
                "delta_pe": 0.0,
                "ce_type": bu_label if side == "CE" else None,
                "pe_type": bu_label if side == "PE" else None,
                "rule": "R8",
                "stars": "⭐⭐⭐",
                "size": f"{lots} lots",
                "summary_line": f"{idx_st.get('name', underlying_key)}: FIRST POSITIVE TICK -> ENTERED {order_side} {symbol} @ {entry_price}",
            }
            if underlying_key == NIFTY_KEY:
                self.last_decision = decision_record
                self.last_evaluation = idx_st["last_evaluation"]

            try:
                self.database.insert_strategy_trade(trade)
                self.database.insert_strategy_signal({
                    "timestamp_ist": trigger_time_str,
                    "timestamp_ms": trigger_ms,
                    "instrument_name": idx_st.get("name", underlying_key),
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
                    "confidence": "⭐⭐⭐ FIRST POSITIVE TICK",
                    "action_taken": f"ENTERED_{order_side}",
                })
                self.add_alert(
                    "ENTRY_ORDER_FILLED",
                    f"ENTERED {order_side} {symbol} @ {entry_price:.2f} (First-tick 09:15, Lots: {lots}, SL: {sl_price:.2f}, Tgt: {tgt_price:.2f})",
                    level="INFO",
                    details={"underlying": underlying_key, "trade_id": trade_id, "side": order_side, "entry_price": entry_price, "lots": lots},
                )
            except Exception as exc:
                LOGGER.error("DB insert error for %s: %s", underlying_key, exc)

            try:
                with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        trade_id, today_str, order_side, symbol, strike, lots, total_qty,
                        trigger_time_str, entry_price,
                        "", "", "",
                        0.0, 0.0,
                        "OPEN",
                        idx_st.get("spot_price") or self.spot_price,
                        strike,
                        bu_label,
                        bot_latency_ms,
                        net_latency_ms,
                    ])
            except Exception as exc:
                LOGGER.error("CSV append error: %s", exc)

            LOGGER.info("ENTERED FIRST POSITIVE TICK TRADE for %s: %s (%s) @ %.2f (Lots: %d, Total Qty: %d)", underlying_key, symbol, inst_key, entry_price, lots, total_qty)
            return trade

    # -------------------------------------------------------------------------
    # Exit Management (Section 7 & 8: Paper Exit fills at live bid)
    # -------------------------------------------------------------------------

    def _exit_single_trade(self, pos: dict[str, Any], reason: str, underlying_key: str) -> dict[str, Any]:
        now_dt = datetime.now(IST)
        exit_time_str = now_dt.strftime("%H:%M:%S.%f")[:-3]
        exit_ms = int(time.time() * 1000)

        inst_key = pos["instrument_key"]
        cached = self.hot_cache.get(inst_key)
        bid = float(cached.get("bid_price") or 0.0) if cached else 0.0
        ltp = float(cached.get("ltp") or 0.0) if cached else 0.0

        if bid > 0:
            exit_price = bid
        elif ltp > 0:
            exit_price = ltp
        else:
            ref = float(cached.get("ref_ltp") or 0.0) if cached else 0.0
            exit_price = ref if ref > 0 else float(pos.get("entry_price") or 0.0)

        entry_price = float(pos["entry_price"])
        qty = int(pos.get("lot_size") or 1)
        pnl_points = round(exit_price - entry_price, 2)
        net_pnl = round(pnl_points * qty, 2)

        pos["exit_time_ist"] = exit_time_str
        pos["exit_price"] = exit_price
        pos["exit_timestamp_ms"] = exit_ms
        pos["exit_reason"] = reason
        pos["pnl_points"] = pnl_points
        pos["net_pnl"] = net_pnl
        pos["status"] = "CLOSED"

        if underlying_key in self.index_underlyings:
            idx_st = self.index_underlyings[underlying_key]
            idx_st["current_position"] = None
            idx_st["realized_pnl"] = round(idx_st.get("realized_pnl", 0.0) + net_pnl, 2)
            idx_st["cooldown_until"] = now_dt + timedelta(minutes=15)
            idx_st["last_closed_side"] = "CE" if "CE" in pos.get("side", "") else "PE"
            loss_lim = idx_st.get("daily_loss_limit_inr", self.daily_loss_limit_inr)
            max_t = idx_st.get("max_trades_per_day", self.max_trades_per_day)

            if reason == "SQUAREOFF_1515" or idx_st["realized_pnl"] <= -loss_lim:
                idx_st["state"] = "CLOSED"
                if idx_st["realized_pnl"] <= -loss_lim:
                    msg = f"DAILY LOSS LIMIT REACHED for {idx_st['name']}: Loss ₹{abs(idx_st['realized_pnl']):,.2f} exceeds limit ₹{loss_lim:,.2f}. Trading stopped for today."
                    LOGGER.warning(msg)
                    self.add_alert("DAILY_LOSS_LIMIT", msg, level="WARNING", details={"underlying": underlying_key, "loss": idx_st["realized_pnl"], "limit": loss_lim})
            elif idx_st.get("today_trades_count", 0) < max_t:
                idx_st["state"] = "ARMED"
                LOGGER.info("Index %s auto-rearmed after trade exit (trade #%d of %d max).", underlying_key, idx_st["today_trades_count"], max_t)
            else:
                idx_st["state"] = "CLOSED"
                LOGGER.info("Index %s completed daily maximum %d trades. Closed for today.", underlying_key, max_t)

            if underlying_key == NIFTY_KEY:
                self.current_position = None
                self.state = idx_st["state"]

        if underlying_key in self.stock_underlyings:
            st = self.stock_underlyings[underlying_key]
            st["current_position"] = None
            if reason == "SQUAREOFF_1515":
                st["state"] = "CLOSED"
            elif self.stock_max_trades_per_day > 0 and st.get("today_trades_count", 0) >= self.stock_max_trades_per_day:
                st["state"] = "CLOSED"
                LOGGER.info("Stock %s completed daily maximum %d trades. Closed for today.", underlying_key, self.stock_max_trades_per_day)
            else:
                st["state"] = "ARMED"
                LOGGER.info("Stock %s auto-rearmed after trade exit (trade #%d). Ready for next setup.", underlying_key, st.get("today_trades_count", 0))

            # JIT: Clean up / unsubscribe the closed option contract from WebSocket 2
            if self.feed and hasattr(self.feed, "unsubscribe") and inst_key:
                try:
                    self.feed.unsubscribe([inst_key])
                except Exception as ex:
                    LOGGER.debug("Could not unsubscribe exited contract %s: %s", inst_key, ex)

        self.current_positions[underlying_key] = None
        if underlying_key == NIFTY_KEY and underlying_key not in self.index_underlyings:
            self.current_position = None
            self.state = "CLOSED"

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
            LOGGER.error("Failed to update exit in DB for %s: %s", underlying_key, exc)

        try:
            if self.csv_path.exists():
                with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        pos.get("trade_id"),
                        pos.get("trade_date"),
                        pos.get("side"),
                        pos.get("symbol"),
                        pos.get("strike"),
                        pos.get("lots", 1),
                        pos.get("lot_size", qty),
                        pos.get("entry_time_ist"),
                        pos.get("entry_price"),
                        exit_time_str,
                        exit_price,
                        reason,
                        pnl_points,
                        net_pnl,
                        "CLOSED",
                        pos.get("rule_number", ""),
                        pos.get("stars", ""),
                        pos.get("delta_ce", ""),
                        pos.get("delta_pe", ""),
                        pos.get("ce_buildup", ""),
                        pos.get("pe_buildup", ""),
                        pos.get("decision", ""),
                        pos.get("bot_latency_ms", 0.25),
                        pos.get("network_latency_ms", 0.0),
                    ])
        except Exception as exc:
            LOGGER.error("Failed to append exit in CSV for %s: %s", underlying_key, exc)

        alert_msg = f"{pos['symbol']} EXITED ({reason}) @ {exit_price:.2f} | P&L: {pnl_points:+.2f} pts (₹{net_pnl:+,.2f})"
        self.add_alert(
            "EXIT_ORDER_FILLED",
            alert_msg,
            level="INFO",
            details={
                "underlying": underlying_key,
                "trade_id": pos["trade_id"],
                "pnl_points": pnl_points,
                "net_pnl": net_pnl,
                "reason": reason,
                "exit_price": exit_price,
            },
        )

        LOGGER.info(
            "POSITION CLOSED (%s): %s Exit @ %.2f (P&L: %s pts / ₹%s). Reason: %s",
            underlying_key,
            pos["symbol"],
            exit_price,
            pnl_points,
            net_pnl,
            reason,
        )
        return pos

    def manual_exit_indices(self, reason: str = "MANUAL") -> list[dict[str, Any]]:
        """Manual Exit for all open index positions (Nifty 50, Bank Nifty, Sensex)."""
        closed_positions: list[dict[str, Any]] = []
        with self._lock:
            for ukey, idx_st in list(self.index_underlyings.items()):
                p = idx_st.get("current_position") or self.current_positions.get(ukey)
                if p and p.get("status") == "OPEN":
                    res = self._exit_single_trade(p, reason, ukey)
                    if res:
                        closed_positions.append(res)

            if self.current_position and self.current_position.get("status") == "OPEN":
                inst = self.current_position.get("instrument_key", "")
                if not any(cp.get("instrument_key") == inst for cp in closed_positions):
                    res = self._exit_single_trade(self.current_position, reason, NIFTY_KEY)
                    if res:
                        closed_positions.append(res)

            LOGGER.info("MANUAL EXIT INDICES: Closed %d open index positions", len(closed_positions))
            return closed_positions

    def manual_exit_stocks(self, reason: str = "MANUAL") -> list[dict[str, Any]]:
        """Manual Exit for all open stock option positions."""
        closed_positions: list[dict[str, Any]] = []
        with self._lock:
            for sym, st in list(self.stock_underlyings.items()):
                p = st.get("current_position") or self.current_positions.get(sym)
                if p and p.get("status") == "OPEN":
                    res = self._exit_single_trade(p, reason, sym)
                    if res:
                        closed_positions.append(res)

            LOGGER.info("MANUAL EXIT STOCKS: Closed %d open stock positions", len(closed_positions))
            return closed_positions

    def manual_exit(self, reason: str = "MANUAL", underlying_key: str | None = None) -> dict[str, Any] | None:
        """Manual Exit Now button: exits specified open position or all open positions across indices and stocks."""
        with self._lock:
            u_clean = str(underlying_key).strip().upper() if underlying_key else ""
            if u_clean in ("INDICES", "INDEX"):
                closed = self.manual_exit_indices(reason)
                return closed[0] if closed else None

            if u_clean in ("STOCKS", "STOCK"):
                closed = self.manual_exit_stocks(reason)
                return closed[0] if closed else None

            if underlying_key and u_clean not in ("ALL", "", "NONE"):
                # Check if it matches a stock symbol or key
                sym_match = next((s for s, d in self.stock_underlyings.items() if s == u_clean or d.get("key") == underlying_key), None)
                if sym_match:
                    st = self.stock_underlyings[sym_match]
                    pos = st.get("current_position") or self.current_positions.get(sym_match)
                    if pos and pos.get("status") == "OPEN":
                        return self._exit_single_trade(pos, reason, sym_match)
                    return None

                ukey = self.resolve_underlying_key(underlying_key)
                pos = self.current_positions.get(ukey)
                if not pos and ukey in self.index_underlyings:
                    pos = self.index_underlyings[ukey].get("current_position")
                if pos and pos.get("status") == "OPEN":
                    return self._exit_single_trade(pos, reason, ukey)
                return None

            closed_any = None
            for ukey, idx_st in list(self.index_underlyings.items()):
                p = idx_st.get("current_position") or self.current_positions.get(ukey)
                if p and p.get("status") == "OPEN":
                    res = self._exit_single_trade(p, reason, ukey)
                    if not closed_any:
                        closed_any = res

            for sym, st in list(self.stock_underlyings.items()):
                p = st.get("current_position") or self.current_positions.get(sym)
                if p and p.get("status") == "OPEN":
                    res = self._exit_single_trade(p, reason, sym)
                    if not closed_any:
                        closed_any = res

            if self.current_position and self.current_position.get("status") == "OPEN":
                res = self._exit_single_trade(self.current_position, reason, NIFTY_KEY)
                if not closed_any:
                    closed_any = res

            return closed_any

    def squareoff_all_indices_1515(self) -> None:
        """15:15:00 EOD clock square-off across all active index positions."""
        with self._lock:
            for ukey, idx_st in self.index_underlyings.items():
                pos = idx_st.get("current_position")
                if pos and pos.get("status") == "OPEN":
                    self._exit_single_trade(pos, "SQUAREOFF_1515", ukey)

    def emergency_stop(self) -> dict[str, Any]:
        """Emergency stop: exits open position and disables further entries."""
        with self._lock:
            self.enabled = False
            self.state = "HALTED"
            if self.current_position:
                self.manual_exit(reason="EMERGENCY_STOP")
            LOGGER.warning("EMERGENCY STOP ACTIVATED: Bot halted.")
            return self.get_status()

    def manual_enter(self, underlying_key: str | None = None, side: str = "CE") -> dict[str, Any] | None:
        """Manual Enter Button: Enters a CE or PE trade on demand based on strategy rules."""
        with self._lock:
            ukey = self.resolve_underlying_key(underlying_key)
            side = str(side).upper().strip()
            if side not in ("CE", "PE"):
                side = "CE"

            # Check if an index
            if ukey in self.index_underlyings:
                idx_st = self.index_underlyings[ukey]
                if idx_st.get("current_position") is not None:
                    LOGGER.warning("manual_enter: %s already has an active open position!", ukey)
                    return idx_st.get("current_position")

                # Ensure armed
                if not idx_st.get("locked_keys"):
                    self._force_arm_single_index(ukey)

                locked_keys = idx_st.get("locked_keys") or []
                winner = self._select_best_strike(side, locked_keys=locked_keys)
                if not winner:
                    LOGGER.warning("manual_enter: No valid ATM strike with spread <= %.1f%% for %s %s", self.max_spread_pct, ukey, side)
                    return None

                lot_sz = idx_st.get("lot_size") or self.index_lot_sizes.get(ukey, self.lot_size)
                lots = self.lots_full or self.index_lots.get(ukey, 1)
                total_qty = lots * lot_sz

                ask = float(winner.get("ask_price") or 0.0)
                ltp = float(winner.get("ltp") or 0.0)
                entry_price = ask if ask > 0 else (ltp if ltp > 0 else 100.0)
                sl_price = round(entry_price * 0.75, 2)
                tgt_price = round(entry_price * 1.40, 2)

                now_dt = datetime.now(IST)
                trade_id = f"MAN-{now_dt.strftime('%m%d%H%M%S')}-{int(time.time() * 1000) % 1000}"
                order_side = f"BUY_{side}"

                trade = {
                    "trade_id": trade_id,
                    "trade_date": now_dt.strftime("%Y-%m-%d"),
                    "entry_path": "MANUAL_CLICK",
                    "stop_loss_price": sl_price,
                    "target_price": tgt_price,
                    "side": order_side,
                    "symbol": winner.get("trading_symbol", f"{idx_st.get('name')} {winner.get('strike')} {side}"),
                    "instrument_key": winner.get("instrument_key"),
                    "underlying_key": ukey,
                    "strike": winner.get("strike"),
                    "lots": lots,
                    "lot_size": total_qty,
                    "entry_time_ist": now_dt.strftime("%H:%M:%S"),
                    "entry_price": entry_price,
                    "entry_timestamp_ms": int(time.time() * 1000),
                    "exit_time_ist": None,
                    "exit_price": None,
                    "exit_timestamp_ms": None,
                    "exit_reason": None,
                    "pnl_points": 0.0,
                    "net_pnl": 0.0,
                    "status": "OPEN",
                    "spot_price": idx_st.get("spot_price", 0.0),
                    "atm_strike": winner.get("strike"),
                    "current_price": entry_price,
                    "build_up": f"MANUAL {side} CLICK",
                    "rule_number": 1 if side == "CE" else 5,
                    "stars": "★★★",
                    "size_category": "full",
                    "decision": f"{side} Buy",
                    "bot_latency_ms": 0.1,
                    "network_latency_ms": 0.0,
                }

                idx_st["current_position"] = trade
                idx_st["today_trades_count"] = idx_st.get("today_trades_count", 0) + 1
                idx_st["state"] = "HOLDING"
                if ukey == NIFTY_KEY:
                    self.current_position = trade
                    self.state = "HOLDING"
                self.current_positions[ukey] = trade

                try:
                    self.database.insert_strategy_trade(trade)
                except Exception as ex:
                    LOGGER.debug("DB insert error: %s", ex)

                self.add_alert(
                    alert_type=f"ENTRY_{order_side}",
                    message=f"MANUAL ENTERED {order_side} {trade['symbol']} @ {entry_price:.2f} (Lots: {lots}, SL: {sl_price:.2f}, Tgt: {tgt_price:.2f})",
                    level="INFO",
                    details={"underlying": ukey},
                )
                LOGGER.info("MANUAL ENTRY EXECUTED for %s: %s @ %.2f (Lots: %d)", ukey, trade["symbol"], entry_price, lots)
                return trade

            return None

    # -------------------------------------------------------------------------
    # Rehearsal & Testing Utilities
    # -------------------------------------------------------------------------

    def resolve_underlying_key(self, key: str | None) -> str:
        if not key:
            return NIFTY_KEY
        k = key.strip()
        if k in self.index_underlyings:
            return k
        kl = k.lower().replace(" ", "").replace("_", "")
        if "bank" in kl:
            return BANKNIFTY_KEY
        if "sensex" in kl:
            return SENSEX_KEY
        if "nifty" in kl:
            return NIFTY_KEY
        return k

    def _force_arm_single_index(self, ukey: str) -> None:
        idx_st = self.index_underlyings.get(ukey)
        if not idx_st:
            LOGGER.warning("force_arm_now: Unknown index %s", ukey)
            return

        if idx_st.get("current_position") is not None or self.current_positions.get(ukey) is not None:
            LOGGER.warning("force_arm_now: Cannot arm %s because it already has an active open position!", ukey)
            return

        idx_st["today_trades_count"] = 0
        if ukey == NIFTY_KEY:
            self.today_trades_count = 0

        spot = idx_st.get("spot_price") or (self.spot_price if ukey == NIFTY_KEY else None)
        if not spot or spot <= 0:
            t = self.database.get_latest_tick(ukey)
            if t and t.get("ltp"):
                spot = float(t["ltp"])
                idx_st["spot_price"] = spot
                if ukey == NIFTY_KEY:
                    self.spot_price = spot

        if not spot or spot <= 0:
            spot = 25000.0 if ukey == NIFTY_KEY else (60000.0 if ukey == BANKNIFTY_KEY else 80000.0)

        idx_st["snapshot_taken"] = False
        idx_st["locked_keys"] = []
        idx_st["planned_keys"] = []
        self.load_contracts_if_needed(ukey)
        self.plan_8_itm_contracts(spot, underlying_key=ukey)
        self.lock_contracts(underlying_key=ukey)
        self.take_snapshot(underlying_key=ukey)
        idx_st["state"] = "ARMED"
        if ukey == NIFTY_KEY:
            self.state = "ARMED"

        locked = idx_st.get("locked_keys") or []
        if self.feed and locked:
            try:
                self.feed.subscribe(locked, target="indices")
            except Exception as e:
                LOGGER.debug("Feed subscribe on arm: %s", e)

        # PREVIOUS OI BUILDUP EVALUATION (COMMENTED OUT AS PER CLIENT REQUEST):
        # trade = self.evaluate_oi_buildup(underlying_key=ukey, force=True)
        LOGGER.info("FORCE ARM COMPLETED for index %s: State=ARMED (Watching for first positive tick).", ukey)

    def _force_arm_stock(self, sym: str) -> None:
        sym = sym.upper().strip()
        st = self.stock_underlyings.get(sym)
        if not st:
            return
        if st.get("current_position") is not None or self.current_positions.get(sym) is not None:
            LOGGER.warning("force_arm_now: Cannot arm stock %s because it already has an active open position!", sym)
            return

        st["today_trades_count"] = 0
        spot = st.get("spot_price")
        if not spot or spot <= 0:
            key = st.get("key") or f"NSE_EQ|{sym}"
            try:
                ticks = self.database.latest_ticks(instrument_key=key, limit=1)
                if ticks and float(ticks[0].get("ltp") or 0.0) > 0:
                    spot = float(ticks[0]["ltp"])
                    st["spot_price"] = spot
            except Exception as e:
                LOGGER.debug("Could not get tick for stock %s: %s", sym, e)

        if not spot or spot <= 0:
            meta = next((s for s in NIFTY_50_STOCKS if s["symbol"] == sym), None)
            spot = float(meta.get("strike_step", 10.0) * 100) if meta else 1000.0
            st["spot_price"] = spot

        self.plan_stock_contracts(sym, spot)
        self.lock_stock_contracts(sym)
        self.take_stock_snapshot(sym)
        st["state"] = "ARMED"

        # Dynamically subscribe feed to locked option keys
        locked = st.get("locked_keys") or []
        if self.feed and locked:
            try:
                self.feed.subscribe(locked, target="stocks")
            except Exception as e:
                LOGGER.debug("Feed subscribe on stock arm: %s", e)

        trade = self.evaluate_stock_oi_buildup(sym)
        LOGGER.info("FORCE ARM COMPLETED for stock %s: State=ARMED, Trade=%s", sym, trade)

    def force_arm_now(self, underlying_key: str | None = None) -> dict[str, Any]:
        """
        Forces immediate planning, locking, fresh snapshot, and arming for an index or stock.
        - Enables 1 more trade for the instrument today.
        - Only affects the targeted instrument (single-single isolation).
        """
        with self._lock:
            u_clean = str(underlying_key).strip().upper() if underlying_key else ""

            # Check if targeting all stocks
            if u_clean in ("STOCKS", "STOCK"):
                armed_count = 0
                for sym, st in self.stock_underlyings.items():
                    if st.get("enabled", True):
                        self._force_arm_stock(sym)
                        armed_count += 1
                LOGGER.info("FORCE ARM NOW: Armed %d active constituent stocks (%s)", armed_count, list(self.stock_underlyings.keys()))
                return self.get_status()

            # Check if targeting all indices
            if u_clean in ("INDICES", "INDEX"):
                for ukey in self.index_underlyings:
                    self._force_arm_single_index(ukey)
                return self.get_status()

            # Check if targeting a specific stock
            sym_target = None
            if underlying_key:
                if u_clean in self.stock_underlyings:
                    sym_target = u_clean
                else:
                    for s, d in self.stock_underlyings.items():
                        if d.get("key") == underlying_key or s == u_clean.replace("NSE_EQ|", ""):
                            sym_target = s
                            break
            if sym_target:
                self._force_arm_stock(sym_target)
                return self.get_status()

            # Otherwise, target index
            ukey = self.resolve_underlying_key(underlying_key)
            self._force_arm_single_index(ukey)
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
            "max_bullish_stocks": getattr(self, "max_bullish_stocks", 5),
            "max_bearish_stocks": getattr(self, "max_bearish_stocks", 5),
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
            "market_open_time": getattr(self, "market_open_time", "09:15:00"),
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
            if "max_bullish_stocks" in body:
                self.max_bullish_stocks = max(1, min(5, int(body["max_bullish_stocks"])))
            if "max_bearish_stocks" in body:
                self.max_bearish_stocks = max(1, min(5, int(body["max_bearish_stocks"])))
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
                            if hasattr(self, "index_underlyings") and k in self.index_underlyings:
                                self.index_underlyings[k]["lots"] = self.index_lots[k]
                        except (ValueError, TypeError):
                            pass
                if NIFTY_KEY in self.index_lots:
                    self.lots = self.index_lots[NIFTY_KEY]
                self._save_index_lots()
            if "index_enabled" in body and isinstance(body["index_enabled"], dict):
                for k, v in body["index_enabled"].items():
                    if k in self.index_enabled:
                        self.index_enabled[k] = bool(v)
                        if hasattr(self, "index_underlyings") and k in self.index_underlyings:
                            self.index_underlyings[k]["enabled"] = self.index_enabled[k]
                self._save_index_lots()
            if "order_buffer" in body:
                self.order_buffer = float(body["order_buffer"])
            if "max_spread_pct" in body:
                self.max_spread_pct = float(body["max_spread_pct"])
            if "mode" in body and body["mode"] in ("paper", "live"):
                self.mode = body["mode"]
            if "enabled" in body:
                self.enabled = bool(body["enabled"])
            self._save_index_lots()
            LOGGER.info("Updated Strategy config: %s", self.get_config())
            return self.get_config()

    def get_live_open_positions(self) -> dict[str, dict[str, Any]]:
        """Returns all currently open positions with live tick-by-tick marked-to-market P&L."""
        live_pos = {}
        with self._lock:
            for key, pos in self.current_positions.items():
                if not pos or pos.get("status") == "CLOSED":
                    continue
                ik = pos.get("instrument_key")
                hc = self.hot_cache.get(ik) or {}
                bid = float(hc.get("bid_price") or hc.get("ltp") or pos.get("entry_price") or 0.0)
                ask = float(hc.get("ask_price") or hc.get("ltp") or pos.get("entry_price") or 0.0)
                entry_p = float(pos.get("entry_price") or 0.0)
                qty = int(pos.get("lot_size") or 1)
                cur_p = bid if bid > 0 else (ask if ask > 0 else entry_p)
                pts = round(cur_p - entry_p, 2)
                net = round(pts * qty, 2)
                p_copy = dict(pos)
                p_copy["current_price"] = cur_p
                p_copy["bid_price"] = bid
                p_copy["ask_price"] = ask
                p_copy["pnl_points"] = pts
                p_copy["net_pnl"] = net
                live_pos[key] = p_copy
        return live_pos

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trades = self.database.get_strategy_trades(limit=100, trade_date=today_str)
            today_trades = trades
            today_closed = [t for t in today_trades if t.get("status") == "CLOSED"]
            realized_pnl = sum(float(t.get("net_pnl") or 0.0) for t in today_closed)
            realized_pts = sum(float(t.get("pnl_points") or 0.0) for t in today_closed)
            today_wins = sum(1 for t in today_closed if float(t.get("net_pnl") or 0.0) > 0)
            today_losses = sum(1 for t in today_closed if float(t.get("net_pnl") or 0.0) < 0)

            # Marked-to-Market Live P&L for open positions (realized + unrealized)
            live_open = self.get_live_open_positions()
            unrealized_pnl = sum(p.get("net_pnl", 0.0) for p in live_open.values())
            unrealized_pts = sum(p.get("pnl_points", 0.0) for p in live_open.values())
            today_pnl = round(realized_pnl + unrealized_pnl, 2)
            today_pnl_pts = round(realized_pts + unrealized_pts, 2)

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
                        "ref_ltp": c.get("ref_ltp"),
                        "ref_source": c.get("ref_source"),
                        "snap_ltp": snap_c.get("ltp") or c.get("ref_ltp"),
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
                "strategy_name": "Opening Gap Entry - First Positive Tick (NIFTY 50, BANK NIFTY, SENSEX)",
                "state": self.state,
                "enabled": self.enabled,
                "mode": self.mode,
                "spot_price": self.index_underlyings.get(NIFTY_KEY, {}).get("spot_price") or self.spot_price,
                "prev_close": self.index_underlyings.get(NIFTY_KEY, {}).get("prev_close"),
                "net_change": round((self.index_underlyings.get(NIFTY_KEY, {}).get("spot_price") or self.spot_price or 0.0) - (self.index_underlyings.get(NIFTY_KEY, {}).get("prev_close") or 0.0), 2) if self.index_underlyings.get(NIFTY_KEY, {}).get("prev_close") else 0.0,
                "futures_ltp": self.index_underlyings.get(NIFTY_KEY, {}).get("futures_ltp"),
                "futures_oi": self.index_underlyings.get(NIFTY_KEY, {}).get("futures_oi"),
                "futures_buildup": self.index_underlyings.get(NIFTY_KEY, {}).get("futures_buildup", "Neutral"),
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
                "current_positions": {k: v for k, v in self.current_positions.items() if v is not None},
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
                "index_underlyings": {
                    ukey: {
                        "key": d["key"],
                        "name": d["name"],
                        "step": d["step"],
                        "lot_size": d["lot_size"],
                        "lots": d["lots"],
                        "enabled": d["enabled"],
                        "state": d["state"],
                        "spot_price": d["spot_price"],
                        "last_evaluation": d["last_evaluation"],
                        "has_position": d["current_position"] is not None,
                        "position": d["current_position"],
                        "today_trades_count": d.get("today_trades_count", 0),
                        "max_trades_per_day": d.get("max_trades_per_day", self.max_trades_per_day),
                        "realized_pnl": d.get("realized_pnl", 0.0),
                        "daily_loss_limit_inr": d.get("daily_loss_limit_inr", self.daily_loss_limit_inr),
                        "futures_key": d.get("futures_key", ""),
                        "prev_close": d.get("prev_close"),
                        "net_change": round((d.get("spot_price") or 0.0) - (d.get("prev_close") or 0.0), 2) if d.get("prev_close") else 0.0,
                        "futures_ltp": d.get("futures_ltp"),
                        "futures_oi": d.get("futures_oi"),
                        "futures_buildup": d.get("futures_buildup", "Neutral"),
                        "locked_contracts_count": len(d.get("locked_keys") or []),
                    }
                    for ukey, d in self.index_underlyings.items()
                },
                "futures_ltp": self.index_underlyings.get(NIFTY_KEY, {}).get("futures_ltp"),
                "futures_oi": self.index_underlyings.get(NIFTY_KEY, {}).get("futures_oi"),
                "futures_buildup": self.index_underlyings.get(NIFTY_KEY, {}).get("futures_buildup", "Neutral"),
                "alerts": self.get_alerts(limit=20),
                "stock_underlyings": {
                    sym: {
                        "symbol": d["symbol"],
                        "name": d.get("name", sym),
                        "lot_size": d["lot_size"],
                        "lots": d["lots"],
                        "enabled": d["enabled"],
                        "state": d["state"],
                        "spot_price": d["spot_price"],
                        "open_price": d.get("open_price"),
                        "prev_close": d.get("prev_close"),
                        "gap_pct": d.get("gap_pct", 0.0),
                        "gap_against": d.get("gap_against", False),
                        "gap_reason": d.get("gap_reason", ""),
                        "watchlist_rank": d.get("watchlist_rank", "★★★"),
                        "watchlist_direction": d.get("watchlist_direction", "BULLISH"),
                        "futures_buildup": d.get("futures_buildup", "Neutral"),
                        "today_trades_count": d.get("today_trades_count", 0),
                        "max_trades_per_day": self.stock_max_trades_per_day,
                        "path1_done": d.get("path1_done", False),
                        "last_evaluation": d["last_evaluation"],
                        "has_position": d["current_position"] is not None,
                        "position": d["current_position"],
                    }
                    for sym, d in self.stock_underlyings.items()
                },
                "config": self.get_config(),
                "csv_path": str(self.csv_path),
            }


# Backwards compatibility alias
PressureStrategyEngine = GapEntryStrategyEngine


# =========================================================================
# STRATEGY STATUS & PRESERVED ARCHITECTURE
# =========================================================================
"""
ACTIVE STRATEGY:
- Opening Gap Entry - First Positive Tick at 09:15:00 for NIFTY 50, BANK NIFTY, and SENSEX.
  Rule R8: Locks 8 ITM contracts at 09:14:55, stores fixed ref_ltp, arms at 09:15:00.
  First positive tick (LTP > ref_ltp) enters tightest spread contract on that side.

PRESERVED STRATEGY:
- 09:18:00 OI Build-up 16-condition table strategy (Strategy_OI_Buildup_0918_for_LLM.md).
  Preserved in code and commented out for index live execution as per client request.
  Constituent stock options evaluate via the 09:18 OI buildup table.
"""
