using GChartWpf.Services;

namespace GChartWpf;

/// <summary>Where the Python (Flask) bot lives, and whether to run with fake demo data.</summary>
public static class AppSettings
{
    // Change here, or set env var GCHART_API=http://host:port
    public static string BaseUrl =>
        Environment.GetEnvironmentVariable("GCHART_API") ?? "http://127.0.0.1:5000";

    // 3 Supported Core Indices
    public const string NiftyKey = "NSE_INDEX|Nifty 50";
    public const string NiftyName = "NIFTY 50";

    public const string BankNiftyKey = "NSE_INDEX|Nifty Bank";
    public const string BankNiftyName = "BANK NIFTY";

    public const string SensexKey = "BSE_INDEX|SENSEX";
    public const string SensexName = "SENSEX";

    // Backwards compatibility alias
    public const string IndexKey = NiftyKey;
    public const string IndexName = NiftyName;

    // Run:  GChartWpf.exe --demo   (no backend needed, fake data for checking the design)
    public static bool Demo => Environment.GetCommandLineArgs().Contains("--demo")
                               || Environment.GetEnvironmentVariable("GCHART_DEMO") == "1";

    public static IDataSource CreateSource() => Demo ? new DemoSource() : new ApiClient(BaseUrl);
}
