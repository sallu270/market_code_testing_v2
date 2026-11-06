using GChartWpf.Models;

namespace GChartWpf.Services;

public interface IDataSource
{
    Task<List<Candle>> GetCandlesAsync(string instrumentKey, string timeframe);
    Task<ChainSnapshot?> GetChainAsync(string instrumentKey);
    Task<StatusInfo?> GetStatusAsync();
    Task<List<TradeRow>> GetTradesAsync();
    Task<List<StockRow>> GetStocksAsync();
    Task SaveSelectionsAsync(Dictionary<string, int> selections);
    Task SaveIndexLotsAsync(int niftyLots, int bankNiftyLots, int sensexLots);
    Task SaveIndexConfigAsync(int niftyLots, int bankNiftyLots, int sensexLots, bool niftyEnabled, bool bankNiftyEnabled, bool sensexEnabled);
    Task ExitPositionAsync();
}
