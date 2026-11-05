using System.Globalization;
using System.Windows.Data;
using System.Windows.Media;

namespace GChartWpf.Services;

public static class Pal
{
    static SolidColorBrush B(byte r, byte g, byte b) { var x = new SolidColorBrush(Color.FromRgb(r, g, b)); x.Freeze(); return x; }
    public static readonly Brush Green = B(0, 230, 0);
    public static readonly Brush Red = B(255, 42, 42);
    public static readonly Brush Amber = B(255, 179, 0);
    public static readonly Brush Grey = B(138, 138, 138);
    public static readonly Brush White = B(230, 230, 230);
}

public class BuildUpBrushConverter : IValueConverter
{
    public object Convert(object? v, Type t, object? p, CultureInfo c) => (v as string) switch
    {
        "Long Build-up" => Pal.Green,
        "Short Build-up" => Pal.Red,
        "Short Covering" or "Long Unwinding" => Pal.Amber,
        _ => Pal.Grey
    };
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

public class SignBrushConverter : IValueConverter
{
    public object Convert(object? v, Type t, object? p, CultureInfo c)
    {
        double d = v is double x ? x : 0;
        return d > 0 ? Pal.Green : d < 0 ? Pal.Red : Pal.White;
    }
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

public class SideBrushConverter : IValueConverter
{
    public object Convert(object? v, Type t, object? p, CultureInfo c) => (v as string) switch
    {
        "BUY" => Pal.Green,
        "SELL" => Pal.Red,
        _ => Pal.Grey
    };
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}
