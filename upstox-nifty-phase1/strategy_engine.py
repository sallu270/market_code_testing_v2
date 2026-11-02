from __future__ import annotations

import csv
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from database import MarketDatabase

LOGGER = logging.getLogger("strategy_engine")
IST = ZoneInfo("Asia/Kolkata")

# --- Multi-index configuration ---
# key  : Upstox instrument_key for the underlying
# step : strike spacing in points
# itm  : number of steps ITM from ATM for entry
# lot  : lot size
# name : label used in symbol string
INDEX_CONFIG: dict[str, dict[str, Any]] = {
    "NSE_INDEX|Nifty 50": {
        "step": 50.0,
        "itm_steps": 2,
        "lot": 65,
        "name": "NIFTY",
    },
    "NSE_INDEX|Nifty Bank": {
        "step": 100.0,
        "itm_steps": 2,
        "lot": 15,
        "name": "BANKNIFTY",
    },
    "BSE_INDEX|SENSEX": {
        "step": 100.0,
        "itm_steps": 2,
        "lot": 10,
        "name": "SENSEX",
    },
}


def get_underlying_key_for_trade(trade: dict[str, Any]) -> str:
    if trade.get("underlying_key"):
        return trade["underlying_key"]
    sym = str(trade.get("symbol") or "")
    if "BANKNIFTY" in sym:
        return "NSE_INDEX|Nifty Bank"
    elif "SENSEX" in sym:
        return "BSE_INDEX|SENSEX"
    return "NSE_INDEX|Nifty 50"


class PressureStrategyEngine:
    def __init__(self, database: MarketDatabase, base_dir: Path) -> None:
        self.database = database
        self.base_dir = base_dir
        self.csv_path = base_dir / "paper_trades.csv"
        self._lock = threading.RLock()

        # Strategy Configuration (tunable from UI)
        self.enabled: bool = True
        self.qty_threshold: float = 50.0
        self.value_threshold_lakhs: float = 5.0
        self.use_qty_condition: bool = True
        self.use_value_condition: bool = True
        self.use_day_high_condition: bool = True
        self.use_prev_close_condition: bool = True
        self.use_uptick_condition: bool = True

        self.start_time: str = "09:15:00"
        self.end_time: str = "15:15:00"
        self.lot_size: int = 65

        # Contract tracking state:
        # key -> {"volume": float, "ltp": float, "day_high": float, "prev_close": float,
        #          "last_signaled_vol": float}
        self.contract_history: dict[str, dict[str, float]] = {}

        # Position tracking per underlying index:
        # e.g., "NSE_INDEX|Nifty 50": dict | None
        self.current_positions: dict[str, dict[str, Any] | None] = {
            "NSE_INDEX|Nifty 50": None,
            "NSE_INDEX|Nifty Bank": None,
            "BSE_INDEX|SENSEX": None,
        }
        self._trade_counter: int = 1
        self._eod_timer: threading.Timer | None = None
        self._init_csv()
        self._resume_open_position()
        self._schedule_eod_squareoff()

    @property
    def current_position(self) -> dict[str, Any] | None:
        """Returns the primary active open position for backwards compatibility."""
        with self._lock:
            for pos in self.current_positions.values():
                if pos is not None and pos.get("status") == "OPEN":
                    return pos
            return None

    def _init_csv(self) -> None:
        try:
            if not self.csv_path.exists():
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Trade ID", "Date", "Side", "Symbol", "Strike", "Lot Size",
                        "Entry Time (IST)", "Entry Price",
                        "Exit Time (IST)", "Exit Price", "Exit Reason",
                        "P&L Points", "Net P&L (INR)", "Status",
                        "Spot at Entry", "ATM at Entry",
                    ])
        except Exception as exc:
            LOGGER.error("Failed to init paper_trades.csv: %s", exc)

    def _resume_open_position(self) -> None:
        try:
            trades = self.database.get_strategy_trades(limit=50)
            if trades:
                self._trade_counter = len(trades) + 1
            for t in trades:
                if t.get("status") == "OPEN":
                    u_key = get_underlying_key_for_trade(t)
                    if self.current_positions.get(u_key) is None:
                        self.current_positions[u_key] = t
                        LOGGER.info("Resumed open paper trade for %s: %s", u_key, t["trade_id"])
        except Exception as exc:
            LOGGER.error("Failed to resume open position: %s", exc)

    # --- #5 Background EOD Square-off at 15:15:00 IST sharp ---
    def _schedule_eod_squareoff(self) -> None:
        """Schedule a background Timer to fire at exactly 15:15:00 IST."""
        try:
            now = datetime.now(IST)
            target = now.replace(hour=15, minute=15, second=0, microsecond=0)
            if now >= target:
                # Already past 15:15 today — schedule for tomorrow (no-op for trading)
                target += timedelta(days=1)
            delay = (target - now).total_seconds()
            if self._eod_timer is not None:
                self._eod_timer.cancel()
            self._eod_timer = threading.Timer(delay, self._force_eod_squareoff)
            self._eod_timer.daemon = True
            self._eod_timer.name = "eod-squareoff-timer"
            self._eod_timer.start()
            LOGGER.info("EOD square-off timer set: fires in %.1f seconds (at 15:15:00 IST)", delay)
        except Exception as exc:
            LOGGER.error("Failed to schedule EOD square-off timer: %s", exc)

    def _force_eod_squareoff(self) -> None:
        """Called by the background Timer at 15:15:00 IST to force-close any open positions across all instruments."""
        with self._lock:
            now_dt = datetime.now(IST)
            now_ms = int(time.time() * 1000)
            # #9 millisecond timestamp
            time_ist = now_dt.strftime("%H:%M:%S.%f")[:-3]
            open_count = sum(1 for p in self.current_positions.values() if p is not None and p.get("status") == "OPEN")
            if open_count > 0:
                LOGGER.info("EOD Timer fired at %s — forcing square-off on %d active positions.", time_ist, open_count)
                for u_key, pos in list(self.current_positions.items()):
                    if pos is not None and pos.get("status") == "OPEN":
                        key = pos.get("instrument_key", "")
                        hist = self.contract_history.get(key, {})
                        exit_price = float(hist.get("ltp", 0.0))
                        if exit_price > 0:
                            exit_price = round(exit_price * 0.999, 2)
                        else:
                            exit_price = float(pos.get("entry_price", 0.0))
                        self._exit_position(
                            pos=pos,
                            exit_price=exit_price,
                            reason="EOD_1515",
                            timestamp_ms=now_ms,
                            time_ist=time_ist,
                            underlying_key=u_key,
                        )
            else:
                LOGGER.info("EOD Timer fired at 15:15 — no open positions to square off.")

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "qty_threshold": self.qty_threshold,
                "value_threshold_lakhs": self.value_threshold_lakhs,
                "use_qty_condition": self.use_qty_condition,
                "use_value_condition": self.use_value_condition,
                "use_day_high_condition": self.use_day_high_condition,
                "use_prev_close_condition": self.use_prev_close_condition,
                "use_uptick_condition": self.use_uptick_condition,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "lot_size": self.lot_size,
            }

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "enabled" in updates:
                self.enabled = bool(updates["enabled"])
            if "qty_threshold" in updates:
                self.qty_threshold = max(1.0, float(updates["qty_threshold"]))
            if "value_threshold_lakhs" in updates:
                self.value_threshold_lakhs = max(0.1, float(updates["value_threshold_lakhs"]))
            if "use_qty_condition" in updates:
                self.use_qty_condition = bool(updates["use_qty_condition"])
            if "use_value_condition" in updates:
                self.use_value_condition = bool(updates["use_value_condition"])
            if "use_day_high_condition" in updates:
                self.use_day_high_condition = bool(updates["use_day_high_condition"])
            if "use_prev_close_condition" in updates:
                self.use_prev_close_condition = bool(updates["use_prev_close_condition"])
            if "use_uptick_condition" in updates:
                self.use_uptick_condition = bool(updates["use_uptick_condition"])
            return self.get_config()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            trades = self.database.get_strategy_trades(limit=200)
            now_ist = datetime.now(IST)
            today_str = now_ist.strftime("%Y-%m-%d")
            today_start_ms = int(datetime(now_ist.year, now_ist.month, now_ist.day, tzinfo=IST).timestamp() * 1000)

            # --- TODAY'S TRADES & P&L ---
            today_trades = [
                t for t in trades
                if t.get("trade_date") == today_str or (t.get("entry_timestamp_ms") or 0) >= today_start_ms
            ]
            today_closed = [t for t in today_trades if t.get("status") == "CLOSED"]
            today_pnl = sum(float(t.get("net_pnl") or 0.0) for t in today_closed)
            today_pnl_pts = sum(float(t.get("pnl_points") or 0.0) for t in today_closed)
            today_wins = sum(1 for t in today_closed if float(t.get("net_pnl") or 0.0) > 0)
            today_losses = sum(1 for t in today_closed if float(t.get("net_pnl") or 0.0) < 0)

            # --- LIFETIME TRADES & P&L ---
            all_closed = [t for t in trades if t.get("status") == "CLOSED"]
            lifetime_pnl = sum(float(t.get("net_pnl") or 0.0) for t in all_closed)
            lifetime_pnl_pts = sum(float(t.get("pnl_points") or 0.0) for t in all_closed)
            lifetime_wins = sum(1 for t in all_closed if float(t.get("net_pnl") or 0.0) > 0)
            lifetime_losses = sum(1 for t in all_closed if float(t.get("net_pnl") or 0.0) < 0)

            return {
                "enabled": self.enabled,
                "current_position": self.current_position,
                "current_positions": {k: v for k, v in self.current_positions.items() if v is not None and v.get("status") == "OPEN"},
                "today_date": today_str,
                # Today metrics (what "TRADES MADE TODAY" and "TODAY P&L" should show)
                "today_trades": len(today_trades),
                "today_closed_trades": len(today_closed),
                "today_win_trades": today_wins,
                "today_loss_trades": today_losses,
                "today_net_pnl_inr": round(today_pnl, 2),
                "today_pnl_points": round(today_pnl_pts, 2),
                # Lifetime metrics
                "lifetime_trades": len(trades),
                "lifetime_closed_trades": len(all_closed),
                "lifetime_win_trades": lifetime_wins,
                "lifetime_loss_trades": lifetime_losses,
                "lifetime_net_pnl_inr": round(lifetime_pnl, 2),
                "lifetime_pnl_points": round(lifetime_pnl_pts, 2),
                # Compatibility fields (now mapped directly to TODAY)
                "total_trades": len(today_trades),
                "closed_trades": len(today_closed),
                "win_trades": today_wins,
                "loss_trades": today_losses,
                "net_pnl_inr": round(today_pnl, 2),
                "pnl_points_total": round(today_pnl_pts, 2),
                "config": self.get_config(),
                "csv_path": str(self.csv_path),
            }

    def process_option_chain_snapshot(
        self,
        spot_price: float | None,
        strikes: list[dict[str, Any]],
        instrument_key: str = "NSE_INDEX|Nifty 50",
    ) -> dict[str, Any] | None:
        if not self.enabled or not spot_price or not strikes:
            return None

        # Resolve per-index config
        idx_cfg = INDEX_CONFIG.get(instrument_key, INDEX_CONFIG["NSE_INDEX|Nifty 50"])
        step = idx_cfg["step"]

        with self._lock:
            now_dt = datetime.now(IST)
            # #9 — millisecond timestamp in all time strings
            now_time_str = now_dt.strftime("%H:%M:%S.%f")[:-3]
            now_time_hms = now_dt.strftime("%H:%M:%S")  # for comparisons only
            now_ms = int(time.time() * 1000)

            # EOD square-off is handled by the background Timer (_force_eod_squareoff).
            # This poll-based check is a safety net only.
            if now_time_hms >= self.end_time:
                pos = self.current_positions.get(instrument_key)
                if pos is not None and pos.get("status") == "OPEN":
                    LOGGER.info("15:15 EOD safety-net square-off triggered via poll for %s.", instrument_key)
                    self._exit_position(
                        pos=pos,
                        exit_price=self._find_current_price(pos["instrument_key"], strikes, is_exit=True),
                        reason="EOD_1515",
                        timestamp_ms=now_ms,
                        time_ist=now_time_str,
                        underlying_key=instrument_key,
                    )
                return None

            # Skip before 09:15, but pre-warm contract baseline so 09:15:00.xxx ticks can trigger immediately
            if now_time_hms < self.start_time:
                self._warmup_strikes(strikes)
                return None

            atm_strike = round(spot_price / step) * step
            one_percent = spot_price * 0.01

            # Build fast lookup map: strike -> item
            strike_map = {float(item["strike_price"]): item for item in strikes if item.get("strike_price") is not None}

            # Evaluate buy pressure across in-the-money strikes within 1%
            for item in strikes:
                strike = item.get("strike_price")
                if strike is None:
                    continue
                strike = float(strike)

                # 1. CE In-The-Money: strike < spot, within 1%
                if strike < spot_price and (spot_price - strike) <= one_percent:
                    call_opt = item.get("call_options")
                    if call_opt:
                        sig = self._evaluate_contract(call_opt, "CE", strike, spot_price, atm_strike, strike_map, now_time_str, now_ms, idx_cfg, instrument_key)
                        if sig:
                            return sig

                # 2. PE In-The-Money: strike > spot, within 1%
                if strike > spot_price and (strike - spot_price) <= one_percent:
                    put_opt = item.get("put_options")
                    if put_opt:
                        sig = self._evaluate_contract(put_opt, "PE", strike, spot_price, atm_strike, strike_map, now_time_str, now_ms, idx_cfg, instrument_key)
                        if sig:
                            return sig

            return None

    def _warmup_strikes(self, strikes: list[dict[str, Any]]) -> None:
        """Seed contract history baseline before market open so 09:15:00.xxx ticks can trigger immediately."""
        for item in strikes:
            for opt_key in ("call_options", "put_options"):
                opt = item.get(opt_key) or {}
                key = str(opt.get("instrument_key") or "")
                mkt = opt.get("market_data") or {}
                ltp = float(mkt.get("ltp") or 0.0)
                vol = float(mkt.get("volume") or 0.0)
                prev_close = float(mkt.get("close_price") or 0.0)
                oi = float(mkt.get("oi") or mkt.get("open_interest") or 0.0)
                if key and ltp > 0:
                    self.contract_history[key] = {
                        "volume": vol,
                        "ltp": ltp,
                        "day_high": ltp,
                        "prev_close": prev_close,
                        "oi": oi,
                        "last_signaled_vol": vol,
                    }

    def _evaluate_contract(
        self,
        opt: dict[str, Any],
        side: str,
        strike: float,
        spot_price: float,
        atm_strike: float,
        strike_map: dict[float, dict[str, Any]],
        now_time_str: str,
        now_ms: int,
        idx_cfg: dict[str, Any] | None = None,
        underlying_key: str = "NSE_INDEX|Nifty 50",
    ) -> dict[str, Any] | None:
        key = str(opt.get("instrument_key") or "")
        mkt = opt.get("market_data") or {}
        ltp = float(mkt.get("ltp") or 0.0)
        vol = float(mkt.get("volume") or 0.0)
        prev_close = float(mkt.get("close_price") or 0.0)
        # #7 — extract bid, ask, OI for signal logging
        bid_price = float(mkt.get("bid_price") or 0.0)
        ask_price_raw = float(mkt.get("ask_price") or 0.0)
        oi = float(mkt.get("oi") or mkt.get("open_interest") or 0.0)

        if ltp <= 0 or not key:
            return None

        # Retrieve history
        history = self.contract_history.get(key)
        if history is None:
            # #1 Pre-warm: First observation — record baseline, never emit signal
            self.contract_history[key] = {
                "volume": vol,
                "ltp": ltp,
                "day_high": ltp,
                "prev_close": prev_close,
                "oi": oi,
                "last_signaled_vol": vol,  # #4 dedup seed
            }
            return None

        day_high = max(history["day_high"], ltp)
        history["day_high"] = day_high

        prev_vol = history["volume"]
        prev_ltp = history["ltp"]
        prev_oi = history.get("oi", 0.0)

        # #3 — qty delta: only count new trades this tick, zero on first delta
        if prev_vol == 0:
            qty_delta = 0.0
        else:
            qty_delta = max(0.0, vol - prev_vol) if vol >= prev_vol else 0.0
        # Sanity cap: a single tick cannot have >50 000 contracts
        qty_delta = min(qty_delta, 50_000.0)

        oi_change = oi - prev_oi
        val_lakhs = (ltp * qty_delta) / 100_000.0

        # Spread %
        if bid_price > 0 and ask_price_raw > 0:
            spread_pct = round(((ask_price_raw - bid_price) / ((bid_price + ask_price_raw) / 2.0)) * 100.0, 3)
        else:
            spread_pct = 0.0

        # Confidence label based on how many conditions beyond cond1 pass
        # (will be finalised after condition checks below)

        # Update latest readings
        history["volume"] = vol
        history["ltp"] = ltp
        history["oi"] = oi

        # Condition 1: Qty Delta OR Value in Lakhs
        cond_qty = (self.use_qty_condition and qty_delta >= self.qty_threshold)
        cond_val = (self.use_value_condition and val_lakhs >= self.value_threshold_lakhs)
        cond1 = (cond_qty or cond_val)

        # Condition 2: LTP equals Day High
        cond2 = (not self.use_day_high_condition) or (ltp >= day_high and ltp > 0)

        # Condition 3: LTP > Previous Close
        cond3 = (not self.use_prev_close_condition) or (prev_close > 0 and ltp > prev_close)

        # Condition 4: LTP > Previous LTP
        cond4 = (not self.use_uptick_condition) or (prev_ltp > 0 and ltp > prev_ltp)

        # Check if ALL 4 conditions met
        if not (cond1 and cond2 and cond3 and cond4):
            return None

        # #4 — Deduplication: only emit signal if new volume traded since last signal
        last_signaled_vol = history.get("last_signaled_vol", 0.0)
        if vol <= last_signaled_vol:
            return None  # Same tick repeated — suppress
        history["last_signaled_vol"] = vol

        # Confidence label
        conditions_met = sum([bool(cond1), bool(cond2), bool(cond3), bool(cond4)])
        confidence = "HIGH" if conditions_met == 4 else "MEDIUM" if conditions_met == 3 else "LOW"

        inst_name = (idx_cfg.get("name") if idx_cfg else None) or ("SENSEX" if strike >= 70000 else "BANKNIFTY" if strike >= 45000 else "NIFTY")

        # SIGNAL DETECTED!
        signal_data = {
            "timestamp_ist": now_time_str,   # #9 — already HH:MM:SS.mmm
            "timestamp_ms": now_ms,
            "instrument_name": inst_name,
            "side": side,
            "strike": strike,
            "contract_key": key,
            "ltp": ltp,
            "prev_ltp": prev_ltp,
            "qty_delta": qty_delta,
            "val_lakhs": round(val_lakhs, 2),
            "day_high": day_high,
            "prev_close": prev_close,
            # #7 — new signal fields
            "bid_price": round(bid_price, 2),
            "ask_price": round(ask_price_raw, 2),
            "spread_pct": spread_pct,
            "oi_change": round(oi_change, 0),
            "confidence": confidence,
            "action_taken": "",
        }

        # Step 2: Decide side & actions for this specific underlying instrument
        curr_pos = self.current_positions.get(underlying_key)

        if curr_pos is None:
            # First signal for this instrument -> Enter trade!
            signal_data["action_taken"] = f"ENTERED_{side}"
            self.database.insert_strategy_signal(signal_data)
            self._enter_position(side, atm_strike, strike_map, now_time_str, now_ms, spot_price, idx_cfg, underlying_key)
            return signal_data

        current_side = curr_pos["side"]  # 'BUY_CE' or 'BUY_PE'
        if (current_side == "BUY_CE" and side == "CE") or (current_side == "BUY_PE" and side == "PE"):
            # Same side signal while already in position for this instrument -> ignore!
            signal_data["action_taken"] = "IGNORED_SAME_SIDE"
            self.database.insert_strategy_signal(signal_data)
            return signal_data

        # Opposite side signal for this instrument -> Step 4: Exit current and reverse!
        signal_data["action_taken"] = f"REVERSED_TO_{side}"
        self.database.insert_strategy_signal(signal_data)

        # 1. Exit current instrument's position
        curr_key = curr_pos["instrument_key"]
        exit_price = self._find_current_price(curr_key, list(strike_map.values()), is_exit=True)
        self._exit_position(curr_pos, exit_price, f"REVERSAL_{side}", now_ms, now_time_str, underlying_key)

        # 2. Enter opposite side for this instrument
        self._enter_position(side, atm_strike, strike_map, now_time_str, now_ms, spot_price, idx_cfg, underlying_key)
        return signal_data

    def _enter_position(
        self,
        side: str,
        atm_strike: float,
        strike_map: dict[float, dict[str, Any]],
        time_ist: str,
        timestamp_ms: int,
        spot_price: float = 0.0,
        idx_cfg: dict[str, Any] | None = None,
        underlying_key: str = "NSE_INDEX|Nifty 50",
    ) -> None:
        if idx_cfg is None:
            idx_cfg = INDEX_CONFIG["NSE_INDEX|Nifty 50"]

        step = idx_cfg["step"]
        itm_steps = idx_cfg["itm_steps"]
        lot_size = idx_cfg["lot"]
        index_name = idx_cfg["name"]

        # Step 3: Pick ITM+itm_steps strike (ATM minus steps for CE, ATM plus steps for PE)
        if side == "CE":
            target_strike = atm_strike - step * itm_steps
        else:
            target_strike = atm_strike + step * itm_steps

        item = strike_map.get(target_strike)
        if not item and strike_map:
            target_strike = min(strike_map.keys(), key=lambda s: abs(s - target_strike))
            item = strike_map.get(target_strike)

        if not item:
            LOGGER.warning("Target strike %s not found in strike map.", target_strike)
            return

        opt = item.get("call_options") if side == "CE" else item.get("put_options")
        if not opt:
            LOGGER.warning("Option contract for %s %s not found.", target_strike, side)
            return

        key = opt.get("instrument_key", "")
        mkt = opt.get("market_data") or {}
        # #10 — Buy at Ask; fallback: ltp * 1.001 (never flat LTP)
        ask_price = float(mkt.get("ask_price") or 0.0)
        ltp = float(mkt.get("ltp") or 0.0)
        if ask_price > 0:
            entry_price = ask_price
        elif ltp > 0:
            entry_price = round(ltp * 1.001, 2)
        else:
            entry_price = 0.0

        if entry_price <= 0:
            LOGGER.warning("Invalid entry price for %s: %s", key, entry_price)
            return

        trade_id = f"TRD-{now_timestamp_id()}-{self._trade_counter}"
        self._trade_counter += 1
        symbol = f"{index_name} {int(target_strike)} {side}"
        trade_date = datetime.now(IST).strftime("%Y-%m-%d")

        trade = {
            "trade_id": trade_id,
            "trade_date": trade_date,
            "side": f"BUY_{side}",
            "symbol": symbol,
            "instrument_key": key,
            "underlying_key": underlying_key,
            "strike": target_strike,
            "lot_size": lot_size,
            "entry_time_ist": time_ist,       # #9 — already HH:MM:SS.mmm
            "entry_price": round(entry_price, 2),
            "entry_timestamp_ms": timestamp_ms,
            "exit_time_ist": None,
            "exit_price": None,
            "exit_timestamp_ms": None,
            "exit_reason": None,
            "pnl_points": None,
            "net_pnl": None,
            "status": "OPEN",
            # #8 — log spot and ATM at entry
            "spot_price": round(spot_price, 2),
            "atm_strike": round(atm_strike, 2),
        }

        self.current_positions[underlying_key] = trade
        self.database.insert_strategy_trade(trade)
        self._append_csv(trade)
        LOGGER.info(
            "ENTERED PAPER TRADE: %s %s at %s | Spot=%.2f ATM=%.2f",
            trade_id, symbol, entry_price, spot_price, atm_strike,
        )

    def _exit_position(
        self,
        pos: dict[str, Any] | None,
        exit_price: float,
        reason: str,
        timestamp_ms: int,
        time_ist: str,
        underlying_key: str | None = None,
    ) -> None:
        if not pos:
            pos = self.current_position
        if not pos:
            return

        entry_price = float(pos["entry_price"])
        lot_size = int(pos.get("lot_size", self.lot_size))
        pnl_points = round(exit_price - entry_price, 2)
        net_pnl = round(pnl_points * lot_size, 2)

        pos["exit_time_ist"] = time_ist
        pos["exit_price"] = round(exit_price, 2)
        pos["exit_timestamp_ms"] = timestamp_ms
        pos["exit_reason"] = reason
        pos["pnl_points"] = pnl_points
        pos["net_pnl"] = net_pnl
        pos["status"] = "CLOSED"

        self.database.update_strategy_trade(pos)
        self._append_csv(pos)
        LOGGER.info("CLOSED PAPER TRADE: %s | PnL: %s pts (Rs %s)", pos["trade_id"], pnl_points, net_pnl)

        # Clear active position slot for this instrument
        u_key = underlying_key or get_underlying_key_for_trade(pos)
        if u_key in self.current_positions:
            self.current_positions[u_key] = None
        else:
            for k, p in list(self.current_positions.items()):
                if p and p.get("trade_id") == pos.get("trade_id"):
                    self.current_positions[k] = None

    def _find_current_price(self, key: str, strikes: list[dict[str, Any]], is_exit: bool = False) -> float:
        for item in strikes:
            for opt_key in ("call_options", "put_options"):
                opt = item.get(opt_key) or {}
                if opt.get("instrument_key") == key:
                    mkt = opt.get("market_data") or {}
                    ltp = float(mkt.get("ltp") or 0.0)
                    if is_exit:
                        # #10 — Exit at best bid; fallback: ltp * 0.999 (never flat LTP)
                        bid = float(mkt.get("bid_price") or 0.0)
                        if bid > 0:
                            return bid
                        if ltp > 0:
                            return round(ltp * 0.999, 2)
                    else:
                        # #10 — Entry at best ask; fallback: ltp * 1.001
                        ask = float(mkt.get("ask_price") or 0.0)
                        if ask > 0:
                            return ask
                        if ltp > 0:
                            return round(ltp * 1.001, 2)
        return 0.0

    def _append_csv(self, trade: dict[str, Any]) -> None:
        try:
            trade_date = trade.get("trade_date")
            if not trade_date and trade.get("entry_timestamp_ms"):
                trade_date = datetime.fromtimestamp(trade["entry_timestamp_ms"] / 1000, tz=IST).strftime("%Y-%m-%d")

            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade["trade_id"],
                    trade_date or "",
                    trade["side"],
                    trade["symbol"],
                    trade["strike"],
                    trade["lot_size"],
                    trade["entry_time_ist"],
                    trade["entry_price"],
                    trade.get("exit_time_ist") or "",
                    trade.get("exit_price") or "",
                    trade.get("exit_reason") or "",
                    trade.get("pnl_points") or "",
                    trade.get("net_pnl") or "",
                    trade["status"],
                    trade.get("spot_price") or "",   # #8 — Spot at entry
                    trade.get("atm_strike") or "",   # #8 — ATM at entry
                ])
        except Exception as exc:
            LOGGER.error("Failed to write trade to CSV: %s", exc)


def now_timestamp_id() -> str:
    # #9 — include milliseconds in trade ID timestamp
    return datetime.now(IST).strftime("%m%d%H%M%S%f")[:-3]
