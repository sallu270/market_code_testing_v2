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
        self._open_event = threading.Event()
        self._last_error_str: str = ""
        self.max_keys_per_socket: int = 1500

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

    def _cleanup_streamer(self) -> None:
        if self._streamer is not None:
            try:
                if hasattr(self._streamer, "auto_reconnect"):
                    self._streamer.auto_reconnect(False)
            except Exception:
                pass
            try:
                self._streamer.disconnect()
            except Exception:
                pass
            self._streamer = None

    def stop(self) -> None:
        self._stop_event.set()
        self._connected = False
        self._open_event.set()
        self._cleanup_streamer()
        self._emit_status("stopped", f"[{self.name}] Market feed stopped.")

    def _run_forever(self) -> None:
        delay = self.settings.reconnect_min_seconds

        while not self._stop_event.is_set():
            if not self._subscribed_keys:
                self._stop_event.wait(2.0)
                continue

            self._last_error_str = ""
            self._open_event.clear()
            try:
                self._connect_once()
                # Wait up to 6 seconds for handshake _on_open before declaring failure
                self._open_event.wait(6.0)
                if self._connected:
                    delay = self.settings.reconnect_min_seconds
                    while not self._stop_event.wait(1):
                        if not self._connected:
                            break
                else:
                    self._cleanup_streamer()

            except Exception as exc:
                self._connected = False
                self._last_error_str = str(exc)
                self._emit_status("error", f"[{self.name}] {type(exc).__name__}: {exc}")
                self._cleanup_streamer()

            if self._stop_event.is_set():
                break

            err_l = (self._last_error_str or "").lower()
            if "429" in err_l or "too many" in err_l or "403" in err_l or "forbidden" in err_l:
                delay = max(delay, 30.0)
                LOGGER.warning("[%s] Rate limit / Auth guard (%s). Waiting %.0fs cooldown before reconnect.", self.name, self._last_error_str[:80], delay)

            max_cap = max(float(self.settings.reconnect_max_seconds), 45.0 if ("429" in err_l or "403" in err_l) else float(self.settings.reconnect_max_seconds))
            sleep_seconds = min(
                delay + random.uniform(0, 1.5),
                max_cap,
            )
            self._emit_status(
                "reconnecting",
                f"[{self.name}] Reconnecting in {sleep_seconds:.1f} seconds.",
            )
            self._stop_event.wait(sleep_seconds)
            delay = min(delay * 2, max_cap)

    def _connect_once(self) -> None:
        if not self.settings.access_token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is missing.")

        if not self._subscribed_keys:
            LOGGER.warning("[%s] No instrument keys to subscribe to. Waiting...", self.name)
            self._stop_event.wait(2.0)
            return

        self._cleanup_streamer()

        # Upstox V3 full mode allows up to 1,500 combined / 2,000 individual keys per connection.
        keys_list = list(self._subscribed_keys)
        if len(keys_list) > self.max_keys_per_socket:
            LOGGER.warning("[%s] Instrument count %d exceeds socket cap (%d). Pruning overflow.", self.name, len(keys_list), self.max_keys_per_socket)
            keys_list = keys_list[:self.max_keys_per_socket]
            self._subscribed_keys = set(keys_list)

        # Connect initially with the first batch (up to 100 keys) and subscribe the rest in chunks after open
        initial_batch = keys_list[:100]
        self._pending_subscribe_batches = [keys_list[i:i + 100] for i in range(100, len(keys_list), 100)]

        configuration = upstox_client.Configuration()
        configuration.access_token = self.settings.access_token
        api_client = upstox_client.ApiClient(configuration)

        self._streamer = upstox_client.MarketDataStreamerV3(
            api_client,
            initial_batch,
            self.settings.feed_mode,
        )
        try:
            if hasattr(self._streamer, "auto_reconnect"):
                self._streamer.auto_reconnect(False)
        except Exception:
            pass

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
        """Dynamically subscribe additional instrument keys to the live feed in clean 100-key batches."""
        if not instrument_keys:
            return
        to_add = [k for k in instrument_keys if k not in self._subscribed_keys]
        if not to_add:
            return

        max_allowed = self.max_keys_per_socket
        if len(self._subscribed_keys) + len(to_add) > max_allowed:
            available_slots = max(0, max_allowed - len(self._subscribed_keys))
            if len(to_add) > available_slots:
                LOGGER.warning("[%s] Requested %d keys exceeds socket capacity (%d). Truncating batch.", self.name, len(to_add), available_slots)
                to_add = to_add[:available_slots]

        if not to_add:
            return

        self._subscribed_keys.update(to_add)
        if self._streamer and self._connected:
            try:
                use_mode = mode or self.settings.feed_mode
                for i in range(0, len(to_add), 100):
                    chunk = to_add[i:i + 100]
                    self._streamer.subscribe(chunk, use_mode)
                    if i + 100 < len(to_add):
                        time.sleep(0.05)
                LOGGER.info("[%s] Dynamically subscribed to %d instruments (total=%d)", self.name, len(to_add), len(self._subscribed_keys))
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
        self._open_event.set()
        pending = getattr(self, "_pending_subscribe_batches", None) or []
        if pending and self._streamer:
            def _flush_batches():
                for chunk in pending:
                    if not self._connected or self._stop_event.is_set() or not self._streamer:
                        break
                    try:
                        self._streamer.subscribe(chunk, self.settings.feed_mode)
                        time.sleep(0.05)
                    except Exception as exc:
                        LOGGER.warning("[%s] Batch subscribe chunk error: %s", self.name, exc)
            threading.Thread(target=_flush_batches, daemon=True).start()
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
        self._last_error_str = str(error)
        self._open_event.set()
        self._emit_status("error", f"[{self.name}] {error}")

    def _on_close(self, *args: Any) -> None:
        self._connected = False
        self._open_event.set()
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
    Manages up to 3 concurrent Upstox V3 Market Data WebSockets (within Upstox Plus
    5-connection limit) using a single access token:
      1. indices-feed: Major Indices (NIFTY 50, BANK NIFTY, SENSEX), futures,
         and ATM ± 10 basket contracts (42 options per index = 126 options + spots, ~129 keys).
      2. stocks-feed: Primary stocks socket (first ~600 keys).
      3. stocks-feed-2: Overflow stocks socket (remaining ~600 keys for 50 stocks × 22 strikes).
    """

    STOCK_SOCKET_SPLIT_THRESHOLD = 600

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

        stk_1 = stk_keys[:self.STOCK_SOCKET_SPLIT_THRESHOLD]
        stk_2 = stk_keys[self.STOCK_SOCKET_SPLIT_THRESHOLD:]

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
            initial_keys=stk_1,
        )

        self.stocks_feed_2 = UpstoxFeed(
            settings=settings,
            on_tick=self._on_child_tick,
            on_status=lambda s: self._on_child_status("stocks-2", s),
            name="stocks-feed-2",
            initial_keys=stk_2,
        )

        self._child_statuses: dict[str, dict[str, Any]] = {
            "indices": {"state": "init", "connected": False, "message": "Initialized"},
            "stocks": {"state": "init", "connected": False, "message": "Initialized"},
            "stocks-2": {"state": "init", "connected": False, "message": "Initialized"},
        }

    @property
    def running(self) -> bool:
        return self.indices_feed.running or self.stocks_feed.running or self.stocks_feed_2.running

    @property
    def connected(self) -> bool:
        """Returns True if at least one feed is connected."""
        return self.indices_feed.connected or self.stocks_feed.connected or self.stocks_feed_2.connected

    @property
    def both_connected(self) -> bool:
        """Returns True if all active feeds with keys are simultaneously connected."""
        idx_ok = self.indices_feed.connected or not self.indices_feed.subscribed_keys
        stk1_ok = self.stocks_feed.connected or not self.stocks_feed.subscribed_keys
        stk2_ok = self.stocks_feed_2.connected or not self.stocks_feed_2.subscribed_keys
        return self.connected and idx_ok and stk1_ok and stk2_ok

    @property
    def subscribed_keys(self) -> set[str]:
        return (
            self.indices_feed.subscribed_keys
            .union(self.stocks_feed.subscribed_keys)
            .union(self.stocks_feed_2.subscribed_keys)
        )

    def set_classifier(self, classifier: Callable[[str], str]) -> None:
        self.classifier = classifier

    def start(self) -> None:
        LOGGER.info(
            "Starting MultiSocket DualUpstoxFeed: indices (%d), stocks-1 (%d), stocks-2 (%d)",
            len(self.indices_feed.subscribed_keys),
            len(self.stocks_feed.subscribed_keys),
            len(self.stocks_feed_2.subscribed_keys),
        )
        self.indices_feed.start()
        self.stocks_feed.start()
        self.stocks_feed_2.start()

    def stop(self) -> None:
        LOGGER.info("Stopping DualUpstoxFeed...")
        self.indices_feed.stop()
        self.stocks_feed.stop()
        self.stocks_feed_2.stop()

    def _on_child_tick(self, tick: dict[str, Any]) -> None:
        self.on_tick(tick)

    def _on_child_status(self, child_name: str, status: dict[str, Any]) -> None:
        self._child_statuses[child_name] = status
        agg_connected = self.both_connected
        any_connected = self.connected
        agg_state = "connected" if agg_connected else ("partial" if any_connected else status.get("state", "disconnected"))

        stocks_total = len(self.stocks_feed.subscribed_keys) + len(self.stocks_feed_2.subscribed_keys)
        composite_status = {
            "state": agg_state,
            "child_feed": child_name,
            "message": f"[{child_name}] {status.get('message', '')}",
            "connected": any_connected,
            "both_connected": agg_connected,
            "indices_connected": self.indices_feed.connected,
            "stocks_connected": self.stocks_feed.connected or self.stocks_feed_2.connected,
            "indices_count": len(self.indices_feed.subscribed_keys),
            "stocks_count": stocks_total,
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
        if (
            key_str in self._stock_keys
            or key_str in self.stocks_feed.subscribed_keys
            or key_str in self.stocks_feed_2.subscribed_keys
        ):
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
        Balances stock subscriptions cleanly across stocks_feed (<=600) and stocks_feed_2.
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
            already = self.stocks_feed.subscribed_keys.union(self.stocks_feed_2.subscribed_keys)
            new_stk = [k for k in stk_batch if k not in already]
            if new_stk:
                cap_1 = max(0, self.STOCK_SOCKET_SPLIT_THRESHOLD - len(self.stocks_feed.subscribed_keys))
                batch_1 = new_stk[:cap_1]
                batch_2 = new_stk[cap_1:]
                if batch_1:
                    self.stocks_feed.subscribe(batch_1, mode=mode)
                if batch_2:
                    self.stocks_feed_2.subscribe(batch_2, mode=mode)

    def unsubscribe(self, instrument_keys: list[str]) -> None:
        """Routes unsubscribe requests to the appropriate child feed."""
        if not instrument_keys:
            return
        idx_batch: list[str] = []
        stk1_batch: list[str] = []
        stk2_batch: list[str] = []
        for k in instrument_keys:
            k_str = str(k).strip()
            if not k_str:
                continue
            if k_str in self.stocks_feed.subscribed_keys:
                stk1_batch.append(k_str)
                self._stock_keys.discard(k_str)
            elif k_str in self.stocks_feed_2.subscribed_keys:
                stk2_batch.append(k_str)
                self._stock_keys.discard(k_str)
            else:
                idx_batch.append(k_str)
                self._index_keys.discard(k_str)

        if idx_batch:
            self.indices_feed.unsubscribe(idx_batch)
        if stk1_batch:
            self.stocks_feed.unsubscribe(stk1_batch)
        if stk2_batch:
            self.stocks_feed_2.unsubscribe(stk2_batch)
