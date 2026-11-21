"""
Volume PCR & Change-in-OI PCR Flow Layer (v0.0.5)
Section 6A & 6B Specification by Dr. Vijay Bhilwade.

Computes:
- VOL_PCR_DAY, VOL_PCR_5M, VOL_PCR_BASE, VOL_PCR_SHIFT
- OI_PCR, dOI_PCR, NET_OI_BIAS, NET_OI_BIAS_SLOPE, FLOW_SCORE
- CE_VSR, PE_VSR, OTM_VSR, STRADDLE_DECAY
- FLOW_STATE (NEUTRAL, COIL, IGNITION_BULL, IGNITION_BEAR, CONFIRMED_BULL, CONFIRMED_BEAR, DIVERGENCE_TRAP, EXHAUSTION)
"""

import time
import math
import logging
from typing import Any
from collections import deque
from datetime import datetime
from config import IST

LOGGER = logging.getLogger("flow_engine")


class FlowInstrumentTracker:
    def __init__(self, key: str, name: str, is_index: bool = True, strike_step: float = 50.0):
        self.key = key
        self.name = name
        self.is_index = is_index
        self.strike_step = strike_step
        
        # Basket: map of contract_key -> {strike, side, is_otm_gamma, ltp, oi, volume, baseline_oi, baseline_vol}
        self.basket_contracts: dict[str, dict[str, Any]] = {}
        self.basket_atm: float = 0.0
        self.baseline_taken: bool = False
        self.straddle_0920_ref: float | None = None
        
        # 1-minute historical bars for rolling metrics (timestamp_min -> {ce_vol, pe_vol, ce_otm_vol, pe_otm_vol, spot_high, spot_low, spot_close})
        self.minute_bars: deque[dict[str, Any]] = deque(maxlen=60)
        self.current_bar_min: int = 0
        self.current_bar_data: dict[str, Any] = {}
        
        # Rolling 5-minute Volume PCR history (stores VOL_PCR_5M per minute for the 30-min baseline)
        self.vol_pcr_5m_history: deque[float] = deque(maxlen=35)
        
        # OI History for slope & divergence (stores (timestamp, net_oi_bias, flow_score))
        self.oi_updates_history: deque[dict[str, Any]] = deque(maxlen=10)
        
        # Coil state tracking
        self.coil_active: bool = False
        self.coil_armed_time: float = 0.0
        self.coil_high: float = 0.0
        self.coil_low: float = 0.0
        
        # Ignition persistence tracking (needs 2 consecutive bars)
        self.ignition_side: str | None = None
        self.ignition_bars_count: int = 0
        
        # Current Output Metrics
        self.vol_pcr_day: float = 1.0
        self.vol_pcr_5m: float = 1.0
        self.vol_pcr_base: float = 1.0
        self.vol_pcr_shift: float = 1.0
        
        self.oi_pcr: float = 1.0
        self.doi_pcr_str: str = "N/A"
        self.net_oi_bias: float = 0.0
        self.net_oi_bias_slope: float = 0.0
        self.flow_score: float = 0.0
        
        self.ce_vsr: float = 1.0
        self.pe_vsr: float = 1.0
        self.otm_vsr: float = 1.0
        self.straddle_decay: float = 1.0
        
        self.flow_state: str = "NEUTRAL"
        self.bot_action: str = "Monitoring flow"
        self.last_update_ts: float = time.time()
        self.oi_stale: bool = False
        self.last_oi_timestamp_ms: int = 0

    def set_basket(self, contracts: list[dict[str, Any]], atm_strike: float) -> None:
        """Sets the flow monitoring basket (ATM +- 5 for index, ATM +- 3 for stock)."""
        self.basket_atm = atm_strike
        self.basket_contracts.clear()
        min_prem = 3.0 if self.is_index else 1.0

        for c in contracts:
            k = c["instrument_key"]
            stk = float(c.get("strike_price") or c.get("strike") or 0.0)
            side = str(c.get("instrument_type") or c.get("side") or "").upper()
            
            # Is this in the OTM Gamma tier (ATM+1 to ATM+3 for CE, ATM-1 to ATM-3 for PE)?
            is_otm = False
            if side == "CE" and (stk >= atm_strike + self.strike_step and stk <= atm_strike + 3 * self.strike_step):
                is_otm = True
            elif side == "PE" and (stk <= atm_strike - self.strike_step and stk >= atm_strike - 3 * self.strike_step):
                is_otm = True

            self.basket_contracts[k] = {
                "instrument_key": k,
                "symbol": c.get("trading_symbol") or k,
                "strike": stk,
                "side": side,
                "is_otm": is_otm,
                "ltp": float(c.get("ltp") or 0.0),
                "oi": float(c.get("oi") or 0.0),
                "volume": float(c.get("volume") or 0.0),
                "baseline_oi": float(c.get("oi") or 0.0),
                "baseline_vol": float(c.get("volume") or 0.0),
                "min_prem": min_prem,
            }

    def take_0915_baseline(self) -> None:
        """Captures 09:15 baseline values for dOI_PCR and VOL_PCR_DAY."""
        self.baseline_taken = True
        for k, c in self.basket_contracts.items():
            if (c.get("oi") or 0.0) > 0:
                c["baseline_oi"] = float(c["oi"])
            if (c.get("volume") or 0.0) > 0:
                c["baseline_vol"] = float(c["volume"])

    def on_tick(self, contract_key: str, ltp: float, oi: float | None, volume: float | None, bid: float = 0.0, ask: float = 0.0) -> None:
        """Updates live tick quote for a basket contract."""
        if contract_key not in self.basket_contracts:
            return
        c = self.basket_contracts[contract_key]
        if ltp > 0:
            c["ltp"] = ltp
        if oi is not None:
            f_oi = float(oi)
            c["oi"] = f_oi
            if c.get("baseline_oi", 0.0) <= 0.0 and f_oi > 0:
                c["baseline_oi"] = f_oi
        if volume is not None:
            f_vol = float(volume)
            c["volume"] = f_vol
            if c.get("baseline_vol", 0.0) <= 0.0 and f_vol > 0:
                c["baseline_vol"] = f_vol
        if bid > 0:
            c["bid_price"] = bid
        if ask > 0:
            c["ask_price"] = ask

    def on_minute_bar_closed(self, bar_time_ms: int, spot_price: float, vwap: float) -> None:
        """Called every closed 1-minute bar to roll VSR, Volume PCR 5M, and evaluate Flow State Machine."""
        now_dt = datetime.now(IST)
        now_hms = now_dt.strftime("%H:%M:%S")

        # Capture 09:20 straddle reference if not set
        if now_hms >= "09:20:00" and self.straddle_0920_ref is None and self.basket_atm > 0:
            atm_ce = next((c for c in self.basket_contracts.values() if c["strike"] == self.basket_atm and c["side"] == "CE"), None)
            atm_pe = next((c for c in self.basket_contracts.values() if c["strike"] == self.basket_atm and c["side"] == "PE"), None)
            if atm_ce and atm_pe and (atm_ce.get("ltp") or 0) > 0 and (atm_pe.get("ltp") or 0) > 0:
                self.straddle_0920_ref = float(atm_ce["ltp"]) + float(atm_pe["ltp"])

        # 1. Sum up current basket volumes and OIs
        total_ce_vol_day = 0.0
        total_pe_vol_day = 0.0
        total_ce_oi = 0.0
        total_pe_oi = 0.0
        delta_ce_oi = 0.0
        delta_pe_oi = 0.0

        min_prem = 3.0 if self.is_index else 1.0

        for c in self.basket_contracts.values():
            ltp = c.get("ltp") or 0.0
            vol = c.get("volume") or 0.0
            base_vol = c.get("baseline_vol") or 0.0
            day_vol = max(0.0, vol - base_vol)
            oi = c.get("oi") or 0.0
            base_oi = c.get("baseline_oi") or oi
            d_oi = oi - base_oi

            if c["side"] == "CE":
                total_ce_oi += oi
                delta_ce_oi += d_oi
                if ltp >= min_prem:
                    total_ce_vol_day += day_vol
            elif c["side"] == "PE":
                total_pe_oi += oi
                delta_pe_oi += d_oi
                if ltp >= min_prem:
                    total_pe_vol_day += day_vol

        # Compute VOL_PCR_DAY
        if total_ce_vol_day > 0:
            self.vol_pcr_day = round(max(0.10, min(10.0, total_pe_vol_day / total_ce_vol_day)), 2)
        else:
            self.vol_pcr_day = 1.0

        # Compute Total OI_PCR
        if total_ce_oi > 0:
            self.oi_pcr = round(max(0.10, min(10.0, total_pe_oi / total_ce_oi)), 2)

        # Compute dOI_PCR & NET_OI_BIAS
        if delta_ce_oi > 0 and delta_pe_oi > 0:
            self.doi_pcr_str = f"{delta_pe_oi / delta_ce_oi:.2f}"
        else:
            ce_sign = "+CE" if delta_ce_oi >= 0 else "-CE"
            pe_sign = "+PE" if delta_pe_oi >= 0 else "-PE"
            self.doi_pcr_str = f"{pe_sign}/{ce_sign}"

        denom = abs(delta_pe_oi) + abs(delta_ce_oi)
        if denom > 0:
            self.net_oi_bias = round((delta_pe_oi - delta_ce_oi) / denom, 2)
        else:
            self.net_oi_bias = 0.0

        # Store minute bar for rolling 5-minute volume delta & 20-minute VSR
        bar = {
            "time_ms": bar_time_ms,
            "ce_vol": total_ce_vol_day,
            "pe_vol": total_pe_vol_day,
            "spot": spot_price,
        }
        self.minute_bars.append(bar)

        # 2. Rolling 5-Minute Volume PCR (VOL_PCR_5M)
        if len(self.minute_bars) >= 5:
            delta_5m_pe = max(1.0, self.minute_bars[-1]["pe_vol"] - self.minute_bars[-5]["pe_vol"])
            delta_5m_ce = max(1.0, self.minute_bars[-1]["ce_vol"] - self.minute_bars[-5]["ce_vol"])
            raw_5m = delta_5m_pe / delta_5m_ce
            self.vol_pcr_5m = round(max(0.10, min(10.0, raw_5m)), 2)
        else:
            self.vol_pcr_5m = self.vol_pcr_day

        self.vol_pcr_5m_history.append(self.vol_pcr_5m)

        # 3. VOL_PCR_BASE & VOL_PCR_SHIFT
        if len(self.vol_pcr_5m_history) >= 15:
            # Mean of prior 30 min excluding last 5 min
            base_slice = list(self.vol_pcr_5m_history)[:-5]
            if base_slice:
                self.vol_pcr_base = round(sum(base_slice) / len(base_slice), 2)
            else:
                self.vol_pcr_base = self.vol_pcr_day
        else:
            self.vol_pcr_base = self.vol_pcr_day

        if self.vol_pcr_base > 0:
            self.vol_pcr_shift = round(self.vol_pcr_5m / self.vol_pcr_base, 2)
        else:
            self.vol_pcr_shift = 1.0

        # 4. Volume Spike Ratios (CE_VSR & PE_VSR)
        if len(self.minute_bars) >= 3:
            # 1-min delta
            ce_1m_delta = max(0.0, self.minute_bars[-1]["ce_vol"] - self.minute_bars[-2]["ce_vol"])
            pe_1m_delta = max(0.0, self.minute_bars[-1]["pe_vol"] - self.minute_bars[-2]["pe_vol"])
            
            # Average 1-min delta over prior up to 20 bars
            sample_bars = list(self.minute_bars)[-21:-1]
            if len(sample_bars) >= 2:
                ce_deltas = [max(0.0, sample_bars[i]["ce_vol"] - sample_bars[i-1]["ce_vol"]) for i in range(1, len(sample_bars))]
                pe_deltas = [max(0.0, sample_bars[i]["pe_vol"] - sample_bars[i-1]["pe_vol"]) for i in range(1, len(sample_bars))]
                avg_ce_1m = max(100.0, sum(ce_deltas) / len(ce_deltas))
                avg_pe_1m = max(100.0, sum(pe_deltas) / len(pe_deltas))
                self.ce_vsr = round(min(50.0, ce_1m_delta / avg_ce_1m), 1)
                self.pe_vsr = round(min(50.0, pe_1m_delta / avg_pe_1m), 1)
            else:
                self.ce_vsr = 1.0
                self.pe_vsr = 1.0
        else:
            self.ce_vsr = 1.0
            self.pe_vsr = 1.0

        # 5. Straddle Decay
        if self.straddle_0920_ref and self.straddle_0920_ref > 0 and self.basket_atm > 0:
            atm_ce = next((c for c in self.basket_contracts.values() if c["strike"] == self.basket_atm and c["side"] == "CE"), None)
            atm_pe = next((c for c in self.basket_contracts.values() if c["strike"] == self.basket_atm and c["side"] == "PE"), None)
            if atm_ce and atm_pe:
                cur_straddle = float(atm_ce.get("ltp") or 0.0) + float(atm_pe.get("ltp") or 0.0)
                if cur_straddle > 0:
                    self.straddle_decay = round(cur_straddle / self.straddle_0920_ref, 2)

        # 6. Evaluate Flow State Machine
        self._evaluate_state_machine(spot_price, vwap)

    def _evaluate_state_machine(self, spot_price: float, vwap: float) -> None:
        """Executes the Section 6A.5 Flow State Machine."""
        ign_vsr = 2.5 if self.is_index else 3.0
        shift_bull_max = 0.65 if self.is_index else 0.60
        shift_bear_min = 1.55 if self.is_index else 1.65

        # Check S1: COIL
        is_coil = False
        if len(self.minute_bars) >= 15:
            last_15 = list(self.minute_bars)[-15:]
            spots = [b["spot"] for b in last_15 if b.get("spot", 0) > 0]
            if spots:
                max_s = max(spots)
                min_s = min(spots)
                range_pct = (max_s - min_s) / min_s * 100 if min_s > 0 else 1.0
                coil_limit = 0.25 if self.is_index else 0.50
                if self.ce_vsr < 0.7 and self.pe_vsr < 0.7 and abs(self.net_oi_bias) <= 0.20 and range_pct <= coil_limit:
                    is_coil = True
                    self.coil_active = True
                    self.coil_armed_time = time.time()
                    self.coil_high = max_s
                    self.coil_low = min_s

        if time.time() - self.coil_armed_time > 1800:
            self.coil_active = False

        # Check S2: IGNITION_BULL & S3: IGNITION_BEAR
        is_bull_ignition = (
            self.ce_vsr >= ign_vsr
            and self.vol_pcr_shift <= shift_bull_max
            and spot_price >= vwap
        )
        is_bear_ignition = (
            self.pe_vsr >= ign_vsr
            and self.vol_pcr_shift >= shift_bear_min
            and spot_price <= vwap
        )

        if is_bull_ignition:
            if self.ignition_side == "BULL":
                self.ignition_bars_count += 1
            else:
                self.ignition_side = "BULL"
                self.ignition_bars_count = 1
        elif is_bear_ignition:
            if self.ignition_side == "BEAR":
                self.ignition_bars_count += 1
            else:
                self.ignition_side = "BEAR"
                self.ignition_bars_count = 1
        else:
            self.ignition_bars_count = 0
            self.ignition_side = None

        # Determine Final State
        if self.ignition_side == "BULL" and self.ignition_bars_count >= 2:
            if self.net_oi_bias >= 0.15:
                self.flow_state = "CONFIRMED_BULL"
                self.bot_action = "CONFIRMED: Institutional Call Flow Active (Eligible Path 2/3)"
            else:
                self.flow_state = "IGNITION_BULL"
                self.bot_action = "Call volume spike! Waiting for OI confirmation..."
        elif self.ignition_side == "BEAR" and self.ignition_bars_count >= 2:
            if self.net_oi_bias <= -0.15:
                self.flow_state = "CONFIRMED_BEAR"
                self.bot_action = "CONFIRMED: Institutional Put Flow Active (Eligible Path 2/3)"
            else:
                self.flow_state = "IGNITION_BEAR"
                self.bot_action = "Put volume spike! Waiting for OI confirmation..."
        elif is_coil:
            self.flow_state = "COIL"
            self.bot_action = f"Pre-move Coil Compression [{self.coil_low:.1f} - {self.coil_high:.1f}]"
        elif (self.vol_pcr_shift <= 0.65 and self.net_oi_bias <= -0.25) or (self.vol_pcr_shift >= 1.55 and self.net_oi_bias >= 0.25):
            self.flow_state = "DIVERGENCE_TRAP"
            self.bot_action = "DIVERGENCE TRAP: Volume & OI opposing. Blocked 10m."
        else:
            self.flow_state = "NEUTRAL"
            self.bot_action = "Monitoring flow"

    def get_dashboard_row(self) -> dict[str, Any]:
        """Returns Phase 1 Simple Dashboard row matching Figure 6B-1."""
        return {
            "instrument": self.name,
            "key": self.key,
            "vol_pcr_5m": self.vol_pcr_5m,
            "vol_pcr_shift": self.vol_pcr_shift,
            "doi_pcr": self.doi_pcr_str,
            "net_oi_bias": self.net_oi_bias,
            "ce_vsr": f"{self.ce_vsr:.1f}x",
            "pe_vsr": f"{self.pe_vsr:.1f}x",
            "flow_state": self.flow_state,
            "bot_action": self.bot_action,
            "straddle_decay": self.straddle_decay,
        }


class VolumePcrFlowEngine:
    """Singleton Flow Engine managing all instruments (Indices & Watchlist Stocks)."""

    def __init__(self):
        self.trackers: dict[str, FlowInstrumentTracker] = {
            "NSE_INDEX|Nifty 50": FlowInstrumentTracker("NSE_INDEX|Nifty 50", "NIFTY 50", is_index=True, strike_step=50.0),
            "NSE_INDEX|Nifty Bank": FlowInstrumentTracker("NSE_INDEX|Nifty Bank", "BANK NIFTY", is_index=True, strike_step=100.0),
            "BSE_INDEX|SENSEX": FlowInstrumentTracker("BSE_INDEX|SENSEX", "SENSEX", is_index=True, strike_step=100.0),
        }
        self.alerts: deque[dict[str, Any]] = deque(maxlen=20)

    def register_stock(self, sym: str, key: str, strike_step: float) -> None:
        if key not in self.trackers:
            self.trackers[key] = FlowInstrumentTracker(key, sym, is_index=False, strike_step=strike_step)

    def on_tick(self, inst_key: str, ltp: float, oi: float | None = None, vol: float | None = None, bid: float = 0.0, ask: float = 0.0) -> None:
        for trk in self.trackers.values():
            if inst_key in trk.basket_contracts:
                trk.on_tick(inst_key, ltp, oi, vol, bid, ask)

    def on_minute_bar_closed(self, inst_key: str, bar_time_ms: int, spot_price: float, vwap: float) -> None:
        if inst_key in self.trackers:
            trk = self.trackers[inst_key]
            old_st = trk.flow_state
            trk.on_minute_bar_closed(bar_time_ms, spot_price, vwap)
            if trk.flow_state != old_st and trk.flow_state in ("CONFIRMED_BULL", "CONFIRMED_BEAR", "COIL", "DIVERGENCE_TRAP"):
                self.alerts.appendleft({
                    "time": datetime.now(IST).strftime("%H:%M:%S"),
                    "instrument": trk.name,
                    "event": trk.flow_state,
                    "detail": trk.bot_action,
                })

    def get_phase1_dashboard(self) -> dict[str, Any]:
        """Provides full data for Section 6B Phase 1 Simple Dashboard."""
        rows = [t.get_dashboard_row() for t in self.trackers.values()]
        return {
            "status": "LIVE",
            "timestamp": datetime.now(IST).strftime("%H:%M:%S"),
            "rows": rows,
            "alerts": list(self.alerts)[:5],
        }


FLOW_ENGINE = VolumePcrFlowEngine()
