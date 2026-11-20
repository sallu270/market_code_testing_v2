from __future__ import annotations

import json
import sqlite3
import threading
import time
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
        self._tick_count: int = 0
        self._last_commit_time: float = time.time()
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
                    trade_date TEXT,
                    side TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    underlying_key TEXT,
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
                    status TEXT NOT NULL,
                    spot_price REAL,
                    atm_strike REAL,
                    bot_latency_ms REAL,
                    network_latency_ms REAL,
                    build_up TEXT
                );

                CREATE TABLE IF NOT EXISTS strategy_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ist TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    instrument_name TEXT,
                    side TEXT NOT NULL,
                    strike REAL NOT NULL,
                    contract_key TEXT NOT NULL,
                    ltp REAL NOT NULL,
                    prev_ltp REAL,
                    qty_delta REAL,
                    val_lakhs REAL,
                    day_high REAL,
                    prev_close REAL,
                    action_taken TEXT NOT NULL,
                    bid_price REAL,
                    ask_price REAL,
                    spread_pct REAL,
                    oi_change REAL,
                    confidence TEXT
                );

                CREATE TABLE IF NOT EXISTS strategy_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_date TEXT NOT NULL,
                    timestamp_ist TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    message TEXT NOT NULL,
                    details_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_date_time
                    ON strategy_alerts(alert_date, timestamp_ms DESC);
                """
            )
            self._connection.commit()

            # --- Live migration: add new columns to existing tables if missing ---
            self._add_column_if_missing("strategy_trades", "trade_date", "TEXT")
            self._add_column_if_missing("strategy_trades", "underlying_key", "TEXT")
            self._add_column_if_missing("strategy_trades", "spot_price", "REAL")
            self._add_column_if_missing("strategy_trades", "atm_strike", "REAL")
            self._add_column_if_missing("strategy_signals", "instrument_name", "TEXT")
            self._add_column_if_missing("strategy_signals", "bid_price", "REAL")
            self._add_column_if_missing("strategy_signals", "ask_price", "REAL")
            self._add_column_if_missing("strategy_signals", "spread_pct", "REAL")
            self._add_column_if_missing("strategy_signals", "oi_change", "REAL")
            self._add_column_if_missing("strategy_signals", "confidence", "TEXT")

            # Backfill trade_date and underlying_key for any existing trades
            try:
                self._connection.execute(
                    """
                    UPDATE strategy_trades
                    SET trade_date = strftime('%Y-%m-%d', datetime(entry_timestamp_ms / 1000, 'unixepoch', '+330 minutes'))
                    WHERE trade_date IS NULL AND entry_timestamp_ms IS NOT NULL;
                    """
                )
                self._connection.execute(
                    """
                    UPDATE strategy_trades
                    SET underlying_key = CASE
                        WHEN symbol LIKE '%BANKNIFTY%' THEN 'NSE_INDEX|Nifty Bank'
                        WHEN symbol LIKE '%SENSEX%' THEN 'BSE_INDEX|SENSEX'
                        ELSE 'NSE_INDEX|Nifty 50'
                    END
                    WHERE underlying_key IS NULL;
                    """
                )
                self._connection.execute(
                    """
                    UPDATE strategy_signals
                    SET instrument_name = CASE
                        WHEN strike >= 70000 THEN 'SENSEX'
                        WHEN strike >= 45000 THEN 'BANKNIFTY'
                        ELSE 'NIFTY'
                    END
                    WHERE instrument_name IS NULL;
                    """
                )
                self._connection.commit()
            except Exception:
                pass

    def _add_column_if_missing(self, table: str, column: str, col_type: str) -> None:
        """ALTER TABLE … ADD COLUMN when the column does not yet exist (idempotent)."""
        try:
            existing = {
                row[1]
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
                self._connection.commit()
        except Exception as exc:
            import logging
            logging.getLogger("database").warning(
                "Could not add column %s.%s: %s", table, column, exc
            )

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
            self._tick_count += 1
            if self._tick_count >= 50 or (time.time() - self._last_commit_time) > 2.0:
                self._connection.commit()
                self._tick_count = 0
                self._last_commit_time = time.time()

    def flush(self) -> None:
        """Commit any pending tick writes to disk."""
        with self._lock:
            self._connection.commit()
            self._tick_count = 0
            self._last_commit_time = time.time()

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
                    open = CASE 
                        WHEN candles_1m.tick_count > 1 AND excluded.tick_count <= 2 THEN candles_1m.open 
                        ELSE excluded.open 
                    END,
                    high = MAX(candles_1m.high, excluded.high),
                    low = MIN(candles_1m.low, excluded.low),
                    close = CASE 
                        WHEN candles_1m.is_closed = 1 AND excluded.is_closed = 0 THEN candles_1m.close 
                        ELSE excluded.close 
                    END,
                    volume = MAX(candles_1m.volume, excluded.volume),
                    open_interest = COALESCE(excluded.open_interest, candles_1m.open_interest),
                    tick_count = CASE 
                        WHEN excluded.tick_count > candles_1m.tick_count THEN excluded.tick_count 
                        ELSE candles_1m.tick_count + excluded.tick_count 
                    END,
                    is_closed = MAX(candles_1m.is_closed, excluded.is_closed),
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

    def get_candle(self, instrument_key: str, candle_start_ms: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT *
                FROM candles_1m
                WHERE instrument_key = ? AND candle_start_ms = ?
                """,
                (instrument_key, candle_start_ms),
            ).fetchone()
            return dict(row) if row else None

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
                SELECT DISTINCT instrument_key
                FROM candles_1m
                ORDER BY instrument_key
                """
            ).fetchall()
        return [str(row["instrument_key"]) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            # Instant indexed rowid lookup instead of multi-gigabyte full-table scan
            try:
                row_t = self._connection.execute("SELECT rowid FROM ticks ORDER BY rowid DESC LIMIT 1").fetchone()
                ticks_cnt = int(row_t["rowid"]) if row_t and row_t["rowid"] else 0
            except Exception:
                ticks_cnt = 0
            try:
                row_c = self._connection.execute("SELECT rowid FROM candles_1m ORDER BY rowid DESC LIMIT 1").fetchone()
                candles_cnt = int(row_c["rowid"]) if row_c and row_c["rowid"] else 0
            except Exception:
                candles_cnt = 0

        return {"ticks": ticks_cnt, "candles": candles_cnt}

    def insert_strategy_trade(self, trade: dict[str, Any]) -> None:
        with self._lock:
            # Ensure migration columns exist
            for col_name, col_type in [
                ("bot_latency_ms", "REAL"),
                ("network_latency_ms", "REAL"),
                ("build_up", "TEXT"),
            ]:
                try:
                    self._connection.execute(f"ALTER TABLE strategy_trades ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

            self._connection.execute(
                """
                INSERT OR REPLACE INTO strategy_trades (
                    trade_id, trade_date, side, symbol, instrument_key, underlying_key,
                    strike, lot_size, entry_time_ist, entry_price, entry_timestamp_ms,
                    exit_time_ist, exit_price, exit_timestamp_ms, exit_reason,
                    pnl_points, net_pnl, status, spot_price, atm_strike,
                    bot_latency_ms, network_latency_ms, build_up
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade["trade_id"],
                    trade.get("trade_date"),
                    trade["side"],
                    trade["symbol"],
                    trade["instrument_key"],
                    trade.get("underlying_key"),
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
                    trade.get("spot_price"),
                    trade.get("atm_strike"),
                    trade.get("bot_latency_ms"),
                    trade.get("network_latency_ms"),
                    trade.get("build_up"),
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

    def update_strategy_trade_exit(
        self,
        trade_id: str,
        exit_time_ist: str,
        exit_price: float,
        exit_timestamp_ms: int,
        exit_reason: str,
        pnl_points: float,
        net_pnl: float,
    ) -> None:
        self.update_strategy_trade({
            "trade_id": trade_id,
            "exit_time_ist": exit_time_ist,
            "exit_price": exit_price,
            "exit_timestamp_ms": exit_timestamp_ms,
            "exit_reason": exit_reason,
            "pnl_points": pnl_points,
            "net_pnl": net_pnl,
            "status": "CLOSED",
        })

    def get_strategy_trades(self, limit: int = 100, trade_date: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if trade_date and trade_date.lower() != "all":
                rows = self._connection.execute(
                    """
                    SELECT * FROM strategy_trades
                    WHERE trade_date = ?
                    ORDER BY entry_timestamp_ms DESC
                    LIMIT ?
                    """,
                    (trade_date, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM strategy_trades
                    ORDER BY entry_timestamp_ms DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_strategy_trade_dates(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT DISTINCT trade_date FROM strategy_trades
                WHERE trade_date IS NOT NULL AND trade_date != ''
                ORDER BY trade_date DESC
                """
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    def insert_strategy_signal(self, sig: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO strategy_signals (
                    timestamp_ist, timestamp_ms, instrument_name, side, strike,
                    contract_key, ltp, prev_ltp, qty_delta,
                    val_lakhs, day_high, prev_close, action_taken,
                    bid_price, ask_price, spread_pct, oi_change, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig["timestamp_ist"],
                    sig["timestamp_ms"],
                    sig.get("instrument_name"),
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
                    sig.get("bid_price"),
                    sig.get("ask_price"),
                    sig.get("spread_pct"),
                    sig.get("oi_change"),
                    sig.get("confidence"),
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

    def insert_strategy_alert(self, alert: dict[str, Any]) -> int:
        with self._lock:
            cur = self._connection.execute(
                """
                INSERT INTO strategy_alerts (
                    alert_date, timestamp_ist, timestamp_ms, level,
                    alert_type, symbol, message, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert["alert_date"],
                    alert["timestamp_ist"],
                    alert["timestamp_ms"],
                    alert.get("level", "INFO"),
                    alert.get("alert_type", "GENERAL"),
                    alert.get("symbol", ""),
                    alert["message"],
                    alert.get("details_json", "{}"),
                ),
            )
            self._connection.commit()
            return int(cur.lastrowid or 0)

    def get_strategy_alerts(
        self,
        limit: int = 500,
        alert_date: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if alert_date:
                clauses.append("alert_date = ?")
                params.append(alert_date)
            if level and level.upper() not in ("ALL", ""):
                if level.upper() in ("ENTRY", "EXIT"):
                    clauses.append("alert_type LIKE ?")
                    params.append(f"%{level.upper()}%")
                else:
                    clauses.append("(level = ? OR alert_type LIKE ?)")
                    params.extend([level.upper(), f"%{level.upper()}%"])

            where_str = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            query = f"""
                SELECT * FROM strategy_alerts
                {where_str}
                ORDER BY timestamp_ms DESC
                LIMIT ?
            """
            rows = self._connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_strategy_alert_dates(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT DISTINCT alert_date FROM strategy_alerts
                WHERE alert_date IS NOT NULL AND alert_date != ''
                ORDER BY alert_date DESC
                """
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    def get_db_stats(self) -> dict[str, Any]:
        """Returns database size, row counts, and disk statistics."""
        with self._lock:
            size_bytes = self.path.stat().st_size if self.path.exists() else 0
            size_mb = round(size_bytes / (1024 * 1024), 2)
            size_gb = round(size_bytes / (1024 * 1024 * 1024), 2)

            tick_count = self._connection.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
            candle_count = self._connection.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0]
            trade_count = self._connection.execute("SELECT COUNT(*) FROM strategy_trades").fetchone()[0]

            oldest_tick = self._connection.execute("SELECT MIN(exchange_timestamp_ms) FROM ticks").fetchone()[0]
            newest_tick = self._connection.execute("SELECT MAX(exchange_timestamp_ms) FROM ticks").fetchone()[0]

            return {
                "file_path": str(self.path),
                "size_bytes": size_bytes,
                "size_mb": size_mb,
                "size_gb": size_gb,
                "tick_count": tick_count,
                "candle_count": candle_count,
                "trade_count": trade_count,
                "oldest_tick_ms": oldest_tick,
                "newest_tick_ms": newest_tick,
            }

    def prune_old_ticks(self, days_to_keep: int = 3, run_vacuum: bool = False) -> dict[str, Any]:
        """
        Deletes raw ticks older than `days_to_keep` days to reclaim storage.
        Preserves all 1-minute candles and strategy trade records permanently!
        """
        import time
        with self._lock:
            cutoff_ms = int((time.time() - (days_to_keep * 86400)) * 1000)
            initial_size = self.path.stat().st_size if self.path.exists() else 0

            cur = self._connection.cursor()
            cur.execute("DELETE FROM ticks WHERE exchange_timestamp_ms < ?", (cutoff_ms,))
            deleted_ticks = cur.rowcount
            self._connection.commit()

            # Checkpoint WAL file to release pages
            try:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self._connection.commit()
            except Exception:
                pass

            if run_vacuum:
                try:
                    self._connection.execute("VACUUM;")
                    self._connection.commit()
                except Exception:
                    pass

            final_size = self.path.stat().st_size if self.path.exists() else 0
            return {
                "deleted_ticks": deleted_ticks,
                "cutoff_ms": cutoff_ms,
                "initial_size_mb": round(initial_size / (1024 * 1024), 2),
                "final_size_mb": round(final_size / (1024 * 1024), 2),
                "space_saved_mb": round(max(0, initial_size - final_size) / (1024 * 1024), 2),
            }

    def close(self) -> None:
        with self._lock:
            self._connection.commit()
            self._connection.close()