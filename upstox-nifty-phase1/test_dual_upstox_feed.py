import unittest
from unittest.mock import MagicMock
from pathlib import Path

from config import Settings
from upstox_feed import DualUpstoxFeed, UpstoxFeed


class TestDualUpstoxFeed(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            access_token="fake_token",
            instrument_keys=("NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX", "NSE_FO|61471", "NSE_FO|61466"),
            feed_mode="full",
            db_path=Path("market_data.db"),
            host="127.0.0.1",
            port=5000,
            debug=False,
            reconnect_min_seconds=2,
            reconnect_max_seconds=30,
            ui_tick_limit=100,
            ui_candle_limit=120,
        )
        self.received_ticks = []
        self.received_statuses = []

    def on_tick(self, tick):
        self.received_ticks.append(tick)

    def on_status(self, status):
        self.received_statuses.append(status)

    def test_dual_feed_initialization(self):
        index_keys = ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX", "NSE_FO|61471", "NSE_FO|61466"]
        stock_keys = ["NSE_EQ|INE002A01018", "NSE_EQ|INE040A01034", "NSE_EQ|INE090A01021"]

        feed = DualUpstoxFeed(
            settings=self.settings,
            on_tick=self.on_tick,
            on_status=self.on_status,
            index_keys=index_keys,
            stock_keys=stock_keys,
        )

        self.assertEqual(len(feed.indices_feed.subscribed_keys), 5)
        self.assertEqual(len(feed.stocks_feed.subscribed_keys), 3)
        self.assertEqual(len(feed.subscribed_keys), 8)

    def test_dual_feed_routing_and_classification(self):
        index_keys = ["NSE_INDEX|Nifty 50", "NSE_FO|61471"]
        stock_keys = ["NSE_EQ|INE002A01018"]

        def dummy_classifier(k: str) -> str:
            if "RELIANCE" in k or k == "NSE_FO|99999":
                return "stocks"
            if "NIFTY" in k or k == "NSE_FO|88888":
                return "indices"
            return "indices"

        feed = DualUpstoxFeed(
            settings=self.settings,
            on_tick=self.on_tick,
            on_status=self.on_status,
            index_keys=index_keys,
            stock_keys=stock_keys,
            classifier=dummy_classifier,
        )

        # 1. Test auto-classification of index spot & stock EQ
        feed.subscribe(["NSE_INDEX|Nifty IT", "NSE_EQ|INE467B01029"])
        self.assertIn("NSE_INDEX|Nifty IT", feed.indices_feed.subscribed_keys)
        self.assertIn("NSE_EQ|INE467B01029", feed.stocks_feed.subscribed_keys)

        # 2. Test FO key classification via classifier callback
        feed.subscribe(["NSE_FO|88888", "NSE_FO|99999"])
        self.assertIn("NSE_FO|88888", feed.indices_feed.subscribed_keys)
        self.assertIn("NSE_FO|99999", feed.stocks_feed.subscribed_keys)

        # 3. Test explicit target routing
        feed.subscribe(["NSE_FO|11111"], target="indices")
        feed.subscribe(["NSE_FO|22222"], target="stocks")
        self.assertIn("NSE_FO|11111", feed.indices_feed.subscribed_keys)
        self.assertIn("NSE_FO|22222", feed.stocks_feed.subscribed_keys)

    def test_dual_feed_tick_dispatch(self):
        feed = DualUpstoxFeed(
            settings=self.settings,
            on_tick=self.on_tick,
            on_status=self.on_status,
            index_keys=["NSE_INDEX|Nifty 50"],
            stock_keys=["NSE_EQ|INE002A01018"],
        )

        test_tick_index = {"instrument_key": "NSE_INDEX|Nifty 50", "ltp": 24100.0}
        test_tick_stock = {"instrument_key": "NSE_EQ|INE002A01018", "ltp": 1340.0}

        feed._on_child_tick(test_tick_index)
        feed._on_child_tick(test_tick_stock)

        self.assertEqual(len(self.received_ticks), 2)
        self.assertEqual(self.received_ticks[0]["instrument_key"], "NSE_INDEX|Nifty 50")
        self.assertEqual(self.received_ticks[1]["instrument_key"], "NSE_EQ|INE002A01018")

    def test_composite_status(self):
        feed = DualUpstoxFeed(
            settings=self.settings,
            on_tick=self.on_tick,
            on_status=self.on_status,
            index_keys=["NSE_INDEX|Nifty 50"],
            stock_keys=["NSE_EQ|INE002A01018"],
        )

        feed._on_child_status("indices", {"state": "connected", "message": "Indices socket connected"})
        self.assertTrue(len(self.received_statuses) > 0)
        latest = self.received_statuses[-1]
        self.assertEqual(latest["child_feed"], "indices")
        self.assertIn("[indices]", latest["message"])
        self.assertTrue("indices_count" in latest)
        self.assertTrue("stocks_count" in latest)


    def test_unsubscribe(self):
        feed = DualUpstoxFeed(
            settings=self.settings,
            on_tick=self.on_tick,
            on_status=self.on_status,
            index_keys=["NSE_INDEX|Nifty 50"],
            stock_keys=["NSE_EQ|INE002A01018", "NSE_FO|99999"],
        )
        self.assertIn("NSE_FO|99999", feed.stocks_feed.subscribed_keys)
        feed.unsubscribe(["NSE_FO|99999"])
        self.assertNotIn("NSE_FO|99999", feed.stocks_feed.subscribed_keys)
        self.assertNotIn("NSE_FO|99999", feed.subscribed_keys)


if __name__ == "__main__":
    unittest.main()
