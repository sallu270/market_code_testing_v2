using System.Globalization;
using System.Windows;
using System.Windows.Media;
using GChartWpf.Models;

namespace GChartWpf.Controls;

/// <summary>GChart-style candlestick chart: black canvas, neon green/red candles, volume strip, price axis, last-price line.</summary>
public class CandleChart : FrameworkElement
{
    public static readonly DependencyProperty CandlesProperty = DependencyProperty.Register(
        nameof(Candles), typeof(IReadOnlyList<Candle>), typeof(CandleChart),
        new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty IsDailyProperty = DependencyProperty.Register(
        nameof(IsDaily), typeof(bool), typeof(CandleChart),
        new FrameworkPropertyMetadata(false, FrameworkPropertyMetadataOptions.AffectsRender));

    public IReadOnlyList<Candle>? Candles { get => (IReadOnlyList<Candle>?)GetValue(CandlesProperty); set => SetValue(CandlesProperty, value); }
    public bool IsDaily { get => (bool)GetValue(IsDailyProperty); set => SetValue(IsDailyProperty, value); }

    const int MaxCandles = 60;
    static readonly Brush Up = Frozen(0, 230, 0), Down = Frozen(230, 0, 0), Grey = Frozen(140, 140, 140);
    static readonly Pen GridPen = MakePen(Color.FromRgb(45, 45, 45), 1, true);
    static readonly Pen UpPen = MakePen(Color.FromRgb(0, 230, 0), 1, false), DownPen = MakePen(Color.FromRgb(230, 0, 0), 1, false);
    static readonly Pen LastPen = MakePen(Color.FromRgb(200, 200, 200), 1, true);
    static readonly Typeface Mono = new("Consolas");

    static Brush Frozen(byte r, byte g, byte b) { var x = new SolidColorBrush(Color.FromRgb(r, g, b)); x.Freeze(); return x; }
    static Pen MakePen(Color c, double w, bool dashed)
    {
        var p = new Pen(new SolidColorBrush(c), w);
        if (dashed) p.DashStyle = new DashStyle(new double[] { 3, 3 }, 0);
        p.Freeze(); return p;
    }

    FormattedText Txt(string s, Brush b, double size = 11) =>
        new(s, CultureInfo.InvariantCulture, FlowDirection.LeftToRight, Mono, size, b, VisualTreeHelper.GetDpi(this).PixelsPerDip);

    protected override void OnRender(DrawingContext dc)
    {
        double w = ActualWidth, h = ActualHeight;
        dc.DrawRectangle(Brushes.Black, null, new Rect(0, 0, w, h));
        var all = Candles;
        if (all == null || all.Count == 0 || w < 100 || h < 100)
        {
            var t = Txt("Waiting for market data…", Grey, 14);
            dc.DrawText(t, new Point((w - t.Width) / 2, (h - t.Height) / 2));
            return;
        }

        var vis = all.Skip(Math.Max(0, all.Count - MaxCandles)).ToList();
        const double axisW = 66, timeH = 20, gap = 6;
        double volH = Math.Max(30, h * 0.15);
        double plotW = w - axisW, priceH = h - timeH - volH - gap;

        double hi = vis.Max(c => c.High), lo = vis.Min(c => c.Low);
        double pad = Math.Max((hi - lo) * 0.06, 0.01); hi += pad; lo -= pad;
        double maxVol = Math.Max(1, vis.Max(c => c.Volume));
        double Y(double p) => priceH * (1 - (p - lo) / (hi - lo));

        // grid + price labels
        for (int i = 0; i <= 5; i++)
        {
            double price = lo + (hi - lo) * i / 5, y = Y(price);
            dc.DrawLine(GridPen, new Point(0, y), new Point(plotW, y));
            dc.DrawText(Txt(price.ToString("N2", CultureInfo.InvariantCulture), Grey), new Point(plotW + 6, y - 7));
        }

        double slot = plotW / vis.Count, body = Math.Max(1, slot * 0.68);
        for (int i = 0; i < vis.Count; i++)
        {
            var c = vis[i]; bool up = c.Close >= c.Open;
            double cx = slot * i + slot / 2;
            dc.DrawLine(up ? UpPen : DownPen, new Point(cx, Y(c.High)), new Point(cx, Y(c.Low)));
            double top = Y(Math.Max(c.Open, c.Close)), bh = Math.Max(1, Math.Abs(Y(c.Open) - Y(c.Close)));
            dc.DrawRectangle(up ? Up : Down, null, new Rect(cx - body / 2, top, body, bh));

            double vh = volH * c.Volume / maxVol;
            dc.DrawRectangle(up ? Up : Down, null, new Rect(cx - body / 2, priceH + gap + volH - vh, body, vh));

            if (i % Math.Max(1, vis.Count / 7) == 0)
            {
                var ts = DateTimeOffset.FromUnixTimeMilliseconds(c.TimeMs).ToOffset(TimeSpan.FromHours(5.5));
                dc.DrawText(Txt(ts.ToString(IsDaily ? "dd MMM" : "HH:mm", CultureInfo.InvariantCulture), Grey, 10),
                            new Point(cx - 14, h - timeH + 4));
            }
        }

        // last price line + tag
        var last = vis[^1]; double ly = Y(last.Close);
        dc.DrawLine(LastPen, new Point(0, ly), new Point(plotW, ly));
        var tag = Txt(last.Close.ToString("N2", CultureInfo.InvariantCulture), Brushes.Black, 11);
        dc.DrawRectangle(last.Close >= last.Open ? Up : Down, null, new Rect(plotW + 2, ly - 9, axisW - 4, 18));
        dc.DrawText(tag, new Point(plotW + 6, ly - 7));
    }
}
