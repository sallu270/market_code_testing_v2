import unittest
import tempfile
import time
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from database import MarketDatabase
from candle_builder import OneMinuteCandleBuilder
from main import _aggregate_candles

IST = ZoneInfo("Asia/Kolkata")

class TestChartCandleEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_market.db"
        self.db = MarketDatabase(self.db_path)
        self.builder = OneMinuteCandleBuilder(self.db)

    def tearDown(self):
        try:
            self.db._connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_pre_market_and_regular_market_candles(self):
        """Validates that pre-market (09:00-09:15) and regular (09:15-15:30) candles are stored accurately."""
        inst = "NSE_INDEX|Nifty 50"
        
        # 09:05:00 IST tick (Pre-market)
        dt_pre = datetime(2026, 11, 13, 9, 5, 12, tzinfo=IST)
        ts_pre = int(dt_pre.timestamp() * 1000)
        
        tick1 = {
            "instrument_key": inst,
            "exchange_timestamp_ms": ts_pre,
            "received_timestamp_ms": ts_pre,
            "ltp": 25100.0,
            "volume": 1000,
        }
        c1 = self.builder.process_tick(tick1)
        self.assertEqual(c1["candle_start_ms"], int(datetime(2026, 11, 13, 9, 5, 0, tzinfo=IST).timestamp() * 1000))
        self.assertEqual(c1["open"], 25100.0)

        # 09:15:00 IST tick (Market Open)
        dt_open = datetime(2026, 11, 13, 9, 15, 5, tzinfo=IST)
        ts_open = int(dt_open.timestamp() * 1000)
        tick2 = {
            "instrument_key": inst,
            "exchange_timestamp_ms": ts_open,
            "received_timestamp_ms": ts_open,
            "ltp": 25150.0,
            "volume": 2500,
        }
        c2 = self.builder.process_tick(tick2)
        self.assertEqual(c2["candle_start_ms"], int(datetime(2026, 11, 13, 9, 15, 0, tzinfo=IST).timestamp() * 1000))
        self.assertEqual(c2["open"], 25150.0)

        # Pre-market candle in DB must be marked closed when next minute arrived
        pre_db = self.db.get_candle(inst, c1["candle_start_ms"])
        self.assertIsNotNone(pre_db)
        self.assertTrue(pre_db["is_closed"])

    def test_bot_restart_preserves_active_candle(self):
        """When bot stops and restarts during the same minute, it must recover existing open, high, low, volume."""
        inst = "NSE_INDEX|Nifty Bank"
        dt_start = datetime(2026, 11, 13, 10, 30, 10, tzinfo=IST)
        ts_start = int(dt_start.timestamp() * 1000)
        minute_start = int(datetime(2026, 11, 13, 10, 30, 0, tzinfo=IST).timestamp() * 1000)

        # Bot 1 receives 2 ticks
        self.builder.process_tick({
            "instrument_key": inst,
            "exchange_timestamp_ms": ts_start,
            "received_timestamp_ms": ts_start,
            "ltp": 52000.0,
            "volume": 100,
        })
        self.builder.process_tick({
            "instrument_key": inst,
            "exchange_timestamp_ms": ts_start + 5000,
            "received_timestamp_ms": ts_start + 5000,
            "ltp": 52050.0,
            "volume": 150,
        })

        # Simulate bot crash/restart: create brand new builder instance (memory completely empty)
        new_builder = OneMinuteCandleBuilder(self.db)
        
        # New tick at 10:30:40 arrives
        c_after_restart = new_builder.process_tick({
            "instrument_key": inst,
            "exchange_timestamp_ms": ts_start + 30000,
            "received_timestamp_ms": ts_start + 30000,
            "ltp": 51980.0,
            "volume": 200,
        })

        # Must preserve original open 52000, high 52050, low 51980, close 51980
        self.assertEqual(c_after_restart["open"], 52000.0)
        self.assertEqual(c_after_restart["high"], 52050.0)
        self.assertEqual(c_after_restart["low"], 51980.0)
        self.assertEqual(c_after_restart["close"], 51980.0)
        self.assertEqual(c_after_restart["candle_start_ms"], minute_start)

    def test_candle_aggregation_preserves_order_and_levels(self):
        """Aggregating 1m candles into 5m must set open from first, close from last, high from max, low from min."""
        inst = "NSE_INDEX|Nifty 50"
        t0 = int(datetime(2026, 11, 13, 9, 15, 0, tzinfo=IST).timestamp() * 1000)
        
        c_1m = [
            {"candle_start_ms": t0 + 0 * 60000, "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0, "volume": 10},
            {"candle_start_ms": t0 + 1 * 60000, "open": 102.0, "high": 110.0, "low": 101.0, "close": 108.0, "volume": 15},
            {"candle_start_ms": t0 + 2 * 60000, "open": 108.0, "high": 109.0, "low": 95.0, "close": 97.0, "volume": 20},
            {"candle_start_ms": t0 + 3 * 60000, "open": 97.0, "high": 100.0, "low": 96.0, "close": 99.0, "volume": 12},
            {"candle_start_ms": t0 + 4 * 60000, "open": 99.0, "high": 104.0, "low": 98.0, "close": 103.0, "volume": 18},
        ]

        agg_5m = _aggregate_candles(c_1m, "5m")
        self.assertEqual(len(agg_5m), 1)
        bar = agg_5m[0]
        self.assertEqual(bar["candle_start_ms"], t0)
        self.assertEqual(bar["open"], 100.0)
        self.assertEqual(bar["high"], 110.0)
        self.assertEqual(bar["low"], 95.0)
        self.assertEqual(bar["close"], 103.0)
        self.assertEqual(bar["volume"], 75.0)

if __name__ == "__main__":
    unittest.main()
