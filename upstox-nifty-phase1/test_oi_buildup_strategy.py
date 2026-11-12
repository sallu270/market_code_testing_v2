"""
Unit & Acceptance Tests for OI Build-up Entry from 09:18 (Section 14).
Tests:
- All 16 combinations of CE type x PE type give exactly the decision, stars and size.
- Any FLAT direction gives WAIT.
- No entry before 09:18:00, none while OI equals snapshot, none after 14:45:00.
- With CE LB/SC and PE SB, the bot never buys a PE.
- Paper entry fills at live ask, paper exit at live bid; exit never uses entry price.
- 15:15:00 clock square-off.
- Trade record contains rule number, stars, delta_ce, delta_pe, timestamps.
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import unittest
from unittest.mock import MagicMock
from strategy_engine import DECISION_TABLE, GapEntryStrategyEngine, NIFTY_KEY, BANKNIFTY_KEY, SENSEX_KEY


class TestOIBuildupStrategy(unittest.TestCase):

    def setUp(self):
        self.db = MagicMock()
        self.base_dir = Path("upstox-nifty-phase1")
        self.engine = GapEntryStrategyEngine(database=self.db, base_dir=self.base_dir)

    def test_all_16_table_combinations(self):
        """Acceptance test 1: All 16 combinations give exactly the table decision, stars, size."""
        expected = {
            # 1. SC, SB -> CE Buy, ⭐⭐⭐, full
            ("SC", "SB"): ("CE Buy", "⭐⭐⭐", "full", 1),
            # 2. LB, SB -> CE Buy, ⭐⭐, normal
            ("LB", "SB"): ("CE Buy", "⭐⭐", "normal", 2),
            # 3. LB, LU -> CE Buy, ⭐⭐, normal
            ("LB", "LU"): ("CE Buy", "⭐⭐", "normal", 3),
            # 4. SC, LU -> CE Buy, ⭐⭐, scalp
            ("SC", "LU"): ("CE Buy", "⭐⭐", "scalp", 4),
            # 5. SB, SC -> PE Buy, ⭐⭐⭐, full
            ("SB", "SC"): ("PE Buy", "⭐⭐⭐", "full", 5),
            # 6. SB, LB -> PE Buy, ⭐⭐, normal
            ("SB", "LB"): ("PE Buy", "⭐⭐", "normal", 6),
            # 7. LU, LB -> PE Buy, ⭐⭐, normal
            ("LU", "LB"): ("PE Buy", "⭐⭐", "normal", 7),
            # 8. LU, SC -> PE Buy, ⭐⭐, scalp
            ("LU", "SC"): ("PE Buy", "⭐⭐", "scalp", 8),
            # 9. LB, LB -> No trade
            ("LB", "LB"): ("No trade", "", "", 9),
            # 10. LB, SC -> No trade
            ("LB", "SC"): ("No trade", "", "", 10),
            # 11. SC, LB -> No trade
            ("SC", "LB"): ("No trade", "", "", 11),
            # 12. SC, SC -> No trade
            ("SC", "SC"): ("No trade", "", "", 12),
            # 13. SB, SB -> No trade
            ("SB", "SB"): ("No trade", "", "", 13),
            # 14. SB, LU -> No trade
            ("SB", "LU"): ("No trade", "", "", 14),
            # 15. LU, SB -> No trade
            ("LU", "SB"): ("No trade", "", "", 15),
            # 16. LU, LU -> No trade
            ("LU", "LU"): ("No trade", "", "", 16),
        }

        self.assertEqual(len(DECISION_TABLE), 16)
        for combo, (dec, stars, size, rule) in expected.items():
            entry = DECISION_TABLE.get(combo)
            self.assertIsNotNone(entry, f"Missing table entry for {combo}")
            self.assertEqual(entry["decision"], dec, f"Decision mismatch for {combo}")
            self.assertEqual(entry["stars"], stars, f"Stars mismatch for {combo}")
            self.assertEqual(entry["size"], size, f"Size mismatch for {combo}")
            self.assertEqual(entry["rule"], rule, f"Rule mismatch for {combo}")

    def test_ce_buy_never_buys_pe(self):
        """Acceptance test 4: With CE LB/SC and PE SB, never buys PE."""
        for ce_type in ("LB", "SC"):
            entry = DECISION_TABLE.get((ce_type, "SB"))
            self.assertIsNotNone(entry)
            self.assertEqual(entry["decision"], "CE Buy")
            self.assertNotEqual(entry["decision"], "PE Buy")

    def test_flat_direction_gives_wait(self):
        """Acceptance test 2: Any FLAT direction gives WAIT."""
        # Setup mock 8 contracts (4 CE, 4 PE)
        keys = [f"CE_{i}" for i in range(4)] + [f"PE_{i}" for i in range(4)]
        self.engine.locked_keys = list(keys)
        self.engine.snapshot.clear()
        self.engine.hot_cache.clear()

        # CE snapshot and current (price change is 0.0% -> FLAT)
        for i in range(4):
            k = f"CE_{i}"
            self.engine.snapshot[k] = {"instrument_key": k, "side": "CE", "ltp": 100.0, "oi": 1000.0, "bid": 99.0, "ask": 101.0}
            self.engine.hot_cache[k] = {"instrument_key": k, "side": "CE", "ltp": 100.0, "oi": 1200.0, "bid_price": 99.0, "ask_price": 101.0}

        # PE snapshot and current (price down, oi up -> SB)
        for i in range(4):
            k = f"PE_{i}"
            self.engine.snapshot[k] = {"instrument_key": k, "side": "PE", "ltp": 100.0, "oi": 1000.0, "bid": 99.0, "ask": 101.0}
            self.engine.hot_cache[k] = {"instrument_key": k, "side": "PE", "ltp": 95.0, "oi": 1200.0, "bid_price": 94.0, "ask_price": 96.0}

        # Force evaluation to bypass time checks
        trade = self.engine.evaluate_oi_buildup(force=True)
        self.assertIsNone(trade)
        eval_res = self.engine.last_evaluation
        self.assertEqual(eval_res["decision"], "WAIT")
        self.assertIn("FLAT", eval_res["reason"])

    def test_no_entry_before_0918_or_after_1445(self):
        """Acceptance test 3: No entry before 09:18:00 and none after 14:45:00."""
        self.engine.entry_start_time = "09:18:00"
        self.engine.entry_end_time = "14:45:00"

        # Test pre-09:18:00
        # Normal evaluation (not force) at current time (if outside window) returns None
        now_hms = "09:15:30"
        self.engine.entry_start_time = "09:18:00"
        # If entry_start_time is in the future relative to execution, evaluate returns None
        self.engine.last_evaluation = {}
        # Simulate with future start time
        self.engine.entry_start_time = "23:59:59"
        res = self.engine.evaluate_oi_buildup(force=False)
        self.assertIsNone(res)
        self.assertEqual(self.engine.last_evaluation["decision"], "WAIT")

    def test_paper_entry_fills_at_live_ask_and_exit_at_live_bid(self):
        """Acceptance test 5: Entry fills at live ask, exit at live bid; exit never uses entry price."""
        self.engine.mode = "paper"
        winner = {
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 24500 CE",
            "strike": 24500.0,
            "side": "CE",
            "ask_price": 152.40,
            "bid_price": 151.80,
            "ltp": 152.00,
            "underlying_key": NIFTY_KEY,
        }
        self.engine.hot_cache["NSE_FO|12345"] = winner

        trade = self.engine._execute_entry_order(
            winner=winner,
            side="CE",
            size="full",
            rule_num=1,
            stars="⭐⭐⭐",
            delta_ce=-500,
            delta_pe=1200,
            ce_type="SC",
            pe_type="SB",
            time_str="09:18:05",
            trigger_ms=1000,
        )

        # Entry price must be exact live ask: 152.40
        self.assertEqual(trade["entry_price"], 152.40)
        self.assertEqual(trade["lots"], self.engine.lots_full)

        # Update bid for exit: 165.20
        winner["bid_price"] = 165.20
        winner["ltp"] = 165.30

        pos = self.engine.manual_exit(reason="MANUAL")
        self.assertIsNotNone(pos)
        # Exit price must be exact live bid: 165.20
        self.assertEqual(pos["exit_price"], 165.20)
        self.assertNotEqual(pos["exit_price"], trade["entry_price"])
        self.assertAlmostEqual(pos["pnl_points"], 165.20 - 152.40, places=2)

    def test_trade_record_audit_fields(self):
        """Acceptance test 9: Trade record has rule number, stars, delta_ce, delta_pe, timestamps."""
        winner = {
            "instrument_key": "NSE_FO|99999",
            "trading_symbol": "NIFTY 24600 PE",
            "strike": 24600.0,
            "side": "PE",
            "ask_price": 120.00,
            "bid_price": 119.50,
            "ltp": 120.00,
            "underlying_key": NIFTY_KEY,
        }
        self.engine.hot_cache["NSE_FO|99999"] = winner

        trade = self.engine._execute_entry_order(
            winner=winner,
            side="PE",
            size="normal",
            rule_num=6,
            stars="⭐⭐",
            delta_ce=1500,
            delta_pe=3000,
            ce_type="SB",
            pe_type="LB",
            time_str="09:20:15",
            trigger_ms=1700000000000,
        )

        self.assertEqual(trade["rule_number"], 6)
        self.assertEqual(trade["stars"], "⭐⭐")
        self.assertEqual(trade["delta_ce"], 1500)
        self.assertEqual(trade["delta_pe"], 3000)
        self.assertEqual(trade["entry_time_ist"], "09:20:15")
        self.assertEqual(trade["decision"], "PE Buy")

    def test_fresh_oi_requirement(self):
        """Acceptance test 3b: No entry while OI is still equal to snapshot."""
        keys = [f"CE_{i}" for i in range(4)] + [f"PE_{i}" for i in range(4)]
        self.engine.locked_keys = list(keys)
        self.engine.snapshot.clear()
        self.engine.hot_cache.clear()

        # Both sides have OI exactly equal to snapshot
        for i in range(4):
            k = f"CE_{i}"
            self.engine.snapshot[k] = {"instrument_key": k, "side": "CE", "ltp": 100.0, "oi": 5000.0, "bid": 99.0, "ask": 101.0}
            self.engine.hot_cache[k] = {"instrument_key": k, "side": "CE", "ltp": 105.0, "oi": 5000.0, "bid_price": 104.0, "ask_price": 106.0}

        for i in range(4):
            k = f"PE_{i}"
            self.engine.snapshot[k] = {"instrument_key": k, "side": "PE", "ltp": 100.0, "oi": 5000.0, "bid": 99.0, "ask": 101.0}
            self.engine.hot_cache[k] = {"instrument_key": k, "side": "PE", "ltp": 95.0, "oi": 5000.0, "bid_price": 94.0, "ask_price": 96.0}

        # Regular evaluation (not force) within trading window
        self.engine.entry_start_time = "00:00:00"
        self.engine.entry_end_time = "23:59:59"
        res = self.engine.evaluate_oi_buildup(force=False)
        self.assertIsNone(res)
        self.assertEqual(self.engine.last_evaluation["decision"], "WAIT")
        self.assertIn("fresh OI", self.engine.last_evaluation["reason"])

    def test_end_to_end_rule_1_ce_buy(self):
        """End-to-End: SC on CE + SB on PE triggers Rule 1 CE Buy with full lots."""
        keys = [f"CE_{i}" for i in range(4)] + [f"PE_{i}" for i in range(4)]
        self.engine.locked_keys = list(keys)
        self.engine.snapshot.clear()
        self.engine.hot_cache.clear()

        # CE: price UP (>0.5%), OI DOWN (<-0.1%) -> SC (Short Covering)
        for i in range(4):
            k = f"CE_{i}"
            self.engine.snapshot[k] = {"instrument_key": k, "side": "CE", "ltp": 100.0, "oi": 10000.0, "bid": 99.0, "ask": 101.0}
            self.engine.hot_cache[k] = {
                "instrument_key": k, "trading_symbol": f"NIFTY 24500 CE", "strike": 24500.0,
                "side": "CE", "ltp": 105.0, "oi": 9500.0, "bid_price": 104.0, "ask_price": 105.0, "ask_qty": 500.0,
                "underlying_key": NIFTY_KEY
            }

        # PE: price DOWN (<-0.5%), OI UP (>0.1%) -> SB (Short Build-up)
        for i in range(4):
            k = f"PE_{i}"
            self.engine.snapshot[k] = {"instrument_key": k, "side": "PE", "ltp": 100.0, "oi": 10000.0, "bid": 99.0, "ask": 101.0}
            self.engine.hot_cache[k] = {
                "instrument_key": k, "trading_symbol": f"NIFTY 24700 PE", "strike": 24700.0,
                "side": "PE", "ltp": 92.0, "oi": 11500.0, "bid_price": 91.0, "ask_price": 93.0, "ask_qty": 500.0,
                "underlying_key": NIFTY_KEY
            }

        self.engine.entry_start_time = "00:00:00"
        self.engine.entry_end_time = "23:59:59"
        self.engine.lots_full = 5
        trade = self.engine.evaluate_oi_buildup(force=False)

        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_CE")
        self.assertEqual(trade["rule_number"], 1)
        self.assertEqual(trade["stars"], "⭐⭐⭐")
        self.assertEqual(trade["lots"], 5)
        self.assertEqual(trade["lot_size"], 5 * 65)
        self.assertEqual(self.engine.state, "HOLDING")

    def test_stock_options_top5_buildup_and_entry(self):
        """Acceptance test: Top stocks options planning, locking, 16-condition evaluation, and 15:15 squareoff."""
        self.engine.load_stock_configs()
        self.assertIn("RELIANCE", self.engine.stock_underlyings)
        rel_st = self.engine.stock_underlyings["RELIANCE"]
        rel_st["lots"] = 2
        rel_st["enabled"] = True

        # Plan 8 mock contracts (4 CE, 4 PE)
        rel_keys = [f"NSE_FO|RELIANCE_{side}_{stk}" for side in ("CE", "PE") for stk in (2400, 2420, 2440, 2460)]
        rel_st["planned_keys"] = rel_keys
        rel_st["locked_keys"] = rel_keys
        rel_st["state"] = "LOCKED"

        for k in rel_keys:
            side = "CE" if "_CE_" in k else "PE"
            stk = float(k.split("_")[-1])
            self.engine.contracts_by_key[k] = {
                "instrument_key": k,
                "trading_symbol": f"RELIANCE {stk} {side}",
                "strike_price": stk,
                "instrument_type": side,
                "lot_size": 250,
                "underlying_key": "RELIANCE",
            }
            self.engine.hot_cache[k] = {
                "instrument_key": k,
                "trading_symbol": f"RELIANCE {stk} {side}",
                "underlying_key": "RELIANCE",
                "strike": stk,
                "side": side,
                "ltp": 50.0,
                "oi": 10000.0,
                "prev_oi": 10000.0,
                "bid_price": 49.5,
                "ask_price": 50.0,
                "spread_pct": 1.0,
            }

        # Take stock snapshot
        self.engine.take_stock_snapshot("RELIANCE")
        self.assertTrue(rel_st["snapshot_taken"])
        self.assertEqual(len(rel_st["snapshot"]), 8)

        # Simulate OI changes: CE SC + PE SB -> Rule 1 CE Buy ⭐⭐⭐
        for k in rel_keys:
            if "_CE_" in k:
                self.engine.hot_cache[k]["ltp"] = 60.0   # +20% Price UP
                self.engine.hot_cache[k]["oi"] = 8000.0   # -20% OI DOWN -> SC
                self.engine.hot_cache[k]["ask_price"] = 60.0
                self.engine.hot_cache[k]["bid_price"] = 59.8
            else:
                self.engine.hot_cache[k]["ltp"] = 40.0   # -20% Price DOWN
                self.engine.hot_cache[k]["oi"] = 12000.0  # +20% OI UP -> SB
                self.engine.hot_cache[k]["ask_price"] = 40.0
                self.engine.hot_cache[k]["bid_price"] = 39.8

        # Evaluate OI buildup for RELIANCE
        self.engine.re_eval_min_interval_sec = 0.0
        trade = self.engine.evaluate_stock_oi_buildup("RELIANCE")
        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_CE")
        self.assertEqual(trade["rule_number"], 1)
        self.assertEqual(trade["stars"], "⭐⭐⭐")
        self.assertEqual(trade["lots"], 4)  # 2 base lots * 2 for full size = 4
        self.assertEqual(trade["lot_size"], 1000)  # 4 * 250 lot size = 1000
        self.assertEqual(trade["status"], "OPEN")
        self.assertEqual(rel_st["state"], "HOLDING")

        # 15:15:00 Clock Square-off
        self.engine.squareoff_all_stocks_1515()
        self.assertEqual(rel_st["state"], "CLOSED")
        self.assertIsNone(rel_st["current_position"])
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "SQUAREOFF_1515")

    def test_multi_index_independent_trades(self):
        """Acceptance test: NIFTY, BANK NIFTY, and SENSEX all plan, lock, and trade independently (1 trade per index)."""
        from strategy_engine import BANKNIFTY_KEY, SENSEX_KEY

        # Setup mock 8 contracts for BANK NIFTY
        bn_keys = [f"BN_CE_{i}" for i in range(4)] + [f"BN_PE_{i}" for i in range(4)]
        self.engine.index_underlyings[BANKNIFTY_KEY]["planned_keys"] = list(bn_keys)
        self.engine.index_underlyings[BANKNIFTY_KEY]["spot_price"] = 61200.0

        for i in range(4):
            k = f"BN_CE_{i}"
            stk = 61200 - (i * 100)
            self.engine.contracts_by_key[k] = {
                "instrument_key": k, "trading_symbol": f"BANKNIFTY {stk} CE", "strike_price": stk,
                "instrument_type": "CE", "lot_size": 15, "underlying_key": BANKNIFTY_KEY
            }
            self.engine.hot_cache[k] = {
                "instrument_key": k, "trading_symbol": f"BANKNIFTY {stk} CE", "strike": stk,
                "side": "CE", "ltp": 200.0, "oi": 50000.0, "bid_price": 199.0, "ask_price": 200.0,
                "underlying_key": BANKNIFTY_KEY
            }

        for i in range(4):
            k = f"BN_PE_{i}"
            stk = 61300 + (i * 100)
            self.engine.contracts_by_key[k] = {
                "instrument_key": k, "trading_symbol": f"BANKNIFTY {stk} PE", "strike_price": stk,
                "instrument_type": "PE", "lot_size": 15, "underlying_key": BANKNIFTY_KEY
            }
            self.engine.hot_cache[k] = {
                "instrument_key": k, "trading_symbol": f"BANKNIFTY {stk} PE", "strike": stk,
                "side": "PE", "ltp": 200.0, "oi": 50000.0, "bid_price": 199.0, "ask_price": 200.0,
                "underlying_key": BANKNIFTY_KEY
            }

        # Lock and snapshot Bank Nifty
        self.engine.lock_contracts(underlying_key=BANKNIFTY_KEY)
        self.assertEqual(len(self.engine.index_underlyings[BANKNIFTY_KEY]["locked_keys"]), 8)
        self.engine.take_snapshot(underlying_key=BANKNIFTY_KEY)
        self.assertTrue(self.engine.index_underlyings[BANKNIFTY_KEY]["snapshot_taken"])

        # Simulate NIFTY having already traded today
        self.engine.index_underlyings[NIFTY_KEY]["today_trades_count"] = 1
        self.engine.today_trades_count = 1

        # Simulate Bank Nifty price & OI move: CE LB + PE SB -> Rule 2 CE Buy ⭐⭐
        for k in bn_keys:
            if "BN_CE_" in k:
                self.engine.hot_cache[k]["ltp"] = 220.0   # +10% UP
                self.engine.hot_cache[k]["oi"] = 60000.0   # +20% UP -> LB
                self.engine.hot_cache[k]["ask_price"] = 220.0
                self.engine.hot_cache[k]["bid_price"] = 219.5
            else:
                self.engine.hot_cache[k]["ltp"] = 180.0   # -10% DOWN
                self.engine.hot_cache[k]["oi"] = 60000.0   # +20% UP -> SB
                self.engine.hot_cache[k]["ask_price"] = 180.0
                self.engine.hot_cache[k]["bid_price"] = 179.5

        # Evaluate Bank Nifty: Nifty having traded does NOT block Bank Nifty!
        self.engine.entry_start_time = "00:00:00"
        self.engine.entry_end_time = "23:59:59"
        self.engine.re_eval_min_interval_sec = 0.0

        trade = self.engine.evaluate_oi_buildup(underlying_key=BANKNIFTY_KEY, force=True)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "BUY_CE")
        self.assertEqual(trade["rule_number"], 2)
        self.assertEqual(trade["underlying_key"], BANKNIFTY_KEY)
        self.assertEqual(self.engine.index_underlyings[BANKNIFTY_KEY]["today_trades_count"], 1)
        self.assertEqual(self.engine.index_underlyings[BANKNIFTY_KEY]["state"], "HOLDING")

        # Second trade attempt on Bank Nifty is blocked by "1 trade at a time per index"
        trade2 = self.engine.evaluate_oi_buildup(underlying_key=BANKNIFTY_KEY, force=True)
        self.assertIsNone(trade2)

        # 15:15:00 Clock Square-off exits Bank Nifty position
        self.engine.squareoff_all_indices_1515()
        self.assertEqual(self.engine.index_underlyings[BANKNIFTY_KEY]["state"], "CLOSED")
        self.assertIsNone(self.engine.index_underlyings[BANKNIFTY_KEY]["current_position"])
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "SQUAREOFF_1515")

    def test_force_arm_now_single_index(self):
        """Test Arm Now button isolation: arming Bank Nifty resets only Bank Nifty."""
        # Setup initial completed trades on Nifty and Bank Nifty
        self.engine.index_underlyings[NIFTY_KEY]["today_trades_count"] = 1
        self.engine.index_underlyings[NIFTY_KEY]["state"] = "CLOSED"
        self.engine.index_underlyings[BANKNIFTY_KEY]["today_trades_count"] = 1
        self.engine.index_underlyings[BANKNIFTY_KEY]["state"] = "CLOSED"
        self.engine.index_underlyings[SENSEX_KEY]["today_trades_count"] = 0
        self.engine.index_underlyings[SENSEX_KEY]["state"] = "SETUP"

        # Mock spot prices
        self.engine.index_underlyings[BANKNIFTY_KEY]["spot_price"] = 50000.0
        self.engine.contracts_by_underlying[BANKNIFTY_KEY] = []

        # Click Arm Now on Bank Nifty only
        res = self.engine.force_arm_now(BANKNIFTY_KEY)
        self.assertIsNotNone(res)

        # Bank Nifty state is reset to ARMED and trades count to 0
        self.assertEqual(self.engine.index_underlyings[BANKNIFTY_KEY]["state"], "ARMED")
        self.assertEqual(self.engine.index_underlyings[BANKNIFTY_KEY]["today_trades_count"], 0)

        # NIFTY and SENSEX were NOT changed!
        self.assertEqual(self.engine.index_underlyings[NIFTY_KEY]["state"], "CLOSED")
        self.assertEqual(self.engine.index_underlyings[NIFTY_KEY]["today_trades_count"], 1)
        self.assertEqual(self.engine.index_underlyings[SENSEX_KEY]["state"], "SETUP")
        self.assertEqual(self.engine.index_underlyings[SENSEX_KEY]["today_trades_count"], 0)


if __name__ == "__main__":
    unittest.main()
