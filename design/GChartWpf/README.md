# GChart WPF – Gap Entry Terminal (UI for your Python bot)

Native Windows (.NET 8 WPF) front-end for your existing `upstox-nifty-phase1` Flask bot.
It only *displays* data and sends **Exit Position**. All strategy logic stays in Python.

## Layout
1. **Navbar** – NIFTY Index | Stocks | Trades Journal | Settings, chart period keypad (1 3 5 15 D), connection status
2. **Ticker strip** – clock, spot, bot state, expiry, position, P&L, red **Exit Position** button
3. **Chart first** – candlesticks + volume + OHLC legend
4. **Below the chart**
   - *NIFTY Index*: option chain with only **8 strikes** (4 at/below spot + 4 above) that slide as price moves.
     Columns: Build-up, Volume, Bid, Ask, LTP | STRIKE | LTP, Bid, Ask, Volume, Build-up
   - *Stocks*: filters **Top 10 / Top 20 / All**, **All / Buy / Sell**, rank by Volume or Change %, search,
     tick + lots, Save Selections. Click a stock to show its chart.
5. **Arm Now / Reset / full option chain are not in this UI.**

Build-up rule (option chain): price up + OI up = Long Build-up, price down + OI up = Short Build-up,
price up + OI down = Short Covering, price down + OI down = Long Unwinding.
Stocks: Long Build-up = BUY, Short Build-up = SELL (from your screener).

## Run
Requires Windows + .NET 8 SDK.

    dotnet run --project GChartWpf.csproj            # talks to http://127.0.0.1:5000
    dotnet run --project GChartWpf.csproj -- --demo  # fake data, no bot needed (to preview the design)

Start your bot first (`python main.py` in upstox-nifty-phase1). To use another address set env var
`GCHART_API=http://host:port` or edit `AppSettings.cs`.

## API endpoints used
/api/candles, /api/option-chain, /api/strategy/status, /api/strategy/trades,
/api/stocks/screener, /api/stocks/select, POST /api/strategy/exit

## Removing "Arm Now" from the Python side too (optional)
Delete the `/api/strategy/arm-now` route in `main.py` and `force_arm_now()` in `strategy_engine.py`.
(The old web page `templates/index.html` / `dashboard.js` also references it.)

## Files
- `MainWindow.xaml` – whole screen design
- `Themes/GChart.xaml` – colours and control styles (black / teal GChart look)
- `Controls/CandleChart.cs` – custom candlestick chart (no NuGet packages needed)
- `ViewModels/MainViewModel.cs` – refresh loop (1 s), filters, Exit Position
- `Services/ApiClient.cs` – calls your Flask API; `DemoSource.cs` – fake data
