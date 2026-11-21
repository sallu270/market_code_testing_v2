from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import upstox_client

from config import Settings

LOGGER = logging.getLogger("upstox_feed")

TickCallback = Callable[[dict[str, Any]], None]
StatusCallback = Callable[[dict[str, Any]], None]

IST = ZoneInfo("Asia/Kolkata")


class UpstoxFeed:
    """
    Manages a single live Upstox V3 Market Data WebSocket feed.
    Handles connecting, subscribing, protobuf parsing, tick dispatching,
    and automatic exponential backoff reconnection.
    """

    def __init__(
        self,
        settings: Settings,
        on_tick: TickCallback,
        on_status: StatusCallback,
        name: str = "market-feed",
        initial_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.settings = settings
        self.on_tick = on_tick
        self.on_status = on_status
        self.name = name
        self._stop_event = threading.Event()
        self._subscribed_keys: set[str] = (
            set(initial_keys)
            if initial_keys is not None
            else set(self.settings.instrument_keys)
        )
        self._thread: threading.Thread | None = None
        self._streamer: Any = None
        self._connected = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def subscribed_keys(self) -> set[str]:
        return set(self._subscribed_keys)

    def start(self) -> None:
        if self.running:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            name=f"upstox-{self.name}",
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

        self._emit_status("stopped", f"[{self.name}] Market feed stopped.")

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
                err_str = str(exc)
                self._emit_status("error", f"[{self.name}] {type(exc).__name__}: {exc}")
                if "403" in err_str or "Forbidden" in err_str:
                    delay = max(delay, 10.0)
                    LOGGER.warning("[%s] 403 Forbidden detected. Applying 10s cooldown backoff before reconnect.", self.name)

            if self._stop_event.is_set():
                break

            sleep_seconds = min(
                delay + random.uniform(0, 1),
                self.settings.reconnect_max_seconds,
            )
            self._emit_status(
                "reconnecting",
                f"[{self.name}] Reconnecting in {sleep_seconds:.1f} seconds.",
            )
            self._stop_event.wait(sleep_seconds)
            delay = min(delay * 2, self.settings.reconnect_max_seconds)

    def _connect_once(self) -> None:
        if not self.settings.access_token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is missing.")

        if not self._subscribed_keys:
            LOGGER.warning("[%s] No instrument keys to subscribe to. Waiting...", self.name)
            self._stop_event.wait(2.0)
            return

        # Upstox strictly limits WebSocket V3 to 100 instruments per connection.
        # With combined indices + stocks feed, we allow up to 115 keys safely.
        # Enforce hard safety cap to prevent connection drops.
        if len(self._subscribed_keys) > 150:
            LOGGER.warning("[%s] Instrument count %d exceeds safety cap (150). Pruning non-essential keys.", self.name, len(self._subscribed_keys))
            keys_list = list(self._subscribed_keys)[:150]
            self._subscribed_keys = set(keys_list)
        else:
            keys_list = list(self._subscribed_keys)

        configuration = upstox_client.Configuration()
        configuration.access_token = self.settings.access_token
        api_client = upstox_client.ApiClient(configuration)

        self._streamer = upstox_client.MarketDataStreamerV3(
            api_client,
            keys_list,
            self.settings.feed_mode,
        )

        self._streamer.on("open", self._on_open)
        self._streamer.on("message", self._on_message)
        self._streamer.on("error", self._on_error)
        self._streamer.on("close", self._on_close)

        self._emit_status("connecting", f"[{self.name}] Connecting to Upstox Market Data Feed V3 ({len(keys_list)} keys).")
        self._streamer.connect()

    def subscribe(
        self,
        instrument_keys: list[str],
        mode: str | None = None,
        target: str | None = None,
    ) -> None:
        """Dynamically subscribe additional instrument keys to the live feed."""
        if not instrument_keys:
            return
        to_add = [k for k in instrument_keys if k not in self._subscribed_keys]
        if not to_add:
            return

        # Enforce 150 instrument hard ceiling on active keys for combined single-feed mode
        max_allowed = 150
        if len(self._subscribed_keys) + len(to_add) > max_allowed:
            protected = set(getattr(self.settings, "instrument_keys", []))
            prunable = [k for k in self._subscribed_keys if k not in protected]
            slots_needed = (len(self._subscribed_keys) + len(to_add)) - max_allowed
            for pk in prunable[:slots_needed]:
                self._subscribed_keys.discard(pk)
            available_slots = max(0, max_allowed - len(self._subscribed_keys))
            if len(to_add) > available_slots:
                LOGGER.warning("[%s] Requested %d keys exceeds remaining WebSocket capacity (%d). Truncating batch.", self.name, len(to_add), available_slots)
                to_add = to_add[:available_slots]

        if not to_add:
            return

        self._subscribed_keys.update(to_add)
        if self._streamer and self._connected:
            try:
                self._streamer.subscribe(to_add, mode or self.settings.feed_mode)
                LOGGER.info("[%s] Dynamically subscribed to %d instruments: %s", self.name, len(to_add), to_add)
            except Exception as exc:
                LOGGER.error("[%s] Failed to dynamically subscribe: %s", self.name, exc)

    def unsubscribe(self, instrument_keys: list[str]) -> None:
        """Dynamically unsubscribe instrument keys from the live feed to release WebSocket capacity."""
        if not instrument_keys:
            return
        to_remove = [k for k in instrument_keys if k in self._subscribed_keys]
        if not to_remove:
            return
        for k in to_remove:
            self._subscribed_keys.discard(k)
        if self._streamer and self._connected:
            try:
                if hasattr(self._streamer, "unsubscribe"):
                    self._streamer.unsubscribe(to_remove)
                LOGGER.info("[%s] Dynamically unsubscribed from %d instruments: %s", self.name, len(to_remove), to_remove)
            except Exception as exc:
                LOGGER.debug("[%s] Could not dynamically unsubscribe: %s", self.name, exc)

    def _on_open(self, *args: Any) -> None:
        self._connected = True
        self._emit_status(
            "connected",
            f"[{self.name}] Subscribed to {len(self._subscribed_keys)} instrument(s).",
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
                f"[{self.name}] Could not process one feed message: {type(exc).__name__}: {exc}",
            )

    def _on_error(self, error: Any, *args: Any) -> None:
        self._connected = False
        self._emit_status("error", f"[{self.name}] {error}")

    def _on_close(self, *args: Any) -> None:
        self._connected = False
        if not self._stop_event.is_set():
            self._emit_status("disconnected", f"[{self.name}] Upstox market feed disconnected.")

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

        server_timestamp_ms = self._first_integer(payload, "currentTs", "timestamp")

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
                "server_timestamp_ms": server_timestamp_ms,
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
                    market_ff,
                    "vtt",
                    "volumeTradedToday",
                ) or self._first_number(
                    extended,
                    "vtt",
                    "volumeTradedToday",
                ),
                "open_interest": self._first_number(
                    market_ff,
                    "oi",
                    "openInterest",
                ) or self._first_number(
                    extended,
                    "oi",
                    "openInterest",
                ),
                "prev_close": self._first_number(
                    ltpc,
                    "cp",
                    "closePrice",
                    "prevClose",
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
                "name": self.name,
                "message": message,
                "connected": self._connected,
                "timestamp_ms": int(time.time() * 1000),
            }
        )


class DualUpstoxFeed:
    """
    Manages two independent, concurrent Upstox V3 Market Data WebSockets
    using a single access token:
      1. indices-feed: Major Indices (NIFTY 50, BANK NIFTY, SENSEX), their futures,
         and locked index option contracts (~30-40 instruments).
      2. stocks-feed: All 50 Nifty constituent cash stocks (NSE_EQ|...) and active
         stock option contracts (~50-90 instruments).

    Keeps both sockets well below the ~100-120 Upstox instrument connection threshold,
    eliminating [WinError 10054] connection drops and socket starvation.
    """

    def __init__(
        self,
        settings: Settings,
        on_tick: TickCallback,
        on_status: StatusCallback,
        index_keys: list[str] | set[str] | tuple[str, ...] | None = None,
        stock_keys: list[str] | set[str] | tuple[str, ...] | None = None,
        classifier: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.on_tick = on_tick
        self.on_status = on_status
        self.classifier = classifier

        idx_keys = list(index_keys) if index_keys is not None else list(self.settings.instrument_keys)
        stk_keys = list(stock_keys) if stock_keys is not None else []

        self._index_keys: set[str] = set(idx_keys)
        self._stock_keys: set[str] = set(stk_keys)

        self.indices_feed = UpstoxFeed(
            settings=settings,
            on_tick=self._on_child_tick,
            on_status=lambda s: self._on_child_status("indices", s),
            name="indices-feed",
            initial_keys=idx_keys,
        )

        self.stocks_feed = UpstoxFeed(
            settings=settings,
            on_tick=self._on_child_tick,
            on_status=lambda s: self._on_child_status("stocks", s),
            name="stocks-feed",
            initial_keys=stk_keys,
        )

        self._child_statuses: dict[str, dict[str, Any]] = {
            "indices": {"state": "init", "connected": False, "message": "Initialized"},
            "stocks": {"state": "init", "connected": False, "message": "Initialized"},
        }

    @property
    def running(self) -> bool:
        return self.indices_feed.running or self.stocks_feed.running

    @property
    def connected(self) -> bool:
        """Returns True if at least one feed is connected."""
        return self.indices_feed.connected or self.stocks_feed.connected

    @property
    def both_connected(self) -> bool:
        """Returns True if both index and stock feeds are simultaneously connected."""
        return self.indices_feed.connected and self.stocks_feed.connected

    @property
    def subscribed_keys(self) -> set[str]:
        return self.indices_feed.subscribed_keys.union(self.stocks_feed.subscribed_keys)

    def set_classifier(self, classifier: Callable[[str], str]) -> None:
        self.classifier = classifier

    def start(self) -> None:
        LOGGER.info(
            "Starting DualUpstoxFeed: indices-feed (%d keys), stocks-feed (%d keys)",
            len(self.indices_feed.subscribed_keys),
            len(self.stocks_feed.subscribed_keys),
        )
        self.indices_feed.start()
        self.stocks_feed.start()

    def stop(self) -> None:
        LOGGER.info("Stopping DualUpstoxFeed...")
        self.indices_feed.stop()
        self.stocks_feed.stop()

    def _on_child_tick(self, tick: dict[str, Any]) -> None:
        self.on_tick(tick)

    def _on_child_status(self, child_name: str, status: dict[str, Any]) -> None:
        self._child_statuses[child_name] = status
        agg_connected = self.indices_feed.connected and self.stocks_feed.connected
        any_connected = self.indices_feed.connected or self.stocks_feed.connected
        agg_state = "connected" if agg_connected else ("partial" if any_connected else status.get("state", "disconnected"))

        composite_status = {
            "state": agg_state,
            "child_feed": child_name,
            "message": f"[{child_name}] {status.get('message', '')}",
            "connected": any_connected,
            "both_connected": agg_connected,
            "indices_connected": self.indices_feed.connected,
            "stocks_connected": self.stocks_feed.connected,
            "indices_count": len(self.indices_feed.subscribed_keys),
            "stocks_count": len(self.stocks_feed.subscribed_keys),
            "total_subscribed": len(self.subscribed_keys),
            "timestamp_ms": int(time.time() * 1000),
        }
        self.on_status(composite_status)

    def classify_key(self, key: str) -> str:
        """
        Classifies key into 'indices' or 'stocks' dynamically.
        Uses exchange prefixes, ISIN checks, known sets, and optional classifier callback.
        """
        key_str = str(key).strip()
        if not key_str:
            return "indices"

        # 1. Unambiguous exchange segment prefixes
        if key_str.startswith("NSE_INDEX|") or key_str.startswith("BSE_INDEX|") or "INDEX" in key_str.upper():
            return "indices"

        if key_str.startswith("NSE_EQ|") or key_str.startswith("BSE_EQ|") or "|INE" in key_str.upper():
            return "stocks"

        # 2. Known key sets
        if key_str in self._index_keys or key_str in self.indices_feed.subscribed_keys:
            return "indices"
        if key_str in self._stock_keys or key_str in self.stocks_feed.subscribed_keys:
            return "stocks"

        # 3. External classifier callback (e.g., from strategy_engine)
        if self.classifier is not None:
            try:
                target = self.classifier(key_str)
                if target in ("indices", "stocks"):
                    return target
            except Exception as e:
                LOGGER.debug("Classifier callback error for key %s: %s", key_str, e)

        return "indices"

    def subscribe(
        self,
        instrument_keys: list[str],
        mode: str | None = None,
        target: str | None = None,
    ) -> None:
        """
        Routes subscription requests to the appropriate child feed.
        If target is explicitly 'indices' or 'stocks', routes directly.
        Otherwise, automatically classifies each key based on instrument metadata.
        """
        if not instrument_keys:
            return

        idx_batch: list[str] = []
        stk_batch: list[str] = []

        for k in instrument_keys:
            k_str = str(k).strip()
            if not k_str:
                continue
            dest = target or self.classify_key(k_str)
            if dest == "stocks":
                stk_batch.append(k_str)
                self._stock_keys.add(k_str)
            else:
                idx_batch.append(k_str)
                self._index_keys.add(k_str)

        if idx_batch:
            self.indices_feed.subscribe(idx_batch, mode=mode)
        if stk_batch:
            self.stocks_feed.subscribe(stk_batch, mode=mode)

    def unsubscribe(self, instrument_keys: list[str]) -> None:
        """Routes unsubscribe requests to the appropriate child feed."""
        if not instrument_keys:
            return
        idx_batch: list[str] = []
        stk_batch: list[str] = []
        for k in instrument_keys:
            k_str = str(k).strip()
            if not k_str:
                continue
            if k_str in self.stocks_feed.subscribed_keys:
                stk_batch.append(k_str)
                self._stock_keys.discard(k_str)
            else:
                idx_batch.append(k_str)
                self._index_keys.discard(k_str)

        if idx_batch:
            self.indices_feed.unsubscribe(idx_batch)
        if stk_batch:
            self.stocks_feed.unsubscribe(stk_batch)
