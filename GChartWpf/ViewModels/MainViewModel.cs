using System.Collections.ObjectModel;
using System.Globalization;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using GChartWpf.Models;
using GChartWpf.Services;

namespace GChartWpf.ViewModels;

public class MainViewModel : ObservableObject
{
    readonly IDataSource _src;
    readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(1) };
    bool _busy;
    DateTime _lastStocks = DateTime.MinValue, _lastTrades = DateTime.MinValue;

    public MainViewModel(IDataSource src)
    {
        _src = src;
        SetModeCommand = new RelayCommand(p => Mode = (string)p!);
        SetTimeframeCommand = new RelayCommand(p => { Timeframe = (string)p!; _ = RefreshAsync(); });
        SetTopNCommand = new RelayCommand(p => { TopN = int.Parse((string)p!); ApplyStockFilter(); });
        SetSideCommand = new RelayCommand(p => { SideFilter = (string)p!; ApplyStockFilter(); });
        SetRankCommand = new RelayCommand(p => { RankBy = (string)p!; ApplyStockFilter(); });
        SetStrikeTotalCommand = new RelayCommand(p => StrikeTotal = int.Parse((string)p!));
        ExitPositionCommand = new RelayCommand(_ => ExitPosition());
        RefreshStocksCommand = new RelayCommand(async _ => await LoadStocksAsync());
        SaveSelectionsCommand = new RelayCommand(async _ => await SaveSelectionsAsync());
        SaveIndexLotsCommand = new RelayCommand(async _ => await SaveIndexLotsAsync());
        SetNiftyLotsCommand = new RelayCommand(p => { if (int.TryParse(p?.ToString(), out var v)) NiftyLots = v; });
        SetBankNiftyLotsCommand = new RelayCommand(p => { if (int.TryParse(p?.ToString(), out var v)) BankNiftyLots = v; });
        SetSensexLotsCommand = new RelayCommand(p => { if (int.TryParse(p?.ToString(), out var v)) SensexLots = v; });
        ExportTradesCsvCommand = new RelayCommand(_ => ExportTradesCsv());

        _timer.Tick += async (_, _) => { Clock = DateTime.Now.ToString("HH:mm:ss"); await RefreshAsync(); };
        Clock = DateTime.Now.ToString("HH:mm:ss");
        _timer.Start();
        _ = RefreshAsync();
    }

    public void Stop() => _timer.Stop();

    // ---------------- commands ----------------
    public ICommand SetModeCommand { get; }
    public ICommand SetTimeframeCommand { get; }
    public ICommand SetTopNCommand { get; }
    public ICommand SetSideCommand { get; }
    public ICommand SetRankCommand { get; }
    public ICommand SetStrikeTotalCommand { get; }
    public ICommand ExitPositionCommand { get; }
    public ICommand RefreshStocksCommand { get; }
    public ICommand SaveSelectionsCommand { get; }
    public ICommand SaveIndexLotsCommand { get; }
    public ICommand SetNiftyLotsCommand { get; }
    public ICommand SetBankNiftyLotsCommand { get; }
    public ICommand SetSensexLotsCommand { get; }
    public ICommand ExportTradesCsvCommand { get; }

    // ---------------- navigation ----------------
    string _mode = "Nifty50";
    public string Mode
    {
        get => _mode;
        set
        {
            if (!Set(ref _mode, value)) return;
            OnPropertyChanged(nameof(IsNifty50Mode));
            OnPropertyChanged(nameof(IsBankNiftyMode));
            OnPropertyChanged(nameof(IsSensexMode));
            OnPropertyChanged(nameof(IsIndexMode));
            OnPropertyChanged(nameof(IsStocksMode));
            OnPropertyChanged(nameof(IsChartMode));
            OnPropertyChanged(nameof(IsJournalMode));
            OnPropertyChanged(nameof(IsSettingsMode));
            OnPropertyChanged(nameof(CurrentIndexKey));
            OnPropertyChanged(nameof(CurrentIndexName));
            OnPropertyChanged(nameof(ChartTitle));
            if (value == "Stocks" && Stocks.Count == 0) _ = LoadStocksAsync();
            Candles = null;
            _ = RefreshAsync();
        }
    }
    public bool IsNifty50Mode => Mode == "Nifty50" || Mode == "Index";
    public bool IsBankNiftyMode => Mode == "BankNifty";
    public bool IsSensexMode => Mode == "Sensex";
    public bool IsIndexMode => IsNifty50Mode || IsBankNiftyMode || IsSensexMode;
    public bool IsStocksMode => Mode == "Stocks";
    public bool IsChartMode => IsIndexMode;
    public bool IsJournalMode => Mode == "Journal";
    public bool IsSettingsMode => Mode == "Settings";

    public string CurrentIndexKey => Mode switch
    {
        "BankNifty" => AppSettings.BankNiftyKey,
        "Sensex" => AppSettings.SensexKey,
        _ => AppSettings.NiftyKey
    };

    public string CurrentIndexName => Mode switch
    {
        "BankNifty" => AppSettings.BankNiftyName,
        "Sensex" => AppSettings.SensexName,
        _ => AppSettings.NiftyName
    };

    public string ChartTitle => CurrentIndexName;

    // ---------------- header strip ----------------
    string _clock = "", _spotText = "NC=—", _state = "SETUP", _expiry = "", _posText = "FLAT (NO POSITION)", _pnlText = "₹0.00", _pts = "(0.00 pts)", _status = "Starting…", _connText = "Connecting";
    public string Clock { get => _clock; set => Set(ref _clock, value); }
    public string SpotText { get => _spotText; set => Set(ref _spotText, value); }
    public string State { get => _state; set => Set(ref _state, value); }
    public string Expiry { get => _expiry; set => Set(ref _expiry, value); }
    public string PositionText { get => _posText; set => Set(ref _posText, value); }
    public string PnlText { get => _pnlText; set => Set(ref _pnlText, value); }
    public string PointsText { get => _pts; set => Set(ref _pts, value); }
    public string StatusMessage { get => _status; set => Set(ref _status, value); }
    public string ConnText { get => _connText; set => Set(ref _connText, value); }
    Brush _pnlBrush = Pal.White, _chgBrush = Pal.White, _connBrush = Pal.Amber, _posBrush = Pal.Grey;
    public Brush PnlBrush { get => _pnlBrush; set => Set(ref _pnlBrush, value); }
    public Brush ChgBrush { get => _chgBrush; set => Set(ref _chgBrush, value); }
    public Brush ConnBrush { get => _connBrush; set => Set(ref _connBrush, value); }
    public Brush PosBrush { get => _posBrush; set => Set(ref _posBrush, value); }

    // ---------------- chart ----------------
    string _tf = "1m";
    public string Timeframe { get => _tf; set { if (Set(ref _tf, value)) OnPropertyChanged(nameof(IsDaily)); } }
    public bool IsDaily => Timeframe == "1d";
    IReadOnlyList<Candle>? _candles;
    public IReadOnlyList<Candle>? Candles { get => _candles; set => Set(ref _candles, value); }
    string _lo = "—", _lh = "—", _ll = "—", _lc = "—", _lchg = "0.00 (0.00%)", _lv = "—";
    public string LegendO { get => _lo; set => Set(ref _lo, value); }
    public string LegendH { get => _lh; set => Set(ref _lh, value); }
    public string LegendL { get => _ll; set => Set(ref _ll, value); }
    public string LegendC { get => _lc; set => Set(ref _lc, value); }
    public string LegendChg { get => _lchg; set => Set(ref _lchg, value); }
    public string LegendVol { get => _lv; set => Set(ref _lv, value); }

    // ---------------- option chain (8 dynamic strikes) ----------------
    public ObservableCollection<StrikeRow> Strikes { get; } = new();
    int _strikeTotal = 8;
    public int StrikeTotal { get => _strikeTotal; set { if (Set(ref _strikeTotal, value)) _ = RefreshAsync(); } }
    string _atmText = "ATM —";
    public string AtmText { get => _atmText; set => Set(ref _atmText, value); }

    // ---------------- stocks ----------------
    readonly List<StockRow> _allStocks = new();
    public ObservableCollection<StockRow> Stocks { get; } = new();
    public int TopN { get; private set; } = 10;       // 0 = all
    public string SideFilter { get; private set; } = "All";
    public string RankBy { get; private set; } = "Volume";
    string _search = "";
    public string Search { get => _search; set { if (Set(ref _search, value)) ApplyStockFilter(); } }
    StockRow? _selStock;
    public StockRow? SelectedStock
    {
        get => _selStock;
        set { if (Set(ref _selStock, value)) { OnPropertyChanged(nameof(ChartTitle)); _ = RefreshAsync(); } }
    }
    string _selCount = "0 selected";
    public string SelectedCount { get => _selCount; set => Set(ref _selCount, value); }
    string _stocksInfo = "";
    public string StocksInfo { get => _stocksInfo; set => Set(ref _stocksInfo, value); }

    // ---------------- journal ----------------
    public ObservableCollection<TradeRow> Trades { get; } = new();

    // ===================== refresh loop =====================
    async Task RefreshAsync()
    {
        if (_busy) return;
        _busy = true;
        try
        {
            string key = CurrentIndexKey;
            var tStatus = _src.GetStatusAsync();
            var tCandles = IsChartMode ? _src.GetCandlesAsync(key, Timeframe) : null;
            var tChain = IsIndexMode ? _src.GetChainAsync(key) : null;

            ApplyStatus(await tStatus);
            if (tCandles != null) ApplyCandles(await tCandles);
            if (tChain != null) ApplyChain(await tChain);

            if (IsStocksMode && (DateTime.Now - _lastStocks).TotalSeconds > 5) await LoadStocksAsync();
            if (IsJournalMode && (DateTime.Now - _lastTrades).TotalSeconds > 5) await LoadTradesAsync();

            ConnBrush = Pal.Green; ConnText = AppSettings.Demo ? "DEMO DATA" : "Connected";
        }
        catch (Exception ex)
        {
            ConnBrush = Pal.Red; ConnText = "Offline";
            StatusMessage = "Cannot reach bot at " + AppSettings.BaseUrl + " — " + ex.Message;
        }
        finally { _busy = false; }
    }

    void ApplyStatus(StatusInfo? s)
    {
        if (s == null) return;
        State = s.State.ToUpperInvariant();
        if (s.Expiry != "") Expiry = "EXP: " + s.Expiry;
        PositionText = s.HasPosition ? $"LONG {s.PositionSymbol} @ {s.EntryPrice:N2}" : "FLAT (NO POSITION)";
        PosBrush = s.HasPosition ? Pal.Green : Pal.Grey;
        PnlText = (s.TodayNetPnlSigned()) ;
        PointsText = $"({s.TodayPoints:N2} pts)";
        PnlBrush = s.TodayPnl > 0 ? Pal.Green : s.TodayPnl < 0 ? Pal.Red : Pal.White;
        if (!_lotsInitialized)
        {
            _lotsInitialized = true;
            if (s.NiftyLots > 0) NiftyLots = s.NiftyLots;
            if (s.BankNiftyLots > 0) BankNiftyLots = s.BankNiftyLots;
            if (s.SensexLots > 0) SensexLots = s.SensexLots;
        }
    }

    void ApplyCandles(List<Candle> c)
    {
        Candles = c;
        if (c.Count == 0) return;
        var last = c[^1]; var first = c[0];
        double chg = last.Close - first.Open, pct = first.Open > 0 ? chg / first.Open * 100 : 0;
        var ci = CultureInfo.InvariantCulture;
        LegendO = last.Open.ToString("N2", ci); LegendH = last.High.ToString("N2", ci);
        LegendL = last.Low.ToString("N2", ci); LegendC = last.Close.ToString("N2", ci);
        LegendVol = last.Volume.ToString("N0", ci);
        LegendChg = $"{chg:+0.00;-0.00;0.00} ({pct:+0.00;-0.00;0.00}%)";
        ChgBrush = chg >= 0 ? Pal.Green : Pal.Red;
        SpotText = $"NC={last.Close:N2} ({chg:+0.00;-0.00;0.00})";
    }

    void ApplyChain(ChainSnapshot? snap)
    {
        if (snap == null || snap.Strikes.Count == 0) return;
        var rows = ChainWindow.Pick(snap, StrikeTotal);
        Strikes.Clear();
        foreach (var r in rows) Strikes.Add(r);
        var atm = rows.FirstOrDefault(r => r.IsAtm);
        AtmText = atm != null ? $"ATM {atm.Strike:N0}  ·  SPOT {snap.Spot:N2}  ·  EXP {snap.Expiry}" : "ATM —";
    }

    // ===================== stocks =====================
    async Task LoadStocksAsync()
    {
        try
        {
            var fresh = await _src.GetStocksAsync();
            _lastStocks = DateTime.Now;
            // keep what the user ticked/typed locally
            var keep = _allStocks.ToDictionary(s => s.Symbol, s => (s.Selected, s.Lots));
            foreach (var s in _allStocks) s.PropertyChanged -= StockChanged;
            _allStocks.Clear();
            foreach (var s in fresh)
            {
                if (keep.TryGetValue(s.Symbol, out var k)) { s.Selected = k.Selected; s.Lots = k.Lots; }
                s.PropertyChanged += StockChanged; _allStocks.Add(s);
            }
            ApplyStockFilter();
        }
        catch (Exception ex) { StatusMessage = "Stock screener failed: " + ex.Message; }
    }

    void StockChanged(object? s, System.ComponentModel.PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(StockRow.Selected)) UpdateSelectedCount();
    }

    void UpdateSelectedCount() => SelectedCount = $"{_allStocks.Count(s => s.Selected)} selected";

    void ApplyStockFilter()
    {
        IEnumerable<StockRow> q = _allStocks;
        if (SideFilter == "Buy") q = q.Where(s => s.Signal == "BUY");
        else if (SideFilter == "Sell") q = q.Where(s => s.Signal == "SELL");
        if (!string.IsNullOrWhiteSpace(Search))
            q = q.Where(s => s.Symbol.Contains(Search.Trim(), StringComparison.OrdinalIgnoreCase));

        q = RankBy == "Change %" ? q.OrderByDescending(s => Math.Abs(s.ChangePct)) : q.OrderByDescending(s => s.Volume);
        if (TopN > 0) q = q.Take(TopN);

        var list = q.ToList();
        string? keep = SelectedStock?.Symbol;
        Stocks.Clear();
        foreach (var s in list) Stocks.Add(s);
        SelectedStock = Stocks.FirstOrDefault(s => s.Symbol == keep) ?? Stocks.FirstOrDefault();
        StocksInfo = $"Showing {list.Count} of {_allStocks.Count}  ·  {(TopN > 0 ? "Top " + TopN : "All")}  ·  {SideFilter}  ·  by {RankBy}";
        UpdateSelectedCount();
    }

    // ---------------- settings : morning lots ----------------
    bool _lotsInitialized = false;
    int _niftyLots = 1;
    public int NiftyLots { get => _niftyLots; set { if (Set(ref _niftyLots, Math.Max(1, value))) OnPropertyChanged(nameof(NiftyLotsPreview)); } }
    public string NiftyLotsPreview => $"{NiftyLots} lot{(NiftyLots > 1 ? "s" : "")} = {NiftyLots * 65} qty";

    int _bankNiftyLots = 1;
    public int BankNiftyLots { get => _bankNiftyLots; set { if (Set(ref _bankNiftyLots, Math.Max(1, value))) OnPropertyChanged(nameof(BankNiftyLotsPreview)); } }
    public string BankNiftyLotsPreview => $"{BankNiftyLots} lot{(BankNiftyLots > 1 ? "s" : "")} = {BankNiftyLots * 15} qty";

    int _sensexLots = 1;
    public int SensexLots { get => _sensexLots; set { if (Set(ref _sensexLots, Math.Max(1, value))) OnPropertyChanged(nameof(SensexLotsPreview)); } }
    public string SensexLotsPreview => $"{SensexLots} lot{(SensexLots > 1 ? "s" : "")} = {SensexLots * 10} qty";

    async Task SaveIndexLotsAsync()
    {
        try
        {
            await _src.SaveIndexLotsAsync(NiftyLots, BankNiftyLots, SensexLots);
            StatusMessage = $"Saved morning lots: NIFTY 50={NiftyLots} ({NiftyLots * 65} qty), BANK NIFTY={BankNiftyLots} ({BankNiftyLots * 15} qty), SENSEX={SensexLots} ({SensexLots * 10} qty).";
        }
        catch (Exception ex) { StatusMessage = "Failed to save lots: " + ex.Message; }
    }

    async Task SaveSelectionsAsync()
    {
        try
        {
            var sel = _allStocks.Where(s => s.Selected).ToDictionary(s => s.Symbol, s => s.Lots);
            await _src.SaveSelectionsAsync(sel);
            StatusMessage = $"Saved {sel.Count} stock selection(s).";
        }
        catch (Exception ex) { StatusMessage = "Save failed: " + ex.Message; }
    }

    async Task LoadTradesAsync()
    {
        _lastTrades = DateTime.Now;
        var t = await _src.GetTradesAsync();
        Trades.Clear(); foreach (var x in t) Trades.Add(x);
    }

    // ===================== the single action button =====================
    async void ExitPosition()
    {
        var ok = MessageBox.Show("Exit the open position right now?", "Exit Position",
                                 MessageBoxButton.YesNo, MessageBoxImage.Warning);
        if (ok != MessageBoxResult.Yes) return;
        try { await _src.ExitPositionAsync(); StatusMessage = "Exit order sent."; await RefreshAsync(); }
        catch (Exception ex) { StatusMessage = "Exit failed: " + ex.Message; }
    }

    void ExportTradesCsv()
    {
        try
        {
            if (Trades.Count == 0)
            {
                MessageBox.Show("No executed trades available to export.", "Trades Journal", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            var sfd = new Microsoft.Win32.SaveFileDialog
            {
                FileName = $"trades_journal_{DateTime.Now:yyyy-MM-dd}.csv",
                DefaultExt = ".csv",
                Filter = "CSV files (*.csv)|*.csv|All files (*.*)|*.*"
            };

            if (sfd.ShowDialog() != true) return;

            var sb = new System.Text.StringBuilder();
            sb.AppendLine("Trade ID,Side,Symbol,Entry Time,Entry Price,Exit Time,Exit Price,Net P&L (INR),Bot Speed,Status");
            foreach (var t in Trades)
            {
                sb.AppendLine($"\"{t.TradeId}\",\"{t.Side}\",\"{t.Symbol}\",\"{t.EntryTime}\",{t.Entry:F2},\"{t.ExitTime}\",{t.Exit:F2},{t.NetPnl:F2},\"{t.BotMsText}\",\"{t.Status}\"");
            }

            System.IO.File.WriteAllText(sfd.FileName, sb.ToString(), System.Text.Encoding.UTF8);

            StatusMessage = $"Exported {Trades.Count} trades to CSV.";
            var res = MessageBox.Show($"Successfully exported {Trades.Count} trades to:\n{sfd.FileName}\n\nWould you like to open the file now?", "Export Successful", MessageBoxButton.YesNo, MessageBoxImage.Information);
            if (res == MessageBoxResult.Yes)
            {
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(sfd.FileName) { UseShellExecute = true });
            }
        }
        catch (Exception ex)
        {
            MessageBox.Show($"Failed to export CSV: {ex.Message}", "Export Error", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }
}

static class StatusExt
{
    public static string TodayNetPnlSigned(this StatusInfo s) =>
        (s.TodayPnl >= 0 ? "₹" : "-₹") + Math.Abs(s.TodayPnl).ToString("N2", CultureInfo.InvariantCulture);
}
