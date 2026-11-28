"""
Database cleanup script: deletes records older than 2026-11-26 from:
  - ticks (by received_timestamp_ms)
  - candles_1m (by candle_start_ms)
  - strategy_signals (by timestamp_ms)
  - strategy_alerts (by alert_date TEXT)

PRESERVES entirely: strategy_trades, momentum_logs, baseline_snapshots, premarket_locks
No Python source code or schema changes. DB maintenance only.
"""
import sqlite3
import time
from datetime import datetime

DB_PATH = r"d:\Salauddin-projects\algo_trading\upstox-nifty-phase1\market_data.db"

# Cutoff: 2026-11-26 00:00:00 IST (UTC+5:30) = 2026-11-25 18:30:00 UTC
# Milliseconds since epoch
CUTOFF_MS = 1795631400000  # 2026-11-26 00:00:00 IST
CUTOFF_DATE_STR = '2026-11-26'  # for TEXT date columns
BATCH_SIZE = 50000

print(f"[{datetime.now()}] Starting cleanup")
print(f"Cutoff ms: {CUTOFF_MS} ({CUTOFF_DATE_STR})")
print(f"Connecting to: {DB_PATH}")

con = sqlite3.connect(DB_PATH, timeout=120)

# Set pragmas for performance during bulk delete
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA synchronous=NORMAL")
con.execute("PRAGMA cache_size=-131072")  # 128MB cache
con.execute("PRAGMA temp_store=MEMORY")

# ── 1. DELETE from ticks (batched) ─────────────────────────────────────────
print(f"\n[{datetime.now()}] Deleting old ticks (received_timestamp_ms < {CUTOFF_MS})...")
total_ticks = 0
batch_num = 0
while True:
    t0 = time.time()
    cur = con.execute(
        "DELETE FROM ticks WHERE id IN (SELECT id FROM ticks WHERE received_timestamp_ms < ? LIMIT ?)",
        (CUTOFF_MS, BATCH_SIZE)
    )
    n = cur.rowcount
    con.commit()
    elapsed = time.time() - t0
    if n == 0:
        break
    total_ticks += n
    batch_num += 1
    print(f"  Batch {batch_num}: deleted {n} rows in {elapsed:.1f}s (total ticks deleted: {total_ticks:,})")

print(f"[{datetime.now()}] Ticks cleanup done. Total deleted: {total_ticks:,}")

# ── 2. DELETE from candles_1m ───────────────────────────────────────────────
print(f"\n[{datetime.now()}] Deleting old candles_1m (candle_start_ms < {CUTOFF_MS})...")
cur = con.execute("DELETE FROM candles_1m WHERE candle_start_ms < ?", (CUTOFF_MS,))
n_candles = cur.rowcount
con.commit()
print(f"[{datetime.now()}] candles_1m deleted: {n_candles:,} rows")

# ── 3. DELETE from strategy_signals ────────────────────────────────────────
print(f"\n[{datetime.now()}] Deleting old strategy_signals (timestamp_ms < {CUTOFF_MS})...")
cur = con.execute("DELETE FROM strategy_signals WHERE timestamp_ms < ?", (CUTOFF_MS,))
n_signals = cur.rowcount
con.commit()
print(f"[{datetime.now()}] strategy_signals deleted: {n_signals:,} rows")

# ── 4. DELETE from strategy_alerts ─────────────────────────────────────────
print(f"\n[{datetime.now()}] Deleting old strategy_alerts (alert_date < '{CUTOFF_DATE_STR}')...")
cur = con.execute("DELETE FROM strategy_alerts WHERE alert_date < ?", (CUTOFF_DATE_STR,))
n_alerts = cur.rowcount
con.commit()
print(f"[{datetime.now()}] strategy_alerts deleted: {n_alerts:,} rows")

con.close()

print(f"\n{'='*60}")
print(f"CLEANUP SUMMARY:")
print(f"  ticks deleted:            {total_ticks:,}")
print(f"  candles_1m deleted:       {n_candles:,}")
print(f"  strategy_signals deleted: {n_signals:,}")
print(f"  strategy_alerts deleted:  {n_alerts:,}")
print(f"{'='*60}")
print(f"[{datetime.now()}] All deletes complete. Run VACUUM next to reclaim disk space.")
