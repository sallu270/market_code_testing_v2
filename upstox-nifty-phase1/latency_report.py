"""Feed Latency and Delay Report for Upstox Market Data.

Measures the exact delay between:
  1. Exchange timestamp (when the trade / index was generated on NSE)
  2. Local receive timestamp (when your machine received it over Upstox WebSocket)
  3. Real-time latency distribution across trading hours

Usage:
  python latency_report.py                   (analyzes market_data.db)
  python latency_report.py path/to/file.db   (analyzes specified SQLite DB)
  python latency_report.py path/to/feed.jsonl (analyzes JSONL feed file)
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def format_ms_and_sec(ms: float) -> str:
    sec = ms / 1000.0
    return f"{ms:6.1f} ms ({sec:5.2f}s)"


def render_ascii_histogram(vals: list[float], bins: int = 10) -> None:
    if not vals:
        return
    min_v, max_v = vals[0], vals[-1]
    if min_v == max_v:
        print(f"  All values = {min_v:.0f} ms")
        return

    # Cap outliers at P99 for a meaningful histogram
    p99 = pct(vals, 99)
    plot_vals = [v for v in vals if v <= p99]
    if not plot_vals:
        plot_vals = vals

    p_min, p_max = plot_vals[0], plot_vals[-1]
    step = (p_max - p_min) / bins if p_max > p_min else 1.0
    counts = [0] * bins

    for v in plot_vals:
        idx = min(bins - 1, int((v - p_min) / step))
        counts[idx] += 1

    max_c = max(counts) or 1
    bar_width = 36

    print("\n  Latency Distribution (up to P99):")
    for i in range(bins):
        low = p_min + i * step
        high = low + step
        bar_len = int((counts[i] / max_c) * bar_width)
        bar = "#" * bar_len
        pct_share = (counts[i] / len(plot_vals)) * 100
        print(f"  [{low:6.0f} - {high:6.0f} ms] | {bar:<{bar_width}} {counts[i]:5d} ({pct_share:4.1f}%)")


def analyze_sqlite(db_path: Path) -> None:
    if not db_path.exists():
        sys.exit(f"Database file not found: {db_path}")

    print(f"\n{'='*72}")
    print(f"  UPSTOX NIFTY MARKET DATA LATENCY & DELAY REPORT")
    print(f"{'='*72}")
    print(f"Database: {db_path.resolve()}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 1. Total ticks
    total_ticks = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    if total_ticks == 0:
        print("\nNo ticks recorded in database yet.")
        return

    # 2. Query valid ticks with positive timestamps
    query = """
        SELECT
            instrument_key,
            exchange_timestamp_ms,
            received_timestamp_ms,
            ltp
        FROM ticks
        WHERE exchange_timestamp_ms > 0 AND received_timestamp_ms > 0
        ORDER BY received_timestamp_ms ASC
    """
    rows = conn.execute(query).fetchall()

    delays: list[tuple[str, float]] = []
    gaps: list[float] = []
    instruments_count = defaultdict(int)
    last_rec: dict[str, int] = {}

    for r in rows:
        key = str(r["instrument_key"] or "NIFTY")
        instruments_count[key] += 1
        t = int(r["exchange_timestamp_ms"])
        rec = int(r["received_timestamp_ms"])

        if key in last_rec:
            gap = rec - last_rec[key]
            if gap >= 0:
                gaps.append(float(gap))
        last_rec[key] = rec

        hour = datetime.fromtimestamp(rec / 1000.0, IST).strftime("%H:00")
        delays.append((hour, float(rec - t)))

    print(f"Total Ticks Analyzed: {len(delays):,} / {total_ticks:,}")
    for inst, count in instruments_count.items():
        print(f"  • {inst}: {count:,} ticks")

    # Overall Latency Stats
    vals = sorted(v for _, v in delays)
    n = len(vals)
    median_val = statistics.median(vals)
    mean_val = statistics.mean(vals)
    p50 = pct(vals, 50)
    p75 = pct(vals, 75)
    p90 = pct(vals, 90)
    p95 = pct(vals, 95)
    p99 = pct(vals, 99)
    min_v = vals[0]
    max_v = vals[-1]

    print(f"\n{'-'*72}")
    print("  EXCHANGE-TO-LOCALHOST LATENCY SUMMARY")
    print(f"{'-'*72}")
    print(f"  • Median Latency (P50):  {format_ms_and_sec(median_val)}")
    print(f"  • Average Latency:       {format_ms_and_sec(mean_val)}")
    print(f"  • 75% of ticks under:    {format_ms_and_sec(p75)}")
    print(f"  • 90% of ticks under:    {format_ms_and_sec(p90)}")
    print(f"  • 95% of ticks under:    {format_ms_and_sec(p95)}")
    print(f"  • 99% of ticks under:    {format_ms_and_sec(p99)}")
    print(f"  • Minimum Observed Delay:{format_ms_and_sec(min_v)}")
    print(f"  • Maximum Observed Delay:{format_ms_and_sec(max_v)}")

    # Negative timestamps check (Clock skew)
    neg_count = sum(1 for v in vals if v < 0)
    if neg_count > 0:
        neg_pct = (neg_count / n) * 100
        print(f"\n  [NOTE] Clock Skew Detected: {neg_count:,} ticks ({neg_pct:.2f}%) had negative latency.")
        print("  Cause: Your Windows PC clock is ~50-100ms ahead of the exchange NTP server.")
        print("  Fix: Run Windows Time Sync (Settings -> Time & Language -> Date & Time -> 'Sync Now').")

    # Histogram
    render_ascii_histogram(vals)

    # Hourly Breakdown
    by_hour: dict[str, list[float]] = defaultdict(list)
    for h, v in delays:
        by_hour[h].append(v)

    print(f"\n{'-'*72}")
    print("  LATENCY BY TRADING HOUR (IST)")
    print(f"{'-'*72}")
    print("  Hour  | Ticks Count |  Median Delay  |    P95 Delay   | Min Delay | Max Delay")
    print("  ------|-------------|----------------|----------------|-----------|-----------")
    for h in sorted(by_hour):
        s = sorted(by_hour[h])
        h_med = statistics.median(s)
        h_p95 = pct(s, 95)
        h_min = s[0]
        h_max = s[-1]
        print(f"  {h} | {len(s):11,d} | {h_med:8.1f} ms   | {h_p95:8.1f} ms   | {h_min:6.0f} ms | {h_max:6.0f} ms")

    # Tick Gaps (Feed Frequency)
    if gaps:
        g = sorted(gaps)
        g_med = statistics.median(g)
        g_p90 = pct(g, 90)
        g_p95 = pct(g, 95)
        ticks_per_sec = 1000.0 / g_med if g_med > 0 else 0
        print(f"\n{'-'*72}")
        print("  TICK STREAMING FREQUENCY (Gap Between Ticks)")
        print(f"{'-'*72}")
        print(f"  • Median interval between ticks: {g_med:.0f} ms (~{ticks_per_sec:.1f} updates per second)")
        print(f"  • 90% of updates arrive within:  {g_p90:.0f} ms")
        print(f"  • 95% of updates arrive within:  {g_p95:.0f} ms")

    print(f"\n{'='*72}")
    print("  LATENCY PIPELINE BREAKDOWN (What This Means for You):")
    print("   1. Exchange (NSE) -> Generates tick")
    print("   2. Upstox Cloud Feed Server -> Dispatches Protobuf binary tick (~100-300ms)")
    print("   3. Your Internet Connection -> Arrives at your local Python WebSocket (~50-150ms)")
    print("   4. Python -> Localhost Web Browser via SSE (~1-3ms localhost delay)")
    print(f"   => TOTAL DELAY TO YOUR SCREEN: ~{median_val:.0f} ms ({median_val/1000:.2f} seconds)")
    print(f"{'='*72}\n")


def analyze_jsonl(jsonl_path: Path) -> None:
    last_vol = {}
    last_rec = {}
    delays = []
    gaps = []
    total = 0
    skipped_no_trade = 0
    skipped_no_time = 0

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            key = d.get("key", "NIFTY")
            t = d.get("t_ms")
            rec = d.get("rec_ms")
            vol = d.get("vol", 0)

            if rec and key in last_rec:
                gaps.append(rec - last_rec[key])
            if rec:
                last_rec[key] = rec

            prev = last_vol.get(key)
            last_vol[key] = vol
            if prev is not None and vol <= prev:
                skipped_no_trade += 1
                continue
            if not t or not rec or t == rec:
                skipped_no_time += 1
                continue
            hour = datetime.fromtimestamp(rec / 1000.0, IST).strftime("%H:00")
            delays.append((hour, float(rec - t)))

    print(f"File: {jsonl_path}")
    print(f"Rows read: {total}")
    print(f"Used for delay: {len(delays)}")
    if not delays:
        print("No valid delay records found.")
        return

    vals = sorted(v for _, v in delays)
    print("\nDelay from exchange trade time (milliseconds):")
    print(f"  Median: {statistics.median(vals):.0f} ms | P95: {pct(vals, 95):.0f} ms | P99: {pct(vals, 99):.0f} ms")


def main() -> None:
    target_path: Path | None = None

    if len(sys.argv) >= 2:
        target_path = Path(sys.argv[1])
    else:
        # Default to market_data.db in the same directory or project root
        current_dir = Path(__file__).resolve().parent
        db_file = current_dir / "market_data.db"
        if db_file.exists():
            target_path = db_file
        else:
            # Fallback check
            target_path = Path("market_data.db")

    if not target_path.exists():
        sys.exit(f"File not found: {target_path}\nRun with: python latency_report.py [path/to/market_data.db]")

    if target_path.suffix == ".jsonl":
        analyze_jsonl(target_path)
    else:
        analyze_sqlite(target_path)


if __name__ == "__main__":
    main()
