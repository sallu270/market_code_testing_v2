from __future__ import annotations

import threading
import time
from typing import Any, Callable

from database import MarketDatabase

CandleCallback = Callable[[dict[str, Any]], None]

class OneMinuteCandleBuilder:
    MINUTE_MS = 60_000

    def __init__(
        self,
        database: MarketDatabase,
        on_candle: CandleCallback | None = None,
    ) -> None:
        self.database = database
        self.on_candle = on_candle
        self._active: dict[str, dict[str, Any]] = {}
        self._last_cumulative_volume: dict[str, float] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _minute_start(timestamp_ms: int) -> int:
        return timestamp_ms - (timestamp_ms % OneMinuteCandleBuilder.MINUTE_MS)

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _volume_increment(self, instrument_key: str, tick: dict[str, Any]) -> float:
        current_volume = self._safe_float(tick.get("volume"))
        if current_volume is None:
            return 0.0

        previous_volume = self._last_cumulative_volume.get(instrument_key)
        self._last_cumulative_volume[instrument_key] = current_volume

        if previous_volume is None or current_volume < previous_volume:
            return 0.0

        return max(0.0, current_volume - previous_volume)

    def process_tick(self, tick: dict[str, Any]) -> dict[str, Any]:
        instrument_key = str(tick["instrument_key"])
        timestamp_ms = int(tick["exchange_timestamp_ms"])
        minute_start = self._minute_start(timestamp_ms)
        price = float(tick["ltp"])
        volume_increment = self._volume_increment(instrument_key, tick)
        open_interest = self._safe_float(tick.get("open_interest"))
        now_ms = int(time.time() * 1000)

        with self._lock:
            current = self._active.get(instrument_key)

            if current is None:
                current = self._new_candle(
                    instrument_key,
                    minute_start,
                    price,
                    volume_increment,
                    open_interest,
                    now_ms,
                )
                self._active[instrument_key] = current

            elif minute_start > current["candle_start_ms"]:
                current["is_closed"] = True
                current["updated_at_ms"] = now_ms
                self.database.upsert_candle(current)
                self._publish(current)

                current = self._new_candle(
                    instrument_key,
                    minute_start,
                    price,
                    volume_increment,
                    open_interest,
                    now_ms,
                )
                self._active[instrument_key] = current

            elif minute_start < current["candle_start_ms"]:
                late_candle = self._new_candle(
                    instrument_key,
                    minute_start,
                    price,
                    volume_increment,
                    open_interest,
                    now_ms,
                )
                self.database.upsert_candle(late_candle)
                return dict(current)

            else:
                current["high"] = max(float(current["high"]), price)
                current["low"] = min(float(current["low"]), price)
                current["close"] = price
                current["volume"] = float(current["volume"]) + volume_increment
                current["open_interest"] = open_interest
                current["tick_count"] = int(current["tick_count"]) + 1
                current["updated_at_ms"] = now_ms

            self.database.upsert_candle(current)
            self._publish(current)
            return dict(current)

    def close_stale_candles(self, now_ms: int | None = None) -> None:
        current_time_ms = now_ms or int(time.time() * 1000)
        # 5-second grace window to absorb network jitter before clearing from active memory
        stale_threshold = self._minute_start(current_time_ms - 5000)

        with self._lock:
            for instrument_key, candle in list(self._active.items()):
                if candle["candle_start_ms"] < stale_threshold:
                    candle["is_closed"] = True
                    candle["updated_at_ms"] = current_time_ms
                    self.database.upsert_candle(candle)
                    self._publish(candle)
                    del self._active[instrument_key]

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(candle) for candle in self._active.values()]

    @staticmethod
    def _new_candle(
        instrument_key: str,
        minute_start: int,
        price: float,
        volume: float,
        open_interest: float | None,
        now_ms: int,
    ) -> dict[str, Any]:
        return {
            "instrument_key": instrument_key,
            "candle_start_ms": minute_start,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
            "open_interest": open_interest,
            "tick_count": 1,
            "is_closed": False,
            "updated_at_ms": now_ms,
        }

    def _publish(self, candle: dict[str, Any]) -> None:
        if self.on_candle:
            self.on_candle(dict(candle))