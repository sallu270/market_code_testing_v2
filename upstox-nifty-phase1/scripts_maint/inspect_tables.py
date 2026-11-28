import sqlite3

DB_PATH = r"d:\Salauddin-projects\algo_trading\upstox-nifty-phase1\market_data.db"
con = sqlite3.connect(DB_PATH)

tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
print("TABLES IN DATABASE:")
for (t,) in tables:
    cnt = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t}: {cnt:,} rows")

con.close()
