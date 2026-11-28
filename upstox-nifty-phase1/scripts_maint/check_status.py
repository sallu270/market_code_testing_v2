import sqlite3
import time

DB_URI = "file:///d:/Salauddin-projects/algo_trading/upstox-nifty-phase1/market_data.db?mode=ro"
CUTOFF_MS = 1795631400000  # 2026-11-26 00:00:00 IST

con = sqlite3.connect(DB_URI, uri=True)

# 1. Quick min/max id on ticks
min_id = con.execute("SELECT MIN(id) FROM ticks").fetchone()[0]
max_id = con.execute("SELECT MAX(id) FROM ticks").fetchone()[0]
print(f"ticks id range: min={min_id}, max={max_id}")

if min_id is not None and max_id is not None:
    min_row = con.execute("SELECT id, received_timestamp_ms FROM ticks WHERE id = ?", (min_id,)).fetchone()
    print(f"min_id row: id={min_row[0]}, timestamp={min_row[1]} (cutoff={CUTOFF_MS})")
    
    # Binary search for cutoff ID
    low = min_id
    high = max_id
    cutoff_id = None
    
    while low <= high:
        mid = (low + high) // 2
        # Find nearest existing row >= mid
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
            
    print(f"Cutoff ID determined: {cutoff_id}")
    if cutoff_id:
        row = con.execute("SELECT id, received_timestamp_ms FROM ticks WHERE id = ?", (cutoff_id,)).fetchone()
        print(f"Row at cutoff_id: id={row[0]}, timestamp={row[1]}")
        prev_row = con.execute("SELECT id, received_timestamp_ms FROM ticks WHERE id < ? ORDER BY id DESC LIMIT 1", (cutoff_id,)).fetchone()
        print(f"Row before cutoff_id: {prev_row}")

con.close()
