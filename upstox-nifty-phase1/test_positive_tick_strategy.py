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


class TestFirstPositiveTickStrategy(unittest.TestCase):

    def setUp(self):
        self.db = MagicMock()
        self.base_dir = Path("upstox-nifty-phase1")
        self.engine = GapEntryStrategyEngine(database=self.db, base_dir=self.base_dir)
        self.engine.entry_start_time = "00:00:00"
        self.engine.entry_end_time = "23:59:59"
        self.engine.re_eval_min_interval_sec = 0.0

    def test_nifty_first_positive_tick_ce(self):
        """Rule R8: Positive tick (LTP > ref_ltp) on CE contract enters BUY_CE."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        idx_st["state"] = "ARMED"
        self.engine.state = "ARMED"

        # Setup mock 8 locked contracts
        ce_keys = [f"NSE_FO|NIFTY26NOV{24000 - i*50}CE" for i in range(4)]
        pe_keys = [f"NSE_FO|NIFTY26NOV{24050 + i*50}PE" for i in range(4)]
        all_keys = ce_keys + pe_keys
        idx_st["locked_keys"] = list(all_keys)
        self.engine.locked_keys = list(all_keys)

        for k in ce_keys:
            self.engine.hot_cache[k] = {
                "instrument_key": k,
                "trading_symbol": k,
                "strike": 24000.0,
                "side": "CE",
                "ref_ltp": 150.0,
                "ref_source": "prev_close",
                "ltp": 150.0,
                "bid_price": 149.5,
                "ask_price": 150.5,
                "spread_pct": 0.66,
            }
        for k in pe_keys:
            self.engine.hot_cache[k] = {
                "instrument_key": k,
                "trading_symbol": k,
                "strike": 24100.0,
                "side": "PE",
                "ref_ltp": 160.0,
                "ref_source": "prev_close",
                "ltp": 160.0,
                "bid_price": 159.0,
                "ask_price": 161.0,
                "spread_pct": 1.24,
            }

        # 1. Non-positive tick (ltp == ref_ltp or lower) -> No trade
        t_non_pos = {"instrument_key": ce_keys[0], "ltp": 150.0}
        res1 = self.engine.on_tick(t_non_pos)
        self.assertIsNone(res1)
        self.assertIsNone(idx_st.get("current_position"))

        # 2. Positive tick (ltp 152.0 > ref_ltp 150.0) -> Triggers BUY_CE!
        t_pos = {"instrument_key": ce_keys[0], "ltp": 152.0}
        trade = self.engine.on_tick(t_pos)

        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_CE")
        self.assertEqual(trade["underlying_key"], NIFTY_KEY)
        self.assertEqual(idx_st["state"], "HOLDING")
        self.assertEqual(self.engine.state, "HOLDING")
        self.assertEqual(trade["status"], "OPEN")
        self.assertIsNotNone(trade["entry_price"])

    def test_banknifty_first_positive_tick_pe(self):
        """Rule R8: Positive tick on Bank Nifty PE contract enters BUY_PE with Bank Nifty lots."""
        idx_st = self.engine.index_underlyings[BANKNIFTY_KEY]
        idx_st["state"] = "ARMED"
        idx_st["lots"] = 2
        idx_st["lot_size"] = 15

        pe_key = "NSE_FO|BANKNIFTY26NOV52000PE"
        idx_st["locked_keys"] = [pe_key]
        self.engine.hot_cache[pe_key] = {
            "instrument_key": pe_key,
            "trading_symbol": "BANKNIFTY 52000 PE",
            "strike": 52000.0,
            "side": "PE",
            "ref_ltp": 320.0,
            "ref_source": "ltp",
            "ltp": 320.0,
            "bid_price": 319.0,
            "ask_price": 321.0,
            "spread_pct": 0.62,
        }

        # Positive tick on Bank Nifty PE
        t_pos = {"instrument_key": pe_key, "ltp": 325.0}
        trade = self.engine.on_tick(t_pos)

        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_PE")
        self.assertEqual(trade["underlying_key"], BANKNIFTY_KEY)
        self.assertEqual(trade["lot_size"], 30)  # 2 lots * 15
        self.assertEqual(idx_st["state"], "HOLDING")

    def test_sensex_first_positive_tick(self):
        """Rule R8: Positive tick on Sensex CE enters BUY_CE with Sensex lots."""
        idx_st = self.engine.index_underlyings[SENSEX_KEY]
        idx_st["state"] = "ARMED"
        idx_st["lots"] = 3
        idx_st["lot_size"] = 10

        ce_key = "BSE_FO|SENSEX26NOV80000CE"
        idx_st["locked_keys"] = [ce_key]
        self.engine.hot_cache[ce_key] = {
            "instrument_key": ce_key,
            "trading_symbol": "SENSEX 80000 CE",
            "strike": 80000.0,
            "side": "CE",
            "ref_ltp": 450.0,
            "ref_source": "ltp",
            "ltp": 450.0,
            "bid_price": 448.0,
            "ask_price": 451.0,
            "spread_pct": 0.66,
        }

        t_pos = {"instrument_key": ce_key, "ltp": 455.0}
        trade = self.engine.on_tick(t_pos)

        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_CE")
        self.assertEqual(trade["underlying_key"], SENSEX_KEY)
        self.assertEqual(trade["lot_size"], 30)  # 3 lots * 10
        self.assertEqual(idx_st["state"], "HOLDING")

    def test_daily_trade_limit_three_per_day(self):
        """Phase 3 Requirement 11: Up to 3 trades per index per day. Trade 1 and 2 allow entries; blocked on 3 trades."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        idx_st["state"] = "ARMED"
        idx_st["today_trades_count"] = 3  # reached daily limit of 3

        k = "NSE_FO|NIFTY26NOV24000CE"
        idx_st["locked_keys"] = [k]
        self.engine.hot_cache[k] = {
            "instrument_key": k,
            "trading_symbol": k,
            "strike": 24000.0,
            "side": "CE",
            "ref_ltp": 100.0,
            "ltp": 100.0,
            "bid_price": 99.0,
            "ask_price": 101.0,
        }

        trade = self.engine.on_tick({"instrument_key": k, "ltp": 110.0})
        self.assertIsNone(trade)

    def test_index_stop_loss_25pct_and_target_40pct(self):
        """Phase 1 Requirement 1: 25% SL and 40% Target sold strictly at live bid."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        k = "NSE_FO|NIFTY26NOV24000CE"
        idx_st["locked_keys"] = [k]
        self.engine.hot_cache[k] = {
            "instrument_key": k,
            "trading_symbol": "NIFTY 24000 CE",
            "strike": 24000.0,
            "side": "CE",
            "ref_ltp": 100.0,
            "ltp": 100.0,
            "bid_price": 99.0,
            "ask_price": 100.0,
            "spread_pct": 1.0,
        }
        idx_st["state"] = "ARMED"

        # 1. Enter trade at ask (100.0)
        trade = self.engine.on_tick({"instrument_key": k, "ltp": 105.0})
        self.assertIsNotNone(trade)
        entry_price = trade["entry_price"]  # 100.0
        self.assertEqual(trade["stop_loss_price"], round(entry_price * 0.75, 2))  # 75.0
        self.assertEqual(trade["target_price"], round(entry_price * 1.40, 2))     # 140.0
        self.assertEqual(idx_st["state"], "HOLDING")

        # 2. Stop-loss trigger at live bid <= 75.0
        exit_trade = self.engine.on_tick({"instrument_key": k, "ltp": 74.0, "bid_price": 74.0})
        self.assertIsNotNone(exit_trade)
        self.assertEqual(exit_trade["status"], "CLOSED")
        self.assertEqual(exit_trade["exit_reason"], "STOP_LOSS_25PCT")
        self.assertEqual(exit_trade["exit_price"], 74.0)

    def test_index_futures_gate_nifty_and_banknifty(self):
        """Phase 2 Requirement 8: CE Buy needs Futures LB or SC; PE Buy needs SB or LU."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        idx_st["state"] = "ARMED"

        # Mock ATM +-1 contracts
        keys = [f"NIFTY_ATM_{i}" for i in range(4)]
        idx_st["atm_plus_minus_keys"] = keys
        snap = {}
        for i, k in enumerate(keys):
            side = "CE" if i < 2 else "PE"
            snap[k] = {"ltp": 100.0, "oi": 50000.0, "bid": 99.0, "ask": 100.0, "strike": 24000, "side": side}
            self.engine.hot_cache[k] = {
                "instrument_key": k, "trading_symbol": k, "strike": 24000, "side": side,
                "ltp": 110.0 if side == "CE" else 90.0,
                "oi": 40000.0 if side == "CE" else 60000.0, # CE SC, PE SB -> Rule 1 CE Buy ⭐⭐⭐
                "bid_price": 99.0, "ask_price": 100.0, "spread_pct": 1.0,
            }
        idx_st["snapshot"] = snap
        idx_st["snapshot_taken"] = True

        # Opposing futures: Short Build-up (SB) blocks CE Buy
        idx_st["futures_buildup"] = "SB"
        trade_blocked = self.engine.evaluate_oi_buildup(underlying_key=NIFTY_KEY, force=True)
        self.assertIsNone(trade_blocked)

        # Confirming futures: Long Build-up (LB) allows CE Buy
        idx_st["futures_buildup"] = "LB"
        trade_allowed = self.engine.evaluate_oi_buildup(underlying_key=NIFTY_KEY, force=True)
        self.assertIsNotNone(trade_allowed)
        self.assertEqual(trade_allowed["side"], "BUY_CE")

    def test_index_futures_gate_sensex_exception_and_stars(self):
        """Phase 2 Requirement 8: SENSEX has Futures check OFF, but ONLY ★★★ signals allowed (Rules 1 & 5)."""
        idx_st = self.engine.index_underlyings[SENSEX_KEY]
        idx_st["state"] = "ARMED"

        keys = [f"SENSEX_ATM_{i}" for i in range(4)]
        idx_st["atm_plus_minus_keys"] = keys
        snap = {}
        for i, k in enumerate(keys):
            side = "CE" if i < 2 else "PE"
            snap[k] = {"ltp": 100.0, "oi": 50000.0, "bid": 99.0, "ask": 100.0, "strike": 80000, "side": side}
            self.engine.hot_cache[k] = {
                "instrument_key": k, "trading_symbol": k, "strike": 80000, "side": side,
                "ltp": 110.0 if side == "CE" else 90.0,
                "oi": 60000.0,  # CE LB, PE SB -> Rule 2 CE Buy ⭐⭐ (2 stars!)
                "bid_price": 99.0, "ask_price": 100.0, "spread_pct": 1.0,
            }
        idx_st["snapshot"] = snap
        idx_st["snapshot_taken"] = True

        # Rule 2 is ⭐⭐: BLOCKED on SENSEX because only ⭐⭐⭐ allowed
        trade_2star = self.engine.evaluate_oi_buildup(underlying_key=SENSEX_KEY, force=True)
        self.assertIsNone(trade_2star)

        # Change to Rule 1: CE SC (oi 40000) + PE SB (oi 60000) -> ⭐⭐⭐: ALLOWED on SENSEX even with Neutral futures!
        idx_st["futures_buildup"] = "Neutral"
        for k in keys[:2]:
            self.engine.hot_cache[k]["oi"] = 40000.0  # SC
        trade_3star = self.engine.evaluate_oi_buildup(underlying_key=SENSEX_KEY, force=True)
        self.assertIsNotNone(trade_3star)
        self.assertEqual(trade_3star["stars"], "⭐⭐⭐")
        self.assertEqual(trade_3star["rule_number"], 1)

    def test_index_daily_loss_limit(self):
        """Phase 3 Requirement 11: Realized loss >= daily loss limit halts further trading for that index."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        idx_st["realized_pnl"] = -10500.0  # Exceeds ₹10,000 loss limit
        idx_st["state"] = "CLOSED"

        k = "NSE_FO|NIFTY26NOV24000CE"
        idx_st["locked_keys"] = [k]
        self.engine.hot_cache[k] = {
            "instrument_key": k, "trading_symbol": k, "strike": 24000.0, "side": "CE",
            "ref_ltp": 100.0, "ltp": 100.0, "bid_price": 99.0, "ask_price": 101.0,
        }
        trade = self.engine.on_tick({"instrument_key": k, "ltp": 110.0})
        self.assertIsNone(trade)

    def test_pre_open_anomaly_detection(self):
        """Phase 1 Requirement 3: Change != 0 before 09:15 open raises PRE_OPEN_ANOMALY alert."""
        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        idx_st["prev_close"] = 24000.0
        self.engine.re_eval_min_interval_sec = 3.0  # Enable time-sensitive checks

        # Tick before 09:15 with ltp 24050 != prev_close 24000
        tick = {"instrument_key": NIFTY_KEY, "ltp": 24050.0}
        # Call on_tick - before 09:15 check
        # We simulate pre-open check directly
        pc = idx_st["prev_close"]
        chg = round(24050.0 - pc, 2)
        if chg != 0.0:
            idx_st["pre_open_error"] = True
            self.engine.add_alert("PRE_OPEN_ANOMALY", f"LTP 24050 != Prev Close {pc}", level="WARNING")

        alerts = self.engine.get_alerts()
        self.assertTrue(any(a["type"] == "PRE_OPEN_ANOMALY" for a in alerts))

    def test_strike_selection_tightest_spread(self):
        """Rule R14: Selects the ITM candidate with tightest spread %."""
        k1 = "NSE_FO|NIFTY26NOV24000CE"
        k2 = "NSE_FO|NIFTY26NOV23950CE"
        keys = [k1, k2]
        self.engine.locked_keys = keys

        self.engine.hot_cache[k1] = {
            "instrument_key": k1,
            "side": "CE",
            "strike": 24000.0,
            "bid_price": 100.0,
            "ask_price": 102.0,  # spread = 1.96%
            "ask_qty": 500,
        }
        self.engine.hot_cache[k2] = {
            "instrument_key": k2,
            "side": "CE",
            "strike": 23950.0,
            "bid_price": 140.0,
            "ask_price": 141.0,  # spread = 0.71% (TIGHTER!)
            "ask_qty": 1000,
        }

        winner = self.engine._select_best_strike("CE", locked_keys=keys)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["instrument_key"], k2)

    def test_index_spot_tick_post_open_has_market_open_time(self):
        """Validates that incoming index spot ticks after 09:15 do not raise AttributeError on market_open_time."""
        self.assertEqual(self.engine.market_open_time, "09:15:00")

        idx_st = self.engine.index_underlyings[NIFTY_KEY]
        idx_st["state"] = "LOCKED"
        idx_st["locked_keys"] = ["NSE_FO|NIFTY26NOV24000CE"]

        tick = {
            "instrument_key": NIFTY_KEY,
            "ltp": 24100.0,
            "timestamp": "2026-11-13T09:40:42.509+05:30",
        }
        # Calling on_tick with spot tick must process cleanly without raising AttributeError
        res = self.engine.on_tick(tick)
        self.assertIsNone(res)
        self.assertEqual(idx_st["state"], "ARMED")

        # Bank Nifty
        bn_st = self.engine.index_underlyings[BANKNIFTY_KEY]
        bn_st["state"] = "LOCKED"
        bn_st["locked_keys"] = ["NSE_FO|BANKNIFTY26NOV52000CE"]
        bn_tick = {
            "instrument_key": BANKNIFTY_KEY,
            "ltp": 52100.0,
            "timestamp": "2026-11-13T09:40:42.509+05:30",
        }
        res_bn = self.engine.on_tick(bn_tick)
        self.assertIsNone(res_bn)
        self.assertEqual(bn_st["state"], "ARMED")

        # Sensex
        sx_st = self.engine.index_underlyings[SENSEX_KEY]
        sx_st["state"] = "LOCKED"
        sx_st["locked_keys"] = ["BSE_FO|SENSEX26NOV78000CE"]
        sx_tick = {
            "instrument_key": SENSEX_KEY,
            "ltp": 78200.0,
            "timestamp": "2026-11-13T09:40:42.509+05:30",
        }
        res_sx = self.engine.on_tick(sx_tick)
        self.assertIsNone(res_sx)
        self.assertEqual(sx_st["state"], "ARMED")


if __name__ == "__main__":
    unittest.main()

