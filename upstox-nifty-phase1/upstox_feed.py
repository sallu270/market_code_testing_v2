from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import upstox_client

from config import Settings

TickCallback = Callable[[dict[str, Any]], None]
StatusCallback = Callable[[dict[str, Any]], None]

IST = ZoneInfo("Asia/Kolkata")

class UpstoxFeed:
    def __init__(
        self,
        settings: Settings,
        on_tick: TickCallback,
        on_status: StatusCallback,
    ) -> None:
        self.settings = settings
        self.on_tick = on_tick
        self.on_status = on_status
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._streamer: Any = None
        self._connected = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self.running:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            name="upstox-market-feed",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._connected = False

        if self._streamer is not None:
            try:
                self._streamer.disconnect()
            except Exception:
                pass

        self._emit_status("stopped", "Market feed stopped.")

    def _run_forever(self) -> None:
        delay = self.settings.reconnect_min_seconds

        while not self._stop_event.is_set():
            try:
                self._connect_once()
                delay = self.settings.reconnect_min_seconds

                while not self._stop_event.wait(1):
                    if not self._connected:
                        break

            except Exception as exc:
                self._connected = False
                self._emit_status("error", f"{type(exc).__name__}: {exc}")

            if self._stop_event.is_set():
                break

            sleep_seconds = min(
                delay + random.uniform(0, 1),
                self.settings.reconnect_max_seconds,
            )
            self._emit_status(
                "reconnecting",
                f"Reconnecting in {sleep_seconds:.1f} seconds.",
            )
            self._stop_event.wait(sleep_seconds)
            delay = min(delay * 2, self.settings.reconnect_max_seconds)

    def _connect_once(self) -> None:
        if not self.settings.access_token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is missing.")

        if not self.settings.instrument_keys:
            raise RuntimeError("UPSTOX_INSTRUMENT_KEYS is missing.")

        configuration = upstox_client.Configuration()
        configuration.access_token = self.settings.access_token
        api_client = upstox_client.ApiClient(configuration)

        self._streamer = upstox_client.MarketDataStreamerV3(
            api_client,
            list(self.settings.instrument_keys),
            self.settings.feed_mode,
        )

        self._streamer.on("open", self._on_open)
        self._streamer.on("message", self._on_message)
        self._streamer.on("error", self._on_error)
        self._streamer.on("close", self._on_close)

        self._emit_status("connecting", "Connecting to Upstox Market Data Feed V3.")
        self._streamer.connect()

    def _on_open(self, *args: Any) -> None:
        self._connected = True
        self._emit_status(
            "connected",
            f"Subscribed to {len(self.settings.instrument_keys)} instrument(s).",
        )

    def _on_message(self, message: Any, *args: Any) -> None:
        try:
            payload = self._normalise_payload(message)
            received_timestamp_ms = int(time.time() * 1000)

            for tick in self._extract_ticks(payload, received_timestamp_ms):
                self.on_tick(tick)

        except Exception as exc:
            self._emit_status(
                "message_error",
                f"Could not process one feed message: {type(exc).__name__}: {exc}",
            )

    def _on_error(self, error: Any, *args: Any) -> None:
        self._connected = False
        self._emit_status("error", str(error))

    def _on_close(self, *args: Any) -> None:
        self._connected = False
        if not self._stop_event.is_set():
            self._emit_status("disconnected", "Upstox market feed disconnected.")

    @staticmethod
    def _normalise_payload(message: Any) -> dict[str, Any]:
        if isinstance(message, dict):
            return message

        if isinstance(message, bytes):
            message = message.decode("utf-8")

        if isinstance(message, str):
            decoded = json.loads(message)
            if not isinstance(decoded, dict):
                raise ValueError("Feed message is not a JSON object.")
            return decoded

        if hasattr(message, "to_dict"):
            decoded = message.to_dict()
            if isinstance(decoded, dict):
                return decoded

        raise TypeError(f"Unsupported feed message type: {type(message).__name__}")

    def _extract_ticks(
        self,
        payload: dict[str, Any],
        received_timestamp_ms: int,
    ) -> list[dict[str, Any]]:
        feeds = payload.get("feeds") or {}
        if not isinstance(feeds, dict):
            return []

        output: list[dict[str, Any]] = []

        for instrument_key, feed_wrapper in feeds.items():
            if not isinstance(feed_wrapper, dict):
                continue

            feed = feed_wrapper.get("fullFeed") or feed_wrapper
            ltpc = self._find_ltpc(feed)
            if not ltpc:
                continue

            ltp = self._first_number(ltpc, "ltp", "lastPrice")
            if ltp is None or ltp <= 0:
                continue

            timestamp_ms = self._first_integer(
                ltpc,
                "ltt",
                "lastTradeTime",
                "timestamp",
            )
            if timestamp_ms is None:
                timestamp_ms = received_timestamp_ms

            if timestamp_ms < 10_000_000_000:
                timestamp_ms *= 1000

            market_ff = feed.get("marketFF") or feed.get("marketFullFeed") or feed
            index_ff = feed.get("indexFF") or feed.get("indexFullFeed") or feed
            details = market_ff.get("marketLevel") or market_ff
            option_details = market_ff.get("optionGreeks") or {}
            extended = market_ff.get("eFeedDetails") or index_ff.get("eFeedDetails") or {}

            bid_price, bid_quantity, ask_price, ask_quantity = self._best_depth(
                details
            )

            tick = {
                "instrument_key": str(instrument_key),
                "exchange_timestamp_ms": timestamp_ms,
                "received_timestamp_ms": received_timestamp_ms,
                "exchange_time_ist": datetime.fromtimestamp(
                    timestamp_ms / 1000,
                    tz=IST,
                ).isoformat(),
                "ltp": ltp,
                "last_quantity": self._first_number(
                    ltpc,
                    "ltq",
                    "lastQuantity",
                ),
                "average_price": self._first_number(
                    extended,
                    "atp",
                    "averageTradedPrice",
                ),
                "volume": self._first_number(
                    extended,
                    "vtt",
                    "volumeTradedToday",
                ),
                "open_interest": self._first_number(
                    extended,
                    "oi",
                    "openInterest",
                ),
                "bid_price": bid_price,
                "bid_quantity": bid_quantity,
                "ask_price": ask_price,
                "ask_quantity": ask_quantity,
                "implied_volatility": self._first_number(
                    option_details,
                    "iv",
                    "impliedVolatility",
                ),
                "raw": payload,
            }
            output.append(tick)

        return output

    @staticmethod
    def _find_ltpc(feed: dict[str, Any]) -> dict[str, Any] | None:
        direct = feed.get("ltpc")
        if isinstance(direct, dict):
            return direct

        for key in (
            "marketFF",
            "marketFullFeed",
            "indexFF",
            "indexFullFeed",
            "firstLevelWithGreeks",
        ):
            nested = feed.get(key)
            if isinstance(nested, dict):
                ltpc = nested.get("ltpc")
                if isinstance(ltpc, dict):
                    return ltpc

        return None

    @staticmethod
    def _best_depth(
        details: dict[str, Any],
    ) -> tuple[float | None, float | None, float | None, float | None]:
        quotes = details.get("bidAskQuote") or details.get("quotes") or []
        if not isinstance(quotes, list) or not quotes:
            return None, None, None, None

        best = quotes[0]
        if not isinstance(best, dict):
            return None, None, None, None

        return (
            UpstoxFeed._first_number(best, "bidP", "bidPrice"),
            UpstoxFeed._first_number(best, "bidQ", "bidQuantity"),
            UpstoxFeed._first_number(best, "askP", "askPrice"),
            UpstoxFeed._first_number(best, "askQ", "askQuantity"),
        )

    @staticmethod
    def _first_number(
        values: dict[str, Any],
        *keys: str,
    ) -> float | None:
        for key in keys:
            value = values.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _first_integer(
        values: dict[str, Any],
        *keys: str,
    ) -> int | None:
        for key in keys:
            value = values.get(key)
            if value is None:
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return None

    def _emit_status(self, state: str, message: str) -> None:
        self.on_status(
            {
                "state": state,
                "message": message,
                "connected": self._connected,
                "timestamp_ms": int(time.time() * 1000),
            }
        )