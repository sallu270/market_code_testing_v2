using GChartWpf.Models;

namespace GChartWpf.Services;

public interface IDataSource
{
    Task<List<Candle>> GetCandlesAsync(string instrumentKey, string timeframe);
    Task<ChainSnapshot?> GetChainAsync(string instrumentKey);
    Task<StatusInfo?> GetStatusAsync();
    Task<List<TradeRow>> GetTradesAsync(string? date = "today");
    Task<List<string>> GetTradeDatesAsync();
    Task<List<StockRow>> GetStocksAsync();
    Task SaveSelectionsAsync(Dictionary<string, int> selections);
    Task<TopStocksConfigResponse?> GetTopStocksConfigAsync(int? topN = null);
    Task SaveTopStocksConfigAsync(int topN, List<StockConfigRow> stocks);
    Task SaveIndexLotsAsync(int niftyLots, int bankNiftyLots, int sensexLots);
    Task SaveIndexConfigAsync(int niftyLots, int bankNiftyLots, int sensexLots, bool niftyEnabled, bool bankNiftyEnabled, bool sensexEnabled);
    Task ExitPositionAsync(string? indexKey = null);
    Task ExitIndicesAsync();
    Task ExitStocksAsync();
    Task ArmNowAsync(string? indexKey = null);
    Task<(List<FlowRow> Rows, List<FlowAlert> Alerts)> GetFlowDashboardAsync();
}
