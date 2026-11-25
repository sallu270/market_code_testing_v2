from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from strategy_engine import (
    GapEntryStrategyEngine,
    NIFTY_KEY,
    BANKNIFTY_KEY,
    SENSEX_KEY,
)


class TestSumSignalStrategy(unittest.TestCase):

    def setUp(self):
        self.db = MagicMock()
        self.base_dir = Path("upstox-nifty-phase1")
        self.engine = GapEntryStrategyEngine(database=self.db, base_dir=self.base_dir)
        self.engine.entry_start_time = "09:18:00"
        self.engine.entry_end_time = "14:45:00"
        self.engine.re_eval_min_interval_sec = 0.0

        # Setup mock ATM +-5 strikes for NIFTY (spot = 24000, step = 50)
        # 11 CE strikes: 23750 to 24250
        # 11 PE strikes: 23750 to 24250
        self.spot = 24000.0
        self.ce_keys = [f"NSE_FO|NIFTY26NOV{23750 + i*50}CE" for i in range(11)]
        self.pe_keys = [f"NSE_FO|NIFTY26NOV{23750 + i*50}PE" for i in range(11)]

        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        idx_st["spot_price"] = self.spot
        idx_st["ce_basket_keys"] = list(self.ce_keys)
        idx_st["pe_basket_keys"] = list(self.pe_keys)
        idx_st["locked_keys"] = list(self.ce_keys + self.pe_keys)
        idx_st["whipsaw_sec"] = 300.0  # 5 minutes

        # Populate hot cache with baseline prices = 100.0 each (Baseline sum = 1100.0 each)
        for k in self.ce_keys:
            strike = float(k.split("CE")[0].split("NOV")[-1])
            self.engine.hot_cache[k] = {
                "instrument_key": k,
                "trading_symbol": k,
                "strike": strike,
                "side": "CE",
                "underlying_key": NIFTY_KEY,
                "ltp": 100.0,
                "bid_price": 99.5,
                "ask_price": 100.0,
                "spread_pct": 0.5,
            }

        for k in self.pe_keys:
            strike = float(k.split("PE")[0].split("NOV")[-1])
            self.engine.hot_cache[k] = {
                "instrument_key": k,
                "trading_symbol": k,
                "strike": strike,
                "side": "PE",
                "underlying_key": NIFTY_KEY,
                "ltp": 100.0,
                "bid_price": 99.5,
                "ask_price": 100.0,
                "spread_pct": 0.5,
            }

        # Lock baseline sums
        idx_st["ce_baseline_sum"] = 1100.0
        idx_st["pe_baseline_sum"] = 1100.0
        idx_st["baseline_locked"] = True

    def test_sum_signal_bullish_calculation(self):
        """Bullish: CE % > +2.0% AND PE % < -2.0% produces raw_signal 'CE Buy'."""
        # Increase CE prices by 5% (sum becomes 1155.0, +5.0%)
        for k in self.ce_keys:
            self.engine.hot_cache[k]["ltp"] = 105.0

        # Decrease PE prices by 5% (sum becomes 1045.0, -5.0%)
        for k in self.pe_keys:
            self.engine.hot_cache[k]["ltp"] = 95.0

        eval_res = self.engine.evaluate_sum_signal(underlying_key=NIFTY_KEY, force=True)
        idx_st = self.engine.index_underlyings[NIFTY_KEY]

        self.assertEqual(idx_st["ce_current_sum"], 1155.0)
        self.assertEqual(idx_st["pe_current_sum"], 1045.0)
        self.assertEqual(idx_st["ce_pct"], 5.0)
        self.assertEqual(idx_st["pe_pct"], -5.0)
        self.assertEqual(idx_st["score"], 10.0)  # +5 - (-5) = +10.0%
        self.assertEqual(idx_st["raw_signal"], "CE Buy")

    def test_sum_signal_bearish_calculation(self):
        """Bearish: PE % > +2.0% AND CE % < -2.0% produces raw_signal 'PE Buy'."""
        # Decrease CE prices by 4% (sum becomes 1056.0, -4.0%)
        for k in self.ce_keys:
            self.engine.hot_cache[k]["ltp"] = 96.0

        # Increase PE prices by 4% (sum becomes 1144.0, +4.0%)
        for k in self.pe_keys:
            self.engine.hot_cache[k]["ltp"] = 104.0

        eval_res = self.engine.evaluate_sum_signal(underlying_key=NIFTY_KEY, force=True)
        idx_st = self.engine.index_underlyings[NIFTY_KEY]

        self.assertEqual(idx_st["ce_pct"], -4.0)
        self.assertEqual(idx_st["pe_pct"], 4.0)
        self.assertEqual(idx_st["score"], -8.0)  # -4 - (+4) = -8.0%
        self.assertEqual(idx_st["raw_signal"], "PE Buy")

    def test_sum_signal_neutral_zone(self):
        """Neutral: within +-2.0% buffer produces 'Neutral' raw_signal."""
        for k in self.ce_keys:
            self.engine.hot_cache[k]["ltp"] = 101.0  # +1.0%

        for k in self.pe_keys:
            self.engine.hot_cache[k]["ltp"] = 99.0   # -1.0%

        eval_res = self.engine.evaluate_sum_signal(underlying_key=NIFTY_KEY, force=True)
        idx_st = self.engine.index_underlyings[NIFTY_KEY]

        self.assertEqual(idx_st["raw_signal"], "Neutral")
        self.assertEqual(idx_st["score"], 2.0)

    def test_whipsaw_5min_continuous_hold(self):
        """Whipsaw Filter: Signal must hold continuously for 300s before confirming."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]

        # Trigger bullish raw signal
        for k in self.ce_keys:
            self.engine.hot_cache[k]["ltp"] = 105.0
        for k in self.pe_keys:
            self.engine.hot_cache[k]["ltp"] = 95.0

        self.engine.evaluate_sum_signal(underlying_key=NIFTY_KEY, force=True)
        self.assertEqual(idx_st["raw_signal"], "CE Buy")
        self.assertIsNone(idx_st["confirmed_signal"])

        # Simulate 100 seconds elapsed (not yet confirmed)
        idx_st["candidate_start_time"] = idx_st["candidate_start_time"] - 100.0
        self.engine.evaluate_sum_signal(underlying_key=NIFTY_KEY, force=True)
        self.assertGreaterEqual(idx_st["hold_elapsed_sec"], 100.0)
        self.assertIsNone(idx_st["confirmed_signal"])

        # If signal drops to Neutral at 200s, timer resets to 0
        for k in self.ce_keys:
            self.engine.hot_cache[k]["ltp"] = 100.0
        for k in self.pe_keys:
            self.engine.hot_cache[k]["ltp"] = 100.0

        self.engine.evaluate_sum_signal(underlying_key=NIFTY_KEY, force=True)
        self.assertEqual(idx_st["raw_signal"], "Neutral")
        self.assertEqual(idx_st["hold_elapsed_sec"], 0.0)
        self.assertIsNone(idx_st["candidate_signal"])

        # Signal re-triggers and completes full 300s
        for k in self.ce_keys:
            self.engine.hot_cache[k]["ltp"] = 105.0
        for k in self.pe_keys:
            self.engine.hot_cache[k]["ltp"] = 95.0

        self.engine.evaluate_sum_signal(underlying_key=NIFTY_KEY, force=True)
        idx_st["candidate_start_time"] = idx_st["candidate_start_time"] - 301.0
        trade = self.engine.evaluate_sum_signal(underlying_key=NIFTY_KEY, force=True)

        self.assertEqual(idx_st["confirmed_signal"], "CE Buy")
        self.assertIsNotNone(trade)
        self.assertEqual(trade["entry_path"], "SUM_SIGNAL")
        self.assertEqual(trade["side"], "BUY_CE")
        self.assertEqual(trade["stop_loss_price"], round(trade["entry_price"] * 0.80, 2))  # -20% SL
        self.assertEqual(trade["target_price"], round(trade["entry_price"] * 1.40, 2))     # +40% Target

    def test_stepped_trailing_stop_loss(self):
        """Stepped Trailing SL: Moves to cost at +5% profit, raises by +2% for every further +3% profit."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        trade = self.engine._enter_sum_signal_trade(underlying_key=NIFTY_KEY, side="CE")
        self.assertIsNotNone(trade)
        entry_p = trade["entry_price"]
        self.assertEqual(trade["stop_loss_price"], round(entry_p * 0.80, 2))

        atm_key = trade["instrument_key"]

        # 1. At +4% profit: SL remains at -20% (entry * 0.80)
        self.engine.on_tick({"instrument_key": atm_key, "ltp": entry_p * 1.04, "bid_price": entry_p * 1.04})
        self.assertEqual(trade["stop_loss_price"], round(entry_p * 0.80, 2))

        # 2. At +5% profit: SL moves to Cost (Break-even: 1.0 * entry_p)
        self.engine.on_tick({"instrument_key": atm_key, "ltp": entry_p * 1.05, "bid_price": entry_p * 1.05})
        self.assertEqual(trade["stop_loss_price"], round(entry_p * 1.00, 2))
        self.assertTrue(trade["trailing_active"])

        # 3. At +8% profit (+3% further): SL raises by +2% (1.02 * entry_p)
        self.engine.on_tick({"instrument_key": atm_key, "ltp": entry_p * 1.08, "bid_price": entry_p * 1.08})
        self.assertEqual(trade["stop_loss_price"], round(entry_p * 1.02, 2))

        # 4. At +11% profit (+3% further): SL raises by +2% (1.04 * entry_p)
        self.engine.on_tick({"instrument_key": atm_key, "ltp": entry_p * 1.11, "bid_price": entry_p * 1.11})
        self.assertEqual(trade["stop_loss_price"], round(entry_p * 1.04, 2))

        # 5. Price pulls back to 1.03 (below trailing SL of 1.04): Exits on TRAILING_STOP_LOSS
        exit_trade = self.engine.on_tick({"instrument_key": atm_key, "ltp": entry_p * 1.03, "bid_price": entry_p * 1.03})
        self.assertIsNotNone(exit_trade)
        self.assertEqual(exit_trade["status"], "CLOSED")
        self.assertEqual(exit_trade["exit_reason"], "TRAILING_STOP_LOSS")

    def test_confirmed_reversal_flips_trade(self):
        """Confirmed Reversal: Opposite signal after 5 minutes closes active trade and flips position."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]

        # Enter CE trade
        ce_trade = self.engine._enter_sum_signal_trade(underlying_key=NIFTY_KEY, side="CE")
        self.assertEqual(idx_st["current_position"]["side"], "BUY_CE")
        ce_key = ce_trade["instrument_key"]

        # Signal flips to confirmed PE Buy
        idx_st["confirmed_signal"] = "PE Buy"

        # Ticking the held contract triggers immediate reversal
        res = self.engine.on_tick({"instrument_key": ce_key, "ltp": 98.0, "bid_price": 98.0})
        self.assertIsNotNone(res)
        self.assertEqual(res["status"], "CLOSED")
        self.assertEqual(res["exit_reason"], "SIGNAL_REVERSAL_PE")

        # New active position is flipped to PE Buy!
        new_pos = idx_st.get("current_position")
        self.assertIsNotNone(new_pos)
        self.assertEqual(new_pos["side"], "BUY_PE")


if __name__ == "__main__":
    unittest.main()
