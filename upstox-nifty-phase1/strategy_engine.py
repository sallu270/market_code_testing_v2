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
from instruments_master import UpstoxInstrumentsMaster
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
    BANKNIFTY_KEY: {"name": "BANK NIFTY", "step": 100.0, "lot_size": 30, "symbol_prefix": "BANKNIFTY", "futures_key": "NSE_FO|61466"},
    SENSEX_KEY: {"name": "SENSEX", "step": 100.0, "lot_size": 20, "symbol_prefix": "SENSEX", "futures_key": ""},
}

# =========================================================================
# STRATEGY 1 & STRATEGY 2 DUAL TRADING ARCHITECTURE
# Strategy 1 (Indices): 09:14:55 Lock & 09:15:00-09:18:00 First Positive Tick Entry
# Strategy 2 (Indices & 50 Stocks): Dr. Vijay Bhilwade Sum Signal (Score = CE% - PE%)
#   - Indices: ATM ± 10 (21 CE + 21 PE = 42 contracts per index)
#   - Stocks: ATM ± 5 (11 CE + 11 PE = 22 contracts per stock) across all 50 Nifty stocks
#   - Live 1 to 50 Ranking & Top 3 Bullish / Bottom 3 Bearish 300s Whipsaw Entry
#   - Stepped Trailing SL (-20% SL, +40% Target, +5% to Cost, +2% per +3% profit)
#   - Non-cutting Position Management (Active trades never cut on rank changes)
# =========================================================================


class GapEntryStrategyEngine:
    """
    Unified Production Strategy Engine (Indices & All 50 NIFTY Constituent Stocks).
    
    1. Strategy 1 (Indices: NIFTY 50, BANK NIFTY, SENSEX):
       - 09:14:55: Lock ATM ± 10 contracts (42 options) and store fixed reference prices.
       - 09:15:00 to 09:18:00: Market open -> State transitions to ARMED.
       - First positive tick (LTP > ref_ltp) enters winning contract on tightest spread.
       - Stop Loss -20%, Target +40%, Stepped Trailing SL, 15:15:00 EOD Square-off.
    
    2. Strategy 2 (All 53 Underlyings: 3 Indices + 50 NIFTY Stocks):
       - 09:15:00: Unified Baseline Sum freeze (CE sum & PE sum fixed for the day).
       - Live Score = CE % Change - PE % Change (evaluated every second).
       - Stocks Live 1 to 50 Ranking:
           * Top 3 Bullish (Rank 1, 2, 3): BUY CE (ITM-2) with 300s whipsaw confirmation.
           * Bottom 3 Bearish (Rank 48, 49, 50): BUY PE (ITM-2) with 300s whipsaw confirmation.
       - Active positions are NEVER closed due to ranking changes; managed strictly on SL/TP/Reversal.
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
        self.max_trades_per_day: int = 3
        self.daily_loss_limit_inr: float = 10000.0
        self.stock_max_trades_per_day: int = 3
        self.max_bullish_stocks: int = 3
        self.max_bearish_stocks: int = 3
        self.today_trades_count: int = 0

        # Configurable Strategy Brief Parameters (Dr. Bhilwade 24-11-2026 & 27-11-2026 Review)
        self.entry_itm_depth: int = 2
        self.min_entry_premium: float = 50.0
        self.max_entry_premium: float = 2500.0
        self.whipsaw_sec: float = 300.0
        self.max_stock_spread_pct: float = 5.0
        self.min_stock_basket_premium: float = 5.0
        self.allow_double_exposure: bool = False

        # O(1) fast key -> underlying lookup table (crucial for 1,200+ streaming keys)
        self._key_to_underlying: dict[str, str] = {}
        self._stock_rankings: list[dict[str, Any]] = []
        self._top_bullish: list[dict[str, Any]] = []
        self._top_bearish: list[dict[str, Any]] = []
        self._last_stock_rank_ts: float = 0.0
        self._last_rank_emit_ts: float = 0.0

        # Daily Upstox Instruments Master (single download, dynamic official exchange lot sizes)
        self.instruments_master = UpstoxInstrumentsMaster(base_dir)

        # Phase 3: Live Alerts system
        self.alerts: list[dict[str, Any]] = []
        self._alerts_lock = threading.Lock()
        self._alert_flags: set[str] = set()
        self.last_tick_time: float = time.time()
        self._active_stock_option_keys: set[str] = set()

        # Live Percentage Momentum History (Second-by-second changes in CE %, PE %, and Score)
        self.momentum_history: list[dict[str, Any]] = []
        self._momentum_lock = threading.Lock()

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
            "NSE_INDEX|Nifty Bank": 30,
            "BSE_INDEX|SENSEX": 20,
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

        # Locked Basket Contracts (ATM ± 10 for Indices: 21 CE + 21 PE = 42 contracts per index)
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
                        self.add_alert(
                            "INDEX_ARMED",
                            f"{idx_st['name']}: ARMED at spot ₹{spot:.2f} (Mid-day start - Path 2 Sum Signal active)",
                            level="INFO",
                            details={"underlying": ukey, "spot": spot},
                        )

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
                            self.max_bullish_stocks = max(1, min(10, int(data["max_bullish_stocks"])))
                        if "max_bearish_stocks" in data:
                            self.max_bearish_stocks = max(1, min(10, int(data["max_bearish_stocks"])))
                        if "entry_itm_depth" in data:
                            self.entry_itm_depth = max(1, min(5, int(data["entry_itm_depth"])))
                        if "min_entry_premium" in data:
                            self.min_entry_premium = max(1.0, float(data["min_entry_premium"]))
                        if "max_entry_premium" in data:
                            self.max_entry_premium = max(50.0, float(data["max_entry_premium"]))
                        if "whipsaw_sec" in data:
                            self.whipsaw_sec = max(10.0, float(data["whipsaw_sec"]))
                        if "allow_double_exposure" in data:
                            self.allow_double_exposure = bool(data["allow_double_exposure"])
                        if NIFTY_KEY in self.index_lots:
                            self.lots = self.index_lots[NIFTY_KEY]
                        if hasattr(self, "index_underlyings"):
                            for ukey, idx_st in self.index_underlyings.items():
                                if ukey in self.index_lots:
                                    idx_st["lots"] = self.index_lots[ukey]
                                if ukey in self.index_enabled:
                                    idx_st["enabled"] = self.index_enabled[ukey]
                                idx_st["whipsaw_sec"] = self.whipsaw_sec
                LOGGER.info("Loaded config from %s: lots=%s, ITM=%d, prem=[%.0f,%.0f], whipsaw=%.0fs", self.lots_file, self.index_lots, self.entry_itm_depth, self.min_entry_premium, self.max_entry_premium, self.whipsaw_sec)
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
                    "max_bullish_stocks": getattr(self, "max_bullish_stocks", 3),
                    "max_bearish_stocks": getattr(self, "max_bearish_stocks", 3),
                    "entry_itm_depth": getattr(self, "entry_itm_depth", 2),
                    "min_entry_premium": getattr(self, "min_entry_premium", 50.0),
                    "max_entry_premium": getattr(self, "max_entry_premium", 2500.0),
                    "whipsaw_sec": getattr(self, "whipsaw_sec", 300.0),
                    "allow_double_exposure": getattr(self, "allow_double_exposure", False),
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
            if hasattr(self, "on_alert") and callable(self.on_alert):
                try:
                    self.on_alert(alert)
                except Exception as cb_err:
                    LOGGER.debug("on_alert callback error: %s", cb_err)
            return alert

    def get_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._alerts_lock:
            return list(reversed(self.alerts[-limit:]))

    def get_momentum_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._momentum_lock:
            if self.momentum_history:
                return list(reversed(self.momentum_history[-limit:]))
        if hasattr(self, "database") and self.database:
            try:
                today_s = datetime.now(IST).strftime("%Y-%m-%d")
                db_logs = self.database.get_momentum_logs(date=today_s, limit=limit)
                return [{
                    "id": f"db_{l.get('id')}",
                    "time": l.get("timestamp_ist", ""),
                    "key": l.get("underlying_key", ""),
                    "name": l.get("index_name", ""),
                    "ce_pct": float(l.get("ce_pct") or 0.0),
                    "prev_ce_pct": float(l.get("prev_ce_pct") or 0.0),
                    "delta_ce": float(l.get("delta_ce") or 0.0),
                    "pe_pct": float(l.get("pe_pct") or 0.0),
                    "prev_pe_pct": float(l.get("prev_pe_pct") or 0.0),
                    "delta_pe": float(l.get("delta_pe") or 0.0),
                    "score": round(float(l.get("ce_pct") or 0.0) - float(l.get("pe_pct") or 0.0), 2),
                    "raw_signal": l.get("raw_signal", "Neutral"),
                    "hold_sec": l.get("hold_sec", 0),
                    "confirmed": bool(l.get("confirmed")),
                } for l in db_logs]
            except Exception:
                pass
        return []

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

        # 4. Open position contracts (for live Trade Book P&L only — strictly EXCLUDED from Sum Signal basket)
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if hasattr(self, "database") and self.database:
            try:
                open_trades = self.database.get_strategy_trades(limit=50, trade_date=today_str)
                for ot in open_trades:
                    if ot.get("status") == "OPEN" and ot.get("instrument_key"):
                        idx_keys.append(ot["instrument_key"])
            except Exception:
                pass

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

    # -------------------------------------------------------------------------
    # 50 NIFTY Constituent Stocks Universe Management (Section 1-4 of Brief)
    # -------------------------------------------------------------------------
    def _refresh_dynamic_lot_sizes(self) -> None:
        """Syncs official exchange lot sizes from Upstox Daily Instruments Master."""
        try:
            for ukey, cfg in INDEX_CONFIG.items():
                live_lot = self.instruments_master.get_lot_size(cfg["symbol_prefix"], default=cfg["lot_size"])
                if live_lot > 0:
                    self.index_lot_sizes[ukey] = live_lot
                    if ukey in self.index_underlyings:
                        self.index_underlyings[ukey]["lot_size"] = live_lot
                    if ukey == NIFTY_KEY:
                        self.lot_size = live_lot
        except Exception as exc:
            LOGGER.debug("Dynamic lot size refresh skipped: %s", exc)

    def load_stock_configs(self) -> None:
        """Initializes all 50 NIFTY constituent stocks (with official lot sizes) for Sum Signal Ranking & Trading."""
        with self._lock:
            for meta in NIFTY_50_STOCKS:
                sym = meta["symbol"].upper()
                eq_key = meta["key"]
                lot_sz = meta["lot_size"]
                if sym not in self.stock_underlyings:
                    self.stock_underlyings[sym] = {
                        "key": eq_key,
                        "symbol": sym,
                        "name": sym,
                        "step": float(meta.get("strike_step") or 10.0),
                        "lot_size": lot_sz,
                        "lots": 1,
                        "enabled": True,
                        "spot_price": None,
                        "prev_close": None,
                        "atm_strike": None,
                        "state": "SETUP",
                        "planned_keys": [],
                        "locked_keys": [],
                        "atm_plus_minus_keys": [],
                        "ce_basket_keys": [],
                        "pe_basket_keys": [],
                        "baseline_prices": {},
                        "ce_baseline_sum": 0.0,
                        "pe_baseline_sum": 0.0,
                        "ce_current_sum": 0.0,
                        "pe_current_sum": 0.0,
                        "baseline_locked": False,
                        "snapshot_taken": False,
                        "snapshot": {},
                        "ce_pct": 0.0,
                        "pe_pct": 0.0,
                        "score": 0.0,
                        "rank": 0,
                        "raw_signal": "Neutral",
                        "candidate_signal": None,
                        "candidate_start_time": None,
                        "hold_elapsed_sec": 0.0,
                        "confirmed_signal": None,
                        "cooldown_until": None,
                        "last_closed_side": None,
                        "whipsaw_sec": float(getattr(self, "whipsaw_sec", 300.0)),
                        "current_position": None,
                        "path2_position": None,
                        "today_trades_count": 0,
                        "realized_pnl": 0.0,
                        "daily_loss_limit_inr": self.daily_loss_limit_inr,
                        "max_trades_per_day": self.stock_max_trades_per_day,
                        "last_evaluation": {"decision": "WAIT", "raw_signal": "Neutral", "score": 0.0},
                    }
                    self._key_to_underlying[eq_key] = sym

    def plan_all_stocks(self) -> None:
        """Plans ATM +- 5 (22 strikes = 11 CE + 11 PE) for all 50 NIFTY stocks using Upstox Daily Master."""
        with self._lock:
            self._refresh_dynamic_lot_sizes()
            all_sub_keys: list[str] = []
            for sym, st in self.stock_underlyings.items():
                if not st.get("enabled", True):
                    continue
                if st.get("planned_keys"):
                    continue
                spot = st.get("spot_price")
                if not spot or float(spot) <= 0:
                    continue
                keys = self.plan_stock_contracts(sym, float(spot), subscribe_now=False)
                if keys:
                    all_sub_keys.extend(keys)

            if all_sub_keys and self.feed and hasattr(self.feed, "subscribe"):
                try:
                    self.feed.subscribe(all_sub_keys, target="stocks")
                except Exception as exc:
                    LOGGER.debug("Batch stock subscribe failed: %s", exc)

    def plan_stock_contracts(self, sym: str, spot: float, subscribe_now: bool = True) -> list[str]:
        """Plans ATM +- 5 (11 CE + 11 PE = 22 strikes) for a stock from Upstox Master."""
        st = self.stock_underlyings.get(sym)
        if not st or spot <= 0:
            return []

        if sym not in self.ce_by_underlying or not self.ce_by_underlying[sym]:
            exp, official_lot, contracts = self.instruments_master.get_nearest_options(sym)
            if contracts:
                st["lot_size"] = official_lot
                self.expiries_by_underlying[sym] = exp
                self.contracts_by_underlying[sym] = contracts
                ce_m: dict[float, dict[str, Any]] = {}
                pe_m: dict[float, dict[str, Any]] = {}
                for c in contracts:
                    c["underlying_key"] = sym
                    k = c.get("instrument_key")
                    stk = float(c.get("strike_price") or 0.0)
                    itype = str(c.get("instrument_type") or "").upper()
                    if k:
                        self.contracts_by_key[k] = c
                    if itype == "CE":
                        ce_m[stk] = c
                    elif itype == "PE":
                        pe_m[stk] = c
                self.ce_by_underlying[sym] = ce_m
                self.pe_by_underlying[sym] = pe_m

        ce_dict = self.ce_by_underlying.get(sym, {})
        pe_dict = self.pe_by_underlying.get(sym, {})
        if not ce_dict or not pe_dict:
            return []

        all_strikes = sorted(set(ce_dict.keys()) & set(pe_dict.keys()))
        if not all_strikes:
            all_strikes = sorted(set(ce_dict.keys()) | set(pe_dict.keys()))
        if not all_strikes:
            return []

        atm_strike = min(all_strikes, key=lambda s: abs(s - spot))
        atm_idx = all_strikes.index(atm_strike)
        start_idx = max(0, atm_idx - 5)
        end_idx = min(len(all_strikes), start_idx + 11)
        if end_idx - start_idx < 11 and len(all_strikes) >= 11:
            start_idx = max(0, end_idx - 11)
        window_strikes = all_strikes[start_idx:end_idx]

        ce_keys: list[str] = []
        pe_keys: list[str] = []
        keys: list[str] = []
        for s in window_strikes:
            if s in ce_dict and ce_dict[s].get("instrument_key"):
                ck = ce_dict[s]["instrument_key"]
                ce_keys.append(ck)
                keys.append(ck)
            if s in pe_dict and pe_dict[s].get("instrument_key"):
                pk = pe_dict[s]["instrument_key"]
                pe_keys.append(pk)
                keys.append(pk)

        st["planned_keys"] = keys
        st["ce_basket_keys"] = ce_keys
        st["pe_basket_keys"] = pe_keys
        st["atm_plus_minus_keys"] = list(keys)
        st["atm_strike"] = atm_strike
        if st["state"] == "SETUP":
            st["state"] = "PLANNING"

        for k in keys:
            self._key_to_underlying[k] = sym
            if k not in self.hot_cache:
                c = self.contracts_by_key.get(k, {})
                self.hot_cache[k] = {
                    "instrument_key": k,
                    "trading_symbol": c.get("trading_symbol") or k,
                    "underlying_key": sym,
                    "strike": float(c.get("strike_price") or 0.0),
                    "side": str(c.get("instrument_type") or "").upper(),
                    "ref_ltp": None,
                    "ref_source": None,
                    "prev_close": 0.0,
                    "ltp": None,
                    "bid_price": 0.0,
                    "bid_qty": 0.0,
                    "ask_price": 0.0,
                    "ask_qty": 0.0,
                    "spread_pct": None,
                    "last_trade_ms": None,
                    "last_quote_ms": None,
                }

        if subscribe_now and keys and self.feed and hasattr(self.feed, "subscribe"):
            self.feed.subscribe(keys, target="stocks")
        return keys

    def lock_stock_contracts(self, sym: str) -> None:
        st = self.stock_underlyings.get(sym)
        if not st or not st.get("enabled", True):
            return
        if not st.get("planned_keys") and st.get("spot_price"):
            self.plan_stock_contracts(sym, float(st["spot_price"]), subscribe_now=False)
        p_keys = st.get("planned_keys") or []
        if not p_keys:
            return
        st["locked_keys"] = list(p_keys)
        st["state"] = "LOCKED"

    def lock_all_stocks(self) -> None:
        with self._lock:
            self.plan_all_stocks()
            for sym in self.stock_underlyings:
                self.lock_stock_contracts(sym)

    def take_stock_snapshot(self, sym: str) -> None:
        """Captures 09:15:00 single-source-of-truth baseline for a stock's 22 options (mid-price, NEVER prev_close)."""
        st = self.stock_underlyings.get(sym)
        if not st or not st.get("enabled", True):
            return
        if st.get("snapshot_taken") and st.get("baseline_locked"):
            return
        if not st.get("locked_keys"):
            self.lock_stock_contracts(sym)
        l_keys = st.get("locked_keys") or []
        if not l_keys:
            return

        today_s = datetime.now(IST).strftime("%Y-%m-%d")
        existing = self.database.get_baseline_snapshot(today_s, sym) if self.database else None
        if existing and existing.get("baseline_prices"):
            st["baseline_prices"] = {k: float(v) for k, v in existing["baseline_prices"].items() if float(v or 0) > 0}
        else:
            st["baseline_prices"] = {}
            for k in l_keys:
                c = self.hot_cache.get(k, {})
                bid_p = float(c.get("bid_price") or 0.0)
                ask_p = float(c.get("ask_price") or 0.0)
                ltp_p = float(c.get("ltp") or 0.0)
                # Brief Section 3 #8: Stocks use mid-price (bid+ask)/2 if valid, fallback to live trade ltp (NEVER prev_close)
                if bid_p > 0 and ask_p > 0 and ask_p >= bid_p:
                    snap_p = round((bid_p + ask_p) / 2.0, 2)
                elif ltp_p > 0:
                    snap_p = ltp_p
                else:
                    snap_p = 0.0
                if snap_p > 0.0:
                    st["baseline_prices"][k] = snap_p

        self._recompute_unified_baseline_sums(st)
        st["snapshot_taken"] = True
        st["baseline_locked"] = True
        st["state"] = "ARMED"
        if self.database and st["ce_baseline_sum"] > 0 and st["pe_baseline_sum"] > 0:
            try:
                self.database.save_baseline_snapshot(today_s, sym, st["ce_baseline_sum"], st["pe_baseline_sum"], st["baseline_prices"])
            except Exception:
                pass

    def snapshot_all_stocks(self) -> None:
        with self._lock:
            for sym in self.stock_underlyings:
                self.take_stock_snapshot(sym)

    def arm_all_stocks(self) -> None:
        with self._lock:
            for sym, st in self.stock_underlyings.items():
                if st.get("enabled", True) and st.get("state") in ("SETUP", "PLANNING", "LOCKED"):
                    st["state"] = "ARMED"

    def squareoff_all_stocks_1515(self) -> None:
        with self._lock:
            for sym, st in self.stock_underlyings.items():
                pos = st.get("path2_position") or st.get("current_position")
                if pos and isinstance(pos, dict) and pos.get("status") == "OPEN":
                    self._exit_single_trade(pos, "SQUAREOFF_1515", sym)
                st["state"] = "CLOSED"

    def manual_exit_stocks(self, reason: str = "MANUAL") -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        with self._lock:
            for sym, st in list(self.stock_underlyings.items()):
                pos = st.get("path2_position") or st.get("current_position")
                if pos and isinstance(pos, dict) and pos.get("status") == "OPEN":
                    res = self._exit_single_trade(pos, reason, sym)
                    if res:
                        closed.append(res)
        return closed

    def _recompute_unified_baseline_sums(self, state_obj: dict[str, Any]) -> None:
        """
        Single Source of Truth: Computes ce_baseline_sum and pe_baseline_sum directly
        from per-strike baseline_prices so the Summary Table ALWAYS equals the Strike Table.
        """
        b_prices = state_obj.get("baseline_prices") or {}
        if not b_prices:
            return
        ce_keys = state_obj.get("ce_basket_keys") or []
        pe_keys = state_obj.get("pe_basket_keys") or []
        ce_sum = sum(float(b_prices[k]) for k in ce_keys if k in b_prices and float(b_prices.get(k) or 0) > 0)
        pe_sum = sum(float(b_prices[k]) for k in pe_keys if k in b_prices and float(b_prices.get(k) or 0) > 0)
        state_obj["ce_baseline_sum"] = round(ce_sum, 2)
        state_obj["pe_baseline_sum"] = round(pe_sum, 2)

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
                        is_p1 = (tr.get("strategy") == "PATH_1_0915" or str(tr.get("trade_id", "")).startswith("GAP-"))
                        if is_p1:
                            self.index_underlyings[ukey]["path1_trades_count"] = self.index_underlyings[ukey].get("path1_trades_count", 0) + 1
                        else:
                            self.index_underlyings[ukey]["path2_trades_count"] = self.index_underlyings[ukey].get("path2_trades_count", 0) + 1
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

                    # Auto-subscribe open trade contract key to feed for live Trade Book PnL tracking
                    ik = t.get("instrument_key")
                    if ik and hasattr(self, "market_data_feed") and self.market_data_feed and hasattr(self.market_data_feed, "subscribe"):
                        try:
                            self.market_data_feed.subscribe([ik], target="indices")
                        except Exception:
                            pass

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
        """Loads nearest-expiry index option contracts and official lot size from Upstox Daily Instruments Master."""
        sym_map = {
            NIFTY_KEY: "NIFTY",
            BANKNIFTY_KEY: "BANKNIFTY",
            SENSEX_KEY: "SENSEX",
            "NSE_INDEX|Nifty 50": "NIFTY",
            "NSE_INDEX|Nifty Bank": "BANKNIFTY",
            "BSE_INDEX|SENSEX": "SENSEX",
        }
        target_sym = sym_map.get(underlying_key)
        if not target_sym:
            return False

        try:
            act_exp, official_lot, matched = self.instruments_master.get_nearest_options(target_sym)
            if not matched:
                return False

            if official_lot > 0 and underlying_key in self.index_underlyings:
                self.index_lot_sizes[underlying_key] = official_lot
                self.index_underlyings[underlying_key]["lot_size"] = official_lot
                if underlying_key == NIFTY_KEY:
                    self.lot_size = official_lot

            self.contracts_by_underlying[underlying_key] = matched
            self.expiries_by_underlying[underlying_key] = act_exp

            if underlying_key == NIFTY_KEY:
                self.active_expiry = act_exp
                self.all_contracts = matched

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
                "Loaded %s contracts for %s expiry %s (lot_size=%s) from Instruments Master (%s CE, %s PE).",
                len(matched), underlying_key, act_exp, official_lot, len(ce_map), len(pe_map),
            )
            return True
        except Exception as exc:
            LOGGER.error("Failed to load option contracts from Instruments Master for %s: %s", underlying_key, exc)
            return False

    def load_contracts_if_needed(self, underlying_key: str = NIFTY_KEY) -> bool:
        """Fetch option contracts for index (preferring Daily Instruments Master to avoid 429s)."""
        if underlying_key in self.contracts_by_underlying and self.contracts_by_underlying[underlying_key]:
            return True

        # 1. Prefer Daily Instruments Master (1 single download for all 53 symbols + live official lot sizes)
        if self._load_index_contracts_from_master(underlying_key):
            return True

        # 2. Fallback to Upstox Option Contract API if master unavailable
        if self.access_token:
            try:
                enc_key = urllib.parse.quote(underlying_key)
                url = f"https://api.upstox.com/v2/option/contract?instrument_key={enc_key}"
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
                    if contracts:
                        today_str = datetime.now(IST).strftime("%Y-%m-%d")
                        expiries = sorted(list({c["expiry"] for c in contracts if c.get("expiry") and c.get("expiry") >= today_str}))
                        if expiries:
                            act_exp = expiries[0]
                            matched = [c for c in contracts if c.get("expiry") == act_exp]
                            self.contracts_by_underlying[underlying_key] = matched
                            self.expiries_by_underlying[underlying_key] = act_exp

                            if underlying_key == NIFTY_KEY:
                                self.active_expiry = act_exp
                                self.all_contracts = matched

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
                                "Loaded %s live contracts for %s expiry %s from Upstox API (%s CE, %s PE).",
                                len(matched), underlying_key, act_exp, len(ce_map), len(pe_map),
                            )
                            return True
            except Exception as exc:
                LOGGER.warning("Could not load option contracts from Upstox API for %s: %s", underlying_key, exc)

        return False

    def plan_8_itm_contracts(self, spot: float, underlying_key: str = NIFTY_KEY) -> list[str]:
        """
        Plans ATM +- 10 Strikes (21 CE and 21 PE strikes = 42 contracts total, including OTM)
        for Indices (NIFTY 50, BANK NIFTY, SENSEX) for both 09:15 Positive Tick and Sum Signal (Score = CE % - PE %).
        """
        if underlying_key not in self.ce_by_underlying or not self.ce_by_underlying[underlying_key]:
            self.load_contracts_if_needed(underlying_key)

        ce_dict = self.ce_by_underlying.get(underlying_key) or (self.ce_by_strike if underlying_key == NIFTY_KEY else {})
        pe_dict = self.pe_by_underlying.get(underlying_key) or (self.pe_by_strike if underlying_key == NIFTY_KEY else {})

        step = float(self.index_underlyings.get(underlying_key, {}).get("step") or 50.0)
        atm_strike = round(spot / step) * step

        # Select 21 symmetric strikes centered around ATM (ATM-10 .. ATM .. ATM+10) for Indices
        all_strikes = sorted(set(ce_dict.keys()) & set(pe_dict.keys()))
        if not all_strikes:
            all_strikes = sorted(set(ce_dict.keys()) | set(pe_dict.keys()))

        ce_basket_keys: list[str] = []
        pe_basket_keys: list[str] = []
        keys: list[str] = []

        if all_strikes:
            nearest_atm = min(all_strikes, key=lambda s: abs(s - atm_strike))
            atm_strike = nearest_atm
            atm_idx = all_strikes.index(nearest_atm)
            start_idx = max(0, atm_idx - 10)
            end_idx = min(len(all_strikes), start_idx + 21)
            if end_idx - start_idx < 21 and len(all_strikes) >= 21:
                start_idx = max(0, end_idx - 21)
            selected_strikes = all_strikes[start_idx:end_idx]

            for s in selected_strikes:
                c = ce_dict.get(s)
                if c and c.get("instrument_key"):
                    keys.append(c["instrument_key"])
                    ce_basket_keys.append(c["instrument_key"])
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

        # Pre-populate hot cache and O(1) lookup table
        for k in keys:
            self._key_to_underlying[k] = underlying_key
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
        """Plans ATM ± 10 basket contracts (42 options per index) for all enabled indices if spot price is known."""
        for ukey, idx_st in self.index_underlyings.items():
            if idx_st.get("enabled", True):
                spot = idx_st.get("spot_price")
                if spot and spot > 0 and not idx_st.get("planned_keys"):
                    self.plan_8_itm_contracts(spot, underlying_key=ukey)

    # -------------------------------------------------------------------------
    # Timeline Step 1: Lock Time (09:14:55)
    # -------------------------------------------------------------------------

    def lock_contracts(self, underlying_key: str | None = None) -> None:
        """Lock the ATM +- 10 (42 contracts for indices) at 09:14:55 with real live non-zero reference prices."""
        with self._lock:
            today_s = datetime.now(IST).strftime("%Y-%m-%d")
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
                missing_count = 0
                for k in idx_state["locked_keys"]:
                    entry = self.hot_cache.get(k)
                    if not entry:
                        continue
                    live_ltp = float(entry.get("ltp") or 0.0)
                    bid_p = float(entry.get("bid_price") or 0.0)
                    ask_p = float(entry.get("ask_price") or 0.0)
                    if live_ltp > 0.0:
                        entry["ref_ltp"] = live_ltp
                        entry["ref_source"] = "live_ltp"
                    elif bid_p > 0.0 and ask_p > 0.0 and ask_p >= bid_p:
                        entry["ref_ltp"] = round((bid_p + ask_p) / 2.0, 2)
                        entry["ref_source"] = "mid_quote"
                    else:
                        # NEVER fall back to prev_close or fake 0.0 as valid ref; flag MISSING explicitly
                        entry["ref_ltp"] = 0.0
                        entry["ref_source"] = "MISSING_091455"
                        missing_count += 1

                if missing_count > 0:
                    LOGGER.warning(
                        "[09:14:55 LOCK] %s had %d/%d contracts with 0/no live quote at 09:14:55 — flagged MISSING_091455 (no false >0 trigger).",
                        ukey, missing_count, len(idx_state["locked_keys"])
                    )

                snapshot_091455 = []
                for k in idx_state["locked_keys"]:
                    c_info = self.contracts_by_key.get(k, {})
                    h_info = self.hot_cache.get(k, {})
                    snapshot_091455.append({
                        "instrument_key": k,
                        "symbol": h_info.get("trading_symbol") or c_info.get("trading_symbol") or k,
                        "strike": float(h_info.get("strike") or c_info.get("strike_price") or 0.0),
                        "side": str(h_info.get("side") or c_info.get("option_type") or ("CE" if "CE" in str(k) else "PE")).upper(),
                        "ref_ltp": float(h_info.get("ref_ltp") or 0.0),
                        "ref_source": h_info.get("ref_source") or "MISSING_091455",
                        "lock_time": "09:14:55",
                    })
                idx_state["locked_091455_snapshot"] = snapshot_091455
                if hasattr(self, "database") and self.database and snapshot_091455:
                    try:
                        self.database.save_premarket_lock(today_s, ukey, snapshot_091455)
                    except Exception:
                        pass

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
                ce_cnt = len(idx_state.get("ce_basket_keys") or [])
                pe_cnt = len(idx_state.get("pe_basket_keys") or [])
                LOGGER.info("LOCKED %d contracts (%d CE + %d PE) for %s at 09:14:55.", len(snapshot_091455), ce_cnt, pe_cnt, ukey)
                self.add_alert(
                    "PRE_MARKET_LOCKED",
                    f"{idx_state['name']}: 09:14:55 Pre-Market Locked ({len(snapshot_091455)} Contracts: {ce_cnt} CE + {pe_cnt} PE, missing={missing_count})",
                    level="WARNING" if missing_count > 0 else "INFO",
                    details={"underlying": ukey, "count": len(snapshot_091455), "missing_count": missing_count},
                )

    def lock_all_indices(self) -> None:
        self.lock_contracts()

    # -------------------------------------------------------------------------
    # Timeline Step 2: 09:15:00 Snapshot (Unified Single Source of Truth)
    # -------------------------------------------------------------------------

    def take_snapshot(self, underlying_key: str | None = None) -> None:
        """
        At 09:15:00, captures the single unified baseline price per strike from live WebSocket
        (first trade LTP or bid/ask mid-price; NEVER prev_close). Summary sum == Sum(per-strike).
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
                    bid = float(c.get("bid_price") or 0.0)
                    ask = float(c.get("ask_price") or 0.0)
                    ltp = float(c.get("ltp") or 0.0)
                    if ltp <= 0.0 and bid > 0.0 and ask > 0.0 and ask >= bid:
                        ltp = round((bid + ask) / 2.0, 2)
                    oi = float(c.get("oi") or c.get("prev_oi") or 0.0)
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

                # Phase 2: 09:15 Futures Snapshot
                fut_k = idx_state.get("futures_key")
                if fut_k:
                    fut_c = self.hot_cache.get(fut_k, {})
                    fut_ltp = float(fut_c.get("ltp") or idx_state.get("futures_ltp") or 0.0)
                    fut_oi = float(fut_c.get("oi") or idx_state.get("futures_oi") or 0.0)
                    idx_state["futures_snap_ltp"] = fut_ltp
                    idx_state["futures_snap_oi"] = fut_oi
                    idx_state["futures_snap_vol"] = float(fut_c.get("volume") or 0.0)

                idx_state["snapshot"] = idx_snap
                idx_state["snapshot_taken"] = True
                idx_state["state"] = "ARMED"

                # Single Unified 09:15:00 Baseline (NEVER prev_close; summary sum strictly == strike table sum)
                today_s = datetime.now(IST).strftime("%Y-%m-%d")
                existing_snap = None
                if hasattr(self, "database") and self.database:
                    try:
                        existing_snap = self.database.get_baseline_snapshot(today_s, ukey)
                    except Exception:
                        pass

                ce_b_keys = idx_state.get("ce_basket_keys") or [k for k in l_keys if self.hot_cache.get(k, {}).get("side") == "CE"]
                pe_b_keys = idx_state.get("pe_basket_keys") or [k for k in l_keys if self.hot_cache.get(k, {}).get("side") == "PE"]
                basket_set = set(ce_b_keys + pe_b_keys)

                restored_prices: dict[str, float] = {}
                if existing_snap and isinstance(existing_snap.get("baseline_prices"), dict):
                    restored_prices = {
                        k: float(v)
                        for k, v in existing_snap["baseline_prices"].items()
                        if k in basket_set and float(v or 0.0) > 0.0
                    }

                if restored_prices and len(restored_prices) >= len(basket_set) * 0.8:
                    idx_state["baseline_prices"] = restored_prices
                else:
                    idx_state["baseline_prices"] = {}
                    for k in ce_b_keys + pe_b_keys:
                        c = self.hot_cache.get(k, {})
                        ltp_v = float(c.get("ltp") or 0.0)
                        bid_p = float(c.get("bid_price") or 0.0)
                        ask_p = float(c.get("ask_price") or 0.0)
                        if ltp_v > 0.0:
                            snap_p = ltp_v
                        elif bid_p > 0.0 and ask_p > 0.0 and ask_p >= bid_p:
                            snap_p = round((bid_p + ask_p) / 2.0, 2)
                        else:
                            # Never use prev_close; leave absent so first fresh 09:15+ tick locks it
                            snap_p = 0.0
                        if snap_p > 0.0:
                            idx_state["baseline_prices"][k] = snap_p

                for k, bp in idx_state["baseline_prices"].items():
                    if k in idx_snap:
                        idx_snap[k]["ltp"] = float(bp)

                self._recompute_unified_baseline_sums(idx_state)
                ce_base_sum = float(idx_state.get("ce_baseline_sum") or 0.0)
                pe_base_sum = float(idx_state.get("pe_baseline_sum") or 0.0)
                idx_state["baseline_locked"] = True

                if hasattr(self, "database") and self.database and ce_base_sum > 0 and pe_base_sum > 0:
                    try:
                        self.database.save_baseline_snapshot(today_s, ukey, ce_base_sum, pe_base_sum, idx_state["baseline_prices"])
                    except Exception as ex:
                        LOGGER.error("Failed to save baseline snapshot to DB: %s", ex)

                LOGGER.info(
                    "UNIFIED 09:15 BASELINE LOCKED for %s: CE Sum = ₹%.2f, PE Sum = ₹%.2f (%d/%d strikes populated).",
                    ukey, ce_base_sum, pe_base_sum, len(idx_state["baseline_prices"]), len(basket_set)
                )
                self.add_alert(
                    "BASELINE_LOCKED",
                    f"{idx_state['name']}: 09:15 Unified Baseline Locked (CE: ₹{ce_base_sum:.2f}, PE: ₹{pe_base_sum:.2f})",
                    level="INFO",
                    details={"underlying": ukey, "ce_base": ce_base_sum, "pe_base": pe_base_sum},
                )

                if ukey == NIFTY_KEY:
                    self.snapshot = dict(idx_snap)
                    self.snapshot_taken = True
                    self.snapshot_at_ms = int(time.time() * 1000)
                    self.state = "ARMED"

                LOGGER.info("SNAPSHOT TAKEN at 09:15:00 for %s (%d contracts). State -> ARMED.", ukey, len(idx_snap))

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

    def _get_underlying_state(self, underlying_key: str) -> tuple[dict[str, Any] | None, bool]:
        if underlying_key in self.index_underlyings:
            return self.index_underlyings[underlying_key], False
        if underlying_key in self.stock_underlyings:
            return self.stock_underlyings[underlying_key], True
        return None, False

    @staticmethod
    def _effective_option_price(c: dict[str, Any], is_stock: bool, max_spread_pct: float = 5.0, min_prem: float = 5.0) -> tuple[float, bool]:
        """
        Returns (effective_price, is_valid).
        For stocks (Brief Section 3 #8): uses mid-price (bid+ask)/2 if valid;
        skips strikes with spread > max_spread_pct (5%) or premium < min_prem (Rs 5).
        For indices: uses live ltp (or mid-price if ltp unavailable).
        """
        bid = float(c.get("bid_price") or 0.0)
        ask = float(c.get("ask_price") or 0.0)
        ltp = float(c.get("ltp") or 0.0)

        if is_stock:
            if bid > 0.0 and ask > 0.0 and ask >= bid:
                mid = round((bid + ask) / 2.0, 2)
                spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
                if spread_pct > max_spread_pct or mid < min_prem:
                    return 0.0, False
                return mid, True
            if ltp >= min_prem:
                return ltp, True
            return 0.0, False
        else:
            if ltp > 0.0:
                return ltp, True
            if bid > 0.0 and ask > 0.0 and ask >= bid:
                return round((bid + ask) / 2.0, 2), True
            return 0.0, False

    def evaluate_sum_signal(
        self,
        underlying_key: str = NIFTY_KEY,
        trigger_ms: int = 0,
        force: bool = False,
        require_consecutive: bool = False,
        allow_stock_entry: bool = True,
    ) -> dict[str, Any] | None:
        """
        Calculates the real-time Sum Signal for any Index or NIFTY-50 Stock:
        - Basket: ATM +- 5 strikes (11 CE and 11 PE contracts = 22 total).
        - CE % = ((Sum of CE live - Sum of CE 09:15) / Sum of CE 09:15) * 100
        - PE % = ((Sum of PE live - Sum of PE 09:15) / Sum of PE 09:15) * 100
        - Score = CE % - PE %
        - Single unified baseline rule: Summary baseline == exact sum of per-strike 09:15 prices.
        - Independent per-symbol confirmation timer (whipsaw_sec, default 300s).
        """
        with self._lock:
            now_dt = datetime.now(IST)
            now_time_hms = now_dt.strftime("%H:%M:%S")
            now_ts = time.time()

            idx_state, is_stock = self._get_underlying_state(underlying_key)
            if not idx_state:
                idx_state = self.index_underlyings.get(NIFTY_KEY)
                underlying_key = NIFTY_KEY
                is_stock = False
            if not idx_state or not idx_state.get("enabled", True):
                return None

            ce_keys = idx_state.get("ce_basket_keys") or []
            pe_keys = idx_state.get("pe_basket_keys") or []

            if not ce_keys or not pe_keys:
                spot = float(idx_state.get("spot_price") or (0.0 if is_stock else (self.spot_price or 0.0)))
                if spot > 0:
                    if is_stock:
                        self.plan_stock_contracts(underlying_key, spot, subscribe_now=False)
                    else:
                        self.plan_8_itm_contracts(spot, underlying_key=underlying_key)
                    ce_keys = idx_state.get("ce_basket_keys") or []
                    pe_keys = idx_state.get("pe_basket_keys") or []

            if not ce_keys or not pe_keys:
                l_keys = idx_state.get("locked_keys") or []
                ce_keys = [k for k in l_keys if "CE" in self.hot_cache.get(k, {}).get("trading_symbol", "") or self.hot_cache.get(k, {}).get("side") == "CE"]
                pe_keys = [k for k in l_keys if "PE" in self.hot_cache.get(k, {}).get("trading_symbol", "") or self.hot_cache.get(k, {}).get("side") == "PE"]

            b_prices = idx_state.get("baseline_prices") or {}
            has_per_strike_base = bool(b_prices)

            # Backfill any missing strike baseline price ONLY if not using a pre-locked summary baseline without per-strike map
            market_open_hms = getattr(self, "market_open_time", getattr(self, "snapshot_time", "09:15:00"))
            allow_backfill = not (idx_state.get("baseline_locked") and not has_per_strike_base and float(idx_state.get("ce_baseline_sum") or 0.0) > 0.0)
            if allow_backfill and (now_time_hms >= market_open_hms or force):
                for k in ce_keys + pe_keys:
                    if float(b_prices.get(k) or 0.0) <= 0.0:
                        c = self.hot_cache.get(k)
                        if c:
                            eff_p, valid_p = self._effective_option_price(
                                c, is_stock=is_stock,
                                max_spread_pct=self.max_stock_spread_pct,
                                min_prem=self.min_stock_basket_premium,
                            )
                            if valid_p and eff_p > 0.0:
                                b_prices[k] = eff_p
                                idx_state.setdefault("baseline_prices", {})[k] = eff_p

            # Compute live sums strictly matching valid per-strike baseline items (single source of truth)
            ce_curr_sum = 0.0
            ce_matched_base = 0.0
            for k in ce_keys:
                c = self.hot_cache.get(k)
                if not c:
                    continue
                eff_p, valid_p = self._effective_option_price(
                    c, is_stock=is_stock,
                    max_spread_pct=self.max_stock_spread_pct,
                    min_prem=self.min_stock_basket_premium,
                )
                if not valid_p or eff_p <= 0.0:
                    continue
                if has_per_strike_base:
                    bp = float(b_prices.get(k) or 0.0)
                    if bp <= 0.0:
                        continue
                    ce_matched_base += bp
                ce_curr_sum += eff_p

            pe_curr_sum = 0.0
            pe_matched_base = 0.0
            for k in pe_keys:
                c = self.hot_cache.get(k)
                if not c:
                    continue
                eff_p, valid_p = self._effective_option_price(
                    c, is_stock=is_stock,
                    max_spread_pct=self.max_stock_spread_pct,
                    min_prem=self.min_stock_basket_premium,
                )
                if not valid_p or eff_p <= 0.0:
                    continue
                if has_per_strike_base:
                    bp = float(b_prices.get(k) or 0.0)
                    if bp <= 0.0:
                        continue
                    pe_matched_base += bp
                pe_curr_sum += eff_p

            idx_state["ce_current_sum"] = round(ce_curr_sum, 2)
            idx_state["pe_current_sum"] = round(pe_curr_sum, 2)

            if has_per_strike_base:
                self._recompute_unified_baseline_sums(idx_state)
                ce_base = ce_matched_base if ce_matched_base > 0 else float(idx_state.get("ce_baseline_sum") or 0.0)
                pe_base = pe_matched_base if pe_matched_base > 0 else float(idx_state.get("pe_baseline_sum") or 0.0)
            else:
                ce_base = float(idx_state.get("ce_baseline_sum") or 0.0)
                pe_base = float(idx_state.get("pe_baseline_sum") or 0.0)

            # Auto-restore from DB ONLY if per-strike baseline_prices match current basket
            if (ce_base <= 0.0 or pe_base <= 0.0) and hasattr(self, "database") and self.database:
                try:
                    today_s = datetime.now(IST).strftime("%Y-%m-%d")
                    snap = self.database.get_baseline_snapshot(today_s, underlying_key)
                    if snap and isinstance(snap.get("baseline_prices"), dict) and snap["baseline_prices"]:
                        basket_set = set(ce_keys + pe_keys)
                        restored = {
                            k: float(v) for k, v in snap["baseline_prices"].items()
                            if k in basket_set and float(v or 0.0) > 0.0
                        }
                        if restored:
                            idx_state["baseline_prices"] = restored
                            self._recompute_unified_baseline_sums(idx_state)
                            idx_state["baseline_locked"] = True
                            ce_base = float(idx_state["ce_baseline_sum"])
                            pe_base = float(idx_state["pe_baseline_sum"])
                except Exception:
                    pass

            # Auto-lock baseline cleanly from live cache if not yet locked and market is open (preserve any already-captured prices)
            if not idx_state.get("baseline_locked") and (now_time_hms >= market_open_hms or force):
                new_bp: dict[str, float] = dict(idx_state.get("baseline_prices") or {})
                for k in ce_keys + pe_keys:
                    if float(new_bp.get(k) or 0.0) > 0.0:
                        continue
                    c = self.hot_cache.get(k)
                    if c:
                        eff_p, valid_p = self._effective_option_price(
                            c, is_stock=is_stock,
                            max_spread_pct=self.max_stock_spread_pct,
                            min_prem=self.min_stock_basket_premium,
                        )
                        if valid_p and eff_p > 0.0:
                            new_bp[k] = eff_p
                if new_bp:
                    idx_state["baseline_prices"] = new_bp
                    self._recompute_unified_baseline_sums(idx_state)
                    idx_state["baseline_locked"] = True
                    ce_base = float(idx_state["ce_baseline_sum"])
                    pe_base = float(idx_state["pe_baseline_sum"])
                    if hasattr(self, "database") and self.database and ce_base > 0 and pe_base > 0:
                        try:
                            self.database.save_baseline_snapshot(datetime.now(IST).strftime("%Y-%m-%d"), underlying_key, ce_base, pe_base, new_bp)
                        except Exception:
                            pass

            ce_pct = round(((ce_curr_sum - ce_base) / ce_base) * 100.0, 2) if ce_base > 0 else 0.0
            pe_pct = round(((pe_curr_sum - pe_base) / pe_base) * 100.0, 2) if pe_base > 0 else 0.0
            score = round(ce_pct - pe_pct, 2)

            idx_state["ce_pct"] = ce_pct
            idx_state["pe_pct"] = pe_pct
            idx_state["score"] = score

            # ±2% buffer rule (Brief Section 3)
            if ce_pct > 2.0 and pe_pct < -2.0:
                raw_signal = "CE Buy"
            elif pe_pct > 2.0 and ce_pct < -2.0:
                raw_signal = "PE Buy"
            else:
                raw_signal = "Neutral"

            idx_state["raw_signal"] = raw_signal

            # Per-symbol continuous whipsaw hold timer (300s default)
            whipsaw_sec = float(idx_state.get("whipsaw_sec") or getattr(self, "whipsaw_sec", 300.0))
            if raw_signal == "Neutral":
                idx_state["candidate_signal"] = None
                idx_state["candidate_start_time"] = None
                idx_state["hold_elapsed_sec"] = 0.0
                idx_state["confirmed_signal"] = None
                idx_state["_alert_confirmed_sent"] = False
            elif raw_signal in ("CE Buy", "PE Buy"):
                if idx_state.get("candidate_signal") == raw_signal:
                    start_t = idx_state.get("candidate_start_time") or now_ts
                    elapsed = round(now_ts - start_t, 1)
                    idx_state["hold_elapsed_sec"] = elapsed
                    if elapsed >= whipsaw_sec:
                        idx_state["confirmed_signal"] = raw_signal
                        if not idx_state.get("_alert_confirmed_sent"):
                            idx_state["_alert_confirmed_sent"] = True
                            self.add_alert(
                                "SIGNAL_CONFIRMED",
                                f"{idx_state['name']}: {raw_signal} CONFIRMED after {int(elapsed)}s hold (Score: {score:+.2f}%)",
                                level="INFO",
                                details={"underlying": underlying_key, "signal": raw_signal, "score": score, "hold_sec": elapsed},
                            )
                else:
                    idx_state["candidate_signal"] = raw_signal
                    idx_state["candidate_start_time"] = now_ts
                    idx_state["hold_elapsed_sec"] = 0.0
                    idx_state["confirmed_signal"] = None
                    idx_state["_alert_confirmed_sent"] = False

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

            # Record full-day momentum progression (async queued for SQLite; 0 WebSocket lag!)
            last_mom = idx_state.get("_last_momentum_event")
            if not last_mom or abs(ce_pct - last_mom["ce_pct"]) >= 0.05 or abs(pe_pct - last_mom["pe_pct"]) >= 0.05 or raw_signal != last_mom["raw_signal"]:
                ist_now = datetime.now(IST)
                raw_n = underlying_key.replace("NSE_INDEX|", "").replace("BSE_INDEX|", "").strip()
                prev_ce = last_mom["ce_pct"] if last_mom else ce_pct
                prev_pe = last_mom["pe_pct"] if last_mom else pe_pct
                prev_sc = last_mom["score"] if last_mom else score
                delta_ce = round(ce_pct - prev_ce, 2)
                delta_pe = round(pe_pct - prev_pe, 2)
                delta_sc = round(score - prev_sc, 2)

                if not is_stock:
                    mom_entry = {
                        "id": f"mom_{int(now_ts * 1000)}",
                        "time": ist_now.strftime("%H:%M:%S"),
                        "key": underlying_key,
                        "name": raw_n,
                        "ce_pct": ce_pct,
                        "prev_ce_pct": prev_ce,
                        "delta_ce": delta_ce,
                        "pe_pct": pe_pct,
                        "prev_pe_pct": prev_pe,
                        "delta_pe": delta_pe,
                        "score": score,
                        "prev_score": prev_sc,
                        "delta_score": delta_sc,
                        "raw_signal": raw_signal,
                        "hold_sec": round(elapsed_sec),
                        "confirmed": bool(confirmed),
                    }
                    with self._momentum_lock:
                        self.momentum_history.append(mom_entry)
                        if len(self.momentum_history) > 500:
                            self.momentum_history.pop(0)

                if hasattr(self, "database") and self.database:
                    try:
                        log_rec = {
                            "log_date": ist_now.strftime("%Y-%m-%d"),
                            "timestamp_ist": ist_now.strftime("%H:%M:%S"),
                            "timestamp_ms": int(now_ts * 1000),
                            "underlying_key": underlying_key,
                            "index_name": raw_n,
                            "prev_ce_pct": prev_ce,
                            "ce_pct": ce_pct,
                            "delta_ce": delta_ce,
                            "prev_pe_pct": prev_pe,
                            "pe_pct": pe_pct,
                            "delta_pe": delta_pe,
                            "score": score,
                            "raw_signal": raw_signal,
                            "hold_sec": round(elapsed_sec),
                            "confirmed": bool(confirmed),
                        }
                        if hasattr(self.database, "enqueue_momentum_log"):
                            self.database.enqueue_momentum_log(log_rec)
                        else:
                            self.database.insert_momentum_log(log_rec)
                    except Exception:
                        pass

                idx_state["_last_momentum_event"] = {
                    "ce_pct": ce_pct,
                    "pe_pct": pe_pct,
                    "score": score,
                    "raw_signal": raw_signal,
                }

            # Confirmed opposite signal reversal check for open Strategy 2 trade
            target_pos = idx_state.get("path2_position")
            if target_pos:
                pos = target_pos
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
                if is_stock and not allow_stock_entry:
                    return None
                side = "CE" if confirmed == "CE Buy" else "PE"

                # Prevent unintended double exposure on the same symbol if Path 1 is still open
                if not is_stock and not getattr(self, "allow_double_exposure", False) and idx_state.get("path1_position"):
                    return None

                # Check 15-minute cooldown for same direction
                cooldown_until = idx_state.get("cooldown_until")
                if cooldown_until and now_dt < cooldown_until:
                    if idx_state.get("last_closed_side") == side:
                        return None

                max_t = idx_state.get("max_trades_per_day", self.stock_max_trades_per_day if is_stock else self.max_trades_per_day)
                cnt_key = "today_trades_count" if is_stock else "path2_trades_count"
                if idx_state.get(cnt_key, 0) >= max_t:
                    return None

                loss_lim = idx_state.get("daily_loss_limit_inr", self.daily_loss_limit_inr)
                if idx_state.get("realized_pnl", 0.0) <= -loss_lim:
                    return None

                if not force and (now_time_hms < self.entry_start_time or now_time_hms >= self.entry_end_time):
                    return None

                return self._enter_sum_signal_trade(underlying_key=underlying_key, side=side)

            return None

    def _select_itm_strike(
        self,
        underlying_key: str,
        side: str,
        spot: float | None = None,
        allowed_keys: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """
        Brief Req #12: Selects configurable ITM N strike (default ITM 2) with premium in
        [min_entry_premium, max_entry_premium] (Rs 50 - Rs 2,500) and spread <= max_spread_pct.
        Only considers contracts with active live quotes (ask > 0 or ltp > 0).
        """
        st_obj, is_stock = self._get_underlying_state(underlying_key)
        if not st_obj:
            return self._select_best_strike(side)

        if spot is None or spot <= 0:
            spot = float(st_obj.get("spot_price") or st_obj.get("atm_strike") or 0.0)

        basket_keys = list(allowed_keys) if allowed_keys is not None else list(
            (st_obj.get("ce_basket_keys" if side == "CE" else "pe_basket_keys") or [])
            + (st_obj.get("locked_keys") or [])
        )
        seen_k: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for k in basket_keys:
            if k in seen_k:
                continue
            seen_k.add(k)
            c = self.hot_cache.get(k)
            if not c or str(c.get("side") or "").upper() != side:
                continue
            if float(c.get("ask_price") or 0.0) > 0.0 or float(c.get("ltp") or 0.0) > 0.0:
                c_copy = dict(c)
                c_copy.setdefault("instrument_key", k)
                candidates.append(c_copy)

        if not candidates and allowed_keys is None:
            for k_e, e in self.hot_cache.items():
                if (
                    e.get("underlying_key") == underlying_key
                    and str(e.get("side") or "").upper() == side
                    and (float(e.get("ask_price") or 0.0) > 0.0 or float(e.get("ltp") or 0.0) > 0.0)
                ):
                    c_copy = dict(e)
                    c_copy.setdefault("instrument_key", k_e)
                    candidates.append(c_copy)
        if not candidates:
            return None

        itm_depth = max(1, int(getattr(self, "entry_itm_depth", 2)))
        min_prem = float(getattr(self, "min_entry_premium", 50.0))
        max_prem = float(getattr(self, "max_entry_premium", 2500.0))

        # Isolate strict ITM strikes ordered from ITM 1 outward (CE: below spot desc; PE: above spot asc)
        if spot and spot > 0:
            if side == "CE":
                itm_cands = sorted([c for c in candidates if float(c.get("strike") or 0.0) < spot], key=lambda c: float(c.get("strike") or 0.0), reverse=True)
                otm_cands = sorted([c for c in candidates if float(c.get("strike") or 0.0) >= spot], key=lambda c: float(c.get("strike") or 0.0))
            else:
                itm_cands = sorted([c for c in candidates if float(c.get("strike") or 0.0) > spot], key=lambda c: float(c.get("strike") or 0.0))
                otm_cands = sorted([c for c in candidates if float(c.get("strike") or 0.0) <= spot], key=lambda c: float(c.get("strike") or 0.0), reverse=True)
            ordered = []
            # Prioritize requested ITM depth index (0-based = itm_depth - 1) first, then nearby ITM strikes
            if len(itm_cands) >= itm_depth:
                ordered.append(itm_cands[itm_depth - 1])
            for idx_c, c in enumerate(itm_cands):
                if idx_c != itm_depth - 1:
                    ordered.append(c)
            ordered.extend(otm_cands)
        else:
            ordered = candidates

        # Filter by premium bounds [min_entry_premium, max_entry_premium] & valid quotes
        for c in ordered:
            ask = float(c.get("ask_price") or 0.0)
            ltp = float(c.get("ltp") or 0.0)
            prem = ask if ask > 0 else ltp
            if prem <= 0:
                continue
            if min_prem <= prem <= max_prem:
                return c

        # Fallback: closest positive-price candidate if all outside [50, 2500]; never return unquoted 0.00
        pos_cands = [c for c in ordered if (float(c.get("ask_price") or 0.0) > 0 or float(c.get("ltp") or 0.0) > 0)]
        return pos_cands[0] if pos_cands else None

    def _enter_sum_signal_trade(self, underlying_key: str, side: str) -> dict[str, Any] | None:
        """Executes a market entry on confirmed Sum Signal for an Index or Ranked Stock."""
        idx_st, is_stock = self._get_underlying_state(underlying_key)
        if not idx_st:
            return None

        winner = self._select_itm_strike(underlying_key, side, spot=idx_st.get("spot_price"))
        if not winner:
            winner = self._select_atm_strike(side, underlying_key=underlying_key)
        if not winner:
            LOGGER.warning("No candidate strike found for %s %s trade", underlying_key, side)
            return None

        ask = float(winner.get("ask_price") or 0.0)
        ltp = float(winner.get("ltp") or 0.0)
        bid = float(winner.get("bid_price") or 0.0)
        if is_stock and bid > 0 and ask > 0:
            ref_price = ask if self.mode != "paper" else round((bid + ask) / 2.0, 2)
        else:
            ref_price = ask if ask > 0 else (ltp if ltp > 0 else 0.0)
        if ref_price <= 0.0:
            LOGGER.warning("ORDER REJECTED: No live quotes for %s (%s)", winner.get("trading_symbol"), winner.get("instrument_key"))
            return None

        limit_price = round(round((ref_price + self.order_buffer) / 0.05) * 0.05, 2)
        entry_price = ref_price if self.mode == "paper" else limit_price
        sl_price = round(entry_price * 0.80, 2)
        tgt_price = round(entry_price * 1.40, 2)

        lots = self.index_lots.get(underlying_key, idx_st.get("lots", 1)) if not is_stock else idx_st.get("lots", 1)
        lot_sz = idx_st.get("lot_size") or (self.index_lot_sizes.get(underlying_key, self.lot_size) if not is_stock else 100)
        total_qty = lots * lot_sz

        now_dt = datetime.now(IST)
        trade_id = f"SUM-{now_dt.strftime('%m%d%H%M%S')}-{self._trade_counter}"
        self._trade_counter += 1

        trade = {
            "trade_id": trade_id,
            "trade_date": now_dt.strftime("%Y-%m-%d"),
            "strategy": "SUM_SIGNAL",
            "entry_path": "SUM_SIGNAL",
            "stop_loss_price": sl_price,
            "target_price": tgt_price,
            "side": f"BUY_{side}",
            "symbol": winner.get("trading_symbol") or f"{idx_st['name']} {winner.get('strike')} {side}",
            "instrument_key": winner.get("instrument_key") or winner.get("trading_symbol") or "",
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
            "rank": idx_st.get("rank", 0),
            "ce_pct": idx_st.get("ce_pct", 0.0),
            "pe_pct": idx_st.get("pe_pct", 0.0),
            "decision": f"{side} Buy",
            "highest_price": entry_price,
            "trailing_active": False,
            "bot_latency_ms": 0.25,
            "network_latency_ms": 0.0,
        }

        idx_st["path2_position"] = trade
        idx_st["current_position"] = trade
        idx_st["path2_trades_count"] = idx_st.get("path2_trades_count", 0) + 1
        idx_st["today_trades_count"] = idx_st.get("today_trades_count", 0) + 1
        idx_st["state"] = "HOLDING"
        if underlying_key == NIFTY_KEY:
            self.current_position = trade
            self.state = "HOLDING"
            self.today_trades_count += 1

        try:
            self.database.insert_strategy_trade(trade)
        except Exception as exc:
            LOGGER.error("DB insert error for Strategy 2 trade %s: %s", underlying_key, exc)

        LOGGER.info(
            "SUM SIGNAL TRADE EXECUTED for %s: %s at %.2f (ITM-%d, SL: %.2f [-20%%], TGT: %.2f [+40%%], Score: %s)",
            underlying_key, trade["symbol"], entry_price, self.entry_itm_depth, sl_price, tgt_price, idx_st.get("score")
        )
        self.add_alert(
            "ENTRY_ORDER_FILLED",
            f"ENTERED {side} {trade['symbol']} @ ₹{entry_price:.2f} (Score {idx_st.get('score', 0.0):+.2f}%, ITM-{self.entry_itm_depth}, Lots: {lots})",
            level="INFO",
            details={
                "underlying": underlying_key,
                "symbol": trade["symbol"],
                "side": side,
                "entry_price": entry_price,
                "lots": lots,
                "trade_id": trade_id,
            },
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
        idx_st, _ = self._get_underlying_state(underlying_key)
        spot = idx_st.get("spot_price") if idx_st else self.spot_price
        if not spot or spot <= 0:
            return self._select_best_strike(side)

        target_keys = (
            (idx_st.get("atm_plus_minus_keys") or [])
            + (idx_st.get("locked_keys") or [])
            + (idx_st.get("ce_basket_keys" if side == "CE" else "pe_basket_keys") or [])
        ) if idx_st else []
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

    def evaluate_all_stocks_ranking(self, allow_entries: bool = True) -> dict[str, Any]:
        """
        Brief Req #6: Every 1 second, computes CE %, PE %, and Score = CE % - PE %
        for all 50 NIFTY stocks, sorts Rank 1..50, identifies Confirmed Top N Bullish & Bearish,
        and executes entries on newly confirmed Top N stocks.
        """
        now_dt = datetime.now(IST)
        now_hms = now_dt.strftime("%H:%M:%S")
        in_entry_window = (self.entry_start_time <= now_hms < self.entry_end_time)

        with self._lock:
            rankings: list[dict[str, Any]] = []
            for sym, st in self.stock_underlyings.items():
                if not st.get("enabled", True):
                    continue
                # Evaluate signal metrics (reversal exits handled here; fresh entries wait for Top N filter)
                self.evaluate_sum_signal(underlying_key=sym, allow_stock_entry=False)
                rankings.append({
                    "symbol": sym,
                    "key": st.get("key"),
                    "spot_price": round(float(st.get("spot_price") or 0.0), 2),
                    "atm_strike": st.get("atm_strike") or 0.0,
                    "lot_size": st.get("lot_size") or 1,
                    "ce_baseline_sum": st.get("ce_baseline_sum") or 0.0,
                    "pe_baseline_sum": st.get("pe_baseline_sum") or 0.0,
                    "ce_current_sum": st.get("ce_current_sum") or 0.0,
                    "pe_current_sum": st.get("pe_current_sum") or 0.0,
                    "ce_pct": float(st.get("ce_pct") or 0.0),
                    "pe_pct": float(st.get("pe_pct") or 0.0),
                    "score": float(st.get("score") or 0.0),
                    "raw_signal": st.get("raw_signal") or "Neutral",
                    "confirmed_signal": st.get("confirmed_signal"),
                    "hold_elapsed_sec": float(st.get("hold_elapsed_sec") or 0.0),
                    "whipsaw_sec": float(st.get("whipsaw_sec") or self.whipsaw_sec),
                    "has_position": st.get("path2_position") is not None,
                    "status": (
                        f"IN POSITION ({st['path2_position'].get('side')})" if st.get("path2_position")
                        else (f"CONFIRMED {st['confirmed_signal']}" if st.get("confirmed_signal")
                              else (f"{st['raw_signal']} ({int(st.get('hold_elapsed_sec') or 0)}s)" if st.get("raw_signal") != "Neutral" else "Neutral"))
                    ),
                })

            # Sort descending by Score = CE % - PE % (Rank 1 = highest positive, Rank 50 = lowest negative)
            rankings.sort(key=lambda r: r["score"], reverse=True)
            for idx_r, row in enumerate(rankings, start=1):
                row["rank"] = idx_r
                if row["symbol"] in self.stock_underlyings:
                    self.stock_underlyings[row["symbol"]]["rank"] = idx_r

            top_bull = [r for r in rankings if r["confirmed_signal"] == "CE Buy"][:self.max_bullish_stocks]
            top_bear = [r for r in reversed(rankings) if r["confirmed_signal"] == "PE Buy"][:self.max_bearish_stocks]

            if allow_entries and in_entry_window:
                for row in top_bull + top_bear:
                    sym = row["symbol"]
                    st = self.stock_underlyings.get(sym)
                    if not st or st.get("path2_position") is not None:
                        continue
                    side = "CE" if row["confirmed_signal"] == "CE Buy" else "PE"
                    cd_until = st.get("cooldown_until")
                    if cd_until and now_dt < cd_until and st.get("last_closed_side") == side:
                        continue
                    if st.get("today_trades_count", 0) >= st.get("max_trades_per_day", self.stock_max_trades_per_day):
                        continue
                    if st.get("realized_pnl", 0.0) <= -st.get("daily_loss_limit_inr", self.daily_loss_limit_inr):
                        continue
                    self._enter_sum_signal_trade(underlying_key=sym, side=side)

            self._stock_rankings = rankings
            self._top_bullish = top_bull
            self._top_bearish = top_bear
            return {
                "rankings": rankings,
                "top_bullish": top_bull,
                "top_bearish": top_bear,
            }

    def evaluate_all_active_underlyings(self) -> list[dict[str, Any]]:
        """Evaluates all 53 underlyings (3 Indices + 50 NIFTY Stocks) every second."""
        trades: list[dict[str, Any]] = []
        now_dt = datetime.now(IST)
        now_hms = now_dt.strftime("%H:%M:%S")
        if not (self.entry_start_time <= now_hms < "15:30:00"):
            return trades

        with self._lock:
            for ukey, idx_st in self.index_underlyings.items():
                if idx_st.get("enabled", True):
                    t = self.evaluate_sum_signal(underlying_key=ukey)
                    if t and isinstance(t, dict) and t.get("status") == "OPEN":
                        trades.append(t)

            # Every-second ranking & evaluation of all 50 NIFTY stocks
            if time.time() - self._last_stock_rank_ts >= 1.0:
                self._last_stock_rank_ts = time.time()
                self.evaluate_all_stocks_ranking(allow_entries=True)

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
        # In replay/test mode (re_eval_min_interval_sec == 0), honor explicit tick ISO timestamp if provided
        if self.re_eval_min_interval_sec == 0.0 and isinstance(tick.get("timestamp"), str) and "T" in tick["timestamp"]:
            try:
                now_dt = datetime.fromisoformat(tick["timestamp"]).astimezone(IST)
                now_time_hms = now_dt.strftime("%H:%M:%S")
            except Exception:
                pass

        # --- 1. SPOT INDEX OR CASH STOCK TICK ---
        eq_sym = self._key_to_underlying.get(inst_key)
        if eq_sym and eq_sym in self.stock_underlyings:
            self.last_tick_time = time.time()
            st_obj = self.stock_underlyings[eq_sym]
            if ltp > 0:
                st_obj["spot_price"] = ltp
            if tick.get("prev_close") is not None and not st_obj.get("prev_close"):
                st_obj["prev_close"] = float(tick["prev_close"])
            with self._lock:
                market_open_hms = getattr(self, "market_open_time", getattr(self, "snapshot_time", "09:15:00"))
                if self.enabled and st_obj.get("enabled", True) and ltp > 0:
                    if not st_obj.get("ce_basket_keys") and not st_obj.get("pe_basket_keys"):
                        self.plan_stock_contracts(eq_sym, ltp)
                    if self.lock_time <= now_time_hms < market_open_hms:
                        if st_obj.get("state") != "LOCKED":
                            self.lock_stock_contracts(eq_sym)
                    elif now_time_hms >= market_open_hms and not st_obj.get("snapshot_taken"):
                        if not st_obj.get("locked_keys"):
                            self.lock_stock_contracts(eq_sym)
                        self.take_stock_snapshot(eq_sym)
            return None

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

        # --- 2. OPTION CONTRACT TICK ---
        with self._lock:
            entry = self.hot_cache.get(inst_key)
            if entry is None:
                contract = self.contracts_by_key.get(inst_key, {})
                entry = {
                    "instrument_key": inst_key,
                    "trading_symbol": contract.get("trading_symbol") or inst_key,
                    "underlying_key": contract.get("underlying_key") or "",
                    "strike": float(contract.get("strike_price") or 0.0),
                    "side": str(contract.get("instrument_type") or "").upper(),
                    "ref_ltp": None,
                    "ref_source": None,
                    "prev_close": float(contract.get("prev_close") or 0.0),
                    "prev_oi": float(contract.get("open_interest") or 0.0),
                    "oi": None,
                    "build_up": "Neutral",
                    "build_up_class": "bu-neutral",
                    "ltp": ltp,
                    "bid_price": float(tick.get("bid_price") or 0.0),
                    "ask_price": float(tick.get("ask_price") or 0.0),
                }
                self.hot_cache[inst_key] = entry

            oi_changed = False
            if entry is not None:
                if ltp > 0:
                    entry["ltp"] = ltp
                    entry["last_trade_ms"] = ts_ms

                if tick.get("bid_price") is not None:
                    entry["bid_price"] = float(tick["bid_price"])
                if tick.get("ask_price") is not None:
                    entry["ask_price"] = float(tick["ask_price"])

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

            # O(1) fast underlying lookup for 53 underlyings (3 Indices + 50 Stocks)
            idx_key = self._key_to_underlying.get(inst_key)
            if not idx_key:
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
                    self._key_to_underlying[inst_key] = idx_key

            if not idx_key:
                return None

            idx_st, is_stock = self._get_underlying_state(idx_key)
            if not idx_st or not idx_st.get("enabled", True):
                return None

            # 1. Evaluate open position(s) risk and exit rules
            active_positions = []
            if idx_st.get("path1_position"):
                active_positions.append(("path1_position", idx_st["path1_position"]))
            if idx_st.get("path2_position"):
                active_positions.append(("path2_position", idx_st["path2_position"]))
            if not active_positions and idx_st.get("current_position"):
                active_positions.append(("current_position", idx_st["current_position"]))

            for p_type, pos in active_positions:
                if pos.get("instrument_key") == inst_key and pos.get("status") == "OPEN":
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

                        is_path1 = (p_type == "path1_position" or pos.get("entry_path") == "PATH_1_0915")

                        if not is_path1:
                            # Strategy 2 (Sum Signal Live %): Stepped Trailing SL
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

                        # Stop-Loss Check (-20% initial SL, or trailing SL for Strategy 2)
                        sl_p = float(pos.get("stop_loss_price") or (entry_price * 0.80))
                        if current_price <= sl_p:
                            reason = "TRAILING_STOP_LOSS" if pos.get("trailing_active") else "STOP_LOSS_20PCT"
                            return self._exit_single_trade(pos, reason, idx_key)

                        # Target Check (+40% above entry price)
                        tgt_p = float(pos.get("target_price") or (entry_price * 1.40))
                        if current_price >= tgt_p:
                            return self._exit_single_trade(pos, "TARGET_40PCT", idx_key)

                        # Confirmed Signal Reversal Check (Strategy 2 only: opposite signal confirmed after 300s hold)
                        if not is_path1:
                            confirmed_sig = idx_st.get("confirmed_signal")
                            if "CE" in pos.get("side", "") and confirmed_sig == "PE Buy":
                                exited_trade = self._exit_single_trade(pos, "SIGNAL_REVERSAL_PE", idx_key)
                                self._enter_sum_signal_trade(underlying_key=idx_key, side="PE")
                                return exited_trade
                            elif "PE" in pos.get("side", "") and confirmed_sig == "CE Buy":
                                exited_trade = self._exit_single_trade(pos, "SIGNAL_REVERSAL_CE", idx_key)
                                self._enter_sum_signal_trade(underlying_key=idx_key, side="CE")
                                return exited_trade

            # Stocks only run Strategy 2 (evaluated in 1s ranking loop + above exit monitoring)
            if is_stock:
                return None

            # =========================================================================
            # STRATEGY 2: LIVE % MOMENTUM EVALUATION (Independent of Strategy 1)
            # =========================================================================
            if now_time_hms >= self.entry_start_time and not idx_st.get("path2_position"):
                trade = self.evaluate_sum_signal(underlying_key=idx_key, trigger_ms=ts_ms)
                if trade and isinstance(trade, dict) and trade.get("status") == "OPEN":
                    return trade

            # Check daily trade limits, loss limit and cutoff 14:45 for Strategy 1
            max_t = idx_st.get("max_trades_per_day", self.max_trades_per_day)
            if idx_st.get("today_trades_count", 0) >= max_t:
                return None
            if idx_st.get("realized_pnl", 0.0) <= -idx_st.get("daily_loss_limit_inr", self.daily_loss_limit_inr):
                return None
            if self.re_eval_min_interval_sec > 0 and now_time_hms >= self.entry_end_time:
                return None

            # =========================================================================
            # STRATEGY 1: FIRST POSITIVE TICK EVALUATION (Rule R8 strictly 09:15 to 09:18)
            # =========================================================================
            if self.re_eval_min_interval_sec > 0 and now_time_hms >= self.entry_start_time:
                idx_st["path1_done"] = True
                if idx_key == NIFTY_KEY:
                    self.path1_done = True
            elif idx_st.get("state") in ("ARMED", "LOCKED") and not idx_st.get("path1_position") and not idx_st.get("path1_done"):
                ref = entry.get("ref_ltp")
                ref_src = entry.get("ref_source")
                if ref is None or float(ref or 0.0) <= 0.0 or ref_src == "MISSING_091455":
                    # Brief Req #3 / Instr #3: If 09:14:55 price is 0 or missing, log it and capture real non-zero price WITHOUT false positive trigger
                    if ltp > 0:
                        LOGGER.info("09:14:55 reference missing/zero for %s (%s) — recording initial opening tick %.2f (skipping false >0 trigger).", entry.get("trading_symbol"), idx_key, ltp)
                        entry["ref_ltp"] = ltp
                        entry["ref_source"] = "initial_tick"
                    return None

                # Rule R8: Fresh trade strictly above real non-zero ref_ltp
                if ltp > float(ref):
                    chosen_side = entry.get("side") or ("CE" if "CE" in entry.get("trading_symbol", "") else "PE")
                    LOGGER.info(
                        "FIRST POSITIVE TICK DETECTED on %s (%s) for %s! LTP %.2f > Ref %.2f. Side: %s",
                        entry.get("trading_symbol"), inst_key, idx_key, ltp, ref, chosen_side,
                    )
                    idx_st["state"] = "ORDER_SENT"
                    if idx_key == NIFTY_KEY:
                        self.state = "ORDER_SENT"

                    winner = self._select_itm_strike(
                        idx_key, chosen_side,
                        spot=idx_st.get("spot_price"),
                        allowed_keys=idx_st.get("locked_keys"),
                    )
                    if not winner:
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
            sl_price = round(entry_price * 0.80, 2)   # -20% SL as per Dr. Bhilwade specification
            tgt_price = round(entry_price * 1.40, 2)  # +40% Target

            order_side = f"BUY_{side}"
            symbol = winner.get("trading_symbol") or f"{idx_st.get('symbol_prefix', 'INDEX')} {winner.get('strike')} {side}"
            inst_key = winner["instrument_key"]
            strike = winner.get("strike")
            bu_label = winner.get("build_up") or "Neutral"

            trade = {
                "trade_id": trade_id,
                "trade_date": today_str,
                "strategy": "PATH_1_0915",
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
                "score": 0.0,
                "rank": 0,
            }

            idx_st["path1_position"] = trade
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
            is_p1 = (pos.get("entry_path") == "PATH_1_0915" or pos.get("strategy") == "PATH_1_0915" or str(pos.get("trade_id", "")).startswith("GAP-"))
            if is_p1:
                idx_st["path1_position"] = None
            else:
                idx_st["path2_position"] = None
                idx_st["cooldown_until"] = now_dt + timedelta(minutes=15)
                idx_st["last_closed_side"] = "CE" if "CE" in pos.get("side", "") else "PE"

            idx_st["current_position"] = idx_st.get("path2_position") or idx_st.get("path1_position")
            idx_st["realized_pnl"] = round(idx_st.get("realized_pnl", 0.0) + net_pnl, 2)
            loss_lim = idx_st.get("daily_loss_limit_inr", self.daily_loss_limit_inr)
            max_t = idx_state.get("max_trades_per_day", self.max_trades_per_day) if 'idx_state' in locals() and idx_state else idx_st.get("max_trades_per_day", self.max_trades_per_day)

            if reason == "SQUAREOFF_1515" or idx_st["realized_pnl"] <= -loss_lim:
                idx_st["state"] = "CLOSED"
                if idx_st["realized_pnl"] <= -loss_lim:
                    msg = f"DAILY LOSS LIMIT REACHED for {idx_st['name']}: Loss ₹{abs(idx_st['realized_pnl']):,.2f} exceeds limit ₹{loss_lim:,.2f}. Trading stopped for today."
                    LOGGER.warning(msg)
                    self.add_alert("DAILY_LOSS_LIMIT", msg, level="WARNING", details={"underlying": underlying_key, "loss": idx_st["realized_pnl"], "limit": loss_lim})
            elif is_p1:
                # Path 1 exit leaves Strategy 2 armed and running
                idx_st["state"] = "ARMED"
                LOGGER.info("Index %s Strategy 1 trade exited. State remains ARMED for Strategy 2.", underlying_key)
            elif idx_st.get("path2_trades_count", 0) < max_t:
                idx_st["state"] = "ARMED"
                LOGGER.info("Index %s Strategy 2 auto-rearmed after trade exit (trade #%d of %d max).", underlying_key, idx_st.get("path2_trades_count", 0), max_t)
            else:
                idx_st["state"] = "CLOSED"
                LOGGER.info("Index %s completed daily maximum %d trades for Strategy 2. Closed for today.", underlying_key, max_t)

            if underlying_key == NIFTY_KEY:
                self.current_position = idx_st["current_position"]
                self.state = idx_st["state"]

        if underlying_key in self.stock_underlyings:
            st = self.stock_underlyings[underlying_key]
            is_p1 = (pos.get("entry_path") == "PATH_1_0915" or pos.get("strategy") == "PATH_1_0915" or str(pos.get("trade_id", "")).startswith("GAP-"))
            if is_p1:
                st["path1_position"] = None
            else:
                st["path2_position"] = None
                st["cooldown_until"] = now_dt + timedelta(minutes=15)
                st["last_closed_side"] = "CE" if "CE" in pos.get("side", "") else "PE"

            st["current_position"] = st.get("path2_position") or st.get("path1_position")
            st["realized_pnl"] = round(st.get("realized_pnl", 0.0) + net_pnl, 2)

            if reason == "SQUAREOFF_1515":
                st["state"] = "CLOSED"
            elif self.stock_max_trades_per_day > 0 and st.get("today_trades_count", 0) >= self.stock_max_trades_per_day:
                st["state"] = "CLOSED"
                LOGGER.info("Stock %s completed daily maximum %d trades. Closed for today.", underlying_key, self.stock_max_trades_per_day)
            else:
                st["state"] = "ARMED"
                LOGGER.info("Stock %s auto-rearmed after trade exit (trade #%d, 15m same-direction cooldown active).", underlying_key, st.get("today_trades_count", 0))

            # Only unsubscribe if this option key is NOT part of the active 22-strike basket
            basket_set = set(st.get("ce_basket_keys") or []) | set(st.get("pe_basket_keys") or [])
            if self.feed and hasattr(self.feed, "unsubscribe") and inst_key and inst_key not in basket_set:
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

    def manual_exit(self, reason: str = "MANUAL", underlying_key: str | None = None) -> dict[str, Any] | None:
        """Manual Exit Now button: exits specified open position or all open positions across indices."""
        with self._lock:
            u_clean = str(underlying_key).strip().upper() if underlying_key else ""
            if u_clean in ("INDICES", "INDEX", "ALL", "", "NONE"):
                closed = self.manual_exit_indices(reason)
                return closed[0] if closed else None

            ukey = self.resolve_underlying_key(underlying_key)
            pos = self.current_positions.get(ukey)
            if not pos and ukey in self.index_underlyings:
                pos = self.index_underlyings[ukey].get("current_position")
            if pos and pos.get("status") == "OPEN":
                return self._exit_single_trade(pos, reason, ukey)
            return None

    def squareoff_all_indices_1515(self) -> None:
        """15:15:00 EOD clock square-off across all active index positions."""
        with self._lock:
            for ukey, idx_st in self.index_underlyings.items():
                for p_key in ("path1_position", "path2_position", "current_position"):
                    pos = idx_st.get(p_key)
                    if pos and isinstance(pos, dict) and pos.get("status") == "OPEN":
                        self._exit_single_trade(pos, "SQUAREOFF_1515", ukey)
                idx_st["state"] = "CLOSED"

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
        self.add_alert(
            "INDEX_ARMED",
            f"{idx_st['name']}: ARMED at spot ₹{spot:.2f} (Watching Sum Signal & First Positive Tick)",
            level="INFO",
            details={"underlying": ukey, "spot": spot},
        )

    def force_arm_now(self, underlying_key: str | None = None) -> dict[str, Any]:
        """Forces immediate planning, locking, fresh snapshot, and arming for indices."""
        with self._lock:
            u_clean = str(underlying_key).strip().upper() if underlying_key else ""

            if u_clean in ("INDICES", "INDEX", "ALL", ""):
                for ukey in self.index_underlyings:
                    self._force_arm_single_index(ukey)
                return self.get_status()

            if u_clean in ("STOCKS", "STOCK"):
                return self.get_status()

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

    # -------------------------------------------------------------------------
    # Status & Configuration API
    # -------------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        return {
            "strategy": "Dr. Vijay Bhilwade Sum Signal (Score = CE % - PE %) + Opening Tick",
            "enabled": self.enabled,
            "mode": self.mode,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "lots_full": self.lots_full,
            "lots_normal": self.lots_normal,
            "lots_scalp": self.lots_scalp,
            "max_bullish_stocks": getattr(self, "max_bullish_stocks", 5),
            "max_bearish_stocks": getattr(self, "max_bearish_stocks", 5),
            "stock_lots": getattr(self, "stock_lots", 1),
            "stock_max_trades_per_day": getattr(self, "stock_max_trades_per_day", 5),
            "entry_itm_depth": getattr(self, "entry_itm_depth", 2),
            "min_entry_premium": getattr(self, "min_entry_premium", 50.0),
            "max_entry_premium": getattr(self, "max_entry_premium", 2500.0),
            "whipsaw_sec": getattr(self, "whipsaw_sec", 300.0),
            "allow_double_exposure": getattr(self, "allow_double_exposure", False),
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

    def get_locked_091455(self) -> dict[str, Any]:
        """Returns the 09:14:55 locked baseline strikes and non-zero prices for all 3 indices."""
        res = {}
        for ukey, idx_st in self.index_underlyings.items():
            contracts = list(idx_st.get("locked_091455_snapshot") or [])
            is_locked = bool(idx_st.get("locked_091455_snapshot"))
            if not contracts:
                p_keys = idx_st.get("planned_keys") or (idx_st.get("ce_basket_keys", []) + idx_st.get("pe_basket_keys", []))
                for k in p_keys:
                    c_info = self.contracts_by_key.get(k, {})
                    h_info = self.hot_cache.get(k, {})
                    ref_val = float(h_info.get("ref_ltp") or h_info.get("ltp") or 0.0)
                    contracts.append({
                        "instrument_key": k,
                        "symbol": h_info.get("trading_symbol") or c_info.get("trading_symbol") or k,
                        "strike": float(h_info.get("strike") or c_info.get("strike_price") or 0.0),
                        "side": str(h_info.get("side") or c_info.get("instrument_type") or ("CE" if "CE" in str(k) else "PE")).upper(),
                        "ref_ltp": ref_val,
                        "ref_source": h_info.get("ref_source") or "planned (pre-lock)",
                        "lock_time": "PENDING (09:14:55)",
                    })
            res[ukey] = {
                "name": idx_st.get("name", ukey),
                "locked": is_locked,
                "lock_time": self.lock_time if is_locked else "09:14:55",
                "atm_strike": idx_st.get("atm_strike"),
                "spot_price": idx_st.get("spot_price"),
                "contracts": contracts,
            }
        return res

    def get_baseline_proof_rows(self, include_stocks: bool = True) -> list[dict[str, Any]]:
        """Generates full 09:14:55 lock + 09:15:00 baseline proof rows for indices and stocks."""
        rows: list[dict[str, Any]] = []
        with self._lock:
            underlyings = list(self.index_underlyings.items())
            if include_stocks:
                underlyings.extend(list(self.stock_underlyings.items()))
            for ukey, st in underlyings:
                name = st.get("name") or st.get("symbol") or ukey
                lock_map = {c.get("instrument_key"): c for c in (st.get("locked_091455_snapshot") or []) if c.get("instrument_key")}
                base_prices = st.get("baseline_prices") or {}
                snap = st.get("snapshot") or {}
                keys = (st.get("ce_basket_keys") or []) + (st.get("pe_basket_keys") or [])
                if not keys:
                    keys = st.get("locked_keys") or []
                for k in keys:
                    hc = self.hot_cache.get(k, {})
                    lk = lock_map.get(k, {})
                    ref_091455 = float(lk.get("ref_ltp") if lk.get("ref_ltp") is not None else (hc.get("ref_ltp") or 0.0))
                    ref_src = str(lk.get("ref_source") or hc.get("ref_source") or ("MISSING_091455" if ref_091455 <= 0 else "live_091455"))
                    base_0915 = float(base_prices.get(k) or (snap.get(k) or {}).get("ltp") or 0.0)
                    ltp = float(hc.get("ltp") or 0.0)
                    bid = float(hc.get("bid_price") or 0.0)
                    ask = float(hc.get("ask_price") or 0.0)
                    mid = round((bid + ask) / 2.0, 2) if bid > 0 and ask > 0 else ltp
                    pct_vs_base = round(((ltp - base_0915) / base_0915) * 100.0, 2) if base_0915 > 0 and ltp > 0 else 0.0
                    rows.append({
                        "underlying_key": ukey,
                        "name": name,
                        "spot_price": float(st.get("spot_price") or 0.0),
                        "atm_strike": float(st.get("atm_strike") or 0.0),
                        "lot_size": int(st.get("lot_size") or 1),
                        "instrument_key": k,
                        "symbol": hc.get("trading_symbol") or lk.get("symbol") or k,
                        "side": str(hc.get("side") or lk.get("side") or "").upper(),
                        "strike": float(hc.get("strike") or lk.get("strike") or 0.0),
                        "locked_091455_ltp": round(ref_091455, 2),
                        "locked_091455_source": ref_src,
                        "baseline_091500_ltp": round(base_0915, 2),
                        "live_ltp": round(ltp, 2),
                        "live_mid": mid,
                        "pct_vs_0915": pct_vs_base,
                        "ce_baseline_sum": float(st.get("ce_baseline_sum") or 0.0),
                        "pe_baseline_sum": float(st.get("pe_baseline_sum") or 0.0),
                    })
        return rows

    def update_config(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "lots_full" in body:
                self.lots_full = max(1, int(body["lots_full"]))
            if "lots_normal" in body:
                self.lots_normal = max(1, int(body["lots_normal"]))
            if "lots_scalp" in body:
                self.lots_scalp = max(1, int(body["lots_scalp"]))
            if "max_bullish_stocks" in body:
                self.max_bullish_stocks = max(1, min(10, int(body["max_bullish_stocks"])))
            if "max_bearish_stocks" in body:
                self.max_bearish_stocks = max(1, min(10, int(body["max_bearish_stocks"])))
            if "stock_lots" in body:
                self.stock_lots = max(1, int(body["stock_lots"]))
            if "stock_max_trades_per_day" in body:
                self.stock_max_trades_per_day = max(1, int(body["stock_max_trades_per_day"]))
            if "entry_itm_depth" in body:
                self.entry_itm_depth = max(1, min(5, int(body["entry_itm_depth"])))
            if "min_entry_premium" in body:
                self.min_entry_premium = max(0.0, float(body["min_entry_premium"]))
            if "max_entry_premium" in body:
                self.max_entry_premium = max(self.min_entry_premium + 1.0, float(body["max_entry_premium"]))
            if "whipsaw_sec" in body:
                self.whipsaw_sec = max(10.0, float(body["whipsaw_sec"]))
                for d in list(self.index_underlyings.values()) + list(self.stock_underlyings.values()):
                    d["whipsaw_sec"] = self.whipsaw_sec
            if "allow_double_exposure" in body:
                self.allow_double_exposure = bool(body["allow_double_exposure"])
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
        """Returns all currently open positions (indices + stocks) with live marked-to-market P&L."""
        live_pos = {}
        with self._lock:
            all_states = list(self.index_underlyings.values()) + list(self.stock_underlyings.values())
            for idx_st in all_states:
                for p_key in ("path1_position", "path2_position", "current_position"):
                    pos = idx_st.get(p_key)
                    if not pos or not isinstance(pos, dict) or pos.get("status") == "CLOSED":
                        continue
                    tid = pos.get("trade_id")
                    if not tid or tid in live_pos:
                        continue
                    ik = pos.get("instrument_key")
                    hc = self.hot_cache.get(ik) or {}
                    bid = float(hc.get("bid_price") or 0.0)
                    ask = float(hc.get("ask_price") or 0.0)
                    ltp_val = float(hc.get("ltp") or 0.0)
                    entry_p = float(pos.get("entry_price") or 0.0)
                    qty = int(pos.get("total_qty") or pos.get("lot_size") or 1)
                    cur_p = bid if bid > 0 else (ltp_val if ltp_val > 0 else (ask if ask > 0 else entry_p))
                    pts = round(cur_p - entry_p, 2)
                    net = round(pts * qty, 2)
                    p_copy = dict(pos)
                    p_copy["current_price"] = cur_p
                    p_copy["bid_price"] = bid
                    p_copy["ask_price"] = ask
                    p_copy["pnl_points"] = pts
                    p_copy["net_pnl"] = net
                    live_pos[tid] = p_copy
        return live_pos

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            trades = self.database.get_strategy_trades(limit=200, trade_date=today_str)
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
                "strategy_name": "Dr. Vijay Bhilwade Sum Signal (NIFTY 50, BANK NIFTY, SENSEX + 50 Stocks)",
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
                "stocks_tracked": len(self.stock_underlyings),
                "stock_rankings": list(getattr(self, "_stock_rankings", [])),
                "top_bullish": list(getattr(self, "_top_bullish", [])),
                "top_bearish": list(getattr(self, "_top_bearish", [])),
                "stock_underlyings": {
                    ukey: {
                        "key": d["key"],
                        "symbol": d.get("symbol", ukey),
                        "state": d.get("state"),
                        "spot_price": d.get("spot_price"),
                        "ce_pct": d.get("ce_pct", 0.0),
                        "pe_pct": d.get("pe_pct", 0.0),
                        "score": d.get("score", 0.0),
                        "raw_signal": d.get("raw_signal", "Neutral"),
                        "confirmed_signal": d.get("confirmed_signal"),
                        "has_position": (d.get("path2_position") or d.get("path1_position") or d.get("current_position")) is not None,
                        "today_trades_count": d.get("today_trades_count", 0),
                    }
                    for ukey, d in self.stock_underlyings.items()
                    if d.get("spot_price")
                },
                "config": self.get_config(),
                "csv_path": str(self.csv_path),
            }


# =========================================================================
# ACTIVE STRATEGY SPECIFICATION (Indices & All 50 NIFTY Stocks)
# =========================================================================
"""
1. STRATEGY 1: Opening Gap Entry - First Positive Tick (Indices: 09:14:55 to 09:18:00)
   - Locks ATM ± 10 contracts (42 options per index) at 09:14:55, stores fixed ref_ltp, arms at 09:15:00.
   - First positive tick (LTP > ref_ltp) enters tightest spread contract on that side.

2. STRATEGY 2: Dr. Vijay Bhilwade Sum Signal & Real-Time Ranking Strategy
   - Indices: ATM ± 10 basket (21 CE + 21 PE = 42 contracts per index).
   - Stocks: ATM ± 5 basket (11 CE + 11 PE = 22 contracts per stock) across all 50 Nifty stocks.
   - 09:15:00 Baseline snapshot locked for CE sum and PE sum across all 53 underlyings.
   - Live Score = CE % - PE %
   - Real-time 1 to 50 stock ranking:
       * Top 3 Bullish (Rank 1, 2, 3): BUY CE (ITM-2) with 300s confirmation.
       * Bottom 3 Bearish (Rank 48, 49, 50): BUY PE (ITM-2) with 300s confirmation.
   - Position Management: -20% initial SL, +40% target, +5% to cost stepped trailing SL.
   - Trade Isolation Rule: Open positions are NEVER cut due to rank drop; managed solely by SL/TP/Reversal.
"""
