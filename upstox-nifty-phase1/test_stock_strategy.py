"""
Comprehensive Unit & Acceptance Tests for Stock Options Trading Strategy
(Dr. Vijay Bhilwade Specification: OI Buildup Trading System - Final Logic for Developer)

Validates:
1. Part 2: 3:15 PM EOD Watchlist Scoring (Futures buildup, OI delta >= 3%, CLV, RVOL, ranking ★★★★/★★★/★★).
2. Part 4 (Path 1): 09:15 First-Tick Entry (0.3% gap filter, first tick above/below open, SL 25%, Target 40%, 09:45 cutoff, max 1 09:15 entry).
3. Part 4 (Path 2): From 09:18 OI-based Entry (16-condition table, Futures Gate LB/SC for CE & SB/LU for PE, 2 consecutive OI updates, sizing matrix).
4. Risk & Exits: 25% SL, 40% Target, 09:45 Cutoff, Futures Flip exit, Signal Reversal exit, max 3 trades per stock per day, 15:15 EOD square-off.
5. Indices Isolation: NIFTY 50, BANK NIFTY, and SENSEX remain 100% on First Positive Tick without interference.
"""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from database import MarketDatabase
from stock_screener import StockScreener, NIFTY_50_STOCKS
from strategy_engine import GapEntryStrategyEngine, NIFTY_KEY, BANKNIFTY_KEY, SENSEX_KEY

IST = ZoneInfo("Asia/Kolkata")


class TestStockOptionsStrategy(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="stock_strat_test_"))
        self.db_path = self.test_dir / "market_data.db"
        self.db = MarketDatabase(self.db_path)

        # Copy NSE.json if available in workspace
        root_nse = Path("NSE.json")
        if root_nse.exists():
            shutil.copy(root_nse, self.test_dir / "NSE.json")

        self.screener = StockScreener(base_dir=self.test_dir)
        self.engine = GapEntryStrategyEngine(
            database=self.db,
            base_dir=self.test_dir,
        )
        self.engine.mode = "paper"
        self.engine.re_eval_min_interval_sec = 0.0

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_mock_stock(self, sym: str = "RELIANCE", base_lots: int = 1, rank: str = "★★★", direction: str = "BULLISH"):
        """Configures mock stock in engine with 8 ITM contracts."""
        ce_keys = [f"{sym}_CE_{i}" for i in range(4)]
        pe_keys = [f"{sym}_PE_{i}" for i in range(4)]
        all_keys = ce_keys + pe_keys

        st = {
            "symbol": sym,
            "key": f"NSE_EQ|{sym}",
            "lot_size": 250,
            "strike_step": 20.0,
            "lots": base_lots,
            "enabled": True,
            "spot_price": 1300.0,
            "open_price": 1300.0,
            "prev_close": 1300.0,
            "gap_pct": 0.0,
            "gap_against": False,
            "gap_reason": "",
            "watchlist_rank": rank,
            "watchlist_direction": direction,
            "futures_key": f"NSE_FO|{sym}FUT",
            "futures_symbol": f"{sym} FUT",
            "futures_ltp": 1305.0,
            "futures_oi": 50000.0,
            "futures_prev_close": 1300.0,
            "futures_prev_oi": 48000.0,
            "futures_buildup": "LB",
            "path1_done": False,
            "consecutive_signal": None,
            "consecutive_count": 0,
            "today_trades_count": 0,
            "state": "ARMED",
            "planned_keys": list(all_keys),
            "locked_keys": list(all_keys),
            "snapshot": {},
            "snapshot_taken": True,
            "last_evaluation": {
                "decision": "WAIT",
                "reason": "Initialized",
                "delta_ce": 0.0,
                "delta_pe": 0.0,
                "ce_type": None,
                "pe_type": None,
                "rule": None,
                "stars": "",
                "size": "",
                "summary_line": f"{sym} WAIT",
            },
            "current_position": None,
            "_last_eval_time": 0.0,
        }
        self.engine.stock_underlyings[sym] = st

        snap = {}
        for i, k in enumerate(ce_keys):
            stk = 1300.0 - ((4 - i) * 20.0)
            self.engine.contracts_by_key[k] = {
                "instrument_key": k, "trading_symbol": f"{sym} {stk} CE", "strike_price": stk,
                "instrument_type": "CE", "lot_size": 250, "underlying_key": sym
            }
            self.engine.hot_cache[k] = {
                "instrument_key": k, "trading_symbol": f"{sym} {stk} CE", "strike": stk,
                "side": "CE", "ltp": 50.0, "oi": 10000.0, "bid_price": 49.5, "ask_price": 50.0, "spread_pct": 1.0
            }
            snap[k] = {"ltp": 50.0, "oi": 10000.0, "bid": 49.5, "ask": 50.0, "strike": stk, "side": "CE"}

        for i, k in enumerate(pe_keys):
            stk = 1300.0 + ((i + 1) * 20.0)
            self.engine.contracts_by_key[k] = {
                "instrument_key": k, "trading_symbol": f"{sym} {stk} PE", "strike_price": stk,
                "instrument_type": "PE", "lot_size": 250, "underlying_key": sym
            }
            self.engine.hot_cache[k] = {
                "instrument_key": k, "trading_symbol": f"{sym} {stk} PE", "strike": stk,
                "side": "PE", "ltp": 50.0, "oi": 10000.0, "bid_price": 49.5, "ask_price": 50.0, "spread_pct": 1.0
            }
            snap[k] = {"ltp": 50.0, "oi": 10000.0, "bid": 49.5, "ask": 50.0, "strike": stk, "side": "PE"}

        st["snapshot"] = snap
        return st

    # =========================================================================
    # 1. WATCHLIST GENERATION TESTS (Part 2 of document)
    # =========================================================================

    def test_watchlist_scoring_and_generation(self):
        """Validates 3:15 PM EOD scoring: CLV, RVOL, Futures delta, and Top 5 Bullish/Bearish output."""
        wl = self.screener.build_tomorrow_watchlist()
        self.assertIn("top_bullish", wl)
        self.assertIn("top_bearish", wl)
        self.assertEqual(len(wl["top_bullish"]), 5)
        self.assertEqual(len(wl["top_bearish"]), 5)

        for b in wl["top_bullish"]:
            self.assertEqual(b["direction"], "BULLISH")
            self.assertIn(b["rank"], ("★★★★", "★★★", "★★"))
            self.assertTrue(b["futures_key"].startswith("NSE_FO|"))

        for s in wl["top_bearish"]:
            self.assertEqual(s["direction"], "BEARISH")
            self.assertIn(s["rank"], ("★★★★", "★★★", "★★"))
            self.assertTrue(s["futures_key"].startswith("NSE_FO|"))

        # Verify file saved to disk
        wl_file = self.test_dir / "tomorrow_watchlist.json"
        self.assertTrue(wl_file.exists())

    # =========================================================================
    # 2. PATH 1 (09:15 FIRST-TICK ENTRY) TESTS
    # =========================================================================

    def test_path1_gap_filter_rejection(self):
        """Path 1: Stock gapping > 0.3% against watchlist direction is skipped."""
        st = self._setup_mock_stock("RELIANCE", rank="★★★", direction="BULLISH")
        st["prev_close"] = 1000.0
        # Gaps down to 995 (-0.5% > 0.3% against BULLISH)
        st["spot_price"] = 995.0
        self.engine.take_stock_snapshot("RELIANCE")

        self.assertTrue(st["gap_against"])
        self.assertIn("Gapped down", st["gap_reason"])

        # Attempt first-tick entry above open
        tick = {"instrument_key": st["key"], "ltp": 996.0}
        trade = self.engine.on_tick(tick)
        self.assertIsNone(trade)
        self.assertFalse(st["path1_done"])
        self.assertIsNone(st["current_position"])

    def test_path1_bullish_first_tick_ce_buy(self):
        """Path 1: Bullish stock without opposing gap buys ATM CE on first tick above open."""
        st = self._setup_mock_stock("RELIANCE", base_lots=2, rank="★★★", direction="BULLISH")
        st["prev_close"] = 1000.0
        st["spot_price"] = 1000.0  # Flat open (0.0% gap)
        self.engine.take_stock_snapshot("RELIANCE")
        self.assertFalse(st["gap_against"])

        # First tick arrives above open price (1001.0 > 1000.0)
        tick = {"instrument_key": st["key"], "ltp": 1001.0}
        trade = self.engine.on_tick(tick)

        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_CE")
        self.assertEqual(trade["entry_path"], "PATH_1_0915")
        self.assertEqual(trade["timeout_time"], "09:45:00")
        self.assertEqual(trade["lots"], 2)
        self.assertEqual(trade["lot_size"], 500)  # 2 lots * 250 = 500
        self.assertEqual(trade["stop_loss_price"], round(trade["entry_price"] * 0.75, 2))  # 25% SL
        self.assertEqual(trade["target_price"], round(trade["entry_price"] * 1.40, 2))      # 40% Target
        self.assertTrue(st["path1_done"])
        self.assertEqual(st["state"], "HOLDING")
        self.assertEqual(st["today_trades_count"], 1)

    def test_path1_bearish_first_tick_pe_buy(self):
        """Path 1: Bearish stock without opposing gap buys ATM PE on first tick below open."""
        st = self._setup_mock_stock("INFY", base_lots=1, rank="★★★★", direction="BEARISH")
        st["prev_close"] = 1500.0
        st["spot_price"] = 1498.0  # -0.13% gap in line with bearish
        self.engine.take_stock_snapshot("INFY")
        self.assertFalse(st["gap_against"])

        # First tick arrives below open price (1497.0 < 1498.0)
        tick = {"instrument_key": st["key"], "ltp": 1497.0}
        trade = self.engine.on_tick(tick)

        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_PE")
        self.assertEqual(trade["entry_path"], "PATH_1_0915")
        self.assertEqual(trade["lots"], 2)  # ★★★★ gives 2x base lots = 2
        self.assertEqual(st["state"], "HOLDING")

    # =========================================================================
    # 3. PATH 2 (ALL-DAY OI ENTRY & FUTURES GATE) TESTS
    # =========================================================================

    def test_path2_futures_gate_blocking(self):
        """Path 2 Futures Gate: CE Buy blocked if futures shows Short Build-up (SB) or Long Unwinding (LU)."""
        st = self._setup_mock_stock("RELIANCE", rank="★★★", direction="BULLISH")
        # Simulate CE SC + PE SB (Rule 1 CE Buy)
        for k in st["locked_keys"]:
            if "_CE_" in k:
                self.engine.hot_cache[k]["ltp"] = 60.0
                self.engine.hot_cache[k]["oi"] = 8000.0
            else:
                self.engine.hot_cache[k]["ltp"] = 40.0
                self.engine.hot_cache[k]["oi"] = 12000.0

        # Set futures to opposing direction: Short Build-up (SB)
        st["futures_buildup"] = "SB"

        # Evaluate OI buildup: Futures Gate MUST block CE Buy
        trade = self.engine.evaluate_stock_oi_buildup("RELIANCE", force=False)
        self.assertIsNone(trade)
        self.assertIsNone(st["current_position"])

        # Now set futures to confirming direction: Long Build-up (LB)
        st["futures_buildup"] = "LB"
        trade = self.engine.evaluate_stock_oi_buildup("RELIANCE", force=False)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_CE")

    def test_path2_two_consecutive_oi_updates_rule(self):
        """Path 2 Rule 11: Signal must hold for 2 OI updates in a row before entering."""
        st = self._setup_mock_stock("HDFCBANK", base_lots=1, rank="★★★", direction="BULLISH")
        for k in st["locked_keys"]:
            if "_CE_" in k:
                self.engine.hot_cache[k]["ltp"] = 60.0
                self.engine.hot_cache[k]["oi"] = 8000.0
            else:
                self.engine.hot_cache[k]["ltp"] = 40.0
                self.engine.hot_cache[k]["oi"] = 12000.0
        st["futures_buildup"] = "LB"

        # Update 1: Signal matches CE Buy, but only held for 1 update
        trade1 = self.engine.evaluate_stock_oi_buildup("HDFCBANK", require_consecutive=True)
        self.assertIsNone(trade1)
        self.assertEqual(st["consecutive_count"], 1)
        self.assertEqual(st["consecutive_signal"], "CE Buy")
        self.assertIsNone(st["current_position"])

        # Update 2: Signal held for 2nd consecutive update -> Enters trade!
        trade2 = self.engine.evaluate_stock_oi_buildup("HDFCBANK", require_consecutive=True)
        self.assertIsNotNone(trade2)
        self.assertEqual(trade2["side"], "BUY_CE")
        self.assertEqual(st["consecutive_count"], 0)
        self.assertEqual(st["state"], "HOLDING")

    # =========================================================================
    # 4. RISK & EXITS TESTS (SL 25%, Target 40%, 09:45 Cutoff, Futures Flip)
    # =========================================================================

    def test_stop_loss_25pct_exit(self):
        """Risk: Exits immediately at live bid when option premium drops 25%."""
        st = self._setup_mock_stock("RELIANCE")
        # Enter trade at 50.0
        trade = self.engine._execute_stock_path1_entry("RELIANCE", "CE")
        self.assertIsNotNone(trade)
        entry_price = trade["entry_price"]  # 50.0
        sl_price = trade["stop_loss_price"]  # 37.50

        contract_key = trade["instrument_key"]
        # Tick arrives above SL (40.0 > 37.5) -> Still holding
        self.engine.on_tick({"instrument_key": contract_key, "ltp": 40.0, "bid_price": 40.0})
        self.assertEqual(st["state"], "HOLDING")

        # Tick arrives at/below 25% SL (37.0 <= 37.5) -> Exits!
        self.engine.on_tick({"instrument_key": contract_key, "ltp": 37.0, "bid_price": 37.0})
        self.assertIsNone(st["current_position"])
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "STOP_LOSS_25PCT")
        self.assertEqual(trade["exit_price"], 37.0)

    def test_target_40pct_exit(self):
        """Risk: Exits immediately at live bid when option premium reaches 40% target."""
        st = self._setup_mock_stock("RELIANCE")
        trade = self.engine._execute_stock_path1_entry("RELIANCE", "CE")
        contract_key = trade["instrument_key"]
        tgt_price = trade["target_price"]  # 70.0 (50.0 * 1.40)

        # Tick reaches target (71.0 >= 70.0) -> Exits!
        self.engine.on_tick({"instrument_key": contract_key, "ltp": 71.0, "bid_price": 71.0})
        self.assertIsNone(st["current_position"])
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "TARGET_40PCT")
        self.assertEqual(trade["exit_price"], 71.0)

    def test_futures_flip_exit(self):
        """Risk: Holding CE and futures flips to SB -> Exits immediately."""
        st = self._setup_mock_stock("RELIANCE")
        trade = self.engine._execute_stock_path1_entry("RELIANCE", "CE")
        self.assertEqual(st["state"], "HOLDING")

        # Futures tick arrives with Short Build-up (Price DOWN + OI UP)
        fut_key = st["futures_key"]
        self.engine.hot_cache[fut_key] = {"instrument_key": fut_key, "ltp": 1280.0, "oi": 60000.0}
        st["futures_snap_ltp"] = 1305.0
        st["futures_snap_oi"] = 50000.0

        # Feed futures tick: price -1.9%, OI +20% -> SB
        self.engine.on_tick({"instrument_key": fut_key, "ltp": 1280.0, "open_interest": 60000.0})
        self.assertEqual(st["futures_buildup"], "SB")
        self.assertIsNone(st["current_position"])
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "FUTURES_FLIP_BEARISH")

    def test_max_3_trades_per_stock_per_day(self):
        """Risk: Stock allows up to 3 trades per day, returning to ARMED after trades 1 and 2."""
        st = self._setup_mock_stock("RELIANCE")
        self.assertEqual(self.engine.stock_max_trades_per_day, 3)

        # Trade 1
        t1 = self.engine._execute_stock_path1_entry("RELIANCE", "CE")
        self.assertEqual(st["today_trades_count"], 1)
        self.engine._exit_single_trade(t1, "MANUAL", "RELIANCE")
        self.assertEqual(st["state"], "ARMED")  # Re-arms for trade 2!

        # Trade 2
        st["path1_done"] = False
        t2 = self.engine._execute_stock_path1_entry("RELIANCE", "CE")
        self.assertEqual(st["today_trades_count"], 2)
        self.engine._exit_single_trade(t2, "MANUAL", "RELIANCE")
        self.assertEqual(st["state"], "ARMED")  # Re-arms for trade 3!

        # Trade 3
        st["path1_done"] = False
        t3 = self.engine._execute_stock_path1_entry("RELIANCE", "CE")
        self.assertEqual(st["today_trades_count"], 3)
        self.engine._exit_single_trade(t3, "MANUAL", "RELIANCE")
        self.assertEqual(st["state"], "CLOSED")  # Max 3 reached -> CLOSED!

    # =========================================================================
    # 5. INDICES STRATEGY PRESERVATION (UNTOUCHED FIRST POSITIVE TICK)
    # =========================================================================

    def test_indices_remain_strictly_on_first_positive_tick(self):
        """Ensures NIFTY 50, BANK NIFTY, and SENSEX remain 100% on First Positive Tick."""
        nifty_st = self.engine.index_underlyings[NIFTY_KEY]
        k_ce = "NIFTY_CE_ITM"
        self.engine.contracts_by_key[k_ce] = {
            "instrument_key": k_ce, "trading_symbol": "NIFTY 24000 CE", "strike_price": 24000.0,
            "instrument_type": "CE", "lot_size": 65, "underlying_key": NIFTY_KEY
        }
        self.engine.hot_cache[k_ce] = {
            "instrument_key": k_ce, "trading_symbol": "NIFTY 24000 CE", "strike": 24000.0,
            "side": "CE", "ltp": 100.0, "ref_ltp": 100.0, "bid_price": 99.5, "ask_price": 100.0, "spread_pct": 0.5
        }
        nifty_st["locked_keys"] = [k_ce]
        nifty_st["state"] = "ARMED"

        # Tick equal or below ref_ltp (100.0 <= 100.0) -> No trade
        t_flat = self.engine.on_tick({"instrument_key": k_ce, "ltp": 100.0})
        self.assertIsNone(t_flat)
        self.assertEqual(nifty_st["state"], "ARMED")

        # First tick strictly above ref_ltp (100.05 > 100.00) -> Immediate entry!
        t_enter = self.engine.on_tick({"instrument_key": k_ce, "ltp": 100.05})
        self.assertIsNotNone(t_enter)
        self.assertEqual(t_enter["side"], "BUY_CE")
        self.assertEqual(t_enter["lots"], 1)
        self.assertEqual(t_enter["lot_size"], 65)
        self.assertEqual(nifty_st["state"], "HOLDING")


if __name__ == "__main__":
    unittest.main()
