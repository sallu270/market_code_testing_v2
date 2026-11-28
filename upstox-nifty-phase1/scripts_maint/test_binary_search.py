import sqlite3
import time

DB_PATH = r"d:\Salauddin-projects\algo_trading\upstox-nifty-phase1\market_data.db"
CUTOFF_MS = 1795631400000  # 2026-11-26 00:00:00 IST

con = sqlite3.connect(DB_PATH)

# Find max_id
t0 = time.time()
max_id = con.execute("SELECT id FROM ticks ORDER BY id DESC LIMIT 1").fetchone()[0]
min_id = con.execute("SELECT id FROM ticks ORDER BY id ASC LIMIT 1").fetchone()[0]
print(f"ticks id range: min={min_id}, max={max_id} (found in {time.time()-t0:.3f}s)")

# Binary search
low = min_id
high = max_id
cutoff_id = None

while low <= high:
    mid = (low + high) // 2
    row = con.execute("SELECT id, received_timestamp_ms FROM ticks WHERE id >= ? ORDER BY id ASC LIMIT 1", (mid,)).fetchone()
    if not row:
        high = mid - 1
        continue
    rid, rts = row
    if rts >= CUTOFF_MS:
        cutoff_id = rid
        high = rid - 1
    else:
        low = rid + 1

print(f"Binary search completed! cutoff_id = {cutoff_id}")
if cutoff_id:
    row = con.execute("SELECT id, received_timestamp_ms FROM ticks WHERE id = ?", (cutoff_id,)).fetchone()
    print(f"Row at cutoff_id: {row}")
    prev_row = con.execute("SELECT id, received_timestamp_ms FROM ticks WHERE id < ? ORDER BY id DESC LIMIT 1", (cutoff_id,)).fetchone()
    print(f"Row before cutoff_id: {prev_row}")

con.close()
