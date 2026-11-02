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

from candle_builder import OneMinuteCandleBuilder
from config import IST, load_settings
from database import MarketDatabase
from strategy_engine import PressureStrategyEngine
from upstox_feed import UpstoxFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("phase1")

settings = load_settings()
database = MarketDatabase(settings.db_path)
strategy_engine = PressureStrategyEngine(database=database, base_dir=settings.db_path.parent)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

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
    except Exception:
        LOGGER.exception("Failed to store or aggregate a market tick.")

feed = UpstoxFeed(
    settings=settings,
    on_tick=handle_tick,
    on_status=handle_status,
)

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

def strategy_background_worker() -> None:
    """Automated background worker that evaluates option chain and pre-warms history without UI dependency."""
    LOGGER.info("Strategy automated background worker started.")
    time.sleep(2)
    # Pre-warm all subscribed indices at startup
    if settings.configured and settings.access_token:
        for inst_key in settings.instrument_keys:
            try:
                status, _, spot, strikes, _ = fetch_option_chain_data(inst_key)
                if status == 200 and strikes:
                    strategy_engine._warmup_strikes(strikes)
                    LOGGER.info("Pre-warmed %s strikes for %s", len(strikes), inst_key)
            except Exception as exc:
                LOGGER.debug("Pre-warm failed for %s: %s", inst_key, exc)

    while True:
        try:
            now_dt = datetime.now(IST)
            now_time = now_dt.strftime("%H:%M:%S")
            # Run between 09:14:00 (pre-warm) and 15:15:05 IST
            if "09:14:00" <= now_time <= "15:15:05" and strategy_engine.enabled and settings.access_token:
                for inst_key in settings.instrument_keys:
                    status, _, spot_price, data, _ = fetch_option_chain_data(inst_key)
                    if status == 200 and spot_price and data:
                        sig = strategy_engine.process_option_chain_snapshot(spot_price, data, inst_key)
                        if sig:
                            publish("strategy_signal", sig)
        except Exception:
            LOGGER.exception("Error in strategy background worker loop")
        time.sleep(1.0)

strategy_worker_thread = threading.Thread(
    target=strategy_background_worker,
    name="strategy-background-worker",
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
def index() -> str:
    return render_template(
        "index.html",
        configured=settings.configured,
        instrument_keys=list(settings.instrument_keys),
        feed_mode=settings.feed_mode,
    )

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

def _aggregate_to_5m(candles_1m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for c in candles_1m:
        start_ms = int(c["candle_start_ms"])
        bucket = (start_ms // 300000) * 300000
        if bucket not in grouped:
            grouped[bucket] = {
                "instrument_key": c.get("instrument_key"),
                "candle_start_ms": bucket,
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
            g = grouped[bucket]
            g["high"] = max(float(g["high"]), float(c["high"]))
            g["low"] = min(float(g["low"]), float(c["low"]))
            g["close"] = c["close"]
            g["volume"] = float(g.get("volume", 0)) + float(c.get("volume", 0))
            g["tick_count"] = int(g.get("tick_count", 0)) + int(c.get("tick_count", 0))
            g["updated_at_ms"] = max(int(g.get("updated_at_ms", 0)), int(c.get("updated_at_ms", 0)))
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

    multiplier = 5 if timeframe == "5m" else 1
    values = database.latest_candles(
        instrument_key=instrument_key,
        limit=limit * multiplier,
    )

    if timeframe == "5m":
        values = _aggregate_to_5m(values)
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

    if spot_price and data and strategy_engine.enabled:
        sig = strategy_engine.process_option_chain_snapshot(spot_price, data, instrument_key)
        if sig:
            publish("strategy_signal", sig)

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

@app.get("/api/strategy/trades")
def api_strategy_trades() -> Response:
    limit = request.args.get("limit", default=100, type=int)
    trades = database.get_strategy_trades(limit=limit)
    return jsonify({"trades": trades})

@app.get("/api/strategy/signals")
def api_strategy_signals() -> Response:
    limit = request.args.get("limit", default=100, type=int)
    signals = database.get_strategy_signals(limit=limit)
    return jsonify({"signals": signals})

@app.get("/api/strategy/export")
def api_strategy_export() -> Response:
    trades = database.get_strategy_trades(limit=1000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Trade ID", "Trade Date", "Side", "Symbol", "Strike", "Lot Size",
        "Entry Time (IST)", "Entry Price", "Exit Time (IST)", "Exit Price",
        "Exit Reason", "P&L Points", "Net P&L (INR)", "Status",
        "Spot at Entry", "ATM at Entry",
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
        "Timestamp (IST)", "Timestamp (ms)", "Side", "Strike", "Contract Key",
        "LTP", "Prev LTP", "Qty Delta", "Value (Lakhs)", "Day High", "Prev Close",
        "Bid Price", "Ask Price", "Spread %", "OI Change", "Confidence", "Action Taken",
    ])
    for s in signals:
        writer.writerow([
            s.get("timestamp_ist", ""),
            s.get("timestamp_ms", ""),
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