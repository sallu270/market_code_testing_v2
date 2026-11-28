from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("instruments_master")
IST = ZoneInfo("Asia/Kolkata")

NSE_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
BSE_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"

# Symbol alias mapping for recent NSE renames
SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "TATAMOTORS": ("TATAMOTORS", "TMPV"),
    "NIFTY": ("NIFTY",),
    "BANKNIFTY": ("BANKNIFTY",),
    "SENSEX": ("SENSEX",),
}


class UpstoxInstrumentsMaster:
    """
    Downloads and caches Upstox's daily NSE & BSE Instruments Master JSON once per day.
    Provides exact nearest-expiry option contracts, trading symbols, and live official
    exchange lot sizes for all 53 underlyings (3 Indices + 50 NIFTY Stocks) in a single
    download with zero option-chain REST API rate limits.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.cache_dir = base_dir / "instruments_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._loaded_date: str = ""
        # Map of underlying_symbol_upper -> list[contract_dict]
        self._options_by_sym: dict[str, list[dict[str, Any]]] = {}
        self._lot_sizes_by_sym: dict[str, int] = {}
        self._futures_by_sym: dict[str, dict[str, Any]] = {}
        self._equity_keys_by_sym: dict[str, str] = {}

    def ensure_loaded(self, force_download: bool = False) -> bool:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        with self._lock:
            if not force_download and self._loaded_date == today_str and self._options_by_sym:
                return True

            nse_path = self.cache_dir / f"NSE_{today_str}.json"
            bse_path = self.cache_dir / f"BSE_{today_str}.json"

            # Clean older dated caches to keep disk tidy
            for old_f in self.cache_dir.glob("*.json"):
                if today_str not in old_f.name:
                    try:
                        old_f.unlink()
                    except Exception:
                        pass

            nse_data = self._load_or_download(nse_path, NSE_MASTER_URL, force_download)
            bse_data = self._load_or_download(bse_path, BSE_MASTER_URL, force_download)

            if not nse_data and not bse_data:
                return False

            self._index_instruments(nse_data or [], bse_data or [], today_str)
            self._loaded_date = today_str
            return bool(self._options_by_sym)

    def _load_or_download(self, local_path: Path, url: str, force: bool) -> list[dict[str, Any]]:
        if not force and local_path.is_file() and local_path.stat().st_size > 100:
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list) and data:
                    LOGGER.info("Loaded cached instruments master from %s (%d entries)", local_path.name, len(data))
                    return data
            except Exception as exc:
                LOGGER.warning("Failed reading cached %s: %s", local_path.name, exc)

        # Fallback: check if root NSE.json exists (only if fresh today)
        try:
            LOGGER.info("Downloading daily Upstox instruments master: %s", url)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_bytes = gzip.decompress(resp.read())
                data = json.loads(raw_bytes.decode("utf-8"))
            if isinstance(data, list) and data:
                with open(local_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                LOGGER.info("Downloaded & cached %s (%d entries)", local_path.name, len(data))
                return data
        except Exception as exc:
            LOGGER.warning("Could not download instruments master from %s: %s", url, exc)

        # Emergency fallback to any local cache or repo NSE.json
        for fallback in [local_path, self.base_dir / "NSE.json", self.base_dir.parent / "NSE.json"]:
            if fallback.is_file() and fallback.stat().st_size > 100_000:
                try:
                    with open(fallback, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list) and data:
                        LOGGER.warning("Using fallback instruments file %s (%d entries)", fallback, len(data))
                        return data
                except Exception:
                    pass
        return []

    def _index_instruments(self, nse_data: list[dict[str, Any]], bse_data: list[dict[str, Any]], today_str: str) -> None:
        opts_by_sym: dict[str, list[dict[str, Any]]] = {}
        futs_by_sym: dict[str, dict[str, Any]] = {}
        eq_keys: dict[str, str] = {}

        for item in nse_data + bse_data:
            seg = str(item.get("segment") or "")
            itype = str(item.get("instrument_type") or "").upper()
            if seg == "NSE_EQ" and itype == "EQ":
                tsym = str(item.get("trading_symbol") or "").upper()
                if tsym and item.get("instrument_key"):
                    eq_keys[tsym] = str(item["instrument_key"])
                continue

            if seg not in ("NSE_FO", "BSE_FO"):
                continue

            sym = str(item.get("underlying_symbol") or item.get("asset_symbol") or "").upper()
            if not sym:
                continue

            exp_ms = int(item.get("expiry") or 0)
            if exp_ms <= 0:
                continue
            # Convert expiry epoch ms (in IST) to YYYY-MM-DD
            exp_date = datetime.fromtimestamp(exp_ms / 1000.0, IST).strftime("%Y-%m-%d")
            if exp_date < today_str:
                continue

            normalized = {
                "instrument_key": str(item.get("instrument_key") or ""),
                "trading_symbol": str(item.get("trading_symbol") or ""),
                "underlying_symbol": sym,
                "strike_price": float(item.get("strike_price") or 0.0),
                "instrument_type": itype,
                "expiry": exp_date,
                "expiry_ms": exp_ms,
                "lot_size": int(item.get("lot_size") or 1),
                "tick_size": float(item.get("tick_size") or 0.05),
                "segment": seg,
            }

            if itype in ("CE", "PE"):
                opts_by_sym.setdefault(sym, []).append(normalized)
            elif itype == "FUT":
                prev_fut = futs_by_sym.get(sym)
                if not prev_fut or exp_date < prev_fut["expiry"]:
                    futs_by_sym[sym] = normalized

        # Normalize aliases (e.g., TMPV -> TATAMOTORS)
        for canonical, aliases in SYMBOL_ALIASES.items():
            if canonical not in opts_by_sym:
                for alias in aliases:
                    if alias in opts_by_sym:
                        opts_by_sym[canonical] = opts_by_sym[alias]
                        break
            if canonical not in futs_by_sym:
                for alias in aliases:
                    if alias in futs_by_sym:
                        futs_by_sym[canonical] = futs_by_sym[alias]
                        break

        # Compute nearest expiry & official exchange lot_size per underlying symbol
        nearest_opts: dict[str, list[dict[str, Any]]] = {}
        lot_sizes: dict[str, int] = {}
        for sym, lst in opts_by_sym.items():
            min_exp = min(c["expiry"] for c in lst)
            active = [c for c in lst if c["expiry"] == min_exp]
            nearest_opts[sym] = active
            if active:
                lot_sizes[sym] = int(active[0].get("lot_size") or 1)

        self._options_by_sym = nearest_opts
        self._lot_sizes_by_sym = lot_sizes
        self._futures_by_sym = futs_by_sym
        self._equity_keys_by_sym = eq_keys
        LOGGER.info(
            "Indexed daily instruments master: %d option underlyings, %d futures, %d equities.",
            len(nearest_opts), len(futs_by_sym), len(eq_keys)
        )

    def get_nearest_options(self, symbol: str) -> tuple[str, int, list[dict[str, Any]]]:
        """Returns (expiry_date, official_lot_size, contracts_list) for the nearest expiry."""
        self.ensure_loaded()
        sym_u = symbol.upper().strip()
        contracts = self._options_by_sym.get(sym_u, [])
        if not contracts and sym_u in SYMBOL_ALIASES:
            for a in SYMBOL_ALIASES[sym_u]:
                if a in self._options_by_sym:
                    contracts = self._options_by_sym[a]
                    break
        if not contracts:
            return "", 0, []
        return contracts[0]["expiry"], int(contracts[0].get("lot_size") or 1), list(contracts)

    def get_lot_size(self, symbol: str, default: int = 1) -> int:
        self.ensure_loaded()
        sym_u = symbol.upper().strip()
        if sym_u in self._lot_sizes_by_sym:
            return self._lot_sizes_by_sym[sym_u]
        if sym_u in SYMBOL_ALIASES:
            for a in SYMBOL_ALIASES[sym_u]:
                if a in self._lot_sizes_by_sym:
                    return self._lot_sizes_by_sym[a]
        return default

    def get_nearest_future(self, symbol: str) -> dict[str, Any] | None:
        self.ensure_loaded()
        return self._futures_by_sym.get(symbol.upper().strip())
