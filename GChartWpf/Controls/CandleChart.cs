using System.Globalization;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using GChartWpf.Models;

namespace GChartWpf.Controls;

/// <summary>
/// Full TradingView-grade interactive Candlestick Chart:
/// - Live Candle Centered Horizontally: Defaults to ~50% screen width with empty future margin on the right
/// - Live Candle Centered Vertically: Defaults to 50% screen height with equal space top and bottom
/// - Free 2D Chart Dragging: Drag left/right past the live edge (no hard wall at 0), drag up/down across prices
/// - Mouse wheel: Zoom in/out horizontally, or over price scale for vertical stretch
/// - Right price scale: Click & drag vertically to stretch/compress candle height (TradingView direction)
/// - Clean "Nice Steps" Round Levels on Price Axis (e.g. 24,600, 24,650, 24,700)
/// - High-contrast live price pill with dashed horizontal price line
/// - Crosshair tracking on both axes (including projected future time in blank right margin)
/// - Double-click: Instantly resets horizontal and vertical position back to centered live candle
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

    // Interactive Zoom, Pan & Scaling State
    int _visibleCount = 60;
    // Negative pan offset = Right Margin / Future space (e.g. -25 places live candle in the center)
    // Positive pan offset = Scrolled back into historical candles
    int _panOffset = -25;
    double _verticalPriceOffset = 0; // vertical price pan offset (0 = live candle centered)
    double _priceScaleFactor = 1.0;  // 1.0 = auto-fit, >1.0 = stretched taller, <1.0 = compressed
    bool _isChartDragging = false;
    bool _isPriceDragging = false;
    Point _dragStartPoint;
    int _dragStartPan;
    double _dragStartVerticalOffset = 0;
    double _dragStartScaleFactor = 1.0;
    Point? _hoverPos;
    double _lastSlotWidth = 10;
    double _lastPlotWidth = 0;
    double _lastPriceHeight = 0;
    double _lastHi = 0;
    double _lastLo = 0;

    // TradingView Color Palette
    static readonly Brush TvUp = Frozen(8, 153, 129);      // TradingView Green #089981
    static readonly Brush TvDown = Frozen(242, 54, 69);    // TradingView Red #F23645
    static readonly Brush Grey = Frozen(140, 140, 140);
    static readonly Brush DarkGrey = Frozen(25, 25, 25);
    static readonly Brush AxisTagBg = Frozen(45, 45, 45);
    static readonly Brush White = Brushes.White;

    static readonly Pen GridPen = MakePen(Color.FromRgb(35, 35, 35), 1, true);
    static readonly Pen AxisBorderPen = MakePen(Color.FromRgb(48, 48, 48), 1, false);
    static readonly Pen CrosshairPen = MakePen(Color.FromRgb(100, 100, 100), 1, true);
    static readonly Pen TvUpPen = MakePen(Color.FromRgb(8, 153, 129), 1, false);
    static readonly Pen TvDownPen = MakePen(Color.FromRgb(242, 54, 69), 1, false);
    static readonly Pen LastPen = MakePen(Color.FromRgb(220, 220, 220), 1, true);
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

    FormattedText Txt(string s, Brush b, double size = 11, bool bold = false) =>
        new(s, CultureInfo.InvariantCulture, FlowDirection.LeftToRight,
            bold ? new Typeface(Mono.FontFamily, FontStyles.Normal, FontWeights.Bold, FontStretches.Normal) : Mono,
            size, b, VisualTreeHelper.GetDpi(this).PixelsPerDip);

    // ---------------- Interactive Mouse Events ----------------

    protected override void OnMouseWheel(MouseWheelEventArgs e)
    {
        base.OnMouseWheel(e);
        var pt = e.GetPosition(this);
        if (pt.X >= _lastPlotWidth && pt.Y <= _lastPriceHeight)
        {
            // Scrolling wheel over right price scale -> Vertical stretch/compress
            double factor = e.Delta > 0 ? 1.12 : 0.89;
            _priceScaleFactor = Math.Max(0.15, Math.Min(8.0, _priceScaleFactor * factor));
            InvalidateVisual();
            return;
        }

        int step = Math.Max(3, _visibleCount / 10);
        int oldVisible = _visibleCount;
        if (e.Delta > 0)
            _visibleCount = Math.Max(15, _visibleCount - step);
        else if (e.Delta < 0)
            _visibleCount = Math.Min(300, _visibleCount + step);

        // Keep horizontal centering proportional while zooming
        if (oldVisible != _visibleCount)
        {
            _panOffset = (int)Math.Round((double)_panOffset * _visibleCount / oldVisible);
        }

        InvalidateVisual();
    }

    protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
    {
        base.OnMouseLeftButtonDown(e);
        var pt = e.GetPosition(this);

        if (e.ClickCount == 2)
        {
            // Double-click -> Snap live candle back to DEAD CENTER (both horizontally and vertically)
            _visibleCount = 60;
            _panOffset = -(_visibleCount / 2); // Center horizontally (~30 bars right margin)
            _verticalPriceOffset = 0;          // Center vertically (50% screen height)
            _priceScaleFactor = 1.0;          // Reset vertical scale to auto-fit
            InvalidateVisual();
            return;
        }

        if (pt.X >= _lastPlotWidth && pt.Y <= _lastPriceHeight)
        {
            // Clicked on right price scale -> Start vertical stretch dragging
            _isPriceDragging = true;
            _isChartDragging = false;
            _dragStartPoint = pt;
            _dragStartScaleFactor = _priceScaleFactor;
            CaptureMouse();
            Cursor = Cursors.SizeNS;
        }
        else
        {
            // Clicked on chart area -> Free 2D Pan (time left/right + price up/down)
            _isChartDragging = true;
            _isPriceDragging = false;
            _dragStartPoint = pt;
            _dragStartPan = _panOffset;
            _dragStartVerticalOffset = _verticalPriceOffset;
            CaptureMouse();
            Cursor = Cursors.SizeAll;
        }
    }

    protected override void OnMouseLeftButtonUp(MouseButtonEventArgs e)
    {
        base.OnMouseLeftButtonUp(e);
        if (_isChartDragging || _isPriceDragging)
        {
            _isChartDragging = false;
            _isPriceDragging = false;
            ReleaseMouseCapture();
            UpdateCursor(e.GetPosition(this));
        }
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        var pt = e.GetPosition(this);
        _hoverPos = pt;

        if (_isPriceDragging)
        {
            // Dragging on price scale vertically stretches / compresses the candles (TradingView direction)
            double dy = pt.Y - _dragStartPoint.Y;
            _priceScaleFactor = Math.Max(0.15, Math.Min(8.0, _dragStartScaleFactor * Math.Pow(1.006, -dy)));
        }
        else if (_isChartDragging)
        {
            // 1. Horizontal free pan (allows pulling live candle into center or left, no hard wall at 0)
            double dx = pt.X - _dragStartPoint.X;
            double slot = Math.Max(2, _lastSlotWidth);
            int candleShift = (int)Math.Round(dx / slot);
            int total = Candles?.Count ?? 0;

            // Allow right margin down to -80% of screen width (empty space on right)
            int minPan = -(int)(_visibleCount * 0.85);
            // Allow historical scrolling back to older candles
            int maxPan = Math.Max(0, total - 5);
            _panOffset = Math.Max(minPan, Math.Min(maxPan, _dragStartPan + candleShift));

            // 2. Vertical free pan (price up/down)
            double dy = pt.Y - _dragStartPoint.Y;
            double priceSpan = Math.Max(1.0, _lastHi - _lastLo);
            double pricePerPixel = priceSpan / Math.Max(10, _lastPriceHeight);
            _verticalPriceOffset = _dragStartVerticalOffset + dy * pricePerPixel;
        }
        else
        {
            UpdateCursor(pt);
        }

        InvalidateVisual();
    }

    void UpdateCursor(Point pt)
    {
        if (pt.X >= _lastPlotWidth && pt.Y <= _lastPriceHeight)
            Cursor = Cursors.SizeNS; // Price scale vertical resize cursor
        else
            Cursor = Cursors.Cross;  // Chart crosshair cursor
    }

    protected override void OnMouseLeave(MouseEventArgs e)
    {
        base.OnMouseLeave(e);
        if (!_isChartDragging && !_isPriceDragging)
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

        // Clean layout with generous price axis width
        const double axisW = 82, timeH = 22, gap = 6;
        double volH = Math.Max(30, h * 0.14);
        double plotW = Math.Max(10, w - axisW), priceH = Math.Max(10, h - timeH - volH - gap);
        _lastPlotWidth = plotW;
        _lastPriceHeight = priceH;

        double slot = plotW / count;
        _lastSlotWidth = slot;

        // TradingView Horizontal Slot Placement:
        // For candle index k (0 <= k < total):
        // slotIndex = count - total + k + _panOffset
        // When _panOffset < 0, empty future slots appear on the right side!
        int startK = Math.Max(0, total - count - _panOffset);
        int endK = Math.Min(total - 1, total - 1 - _panOffset);

        var visCandles = new List<Candle>();
        if (startK <= endK && startK < total && endK >= 0)
        {
            for (int k = Math.Max(0, startK); k <= Math.Min(total - 1, endK); k++)
                visCandles.Add(all[k]);
        }
        if (visCandles.Count == 0 && total > 0)
        {
            visCandles.Add(all[^1]);
        }

        // Filter valid candles for price bounds
        var validCandles = visCandles.Where(c => c.Low > 0 && c.High > 0).ToList();
        if (validCandles.Count == 0) validCandles.Add(all[^1]);

        double baseHi = validCandles.Max(c => c.High);
        double baseLo = validCandles.Min(c => c.Low);
        double livePrice = all[^1].Close;

        // Live candle centered vertically at 50% screen height by default
        double maxDistFromLive = Math.Max(1.0, Math.Max(Math.Abs(baseHi - livePrice), Math.Abs(livePrice - baseLo)));
        double halfSpan = Math.Max(0.5, maxDistFromLive * 1.15) / _priceScaleFactor;

        double midPrice = livePrice + _verticalPriceOffset;
        double hi = midPrice + halfSpan;
        double lo = midPrice - halfSpan;
        if (hi <= lo) hi = lo + 1.0;

        _lastHi = hi;
        _lastLo = lo;

        double maxVol = Math.Max(1, validCandles.Max(c => c.Volume));
        double Y(double p) => priceH * (1.0 - (p - lo) / (hi - lo));

        // Draw clean axis separator borders
        dc.DrawLine(AxisBorderPen, new Point(plotW, 0), new Point(plotW, h - timeH));
        dc.DrawLine(AxisBorderPen, new Point(0, h - timeH), new Point(w, h - timeH));

        // Background Grid + Clean "Nice Steps" Round Price Labels (TradingView Style)
        double priceRange = hi - lo;
        double targetStep = priceRange / 6.0;
        double mag = Math.Pow(10, Math.Floor(Math.Log10(Math.Max(0.001, targetStep))));
        double frac = targetStep / mag;
        double step;
        if (frac < 1.5) step = 1.0 * mag;
        else if (frac < 3.0) step = 2.0 * mag;
        else if (frac < 7.0) step = 5.0 * mag;
        else step = 10.0 * mag;

        double firstPrice = Math.Ceiling(lo / step) * step;
        for (double p = firstPrice; p <= hi; p += step)
        {
            double gy = Y(p);
            if (gy >= 0 && gy <= priceH)
            {
                dc.DrawLine(GridPen, new Point(0, gy), new Point(plotW, gy));
                string pStr = step >= 1 ? p.ToString("N0", CultureInfo.InvariantCulture) : p.ToString("N2", CultureInfo.InvariantCulture);
                dc.DrawText(Txt(pStr, Grey, 10.5), new Point(plotW + 6, gy - 6.5));
            }
        }

        // Draw Visible Candlesticks & Volume Bars
        double body = Math.Max(1, slot * 0.70);
        double latestCandleCx = plotW - slot / 2;

        for (int k = Math.Max(0, startK); k <= Math.Min(total - 1, endK); k++)
        {
            var c = all[k];
            int slotIdx = count - total + k + _panOffset;
            if (slotIdx < 0 || slotIdx >= count) continue;

            double cx = slotIdx * slot + slot / 2;
            if (k == total - 1) latestCandleCx = cx;

            bool up = c.Close >= c.Open;

            // Wick
            dc.DrawLine(up ? TvUpPen : TvDownPen, new Point(cx, Y(c.High)), new Point(cx, Y(c.Low)));

            // Body
            double top = Y(Math.Max(c.Open, c.Close));
            double bh = Math.Max(1, Math.Abs(Y(c.Open) - Y(c.Close)));
            dc.DrawRectangle(up ? TvUp : TvDown, null, new Rect(cx - body / 2, top, body, bh));

            // Volume bar
            double vh = volH * c.Volume / maxVol;
            dc.DrawRectangle(up ? TvUp : TvDown, null, new Rect(cx - body / 2, priceH + gap + volH - vh, body, vh));

            // Time axis labels
            if (slotIdx % Math.Max(1, count / 7) == 0)
            {
                var ts = DateTimeOffset.FromUnixTimeMilliseconds(c.TimeMs).ToOffset(TimeSpan.FromHours(5.5));
                dc.DrawText(Txt(ts.ToString(IsDaily ? "dd MMM" : "HH:mm", CultureInfo.InvariantCulture), Grey, 10),
                            new Point(cx - 14, h - timeH + 5));
            }
        }

        // TradingView High-Contrast Real-Time Price Line & Pill Badge
        double ly = Y(livePrice);
        if (ly >= 0 && ly <= priceH)
        {
            // Dotted horizontal line extending from live candle all the way to the price axis
            dc.DrawLine(LastPen, new Point(0, ly), new Point(plotW, ly));

            // High-contrast pill badge on price axis
            var lastCandle = all[^1];
            var badgeBrush = lastCandle.Close >= lastCandle.Open ? TvUp : TvDown;
            var pillRect = new Rect(plotW + 3, ly - 9.5, axisW - 6, 19);
            dc.DrawRoundedRectangle(badgeBrush, null, pillRect, 2, 2);

            // Bold white text for supreme legibility
            var tag = Txt(livePrice.ToString("N2", CultureInfo.InvariantCulture), White, 11, bold: true);
            dc.DrawText(tag, new Point(plotW + 6, ly - 7.5));
        }

        // Status Indicator Pill (Top Left)
        int defaultCenterPan = -(count / 2);
        bool isHistorical = _panOffset > 0;
        bool isCustomPan = Math.Abs(_panOffset - defaultCenterPan) > 4 || Math.Abs(_verticalPriceOffset) > 1.0 || Math.Abs(_priceScaleFactor - 1.0) > 0.1;

        if (isHistorical)
        {
            var histPill = Txt($"◀ PAUSED ({_panOffset} candles back) · Double-click to LIVE CENTER", Frozen(255, 179, 0), 10);
            dc.DrawRectangle(DarkGrey, null, new Rect(10, 8, histPill.Width + 12, 18));
            dc.DrawText(histPill, new Point(16, 10));
        }
        else if (isCustomPan)
        {
            var customPill = Txt("↔ Custom View · Double-click to LIVE CENTER", Frozen(100, 180, 255), 9.5);
            dc.DrawRectangle(DarkGrey, null, new Rect(10, 8, customPill.Width + 10, 16));
            dc.DrawText(customPill, new Point(15, 9));
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

                    // Hovered price badge on right Y axis (dark pill with white text)
                    double hoveredPrice = lo + (hi - lo) * (1.0 - hp.Y / priceH);
                    var priceText = Txt(hoveredPrice.ToString("N2", CultureInfo.InvariantCulture), White, 10.5);
                    dc.DrawRoundedRectangle(AxisTagBg, null, new Rect(plotW + 3, hp.Y - 8.5, axisW - 6, 17), 2, 2);
                    dc.DrawText(priceText, new Point(plotW + 6, hp.Y - 7));
                }

                // Hovered time badge on bottom X axis
                int hoverSlot = (int)Math.Floor(hp.X / slot);
                int hoverK = hoverSlot - (count - total + _panOffset);

                if (hoverK >= 0 && hoverK < total)
                {
                    var hc = all[hoverK];
                    var hts = DateTimeOffset.FromUnixTimeMilliseconds(hc.TimeMs).ToOffset(TimeSpan.FromHours(5.5));
                    string timeStr = hts.ToString(IsDaily ? "dd MMM yyyy" : "HH:mm:ss", CultureInfo.InvariantCulture);
                    var timeText = Txt(timeStr, White, 10);
                    double tx = Math.Max(2, Math.Min(plotW - timeText.Width - 8, hp.X - timeText.Width / 2));
                    dc.DrawRoundedRectangle(AxisTagBg, null, new Rect(tx - 4, h - timeH + 2, timeText.Width + 8, 17), 2, 2);
                    dc.DrawText(timeText, new Point(tx, h - timeH + 3.5));
                }
                else if (hoverK >= total && total > 0)
                {
                    // Projecting future time in the blank right margin area!
                    long stepMs = total > 1 ? (all[^1].TimeMs - all[^2].TimeMs) : 60000;
                    if (stepMs <= 0) stepMs = 60000;
                    long futureMs = all[^1].TimeMs + (hoverK - (total - 1)) * stepMs;
                    var fts = DateTimeOffset.FromUnixTimeMilliseconds(futureMs).ToOffset(TimeSpan.FromHours(5.5));
                    string timeStr = fts.ToString(IsDaily ? "dd MMM yyyy" : "HH:mm:ss", CultureInfo.InvariantCulture);
                    var timeText = Txt(timeStr, Frozen(180, 180, 180), 10);
                    double tx = Math.Max(2, Math.Min(plotW - timeText.Width - 8, hp.X - timeText.Width / 2));
                    dc.DrawRoundedRectangle(AxisTagBg, null, new Rect(tx - 4, h - timeH + 2, timeText.Width + 8, 17), 2, 2);
                    dc.DrawText(timeText, new Point(tx, h - timeH + 3.5));
                }
            }
        }
    }
}
