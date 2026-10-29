from __future__ import annotations

import csv
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from database import MarketDatabase

LOGGER = logging.getLogger("strategy_engine")
IST = ZoneInfo("Asia/Kolkata")


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
        # key -> {"volume": float, "ltp": float, "day_high": float, "prev_close": float}
        self.contract_history: dict[str, dict[str, float]] = {}

        # Position tracking
        self.current_position: dict[str, Any] | None = None
        self._trade_counter: int = 1
        self._init_csv()
        self._resume_open_position()

    def _init_csv(self) -> None:
        try:
            if not self.csv_path.exists():
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Trade ID", "Side", "Symbol", "Strike", "Lot Size",
                        "Entry Time (IST)", "Entry Price",
                        "Exit Time (IST)", "Exit Price", "Exit Reason",
                        "P&L Points", "Net P&L (INR)", "Status"
                    ])
        except Exception as exc:
            LOGGER.error("Failed to init paper_trades.csv: %s", exc)

    def _resume_open_position(self) -> None:
        try:
            trades = self.database.get_strategy_trades(limit=10)
            if trades:
                self._trade_counter = len(trades) + 1
            for t in trades:
                if t.get("status") == "OPEN":
                    self.current_position = t
                    LOGGER.info("Resumed open paper trade: %s", t["trade_id"])
                    break
        except Exception as exc:
            LOGGER.error("Failed to resume open position: %s", exc)

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
            closed_trades = [t for t in trades if t.get("status") == "CLOSED"]
            total_pnl = sum(float(t.get("net_pnl") or 0.0) for t in closed_trades)
            win_trades = sum(1 for t in closed_trades if float(t.get("net_pnl") or 0.0) > 0)
            loss_trades = sum(1 for t in closed_trades if float(t.get("net_pnl") or 0.0) < 0)

            return {
                "enabled": self.enabled,
                "current_position": self.current_position,
                "total_trades": len(trades),
                "closed_trades": len(closed_trades),
                "win_trades": win_trades,
                "loss_trades": loss_trades,
                "net_pnl_inr": round(total_pnl, 2),
                "config": self.get_config(),
                "csv_path": str(self.csv_path),
            }

    def process_option_chain_snapshot(
        self,
        spot_price: float | None,
        strikes: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not self.enabled or not spot_price or not strikes:
            return None

        with self._lock:
            now_dt = datetime.now(IST)
            now_time_str = now_dt.strftime("%H:%M:%S")
            now_ms = int(time.time() * 1000)

            # EOD Square-off at 15:15
            if now_time_str >= self.end_time:
                if self.current_position is not None:
                    LOGGER.info("15:15 EOD Square-off triggered.")
                    self._exit_position(
                        exit_price=self._find_current_price(self.current_position["instrument_key"], strikes, is_exit=True),
                        reason="EOD_1515",
                        timestamp_ms=now_ms,
                        time_ist=now_time_str
                    )
                return None

            # Skip before 09:15
            if now_time_str < self.start_time:
                return None

            atm_strike = round(spot_price / 50.0) * 50.0
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
                        sig = self._evaluate_contract(call_opt, "CE", strike, spot_price, atm_strike, strike_map, now_time_str, now_ms)
                        if sig:
                            return sig

                # 2. PE In-The-Money: strike > spot, within 1%
                if strike > spot_price and (strike - spot_price) <= one_percent:
                    put_opt = item.get("put_options")
                    if put_opt:
                        sig = self._evaluate_contract(put_opt, "PE", strike, spot_price, atm_strike, strike_map, now_time_str, now_ms)
                        if sig:
                            return sig

            return None

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
    ) -> dict[str, Any] | None:
        key = str(opt.get("instrument_key") or "")
        mkt = opt.get("market_data") or {}
        ltp = float(mkt.get("ltp") or 0.0)
        vol = float(mkt.get("volume") or 0.0)
        prev_close = float(mkt.get("close_price") or 0.0)

        if ltp <= 0 or not key:
            return None

        # Retrieve history
        history = self.contract_history.get(key)
        if history is None:
            # First observation of this contract: record baseline
            self.contract_history[key] = {
                "volume": vol,
                "ltp": ltp,
                "day_high": ltp,
                "prev_close": prev_close,
            }
            return None

        day_high = max(history["day_high"], ltp)
        history["day_high"] = day_high

        prev_vol = history["volume"]
        prev_ltp = history["ltp"]
        qty_delta = max(0.0, vol - prev_vol) if vol >= prev_vol else 0.0
        val_lakhs = (ltp * qty_delta) / 100000.0

        # Update latest readings
        history["volume"] = vol
        history["ltp"] = ltp

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

        # SIGNAL DETECTED!
        signal_data = {
            "timestamp_ist": now_time_str,
            "timestamp_ms": now_ms,
            "side": side,
            "strike": strike,
            "contract_key": key,
            "ltp": ltp,
            "prev_ltp": prev_ltp,
            "qty_delta": qty_delta,
            "val_lakhs": round(val_lakhs, 2),
            "day_high": day_high,
            "prev_close": prev_close,
            "action_taken": "",
        }

        # Step 2: Decide side & actions
        if self.current_position is None:
            # First signal -> Enter trade!
            signal_data["action_taken"] = f"ENTERED_{side}"
            self.database.insert_strategy_signal(signal_data)
            self._enter_position(side, atm_strike, strike_map, now_time_str, now_ms)
            return signal_data

        current_side = self.current_position["side"]  # 'BUY_CE' or 'BUY_PE'
        if (current_side == "BUY_CE" and side == "CE") or (current_side == "BUY_PE" and side == "PE"):
            # Same side signal while already in position -> ignore!
            signal_data["action_taken"] = "IGNORED_SAME_SIDE"
            self.database.insert_strategy_signal(signal_data)
            return signal_data

        # Opposite side signal -> Step 4: Exit current and reverse!
        signal_data["action_taken"] = f"REVERSED_TO_{side}"
        self.database.insert_strategy_signal(signal_data)

        # 1. Exit current
        curr_key = self.current_position["instrument_key"]
        exit_price = self._find_current_price(curr_key, list(strike_map.values()), is_exit=True)
        self._exit_position(exit_price, f"REVERSAL_{side}", now_ms, now_time_str)

        # 2. Enter opposite side
        self._enter_position(side, atm_strike, strike_map, now_time_str, now_ms)
        return signal_data

    def _enter_position(
        self,
        side: str,
        atm_strike: float,
        strike_map: dict[float, dict[str, Any]],
        time_ist: str,
        timestamp_ms: int,
    ) -> None:
        # Step 3: Pick ITM+2 strike (ATM minus 2 steps for CE, ATM plus 2 steps for PE)
        # Step size = 50 points
        if side == "CE":
            target_strike = atm_strike - 100.0
        else:
            target_strike = atm_strike + 100.0

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
        # Buy limit at best ask (or LTP)
        ask_price = float(mkt.get("ask_price") or 0.0)
        ltp = float(mkt.get("ltp") or 0.0)
        entry_price = ask_price if ask_price > 0 else ltp

        if entry_price <= 0:
            LOGGER.warning("Invalid entry price for %s: %s", key, entry_price)
            return

        trade_id = f"TRD-{now_timestamp_id()}-{self._trade_counter}"
        self._trade_counter += 1
        symbol = f"NIFTY {int(target_strike)} {side}"

        trade = {
            "trade_id": trade_id,
            "side": f"BUY_{side}",
            "symbol": symbol,
            "instrument_key": key,
            "strike": target_strike,
            "lot_size": self.lot_size,
            "entry_time_ist": time_ist,
            "entry_price": round(entry_price, 2),
            "entry_timestamp_ms": timestamp_ms,
            "exit_time_ist": None,
            "exit_price": None,
            "exit_timestamp_ms": None,
            "exit_reason": None,
            "pnl_points": None,
            "net_pnl": None,
            "status": "OPEN",
        }

        self.current_position = trade
        self.database.insert_strategy_trade(trade)
        self._append_csv(trade)
        LOGGER.info("ENTERED PAPER TRADE: %s %s at %s", trade_id, symbol, entry_price)

    def _exit_position(
        self,
        exit_price: float,
        reason: str,
        timestamp_ms: int,
        time_ist: str,
    ) -> None:
        if not self.current_position:
            return

        pos = self.current_position
        entry_price = float(pos["entry_price"])
        pnl_points = round(exit_price - entry_price, 2)
        net_pnl = round(pnl_points * self.lot_size, 2)

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
        self.current_position = None

    def _find_current_price(self, key: str, strikes: list[dict[str, Any]], is_exit: bool = False) -> float:
        for item in strikes:
            for opt_key in ("call_options", "put_options"):
                opt = item.get(opt_key) or {}
                if opt.get("instrument_key") == key:
                    mkt = opt.get("market_data") or {}
                    if is_exit:
                        # Exit at best bid (or LTP)
                        bid = float(mkt.get("bid_price") or 0.0)
                        if bid > 0:
                            return bid
                    else:
                        ask = float(mkt.get("ask_price") or 0.0)
                        if ask > 0:
                            return ask
                    return float(mkt.get("ltp") or 0.0)
        return 0.0

    def _append_csv(self, trade: dict[str, Any]) -> None:
        try:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade["trade_id"],
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
                ])
        except Exception as exc:
            LOGGER.error("Failed to write trade to CSV: %s", exc)


def now_timestamp_id() -> str:
    return datetime.now(IST).strftime("%m%d%H%M%S")
