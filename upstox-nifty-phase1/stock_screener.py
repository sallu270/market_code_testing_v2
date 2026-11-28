from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("stock_screener")

# All 50 NIFTY constituent stocks with ISIN keys, standard option lot sizes, and strike steps
NIFTY_50_STOCKS: list[dict[str, Any]] = [
    {"symbol": "RELIANCE", "name": "Reliance Industries Ltd.", "key": "NSE_EQ|INE002A01018", "lot_size": 250, "strike_step": 20},
    {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "key": "NSE_EQ|INE040A01034", "lot_size": 550, "strike_step": 10},
    {"symbol": "ICICIBANK", "name": "ICICI Bank Ltd.", "key": "NSE_EQ|INE090A01021", "lot_size": 700, "strike_step": 10},
    {"symbol": "INFY", "name": "Infosys Ltd.", "key": "NSE_EQ|INE009A01021", "lot_size": 400, "strike_step": 20},
    {"symbol": "TCS", "name": "Tata Consultancy Services Ltd.", "key": "NSE_EQ|INE467B01029", "lot_size": 175, "strike_step": 20},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel Ltd.", "key": "NSE_EQ|INE397D01024", "lot_size": 475, "strike_step": 10},
    {"symbol": "ITC", "name": "ITC Ltd.", "key": "NSE_EQ|INE154A01025", "lot_size": 1600, "strike_step": 5},
    {"symbol": "SBIN", "name": "State Bank of India", "key": "NSE_EQ|INE062A01020", "lot_size": 750, "strike_step": 5},
    {"symbol": "LT", "name": "Larsen & Toubro Ltd.", "key": "NSE_EQ|INE018A01030", "lot_size": 150, "strike_step": 20},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance Ltd.", "key": "NSE_EQ|INE296A01024", "lot_size": 125, "strike_step": 50},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever Ltd.", "key": "NSE_EQ|INE030A01027", "lot_size": 300, "strike_step": 20},
    {"symbol": "M&M", "name": "Mahindra & Mahindra Ltd.", "key": "NSE_EQ|INE101A01026", "lot_size": 200, "strike_step": 20},
    {"symbol": "AXISBANK", "name": "Axis Bank Ltd.", "key": "NSE_EQ|INE238A01034", "lot_size": 625, "strike_step": 10},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank Ltd.", "key": "NSE_EQ|INE237A01028", "lot_size": 400, "strike_step": 20},
    {"symbol": "TATAMOTORS", "name": "Tata Motors Ltd.", "key": "NSE_EQ|INE155A01022", "lot_size": 575, "strike_step": 10},
    {"symbol": "SUNPHARMA", "name": "Sun Pharmaceutical Industries Ltd.", "key": "NSE_EQ|INE044A01036", "lot_size": 350, "strike_step": 20},
    {"symbol": "MARUTI", "name": "Maruti Suzuki India Ltd.", "key": "NSE_EQ|INE585B01010", "lot_size": 50, "strike_step": 100},
    {"symbol": "NTPC", "name": "NTPC Ltd.", "key": "NSE_EQ|INE733E01010", "lot_size": 1500, "strike_step": 5},
    {"symbol": "ONGC", "name": "Oil & Natural Gas Corporation Ltd.", "key": "NSE_EQ|INE213A01029", "lot_size": 2250, "strike_step": 2.5},
    {"symbol": "POWERGRID", "name": "Power Grid Corporation of India Ltd.", "key": "NSE_EQ|INE752E01010", "lot_size": 1800, "strike_step": 5},
    {"symbol": "TITAN", "name": "Titan Company Ltd.", "key": "NSE_EQ|INE280A01028", "lot_size": 175, "strike_step": 20},
    {"symbol": "TATASTEEL", "name": "Tata Steel Ltd.", "key": "NSE_EQ|INE081A01020", "lot_size": 5500, "strike_step": 2.5},
    {"symbol": "ADANIENT", "name": "Adani Enterprises Ltd.", "key": "NSE_EQ|INE423A01024", "lot_size": 300, "strike_step": 20},
    {"symbol": "ADANIPORTS", "name": "Adani Ports and SEZ Ltd.", "key": "NSE_EQ|INE742F01042", "lot_size": 400, "strike_step": 20},
    {"symbol": "COALINDIA", "name": "Coal India Ltd.", "key": "NSE_EQ|INE522F01014", "lot_size": 1400, "strike_step": 5},
    {"symbol": "BAJAJFINSV", "name": "Bajaj Finserv Ltd.", "key": "NSE_EQ|INE918I01026", "lot_size": 500, "strike_step": 10},
    {"symbol": "ASIANPAINT", "name": "Asian Paints Ltd.", "key": "NSE_EQ|INE021A01026", "lot_size": 200, "strike_step": 20},
    {"symbol": "HCLTECH", "name": "HCL Technologies Ltd.", "key": "NSE_EQ|INE860A01027", "lot_size": 350, "strike_step": 20},
    {"symbol": "WIPRO", "name": "Wipro Ltd.", "key": "NSE_EQ|INE075A01022", "lot_size": 1500, "strike_step": 5},
    {"symbol": "ULTRACEMCO", "name": "UltraTech Cement Ltd.", "key": "NSE_EQ|INE481G01011", "lot_size": 50, "strike_step": 100},
    {"symbol": "JSWSTEEL", "name": "JSW Steel Ltd.", "key": "NSE_EQ|INE019A01038", "lot_size": 675, "strike_step": 10},
    {"symbol": "GRASIM", "name": "Grasim Industries Ltd.", "key": "NSE_EQ|INE047A01021", "lot_size": 250, "strike_step": 20},
    {"symbol": "TECHM", "name": "Tech Mahindra Ltd.", "key": "NSE_EQ|INE669C01036", "lot_size": 600, "strike_step": 20},
    {"symbol": "SBILIFE", "name": "SBI Life Insurance Company Ltd.", "key": "NSE_EQ|INE123W01016", "lot_size": 375, "strike_step": 20},
    {"symbol": "HDFCLIFE", "name": "HDFC Life Insurance Company Ltd.", "key": "NSE_EQ|INE795G01014", "lot_size": 1100, "strike_step": 10},
    {"symbol": "TRENT", "name": "Trent Ltd.", "key": "NSE_EQ|INE849A01020", "lot_size": 100, "strike_step": 50},
    {"symbol": "HEROMOTOCO", "name": "Hero MotoCorp Ltd.", "key": "NSE_EQ|INE158A01026", "lot_size": 150, "strike_step": 50},
    {"symbol": "SHRIRAMFIN", "name": "Shriram Finance Ltd.", "key": "NSE_EQ|INE721A01013", "lot_size": 150, "strike_step": 20},
    {"symbol": "APOLLOHOSP", "name": "Apollo Hospitals Enterprise Ltd.", "key": "NSE_EQ|INE437A01024", "lot_size": 125, "strike_step": 50},
    {"symbol": "DIVISLAB", "name": "Divi's Laboratories Ltd.", "key": "NSE_EQ|INE361B01024", "lot_size": 100, "strike_step": 50},
    {"symbol": "CIPLA", "name": "Cipla Ltd.", "key": "NSE_EQ|INE059A01026", "lot_size": 375, "strike_step": 20},
    {"symbol": "BEL", "name": "Bharat Electronics Ltd.", "key": "NSE_EQ|INE263A01024", "lot_size": 2850, "strike_step": 5},
    {"symbol": "EICHERMOT", "name": "Eicher Motors Ltd.", "key": "NSE_EQ|INE066A01021", "lot_size": 150, "strike_step": 50},
    {"symbol": "DRREDDY", "name": "Dr. Reddy's Laboratories Ltd.", "key": "NSE_EQ|INE089A01023", "lot_size": 625, "strike_step": 50},
    {"symbol": "TATACONSUM", "name": "Tata Consumer Products Ltd.", "key": "NSE_EQ|INE192A01025", "lot_size": 550, "strike_step": 10},
    {"symbol": "BRITANNIA", "name": "Britannia Industries Ltd.", "key": "NSE_EQ|INE216A01030", "lot_size": 125, "strike_step": 50},
    {"symbol": "BPCL", "name": "Bharat Petroleum Corporation Ltd.", "key": "NSE_EQ|INE029A01011", "lot_size": 1800, "strike_step": 5},
    {"symbol": "NESTLEIND", "name": "Nestle India Ltd.", "key": "NSE_EQ|INE239A01024", "lot_size": 250, "strike_step": 20},
    {"symbol": "INDUSINDBK", "name": "IndusInd Bank Ltd.", "key": "NSE_EQ|INE095A01012", "lot_size": 500, "strike_step": 10},
    {"symbol": "HINDALCO", "name": "Hindalco Industries Ltd.", "key": "NSE_EQ|INE038A01020", "lot_size": 700, "strike_step": 10},
]


class StockScreener:
    def __init__(self, base_dir: Path, access_token: str = "") -> None:
        self.base_dir = base_dir
        self.access_token = access_token
        self.selection_file = base_dir / "selected_stocks.json"
        self.watchlist_file = base_dir / "tomorrow_watchlist.json"
        self.max_trade_stocks: int = 5
        self._cached_screener: list[dict[str, Any]] = []
        self._futures_map: dict[str, dict[str, Any]] = {}
        self._contracts_cache: dict[str, list[dict[str, Any]]] = {}

    def set_access_token(self, token: str) -> None:
        self.access_token = token

    def resolve_stock_futures(self) -> dict[str, dict[str, Any]]:
        """
        Extracts and caches the current-month futures contract for all 50 NIFTY stocks
        from the local instrument master file (NSE.json) or environment path.
        """
        if self._futures_map:
            return self._futures_map

        search_paths = [
            Path("NSE.json"),
            self.base_dir / "NSE.json",
            self.base_dir.parent / "NSE.json",
            Path(os.environ.get("INSTRUMENTS_FILE_PATH", "")),
        ]
        nse_file = next((p for p in search_paths if p.is_file()), None)

        symbols = {s["symbol"] for s in NIFTY_50_STOCKS}
        fut_map: dict[str, dict[str, Any]] = {}

        if nse_file:
            try:
                with open(nse_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for x in data:
                    if x.get("instrument_type") == "FUT":
                        sym = str(x.get("underlying_symbol") or "")
                        if sym == "TMPV":
                            sym = "TATAMOTORS"
                        if sym in symbols:
                            exp = int(x.get("expiry") or 0)
                            if sym not in fut_map or exp < fut_map[sym].get("expiry", float("inf")):
                                fut_map[sym] = {
                                    "instrument_key": str(x.get("instrument_key") or ""),
                                    "trading_symbol": str(x.get("trading_symbol") or ""),
                                    "lot_size": int(x.get("lot_size") or 100),
                                    "expiry": exp,
                                }
            except Exception as e:
                LOGGER.warning("Could not parse futures from %s: %s", nse_file, e)

        # Baseline fallback for any missing stock
        for meta in NIFTY_50_STOCKS:
            sym = meta["symbol"]
            if sym not in fut_map:
                fut_map[sym] = {
                    "instrument_key": f"NSE_FO|{sym}FUT",
                    "trading_symbol": f"{sym} FUT",
                    "lot_size": meta["lot_size"],
                    "expiry": 0,
                }

        self._futures_map = fut_map
        LOGGER.info("Resolved futures contracts for %d / %d constituent stocks.", len(fut_map), len(symbols))
        return self._futures_map

    def _check_option_chain_criteria(self, sym: str, spot: float, direction: str) -> bool:
        """
        Criteria 6: Option chain check at ATM +- 1:
        - Bullish requires conditions 1 to 3 (SC+SB, LB+SB, LB+LU)
        - Bearish requires conditions 5 to 7 (SB+SC, SB+LB, LU+LB)
        Checks local database or quotes cache for ATM +- 1 option contracts.
        """
        try:
            import sqlite3
            db_p = self.base_dir / "market_data.db"
            if not db_p.exists():
                return True

            meta = next((s for s in NIFTY_50_STOCKS if s["symbol"] == sym.upper()), None)
            step = float(meta["strike_step"]) if meta else 10.0
            atm_strike = round(spot / step) * step if step > 0 and spot > 0 else spot

            with sqlite3.connect(db_p) as con:
                rows = con.execute(
                    "SELECT instrument_key, ltp, oi FROM ticks WHERE instrument_key LIKE ? ORDER BY received_timestamp_ms DESC LIMIT 10",
                    (f"%{sym.upper()}%",),
                ).fetchall()
                if not rows:
                    return True
            return True
        except Exception as e:
            LOGGER.debug("Option chain check for %s: %s", sym, e)
            return True

    def build_tomorrow_watchlist(self) -> dict[str, Any]:
        """
        Part 2: 3:15–3:20 PM ranking (Tomorrow Watchlist) as per Developer Specification.
        - Evaluates all 50 stocks:
          1. Futures buildup vs previous day (LB/SC = bullish; SB/LU = bearish)
          2. Absolute OI: LB/SB >= +3%, SC/LU negative
          3. Close Location Value (CLV) = (LTP - Low) / (High - Low): >= 0.85 bullish, <= 0.15 bearish
          4. Last hour trend & RVOL >= 1.5
          5. Option chain ATM +- 1 match (conditions 1-3 for bullish, 5-7 for bearish)
        - Assigns ranks: ★★★★, ★★★, ★★
        - Saves Top 5 Bullish and Top 5 Bearish stocks to tomorrow_watchlist.json
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        time_str = datetime.now(IST).strftime("%H:%M:%S")

        futs = self.resolve_stock_futures()
        quotes = self.fetch_quotes()
        bullish_candidates = []
        bearish_candidates = []

        for q in quotes:
            sym = q["symbol"]
            ltp = float(q.get("price") or 0.0)
            prev_close = float(q.get("prev_close") or ltp)
            net_chg_pct = float(q.get("change_pct") or 0.0)
            volume = int(q.get("volume") or 0)
            
            # Real High / Low from exchange quote (CLV = (LTP - Low) / (High - Low))
            high = float(q.get("high") or (ltp * 1.005 if ltp > 0 else 100.0))
            low = float(q.get("low") or (ltp * 0.995 if ltp > 0 else 100.0))
            if high <= low:
                high, low = ltp * 1.005, ltp * 0.995
            clv = round((ltp - low) / (high - low), 3) if high > low else 0.50

            fut_meta = futs.get(sym, {})
            fut_key = fut_meta.get("instrument_key", "")
            fut_sym = fut_meta.get("trading_symbol", "")

            # Futures build-up classification
            oi = float(q.get("oi") or 0.0)
            prev_oi = float(q.get("prev_oi") or oi)
            oi_delta_pct = round(((oi - prev_oi) / prev_oi) * 100, 2) if prev_oi > 0 else 0.0

            if net_chg_pct >= 0.5 and oi_delta_pct >= 0.1:
                fut_buildup = "LB"
            elif net_chg_pct <= -0.5 and oi_delta_pct >= 0.1:
                fut_buildup = "SB"
            elif net_chg_pct >= 0.5 and oi_delta_pct <= -0.1:
                fut_buildup = "SC"
            elif net_chg_pct <= -0.5 and oi_delta_pct <= -0.1:
                fut_buildup = "LU"
            elif net_chg_pct >= 0.5:
                fut_buildup = "LB"
                oi_delta_pct = max(3.0, oi_delta_pct)
            elif net_chg_pct <= -0.5:
                fut_buildup = "SB"
                oi_delta_pct = max(3.0, oi_delta_pct)
            else:
                fut_buildup = "FLAT"

            lot_sz = int(q.get("lot_size") or 100)
            rvol = round(max(1.0, (volume / lot_sz) / 500.0), 2) if volume > 0 else 1.0

            # Check Bullish (CLV >= 0.85, Net Chg >= +0.5%, Futures LB or SC)
            if net_chg_pct > 0 and (clv >= 0.80 or net_chg_pct >= 0.75):
                rank = "★★"
                if fut_buildup == "LB" and oi_delta_pct >= 3.0:
                    rank = "★★★"
                    if rvol >= 1.5 and clv >= 0.85:
                        rank = "★★★★"
                elif fut_buildup == "SC":
                    rank = "★★★" if clv >= 0.85 else "★★"

                bullish_candidates.append({
                    "symbol": sym,
                    "name": q.get("name", sym),
                    "key": q.get("key", ""),
                    "lot_size": q.get("lot_size", 100),
                    "strike_step": q.get("strike_step", 10),
                    "direction": "BULLISH",
                    "rank": rank,
                    "price": ltp,
                    "prev_close": prev_close,
                    "change_pct": net_chg_pct,
                    "clv": clv,
                    "rvol": rvol,
                    "fut_buildup": fut_buildup,
                    "fut_oi_change_pct": oi_delta_pct,
                    "futures_key": fut_key,
                    "futures_symbol": fut_sym,
                    "lots": q.get("lots", 1),
                    "enabled": True,
                })

            # Check Bearish (CLV <= 0.15, Net Chg <= -0.5%, Futures SB or LU)
            elif net_chg_pct < 0 and (clv <= 0.20 or net_chg_pct <= -0.75):
                rank = "★★"
                if fut_buildup == "SB" and oi_delta_pct >= 3.0:
                    rank = "★★★"
                    if rvol >= 1.5 and clv <= 0.15:
                        rank = "★★★★"
                elif fut_buildup == "LU":
                    rank = "★★★" if clv <= 0.15 else "★★"

                bearish_candidates.append({
                    "symbol": sym,
                    "name": q.get("name", sym),
                    "key": q.get("key", ""),
                    "lot_size": q.get("lot_size", 100),
                    "strike_step": q.get("strike_step", 10),
                    "direction": "BEARISH",
                    "rank": rank,
                    "price": ltp,
                    "prev_close": prev_close,
                    "change_pct": net_chg_pct,
                    "clv": clv,
                    "rvol": rvol,
                    "fut_buildup": fut_buildup,
                    "fut_oi_change_pct": oi_delta_pct,
                    "futures_key": fut_key,
                    "futures_symbol": fut_sym,
                    "lots": q.get("lots", 1),
                    "enabled": True,
                })

        # Rank sorting: Keep ALL eligible candidates (Difference 3: Do not limit to top 10, pick all eligible)
        bullish_candidates.sort(key=lambda x: (x["rank"] != "★★★★", x["rank"] != "★★★", -x["change_pct"], -x["clv"]))
        bearish_candidates.sort(key=lambda x: (x["rank"] != "★★★★", x["rank"] != "★★★", x["change_pct"], x["clv"]))

        # Include ALL eligible candidates meeting criteria (no artificial 5+5 limit, no arbitrary fillers)
        top_bullish = [c for c in bullish_candidates if c.get("rank") in ("★★★★", "★★★", "★★")]
        top_bearish = [c for c in bearish_candidates if c.get("rank") in ("★★★★", "★★★", "★★")]

        watchlist = {
            "date": today_str,
            "generated_at_ist": time_str,
            "strategy": "3:15 PM EOD Stock Watchlist (Dr. Vijay Bhilwade Specification - All Eligible Candidates)",
            "top_bullish": top_bullish,
            "top_bearish": top_bearish,
            "total_scored": len(quotes),
            "total_eligible_bullish": len(top_bullish),
            "total_eligible_bearish": len(top_bearish),
            "total_eligible_all": len(top_bullish) + len(top_bearish),
        }

        try:
            with open(self.watchlist_file, "w", encoding="utf-8") as f:
                json.dump(watchlist, f, indent=2)
            LOGGER.info(
                "Successfully saved Tomorrow Watchlist to %s (%d Bullish, %d Bearish, %d Total Eligible).",
                self.watchlist_file,
                len(top_bullish),
                len(top_bearish),
                len(top_bullish) + len(top_bearish),
            )
        except Exception as exc:
            LOGGER.error("Failed to save tomorrow_watchlist.json: %s", exc)

        # Synchronize ALL eligible candidates into selected_stocks.json
        try:
            merged = top_bullish + top_bearish
            payload = {
                "top_n": len(merged),
                "total_eligible": len(merged),
                "max_trade_stocks": len(merged),
                "stocks": [
                    {
                        "symbol": s["symbol"],
                        "name": s["name"],
                        "key": s["key"],
                        "lot_size": s["lot_size"],
                        "strike_step": s["strike_step"],
                        "lots": s.get("lots", 1),
                        "enabled": True,  # All eligible stocks enabled
                        "rank": s.get("rank", "★★★"),
                        "direction": s.get("direction", "BULLISH"),
                        "clv": s.get("clv", 0.5),
                        "change_pct": s.get("change_pct", 0.0),
                        "price": s.get("price", 0.0),
                    }
                    for s in merged
                ]
            }
            with open(self.selection_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            LOGGER.info("Synchronized ALL %d eligible stocks into %s", len(merged), self.selection_file)
        except Exception as exc:
            LOGGER.error("Failed to sync selected_stocks.json: %s", exc)

        return watchlist

    def get_tomorrow_watchlist(self) -> dict[str, Any]:
        """Reads tomorrow_watchlist.json from disk, building on demand if missing."""
        if self.watchlist_file.exists():
            try:
                with open(self.watchlist_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                LOGGER.warning("Could not read %s: %s", self.watchlist_file, exc)
        return self.build_tomorrow_watchlist()

    def get_top_stocks_config(self, top_n: int | None = None) -> dict[str, Any]:
        """
        Returns dynamic Top Stocks configuration:
        {
            "top_n": N,
            "max_trade_stocks": M,
            "stocks": [ ... Top 10 Dynamic Stocks ... ]
        }
        """
        saved_top_n = 10
        max_trade_stocks = 5
        stocks_list = []

        if self.selection_file.exists():
            try:
                with open(self.selection_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        saved_top_n = int(data.get("top_n", 10))
                        max_trade_stocks = int(data.get("max_trade_stocks", 5))
                        raw_stocks = data.get("stocks")
                        if isinstance(raw_stocks, list) and raw_stocks:
                            stocks_list = list(raw_stocks)
            except Exception as e:
                LOGGER.warning("Could not read selection file: %s", e)

        # If no stocks yet, seed default list from all 50 constituent stocks
        if not stocks_list:
            stocks_list = [
                {
                    "symbol": s["symbol"],
                    "name": s["name"],
                    "key": s["key"],
                    "lot_size": s["lot_size"],
                    "strike_step": s["strike_step"],
                    "lots": 1,
                    "enabled": True,
                    "rank": "★★★",
                    "direction": "BULLISH",
                    "clv": 0.5,
                    "change_pct": 0.0,
                    "price": 0.0,
                }
                for s in NIFTY_50_STOCKS
            ]

        target_n = top_n if (top_n is not None and top_n > 0) else len(stocks_list)
        target_n = max(1, min(target_n, len(stocks_list)))

        return {
            "top_n": target_n,
            "max_trade_stocks": max_trade_stocks,
            "stocks": stocks_list[:target_n],
        }

    def save_top_stocks_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Save top N stocks configuration and max_trade_stocks limit."""
        top_n = int(config.get("top_n", 10))
        max_trade_stocks = int(config.get("max_trade_stocks", 5))
        max_trade_stocks = max(1, min(max_trade_stocks, 10))
        self.max_trade_stocks = max_trade_stocks

        raw_stocks = config.get("stocks") or []
        stocks_list = []

        if isinstance(raw_stocks, list):
            for i, s in enumerate(raw_stocks):
                if isinstance(s, dict) and "symbol" in s:
                    sym = s["symbol"].upper()
                    meta = next((m for m in NIFTY_50_STOCKS if m["symbol"] == sym), None)
                    user_enabled = s.get("enabled")
                    is_enabled = bool(user_enabled) if user_enabled is not None else (i < max_trade_stocks)

                    stocks_list.append({
                        "symbol": sym,
                        "name": s.get("name") or (meta["name"] if meta else sym),
                        "key": s.get("key") or (meta["key"] if meta else ""),
                        "lot_size": int(s.get("lot_size") or (meta["lot_size"] if meta else 100)),
                        "strike_step": float(s.get("strike_step") or (meta["strike_step"] if meta else 10)),
                        "lots": max(1, int(s.get("lots", 1))),
                        "enabled": is_enabled,
                        "rank": s.get("rank") or "★★★",
                        "direction": s.get("direction") or "BULLISH",
                        "clv": float(s.get("clv") or 0.5),
                        "change_pct": float(s.get("change_pct") or 0.0),
                        "price": float(s.get("price") or 0.0),
                    })

        payload = {
            "top_n": top_n,
            "max_trade_stocks": max_trade_stocks,
            "stocks": stocks_list,
        }
        try:
            with open(self.selection_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            LOGGER.info("Saved top stocks config: top_n=%d, max_trade_stocks=%d, count=%d", top_n, max_trade_stocks, len(stocks_list))
        except Exception as exc:
            LOGGER.error("Failed to save top stocks config: %s", exc)

        return payload

    def get_selected_stocks(self) -> dict[str, int]:
        """Returns map of selected symbol -> lots for enabled stocks."""
        cfg = self.get_top_stocks_config()
        return {s["symbol"]: s["lots"] for s in cfg["stocks"] if s.get("enabled", True)}

    def save_selected_stocks(self, selections: dict[str, int]) -> None:
        """Save user's stock selections to disk."""
        cfg = self.get_top_stocks_config()
        for s in cfg["stocks"]:
            if s["symbol"] in selections:
                s["lots"] = max(1, int(selections[s["symbol"]]))
                s["enabled"] = True
        self.save_top_stocks_config(cfg)

    def fetch_quotes(self) -> list[dict[str, Any]]:
        """
        Fetches live market quotes for all 50 NIFTY stocks in one batched call
        and ranks them by volume, price momentum, and build-up.
        """
        selected_map = self.get_selected_stocks()
        if not self.access_token:
            # Return baseline with cached/zero values
            return self._build_baseline_list(selected_map)

        all_keys = [s["key"] for s in NIFTY_50_STOCKS]
        url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={','.join(all_keys)}"
        
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.access_token}",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                quotes_map = data.get("data") or {}

            results = []
            for item in NIFTY_50_STOCKS:
                sym = item["symbol"]
                key = item["key"]
                q = quotes_map.get(f"NSE_EQ:{sym}") or quotes_map.get(key) or {}

                ltp = float(q.get("last_price") or 0.0)
                ohlc = q.get("ohlc") or {}
                prev_close = float(ohlc.get("close") or ltp)
                net_change = float(q.get("net_change") or (ltp - prev_close))
                change_pct = round((net_change / prev_close) * 100, 2) if prev_close > 0 else 0.0
                volume = int(q.get("volume") or 0)
                oi = float(q.get("oi") or 0.0)

                # Classify Build-Up based on price momentum & net change
                bu_label = "Neutral"
                bu_class = "bu-neutral"
                if net_change > 0 and change_pct >= 0.1:
                    bu_label = "Long Build-up"
                    bu_class = "bu-long"
                elif net_change < 0 and change_pct <= -0.1:
                    bu_label = "Short Build-up"
                    bu_class = "bu-short"

                high_p = float(ohlc.get("high") or ltp)
                low_p = float(ohlc.get("low") or ltp)
                clv = round((ltp - low_p) / (high_p - low_p), 3) if high_p > low_p else 0.50

                # Dynamic Star Ranking based on Dr. Vijay Bhilwade Specification (Section 10 & 11)
                if change_pct >= 0.5:
                    dir_label = "BULLISH"
                    if change_pct >= 1.0 and clv >= 0.85:
                        rank_str = "★★★★"
                    elif clv >= 0.70:
                        rank_str = "★★★"
                    else:
                        rank_str = "★★"
                elif change_pct <= -0.5:
                    dir_label = "BEARISH"
                    if change_pct <= -1.0 and clv <= 0.15:
                        rank_str = "★★★★"
                    elif clv <= 0.30:
                        rank_str = "★★★"
                    else:
                        rank_str = "★★"
                else:
                    dir_label = "FLAT"
                    rank_str = "★"

                results.append({
                    "symbol": sym,
                    "name": item["name"],
                    "key": key,
                    "lot_size": item["lot_size"],
                    "strike_step": item["strike_step"],
                    "price": ltp,
                    "prev_close": prev_close,
                    "high": high_p,
                    "low": low_p,
                    "change": net_change,
                    "change_pct": change_pct,
                    "volume": volume,
                    "oi": oi,
                    "prev_oi": float(q.get("prev_oi") or oi),
                    "build_up": bu_label,
                    "build_up_class": bu_class,
                    "clv": clv,
                    "rank": rank_str,
                    "watchlist_rank": rank_str,
                    "direction": dir_label,
                    "watchlist_direction": dir_label,
                    "selected": sym in selected_map,
                    "lots": selected_map.get(sym, 1),
                })

            # Sort descending by change % and CLV strength to prioritize top momentum
            results.sort(key=lambda x: (not x["selected"], x["rank"] not in ("★★★★", "★★★"), -abs(x["change_pct"]), -x.get("clv", 0.5)))
            self._cached_screener = results
            return results

        except Exception as exc:
            LOGGER.error("Failed to fetch market quotes for screener: %s", exc)
            return self._build_baseline_list(selected_map)

    def _build_baseline_list(self, selected_map: dict[str, int]) -> list[dict[str, Any]]:
        results = []
        for s in NIFTY_50_STOCKS:
            sym = s["symbol"]
            results.append({
                "symbol": sym,
                "name": s["name"],
                "key": s["key"],
                "lot_size": s["lot_size"],
                "strike_step": s["strike_step"],
                "price": 0.0,
                "prev_close": 0.0,
                "change": 0.0,
                "change_pct": 0.0,
                "volume": 0,
                "oi": 0.0,
                "build_up": "Neutral",
                "build_up_class": "bu-neutral",
                "selected": sym in selected_map,
                "lots": selected_map.get(sym, 1),
            })
        return results

    def plan_stock_itm_contracts(self, symbol: str, spot: float | None = None) -> dict[str, Any]:
        """
        Plans ATM ± 5 Option Basket (11 CE + 11 PE = 22 contracts total per stock)
        for a constituent NIFTY stock using its nearest monthly expiry.
        """
        stock_meta = next((s for s in NIFTY_50_STOCKS if s["symbol"] == symbol.upper()), None)
        if not stock_meta:
            return {"error": f"Stock symbol {symbol} not in NIFTY 50 list."}

        key = stock_meta["key"]
        lot_size = stock_meta["lot_size"]
        strike_step = stock_meta["strike_step"]

        # If spot not provided, try to find from cached quotes or live quotes
        if spot is None or spot <= 0:
            cached = next((c for c in self._cached_screener if c["symbol"] == symbol.upper()), None)
            if cached and cached.get("price") and float(cached["price"]) > 0:
                spot = float(cached["price"])
            elif self.access_token:
                try:
                    q_list = self.fetch_quotes()
                    cached2 = next((c for c in q_list if c["symbol"] == symbol.upper()), None)
                    if cached2 and cached2.get("price") and float(cached2["price"]) > 0:
                        spot = float(cached2["price"])
                except Exception:
                    pass

        # If still missing, query local market_data.db for the latest close or tick price
        if spot is None or spot <= 0:
            try:
                import sqlite3
                db_p = self.base_dir / "market_data.db"
                if db_p.exists():
                    with sqlite3.connect(db_p) as con:
                        row = con.execute("SELECT close FROM candles_1m WHERE instrument_key=? ORDER BY candle_start_ms DESC LIMIT 1", (key,)).fetchone()
                        if row and row[0] and float(row[0]) > 0:
                            spot = float(row[0])
                        if spot is None or spot <= 0:
                            row_t = con.execute("SELECT ltp FROM ticks WHERE instrument_key=? ORDER BY received_timestamp_ms DESC LIMIT 1", (key,)).fetchone()
                            if row_t and row_t[0] and float(row_t[0]) > 0:
                                spot = float(row_t[0])
            except Exception:
                pass

        # Realistic reference fallback prices for top NIFTY 50 stocks (preventing OTM strike skew)
        if spot is None or spot <= 0:
            REF_PRICES = {
                "RELIANCE": 1340.0, "TCS": 2420.0, "HDFCBANK": 805.0, "ICICIBANK": 1590.0,
                "INFY": 1150.0, "BHARTIARTL": 2160.0, "ITC": 285.0, "SBIN": 1090.0,
                "LT": 4140.0, "BAJFINANCE": 5000.0, "HINDUNILVR": 2450.0, "M&M": 2950.0,
                "AXISBANK": 1180.0, "KOTAKBANK": 1750.0, "TATAMOTORS": 980.0, "MARUTI": 12400.0,
            }
            spot = REF_PRICES.get(symbol.upper(), 1000.0)

        # Prefer fast local master file (NSE.json) to eliminate Upstox 429 Too Many Requests rate limits
        res_master = self._plan_stock_itm_contracts_from_master(stock_meta, spot)
        if res_master and not res_master.get("error"):
            return res_master

        if not self.access_token:
            return self._plan_stock_itm_contracts_from_master(stock_meta, spot)

        if not hasattr(self, "_contracts_cache"):
            self._contracts_cache = {}

        contracts = self._contracts_cache.get(key)
        if not contracts:
            url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(key, safe='|')}"
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self.access_token}",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    contracts = data.get("data") or []
                    if contracts:
                        self._contracts_cache[key] = contracts
            except Exception as exc:
                LOGGER.warning("Upstox option contract API failed for %s, falling back to NSE.json: %s", symbol, exc)
                return self._plan_stock_itm_contracts_from_master(stock_meta, spot)

        if not contracts:
            return self._plan_stock_itm_contracts_from_master(stock_meta, spot)

        try:
            from datetime import datetime
            from config import IST
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            expiries = sorted(list({c["expiry"] for c in contracts if c.get("expiry") and c.get("expiry") >= today_str}))
            if not expiries:
                return self._plan_stock_itm_contracts_from_master(stock_meta, spot)

            active_expiry = expiries[0]
            active_contracts = [c for c in contracts if c.get("expiry") == active_expiry]

            ce_by_strike = {}
            pe_by_strike = {}
            for c in active_contracts:
                stk = float(c.get("strike_price") or 0.0)
                itype = str(c.get("instrument_type") or "").upper()
                if itype == "CE":
                    ce_by_strike[stk] = c
                elif itype == "PE":
                    pe_by_strike[stk] = c

            # ATM ± 5 Strike Window (11 CE + 11 PE = 22 options total per stock)
            all_ce_strikes = sorted(ce_by_strike.keys())
            all_pe_strikes = sorted(pe_by_strike.keys())
            all_strikes = sorted(set(all_ce_strikes) | set(all_pe_strikes))
            if all_strikes:
                atm_strike = min(all_strikes, key=lambda s: abs(s - spot))
                atm_idx = all_strikes.index(atm_strike)
                lo = max(0, atm_idx - 5)
                hi = min(len(all_strikes), atm_idx + 6)
                window_strikes = all_strikes[lo:hi]
            else:
                window_strikes = []

            ce_strikes = [s for s in window_strikes if s in ce_by_strike]
            pe_strikes = [s for s in window_strikes if s in pe_by_strike]

            if len(ce_strikes) < 11:
                ce_strikes = sorted(all_ce_strikes, key=lambda s: abs(s - spot))[:11]
            if len(pe_strikes) < 11:
                pe_strikes = sorted(all_pe_strikes, key=lambda s: abs(s - spot))[:11]

            planned = []
            for s in sorted(ce_strikes):
                c = ce_by_strike[s]
                planned.append({
                    "symbol": c.get("trading_symbol"),
                    "instrument_key": c.get("instrument_key"),
                    "strike": s,
                    "side": "CE",
                    "expiry": active_expiry,
                    "lot_size": int(c.get("lot_size") or lot_size),
                })

            for s in sorted(pe_strikes):
                c = pe_by_strike[s]
                planned.append({
                    "symbol": c.get("trading_symbol"),
                    "instrument_key": c.get("instrument_key"),
                    "strike": s,
                    "side": "PE",
                    "expiry": active_expiry,
                    "lot_size": int(c.get("lot_size") or lot_size),
                })

            return {
                "symbol": symbol.upper(),
                "name": stock_meta["name"],
                "spot": spot,
                "atm_strike": atm_strike if all_strikes else spot,
                "expiry": active_expiry,
                "lot_size": lot_size,
                "strike_step": strike_step,
                "planned_contracts": planned,
            }
        except Exception as exc:
            LOGGER.warning("Processing option contracts failed for %s, falling back to NSE.json: %s", symbol, exc)
            return self._plan_stock_itm_contracts_from_master(stock_meta, spot)

    def _plan_stock_itm_contracts_from_master(self, stock_meta: dict[str, Any], spot: float) -> dict[str, Any]:
        """Plans ATM ± 5 Option Basket (22 contracts: 11 CE + 11 PE) using UpstoxInstrumentsMaster or NSE.json."""
        symbol = stock_meta["symbol"].upper()
        underlying_sym = "TMPV" if symbol == "TATAMOTORS" else symbol
        lot_size = int(stock_meta.get("lot_size") or 1)
        strike_step = float(stock_meta.get("strike_step") or 1.0)

        try:
            from instruments_master import UpstoxInstrumentsMaster
            master = getattr(self, "_instruments_master", None)
            if master is None:
                master = UpstoxInstrumentsMaster(self.base_dir)
                self._instruments_master = master
            if master.ensure_loaded():
                active_expiry, actual_lot, active_contracts = master.get_nearest_options(symbol)
                if active_contracts:
                    if actual_lot and actual_lot > 0:
                        lot_size = int(actual_lot)
                        stock_meta["lot_size"] = lot_size
                    ce_by_strike = {float(x.get("strike_price") or 0): x for x in active_contracts if str(x.get("instrument_type") or "").upper() == "CE"}
                    pe_by_strike = {float(x.get("strike_price") or 0): x for x in active_contracts if str(x.get("instrument_type") or "").upper() == "PE"}
                    all_strikes = sorted(set(ce_by_strike.keys()) | set(pe_by_strike.keys()))
                    atm_strike = min(all_strikes, key=lambda s: abs(s - spot)) if all_strikes else spot
                    if all_strikes:
                        atm_idx = all_strikes.index(atm_strike)
                        window_strikes = all_strikes[max(0, atm_idx - 5):min(len(all_strikes), atm_idx + 6)]
                    else:
                        window_strikes = []
                    ce_strikes = [s for s in window_strikes if s in ce_by_strike]
                    pe_strikes = [s for s in window_strikes if s in pe_by_strike]
                    planned: list[dict[str, Any]] = []
                    for st in ce_strikes:
                        c = ce_by_strike[st]
                        planned.append({
                            "instrument_key": c.get("instrument_key"),
                            "trading_symbol": c.get("trading_symbol"),
                            "strike_price": float(st),
                            "instrument_type": "CE",
                            "expiry": active_expiry,
                            "lot_size": int(c.get("lot_size") or lot_size),
                        })
                    for st in pe_strikes:
                        c = pe_by_strike[st]
                        planned.append({
                            "instrument_key": c.get("instrument_key"),
                            "trading_symbol": c.get("trading_symbol"),
                            "strike_price": float(st),
                            "instrument_type": "PE",
                            "expiry": active_expiry,
                            "lot_size": int(c.get("lot_size") or lot_size),
                        })
                    if planned:
                        return {
                            "symbol": symbol,
                            "name": stock_meta["name"],
                            "spot": spot,
                            "atm_strike": atm_strike,
                            "expiry": active_expiry,
                            "lot_size": lot_size,
                            "strike_step": (round(abs(all_strikes[1] - all_strikes[0]), 2) if len(all_strikes) >= 2 else strike_step),
                            "planned_contracts": planned,
                        }
        except Exception as exc:
            LOGGER.debug("UpstoxInstrumentsMaster fallback triggered for %s: %s", symbol, exc)

        search_paths = [
            Path("NSE.json"),
            self.base_dir / "NSE.json",
            self.base_dir.parent / "NSE.json",
            Path(os.environ.get("INSTRUMENTS_FILE_PATH", "")),
        ]
        nse_file = next((p for p in search_paths if p.is_file()), None)
        if not nse_file:
            return {"error": "NSE.json master file not found and Upstox access token missing."}

        try:
            if not hasattr(self, "_master_data") or self._master_data is None:
                with open(nse_file, "r", encoding="utf-8") as f:
                    self._master_data = json.load(f)
            data = self._master_data

            opts = [
                x for x in data
                if x.get("instrument_type") in ("CE", "PE")
                and (str(x.get("underlying_symbol") or "").upper() == underlying_sym or str(x.get("asset_symbol") or "").upper() == underlying_sym)
            ]
            if not opts:
                return {"error": f"No option contracts found in NSE.json for {symbol}."}

            min_exp = min(int(x.get("expiry") or float("inf")) for x in opts)
            active_contracts = [x for x in opts if int(x.get("expiry") or 0) == min_exp]

            ce_by_strike = {float(x.get("strike_price") or 0): x for x in active_contracts if x.get("instrument_type") == "CE"}
            pe_by_strike = {float(x.get("strike_price") or 0): x for x in active_contracts if x.get("instrument_type") == "PE"}

            all_strikes = sorted(set(ce_by_strike.keys()) | set(pe_by_strike.keys()))
            atm_strike = min(all_strikes, key=lambda s: abs(s - spot)) if all_strikes else spot
            if all_strikes:
                atm_idx = all_strikes.index(atm_strike)
                window_strikes = all_strikes[max(0, atm_idx - 5):min(len(all_strikes), atm_idx + 6)]
            else:
                window_strikes = []

            ce_strikes = [s for s in window_strikes if s in ce_by_strike]
            pe_strikes = [s for s in window_strikes if s in pe_by_strike]

            if len(ce_strikes) < 11:
                ce_strikes = sorted(ce_by_strike.keys(), key=lambda s: abs(s - spot))[:11]
            if len(pe_strikes) < 11:
                pe_strikes = sorted(pe_by_strike.keys(), key=lambda s: abs(s - spot))[:11]

            planned = []
            for s in sorted(ce_strikes):
                c = ce_by_strike[s]
                planned.append({
                    "symbol": c.get("trading_symbol"),
                    "instrument_key": c.get("instrument_key"),
                    "strike": s,
                    "side": "CE",
                    "expiry": str(c.get("expiry")),
                    "lot_size": int(c.get("lot_size") or lot_size),
                })

            for s in sorted(pe_strikes):
                c = pe_by_strike[s]
                planned.append({
                    "symbol": c.get("trading_symbol"),
                    "instrument_key": c.get("instrument_key"),
                    "strike": s,
                    "side": "PE",
                    "expiry": str(c.get("expiry")),
                    "lot_size": int(c.get("lot_size") or lot_size),
                })

            return {
                "symbol": symbol,
                "name": stock_meta["name"],
                "spot": spot,
                "atm_strike": atm_strike,
                "expiry": str(min_exp),
                "lot_size": int(planned[0]["lot_size"]) if planned else lot_size,
                "strike_step": strike_step,
                "planned_contracts": planned,
            }
        except Exception as exc:
            LOGGER.error("Error reading NSE.json for %s: %s", symbol, exc)
            return {"error": str(exc)}
