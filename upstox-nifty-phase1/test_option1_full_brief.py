import csv
import gzip
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import IST, Settings
from database import MarketDatabase
from instruments_master import UpstoxInstrumentsMaster
from stock_screener import NIFTY_50_STOCKS
from strategy_engine import GapEntryStrategyEngine, NIFTY_KEY, BANKNIFTY_KEY, SENSEX_KEY
from upstox_feed import DualUpstoxFeed, UpstoxFeed


class DummyFeed:
    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []

    def subscribe(self, keys, **kwargs):
        self.subscribed.extend(keys)

    def unsubscribe(self, keys, **kwargs):
        self.unsubscribed.extend(keys)


def _sample_nse_master() -> list[dict]:
    exp_ms = int(datetime(2026, 12, 3, 15, 30, tzinfo=IST).timestamp() * 1000)
    rows = []
    for idx_sym, lot, center, step in [("NIFTY", 65, 24000, 50), ("BANKNIFTY", 30, 52000, 100)]:
        rows.append({
            "segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": idx_sym,
            "instrument_key": f"NSE_FO|{idx_sym}FUT", "trading_symbol": f"{idx_sym} FUT",
            "lot_size": lot, "expiry": exp_ms,
        })
        for i in range(-12, 13):
            stk = center + i * step
            for opt_type in ("CE", "PE"):
                rows.append({
                    "segment": "NSE_FO", "instrument_type": opt_type, "underlying_symbol": idx_sym,
                    "instrument_key": f"NSE_FO|{idx_sym}{stk}{opt_type}",
                    "trading_symbol": f"{idx_sym} {stk} {opt_type}",
                    "strike_price": float(stk), "lot_size": lot, "expiry": exp_ms,
                })
    for sym, lot, center, step in [("RELIANCE", 500, 1300, 20), ("HDFCBANK", 550, 1700, 20), ("TMPV", 800, 900, 10)]:
        for i in range(-6, 7):
            stk = center + i * step
            for opt_type in ("CE", "PE"):
                rows.append({
                    "segment": "NSE_FO", "instrument_type": opt_type, "underlying_symbol": sym,
                    "instrument_key": f"NSE_FO|{sym}{stk}{opt_type}",
                    "trading_symbol": f"{sym} {stk} {opt_type}",
                    "strike_price": float(stk), "lot_size": lot, "expiry": exp_ms,
                })
    return rows


def _sample_bse_master() -> list[dict]:
    exp_ms = int(datetime(2026, 12, 4, 15, 30, tzinfo=IST).timestamp() * 1000)
    rows = [{
        "segment": "BSE_FO", "instrument_type": "FUT", "underlying_symbol": "SENSEX",
        "instrument_key": "BSE_FO|SENSEXFUT", "trading_symbol": "SENSEX FUT",
        "lot_size": 20, "expiry": exp_ms,
    }]
    for i in range(-12, 13):
        stk = 80000 + i * 100
        for opt_type in ("CE", "PE"):
            rows.append({
                "segment": "BSE_FO", "instrument_type": opt_type, "underlying_symbol": "SENSEX",
                "instrument_key": f"BSE_FO|SENSEX{stk}{opt_type}",
                "trading_symbol": f"SENSEX {stk} {opt_type}",
                "strike_price": float(stk), "lot_size": 20, "expiry": exp_ms,
            })
    return rows


class TestOption1FullBrief(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_dir = Path(self.tmp_dir.name)
        self.cache_dir = self.base_dir / "instruments_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        with open(self.cache_dir / f"NSE_{today_str}.json", "w", encoding="utf-8") as f:
            json.dump(_sample_nse_master(), f)
        with open(self.cache_dir / f"BSE_{today_str}.json", "w", encoding="utf-8") as f:
            json.dump(_sample_bse_master(), f)

        self.db = MarketDatabase(self.base_dir / "test.db")
        self.engine = GapEntryStrategyEngine(database=self.db, base_dir=self.base_dir, access_token="")
        self.engine.mode = "paper"
        self.feed = DummyFeed()
        self.engine.set_feed(self.feed)

    def tearDown(self):
        self.db.close()
        self.tmp_dir.cleanup()

    def test_01_instruments_master_lot_sizes_and_alias(self):
        master = UpstoxInstrumentsMaster(self.base_dir)
        self.assertEqual(master.get_lot_size("NIFTY"), 65)
        self.assertEqual(master.get_lot_size("BANKNIFTY"), 30)
        self.assertEqual(master.get_lot_size("SENSEX"), 20)
        self.assertEqual(master.get_lot_size("TATAMOTORS"), 800)

        self.assertEqual(len(self.engine.stock_underlyings), 50)
        self.assertEqual(self.engine.index_lot_sizes[NIFTY_KEY], 65)
        self.assertEqual(self.engine.index_lot_sizes[BANKNIFTY_KEY], 30)
        self.assertEqual(self.engine.index_lot_sizes[SENSEX_KEY], 20)

    def test_02_atm_plus_minus_5_basket_and_unified_0915_baseline(self):
        self.engine.load_contracts_if_needed(NIFTY_KEY)
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        self.engine.plan_8_itm_contracts(24010.0, underlying_key=NIFTY_KEY)

        self.assertEqual(len(idx_st["ce_basket_keys"]), 21)
        self.assertEqual(len(idx_st["pe_basket_keys"]), 21)
        self.assertEqual(len(idx_st["planned_keys"]), 42)

        for i, k in enumerate(idx_st["ce_basket_keys"]):
            self.engine.hot_cache[k] = {
                "ltp": 100.0 + i * 25.0, "prev_close": 999.0, "oi": 1000,
                "bid_price": 99.0 + i * 25.0, "ask_price": 101.0 + i * 25.0,
                "spread_pct": 1.0, "strike": 23750.0 + i * 50, "side": "CE",
            }
        for i, k in enumerate(idx_st["pe_basket_keys"]):
            self.engine.hot_cache[k] = {
                "ltp": 80.0 + i * 20.0, "prev_close": 888.0, "oi": 1000,
                "bid_price": 79.0 + i * 20.0, "ask_price": 81.0 + i * 20.0,
                "spread_pct": 1.0, "strike": 23750.0 + i * 50, "side": "PE",
            }

        self.engine.lock_contracts(underlying_key=NIFTY_KEY)
        self.engine.take_snapshot(underlying_key=NIFTY_KEY)

        base_ce = sum(idx_st["baseline_prices"][k] for k in idx_st["ce_basket_keys"])
        base_pe = sum(idx_st["baseline_prices"][k] for k in idx_st["pe_basket_keys"])
        self.assertAlmostEqual(idx_st["ce_baseline_sum"], round(base_ce, 2), places=2)
        self.assertAlmostEqual(idx_st["pe_baseline_sum"], round(base_pe, 2), places=2)
        self.assertNotAlmostEqual(idx_st["ce_baseline_sum"], 999.0 * 11, places=1)

    def test_03_091455_zero_price_never_triggers_false_positive_tick(self):
        self.engine.load_contracts_if_needed(NIFTY_KEY)
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        self.engine.plan_8_itm_contracts(24000.0, underlying_key=NIFTY_KEY)

        k_zero = idx_st["ce_basket_keys"][0]
        for k in idx_st["planned_keys"]:
            self.engine.hot_cache[k] = {
                "ltp": 0.0 if k == k_zero else 250.0,
                "prev_close": 300.0,
                "bid_price": 0.0 if k == k_zero else 249.0,
                "ask_price": 0.0 if k == k_zero else 251.0,
                "spread_pct": 0.5,
                "strike": 24000.0,
                "side": "CE" if "CE" in k else "PE",
            }

        self.engine.lock_contracts(underlying_key=NIFTY_KEY)
        self.assertEqual(self.engine.hot_cache[k_zero]["ref_ltp"], 0.0)
        self.assertEqual(self.engine.hot_cache[k_zero]["ref_source"], "MISSING_091455")

        idx_st["state"] = "ARMED"
        self.engine.enabled = True
        mock_dt = datetime(2026, 11, 27, 9, 15, 2, tzinfo=IST)
        with patch("strategy_engine.datetime") as dt_mod:
            dt_mod.now.return_value = mock_dt
            dt_mod.side_effect = lambda *a, **kw: datetime(*a, **kw)
            res = self.engine.on_tick({
                "instrument_key": k_zero,
                "ltp": 120.0,
                "bid_price": 119.5,
                "ask_price": 120.5,
                "received_timestamp_ms": int(mock_dt.timestamp() * 1000),
            })
        self.assertIsNone(res)
        self.assertIsNone(idx_st.get("path1_position"))

    def test_04_stock_mid_price_filter_ranking_and_entry(self):
        rel_key = "RELIANCE"
        hdfc_key = "HDFCBANK"
        self.engine.plan_stock_contracts(rel_key, 1300.0)
        self.engine.plan_stock_contracts(hdfc_key, 1700.0)

        rel_st = self.engine.stock_underlyings[rel_key]
        hdfc_st = self.engine.stock_underlyings[hdfc_key]
        self.assertEqual(len(rel_st["ce_basket_keys"]), 11)
        self.assertEqual(len(rel_st["pe_basket_keys"]), 11)

        for k in rel_st["ce_basket_keys"]:
            stk = float(self.engine.contracts_by_key.get(k, {}).get("strike_price") or 1300.0)
            if stk == 1400.0:
                self.engine.hot_cache[k] = {"instrument_key": k, "ltp": 100.0, "bid_price": 80.0, "ask_price": 120.0, "strike": stk, "side": "CE", "trading_symbol": f"REL{int(stk)}CE"}
                rel_st["baseline_prices"][k] = 100.0
            elif stk == 1380.0:
                self.engine.hot_cache[k] = {"instrument_key": k, "ltp": 3.0, "bid_price": 2.9, "ask_price": 3.1, "strike": stk, "side": "CE", "trading_symbol": f"REL{int(stk)}CE"}
                rel_st["baseline_prices"][k] = 3.0
            else:
                rel_st["baseline_prices"][k] = 100.0
                self.engine.hot_cache[k] = {"instrument_key": k, "ltp": 101.0, "bid_price": 105.0, "ask_price": 107.0, "strike": stk, "side": "CE", "trading_symbol": f"REL{int(stk)}CE"}
        for k in rel_st["pe_basket_keys"]:
            stk = float(self.engine.contracts_by_key.get(k, {}).get("strike_price") or 1300.0)
            rel_st["baseline_prices"][k] = 100.0
            self.engine.hot_cache[k] = {"instrument_key": k, "ltp": 98.0, "bid_price": 94.0, "ask_price": 96.0, "strike": stk, "side": "PE", "trading_symbol": f"REL{int(stk)}PE"}

        for k in hdfc_st["ce_basket_keys"]:
            stk = float(self.engine.contracts_by_key.get(k, {}).get("strike_price") or 1700.0)
            hdfc_st["baseline_prices"][k] = 100.0
            self.engine.hot_cache[k] = {"instrument_key": k, "ltp": 95.0, "bid_price": 94.5, "ask_price": 95.5, "strike": stk, "side": "CE", "trading_symbol": f"HDFC{int(stk)}CE"}
        for k in hdfc_st["pe_basket_keys"]:
            stk = float(self.engine.contracts_by_key.get(k, {}).get("strike_price") or 1700.0)
            hdfc_st["baseline_prices"][k] = 100.0
            self.engine.hot_cache[k] = {"instrument_key": k, "ltp": 104.0, "bid_price": 103.5, "ask_price": 104.5, "strike": stk, "side": "PE", "trading_symbol": f"HDFC{int(stk)}PE"}

        rel_st["spot_price"] = 1300.0
        rel_st["state"] = "ARMED"
        rel_st["snapshot_taken"] = True
        rel_st["baseline_locked"] = True
        hdfc_st["spot_price"] = 1700.0
        hdfc_st["state"] = "ARMED"
        hdfc_st["snapshot_taken"] = True
        hdfc_st["baseline_locked"] = True
        self.engine._recompute_unified_baseline_sums(rel_st)
        self.engine._recompute_unified_baseline_sums(hdfc_st)

        t0 = datetime(2026, 11, 27, 10, 0, 0, tzinfo=IST)
        with patch("strategy_engine.datetime") as dt_mod:
            dt_mod.now.return_value = t0
            dt_mod.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.engine.evaluate_all_stocks_ranking()

        ranks = self.engine._stock_rankings
        self.assertGreaterEqual(len(ranks), 2)
        self.assertEqual(ranks[0]["symbol"], "RELIANCE")
        self.assertEqual(ranks[0]["rank"], 1)
        self.assertGreater(ranks[0]["score"], 10.0)
        self.assertEqual(ranks[0]["raw_signal"], "CE Buy")
        self.assertEqual(ranks[-1]["symbol"], "HDFCBANK")
        self.assertEqual(ranks[-1]["raw_signal"], "PE Buy")

        rel_st["candidate_start_time"] = (rel_st.get("candidate_start_time") or 0.0) - 305.0
        hdfc_st["candidate_start_time"] = (hdfc_st.get("candidate_start_time") or 0.0) - 305.0

        t1 = datetime(2026, 11, 27, 10, 5, 5, tzinfo=IST)
        with patch("strategy_engine.datetime") as dt_mod:
            dt_mod.now.return_value = t1
            dt_mod.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.engine.evaluate_all_stocks_ranking()

        self.assertGreaterEqual(len(self.engine._top_bullish), 1)
        self.assertEqual(self.engine._top_bullish[0]["symbol"], "RELIANCE")
        self.assertIsNotNone(rel_st.get("path2_position"))
        self.assertEqual(rel_st["path2_position"]["strike"], 1260.0)

    def test_05_configurable_itm_depth_and_premium_band(self):
        self.engine.load_contracts_if_needed(NIFTY_KEY)
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        self.engine.plan_8_itm_contracts(24000.0, underlying_key=NIFTY_KEY)

        for k in idx_st["ce_basket_keys"]:
            info = self.engine.contracts_by_key.get(k, {})
            stk = float(info.get("strike_price") or 24000.0)
            ltp = 2600.0 if stk == 23950.0 else 350.0
            self.engine.hot_cache[k] = {
                "ltp": ltp, "bid_price": ltp - 1.0, "ask_price": ltp + 1.0,
                "spread_pct": 0.5, "strike": stk, "side": "CE",
                "trading_symbol": info.get("trading_symbol", k),
            }

        self.engine.update_config({"entry_itm_depth": 1, "min_entry_premium": 50.0, "max_entry_premium": 2500.0})
        best = self.engine._select_itm_strike(NIFTY_KEY, "CE", spot=24000.0)
        self.assertIsNotNone(best)
        self.assertEqual(best["strike"], 23900.0)

        self.engine.update_config({"entry_itm_depth": 3})
        best3 = self.engine._select_itm_strike(NIFTY_KEY, "CE", spot=24000.0)
        self.assertEqual(best3["strike"], 23850.0)

    def test_06_multi_socket_partitioning_1219_keys(self):
        s = Settings(
            access_token="dummy",
            instrument_keys=("NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX"),
            feed_mode="full",
            db_path=self.base_dir / "t.db",
            host="127.0.0.1", port=5000, debug=False,
            reconnect_min_seconds=2, reconnect_max_seconds=30,
            ui_tick_limit=200, ui_candle_limit=200,
        )
        stock_keys = [m["key"] for m in NIFTY_50_STOCKS] + [f"NSE_FO|OPT_{i}" for i in range(1100)]
        dual = DualUpstoxFeed(
            settings=s,
            on_tick=lambda t: None,
            on_status=lambda st: None,
            classifier=lambda k: "INDEX" if k in s.instrument_keys else "STOCK",
            index_keys=list(s.instrument_keys) + [f"NSE_FO|IDX_{i}" for i in range(66)],
            stock_keys=stock_keys,
        )
        self.assertIsNotNone(dual.stocks_feed_2)
        self.assertEqual(len(dual.indices_feed.subscribed_keys), 69)
        self.assertEqual(len(dual.stocks_feed.subscribed_keys) + len(dual.stocks_feed_2.subscribed_keys), 1150)
        self.assertLessEqual(len(dual.stocks_feed.subscribed_keys), 600)
        self.assertLessEqual(len(dual.stocks_feed_2.subscribed_keys), 600)

        uf = UpstoxFeed(s, lambda t: None, lambda st: None, initial_keys=[])
        self.assertGreaterEqual(uf.max_keys_per_socket, 1500)
        uf._streamer = MagicMock()
        uf._connected = True
        uf.subscribe([f"NSE_FO|CHUNK_{i}" for i in range(250)])
        self.assertEqual(uf._streamer.subscribe.call_count, 3)
        self.assertEqual(len(uf.subscribed_keys), 250)

    def test_07_async_db_and_flask_endpoints(self):
        for i in range(5):
            self.db.enqueue_tick({
                "instrument_key": f"NSE_INDEX|TEST_{i}",
                "symbol": "TEST",
                "feed_type": "ltpc",
                "ltp": 100.0 + i,
                "ltt": "1000",
                "ltq": 10,
                "cp": 99.0,
                "oi": 500,
                "received_timestamp_ms": 1700000000000 + i,
                "raw": {},
            })
        self.db.enqueue_momentum_log({
            "log_date": "2026-11-27", "timestamp_ist": "10:00:01", "timestamp_ms": 1700000001000,
            "underlying_key": NIFTY_KEY, "index_name": "NIFTY 50",
            "prev_ce_pct": 1.0, "ce_pct": 3.5, "delta_ce": 2.5,
            "prev_pe_pct": -1.0, "pe_pct": -2.5, "delta_pe": -1.5,
            "raw_signal": "CE Buy", "hold_sec": 10, "confirmed": False, "score": 6.0,
        })
        self.db.save_premarket_lock("2026-11-27", NIFTY_KEY, [
            {"instrument_key": "NSE_FO|NIFTY23900CE", "side": "CE", "strike": 23900.0, "ref_ltp": 155.0, "ref_source": "live_091455"}
        ])
        self.db._drain_async_queues()

        logs = self.db.get_momentum_logs(date="2026-11-27", limit=100000)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["score"], 6.0)
        locked = self.db.get_premarket_lock("2026-11-27", NIFTY_KEY)
        self.assertIsNotNone(locked)
        self.assertEqual(locked[0]["ref_ltp"], 155.0)

        import dataclasses
        from config import load_settings
        real_settings = dataclasses.replace(load_settings(), db_path=self.base_dir / "test.db")

        with patch("config.load_settings", return_value=real_settings), \
             patch("upstox_feed.DualUpstoxFeed.start", return_value=None), \
             patch("threading.Thread.start", return_value=None), \
             patch("threading.Timer.start", return_value=None):
            import main as app_main
        orig_db, orig_eng = app_main.database, app_main.strategy_engine
        app_main.database = self.db
        app_main.strategy_engine = self.engine
        try:
            client = app_main.app.test_client()

            resp_dash = client.get("/api/dashboard-data")
            self.assertEqual(resp_dash.status_code, 200)
            data = resp_dash.get_json()
            self.assertIn("stock_rankings", data)
            self.assertIn("entry_itm_depth", data["config"])
            self.assertIn("index_lot_sizes", data["config"])

            resp_cfg = client.post("/api/strategy/config", json={"entry_itm_depth": 2, "min_entry_premium": 50.0, "max_entry_premium": 2500.0})
            self.assertEqual(resp_cfg.status_code, 200)

            resp_proof = client.get("/api/strategy/baseline-proof.csv")
            self.assertEqual(resp_proof.status_code, 200)
            self.assertIn("09:14:55 Lock LTP", resp_proof.get_data(as_text=True))

            resp_mom_csv = client.get("/api/strategy/momentum/export.csv")
            self.assertEqual(resp_mom_csv.status_code, 200)
            self.assertIn("Score (CE%-PE%)", resp_mom_csv.get_data(as_text=True))
        finally:
            app_main.database = orig_db
            app_main.strategy_engine = orig_eng


if __name__ == "__main__":
    unittest.main()
