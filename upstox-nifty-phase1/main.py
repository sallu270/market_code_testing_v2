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

from flask import Flask, Response, jsonify, request, send_file, stream_with_context
import urllib.error
import urllib.parse
import urllib.request

from candle_builder import OneMinuteCandleBuilder
from config import IST, load_settings
from database import MarketDatabase
from stock_screener import StockScreener
from strategy_engine import GapEntryStrategyEngine, PressureStrategyEngine
from upstox_feed import UpstoxFeed

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
    if not status.get("connected") and hasattr(strategy_engine, "add_alert"):
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

        trade = strategy_engine.on_tick(tick)
        if trade:
            publish("strategy_trade", trade)
    except Exception:
        LOGGER.exception("Failed to store or aggregate a market tick.")

feed = UpstoxFeed(
    settings=settings,
    on_tick=handle_tick,
    on_status=handle_status,
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
    while True:
        try:
            now_dt = datetime.now(IST)
            now_time = now_dt.strftime("%H:%M:%S")

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

                # EOD square-off at 15:15:00 by clock (strictly at live bid)
                if now_time >= "15:15:00":
                    if hasattr(strategy_engine, "squareoff_all_indices_1515"):
                        strategy_engine.squareoff_all_indices_1515()
                    elif strategy_engine.state == "HOLDING":
                        strategy_engine.manual_exit(reason="SQUAREOFF_1515")
                    if hasattr(strategy_engine, "squareoff_all_stocks_1515"):
                        strategy_engine.squareoff_all_stocks_1515()

                    # 3:15 PM EOD Watchlist Build (Dr. Vijay Bhilwade Specification)
                    if "15:15:00" <= now_time <= "15:20:00" and not getattr(strategy_engine, "_watchlist_built_today", False):
                        try:
                            stock_screener.build_tomorrow_watchlist()
                            strategy_engine._watchlist_built_today = True
                            LOGGER.info("15:15:00 EOD Tomorrow Watchlist automatically generated.")
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
def index() -> Response:
    return jsonify({
        "status": "online",
        "service": "GChart Trading Backend Engine",
        "version": "2.0.0",
        "client": "GChartWpf (.NET 8 WPF Desktop Terminal)",
        "configured": settings.configured,
        "feed_running": feed.running,
        "timestamp_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.get("/api/health")
def api_health() -> Response:
    return jsonify(
        {
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
    )

@app.get("/api/instruments")
def api_instruments() -> Response:
    values = sorted(
        set(database.instruments()).union(settings.instrument_keys)
    )
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
        return candles_1m

    bucket_ms_map = {
        "3m": 3 * 60 * 1000,
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
    }
    is_daily = timeframe in ("d", "1d", "day")

    grouped: dict[Any, dict[str, Any]] = {}
    for c in candles_1m:
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
    limit = max(1, min(limit or settings.ui_candle_limit, 1000))

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
        limit=min(limit * multiplier, 2000),
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

    if data and hasattr(feed, "subscribe"):
        try:
            keys_to_sub = []
            for item in data[:40]:
                call_k = item.get("call_options", {}).get("instrument_key")
                put_k = item.get("put_options", {}).get("instrument_key")
                if call_k:
                    keys_to_sub.append(call_k)
                if put_k:
                    keys_to_sub.append(put_k)
            if keys_to_sub:
                feed.subscribe(keys_to_sub)
        except Exception as ex:
            LOGGER.warning("Could not auto-subscribe option chain keys: %s", ex)

    return jsonify({
        "status": "success",
        "expiry_date": resolved_expiry,
        "spot_price": spot_price,
        "strikes": data,
    })

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