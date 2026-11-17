using System.Windows.Media;
using GChartWpf.ViewModels;
using GChartWpf.Services;

namespace GChartWpf.Models;

public record Candle(long TimeMs, double Open, double High, double Low, double Close, double Volume);

public class OptionLeg
{
    public string Key = "";
    public double Ltp, Volume, Oi, PrevOi, Close, Bid, Ask;
    public string BuildUp = "—";
}

public class ChainStrike
{
    public double Strike;
    public OptionLeg Ce = new();
    public OptionLeg Pe = new();
}

public class ChainSnapshot
{
    public double Spot;
    public string Expiry = "";
    public List<ChainStrike> Strikes = new();
}

/// <summary>One flat row of the 8-strike option chain grid.</summary>
public class StrikeRow
{
    public double Strike { get; set; }
    public bool IsAtm { get; set; }

    public string CeBuildUp { get; set; } = "—";
    public double CeOi { get; set; }
    public double CeOiChange { get; set; }
    public string CeOiChangeText { get; set; } = "—";
    public double CeVolume { get; set; }
    public double CeBid { get; set; }
    public double CeAsk { get; set; }
    public double CeLtp { get; set; }

    public double PeLtp { get; set; }
    public double PeBid { get; set; }
    public double PeAsk { get; set; }
    public double PeVolume { get; set; }
    public double PeOiChange { get; set; }
    public string PeOiChangeText { get; set; } = "—";
    public double PeOi { get; set; }
    public string PeBuildUp { get; set; } = "—";
}

public class IndexStatusInfo
{
    public string Key = "";
    public string Name = "";
    public string State = "SETUP";
    public double SpotPrice;
    public bool HasPosition;
    public string PositionSymbol = "";
    public double EntryPrice;
    public string SummaryLine = "";
    public int TodayTradesCount;
    public double FuturesLtp;
    public double FuturesOi;
    public string FuturesBuildup = "Neutral";
}

public class StatusInfo
{
    public string State = "—";
    public double Spot;
    public string Expiry = "";
    public bool HasPosition;
    public string PositionSymbol = "";
    public double EntryPrice;
    public double TodayPnl;
    public double TodayPoints;
    public int TodayTrades;
    public double FuturesLtp;
    public double FuturesOi;
    public string FuturesBuildup = "Neutral";
    public int NiftyLots = 1;
    public int BankNiftyLots = 1;
    public int SensexLots = 1;
    public bool NiftyEnabled = true;
    public bool BankNiftyEnabled = true;
    public bool SensexEnabled = true;
    public double DeltaCe;
    public double DeltaPe;
    public string CeBuildup = "";
    public string PeBuildup = "";
    public int? RuleNumber;
    public string Stars = "";
    public string Decision = "WAIT";
    public string SummaryLine = "";
    public Dictionary<string, IndexStatusInfo> IndexUnderlyings = new();
}

public class TradeRow
{
    public string TradeId { get; set; } = "";
    public string TradeDate { get; set; } = "";
    public string Side { get; set; } = "";
    public string Symbol { get; set; } = "";
    public string EntryTime { get; set; } = "—";
    public double Entry { get; set; }
    public string ExitTime { get; set; } = "—";
    public double Exit { get; set; }
    public double NetPnl { get; set; }
    public string Status { get; set; } = "";
    public double BotMs { get; set; }
    public string BotMsText => BotMs > 0 ? $"{BotMs:0.00} ms" : "—";
}

public class StockRow : ObservableObject
{
    public string Symbol { get; set; } = "";
    public string Name { get; set; } = "";
    public string Key { get; set; } = "";
    public int LotSize { get; set; }

    double _price, _chg, _vol; string _bu = "Neutral"; bool _sel; int _lots = 1;
    public double Price { get => _price; set => Set(ref _price, value); }
    public double ChangePct { get => _chg; set => Set(ref _chg, value); }
    public double Volume { get => _vol; set => Set(ref _vol, value); }
    public string BuildUp { get => _bu; set { if (Set(ref _bu, value)) OnPropertyChanged(nameof(Signal)); } }
    public bool Selected { get => _sel; set => Set(ref _sel, value); }
    public int Lots { get => _lots; set => Set(ref _lots, value < 1 ? 1 : value); }

    /// <summary>BUY for Long Build-up, SELL for Short Build-up, otherwise —</summary>
    public string Signal => BuildUp == "Long Build-up" ? "BUY" : BuildUp == "Short Build-up" ? "SELL" : "—";

    public string WatchlistRank { get; set; } = "★★★";
    public string PlannedDirection { get; set; } = "BULL";
    public Brush DirectionBrush => PlannedDirection == "BULL" || PlannedDirection == "BULLISH" ? Pal.Green : Pal.Red;
}

public class TopStocksConfigResponse
{
    public int TopN { get; set; } = 5;
    public List<StockConfigRow> Stocks { get; set; } = new();
}

public class StockConfigRow : ObservableObject
{
    public string Symbol { get; set; } = "";
    public string Name { get; set; } = "";
    public string Key { get; set; } = "";
    public int LotSize { get; set; } = 100;
    public double StrikeStep { get; set; } = 10.0;
    public string WatchlistRank { get; set; } = "★★★";
    public string PlannedDirection { get; set; } = "BULL";

    int _lots = 1;
    public int Lots
    {
        get => _lots;
        set
        {
            if (Set(ref _lots, Math.Max(1, value)))
            {
                OnPropertyChanged(nameof(QuantityPreview));
            }
        }
    }

    bool _enabled = true;
    public bool Enabled
    {
        get => _enabled;
        set
        {
            if (Set(ref _enabled, value))
            {
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    public string StatusText => Enabled ? "ACTIVE" : "PAUSED TODAY";
    public string QuantityPreview => $"{Lots} lot{(Lots > 1 ? "s" : "")} = {Lots * LotSize} qty";
}

public class FlowRow : ObservableObject
{
    public string Instrument { get; set; } = "";
    public string Key { get; set; } = "";
    public double VolPcr5m { get; set; }
    public double VolPcrShift { get; set; }
    public string DoiPcr { get; set; } = "N/A";
    public double NetOiBias { get; set; }
    public string CeVsr { get; set; } = "1.0x";
    public string PeVsr { get; set; } = "1.0x";
    public string FlowState { get; set; } = "NEUTRAL";
    public string BotAction { get; set; } = "Monitoring flow";
    public double StraddleDecay { get; set; } = 1.0;

    public Brush VolPcrBrush => VolPcrShift <= 0.65 ? Pal.Green : VolPcrShift >= 1.55 ? Pal.Red : Pal.White;
    public Brush NetOiBrush => NetOiBias >= 0.15 ? Pal.Green : NetOiBias <= -0.15 ? Pal.Red : Pal.White;
    public Brush FlowStateBrush => FlowState.Contains("BULL") ? Pal.Green :
                                   FlowState.Contains("BEAR") ? Pal.Red :
                                   FlowState == "COIL" || FlowState == "DIVERGENCE_TRAP" ? Pal.Amber : Pal.Grey;
}

public class FlowAlert
{
    public string Time { get; set; } = "";
    public string Instrument { get; set; } = "";
    public string Event { get; set; } = "";
    public string Detail { get; set; } = "";
}
