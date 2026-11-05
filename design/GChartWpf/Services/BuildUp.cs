using GChartWpf.Models;

namespace GChartWpf.Services;

public static class BuildUp
{
    /// price up + OI up = Long Build-up, price down + OI up = Short Build-up,
    /// price up + OI down = Short Covering, price down + OI down = Long Unwinding
    public static string Classify(double ltp, double prevClose, double oi, double prevOi)
    {
        if (ltp <= 0 || prevClose <= 0 || prevOi <= 0) return "—";
        double dp = ltp - prevClose, doi = oi - prevOi;
        if (dp > 0 && doi > 0) return "Long Build-up";
        if (dp < 0 && doi > 0) return "Short Build-up";
        if (dp > 0 && doi < 0) return "Short Covering";
        if (dp < 0 && doi < 0) return "Long Unwinding";
        return "Neutral";
    }
}

/// <summary>Picks the 8 strikes nearest to spot (4 at/below, 4 above) so the window slides with price.</summary>
public static class ChainWindow
{
    public static List<StrikeRow> Pick(ChainSnapshot snap, int total)
    {
        int half = Math.Max(1, total / 2);
        var all = snap.Strikes.OrderBy(s => s.Strike).ToList();
        if (all.Count == 0) return new();

        var below = all.Where(s => s.Strike <= snap.Spot).TakeLast(half);
        var above = all.Where(s => s.Strike > snap.Spot).Take(half);
        var chosen = below.Concat(above).OrderBy(s => s.Strike).ToList();

        double atm = all.OrderBy(s => Math.Abs(s.Strike - snap.Spot)).First().Strike;
        return chosen.Select(s => new StrikeRow
        {
            Strike = s.Strike,
            IsAtm = s.Strike == atm,
            CeBuildUp = s.Ce.BuildUp, CeVolume = s.Ce.Volume, CeBid = s.Ce.Bid, CeAsk = s.Ce.Ask, CeLtp = s.Ce.Ltp,
            PeLtp = s.Pe.Ltp, PeBid = s.Pe.Bid, PeAsk = s.Pe.Ask, PeVolume = s.Pe.Volume, PeBuildUp = s.Pe.BuildUp
        }).ToList();
    }
}
