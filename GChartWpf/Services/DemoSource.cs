using GChartWpf.Models;

namespace GChartWpf.Services;

/// <summary>Fake data so you can preview the whole UI without the Python bot. Run with --demo.</summary>
public class DemoSource : IDataSource
{
    readonly Random _r = new();
    readonly Dictionary<string, List<Candle>> _hist = new();
    readonly Dictionary<string, double> _last = new();
    bool _hasPos = true;
    readonly string[] _syms = { "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","SBIN","ITC","LT","AXISBANK","BHARTIARTL",
                                "KOTAKBANK","MARUTI","SUNPHARMA","TATAMOTORS","TITAN","WIPRO","NTPC","ONGC","ADANIENT","BAJFINANCE" };
    List<StockRow>? _stocks;

    double Spot => _last.GetValueOrDefault(AppSettings.IndexKey, 23536.25);

    List<Candle> History(string key, double start)
    {
        if (_hist.TryGetValue(key, out var h)) return h;
        h = new List<Candle>(); double p = start;
        long t = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 60000 * 60000 - 120 * 60000L;
        for (int i = 0; i < 120; i++, t += 60000) { var c = NextCandle(t, ref p, start); h.Add(c); }
        _last[key] = p; return _hist[key] = h;
    }

    Candle NextCandle(long t, ref double p, double basis)
    {
        double o = p, vol = basis * 0.0012, cl = o + (_r.NextDouble() - 0.5) * vol * 2;
        double hi = Math.Max(o, cl) + _r.NextDouble() * vol, lo = Math.Min(o, cl) - _r.NextDouble() * vol;
        p = cl; return new Candle(t, o, hi, lo, cl, _r.Next(1000, 90000));
    }

    public Task<List<Candle>> GetCandlesAsync(string key, string tf)
    {
        double start = key switch
        {
            AppSettings.BankNiftyKey => 51200,
            AppSettings.SensexKey => 79400,
            AppSettings.NiftyKey => 24600,
            _ => 500 + Math.Abs(key.GetHashCode() % 3000)
        };
        var h = History(key, start);
        // nudge the last candle so the chart looks live
        double p = _last[key]; var last = h[^1]; double cl = p + (_r.NextDouble() - 0.5) * start * 0.0004;
        h[^1] = last with { Close = cl, High = Math.Max(last.High, cl), Low = Math.Min(last.Low, cl) }; _last[key] = cl;
        if (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 60000 * 60000 > last.TimeMs)
            h.Add(NextCandle(last.TimeMs + 60000, ref p, start));

        int n = tf switch { "3m" => 3, "5m" => 5, "15m" => 15, "1d" => 60, _ => 1 };
        if (n == 1) return Task.FromResult(h.ToList());
        var grouped = h.Select((c, i) => (c, g: i / n)).GroupBy(x => x.g).Select(g => new Candle(
            g.First().c.TimeMs, g.First().c.Open, g.Max(x => x.c.High), g.Min(x => x.c.Low), g.Last().c.Close, g.Sum(x => x.c.Volume))).ToList();
        return Task.FromResult(grouped);
    }

    public async Task<ChainSnapshot?> GetChainAsync(string key)
    {
        await GetCandlesAsync(key, "1m");
        double start = key switch
        {
            AppSettings.BankNiftyKey => 51200,
            AppSettings.SensexKey => 79400,
            _ => 24600
        };
        double spot = _last.GetValueOrDefault(key, start);
        double step = (key == AppSettings.BankNiftyKey || key == AppSettings.SensexKey) ? 100 : 50;
        string expiry = key switch
        {
            AppSettings.BankNiftyKey => "2026-11-11",
            AppSettings.SensexKey => "2026-11-13",
            _ => "2026-11-12"
        };
        double atm = Math.Round(spot / step) * step;
        var snap = new ChainSnapshot { Spot = spot, Expiry = expiry };
        for (int i = -4; i <= 4; i++)
        {
            double k = atm + i * step;
            snap.Strikes.Add(new ChainStrike { Strike = k, Ce = Leg(Math.Max(spot - k, 0), spot, k), Pe = Leg(Math.Max(k - spot, 0), spot, k) });
        }
        return snap;
    }

    OptionLeg Leg(double intrinsic, double spot, double k)
    {
        double ltp = Math.Round(intrinsic + 20 + _r.NextDouble() * 15, 2);
        double close = ltp + (_r.NextDouble() - 0.5) * 20;
        double oi = _r.Next(100000, 900000), prev = oi + _r.Next(-80000, 80000);
        return new OptionLeg
        {
            Ltp = ltp, Close = close, Oi = oi, PrevOi = prev, Volume = _r.Next(5000, 600000),
            Bid = Math.Round(ltp - 0.35, 2), Ask = Math.Round(ltp + 0.35, 2),
            BuildUp = BuildUp.Classify(ltp, close, oi, prev)
        };
    }

    public Task<StatusInfo?> GetStatusAsync() => Task.FromResult<StatusInfo?>(new StatusInfo
    {
        State = _hasPos ? "HOLDING" : "SETUP", Spot = Spot, Expiry = "10 NOV 2026", HasPosition = _hasPos,
        PositionSymbol = "NIFTY 10 Nov 2026 CE 23500", EntryPrice = 99.40, TodayPnl = _hasPos ? 1840.5 : 0, TodayPoints = 28.3, TodayTrades = 1,
        FuturesLtp = Spot + 28.5, FuturesOi = 1350000, FuturesBuildup = "Long Build-up"
    });

    public Task<List<TradeRow>> GetTradesAsync(string? date = "today") => Task.FromResult(new List<TradeRow>
    {
        new() { TradeId = "T-0001", TradeDate = DateTime.Today.ToString("yyyy-MM-dd"), Side = "CE", Symbol = "NIFTY 23400 CE", EntryTime = "09:15:00.829", Entry = 135.0, ExitTime = "15:15:00.029", Exit = 161.5, NetPnl = 1722, Status = "CLOSED", BotMs = 0.38 },
        new() { TradeId = "T-0002", TradeDate = DateTime.Today.ToString("yyyy-MM-dd"), Side = "PE", Symbol = "NIFTY 23600 PE", EntryTime = "09:15:00.120", Entry = 88.2, ExitTime = "15:15:00.029", Exit = 74.0, NetPnl = -923, Status = "CLOSED", BotMs = 0.25 },
        new() { TradeId = "T-0003", TradeDate = DateTime.Today.ToString("yyyy-MM-dd"), Side = "CE", Symbol = "NIFTY 23500 CE", EntryTime = "09:15:01.050", Entry = 99.4, ExitTime = "—", Exit = 0, NetPnl = 0, Status = "OPEN", BotMs = 0.42 },
    });

    public Task<List<string>> GetTradeDatesAsync() => Task.FromResult(new List<string>
    {
        DateTime.Today.ToString("yyyy-MM-dd")
    });

    public Task<List<StockRow>> GetStocksAsync()
    {
        string[] bu = { "Long Build-up", "Short Build-up", "Neutral", "Long Build-up", "Short Build-up" };
        _stocks = _syms.Select(s => new StockRow
        {
            Symbol = s, Name = s, Key = "NSE_EQ|" + s, LotSize = _r.Next(1, 20) * 50,
            Price = Math.Round(500 + _r.NextDouble() * 3000, 2), ChangePct = Math.Round((_r.NextDouble() - 0.5) * 6, 2),
            Volume = _r.Next(200000, 9000000), BuildUp = bu[_r.Next(bu.Length)]
        }).ToList();
        return Task.FromResult(_stocks);
    }

    public Task SaveSelectionsAsync(Dictionary<string, int> selections) => Task.CompletedTask;
    public Task<TopStocksConfigResponse?> GetTopStocksConfigAsync(int? topN = null)
    {
        var n = topN ?? 5;
        var names = new[] { "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "BHARTIARTL", "ITC", "SBIN", "LT", "BAJFINANCE" };
        var list = names.Take(n).Select(s => new StockConfigRow
        {
            Symbol = s,
            Name = s + " Ltd.",
            Key = "NSE_EQ|" + s,
            LotSize = 250,
            Lots = 1,
            Enabled = true
        }).ToList();
        return Task.FromResult<TopStocksConfigResponse?>(new TopStocksConfigResponse { TopN = n, Stocks = list });
    }
    public Task SaveTopStocksConfigAsync(int topN, List<StockConfigRow> stocks) => Task.CompletedTask;
    public Task SaveIndexLotsAsync(int niftyLots, int bankNiftyLots, int sensexLots) => Task.CompletedTask;
    public Task SaveIndexConfigAsync(int niftyLots, int bankNiftyLots, int sensexLots, bool niftyEnabled, bool bankNiftyEnabled, bool sensexEnabled) => Task.CompletedTask;
    public Task ExitPositionAsync(string? indexKey = null) { _hasPos = false; return Task.CompletedTask; }
    public Task ExitIndicesAsync() { _hasPos = false; return Task.CompletedTask; }
    public Task ExitStocksAsync() { _hasPos = false; return Task.CompletedTask; }
    public Task ArmNowAsync(string? indexKey = null) => Task.CompletedTask;
    public Task<(List<FlowRow> Rows, List<FlowAlert> Alerts)> GetFlowDashboardAsync() =>
        Task.FromResult((new List<FlowRow>
        {
            new FlowRow { Instrument = "NIFTY 50", Key = "NSE_INDEX|Nifty 50", VolPcr5m = 0.58, VolPcrShift = 0.62, DoiPcr = "+0.45", NetOiBias = 0.32, CeVsr = "3.1x", PeVsr = "0.8x", FlowState = "CONFIRMED_BULL", BotAction = "CONFIRMED: Institutional Call Flow Active", StraddleDecay = 0.45 },
            new FlowRow { Instrument = "BANK NIFTY", Key = "NSE_INDEX|Nifty Bank", VolPcr5m = 1.62, VolPcrShift = 1.68, DoiPcr = "-0.38", NetOiBias = -0.28, CeVsr = "0.7x", PeVsr = "2.9x", FlowState = "CONFIRMED_BEAR", BotAction = "CONFIRMED: Institutional Put Flow Active", StraddleDecay = 0.52 },
            new FlowRow { Instrument = "SENSEX", Key = "BSE_INDEX|SENSEX", VolPcr5m = 0.95, VolPcrShift = 1.02, DoiPcr = "N/A", NetOiBias = 0.04, CeVsr = "1.0x", PeVsr = "1.0x", FlowState = "NEUTRAL", BotAction = "Monitoring flow", StraddleDecay = 0.88 },
        }, new List<FlowAlert>
        {
            new FlowAlert { Time = "09:24:00", Instrument = "NIFTY 50", Event = "CONFIRMED_BULL", Detail = "Institutional Call Flow Active (Eligible Path 2/3)" }
        }));
}
