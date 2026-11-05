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
    string _expiry = "";

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
        using var doc = await GetJson($"/api/candles?instrument_key={Uri.EscapeDataString(key)}&timeframe={tf}&limit=90");
        var list = new List<Candle>();
        foreach (var c in Arr(doc.RootElement, "candles"))
            list.Add(new Candle((long)D(c, "candle_start_ms"), D(c, "open"), D(c, "high"), D(c, "low"), D(c, "close"), D(c, "volume")));
        return list.OrderBy(c => c.TimeMs).ToList();
    }

    // ---------- option chain ----------
    public async Task<ChainSnapshot?> GetChainAsync(string key)
    {
        var url = $"/api/option-chain?instrument_key={Uri.EscapeDataString(key)}";
        if (_expiry != "") url += $"&expiry_date={_expiry}";
        using var doc = await GetJson(url);
        var root = doc.RootElement;
        var snap = new ChainSnapshot { Spot = D(root, "spot_price"), Expiry = S(root, "expiry_date") };
        if (snap.Expiry != "") _expiry = snap.Expiry;

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
        return new StatusInfo
        {
            State = S(r, "state"), Spot = D(r, "spot_price"), Expiry = S(r, "active_expiry"),
            HasPosition = has, PositionSymbol = has ? S(pos, "symbol") : "", EntryPrice = has ? D(pos, "entry_price") : 0,
            TodayPnl = D(r, "today_net_pnl_inr"), TodayPoints = D(r, "today_pnl_points"), TodayTrades = (int)D(r, "today_trades")
        };
    }

    public async Task<List<TradeRow>> GetTradesAsync()
    {
        using var doc = await GetJson("/api/strategy/trades?limit=100");
        return Arr(doc.RootElement, "trades").Select(t => new TradeRow
        {
            TradeId = S(t, "trade_id"), Side = S(t, "side"), Symbol = S(t, "symbol"),
            Entry = D(t, "entry_price"), Exit = D(t, "exit_price"), NetPnl = D(t, "net_pnl"),
            Status = S(t, "status"), BotMs = D(t, "bot_latency_ms")
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

    // ---------- the only action button ----------
    public async Task ExitPositionAsync()
    {
        using var resp = await _http.PostAsync("/api/strategy/exit", new StringContent("{}", Encoding.UTF8, "application/json"));
        resp.EnsureSuccessStatusCode();
    }
}
