"""
Tool to search Upstox instrument keys or refresh NSE master file.
Usage:
    python find_instruments.py --nifty
    python find_instruments.py --symbol NIFTY --strike 22700
    python find_instruments.py --symbol RELIANCE
    python find_instruments.py --download
"""

import argparse
import gzip
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
NSE_FILE = BASE_DIR / "NSE.json"
IST = ZoneInfo("Asia/Kolkata")
UPSTOX_NSE_GZ_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def download_latest_instruments() -> None:
    print(f"Downloading latest NSE instruments from Upstox...")
    req = urllib.request.Request(
        UPSTOX_NSE_GZ_URL,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req) as resp:
        compressed_data = resp.read()
    
    decompressed = gzip.decompress(compressed_data)
    with open(NSE_FILE, "wb") as f:
        f.write(decompressed)
    print(f"Successfully updated {NSE_FILE} ({len(decompressed) // 1024 // 1024} MB)")


def load_instruments() -> list[dict]:
    if not NSE_FILE.exists():
        print(f"File not found: {NSE_FILE}. Downloading automatically...")
        download_latest_instruments()

    with open(NSE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def format_expiry(expiry_ms) -> str:
    if not expiry_ms:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(expiry_ms) / 1000, tz=IST)
        return dt.strftime("%d-%b-%Y")
    except Exception:
        return str(expiry_ms)


def search_nifty(data: list[dict], strike: float | None = None, opt_type: str | None = None) -> None:
    print("\n" + "=" * 78)
    print(" 1. NIFTY SPOT INDEX")
    print("=" * 78)
    for d in data:
        if d.get("instrument_key") == "NSE_INDEX|Nifty 50" or (d.get("segment") == "NSE_INDEX" and d.get("trading_symbol") == "NIFTY"):
            print(f"  Name: {d.get('name'):<25} Symbol: {d.get('trading_symbol'):<10} Key: {d.get('instrument_key')}")
            break

    print("\n" + "=" * 78)
    print(" 2. NIFTY FUTURES (Active Contracts)")
    print("=" * 78)
    futures = [
        d for d in data
        if d.get("name") == "NIFTY" and d.get("instrument_type") == "FUT" and d.get("segment") == "NSE_FO"
    ]
    futures.sort(key=lambda x: int(x.get("expiry") or 0))
    for f in futures[:5]:
        expiry_str = format_expiry(f.get("expiry"))
        print(f"  {f.get('trading_symbol'):<28} Expiry: {expiry_str:<12} Key: {f.get('instrument_key')}")

    print("\n" + "=" * 78)
    print(" 3. NIFTY OPTIONS (CE / PE)")
    print("=" * 78)
    options = [
        d for d in data
        if d.get("name") == "NIFTY" and d.get("segment") == "NSE_FO" and d.get("instrument_type") in ("CE", "PE")
    ]
    if strike is not None:
        options = [d for d in options if float(d.get("strike_price") or 0) == strike]
    if opt_type is not None:
        options = [d for d in options if d.get("instrument_type") == opt_type.upper()]

    options.sort(key=lambda x: (int(x.get("expiry") or 0), float(x.get("strike_price") or 0), x.get("instrument_type")))

    if not options:
        print("  No options matched criteria.")
    else:
        for opt in options[:10]:
            expiry_str = format_expiry(opt.get("expiry"))
            print(f"  {opt.get('trading_symbol'):<32} Expiry: {expiry_str:<12} Key: {opt.get('instrument_key')}")


def search_custom(data: list[dict], query: str, instrument_type: str | None = None) -> None:
    query = query.upper()
    print("\n" + "=" * 78)
    print(f" SEARCH RESULTS FOR: '{query}'")
    print("=" * 78)
    matches = []
    for d in data:
        sym = str(d.get("trading_symbol") or "").upper()
        name = str(d.get("name") or "").upper()
        if query in sym or query in name:
            if instrument_type and d.get("instrument_type") != instrument_type.upper():
                continue
            matches.append(d)

    matches.sort(key=lambda x: (x.get("segment") != "NSE_EQ", x.get("trading_symbol")))
    for m in matches[:15]:
        expiry = format_expiry(m.get("expiry")) if m.get("expiry") else "—"
        print(f"  [{m.get('segment'):<7}] {m.get('trading_symbol'):<30} Expiry: {expiry:<12} Key: {m.get('instrument_key')}")
    if len(matches) > 15:
        print(f"  ... and {len(matches) - 15} more matches.")


def main():
    parser = argparse.ArgumentParser(description="Find Upstox Instrument Keys")
    parser.add_argument("--download", action="store_true", help="Download/refresh NSE.json from Upstox")
    parser.add_argument("--nifty", action="store_true", help="Display NIFTY Index, Futures, and ATM options")
    parser.add_argument("--symbol", type=str, help="Search by symbol (e.g. NIFTY, RELIANCE, BANKNIFTY)")
    parser.add_argument("--strike", type=float, help="Filter options by strike price")
    parser.add_argument("--type", type=str, help="Filter by instrument type (FUT, CE, PE, EQ)")

    args = parser.parse_args()

    if args.download:
        download_latest_instruments()
        return

    data = load_instruments()

    if args.nifty or (not args.symbol and not args.download):
        search_nifty(data, strike=args.strike, opt_type=args.type)
    elif args.symbol:
        if args.symbol.upper() == "NIFTY":
            search_nifty(data, strike=args.strike, opt_type=args.type)
        else:
            search_custom(data, args.symbol, args.type)


if __name__ == "__main__":
    main()
