import sqlite3
con = sqlite3.connect(r'd:\Salauddin-projects\algo_trading\upstox-nifty-phase1\market_data.db', timeout=30)
cutoff_ms = 1795631400000
cutoff_date = '2026-11-26'
print('=== CURRENT DB STATE (after restart) ===')
r = con.execute('SELECT COUNT(*) FROM ticks WHERE received_timestamp_ms < ?', (cutoff_ms,)).fetchone()
total = con.execute('SELECT COUNT(*) FROM ticks').fetchone()
print(f'ticks: total={total[0]:,}, still_to_delete={r[0]:,}')
r = con.execute('SELECT COUNT(*) FROM candles_1m WHERE candle_start_ms < ?', (cutoff_ms,)).fetchone()
total = con.execute('SELECT COUNT(*) FROM candles_1m').fetchone()
print(f'candles_1m: total={total[0]:,}, still_to_delete={r[0]:,}')
r = con.execute('SELECT COUNT(*) FROM strategy_signals WHERE timestamp_ms < ?', (cutoff_ms,)).fetchone()
total = con.execute('SELECT COUNT(*) FROM strategy_signals').fetchone()
print(f'strategy_signals: total={total[0]:,}, still_to_delete={r[0]:,}')
r = con.execute('SELECT COUNT(*) FROM strategy_alerts WHERE alert_date < ?', (cutoff_date,)).fetchone()
total = con.execute('SELECT COUNT(*) FROM strategy_alerts').fetchone()
print(f'strategy_alerts: total={total[0]:,}, still_to_delete={r[0]:,}')
for t in ['strategy_trades','momentum_logs','baseline_snapshots','premarket_locks']:
    c = con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()
    print(f'{t}: {c[0]:,} rows (preserved)')
con.close()
print('=== done ===')
