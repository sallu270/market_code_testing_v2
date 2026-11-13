using System.Globalization;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using GChartWpf.Models;

namespace GChartWpf.Controls;

/// <summary>
/// Professional TradingView-Grade Candlestick & Time-Axis Chart Engine:
/// - PRE-MARKET SESSION (09:00-09:15 IST): Actual OHLCV data rendered with subtle background shading, "PRE-MARKET" badge, and 09:15 dashed vertical separator.
/// - REGULAR MARKET SESSION (09:15-15:30 IST): Regular trading session clearly demarcated with "REGULAR MARKET" label and accurate timeframe alignment.
/// - BOT RESTART / MISSING DATA FIX: Gaps during downtime are preserved as clean empty black space; historical candles never shift or duplicate.
/// - INDIAN MARKET TIME AXIS (Asia/Kolkata): Strict market sessions, weekends/holidays and overnight non-trading hours cleanly removed, with prominent session date separators.
/// - TRADINGVIEW STYLING: High-contrast green/red candles, price gridlines, nice-step round levels, live dotted price line & pill badge, interactive 2D crosshair, and smooth zoom/pan.
/// </summary>
public class CandleChart : FrameworkElement
{
    public static readonly DependencyProperty CandlesProperty = DependencyProperty.Register(
        nameof(Candles), typeof(IReadOnlyList<Candle>), typeof(CandleChart),
        new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty IsDailyProperty = DependencyProperty.Register(
        nameof(IsDaily), typeof(bool), typeof(CandleChart),
        new FrameworkPropertyMetadata(false, FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty TimeframeProperty = DependencyProperty.Register(
        nameof(Timeframe), typeof(string), typeof(CandleChart),
        new FrameworkPropertyMetadata("1m", FrameworkPropertyMetadataOptions.AffectsRender));

    public IReadOnlyList<Candle>? Candles { get => (IReadOnlyList<Candle>?)GetValue(CandlesProperty); set => SetValue(CandlesProperty, value); }
    public bool IsDaily { get => (bool)GetValue(IsDailyProperty); set => SetValue(IsDailyProperty, value); }
    public string Timeframe { get => (string)GetValue(TimeframeProperty); set => SetValue(TimeframeProperty, value); }

    // Interactive Zoom, Pan & Scaling State
    int _visibleCount = 60;
    int _panOffset = -25; // Negative = future margin on the right; Positive = historical scroll
    double _verticalPriceOffset = 0;
    double _priceScaleFactor = 1.0;
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
    static readonly Brush TvUp = Frozen(8, 153, 129);          // TradingView Green #089981
    static readonly Brush TvDown = Frozen(242, 54, 69);        // TradingView Red #F23645
    static readonly Brush PreMarketBg = Frozen(12, 19, 34);    // Subtle Dark Slate/Navy for Pre-Market
    static readonly Brush Grey = Frozen(140, 140, 140);
    static readonly Brush DarkGrey = Frozen(25, 25, 25);
    static readonly Brush AxisTagBg = Frozen(45, 45, 45);
    static readonly Brush DateTagBg = Frozen(38, 38, 38);
    static readonly Brush Amber = Frozen(245, 158, 11);        // Gold/Amber #F59E0B
    static readonly Brush Sky = Frozen(56, 189, 248);          // Sky Blue #38BDF8
    static readonly Brush White = Brushes.White;

    static readonly Pen GridPen = MakePen(Color.FromRgb(30, 30, 30), 1, true);
    static readonly Pen AxisBorderPen = MakePen(Color.FromRgb(48, 48, 48), 1, false);
    static readonly Pen CrosshairPen = MakePen(Color.FromRgb(100, 100, 100), 1, true);
    static readonly Pen TvUpPen = MakePen(Color.FromRgb(8, 153, 129), 1, false);
    static readonly Pen TvDownPen = MakePen(Color.FromRgb(242, 54, 69), 1, false);
    static readonly Pen LastPen = MakePen(Color.FromRgb(220, 220, 220), 1, true);
    static readonly Pen OpenSeparatorPen = MakePen(Color.FromRgb(245, 158, 11), 1.2, true); // 09:15 dashed line
    static readonly Pen DateSeparatorPen = MakePen(Color.FromRgb(65, 65, 65), 1, false);   // Session date line

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

    public class ChartSlot
    {
        public int SlotIndex;
        public long TimeMs;
        public Candle? Candle;
        public bool IsPreMarket;
        public bool IsRegularMarket;
        public bool IsDayFirstSlot;
        public bool Is0915Open;
        public string? DateLabel;
    }

    List<ChartSlot> GenerateSlots(IReadOnlyList<Candle> all)
    {
        var slots = new List<ChartSlot>();
        if (all == null || all.Count == 0) return slots;

        var sorted = all.Where(c => c.TimeMs > 0).OrderBy(c => c.TimeMs).ToList();
        if (sorted.Count == 0) return slots;

        if (IsDaily)
        {
            int slotIdx = 0;
            DateTime? lastDt = null;
            foreach (var c in sorted)
            {
                var dt = DateTimeOffset.FromUnixTimeMilliseconds(c.TimeMs).ToOffset(TimeSpan.FromHours(5.5)).Date;
                bool newMonth = (lastDt == null || dt.Month != lastDt.Value.Month);
                string? dLabel = newMonth ? dt.ToString("dd MMM") : null;
                slots.Add(new ChartSlot
                {
                    SlotIndex = slotIdx++,
                    TimeMs = c.TimeMs,
                    Candle = c,
                    IsPreMarket = false,
                    IsRegularMarket = true,
                    IsDayFirstSlot = newMonth,
                    Is0915Open = false,
                    DateLabel = dLabel
                });
                lastDt = dt;
            }
            return slots;
        }

        // Intraday Mode (1m, 3m, 5m, 15m) aligned to Indian Market hours (IST = UTC+5:30)
        long durMs = Timeframe switch
        {
            "3m" => 180_000L,
            "5m" => 300_000L,
            "15m" => 900_000L,
            _ => 60_000L
        };

        var tz = TimeSpan.FromHours(5.5);
        var dayGroups = sorted.GroupBy(c => DateTimeOffset.FromUnixTimeMilliseconds(c.TimeMs).ToOffset(tz).Date)
                              .OrderBy(g => g.Key)
                              .ToList();

        var todayIst = DateTimeOffset.UtcNow.ToOffset(tz).Date;
        int globalSlotIdx = 0;

        foreach (var group in dayGroups)
        {
            var date = group.Key;
            var dt0900 = new DateTimeOffset(date.Year, date.Month, date.Day, 9, 0, 0, tz);
            long sessionStartMs = dt0900.ToUnixTimeMilliseconds();

            var dt1530 = new DateTimeOffset(date.Year, date.Month, date.Day, 15, 30, 0, tz);
            long sessionLimitMs = dt1530.ToUnixTimeMilliseconds();

            long minCandleMs = group.Min(c => c.TimeMs);
            long maxCandleMs = group.Max(c => c.TimeMs);

            if (minCandleMs < sessionStartMs)
            {
                sessionStartMs = (minCandleMs / durMs) * durMs;
            }

            long sessionEndMs;
            if (date == todayIst)
            {
                long nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                long currentBucket = (nowMs / durMs) * durMs;
                sessionEndMs = Math.Max(maxCandleMs, Math.Min(currentBucket, sessionLimitMs));
            }
            else
            {
                sessionEndMs = Math.Max(maxCandleMs, sessionLimitMs);
            }

            var candleMap = new Dictionary<long, Candle>();
            foreach (var c in group)
            {
                long bucket = (c.TimeMs / durMs) * durMs;
                candleMap[bucket] = c;
            }

            bool isFirstSlotOfDay = true;
            long prevSlotTimeMs = 0;

            for (long t = sessionStartMs; t <= sessionEndMs; t += durMs)
            {
                var ist = DateTimeOffset.FromUnixTimeMilliseconds(t).ToOffset(tz);
                var tod = ist.TimeOfDay;

                if (tod < new TimeSpan(9, 0, 0) || tod > new TimeSpan(15, 30, 0))
                    continue;

                candleMap.TryGetValue(t, out var candle);

                bool isPre = (tod >= new TimeSpan(9, 0, 0) && tod < new TimeSpan(9, 15, 0));
                bool isReg = (tod >= new TimeSpan(9, 15, 0) && tod <= new TimeSpan(15, 30, 0));
                bool is0915 = (tod == new TimeSpan(9, 15, 0)) ||
                              (prevSlotTimeMs > 0 && isPre == false && DateTimeOffset.FromUnixTimeMilliseconds(prevSlotTimeMs).ToOffset(tz).TimeOfDay < new TimeSpan(9, 15, 0));

                string? dateLabel = isFirstSlotOfDay ? ist.ToString("dd MMM") : null;

                slots.Add(new ChartSlot
                {
                    SlotIndex = globalSlotIdx++,
                    TimeMs = t,
                    Candle = candle,
                    IsPreMarket = isPre,
                    IsRegularMarket = isReg,
                    IsDayFirstSlot = isFirstSlotOfDay,
                    Is0915Open = is0915,
                    DateLabel = dateLabel
                });

                isFirstSlotOfDay = false;
                prevSlotTimeMs = t;
            }
        }

        return slots;
    }

    // ---------------- Interactive Mouse Events ----------------

    protected override void OnMouseWheel(MouseWheelEventArgs e)
    {
        base.OnMouseWheel(e);
        var pt = e.GetPosition(this);
        if (pt.X >= _lastPlotWidth && pt.Y <= _lastPriceHeight)
        {
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
            // Double-click -> Snap live candle back to CENTER
            _visibleCount = 60;
            _panOffset = -(_visibleCount / 2);
            _verticalPriceOffset = 0;
            _priceScaleFactor = 1.0;
            InvalidateVisual();
            return;
        }

        if (pt.X >= _lastPlotWidth && pt.Y <= _lastPriceHeight)
        {
            _isPriceDragging = true;
            _isChartDragging = false;
            _dragStartPoint = pt;
            _dragStartScaleFactor = _priceScaleFactor;
            CaptureMouse();
            Cursor = Cursors.SizeNS;
        }
        else
        {
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
            double dy = pt.Y - _dragStartPoint.Y;
            _priceScaleFactor = Math.Max(0.15, Math.Min(8.0, _dragStartScaleFactor * Math.Pow(1.006, -dy)));
        }
        else if (_isChartDragging)
        {
            double dx = pt.X - _dragStartPoint.X;
            double slot = Math.Max(2, _lastSlotWidth);
            int candleShift = (int)Math.Round(dx / slot);

            var all = Candles;
            int total = all?.Count ?? 0;
            int minPan = -(int)(_visibleCount * 0.85);
            int maxPan = Math.Max(0, total + 120);
            _panOffset = Math.Max(minPan, Math.Min(maxPan, _dragStartPan + candleShift));

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
            Cursor = Cursors.SizeNS;
        else
            Cursor = Cursors.Cross;
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

        var slots = GenerateSlots(all);
        int totalSlots = slots.Count;
        if (totalSlots == 0)
        {
            var t = Txt("Waiting for market data…", Grey, 14);
            dc.DrawText(t, new Point((w - t.Width) / 2, (h - t.Height) / 2));
            return;
        }

        int count = Math.Min(totalSlots, _visibleCount);

        const double axisW = 82, timeH = 24, gap = 6;
        double volH = Math.Max(28, h * 0.13);
        double plotW = Math.Max(10, w - axisW), priceH = Math.Max(10, h - timeH - volH - gap);
        _lastPlotWidth = plotW;
        _lastPriceHeight = priceH;

        double slotW = plotW / count;
        _lastSlotWidth = slotW;

        int startIdx = Math.Max(0, totalSlots - count - _panOffset);
        int endIdx = Math.Min(totalSlots - 1, totalSlots - 1 - _panOffset);

        var visSlots = new List<ChartSlot>();
        if (startIdx <= endIdx && startIdx < totalSlots && endIdx >= 0)
        {
            for (int k = Math.Max(0, startIdx); k <= Math.Min(totalSlots - 1, endIdx); k++)
                visSlots.Add(slots[k]);
        }
        if (visSlots.Count == 0 && totalSlots > 0)
        {
            visSlots.Add(slots[^1]);
        }

        // Determine price bounds from visible valid candles
        var validCandles = visSlots.Where(s => s.Candle != null && s.Candle.Low > 0 && s.Candle.High > 0)
                                   .Select(s => s.Candle!)
                                   .ToList();

        var lastValidCandle = all.LastOrDefault(c => c.Close > 0) ?? all[^1];
        if (validCandles.Count == 0) validCandles.Add(lastValidCandle);

        double baseHi = validCandles.Max(c => c.High);
        double baseLo = validCandles.Min(c => c.Low);
        double livePrice = lastValidCandle.Close;

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

        // 1. Pre-Market Background Shading (09:00 - 09:15 IST)
        bool hasPreMarketInView = false;
        double preMarketStartX = -1, preMarketEndX = -1;

        for (int k = Math.Max(0, startIdx); k <= Math.Min(totalSlots - 1, endIdx); k++)
        {
            var s = slots[k];
            int screenIdx = count - totalSlots + k + _panOffset;
            if (screenIdx < 0 || screenIdx >= count) continue;

            double cx = screenIdx * slotW + slotW / 2;
            if (s.IsPreMarket)
            {
                hasPreMarketInView = true;
                if (preMarketStartX < 0) preMarketStartX = cx - slotW / 2;
                preMarketEndX = cx + slotW / 2;

                // Subtle dark shading for pre-market zone
                dc.DrawRectangle(PreMarketBg, null, new Rect(cx - slotW / 2, 0, slotW, priceH + gap + volH));
            }
        }

        // Pre-Market Label Pill
        if (hasPreMarketInView && preMarketStartX >= 0)
        {
            double preW = Math.Max(60, preMarketEndX - preMarketStartX);
            var preLabel = Txt("PRE-MARKET (09:00–09:15)", Sky, 9, bold: true);
            double pillX = Math.Max(preMarketStartX + 4, 10);
            if (pillX + preLabel.Width + 10 < plotW)
            {
                dc.DrawRoundedRectangle(DarkGrey, MakePen(Color.FromRgb(30, 41, 59), 1, false), new Rect(pillX, 6, preLabel.Width + 10, 16), 3, 3);
                dc.DrawText(preLabel, new Point(pillX + 5, 7.5));
            }
        }

        // Draw axis separator lines
        dc.DrawLine(AxisBorderPen, new Point(plotW, 0), new Point(plotW, h - timeH));
        dc.DrawLine(AxisBorderPen, new Point(0, h - timeH), new Point(w, h - timeH));

        // 2. Horizontal Price Gridlines + Clean Round Number Levels (TradingView Nice Steps)
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

        // 3. Render Slots: Candlesticks, Missing Data (Empty Space), Session Separators, Time Axis
        double bodyW = Math.Max(1, slotW * 0.72);
        ChartSlot? hoveredSlot = null;

        for (int k = Math.Max(0, startIdx); k <= Math.Min(totalSlots - 1, endIdx); k++)
        {
            var s = slots[k];
            int screenIdx = count - totalSlots + k + _panOffset;
            if (screenIdx < 0 || screenIdx >= count) continue;

            double cx = screenIdx * slotW + slotW / 2;

            // Session Date Separator (start of a new trading day)
            if (s.IsDayFirstSlot)
            {
                double lineX = cx - slotW / 2;
                dc.DrawLine(DateSeparatorPen, new Point(lineX, 0), new Point(lineX, h - timeH));

                if (!string.IsNullOrEmpty(s.DateLabel))
                {
                    var dTxt = Txt(s.DateLabel, Amber, 9.5, bold: true);
                    dc.DrawRoundedRectangle(DateTagBg, null, new Rect(lineX + 2, h - timeH + 3, dTxt.Width + 6, 17), 2, 2);
                    dc.DrawText(dTxt, new Point(lineX + 5, h - timeH + 4.5));
                }
            }

            // 09:15 AM Market Open Dashed Vertical Separator
            if (s.Is0915Open)
            {
                double lineX = cx - slotW / 2;
                dc.DrawLine(OpenSeparatorPen, new Point(lineX, 0), new Point(lineX, h - timeH));

                var openBadge = Txt("09:15 OPEN", Amber, 8.5, bold: true);
                if (lineX + openBadge.Width + 8 < plotW)
                {
                    dc.DrawRoundedRectangle(DarkGrey, OpenSeparatorPen, new Rect(lineX + 2, 26, openBadge.Width + 8, 15), 2, 2);
                    dc.DrawText(openBadge, new Point(lineX + 6, 27.5));
                }
            }

            // Candlestick rendering (Only if actual candle exists; empty slots remain pure black space)
            if (s.Candle != null)
            {
                var c = s.Candle;
                bool up = c.Close >= c.Open;

                // Wick
                dc.DrawLine(up ? TvUpPen : TvDownPen, new Point(cx, Y(c.High)), new Point(cx, Y(c.Low)));

                // Body
                double top = Y(Math.Max(c.Open, c.Close));
                double bh = Math.Max(1, Math.Abs(Y(c.Open) - Y(c.Close)));
                dc.DrawRectangle(up ? TvUp : TvDown, null, new Rect(cx - bodyW / 2, top, bodyW, bh));

                // Volume Bar
                double vh = volH * c.Volume / maxVol;
                dc.DrawRectangle(up ? TvUp : TvDown, null, new Rect(cx - bodyW / 2, priceH + gap + volH - vh, bodyW, vh));
            }

            // Time axis labels
            if (!s.IsDayFirstSlot && screenIdx % Math.Max(1, count / 7) == 0)
            {
                var ts = DateTimeOffset.FromUnixTimeMilliseconds(s.TimeMs).ToOffset(TimeSpan.FromHours(5.5));
                string label = IsDaily ? ts.ToString("dd MMM", CultureInfo.InvariantCulture) : ts.ToString("HH:mm", CultureInfo.InvariantCulture);
                dc.DrawText(Txt(label, Grey, 9.5), new Point(cx - 14, h - timeH + 5));
            }
        }

        // 4. Real-time Live Price Dotted Line & Pill Badge on Right Price Scale
        double ly = Y(livePrice);
        if (ly >= 0 && ly <= priceH)
        {
            dc.DrawLine(LastPen, new Point(0, ly), new Point(plotW, ly));

            var badgeBrush = lastValidCandle.Close >= lastValidCandle.Open ? TvUp : TvDown;
            var pillRect = new Rect(plotW + 3, ly - 9.5, axisW - 6, 19);
            dc.DrawRoundedRectangle(badgeBrush, null, pillRect, 2, 2);

            var tag = Txt(livePrice.ToString("N2", CultureInfo.InvariantCulture), White, 11, bold: true);
            dc.DrawText(tag, new Point(plotW + 6, ly - 7.5));
        }

        // 5. Interactive Crosshair Tracker
        if (_hoverPos.HasValue)
        {
            var hp = _hoverPos.Value;
            if (hp.X >= 0 && hp.X <= plotW && hp.Y >= 0 && hp.Y <= priceH + gap + volH)
            {
                dc.DrawLine(CrosshairPen, new Point(hp.X, 0), new Point(hp.X, h - timeH));

                if (hp.Y <= priceH)
                {
                    dc.DrawLine(CrosshairPen, new Point(0, hp.Y), new Point(plotW, hp.Y));

                    double hoveredPrice = lo + (hi - lo) * (1.0 - hp.Y / priceH);
                    var priceText = Txt(hoveredPrice.ToString("N2", CultureInfo.InvariantCulture), White, 10.5);
                    dc.DrawRoundedRectangle(AxisTagBg, null, new Rect(plotW + 3, hp.Y - 8.5, axisW - 6, 17), 2, 2);
                    dc.DrawText(priceText, new Point(plotW + 6, hp.Y - 7));
                }

                int hoverScreenSlot = (int)Math.Floor(hp.X / slotW);
                int hoverSlotIdx = hoverScreenSlot - (count - totalSlots + _panOffset);

                if (hoverSlotIdx >= 0 && hoverSlotIdx < totalSlots)
                {
                    hoveredSlot = slots[hoverSlotIdx];
                    var hts = DateTimeOffset.FromUnixTimeMilliseconds(hoveredSlot.TimeMs).ToOffset(TimeSpan.FromHours(5.5));
                    string timeStr = hts.ToString(IsDaily ? "dd MMM yyyy" : "HH:mm:ss", CultureInfo.InvariantCulture);
                    var timeText = Txt(timeStr, White, 10);
                    double tx = Math.Max(2, Math.Min(plotW - timeText.Width - 8, hp.X - timeText.Width / 2));
                    dc.DrawRoundedRectangle(AxisTagBg, null, new Rect(tx - 4, h - timeH + 2, timeText.Width + 8, 18), 2, 2);
                    dc.DrawText(timeText, new Point(tx, h - timeH + 4));
                }
                else if (hoverSlotIdx >= totalSlots && totalSlots > 0)
                {
                    long stepMs = IsDaily ? 86400000L : 60000L;
                    long futureMs = slots[^1].TimeMs + (hoverSlotIdx - (totalSlots - 1)) * stepMs;
                    var fts = DateTimeOffset.FromUnixTimeMilliseconds(futureMs).ToOffset(TimeSpan.FromHours(5.5));
                    string timeStr = fts.ToString(IsDaily ? "dd MMM yyyy" : "HH:mm:ss", CultureInfo.InvariantCulture);
                    var timeText = Txt(timeStr, Frozen(180, 180, 180), 10);
                    double tx = Math.Max(2, Math.Min(plotW - timeText.Width - 8, hp.X - timeText.Width / 2));
                    dc.DrawRoundedRectangle(AxisTagBg, null, new Rect(tx - 4, h - timeH + 2, timeText.Width + 8, 18), 2, 2);
                    dc.DrawText(timeText, new Point(tx, h - timeH + 4));
                }
            }
        }

        // 6. Top Status & OHLC Information Strip
        if (hoveredSlot != null)
        {
            if (hoveredSlot.Candle != null)
            {
                var hc = hoveredSlot.Candle;
                double hChg = hc.Close - hc.Open;
                double hPct = hc.Open > 0 ? (hChg / hc.Open * 100) : 0;
                var chgBrush = hChg >= 0 ? TvUp : TvDown;
                var infoStr = $"O: {hc.Open:N2}  H: {hc.High:N2}  L: {hc.Low:N2}  C: {hc.Close:N2}  Vol: {hc.Volume:N0}  ({hChg:+0.00;-0.00;0.00} / {hPct:+0.00;-0.00;0.00}%)";
                var infoTxt = Txt(infoStr, chgBrush, 10, bold: true);
                dc.DrawRoundedRectangle(DarkGrey, null, new Rect(10, 8, infoTxt.Width + 14, 18), 2, 2);
                dc.DrawText(infoTxt, new Point(17, 10));
            }
            else
            {
                var hts = DateTimeOffset.FromUnixTimeMilliseconds(hoveredSlot.TimeMs).ToOffset(TimeSpan.FromHours(5.5));
                var emptyTxt = Txt($"[EMPTY INTERVAL · BOT OFFLINE · {hts:HH:mm} IST]", Amber, 9.5, bold: true);
                dc.DrawRoundedRectangle(DarkGrey, null, new Rect(10, 8, emptyTxt.Width + 14, 18), 2, 2);
                dc.DrawText(emptyTxt, new Point(17, 10));
            }
        }
        else
        {
            // Navigation pills
            int defaultCenterPan = -(count / 2);
            bool isHistorical = _panOffset > 0;
            bool isCustomPan = Math.Abs(_panOffset - defaultCenterPan) > 4 || Math.Abs(_verticalPriceOffset) > 1.0 || Math.Abs(_priceScaleFactor - 1.0) > 0.1;

            if (isHistorical)
            {
                var histPill = Txt($"◀ PAUSED ({_panOffset} slots back) · Double-click to LIVE CENTER", Frozen(255, 179, 0), 10);
                dc.DrawRoundedRectangle(DarkGrey, null, new Rect(10, 8, histPill.Width + 12, 18), 2, 2);
                dc.DrawText(histPill, new Point(16, 10));
            }
            else if (isCustomPan)
            {
                var customPill = Txt("↔ Custom View · Double-click to LIVE CENTER", Frozen(100, 180, 255), 9.5);
                dc.DrawRoundedRectangle(DarkGrey, null, new Rect(10, 8, customPill.Width + 10, 16), 2, 2);
                dc.DrawText(customPill, new Point(15, 9));
            }
        }
    }
}
