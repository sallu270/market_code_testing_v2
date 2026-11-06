using System.Globalization;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using GChartWpf.Models;

namespace GChartWpf.Controls;

/// <summary>
/// Interactive TradingView-style candlestick chart:
/// - Mouse wheel: Zoom in / Zoom out (adjusts candle width & count)
/// - Click & drag: Pan / scroll left & right through history
/// - Crosshair: Real-time price & time tracker following cursor
/// - Double-click: Resets zoom and snaps back to live price
/// </summary>
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

    // Interactive Zoom, Pan & Crosshair State
    int _visibleCount = 60;
    int _panOffset = 0; // 0 = at live edge
    bool _isDragging = false;
    Point _dragStartPoint;
    int _dragStartPan;
    Point? _hoverPos;
    double _lastSlotWidth = 10;

    static readonly Brush Up = Frozen(0, 230, 0), Down = Frozen(230, 0, 0), Grey = Frozen(140, 140, 140);
    static readonly Brush DarkGrey = Frozen(25, 25, 25), AxisTagBg = Frozen(40, 40, 40);
    static readonly Pen GridPen = MakePen(Color.FromRgb(38, 38, 38), 1, true);
    static readonly Pen CrosshairPen = MakePen(Color.FromRgb(90, 90, 90), 1, true);
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

    public CandleChart()
    {
        ClipToBounds = true;
        Cursor = Cursors.Cross;
    }

    FormattedText Txt(string s, Brush b, double size = 11) =>
        new(s, CultureInfo.InvariantCulture, FlowDirection.LeftToRight, Mono, size, b, VisualTreeHelper.GetDpi(this).PixelsPerDip);

    // ---------------- Interactive Mouse Events ----------------

    protected override void OnMouseWheel(MouseWheelEventArgs e)
    {
        base.OnMouseWheel(e);
        // Scroll Up (Delta > 0) -> Zoom in (candles become thicker & bigger, fewer candles)
        // Scroll Down (Delta < 0) -> Zoom out (candles become thinner & smaller, more candles)
        int step = Math.Max(3, _visibleCount / 10);
        if (e.Delta > 0)
        {
            _visibleCount = Math.Max(15, _visibleCount - step);
        }
        else if (e.Delta < 0)
        {
            _visibleCount = Math.Min(300, _visibleCount + step);
        }
        InvalidateVisual();
    }

    protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
    {
        base.OnMouseLeftButtonDown(e);
        if (e.ClickCount == 2)
        {
            // Double-click -> Reset zoom and snap back to live candles
            _visibleCount = 60;
            _panOffset = 0;
            InvalidateVisual();
            return;
        }

        _isDragging = true;
        _dragStartPoint = e.GetPosition(this);
        _dragStartPan = _panOffset;
        CaptureMouse();
        Cursor = Cursors.SizeWE;
    }

    protected override void OnMouseLeftButtonUp(MouseButtonEventArgs e)
    {
        base.OnMouseLeftButtonUp(e);
        if (_isDragging)
        {
            _isDragging = false;
            ReleaseMouseCapture();
            Cursor = Cursors.Cross;
        }
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        var pt = e.GetPosition(this);
        _hoverPos = pt;

        if (_isDragging)
        {
            double dx = pt.X - _dragStartPoint.X;
            double slot = Math.Max(2, _lastSlotWidth);
            int candleShift = (int)Math.Round(dx / slot);

            // Dragging left (dx < 0) moves forward into newer candles (reduces pan offset)
            // Dragging right (dx > 0) moves backward into historical candles (increases pan offset)
            int total = Candles?.Count ?? 0;
            int maxPan = Math.Max(0, total - _visibleCount);
            _panOffset = Math.Max(0, Math.Min(maxPan, _dragStartPan + candleShift));
        }

        InvalidateVisual();
    }

    protected override void OnMouseLeave(MouseEventArgs e)
    {
        base.OnMouseLeave(e);
        if (!_isDragging)
        {
            _hoverPos = null;
            InvalidateVisual();
        }
    }

    // ---------------- Rendering ----------------

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

        int total = all.Count;
        int count = Math.Min(total, _visibleCount);
        int maxPan = Math.Max(0, total - count);
        _panOffset = Math.Max(0, Math.Min(maxPan, _panOffset));

        int endIdx = total - _panOffset;
        endIdx = Math.Max(count, Math.Min(total, endIdx));
        int startIdx = Math.Max(0, endIdx - count);

        var vis = all.Skip(startIdx).Take(count).ToList();
        if (vis.Count == 0) return;

        const double axisW = 66, timeH = 20, gap = 6;
        double volH = Math.Max(30, h * 0.15);
        double plotW = Math.Max(10, w - axisW), priceH = Math.Max(10, h - timeH - volH - gap);

        double hi = vis.Max(c => c.High), lo = vis.Min(c => c.Low);
        double pad = Math.Max((hi - lo) * 0.06, 0.01); hi += pad; lo -= pad;
        double maxVol = Math.Max(1, vis.Max(c => c.Volume));
        double Y(double p) => priceH * (1 - (p - lo) / (hi - lo));

        // Background grid + price labels
        for (int i = 0; i <= 5; i++)
        {
            double price = lo + (hi - lo) * i / 5, y = Y(price);
            dc.DrawLine(GridPen, new Point(0, y), new Point(plotW, y));
            dc.DrawText(Txt(price.ToString("N2", CultureInfo.InvariantCulture), Grey), new Point(plotW + 6, y - 7));
        }

        double slot = plotW / vis.Count;
        _lastSlotWidth = slot;
        double body = Math.Max(1, slot * 0.68);

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

        // Last price dotted line + price badge on right axis
        var last = vis[^1]; double ly = Y(last.Close);
        dc.DrawLine(LastPen, new Point(0, ly), new Point(plotW, ly));
        var tag = Txt(last.Close.ToString("N2", CultureInfo.InvariantCulture), Brushes.Black, 11);
        dc.DrawRectangle(last.Close >= last.Open ? Up : Down, null, new Rect(plotW + 2, ly - 9, axisW - 4, 18));
        dc.DrawText(tag, new Point(plotW + 6, ly - 7));

        // Scrolled History Indicator (if panned back in time)
        if (_panOffset > 0)
        {
            var histPill = Txt($"◀ PAUSED ({_panOffset} candles back) · Double-click to LIVE", Frozen(255, 179, 0), 10);
            dc.DrawRectangle(DarkGrey, null, new Rect(10, 8, histPill.Width + 12, 18));
            dc.DrawText(histPill, new Point(16, 10));
        }

        // TradingView Interactive Crosshair Tracker
        if (_hoverPos.HasValue)
        {
            var hp = _hoverPos.Value;
            if (hp.X >= 0 && hp.X <= plotW && hp.Y >= 0 && hp.Y <= priceH + gap + volH)
            {
                // Vertical crosshair line
                dc.DrawLine(CrosshairPen, new Point(hp.X, 0), new Point(hp.X, h - timeH));

                // Horizontal crosshair line
                if (hp.Y <= priceH)
                {
                    dc.DrawLine(CrosshairPen, new Point(0, hp.Y), new Point(plotW, hp.Y));

                    // Hovered price badge on right Y axis
                    double hoveredPrice = lo + (hi - lo) * (1 - hp.Y / priceH);
                    var priceText = Txt(hoveredPrice.ToString("N2", CultureInfo.InvariantCulture), Brushes.White, 10.5);
                    dc.DrawRectangle(AxisTagBg, null, new Rect(plotW + 2, hp.Y - 8, axisW - 4, 16));
                    dc.DrawText(priceText, new Point(plotW + 6, hp.Y - 7));
                }

                // Hovered time badge on bottom X axis
                int hoverIdx = (int)Math.Floor(hp.X / slot);
                if (hoverIdx >= 0 && hoverIdx < vis.Count)
                {
                    var hc = vis[hoverIdx];
                    var hts = DateTimeOffset.FromUnixTimeMilliseconds(hc.TimeMs).ToOffset(TimeSpan.FromHours(5.5));
                    string timeStr = hts.ToString(IsDaily ? "dd MMM yyyy" : "HH:mm:ss", CultureInfo.InvariantCulture);
                    var timeText = Txt(timeStr, Brushes.White, 10);
                    double tx = Math.Max(2, Math.Min(plotW - timeText.Width - 8, hp.X - timeText.Width / 2));
                    dc.DrawRectangle(AxisTagBg, null, new Rect(tx - 4, h - timeH + 2, timeText.Width + 8, 16));
                    dc.DrawText(timeText, new Point(tx, h - timeH + 3));
                }
            }
        }
    }
}
