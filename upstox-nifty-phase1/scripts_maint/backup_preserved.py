"""
Backup script: dumps all rows from preserved tables to a SQL file.
Preserved tables: strategy_trades, momentum_logs, baseline_snapshots, premarket_locks
No schema changes. No code changes. DB maintenance only.
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = r"d:\Salauddin-projects\algo_trading\upstox-nifty-phase1\market_data.db"
BACKUP_PATH = r"d:\Salauddin-projects\algo_trading\upstox-nifty-phase1\market_data_preserved_backup_20261128.sql"

PRESERVED_TABLES = ["strategy_trades", "momentum_logs", "baseline_snapshots", "premarket_locks"]

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row

lines = []
lines.append(f"-- Preserved tables backup created: {datetime.now().isoformat()}")
lines.append(f"-- Source DB: {DB_PATH}")
lines.append("-- Tables: " + ", ".join(PRESERVED_TABLES))
lines.append("")

total_rows = 0
for table in PRESERVED_TABLES:
    rows = [dict(r) for r in con.execute(f"SELECT * FROM {table}").fetchall()]
    lines.append(f"-- TABLE: {table} ({len(rows)} rows)")
    total_rows += len(rows)
    for row in rows:
        cols = list(row.keys())
        col_str = ", ".join(cols)
        val_parts = []
        for v in row.values():
            if v is None:
                val_parts.append("NULL")
            elif isinstance(v, (int, float)):
                val_parts.append(str(v))
            else:
                escaped = str(v).replace("'", "''")
                val_parts.append(f"'{escaped}'")
        val_str = ", ".join(val_parts)
        lines.append(f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({val_str});")
    lines.append("")

con.close()

with open(BACKUP_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

size_kb = os.path.getsize(BACKUP_PATH) / 1024
print(f"Backup complete: {BACKUP_PATH}")
print(f"Total rows backed up: {total_rows}")
print(f"Backup file size: {size_kb:.1f} KB")
