"""
Vacuum script: compacts the cleaned database into a new compact file.
Uses VACUUM INTO to avoid running out of disk space on Drive D.
Once compacted and verified, replaces market_data.db with the compact version.
"""
import sqlite3
import os
import sys
import time
from datetime import datetime

DB_DIR = r"d:\Salauddin-projects\algo_trading\upstox-nifty-phase1"
DB_PATH = os.path.join(DB_DIR, "market_data.db")
COMPACT_PATH = os.path.join(DB_DIR, "market_data_compact.db")

print(f"[{datetime.now()}] Starting VACUUM INTO {COMPACT_PATH}...")

if os.path.exists(COMPACT_PATH):
    os.remove(COMPACT_PATH)

con = sqlite3.connect(DB_PATH)
con.execute("PRAGMA busy_timeout = 60000")

# Checkpoint WAL first to flush all commits into main db file
print(f"[{datetime.now()}] Checkpointing WAL...")
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

t0 = time.time()
print(f"[{datetime.now()}] Executing VACUUM INTO...")
# Escape single quotes in path for SQL
safe_path = COMPACT_PATH.replace("'", "''")
con.execute(f"VACUUM INTO '{safe_path}'")
con.close()
elapsed = time.time() - t0

compact_size_mb = os.path.getsize(COMPACT_PATH) / (1024 * 1024)
print(f"[{datetime.now()}] VACUUM INTO completed in {elapsed:.1f}s. Compact file size: {compact_size_mb:.2f} MB")

# Verify the compact DB
print(f"[{datetime.now()}] Verifying compact DB integrity...")
con_compact = sqlite3.connect(COMPACT_PATH)
integrity = con_compact.execute("PRAGMA integrity_check").fetchone()[0]
print(f"Integrity check: {integrity}")

if integrity != "ok":
    print("ERROR: Integrity check failed on compact database! Aborting replacement.")
    con_compact.close()
    sys.exit(1)

# Check row counts in compact DB
CUTOFF_MS = 1795631400000
CUTOFF_DATE_STR = '2026-11-26'

print(f"[{datetime.now()}] Verifying row counts in compact DB:")
for t, col in [('ticks', 'received_timestamp_ms'), ('candles_1m', 'candle_start_ms'), ('strategy_signals', 'timestamp_ms')]:
    cnt = con_compact.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    older = con_compact.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} < ?", (CUTOFF_MS,)).fetchone()[0]
    print(f"  {t}: total={cnt:,}, older (< cutoff)={older}")

alerts_cnt = con_compact.execute("SELECT COUNT(*) FROM strategy_alerts").fetchone()[0]
alerts_older = con_compact.execute("SELECT COUNT(*) FROM strategy_alerts WHERE alert_date < ?", (CUTOFF_DATE_STR,)).fetchone()[0]
print(f"  strategy_alerts: total={alerts_cnt:,}, older (< cutoff)={alerts_older}")

for t in ['strategy_trades', 'momentum_logs', 'baseline_snapshots', 'premarket_locks']:
    cnt = con_compact.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t} (PRESERVED): count={cnt:,}")

con_compact.close()

# Now safely swap files
print(f"\n[{datetime.now()}] Replacing original {DB_PATH} with compact version...")
# Remove WAL and SHM
for ext in ["-wal", "-shm"]:
    f = DB_PATH + ext
    if os.path.exists(f):
        try:
            os.remove(f)
            print(f"Removed {f}")
        except Exception as e:
            print(f"Note: could not remove {f}: {e}")

# Replace main db file
backup_old = os.path.join(DB_DIR, "market_data_pre_swap.db.bak")
if os.path.exists(backup_old):
    os.remove(backup_old)

# Rename old to bak, rename compact to original, then remove bak
os.rename(DB_PATH, backup_old)
os.rename(COMPACT_PATH, DB_PATH)
os.remove(backup_old)

final_size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"[{datetime.now()}] Replacement SUCCESSFUL! New DB size: {final_size_mb:.2f} MB")
print("VACUUM & RECLAIM COMPLETE.")
