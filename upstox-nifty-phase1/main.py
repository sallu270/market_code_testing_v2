from __future__ import annotations

import atexit
import csv
import io
import json
import logging
import queue
import signal
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
import urllib.error
import urllib.parse
import urllib.request

import os
from pathlib import Path

from candle_builder import OneMinuteCandleBuilder
from config import IST, load_settings
from database import MarketDatabase
from flow_engine import FLOW_ENGINE
from stock_screener import NIFTY_50_STOCKS, StockScreener
from strategy_engine import GapEntryStrategyEngine, PressureStrategyEngine
from upstox_feed import DualUpstoxFeed, UpstoxFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("phase1")

settings = load_settings()
database = MarketDatabase(settings.db_path)
strategy_engine = GapEntryStrategyEngine(
    database=database,
    base_dir=settings.db_path.parent,
    access_token=settings.access_token,
)
stock_screener = StockScreener(
    base_dir=settings.db_path.parent,
    access_token=settings.access_token,
)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True

@app.after_request
def add_cache_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

subscribers: set[queue.Queue[dict[str, Any]]] = set()
subscribers_lock = threading.RLock()
recent_ticks: deque[dict[str, Any]] = deque(maxlen=settings.ui_tick_limit)
latest_status: dict[str, Any] = {
    "state": "starting",
    "message": "Application is starting.",
    "connected": False,
    "timestamp_ms": int(time.time() * 1000),
}

def publish(event_type: str, payload: dict[str, Any]) -> None:
    event = {
        "type": event_type,
        "payload": payload,
        "server_timestamp_ms": int(time.time() * 1000),
    }

    dead_queues: list[queue.Queue[dict[str, Any]]] = []

    with subscribers_lock:
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                dead_queues.append(subscriber)

        for subscriber in dead_queues:
            subscribers.discard(subscriber)

def handle_status(status: dict[str, Any]) -> None:
    global latest_status
    latest_status = status
    LOGGER.info("%s: %s", status["state"], status["message"])
    publish("status", status)
    if status.get("state") in ("disconnected", "error") and not status.get("connected") and hasattr(strategy_engine, "add_alert"):
        alert = strategy_engine.add_alert("FEED_LOST", f"Feed disconnected: {status.get('message', 'WebSocket offline')}", level="ERROR")
        publish("alert", alert)

def handle_candle(candle: dict[str, Any]) -> None:
    publish("candle", candle)

candle_builder = OneMinuteCandleBuilder(
    database=database,
    on_candle=handle_candle,
)

def handle_tick(tick: dict[str, Any]) -> None:
    try:
        database.insert_tick(tick)
        recent_ticks.appendleft(tick)
        publish("tick", tick)
        candle_builder.process_tick(tick)

        # Feed tick to PCR Flow Engine (v0.0.5 Section 6A)
        inst_k = str(tick.get("instrument_key") or "")
        FLOW_ENGINE.on_tick(
            inst_k,
            ltp=float(tick.get("ltp") or 0.0),
            oi=float(tick["open_interest"]) if tick.get("open_interest") is not None else None,
            vol=float(tick["volume"]) if tick.get("volume") is not None else None,
            bid=float(tick.get("bid_price") or 0.0),
            ask=float(tick.get("ask_price") or 0.0),
        )

        trade = strategy_engine.on_tick(tick)
        if trade:
            publish("strategy_trade", trade)
    except Exception:
        LOGGER.exception("Failed to store or aggregate a market tick.")

# SINGLE WebSocket: Upstox standard accounts only allow 1 concurrent WebSocket connection per token.
# We combine all index + stock keys into one UpstoxFeed, wrapped in DualUpstoxFeed for routing compatibility.
# DualUpstoxFeed is kept intact but forced into single-feed mode by passing empty stock_keys and
# a combined index_keys list that includes all 50 equity keys + index/futures keys.

initial_index_keys = list(settings.instrument_keys)
initial_stock_keys = [s["key"] for s in NIFTY_50_STOCKS if s.get("key")]

# Combine all keys into one list (indices first, then stocks) for single WebSocket
all_initial_keys = initial_index_keys + [k for k in initial_stock_keys if k not in initial_index_keys]

# Use a single UpstoxFeed instead of DualUpstoxFeed to avoid 403 from second connection
feed = UpstoxFeed(
    settings=settings,
    on_tick=handle_tick,
    on_status=handle_status,
    name="main-feed",
    initial_keys=all_initial_keys,
)
strategy_engine.set_feed(feed)

def candle_housekeeping() -> None:
    while True:
        try:
            candle_builder.close_stale_candles()
        except Exception:
            LOGGER.exception("Candle housekeeping failed.")
        time.sleep(1)

housekeeping_thread = threading.Thread(
    target=candle_housekeeping,
    name="candle-housekeeping",
    daemon=True,
)
housekeeping_thread.start()

def fetch_option_chain_data(
    instrument_key: str, expiry_date: str = ""
) -> tuple[int, Any, float | None, list[dict[str, Any]], str]:
    if not settings.access_token:
        return 400, "UPSTOX_ACCESS_TOKEN is missing", None, [], ""

    encoded_key = urllib.parse.quote(instrument_key, safe="|")
    if not expiry_date:
        try:
            exp_url = f"https://api.upstox.com/v2/option/contract?instrument_key={encoded_key}"
            status, exp_payload = _fetch_upstox_json(exp_url, settings.access_token)
            if status == 200 and isinstance(exp_payload, dict):
                contracts = exp_payload.get("data") or []
                expiries = sorted(list({c["expiry"] for c in contracts if c.get("expiry")}))
                if expiries:
                    expiry_date = expiries[0]
        except Exception:
            pass

    if not expiry_date:
        return 400, "expiry_date is required", None, [], ""

    try:
        url = f"https://api.upstox.com/v2/option/chain?instrument_key={encoded_key}&expiry_date={expiry_date}"
        status, payload = _fetch_upstox_json(url, settings.access_token)
        if status != 200 or not isinstance(payload, dict):
            return status, payload, None, [], expiry_date

        data = payload.get("data") or []
        spot_price = None
        for item in data:
            if item.get("underlying_spot_price"):
                spot_price = item.get("underlying_spot_price")
                break

        return status, payload, spot_price, data, expiry_date
    except Exception as exc:
        return 500, str(exc), None, [], expiry_date

def strategy_schedule_worker() -> None:
    """
    Clock driver and coordinator for Opening Gap Entry Strategy (NIFTY 50).
    Drives schedule checkpoints (09:14:55 lock, 09:15:00 arm, 15:15:00 square-off).
    Preloads option chain once on startup.
    Broadcasts live strategy status to SSE clients.
    """
    LOGGER.info("Opening Gap Entry Strategy schedule worker started.")
    time.sleep(2)
    # Preload contracts once at startup for all 3 supported indices
    if settings.configured and settings.access_token:
        for ukey in ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX"]:
            try:
                strategy_engine.load_contracts_if_needed(ukey)
            except Exception as exc:
                LOGGER.warning("Startup contract load failed for %s: %s", ukey, exc)
        LOGGER.info("Gap Entry Strategy contracts preloaded across indices.")

    last_broadcast_ms = 0
    _last_flow_bar_min = -1
    while True:
        try:
            now_dt = datetime.now(IST)
            now_time = now_dt.strftime("%H:%M:%S")

            # Sync basket contracts to FLOW_ENGINE (v0.0.5 Section 6A)
            for ukey, idx_st in strategy_engine.index_underlyings.items():
                if ukey in FLOW_ENGINE.trackers:
                    trk = FLOW_ENGINE.trackers[ukey]
                    if not trk.basket_contracts and idx_st.get("locked_keys"):
                        b_contracts = [
                            strategy_engine.hot_cache[k] for k in idx_st["locked_keys"]
                            if k in strategy_engine.hot_cache
                        ]
                        if b_contracts:
                            spot_p = idx_st.get("spot_price") or 0.0
                            trk.set_basket(b_contracts, atm_strike=spot_p)
                            if not trk.baseline_taken:
                                trk.take_0915_baseline()
                            if feed and hasattr(feed, "subscribe"):
                                b_keys = [c["instrument_key"] for c in b_contracts if c.get("instrument_key")]
                                if b_keys:
                                    feed.subscribe(b_keys)

            # Trigger 1-minute bar evaluation for Flow Engine
            cur_minute = now_dt.minute
            if cur_minute != _last_flow_bar_min:
                _last_flow_bar_min = cur_minute
                cur_bar_ms = int(time.time() * 1000)
                for ukey, trk in FLOW_ENGINE.trackers.items():
                    if trk.is_index and ukey in strategy_engine.index_underlyings:
                        st_item = strategy_engine.index_underlyings[ukey]
                        s_p = float(st_item.get("spot_price") or 0.0)
                        if s_p > 0:
                            trk.on_minute_bar_closed(cur_bar_ms, s_p, s_p)

            if strategy_engine.enabled:
                # Dynamic planning for indices
                if hasattr(strategy_engine, "plan_all_indices"):
                    strategy_engine.plan_all_indices()

                # Lock at 09:14:55
                if "09:14:55" <= now_time < "09:15:00":
                    if hasattr(strategy_engine, "lock_all_indices"):
                        strategy_engine.lock_all_indices()
                    elif strategy_engine.state in ("SETUP", "PLANNING"):
                        strategy_engine.lock_contracts()
                    if hasattr(strategy_engine, "lock_all_stocks"):
                        strategy_engine.lock_all_stocks()

                # Snapshot at 09:15:00: Market Open -> Arm indices for First Positive Tick
                elif "09:15:00" <= now_time < "09:18:00":
                    if hasattr(strategy_engine, "snapshot_all_indices"):
                        strategy_engine.snapshot_all_indices()
                    elif not getattr(strategy_engine, "snapshot_taken", False):
                        strategy_engine.take_snapshot()
                    if hasattr(strategy_engine, "arm_all_indices"):
                        strategy_engine.arm_all_indices()
                    elif strategy_engine.state in ("LOCKED", "DATA_COLLECTION", "PLANNING", "SETUP"):
                        strategy_engine.state = "ARMED"
                    if hasattr(strategy_engine, "snapshot_all_stocks"):
                        strategy_engine.snapshot_all_stocks()

                # From 09:18:00: Evaluation window active
                elif now_time >= "09:18:00":
                    if hasattr(strategy_engine, "snapshot_all_indices"):
                        strategy_engine.snapshot_all_indices()
                    elif not getattr(strategy_engine, "snapshot_taken", False):
                        strategy_engine.take_snapshot()
                    if hasattr(strategy_engine, "arm_all_indices"):
                        strategy_engine.arm_all_indices()
                    elif strategy_engine.state in ("LOCKED", "DATA_COLLECTION"):
                        strategy_engine.state = "ARMED"
                    if hasattr(strategy_engine, "snapshot_all_stocks"):
                        strategy_engine.snapshot_all_stocks()
                    if hasattr(strategy_engine, "arm_all_stocks"):
                        strategy_engine.arm_all_stocks()

                    # Periodic evaluation across active underlyings (indices & stocks)
                    if now_time < "14:45:00" and hasattr(strategy_engine, "evaluate_all_active_underlyings"):
                        strategy_engine.evaluate_all_active_underlyings()

                # Phase 3 Alerts: 14:45 Cutoff alert
                if "14:45:00" <= now_time < "14:46:00" and "CUTOFF_1445" not in getattr(strategy_engine, "_alert_flags", set()):
                    strategy_engine._alert_flags.add("CUTOFF_1445")
                    alert = strategy_engine.add_alert("NO_ENTRY_AFTER_1445", "14:45 Cutoff: No new trade entries allowed for today.", level="WARNING")
                    publish("alert", alert)

                # Phase 3 Alerts: 15:10 Warning alert (5 minutes to square-off)
                if "15:10:00" <= now_time < "15:11:00" and "WARNING_1510" not in getattr(strategy_engine, "_alert_flags", set()):
                    strategy_engine._alert_flags.add("WARNING_1510")
                    alert = strategy_engine.add_alert("WARNING_1510", "15:10 Warning: 5 minutes until mandatory 15:15:00 square-off.", level="WARNING")
                    publish("alert", alert)

                # Phase 3 Alerts: Stale data check (> 60 seconds without ticks during market hours)
                if "09:15:00" <= now_time <= "15:30:00" and hasattr(strategy_engine, "last_tick_time"):
                    if time.time() - strategy_engine.last_tick_time > 60:
                        if "STALE_DATA" not in getattr(strategy_engine, "_alert_flags", set()):
                            strategy_engine._alert_flags.add("STALE_DATA")
                            alert = strategy_engine.add_alert("STALE_DATA", "Stale data: No market ticks received for >60s.", level="WARNING")
                            publish("alert", alert)
                    else:
                        if "STALE_DATA" in getattr(strategy_engine, "_alert_flags", set()):
                            strategy_engine._alert_flags.discard("STALE_DATA")

                # Reset daily flags before 09:14:00
                if now_time < "09:14:00":
                    strategy_engine._watchlist_built_today = False

                # EOD square-off at 15:15:00 by clock (strictly at live bid)
                if now_time >= "15:15:00":
                    if hasattr(strategy_engine, "squareoff_all_indices_1515"):
                        strategy_engine.squareoff_all_indices_1515()
                    elif strategy_engine.state == "HOLDING":
                        strategy_engine.manual_exit(reason="SQUAREOFF_1515")
                    if hasattr(strategy_engine, "squareoff_all_stocks_1515"):
                        strategy_engine.squareoff_all_stocks_1515()

                    # 3:15 PM EOD Watchlist Build (Dr. Vijay Bhilwade Specification)
                    # Evaluates all 50 constituent stocks and records next day's top 10 stocks
                    if "15:15:00" <= now_time <= "15:30:00" and not getattr(strategy_engine, "_watchlist_built_today", False):
                        try:
                            LOGGER.info("15:15:00 EOD: Scanning all 50 constituent stocks to record tomorrow's Top Watchlist...")
                            wl = stock_screener.build_tomorrow_watchlist()
                            strategy_engine._watchlist_built_today = True
                            if hasattr(strategy_engine, "load_stock_configs"):
                                strategy_engine.load_stock_configs()
                            LOGGER.info(
                                "15:15:00 EOD Tomorrow Watchlist recorded: %d Bullish, %d Bearish",
                                len(wl.get("top_bullish", [])), len(wl.get("top_bearish", []))
                            )
                        except Exception as e:
                            LOGGER.warning("Could not auto-generate watchlist: %s", e)

            # Periodic SSE status broadcast (every 1.5 seconds)
            cur_ms = int(time.time() * 1000)
            if cur_ms - last_broadcast_ms >= 1500:
                publish("strategy_update", strategy_engine.get_status())
                last_broadcast_ms = cur_ms

        except Exception:
            LOGGER.exception("Error in strategy schedule worker loop")
        time.sleep(0.5)

strategy_worker_thread = threading.Thread(
    target=strategy_schedule_worker,
    name="strategy-schedule-worker",
    daemon=True,
)
strategy_worker_thread.start()

if settings.configured:
    feed.start()
else:
    handle_status(
        {
            "state": "configuration_required",
            "message": (
                "Add UPSTOX_ACCESS_TOKEN and UPSTOX_INSTRUMENT_KEYS to .env, "
                "then restart the application."
            ),
            "connected": False,
            "timestamp_ms": int(time.time() * 1000),
        }
    )

@app.get("/")
def index() -> Any:
    """Renders the high-performance OI Conditions Dashboard."""
    return render_template("index.html")

@app.get("/api/dashboard-data")
def api_dashboard_data() -> Response:
    """Aggregates all live data required for the OI Conditions Dashboard."""
    st = strategy_engine.get_status()

    # 1. Indices summary
    indices_out = []
    for ukey in ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX"]:
        idx_st = strategy_engine.index_underlyings.get(ukey, {})
        if not idx_st:
            continue
        spot = float(idx_st.get("spot_price") or idx_st.get("open_price") or 0.0)
        prev_c = float(idx_st.get("prev_close") or spot or 1.0)
        net_chg_pct = round(((spot - prev_c) / prev_c) * 100.0, 2) if prev_c > 0 else 0.0

        last_eval = idx_st.get("last_evaluation") or {}
        decision = last_eval.get("decision") or "No trade"
        rule_num = last_eval.get("rule")
        stars = last_eval.get("stars", "")
        size = last_eval.get("size", "")
        ce_type = last_eval.get("ce_type") or "Neutral"
        pe_type = last_eval.get("pe_type") or "Neutral"
        delta_ce = float(last_eval.get("delta_ce") or 0.0)
        delta_pe = float(last_eval.get("delta_pe") or 0.0)
        ce_price_pct = float(last_eval.get("ce_price_pct") or 0.0)
        pe_price_pct = float(last_eval.get("pe_price_pct") or 0.0)

        # OI Balance ratio I = (delta_pe - delta_ce) / (|delta_pe| + |delta_ce|)
        tot_oi_abs = abs(delta_pe) + abs(delta_ce)
        oi_i = round((delta_pe - delta_ce) / tot_oi_abs, 2) if tot_oi_abs > 0 else 0.0

        # Futures
        fut_snap_ltp = float(idx_st.get("futures_snap_ltp") or idx_st.get("prev_close") or 1.0)
        fut_ltp = float(idx_st.get("futures_ltp") or fut_snap_ltp)
        fut_price_pct = round(((fut_ltp - fut_snap_ltp) / fut_snap_ltp) * 100.0, 2) if fut_snap_ltp > 0 else 0.0

        fut_snap_oi = float(idx_st.get("futures_snap_oi") or 0.0)
        fut_oi = float(idx_st.get("futures_oi") or 0.0)
        fut_oi_pct = round(((fut_oi - fut_snap_oi) / fut_snap_oi) * 100.0, 2) if (fut_snap_oi > 100 and fut_oi > 0) else 0.0
        fut_bu = idx_st.get("futures_buildup", "Neutral")

        # Gate status
        if ukey == "BSE_INDEX|SENSEX":
            gate_status = "OFF"
        else:
            if decision == "CE Buy":
                gate_status = "Pass" if fut_bu in ("LB", "SC") else "Block"
            elif decision == "PE Buy":
                gate_status = "Pass" if fut_bu in ("SB", "LU") else "Block"
            else:
                gate_status = "Pass"

        # Action status
        if decision == "CE Buy":
            action = "ENTER CE"
        elif decision == "PE Buy":
            action = "ENTER PE"
        elif idx_st.get("has_position"):
            action = "EXIT 1/2"
        else:
            action = "NO_TRADE"

        atm_strike = idx_st.get("atm_strike")
        if not atm_strike and spot > 0:
            step = float(idx_st.get("step") or 50.0)
            atm_strike = round(spot / step) * step

        # Strikes detail
        strikes_data = []
        l_keys = idx_st.get("locked_keys") or []
        hot_cache = strategy_engine.hot_cache
        snap = idx_st.get("snapshot") or {}

        by_strike: dict[float, dict[str, Any]] = {}
        for k in l_keys:
            c = hot_cache.get(k, {})
            stk = float(c.get("strike") or 0.0)
            side = c.get("side")
            if not stk or not side:
                continue
            if stk not in by_strike:
                by_strike[stk] = {"strike": stk}
            ltp = float(c.get("ltp") or 0.0)
            oi = float(c.get("oi") or 0.0)
            c_snap = snap.get(k, {})
            snap_ltp = float(c_snap.get("ltp") or c.get("ref_ltp") or ltp or 1.0)
            snap_oi = float(c_snap.get("oi") or c.get("prev_oi") or oi or 1.0)

            d_oi = oi - snap_oi if snap_oi > 0 else 0.0
            d_oi_pct = round((d_oi / snap_oi) * 100.0, 2) if snap_oi > 0 else 0.0
            p_pct = round(((ltp - snap_ltp) / snap_ltp) * 100.0, 2) if snap_ltp > 0 else 0.0

            p_dir = "UP" if p_pct >= 0.5 else ("DOWN" if p_pct <= -0.5 else "FLAT")
            oi_dir = "UP" if d_oi_pct >= 1.0 else ("DOWN" if d_oi_pct <= -1.0 else "FLAT")
            if p_dir == "UP" and oi_dir == "UP":
                bu = "LB"
            elif p_dir == "DOWN" and oi_dir == "UP":
                bu = "SB"
            elif p_dir == "UP" and oi_dir == "DOWN":
                bu = "SC"
            elif p_dir == "DOWN" and oi_dir == "DOWN":
                bu = "LU"
            else:
                bu = "NEU"

            if side == "CE":
                by_strike[stk]["ce_oi"] = oi
                by_strike[stk]["ce_delta_oi"] = d_oi
                by_strike[stk]["ce_delta_oi_pct"] = d_oi_pct
                by_strike[stk]["ce_price_pct"] = p_pct
                by_strike[stk]["ce_buildup"] = bu
            else:
                by_strike[stk]["pe_oi"] = oi
                by_strike[stk]["pe_delta_oi"] = d_oi
                by_strike[stk]["pe_delta_oi_pct"] = d_oi_pct
                by_strike[stk]["pe_price_pct"] = p_pct
                by_strike[stk]["pe_buildup"] = bu

        sorted_stks = sorted(by_strike.values(), key=lambda x: x["strike"])

        # Aggregate live sum across ATM ±1 strikes if last_eval delta is 0
        sum_delta_ce = sum(stk_val.get("ce_delta_oi", 0.0) for stk_val in by_strike.values())
        sum_delta_pe = sum(stk_val.get("pe_delta_oi", 0.0) for stk_val in by_strike.values())
        if delta_ce == 0.0 and sum_delta_ce != 0.0:
            delta_ce = sum_delta_ce
        if delta_pe == 0.0 and sum_delta_pe != 0.0:
            delta_pe = sum_delta_pe

        # Re-derive OI Balance ratio I with live delta
        tot_oi_abs = abs(delta_pe) + abs(delta_ce)
        oi_i = round((delta_pe - delta_ce) / tot_oi_abs, 2) if tot_oi_abs > 0 else 0.0

        # Real consecutive hold status from engine state
        c_count = int(idx_st.get("consecutive_count", 0))
        c_sig = idx_st.get("consecutive_signal")
        if decision in ("CE Buy", "PE Buy") or c_sig:
            hold_status = f"{c_count}/2"
        else:
            hold_status = "-"

        indices_out.append({
            "key": ukey,
            "name": idx_st.get("name", ukey),
            "spot_price": spot,
            "atm_strike": atm_strike,
            "net_change_pct": net_chg_pct,
            "delta_ce": delta_ce,
            "delta_ce_pct": round(delta_ce / 100000.0, 2) if delta_ce else 0.0,
            "ce_price_pct": ce_price_pct,
            "ce_buildup": ce_type,
            "delta_pe": delta_pe,
            "delta_pe_pct": round(delta_pe / 100000.0, 2) if delta_pe else 0.0,
            "pe_price_pct": pe_price_pct,
            "pe_buildup": pe_type,
            "oi_balance_i": oi_i,
            "rule_number": rule_num,
            "decision": decision,
            "futures_price_pct": fut_price_pct,
            "futures_oi_pct": fut_oi_pct,
            "futures_buildup": fut_bu,
            "futures_gate": gate_status,
            "stars": stars,
            "size": size,
            "hold": hold_status,
            "action": action,
            "strikes": sorted_stks,
        })

    # 2. Top stocks enriched with live engine evaluation and database state
    wl = stock_screener.get_tomorrow_watchlist()
    raw_bullish = wl.get("top_bullish", [])
    raw_bearish = wl.get("top_bearish", [])

    def _enrich_stock(s: dict[str, Any], direction_hint: str) -> dict[str, Any]:
        sym = s.get("symbol", "")
        stk_st = strategy_engine.stock_underlyings.get(sym, {})
        last_eval = stk_st.get("last_evaluation") or {}

        delta_ce = float(last_eval.get("delta_ce") or 0.0)
        delta_pe = float(last_eval.get("delta_pe") or 0.0)
        ce_price_pct = float(last_eval.get("ce_price_pct") or 0.0)
        pe_price_pct = float(last_eval.get("pe_price_pct") or 0.0)
        ce_bu = last_eval.get("ce_type")
        pe_bu = last_eval.get("pe_type")
        rule_num = last_eval.get("rule")
        decision = last_eval.get("decision") or "No trade"

        tot_oi_abs = abs(delta_pe) + abs(delta_ce)
        oi_i = round((delta_pe - delta_ce) / tot_oi_abs, 2) if tot_oi_abs > 0 else (0.0 if (delta_ce or delta_pe) else None)

        fut_ltp = float(stk_st.get("futures_ltp") or 0.0)
        fut_prev = float(stk_st.get("futures_prev_close") or fut_ltp or 1.0)
        fut_price_pct = round(((fut_ltp - fut_prev) / fut_prev) * 100.0, 2) if (fut_prev > 0 and fut_ltp > 0) else float(s.get("change_pct") or 0.0)

        fut_oi = float(stk_st.get("futures_oi") or 0.0)
        fut_prev_oi = float(stk_st.get("futures_prev_oi") or fut_oi or 1.0)
        fut_oi_pct = round(((fut_oi - fut_prev_oi) / fut_prev_oi) * 100.0, 2) if (fut_prev_oi > 0 and fut_oi > 0) else float(s.get("fut_oi_change_pct") or 0.0)
        fut_bu = stk_st.get("futures_buildup") or s.get("fut_buildup") or "NEU"

        if decision == "CE Buy":
            action = "ENTER CE"
        elif decision == "PE Buy":
            action = "ENTER PE"
        elif stk_st.get("current_position"):
            action = "EXIT 1/2"
        else:
            action = "NO_TRADE"

        clv = float(s.get("clv") if s.get("clv") is not None else 0.5)
        score = round(clv * 100) if direction_hint == "BULLISH" else round((clv - 1.0) * 100)

        return {
            "symbol": sym,
            "name": s.get("name", sym),
            "score": score,
            "rank": s.get("rank") or stk_st.get("watchlist_rank") or "★★",
            "size": last_eval.get("size") or "Normal",
            "delta_ce": delta_ce,
            "ce_oi_pct": round(delta_ce / 100000.0, 2) if delta_ce else None,
            "ce_price_pct": ce_price_pct if ce_price_pct != 0.0 else None,
            "ce_buildup": ce_bu,
            "delta_pe": delta_pe,
            "pe_oi_pct": round(delta_pe / 100000.0, 2) if delta_pe else None,
            "pe_price_pct": pe_price_pct if pe_price_pct != 0.0 else None,
            "pe_buildup": pe_bu,
            "oi_balance_i": oi_i,
            "rule_number": rule_num,
            "futures_price_pct": fut_price_pct,
            "futures_oi_pct": fut_oi_pct,
            "fut_buildup": fut_bu,
            "decision": decision,
            "action": action,
        }

    max_bull = getattr(strategy_engine, "max_bullish_stocks", 5)
    max_bear = getattr(strategy_engine, "max_bearish_stocks", 5)
    top_bullish = [_enrich_stock(s, "BULLISH") for s in raw_bullish[:max_bull]]
    top_bearish = [_enrich_stock(s, "BEARISH") for s in raw_bearish[:max_bear]]

    # 3. Alerts
    recent_alerts = strategy_engine.get_alerts(limit=50)

    last_tick_ts = getattr(strategy_engine, "last_tick_time", 0.0)
    last_update_str = datetime.fromtimestamp(last_tick_ts, tz=IST).strftime("%H:%M:%S") if last_tick_ts > 0 else "09:15:00"

    return jsonify({
        "status": "online",
        "feed_connected": feed.connected if feed else False,
        "feed_instruments": len(feed.subscribed_keys) if feed else 0,
        "mode": getattr(strategy_engine, "mode", "paper"),
        "baseline_time": "09:15:00",
        "last_market_update": last_update_str,
        "today_trades": st.get("today_trades", 0),
        "today_net_pnl": st.get("today_net_pnl_inr", 0.0),
        "indices": indices_out,
        "top_bullish": top_bullish,
        "top_bearish": top_bearish,
        "recent_alerts": recent_alerts,
        "config": {
            "max_bullish_stocks": max_bull,
            "max_bearish_stocks": max_bear,
            "lots_full": getattr(strategy_engine, "lots_full", 1),
            "lots_normal": getattr(strategy_engine, "lots_normal", 1),
            "lots_scalp": getattr(strategy_engine, "lots_scalp", 1),
            "index_lots": getattr(strategy_engine, "index_lots", {}),
        },
        "timestamp_ist": datetime.now(IST).strftime("%H:%M:%S"),
    })

@app.get("/api/health")
def api_health() -> Response:
    health_data: dict[str, Any] = {
        "application": "upstox-nifty-phase1",
        "configured": settings.configured,
        "feed_running": feed.running,
        "feed_connected": feed.connected,
        "feed_mode": settings.feed_mode,
        "instrument_keys": list(settings.instrument_keys),
        "database": str(settings.db_path),
        "counts": database.counts(),
        "status": latest_status,
        "server_time_ist": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if hasattr(feed, "indices_feed") and hasattr(feed, "stocks_feed"):
        health_data.update({
            "dual_feed": True,
            "both_connected": getattr(feed, "both_connected", False),
            "indices_connected": feed.indices_feed.connected,
            "stocks_connected": feed.stocks_feed.connected,
            "indices_subscribed_count": len(feed.indices_feed.subscribed_keys),
            "stocks_subscribed_count": len(feed.stocks_feed.subscribed_keys),
            "total_subscribed_count": len(feed.subscribed_keys),
        })
    else:
        # Single UpstoxFeed mode (combined)
        health_data.update({
            "dual_feed": False,
            "both_connected": feed.connected,
            "total_subscribed_count": len(feed.subscribed_keys),
        })
    return jsonify(health_data)

@app.get("/api/instruments")
def api_instruments() -> Response:
    insts = set(database.instruments()).union(settings.instrument_keys)
    if hasattr(feed, "subscribed_keys"):
        insts = insts.union(feed.subscribed_keys)
    values = sorted(insts)
    return jsonify({"instruments": values})

@app.get("/api/ticks")
def api_ticks() -> Response:
    limit = request.args.get("limit", default=50, type=int)
    limit = max(1, min(limit or 50, 500))
    instrument_key = request.args.get("instrument_key", "").strip() or None

    if recent_ticks and not instrument_key:
        values = list(recent_ticks)[:limit]
    else:
        values = database.latest_ticks(limit=limit, instrument_key=instrument_key)

    cleaned_values = [
        {key: value for key, value in tick.items() if key != "raw"}
        for tick in values
    ]
    return jsonify({"ticks": cleaned_values})

def _aggregate_candles(candles_1m: list[dict[str, Any]], timeframe: str) -> list[dict[str, Any]]:
    if timeframe == "1m":
        return sorted(candles_1m, key=lambda x: int(x["candle_start_ms"]))

    bucket_ms_map = {
        "3m": 3 * 60 * 1000,
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
    }
    is_daily = timeframe in ("d", "1d", "day")

    candles_sorted = sorted(candles_1m, key=lambda x: int(x["candle_start_ms"]))
    grouped: dict[Any, dict[str, Any]] = {}
    for c in candles_sorted:
        start_ms = int(c["candle_start_ms"])
        if is_daily:
            dt = datetime.fromtimestamp(start_ms / 1000, tz=IST)
            bucket_key = dt.strftime("%Y-%m-%d")
            midnight_dt = dt.replace(hour=9, minute=15, second=0, microsecond=0)
            bucket_ms = int(midnight_dt.timestamp() * 1000)
        else:
            dur = bucket_ms_map.get(timeframe, 60 * 1000)
            bucket_ms = (start_ms // dur) * dur
            bucket_key = bucket_ms

        if bucket_key not in grouped:
            grouped[bucket_key] = {
                "instrument_key": c.get("instrument_key"),
                "candle_start_ms": bucket_ms,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c.get("volume", 0),
                "open_interest": c.get("open_interest"),
                "tick_count": c.get("tick_count", 0),
                "is_closed": c.get("is_closed", 0),
                "updated_at_ms": c.get("updated_at_ms", start_ms),
            }
        else:
            g = grouped[bucket_key]
            g["high"] = max(float(g["high"]), float(c["high"]))
            g["low"] = min(float(g["low"]), float(c["low"]))
            g["close"] = c["close"]
            g["volume"] = float(g.get("volume", 0)) + float(c.get("volume", 0))
            g["tick_count"] = int(g.get("tick_count", 0)) + int(c.get("tick_count", 0))
            g["updated_at_ms"] = max(int(g.get("updated_at_ms", 0)), int(c.get("updated_at_ms", 0)))
            g["is_closed"] = c.get("is_closed", g.get("is_closed", 0))
    return list(grouped.values())

@app.get("/api/candles")
def api_candles() -> Response:
    instrument_key = request.args.get("instrument_key", "").strip() or None
    timeframe = request.args.get("timeframe", "1m").strip().lower()
    limit = request.args.get(
        "limit",
        default=settings.ui_candle_limit,
        type=int,
    )
    limit = max(1, min(limit or settings.ui_candle_limit, 2000))

    multipliers = {
        "1m": 1,
        "3m": 3,
        "5m": 5,
        "15m": 15,
        "d": 400,
        "1d": 400,
    }
    multiplier = multipliers.get(timeframe, 1)
    values = database.latest_candles(
        instrument_key=instrument_key,
        limit=min(limit * multiplier, 3000),
    )

    if timeframe in ("3m", "5m", "15m", "d", "1d"):
        values = _aggregate_candles(values, timeframe)
        if len(values) > limit:
            values = values[-limit:]

    return jsonify({"candles": values})

def _fetch_upstox_json(url: str, token: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw_error)
        except Exception:
            return exc.code, raw_error
    except Exception as exc:
        return 500, str(exc)

@app.get("/api/option-chain/expiries")
def api_option_expiries() -> Response:
    if not settings.access_token:
        return jsonify({"error": "UPSTOX_ACCESS_TOKEN is missing"}), 400
    # #2 — support dynamic instrument_key query param (default: NIFTY 50)
    instrument_key = request.args.get("instrument_key", "NSE_INDEX|Nifty 50").strip()
    try:
        url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key, safe='|')}"
        status, payload = _fetch_upstox_json(url, settings.access_token)
        if status != 200 or not isinstance(payload, dict):
            return jsonify({"error": f"Upstox API error: {status}", "detail": payload}), status
        contracts = payload.get("data") or []
        expiries = sorted(list({c["expiry"] for c in contracts if c.get("expiry")}))
        return jsonify({"expiries": expiries})
    except Exception as exc:
        LOGGER.exception("Failed to fetch option expiries")
        return jsonify({"error": str(exc)}), 500

@app.get("/api/option-chain")
def api_option_chain() -> Response:
    if not settings.access_token:
        return jsonify({"error": "UPSTOX_ACCESS_TOKEN is missing"}), 400

    instrument_key = request.args.get("instrument_key", "NSE_INDEX|Nifty 50").strip()
    expiry_date = request.args.get("expiry_date", "").strip()

    status, payload, spot_price, data, resolved_expiry = fetch_option_chain_data(instrument_key, expiry_date)
    if status != 200:
        return jsonify({"error": f"Upstox API error: {status}", "detail": payload}), status

    if hasattr(strategy_engine, "process_option_chain_snapshot") and spot_price and data and getattr(strategy_engine, "enabled", False):
        sig = strategy_engine.process_option_chain_snapshot(spot_price, data, instrument_key)
        if sig:
            publish("strategy_signal", sig)


    return jsonify({
        "status": "success",
        "expiry_date": resolved_expiry,
        "spot_price": spot_price,
        "strikes": data,
    })

@app.post("/api/feed/subscribe")
def api_feed_subscribe() -> Response:
    body = request.get_json(silent=True) or {}
    keys = body.get("keys", [])
    if feed and hasattr(feed, "subscribe") and keys:
        feed.subscribe(keys)
    return jsonify({"status": "subscribed", "keys": keys})

@app.get("/api/strategy/status")
def api_strategy_status() -> Response:
    return jsonify(strategy_engine.get_status())

@app.post("/api/strategy/config")
def api_strategy_config() -> Response:
    body = request.get_json(silent=True) or {}
    new_cfg = strategy_engine.update_config(body)
    return jsonify(new_cfg)

@app.route("/api/strategy/exit", methods=["GET", "POST"])
def api_strategy_exit() -> Response:
    """Manual exit trigger for open position.
    Supports:
      - scope="INDICES" or key="INDICES": exits all 3 indices (Nifty, Bank Nifty, Sensex).
      - scope="STOCKS" or key="STOCKS": exits all stock options.
      - scope="ALL" or None: exits all positions.
      - or key for single underlying index/stock.
    """
    body = request.get_json(silent=True) or {}
    scope = (body.get("scope") or request.args.get("scope") or "").strip().upper()
    key = body.get("underlying_key") or body.get("index") or request.args.get("index") or None

    if scope == "INDICES" or (key and str(key).strip().upper() in ("INDICES", "INDEX")):
        closed = strategy_engine.manual_exit_indices(reason="MANUAL")
        publish("strategy_update", strategy_engine.get_status())
        return jsonify({"success": True, "scope": "INDICES", "closed": closed})

    if scope == "STOCKS" or (key and str(key).strip().upper() in ("STOCKS", "STOCK")):
        closed = strategy_engine.manual_exit_stocks(reason="MANUAL")
        publish("strategy_update", strategy_engine.get_status())
        return jsonify({"success": True, "scope": "STOCKS", "closed": closed})

    if key and str(key).strip().upper() in ("ALL", "", "NONE"):
        key = None

    pos = strategy_engine.manual_exit(reason="MANUAL", underlying_key=key)
    publish("strategy_update", strategy_engine.get_status())
    return jsonify({"success": True, "position": pos})

@app.route("/api/strategy/arm-now", methods=["GET", "POST"])
def api_strategy_arm_now() -> Response:
    """Manual trigger: forces immediate planning, locking, snapshot, and arming for an index or stock."""
    body = request.get_json(silent=True) or {}
    key = body.get("underlying_key") or body.get("index") or body.get("symbol") or request.args.get("index") or request.args.get("symbol") or None
    status = strategy_engine.force_arm_now(underlying_key=key)
    publish("strategy_update", status)
    return jsonify({"success": True, "status": status})

@app.route("/api/strategy/manual-enter", methods=["GET", "POST"])
def api_strategy_manual_enter() -> Response:
    """Manual enter trigger: enters a CE or PE trade on demand based on strategy rules."""
    body = request.get_json(silent=True) or {}
    key = body.get("underlying_key") or body.get("index") or body.get("symbol") or request.args.get("index") or request.args.get("symbol") or None
    side = body.get("side") or request.args.get("side") or "CE"
    trade = strategy_engine.manual_enter(underlying_key=key, side=side)
    publish("strategy_update", strategy_engine.get_status())
    return jsonify({"success": bool(trade), "trade": trade})

@app.post("/api/strategy/reset")
def api_strategy_reset() -> Response:
    """Reset strategy state back to PLANNING for testing."""
    status = strategy_engine.reset_state()
    publish("strategy_update", status)
    return jsonify({"success": True, "status": status})

@app.get("/api/strategy/trades")
def api_strategy_trades() -> Response:
    limit = request.args.get("limit", default=200, type=int)
    date_param = request.args.get("date", default=None, type=str)

    # Live today by default: only show trades from today unless specific date requested
    if not date_param or date_param.strip().lower() == "today":
        target_date = datetime.now(IST).strftime("%Y-%m-%d")
    elif date_param.strip().lower() == "all":
        target_date = None
    else:
        target_date = date_param.strip()

    trades = database.get_strategy_trades(limit=limit, trade_date=target_date)

    # Merge live MTM P&L for open positions so UI Journal displays ticking live P&L on every tick
    if hasattr(strategy_engine, "get_live_open_positions"):
        try:
            live_map = strategy_engine.get_live_open_positions()
            trade_id_map = {p.get("trade_id"): p for p in live_map.values() if p.get("trade_id")}
            for t in trades:
                if t.get("status") == "OPEN":
                    tid = t.get("trade_id")
                    if tid in trade_id_map:
                        live = trade_id_map[tid]
                        t["net_pnl"] = live.get("net_pnl", 0.0)
                        t["pnl_points"] = live.get("pnl_points", 0.0)
                        t["current_price"] = live.get("current_price", 0.0)
                        t["exit_price"] = live.get("bid_price", 0.0)
        except Exception as ex:
            LOGGER.debug("Could not merge live trades PnL: %s", ex)

    return jsonify({
        "trades": trades,
        "date": target_date or "all",
        "today": datetime.now(IST).strftime("%Y-%m-%d"),
        "count": len(trades),
    })

@app.get("/api/strategy/trade-dates")
def api_strategy_trade_dates() -> Response:
    dates = database.get_strategy_trade_dates()
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    if today_str not in dates:
        dates.insert(0, today_str)
    return jsonify({"dates": dates, "today": today_str})

@app.get("/api/strategy/signals")
def api_strategy_signals() -> Response:
    limit = request.args.get("limit", default=100, type=int)
    signals = database.get_strategy_signals(limit=limit)
    return jsonify({"signals": signals})

@app.get("/api/strategy/alerts")
def api_strategy_alerts() -> Response:
    limit = request.args.get("limit", default=50, type=int)
    alerts = strategy_engine.get_alerts(limit=limit) if hasattr(strategy_engine, "get_alerts") else []
    return jsonify({"alerts": alerts})

@app.get("/api/strategy/flow")
def api_strategy_flow() -> Response:
    """Section 6B Phase 1 Simple PCR Flow Monitor endpoint."""
    try:
        return jsonify(FLOW_ENGINE.get_phase1_dashboard())
    except Exception as exc:
        LOGGER.error("Failed to generate flow dashboard: %s", exc)
        return jsonify({"status": "LIVE", "timestamp": "", "rows": [], "alerts": []})

# -------------------------------------------------------------------------
# Release 2: NIFTY 50 Stock Screener & Selection Endpoints
# -------------------------------------------------------------------------

@app.get("/api/stocks/screener")
def api_stocks_screener() -> Response:
    """Fetch live screener data for all 50 NIFTY stocks with Volume and Build-up."""
    stocks = stock_screener.fetch_quotes()
    return jsonify({"stocks": stocks, "count": len(stocks)})

@app.get("/api/stocks/config")
def api_stocks_config_get() -> Response:
    """Get top N stocks configuration (top_n, lots, enabled)."""
    top_n = request.args.get("top_n", default=None, type=int)
    cfg = stock_screener.get_top_stocks_config(top_n=top_n)
    return jsonify(cfg)

@app.post("/api/stocks/config")
def api_stocks_config_save() -> Response:
    """Save top N stocks configuration (top_n, lots, enabled)."""
    body = request.get_json(silent=True) or {}
    saved = stock_screener.save_top_stocks_config(body)
    if hasattr(strategy_engine, "load_stock_configs"):
        strategy_engine.load_stock_configs()
    publish("strategy_update", strategy_engine.get_status())
    return jsonify({"success": True, "config": saved})

@app.post("/api/stocks/select")
def api_stocks_select() -> Response:
    """Save selected stocks and lot quantities."""
    body = request.get_json(silent=True) or {}
    selections = body.get("selections") or {}
    stock_screener.save_selected_stocks(selections)
    if hasattr(strategy_engine, "load_stock_configs"):
        strategy_engine.load_stock_configs()
    return jsonify({"success": True, "selections": selections})

@app.get("/api/stocks/watchlist")
def api_stocks_watchlist_get() -> Response:
    """Get Tomorrow Watchlist (3:15 PM EOD scoring: Top 5 Bullish, Top 5 Bearish)."""
    wl = stock_screener.get_tomorrow_watchlist()
    return jsonify(wl)

@app.post("/api/stocks/watchlist/build")
def api_stocks_watchlist_build() -> Response:
    """Trigger manual re-calculation of Tomorrow Watchlist."""
    wl = stock_screener.build_tomorrow_watchlist()
    if hasattr(strategy_engine, "load_stock_configs"):
        strategy_engine.load_stock_configs()
    publish("strategy_update", strategy_engine.get_status())
    return jsonify({"success": True, "watchlist": wl})

@app.get("/api/stocks/plan/<symbol>")
def api_stocks_plan(symbol: str) -> Response:
    """Fetch and plan 8 ITM Option contracts for a constituent NIFTY stock."""
    spot = request.args.get("spot", default=0.0, type=float)
    plan = stock_screener.plan_stock_itm_contracts(symbol=symbol, spot=spot)
    return jsonify(plan)

@app.post("/api/strategy/rehearsal")
def api_strategy_rehearsal() -> Response:
    """Run morning rehearsal mode to audit trigger & strike selector latency."""
    res = strategy_engine.rehearse_entry()
    return jsonify(res)

@app.get("/api/strategy/export")
def api_strategy_export() -> Response:
    date_param = request.args.get("date", default=None, type=str)
    if not date_param or date_param.strip().lower() == "today":
        target_date = datetime.now(IST).strftime("%Y-%m-%d")
    elif date_param.strip().lower() == "all":
        target_date = None
    else:
        target_date = date_param.strip()

    trades = database.get_strategy_trades(limit=2000, trade_date=target_date)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Trade ID", "Trade Date", "Side", "Symbol", "Strike", "Lot Size",
        "Entry Time (IST)", "Entry Price", "Exit Time (IST)", "Exit Price",
        "Exit Reason", "P&L Points", "Net P&L (INR)", "Status",
        "Spot at Entry", "ATM at Entry",
        "Build-up", "Bot Latency (ms)", "Network Latency (ms)",
    ])
    for t in trades:
        writer.writerow([
            t.get("trade_id", ""),
            t.get("trade_date", ""),
            t.get("side", ""),
            t.get("symbol", ""),
            t.get("strike", ""),
            t.get("lot_size", 65),
            t.get("entry_time_ist", ""),
            t.get("entry_price", ""),
            t.get("exit_time_ist", ""),
            t.get("exit_price", ""),
            t.get("exit_reason", ""),
            t.get("pnl_points", ""),
            t.get("net_pnl", ""),
            t.get("status", ""),
            t.get("spot_price", ""),
            t.get("atm_strike", ""),
            t.get("build_up", "Neutral"),
            t.get("bot_latency_ms", ""),
            t.get("network_latency_ms", ""),
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=paper_trades.csv"},
    )

@app.get("/api/strategy/export-signals")
def api_strategy_export_signals() -> Response:
    signals = database.get_strategy_signals(limit=2000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Timestamp (IST)", "Timestamp (ms)", "Instrument", "Side", "Strike", "Contract Key",
        "LTP", "Prev LTP", "Qty Delta", "Value (Lakhs)", "Day High", "Prev Close",
        "Bid Price", "Ask Price", "Spread %", "OI Change", "Confidence", "Action Taken",
    ])
    for s in signals:
        inst = s.get("instrument_name") or ("SENSEX" if (s.get("strike") or 0) >= 70000 else "BANKNIFTY" if (s.get("strike") or 0) >= 45000 else "NIFTY")
        writer.writerow([
            s.get("timestamp_ist", ""),
            s.get("timestamp_ms", ""),
            inst,
            s.get("side", ""),
            s.get("strike", ""),
            s.get("contract_key", ""),
            s.get("ltp", ""),
            s.get("prev_ltp", ""),
            s.get("qty_delta", ""),
            s.get("val_lakhs", ""),
            s.get("day_high", ""),
            s.get("prev_close", ""),
            s.get("bid_price", ""),
            s.get("ask_price", ""),
            s.get("spread_pct", ""),
            s.get("oi_change", ""),
            s.get("confidence", ""),
            s.get("action_taken", ""),
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=strategy_signals.csv"},
    )

@app.get("/api/strategy/export-ticks")
def api_strategy_export_ticks() -> Response:
    limit = request.args.get("limit", default=5000, type=int)
    ticks = database.latest_ticks(limit=limit)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Instrument Key", "Exchange Time (ms)", "Received Time (ms)",
        "LTP", "Last Qty", "Avg Price", "Volume", "OI",
        "Bid Price", "Bid Qty", "Ask Price", "Ask Qty",
    ])
    for t in ticks:
        writer.writerow([
            t.get("instrument_key", ""),
            t.get("exchange_timestamp_ms", ""),
            t.get("received_timestamp_ms", ""),
            t.get("ltp", ""),
            t.get("last_quantity", ""),
            t.get("average_price", ""),
            t.get("volume", ""),
            t.get("open_interest", ""),
            t.get("bid_price", ""),
            t.get("bid_quantity", ""),
            t.get("ask_price", ""),
            t.get("ask_quantity", ""),
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=today_ticks.csv"},
    )

@app.get("/api/database/stats")
def api_database_stats() -> Response:
    """Get database size, row counts, and health stats."""
    return jsonify(database.get_db_stats())

@app.post("/api/database/prune")
def api_database_prune() -> Response:
    """Prune raw ticks older than N days (default 3) to reclaim disk space."""
    body = request.get_json(silent=True) or {}
    days = int(body.get("days_to_keep", 3))
    run_vac = bool(body.get("run_vacuum", False))
    res = database.prune_old_ticks(days_to_keep=days, run_vacuum=run_vac)
    return jsonify({"success": True, "stats": res})

@app.get("/api/stream")
def api_stream() -> Response:
    subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)

    with subscribers_lock:
        subscribers.add(subscriber)

    @stream_with_context
    def event_stream():
        try:
            initial_event = {
                "type": "status",
                "payload": latest_status,
                "server_timestamp_ms": int(time.time() * 1000),
            }
            yield f"data: {json.dumps(initial_event)}\n\n"

            while True:
                try:
                    event = subscriber.get(timeout=15)
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            with subscribers_lock:
                subscribers.discard(subscriber)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

def shutdown() -> None:
    try:
        feed.stop()
    finally:
        database.close()

atexit.register(shutdown)

def handle_shutdown_signal(signum: int, frame: Any) -> None:
    LOGGER.info("Received shutdown signal %s.", signum)
    shutdown()
    raise SystemExit(0)

signal.signal(signal.SIGINT, handle_shutdown_signal)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, handle_shutdown_signal)

if __name__ == "__main__":
    import sys
    import socket

    # Test if port is already in use by another instance
    _test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _test_sock.bind(("127.0.0.1", settings.port))
        _test_sock.close()
    except OSError:
        print(f"ERROR: Port {settings.port} is already in use by another running engine instance. Exiting to prevent port/feed collision.")
        sys.exit(1)

    print("=" * 68)
    print("Upstox NIFTY Phase 1 — live data collector")
    print(f"Dashboard: http://{settings.host}:{settings.port}")
    print(f"Database:  {settings.db_path}")
    print(f"Configured: {'yes' if settings.configured else 'no'}")
    print("=" * 68)

    app.run(
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
        threaded=True,
        use_reloader=False,
    )