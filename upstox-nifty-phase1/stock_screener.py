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
        self._cached_screener: list[dict[str, Any]] = []

    def set_access_token(self, token: str) -> None:
        self.access_token = token

    def get_top_stocks_config(self, top_n: int | None = None) -> dict[str, Any]:
        """
        Returns {
            "top_n": N,
            "stocks": [
                {"symbol": s, "name": ..., "key": ..., "lot_size": ..., "strike_step": ..., "lots": ..., "enabled": ...}
            ]
        }
        """
        saved_top_n = 5
        saved_stocks_dict: dict[str, dict[str, Any]] = {}

        if self.selection_file.exists():
            try:
                with open(self.selection_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if "top_n" in data and "stocks" in data:
                            saved_top_n = int(data.get("top_n", 5))
                            raw_stocks = data.get("stocks")
                            if isinstance(raw_stocks, list):
                                for s in raw_stocks:
                                    if isinstance(s, dict) and "symbol" in s:
                                        saved_stocks_dict[s["symbol"].upper()] = s
                            elif isinstance(raw_stocks, dict):
                                saved_stocks_dict = {k.upper(): v for k, v in raw_stocks.items()}
                        else:
                            # Legacy format: {"RELIANCE": 2, "INFY": 3}
                            for sym, lots in data.items():
                                saved_stocks_dict[sym.upper()] = {"lots": int(lots), "enabled": True}
                            saved_top_n = len(saved_stocks_dict) if saved_stocks_dict else 5
            except Exception as e:
                LOGGER.warning("Could not read selection file: %s", e)

        target_n = top_n if (top_n is not None and top_n > 0) else saved_top_n
        target_n = max(1, min(target_n, len(NIFTY_50_STOCKS)))

        stocks_list = []
        for meta in NIFTY_50_STOCKS[:target_n]:
            sym = meta["symbol"].upper()
            saved = saved_stocks_dict.get(sym, {})
            lots = int(saved.get("lots", 1)) if isinstance(saved, dict) else 1
            enabled = bool(saved.get("enabled", True)) if isinstance(saved, dict) else True
            stocks_list.append({
                "symbol": sym,
                "name": meta["name"],
                "key": meta["key"],
                "lot_size": meta["lot_size"],
                "strike_step": meta["strike_step"],
                "lots": max(1, lots),
                "enabled": enabled,
            })

        return {
            "top_n": target_n,
            "stocks": stocks_list,
        }

    def save_top_stocks_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Save top N stocks configuration."""
        top_n = int(config.get("top_n", 5))
        raw_stocks = config.get("stocks") or []
        stocks_list = []

        if isinstance(raw_stocks, list):
            for s in raw_stocks:
                if isinstance(s, dict) and "symbol" in s:
                    sym = s["symbol"].upper()
                    meta = next((m for m in NIFTY_50_STOCKS if m["symbol"] == sym), None)
                    stocks_list.append({
                        "symbol": sym,
                        "name": s.get("name") or (meta["name"] if meta else sym),
                        "key": s.get("key") or (meta["key"] if meta else ""),
                        "lot_size": int(s.get("lot_size") or (meta["lot_size"] if meta else 1)),
                        "strike_step": float(s.get("strike_step") or (meta["strike_step"] if meta else 10)),
                        "lots": max(1, int(s.get("lots", 1))),
                        "enabled": bool(s.get("enabled", True)),
                    })

        payload = {
            "top_n": top_n,
            "stocks": stocks_list,
        }
        try:
            with open(self.selection_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            LOGGER.info("Saved top stocks config: top_n=%d, count=%d", top_n, len(stocks_list))
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

                results.append({
                    "symbol": sym,
                    "name": item["name"],
                    "key": key,
                    "lot_size": item["lot_size"],
                    "strike_step": item["strike_step"],
                    "price": ltp,
                    "prev_close": prev_close,
                    "change": net_change,
                    "change_pct": change_pct,
                    "volume": volume,
                    "oi": oi,
                    "build_up": bu_label,
                    "build_up_class": bu_class,
                    "selected": sym in selected_map,
                    "lots": selected_map.get(sym, 1),
                })

            # Sort descending by volume by default
            results.sort(key=lambda x: (not x["selected"], -x["volume"]))
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
        Plans 8 ITM Option Contracts (4 CE below spot, 4 PE above spot)
        for a constituent NIFTY stock using its nearest monthly expiry.
        """
        stock_meta = next((s for s in NIFTY_50_STOCKS if s["symbol"] == symbol.upper()), None)
        if not stock_meta:
            return {"error": f"Stock symbol {symbol} not in NIFTY 50 list."}

        key = stock_meta["key"]
        lot_size = stock_meta["lot_size"]
        strike_step = stock_meta["strike_step"]

        # If spot not provided, try to find from cached quotes
        if spot is None or spot <= 0:
            cached = next((c for c in self._cached_screener if c["symbol"] == symbol.upper()), None)
            if cached and cached.get("price"):
                spot = float(cached["price"])
            else:
                spot = 1000.0  # Fallback

        if not self.access_token:
            return {"error": "Upstox access token required to fetch stock option chain."}

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
                if not contracts:
                    return {"error": f"No option contracts found for {symbol}."}

                from datetime import datetime
                from config import IST
                today_str = datetime.now(IST).strftime("%Y-%m-%d")
                expiries = sorted(list({c["expiry"] for c in contracts if c.get("expiry") and c.get("expiry") >= today_str}))
                if not expiries:
                    return {"error": f"No upcoming expiries found for {symbol}."}

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

                # Rule R5: 4 CE below spot, 4 PE above spot (excluding equal)
                ce_strikes = sorted([s for s in ce_by_strike.keys() if s < spot], reverse=True)[:4]
                pe_strikes = sorted([s for s in pe_by_strike.keys() if s > spot])[:4]

                planned = []
                for s in sorted(ce_strikes):
                    c = ce_by_strike[s]
                    planned.append({
                        "symbol": c.get("trading_symbol"),
                        "instrument_key": c.get("instrument_key"),
                        "strike": s,
                        "side": "CE",
                        "expiry": active_expiry,
                        "lot_size": lot_size,
                    })

                for s in sorted(pe_strikes):
                    c = pe_by_strike[s]
                    planned.append({
                        "symbol": c.get("trading_symbol"),
                        "instrument_key": c.get("instrument_key"),
                        "strike": s,
                        "side": "PE",
                        "expiry": active_expiry,
                        "lot_size": lot_size,
                    })

                return {
                    "symbol": symbol.upper(),
                    "name": stock_meta["name"],
                    "spot": spot,
                    "expiry": active_expiry,
                    "lot_size": lot_size,
                    "strike_step": strike_step,
                    "planned_contracts": planned,
                }
        except Exception as exc:
            LOGGER.error("Failed to plan stock ITM contracts for %s: %s", symbol, exc)
            return {"error": str(exc)}
