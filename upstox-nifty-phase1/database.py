from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

class MarketDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path),
            check_same_thread=False,
            timeout=30,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()
        self._create_schema()

    def _configure(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL;")
            self._connection.execute("PRAGMA synchronous=NORMAL;")
            self._connection.execute("PRAGMA foreign_keys=ON;")
            self._connection.commit()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_key TEXT NOT NULL,
                    exchange_timestamp_ms INTEGER NOT NULL,
                    received_timestamp_ms INTEGER NOT NULL,
                    ltp REAL NOT NULL,
                    last_quantity REAL,
                    average_price REAL,
                    volume REAL,
                    open_interest REAL,
                    bid_price REAL,
                    bid_quantity REAL,
                    ask_price REAL,
                    ask_quantity REAL,
                    raw_json TEXT NOT NULL,
                    UNIQUE(
                        instrument_key,
                        exchange_timestamp_ms,
                        ltp,
                        received_timestamp_ms
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_ticks_instrument_time
                    ON ticks(instrument_key, exchange_timestamp_ms DESC);

                CREATE TABLE IF NOT EXISTS candles_1m (
                    instrument_key TEXT NOT NULL,
                    candle_start_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL DEFAULT 0,
                    open_interest REAL,
                    tick_count INTEGER NOT NULL DEFAULT 0,
                    is_closed INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(instrument_key, candle_start_ms)
                );

                CREATE INDEX IF NOT EXISTS idx_candles_instrument_time
                    ON candles_1m(instrument_key, candle_start_ms DESC);

                CREATE TABLE IF NOT EXISTS strategy_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE NOT NULL,
                    side TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    strike REAL NOT NULL,
                    lot_size INTEGER NOT NULL DEFAULT 65,
                    entry_time_ist TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_timestamp_ms INTEGER NOT NULL,
                    exit_time_ist TEXT,
                    exit_price REAL,
                    exit_timestamp_ms INTEGER,
                    exit_reason TEXT,
                    pnl_points REAL,
                    net_pnl REAL,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ist TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    strike REAL NOT NULL,
                    contract_key TEXT NOT NULL,
                    ltp REAL NOT NULL,
                    prev_ltp REAL,
                    qty_delta REAL,
                    val_lakhs REAL,
                    day_high REAL,
                    prev_close REAL,
                    action_taken TEXT NOT NULL
                );
                """
            )
            self._connection.commit()

    def insert_tick(self, tick: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO ticks (
                    instrument_key,
                    exchange_timestamp_ms,
                    received_timestamp_ms,
                    ltp,
                    last_quantity,
                    average_price,
                    volume,
                    open_interest,
                    bid_price,
                    bid_quantity,
                    ask_price,
                    ask_quantity,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tick["instrument_key"],
                    tick["exchange_timestamp_ms"],
                    tick["received_timestamp_ms"],
                    tick["ltp"],
                    tick.get("last_quantity"),
                    tick.get("average_price"),
                    tick.get("volume"),
                    tick.get("open_interest"),
                    tick.get("bid_price"),
                    tick.get("bid_quantity"),
                    tick.get("ask_price"),
                    tick.get("ask_quantity"),
                    json.dumps(tick.get("raw", {}), separators=(",", ":")),
                ),
            )
            self._connection.commit()

    def upsert_candle(self, candle: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO candles_1m (
                    instrument_key,
                    candle_start_ms,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    open_interest,
                    tick_count,
                    is_closed,
                    updated_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_key, candle_start_ms)
                DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    open_interest = excluded.open_interest,
                    tick_count = excluded.tick_count,
                    is_closed = excluded.is_closed,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    candle["instrument_key"],
                    candle["candle_start_ms"],
                    candle["open"],
                    candle["high"],
                    candle["low"],
                    candle["close"],
                    candle.get("volume", 0),
                    candle.get("open_interest"),
                    candle.get("tick_count", 0),
                    1 if candle.get("is_closed") else 0,
                    candle["updated_at_ms"],
                ),
            )
            self._connection.commit()

    def mark_candle_closed(
        self,
        instrument_key: str,
        candle_start_ms: int,
        updated_at_ms: int,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE candles_1m
                SET is_closed = 1, updated_at_ms = ?
                WHERE instrument_key = ? AND candle_start_ms = ?
                """,
                (updated_at_ms, instrument_key, candle_start_ms),
            )
            self._connection.commit()

    def latest_ticks(
        self,
        limit: int = 100,
        instrument_key: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 2000))
        with self._lock:
            if instrument_key:
                rows = self._connection.execute(
                    """
                    SELECT
                        instrument_key,
                        exchange_timestamp_ms,
                        received_timestamp_ms,
                        ltp,
                        last_quantity,
                        average_price,
                        volume,
                        open_interest,
                        bid_price,
                        bid_quantity,
                        ask_price,
                        ask_quantity
                    FROM ticks
                    WHERE instrument_key = ?
                    ORDER BY exchange_timestamp_ms DESC, id DESC
                    LIMIT ?
                    """,
                    (instrument_key, safe_limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT
                        instrument_key,
                        exchange_timestamp_ms,
                        received_timestamp_ms,
                        ltp,
                        last_quantity,
                        average_price,
                        volume,
                        open_interest,
                        bid_price,
                        bid_quantity,
                        ask_price,
                        ask_quantity
                    FROM ticks
                    ORDER BY exchange_timestamp_ms DESC, id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def latest_candles(
        self,
        instrument_key: str | None = None,
        limit: int = 120,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 2000))

        with self._lock:
            if instrument_key:
                rows = self._connection.execute(
                    """
                    SELECT *
                    FROM candles_1m
                    WHERE instrument_key = ?
                    ORDER BY candle_start_ms DESC
                    LIMIT ?
                    """,
                    (instrument_key, safe_limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT *
                    FROM candles_1m
                    ORDER BY candle_start_ms DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()

        return list(reversed([dict(row) for row in rows]))

    def instruments(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT instrument_key
                FROM (
                    SELECT DISTINCT instrument_key FROM ticks
                    UNION
                    SELECT DISTINCT instrument_key FROM candles_1m
                )
                ORDER BY instrument_key
                """
            ).fetchall()
        return [str(row["instrument_key"]) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            ticks = self._connection.execute(
                "SELECT COUNT(*) AS count FROM ticks"
            ).fetchone()["count"]
            candles = self._connection.execute(
                "SELECT COUNT(*) AS count FROM candles_1m"
            ).fetchone()["count"]

        return {"ticks": int(ticks), "candles": int(candles)}

    def insert_strategy_trade(self, trade: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO strategy_trades (
                    trade_id, side, symbol, instrument_key, strike,
                    lot_size, entry_time_ist, entry_price, entry_timestamp_ms,
                    exit_time_ist, exit_price, exit_timestamp_ms, exit_reason,
                    pnl_points, net_pnl, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade["trade_id"],
                    trade["side"],
                    trade["symbol"],
                    trade["instrument_key"],
                    trade["strike"],
                    trade.get("lot_size", 65),
                    trade["entry_time_ist"],
                    trade["entry_price"],
                    trade["entry_timestamp_ms"],
                    trade.get("exit_time_ist"),
                    trade.get("exit_price"),
                    trade.get("exit_timestamp_ms"),
                    trade.get("exit_reason"),
                    trade.get("pnl_points"),
                    trade.get("net_pnl"),
                    trade["status"],
                ),
            )
            self._connection.commit()

    def update_strategy_trade(self, trade: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE strategy_trades
                SET exit_time_ist = ?,
                    exit_price = ?,
                    exit_timestamp_ms = ?,
                    exit_reason = ?,
                    pnl_points = ?,
                    net_pnl = ?,
                    status = ?
                WHERE trade_id = ?
                """,
                (
                    trade.get("exit_time_ist"),
                    trade.get("exit_price"),
                    trade.get("exit_timestamp_ms"),
                    trade.get("exit_reason"),
                    trade.get("pnl_points"),
                    trade.get("net_pnl"),
                    trade["status"],
                    trade["trade_id"],
                ),
            )
            self._connection.commit()

    def get_strategy_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM strategy_trades
                ORDER BY entry_timestamp_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_strategy_signal(self, sig: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO strategy_signals (
                    timestamp_ist, timestamp_ms, side, strike,
                    contract_key, ltp, prev_ltp, qty_delta,
                    val_lakhs, day_high, prev_close, action_taken
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig["timestamp_ist"],
                    sig["timestamp_ms"],
                    sig["side"],
                    sig["strike"],
                    sig["contract_key"],
                    sig["ltp"],
                    sig.get("prev_ltp"),
                    sig.get("qty_delta"),
                    sig.get("val_lakhs"),
                    sig.get("day_high"),
                    sig.get("prev_close"),
                    sig["action_taken"],
                ),
            )
            self._connection.commit()

    def get_strategy_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM strategy_signals
                ORDER BY timestamp_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()