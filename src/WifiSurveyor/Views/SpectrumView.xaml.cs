using System.Diagnostics;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using WifiSurveyor.RF;
using WifiSurveyor.Services;
using WifiSurveyor.Surveying;
using WifiSurveyor.Wifi;
using Shapes = System.Windows.Shapes;

namespace WifiSurveyor.Views;

/// <summary>
/// Live spectrum. A preset is one or more frequency ranges. One range sweeps continuously.
/// Several ranges (different modules, or wider than the 600 MHz span limit) are swept in turn,
/// a few sweeps each, and shown side by side with one panel per band.
/// </summary>
public partial class SpectrumView : UserControl
{
    sealed record RangeSpec(double Start, double End, string Group);
    sealed record GroupSpec(string Title, double Weight);
    sealed record Preset(string Name, RangeSpec[] Ranges);

    static readonly Dictionary<string, GroupSpec> Groups = new()
    {
        ["2.4"] = new("2.4 GHz", 1),
        ["5"] = new("5 GHz", 2.2),
        ["6"] = new("6 GHz: 5925–6100 MHz is the analyzer's limit (band runs to 7125)", 1.2),
        ["custom"] = new("Custom range", 1)
    };

    static readonly RangeSpec R24 = new(2400, 2500, "2.4");
    static readonly RangeSpec R5Low = new(5150, 5500, "5");
    static readonly RangeSpec R5High = new(5500, 5925, "5");
    static readonly RangeSpec R6 = new(5925, 6100, "6");

    static readonly Preset[] Presets =
    {
        new("2.4 GHz", new[] { R24 }),
        new("5 GHz, lower (5150–5500)", new[] { R5Low }),
        new("5 GHz, upper (5500–5925)", new[] { R5High }),
        new("6 GHz, channels 1–29 (5925–6100)", new[] { R6 }),
        new("5 GHz, full band (2 ranges)", new[] { R5Low, R5High }),
        new("2.4 and 5 GHz (3 ranges)", new[] { R24, R5Low, R5High }),
        new("2.4, 5, and 6 GHz (4 ranges)", new[] { R24, R5Low, R5High, R6 }),
        new("Custom", Array.Empty<RangeSpec>())
    };

    const double DbTop = -20, DbBottom = -110;
    const double PlotLeft = 44, PlotRight = 10;
    const int WfCols = 400, WfRows = 300;
    const int NoData = unchecked((int)0xFF151A22);

    static readonly SolidColorBrush GridLine = Draw.Brush(255, 255, 255, 24);
    static readonly SolidColorBrush PscLine = Draw.Brush(255, 255, 255, 60);
    static readonly SolidColorBrush PanelRule = Draw.Brush(255, 255, 255, 50);
    static readonly SolidColorBrush AxisText = Draw.Brush(169, 182, 200);
    static readonly SolidColorBrush TitleText = Draw.Brush(226, 232, 240);
    static readonly SolidColorBrush LiveTrace = Draw.Brush(250, 204, 21);
    static readonly SolidColorBrush MaxTrace = Draw.Brush(248, 113, 113, 200);
    static readonly SolidColorBrush AvgTrace = Draw.Brush(56, 189, 248);
    static readonly SolidColorBrush NetMarker = Draw.Brush(255, 255, 255, 220);
    static readonly SolidColorBrush ErrorText = Draw.Brush(252, 165, 165);

    sealed class Segment
    {
        public Segment(RangeSpec spec) => Spec = spec;
        public RangeSpec Spec { get; }
        public RfExplorerConfig? Config { get; set; }
        public double[]? Last { get; set; }
        public double[]? Max { get; set; }
        public double[]? AvgMw { get; set; }
        public int AvgCount { get; set; }
        public int Received { get; set; }
        public string? Error { get; set; }

        public double[]? Avg => AvgMw == null || AvgCount == 0
            ? null
            : AvgMw.Select(v => SpectrumAnalysis.ToDbm(v / AvgCount)).ToArray();

        public double? ValueAt(double[]? data, double f)
        {
            if (data == null || Config is not { StepMHz: > 0 } c) return null;
            int i = (int)Math.Round((f - c.StartMHz) / c.StepMHz);
            return i >= 0 && i < data.Length ? data[i] : null;
        }

        public void Reset()
        {
            Last = Max = AvgMw = null;
            AvgCount = 0;
        }
    }

    sealed class ChartPanel
    {
        public ChartPanel(GroupSpec group, List<Segment> segments, Canvas canvas, Image image)
        {
            Group = group;
            Segments = segments;
            Canvas = canvas;
            Image = image;
        }

        public GroupSpec Group { get; }
        public List<Segment> Segments { get; }
        public Canvas Canvas { get; }
        public Image Image { get; }
        public WriteableBitmap? Bitmap { get; set; }
        public int[]? Pixels { get; set; }
        public double Start => Segments.Min(s => s.Spec.Start);
        public double End => Segments.Max(s => s.Spec.End);
    }

    readonly AppServices _svc = AppServices.Instance;
    readonly List<ChartPanel> _panels = new();
    RfExplorerDevice? _dev;
    Segment[] _segments = Array.Empty<Segment>();
    Segment? _activeSeg;
    TaskCompletionSource? _segmentDone;
    CancellationTokenSource? _runCts;
    int _skip, _target;
    bool _live, _ready;
    DateTime _lastDraw = DateTime.MinValue;
    (Canvas C, Point P)? _mouse;

    bool Cycling => _live && _segments.Length > 1;

    public SpectrumView()
    {
        InitializeComponent();
        foreach (var p in Presets) PresetBox.Items.Add(p.Name);
        PresetBox.SelectedIndex = 0;
        SetRanges(Presets[0].Ranges);

        _svc.SpectrumReleased += () => Dispatcher.InvokeAsync(() =>
        {
            // Cycling mode reconfigures on every range anyway; a single range needs re-applying.
            if (_live && !Cycling) RestartRun();
        });
        _svc.DevicesChanged += () => Dispatcher.InvokeAsync(() =>
        {
            if (_live && !ReferenceEquals(_svc.Rfe, _dev)) Stop("The analyzer was disconnected.");
        });
        _ready = true;
    }

    // ================= Range selection =================

    void Preset_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (!_ready) return;
        var p = Presets[Math.Max(0, PresetBox.SelectedIndex)];
        if (p.Ranges.Length == 0) return;   // Custom: wait for "Use custom range"
        if (p.Ranges.Length == 1)
        {
            StartBox.Text = p.Ranges[0].Start.ToString(CultureInfo.CurrentCulture);
            EndBox.Text = p.Ranges[0].End.ToString(CultureInfo.CurrentCulture);
        }
        SetRanges(p.Ranges);
    }

    void Apply_Click(object sender, RoutedEventArgs e)
    {
        if (!TryParse(StartBox.Text, out var s) || !TryParse(EndBox.Text, out var end) || end <= s)
        {
            SpecStatus.Text = "Enter a start and end frequency in MHz, with the end above the start.";
            return;
        }
        PresetBox.SelectedIndex = Presets.Length - 1;
        SetRanges(new[] { new RangeSpec(s, end, "custom") });
    }

    void SetRanges(RangeSpec[] ranges)
    {
        _runCts?.Cancel();
        _activeSeg = null;
        _segments = ranges.Select(r => new Segment(r)).ToArray();
        BuildLayout();
        WaterfallLabel.Text = _segments.Length > 1
            ? "Waterfall, one row per full cycle, newest on top"
            : "Waterfall, newest on top";
        if (_live) RestartRun();
    }

    void BuildLayout()
    {
        ChartHost.Children.Clear();
        ChartHost.ColumnDefinitions.Clear();
        WaterfallHost.Children.Clear();
        WaterfallHost.ColumnDefinitions.Clear();
        _panels.Clear();

        int col = 0;
        foreach (var group in _segments.GroupBy(s => s.Spec.Group))
        {
            var spec = Groups[group.Key];
            var canvas = new Canvas { Background = Brushes.Transparent, ClipToBounds = true };
            var image = new Image { Stretch = Stretch.Fill, Margin = new Thickness(PlotLeft, 0, PlotRight, 0) };
            RenderOptions.SetBitmapScalingMode(image, BitmapScalingMode.NearestNeighbor);

            canvas.SizeChanged += (_, _) => Redraw();
            canvas.MouseMove += (_, e) =>
            {
                _mouse = (canvas, e.GetPosition(canvas));
                if (!_live) Redraw();
            };
            canvas.MouseLeave += (_, _) =>
            {
                _mouse = null;
                Redraw();
            };

            foreach (var host in new[] { ChartHost, WaterfallHost })
                host.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(spec.Weight, GridUnitType.Star) });
            Grid.SetColumn(canvas, col);
            Grid.SetColumn(image, col);
            ChartHost.Children.Add(canvas);
            WaterfallHost.Children.Add(image);

            _panels.Add(new ChartPanel(spec, group.ToList(), canvas, image));
            col++;
        }
        Redraw();
    }

    static bool TryParse(string text, out double value) =>
        double.TryParse(text, NumberStyles.Float, CultureInfo.CurrentCulture, out value) ||
        double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out value);

    int SweepsPerRange() => int.TryParse(SweepsBox.Text, out var k) ? Math.Clamp(k, 1, 50) : 3;

    // ================= Start / stop =================

    void StartStop_Click(object sender, RoutedEventArgs e)
    {
        if (_live)
        {
            Stop("Stopped.");
            return;
        }
        _dev = _svc.Rfe;
        if (_dev is not { IsOpen: true })
        {
            SpecStatus.Text = "Connect the RF Explorer on the Devices tab first.";
            return;
        }
        _dev.SweepReceived += OnSweep;
        _live = true;
        StartStop.Content = "Stop";
        RestartRun();
    }

    void Stop(string message)
    {
        _runCts?.Cancel();
        _runCts = null;
        _activeSeg = null;
        if (_dev != null) _dev.SweepReceived -= OnSweep;
        _live = false;
        StartStop.Content = "Start";
        SpecStatus.Text = message;
        Redraw();
    }

    void RestartRun()
    {
        _runCts?.Cancel();
        _runCts = new CancellationTokenSource();
        var ct = _runCts.Token;
        _activeSeg = null;
        if (_segments.Length == 1) _ = RunSingleAsync(_segments, ct);
        else if (_segments.Length > 1) _ = RunCycleAsync(_segments, ct);
    }

    async Task RunSingleAsync(Segment[] segs, CancellationToken ct)
    {
        var seg = segs[0];
        try
        {
            if (_svc.SurveyUsingSpectrum) SpecStatus.Text = "Waiting for the survey measurement to finish…";
            await _svc.AnalyzerLock.WaitAsync(ct);
            try
            {
                SpecStatus.Text = "Configuring the analyzer…";
                var cfg = await _dev!.ConfigureAsync(seg.Spec.Start, seg.Spec.End, ct);
                ct.ThrowIfCancellationRequested();
                seg.Config = cfg;
                seg.Error = null;
                _skip = 1;
                _target = int.MaxValue;
                _segmentDone = null;
                _activeSeg = seg;
                SpecStatus.Text =
                    $"{cfg.StartMHz:0.###}–{cfg.EndMHz:0.###} MHz, {cfg.SweepPoints} points, RBW {cfg.RbwKHz:0} kHz";
            }
            finally
            {
                _svc.AnalyzerLock.Release();
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            seg.Error = ex.Message;
            SpecStatus.Text = ex.Message;
        }
        Redraw();
    }

    async Task RunCycleAsync(Segment[] segs, CancellationToken ct)
    {
        var timer = new Stopwatch();
        try
        {
            while (true)
            {
                timer.Restart();
                int k = SweepsPerRange();
                for (int i = 0; i < segs.Length; i++)
                {
                    var seg = segs[i];
                    if (_svc.SurveyUsingSpectrum) SpecStatus.Text = "Paused while a survey point is measured…";
                    await _svc.AnalyzerLock.WaitAsync(ct);
                    bool configured = false;
                    try
                    {
                        SpecStatus.Text = $"Sweeping {seg.Spec.Start:0}–{seg.Spec.End:0} MHz (range {i + 1} of {segs.Length})";
                        var cfg = await _dev!.ConfigureAsync(seg.Spec.Start, seg.Spec.End, ct);
                        ct.ThrowIfCancellationRequested();
                        configured = true;
                        seg.Config = cfg;
                        var done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                        seg.Received = 0;
                        _skip = 1;   // first sweep after a reconfigure can be partial
                        _target = k;
                        _segmentDone = done;
                        _activeSeg = seg;
                        Redraw();
                        await done.Task.WaitAsync(TimeSpan.FromSeconds(10 + k * 4), ct);
                        seg.Error = null;
                    }
                    catch (Exception ex) when (ex is TimeoutException or InvalidOperationException)
                    {
                        seg.Error = configured ? "No sweeps arrived for this range." : ex.Message;
                    }
                    finally
                    {
                        if (ReferenceEquals(_activeSeg, seg))
                        {
                            _activeSeg = null;
                            _segmentDone = null;
                        }
                        _svc.AnalyzerLock.Release();
                    }
                    if (seg.Error != null) await Task.Delay(1000, ct);
                }

                PushWaterfall();
                Redraw();
                SpecStatus.Text =
                    $"{segs.Length} ranges, {k} sweeps each. Last full cycle took {timer.Elapsed.TotalSeconds:0.0} s.";
            }
        }
        catch (OperationCanceledException) { }
    }

    // ================= Sweep handling =================

    // Called on the serial reader thread
    void OnSweep(double[] data, RfExplorerConfig cfg)
    {
        if (!_live || _svc.SurveyUsingSpectrum) return;
        Dispatcher.InvokeAsync(() => OnSweepUi(data, cfg));
    }

    void OnSweepUi(double[] data, RfExplorerConfig cfg)
    {
        var seg = _activeSeg;
        if (!_live || seg?.Config == null) return;
        if (Math.Abs(cfg.StartMHz - seg.Config.StartMHz) > Math.Max(0.5, cfg.StepMHz)) return;   // another range's sweep
        if (_skip > 0)
        {
            _skip--;
            return;
        }

        seg.Config = cfg;
        if (seg.Max == null || seg.AvgMw == null || seg.Max.Length != data.Length)
        {
            seg.Max = (double[])data.Clone();
            seg.AvgMw = new double[data.Length];
            seg.AvgCount = 0;
        }
        for (int i = 0; i < data.Length; i++)
        {
            if (data[i] > seg.Max[i]) seg.Max[i] = data[i];
            seg.AvgMw[i] += SpectrumAnalysis.ToMw(data[i]);
        }
        seg.AvgCount++;
        seg.Last = data;
        seg.Error = null;

        if (!Cycling) PushWaterfall();
        if (++seg.Received >= _target) _segmentDone?.TrySetResult();

        if ((DateTime.UtcNow - _lastDraw).TotalMilliseconds > 80)
        {
            Redraw();
            _lastDraw = DateTime.UtcNow;
        }
    }

    void Reset_Click(object sender, RoutedEventArgs e)
    {
        foreach (var s in _segments) s.Reset();
        foreach (var p in _panels)
        {
            if (p.Pixels == null || p.Bitmap == null) continue;
            Array.Fill(p.Pixels, 0);
            p.Bitmap.WritePixels(new Int32Rect(0, 0, WfCols, WfRows), p.Pixels, WfCols * 4, 0);
        }
        Redraw();
    }

    void Option_Click(object sender, RoutedEventArgs e) => Redraw();

    /// <summary>Adds one waterfall row per panel, resampled onto a fixed number of columns.</summary>
    void PushWaterfall()
    {
        foreach (var p in _panels)
        {
            if (p.Bitmap == null || p.Pixels == null)
            {
                p.Pixels = new int[WfCols * WfRows];
                p.Bitmap = new WriteableBitmap(WfCols, WfRows, 96, 96, PixelFormats.Bgra32, null);
                p.Image.Source = p.Bitmap;
            }
            var px = p.Pixels;
            Array.Copy(px, 0, px, WfCols, WfCols * (WfRows - 1));   // scroll down one row

            double start = p.Start, span = p.End - p.Start;
            for (int col = 0; col < WfCols; col++)
            {
                double f = start + (col + 0.5) / WfCols * span;
                double? v = null;
                foreach (var seg in p.Segments)
                {
                    if (f < seg.Spec.Start || f > seg.Spec.End) continue;
                    v = seg.ValueAt(seg.Last, f);
                    if (v != null) break;
                }
                px[col] = v is double d
                    ? HeatmapRenderer.SpectrumColor((d - DbBottom) / (DbTop - DbBottom))
                    : NoData;
            }
            p.Bitmap.WritePixels(new Int32Rect(0, 0, WfCols, WfRows), px, WfCols * 4, 0);
        }
    }

    // ================= Drawing =================

    void Redraw()
    {
        for (int i = 0; i < _panels.Count; i++) DrawPanel(_panels[i], first: i == 0);
    }

    void DrawPanel(ChartPanel p, bool first)
    {
        var c = p.Canvas;
        c.Children.Clear();
        if (c.ActualWidth < 100 || c.ActualHeight < 80) return;

        double s = p.Start, e = p.End;
        var plot = new PlotArea(c, s, e, DbBottom, DbTop, PlotLeft, PlotRight);
        if (!first) Draw.Line(c, 0.5, 0, 0.5, c.ActualHeight, PanelRule);
        Draw.Text(c, p.Group.Title, plot.Rect.Left, 1, TitleText, 11);
        Draw.YGrid(c, plot, 10, AxisText, GridLine);

        double step = Draw.NiceStep((e - s) / Math.Max(2, plot.Rect.Width / 70));
        for (double f = Math.Ceiling(s / step) * step; f <= e + 1e-9; f += step)
            Draw.Text(c, f.ToString("0.#"), plot.X(f), plot.Rect.Bottom + 4, AxisText, 10, center: true);

        // Channel markers. 6 GHz channels are looked up by frequency since their numbers overlap 2.4 GHz.
        double lastLabel = double.NegativeInfinity;
        foreach (var ch in WifiChannels.All.Concat(WifiChannels.Band6)
                     .Where(ch => ch.CenterMHz >= s && ch.CenterMHz <= e)
                     .OrderBy(ch => ch.CenterMHz))
        {
            double x = plot.X(ch.CenterMHz);
            bool psc = ch.Band == "6" && WifiChannels.IsPsc(ch.Number);
            Draw.Line(c, x, plot.Rect.Top + 12, x, plot.Rect.Bottom, psc ? PscLine : GridLine, 1, dashed: !psc);
            if (x - lastLabel < 22) continue;
            Draw.Text(c, psc ? $"{ch.Number}*" : ch.Number.ToString(), x, plot.Rect.Top, psc ? TitleText : AxisText, 10,
                center: true);
            lastLabel = x;
        }

        // Which range is being swept right now
        if (Cycling && _activeSeg is { } act && p.Segments.Contains(act))
        {
            double x0 = plot.X(Math.Max(act.Spec.Start, s)), x1 = plot.X(Math.Min(act.Spec.End, e));
            var bar = new Shapes.Rectangle { Width = Math.Max(1, x1 - x0), Height = 3, Fill = LiveTrace };
            Canvas.SetLeft(bar, x0);
            Canvas.SetTop(bar, plot.Rect.Bottom - 3);
            c.Children.Add(bar);
        }

        if (ShowNets.IsChecked == true)
        {
            // Laptop RSSI and analyzer power use different antennas, so treat these as labels, not levels.
            foreach (var n in _svc.LastScan.Where(n => n.FrequencyMHz >= s && n.FrequencyMHz <= e))
            {
                double x = plot.X(n.FrequencyMHz), y = plot.Y(Math.Clamp(n.Rssi, DbBottom, DbTop));
                var dot = new Shapes.Ellipse { Width = 6, Height = 6, Fill = NetMarker };
                Canvas.SetLeft(dot, x - 3);
                Canvas.SetTop(dot, y - 3);
                c.Children.Add(dot);
                if (n.Rssi > -80)
                    Draw.Text(c, string.IsNullOrEmpty(n.Ssid) ? "(hidden)" : n.Ssid, x + 5, y - 7, NetMarker, 10);
            }
        }

        foreach (var seg in p.Segments)
        {
            if (ShowMax.IsChecked == true && seg.Max != null) Draw.Poly(c, Trace(plot, seg, seg.Max), MaxTrace, 1);
            if (ShowAvg.IsChecked == true && seg.Avg is { } avg) Draw.Poly(c, Trace(plot, seg, avg), AvgTrace, 1.5);
            if (seg.Last != null) Draw.Poly(c, Trace(plot, seg, seg.Last), LiveTrace, 1.5);
            if (seg.Error != null)
            {
                var t = Draw.Text(c, seg.Error, plot.X(Math.Max(seg.Spec.Start, s)) + 6,
                    plot.Rect.Top + plot.Rect.Height / 2, ErrorText, 11);
                t.MaxWidth = Math.Max(60, plot.X(Math.Min(seg.Spec.End, e)) - plot.X(Math.Max(seg.Spec.Start, s)) - 12);
                t.TextWrapping = TextWrapping.Wrap;
            }
        }

        if (_mouse is { } m && ReferenceEquals(m.C, c) && plot.Rect.Contains(m.P))
        {
            double f = plot.XValue(m.P.X);
            var seg = p.Segments.FirstOrDefault(sg =>
                f >= sg.Spec.Start && f <= sg.Spec.End && sg.ValueAt(sg.Last, f) != null);
            if (seg != null)
            {
                var text = $"{f:0.000} MHz   live {seg.ValueAt(seg.Last, f):0.0} dBm";
                if (seg.ValueAt(seg.Max, f) is double mx) text += $"   max {mx:0.0} dBm";
                double tx = m.P.X + 8 > plot.Rect.Right - 230 ? m.P.X - 238 : m.P.X + 8;
                Draw.Line(c, m.P.X, plot.Rect.Top, m.P.X, plot.Rect.Bottom, AxisText);
                Draw.Text(c, text, Math.Max(plot.Rect.Left, tx), plot.Rect.Top + 16, Brushes.White, 12);
            }
        }
    }

    static IEnumerable<Point> Trace(PlotArea plot, Segment seg, double[] data)
    {
        double step = seg.Config is { StepMHz: > 0 } c
            ? c.StepMHz
            : (seg.Spec.End - seg.Spec.Start) / Math.Max(1, data.Length - 1);
        double start = seg.Config?.StartMHz ?? seg.Spec.Start;
        for (int i = 0; i < data.Length; i++)
            yield return new Point(plot.X(start + i * step), plot.Y(Math.Clamp(data[i], DbBottom, DbTop)));
    }
}
