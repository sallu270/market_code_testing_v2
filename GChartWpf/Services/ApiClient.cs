using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using GChartWpf.Models;

namespace GChartWpf.Services;

/// <summary>Talks to the existing Python/Flask bot (main.py). No bot logic lives here.</summary>
public class ApiClient : IDataSource
{
    readonly HttpClient _http;
    readonly Dictionary<string, string> _expiries = new();

    public ApiClient(string baseUrl)
    {
        _http = new HttpClient { BaseAddress = new Uri(baseUrl), Timeout = TimeSpan.FromSeconds(8) };
    }

    // ---------- JSON helpers ----------
    static double D(JsonElement e, string name)
    {
        if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty(name, out var v)) return 0;
        if (v.ValueKind == JsonValueKind.Number) return v.GetDouble();
        if (v.ValueKind == JsonValueKind.String &&
            double.TryParse(v.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out var d)) return d;
        return 0;
    }
    static string S(JsonElement e, string name)
    {
        if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty(name, out var v)) return "";
        return v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : v.ValueKind == JsonValueKind.Null ? "" : v.ToString();
    }
    static JsonElement O(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) ? v : default;

    static IEnumerable<JsonElement> Arr(JsonElement e, string name)
    {
        var v = O(e, name);
        return v.ValueKind == JsonValueKind.Array ? v.EnumerateArray().ToList() : new List<JsonElement>();
    }

    async Task<JsonDocument> GetJson(string url)
    {
        using var resp = await _http.GetAsync(url);
        resp.EnsureSuccessStatusCode();
        return JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
    }

    // ---------- chart ----------
    public async Task<List<Candle>> GetCandlesAsync(string key, string tf)
    {
        if (tf == "tick") tf = "1m";
        using var doc = await GetJson($"/api/candles?instrument_key={Uri.EscapeDataString(key)}&timeframe={tf}&limit=300");
        var list = new List<Candle>();
        foreach (var c in Arr(doc.RootElement, "candles"))
            list.Add(new Candle((long)D(c, "candle_start_ms"), D(c, "open"), D(c, "high"), D(c, "low"), D(c, "close"), D(c, "volume")));
        return list.OrderBy(c => c.TimeMs).ToList();
    }

    // ---------- option chain ----------
    public async Task<ChainSnapshot?> GetChainAsync(string key)
    {
        var url = $"/api/option-chain?instrument_key={Uri.EscapeDataString(key)}";
        if (_expiries.TryGetValue(key, out var exp) && !string.IsNullOrEmpty(exp)) url += $"&expiry_date={exp}";
        using var doc = await GetJson(url);
        var root = doc.RootElement;
        var snap = new ChainSnapshot { Spot = D(root, "spot_price"), Expiry = S(root, "expiry_date") };
        if (snap.Expiry != "") _expiries[key] = snap.Expiry;

        foreach (var s in Arr(root, "strikes"))
            snap.Strikes.Add(new ChainStrike { Strike = D(s, "strike_price"), Ce = Leg(O(s, "call_options")), Pe = Leg(O(s, "put_options")) });
        return snap;
    }

    static OptionLeg Leg(JsonElement opt)
    {
        var m = O(opt, "market_data");
        var leg = new OptionLeg
        {
            Key = S(opt, "instrument_key"),
            Ltp = D(m, "ltp"), Volume = D(m, "volume"), Oi = D(m, "oi"), PrevOi = D(m, "prev_oi"),
            Close = D(m, "close_price"), Bid = D(m, "bid_price"), Ask = D(m, "ask_price")
        };
        leg.BuildUp = BuildUp.Classify(leg.Ltp, leg.Close, leg.Oi, leg.PrevOi);
        return leg;
    }

    // ---------- bot status ----------
    public async Task<StatusInfo?> GetStatusAsync()
    {
        using var doc = await GetJson("/api/strategy/status");
        var r = doc.RootElement;
        var pos = O(r, "current_position");
        bool has = pos.ValueKind == JsonValueKind.Object && S(pos, "status") != "CLOSED";
        var idxLots = O(r, "index_lots");
        int nLots = 1, bnLots = 1, sLots = 1;
        if (idxLots.ValueKind == JsonValueKind.Object)
        {
            if (idxLots.TryGetProperty("NSE_INDEX|Nifty 50", out var np) && np.TryGetInt32(out var nv)) nLots = nv;
            if (idxLots.TryGetProperty("NSE_INDEX|Nifty Bank", out var bnp) && bnp.TryGetInt32(out var bnv)) bnLots = bnv;
            if (idxLots.TryGetProperty("BSE_INDEX|SENSEX", out var sp) && sp.TryGetInt32(out var sv)) sLots = sv;
        }

        var idxEnabled = O(r, "index_enabled");
        bool nEn = true, bnEn = true, sEn = true;
        if (idxEnabled.ValueKind == JsonValueKind.Object)
        {
            if (idxEnabled.TryGetProperty("NSE_INDEX|Nifty 50", out var nep)) nEn = nep.ValueKind == JsonValueKind.True;
            if (idxEnabled.TryGetProperty("NSE_INDEX|Nifty Bank", out var bnep)) bnEn = bnep.ValueKind == JsonValueKind.True;
            if (idxEnabled.TryGetProperty("BSE_INDEX|SENSEX", out var sep)) sEn = sep.ValueKind == JsonValueKind.True;
        }

        var idxUnderlyings = new Dictionary<string, IndexStatusInfo>();
        if (r.TryGetProperty("index_underlyings", out var iuElem) && iuElem.ValueKind == JsonValueKind.Object)
        {
            foreach (var prop in iuElem.EnumerateObject())
            {
                var val = prop.Value;
                var info = new IndexStatusInfo
                {
                    Key = prop.Name,
                    Name = S(val, "name"),
                    State = S(val, "state"),
                    SpotPrice = D(val, "spot_price"),
                    HasPosition = val.TryGetProperty("has_position", out var hp) && hp.ValueKind == JsonValueKind.True,
                    TodayTradesCount = (int)D(val, "today_trades_count")
                };
                if (val.TryGetProperty("position", out var posElem) && posElem.ValueKind == JsonValueKind.Object)
                {
                    info.PositionSymbol = S(posElem, "symbol");
                    info.EntryPrice = D(posElem, "entry_price");
                }
                if (val.TryGetProperty("last_evaluation", out var evalElem) && evalElem.ValueKind == JsonValueKind.Object)
                {
                    info.SummaryLine = S(evalElem, "summary_line");
                }
                idxUnderlyings[prop.Name] = info;
            }
        }

        return new StatusInfo
        {
            State = S(r, "state"), Spot = D(r, "spot_price"), Expiry = S(r, "active_expiry"),
            HasPosition = has, PositionSymbol = has ? S(pos, "symbol") : "", EntryPrice = has ? D(pos, "entry_price") : 0,
            TodayPnl = D(r, "today_net_pnl_inr"), TodayPoints = D(r, "today_pnl_points"), TodayTrades = (int)D(r, "today_trades"),
            NiftyLots = nLots, BankNiftyLots = bnLots, SensexLots = sLots,
            NiftyEnabled = nEn, BankNiftyEnabled = bnEn, SensexEnabled = sEn,
            DeltaCe = D(r, "delta_ce"), DeltaPe = D(r, "delta_pe"),
            CeBuildup = S(r, "ce_buildup"), PeBuildup = S(r, "pe_buildup"),
            Stars = S(r, "stars"), Decision = S(r, "decision"),
            SummaryLine = S(r, "summary_line"),
            IndexUnderlyings = idxUnderlyings,
            RuleNumber = r.TryGetProperty("rule_number", out var rp) && rp.ValueKind == JsonValueKind.Number ? rp.GetInt32() : null
        };
    }

    public async Task<List<TradeRow>> GetTradesAsync()
    {
        using var doc = await GetJson("/api/strategy/trades?limit=100");
        return Arr(doc.RootElement, "trades").Select(t => new TradeRow
        {
            TradeId = S(t, "trade_id"),
            Side = S(t, "side"),
            Symbol = S(t, "symbol"),
            EntryTime = S(t, "entry_time_ist") is { Length: > 0 } et ? et : "—",
            Entry = D(t, "entry_price"),
            ExitTime = S(t, "exit_time_ist") is { Length: > 0 } xt ? xt : "—",
            Exit = D(t, "exit_price"),
            NetPnl = D(t, "net_pnl"),
            Status = S(t, "status"),
            BotMs = D(t, "bot_latency_ms")
        }).ToList();
    }

    // ---------- stocks ----------
    public async Task<List<StockRow>> GetStocksAsync()
    {
        using var doc = await GetJson("/api/stocks/screener");
        return Arr(doc.RootElement, "stocks").Select(s => new StockRow
        {
            Symbol = S(s, "symbol"), Name = S(s, "name"), Key = S(s, "key"), LotSize = (int)D(s, "lot_size"),
            Price = D(s, "price"), ChangePct = D(s, "change_pct"), Volume = D(s, "volume"),
            BuildUp = S(s, "build_up") is { Length: > 0 } b ? b : "Neutral",
            Selected = s.TryGetProperty("selected", out var sel) && sel.ValueKind == JsonValueKind.True,
            Lots = Math.Max(1, (int)D(s, "lots"))
        }).ToList();
    }

    public async Task SaveSelectionsAsync(Dictionary<string, int> selections)
    {
        var body = JsonSerializer.Serialize(new { selections });
        using var resp = await _http.PostAsync("/api/stocks/select", new StringContent(body, Encoding.UTF8, "application/json"));
        resp.EnsureSuccessStatusCode();
    }

    public async Task<TopStocksConfigResponse?> GetTopStocksConfigAsync(int? topN = null)
    {
        var url = topN.HasValue ? $"/api/stocks/config?top_n={topN.Value}" : "/api/stocks/config";
        using var doc = await GetJson(url);
        var root = doc.RootElement;
        var res = new TopStocksConfigResponse
        {
            TopN = root.TryGetProperty("top_n", out var tn) ? tn.GetInt32() : 5,
            Stocks = new List<StockConfigRow>()
        };
        if (root.TryGetProperty("stocks", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var el in arr.EnumerateArray())
            {
                res.Stocks.Add(new StockConfigRow
                {
                    Symbol = S(el, "symbol"),
                    Name = S(el, "name"),
                    Key = S(el, "key"),
                    LotSize = (int)D(el, "lot_size"),
                    StrikeStep = D(el, "strike_step"),
                    Lots = Math.Max(1, (int)D(el, "lots")),
                    Enabled = !el.TryGetProperty("enabled", out var en) || en.ValueKind != JsonValueKind.False
                });
            }
        }
        return res;
    }

    public async Task SaveTopStocksConfigAsync(int topN, List<StockConfigRow> stocks)
    {
        var body = JsonSerializer.Serialize(new
        {
            top_n = topN,
            stocks = stocks.Select(s => new
            {
                symbol = s.Symbol,
                name = s.Name,
                key = s.Key,
                lot_size = s.LotSize,
                strike_step = s.StrikeStep,
                lots = s.Lots,
                enabled = s.Enabled
            })
        });
        using var resp = await _http.PostAsync("/api/stocks/config", new StringContent(body, Encoding.UTF8, "application/json"));
        resp.EnsureSuccessStatusCode();
    }

    public Task SaveIndexLotsAsync(int niftyLots, int bankNiftyLots, int sensexLots)
        => SaveIndexConfigAsync(niftyLots, bankNiftyLots, sensexLots, true, true, true);

    public async Task SaveIndexConfigAsync(int niftyLots, int bankNiftyLots, int sensexLots, bool niftyEnabled, bool bankNiftyEnabled, bool sensexEnabled)
    {
        var body = JsonSerializer.Serialize(new
        {
            index_lots = new Dictionary<string, int>
            {
                { "NSE_INDEX|Nifty 50", niftyLots },
                { "NSE_INDEX|Nifty Bank", bankNiftyLots },
                { "BSE_INDEX|SENSEX", sensexLots }
            },
            index_enabled = new Dictionary<string, bool>
            {
                { "NSE_INDEX|Nifty 50", niftyEnabled },
                { "NSE_INDEX|Nifty Bank", bankNiftyEnabled },
                { "BSE_INDEX|SENSEX", sensexEnabled }
            }
        });
        using var resp = await _http.PostAsync("/api/strategy/config", new StringContent(body, Encoding.UTF8, "application/json"));
        resp.EnsureSuccessStatusCode();
    }

    // ---------- action buttons ----------
    public async Task ExitPositionAsync(string? indexKey = null)
    {
        var body = JsonSerializer.Serialize(new { index = indexKey ?? "" });
        using var resp = await _http.PostAsync("/api/strategy/exit", new StringContent(body, Encoding.UTF8, "application/json"));
        resp.EnsureSuccessStatusCode();
    }

    public async Task ArmNowAsync(string? indexKey = null)
    {
        var body = JsonSerializer.Serialize(new { index = indexKey ?? "" });
        using var resp = await _http.PostAsync("/api/strategy/arm-now", new StringContent(body, Encoding.UTF8, "application/json"));
        resp.EnsureSuccessStatusCode();
    }
}
