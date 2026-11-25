import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
conn = sqlite3.connect("upstox-nifty-phase1/market_data.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT candle_start_ms, open, high, low, close, is_closed 
    FROM candles_1m 
    WHERE instrument_key = 'NSE_INDEX|Nifty 50' 
    ORDER BY candle_start_ms DESC 
    LIMIT 40
""")
rows = list(reversed(cursor.fetchall()))
print(f"Total rows: {len(rows)}")
for r in rows:
    dt = datetime.fromtimestamp(r[0]/1000, tz=IST)
    print(f"{dt.strftime('%Y-%m-%d %H:%M:%S')} | O: {r[1]:.2f} | H: {r[2]:.2f} | L: {r[3]:.2f} | C: {r[4]:.2f} | closed: {r[5]}")

