using System.Globalization;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Win32;
using WifiSurveyor.Models;
using WifiSurveyor.Positioning;
using WifiSurveyor.Services;
using WifiSurveyor.Surveying;
using WifiSurveyor.Wifi;
using Shapes = System.Windows.Shapes;

namespace WifiSurveyor.Views;

public record FilterItem(string Label, string? Value)
{
    public override string ToString() => Label;
}

public partial class SurveyView : UserControl
{
    static readonly SolidColorBrush PendingBrush = Draw.Brush(29, 78, 216);
    static readonly SolidColorBrush AnchorBrush = Draw.Brush(126, 34, 206);
    static readonly SolidColorBrush GpsBrush = Draw.Brush(14, 116, 144);
    static readonly SolidColorBrush ApEstimateBrush = Draw.Brush(29, 78, 216);
    static readonly SolidColorBrush InterferenceBrush = Draw.Brush(147, 51, 234);
    static readonly SolidColorBrush WallBrush = Draw.Brush(71, 85, 105);
    static readonly SolidColorBrush SuggestedWallBrush = Draw.Brush(217, 119, 6);
    static readonly SolidColorBrush InferredWallBrush = Draw.Brush(13, 148, 136);
    static readonly SolidColorBrush WalkBrush = Draw.Brush(71, 85, 105);

    readonly AppServices _svc = AppServices.Instance;
    readonly DispatcherTimer _gpsTimer = new() { Interval = TimeSpan.FromSeconds(2) };
    readonly List<Point> _calibClicks = new();
    readonly List<Point> _wallClicks = new();
    Barrier? _selectedWall;
    int _dragHandle;          // 0 none, 1 first end, 2 second end, 3 whole wall
    Point _dragFrom;
    List<TransmitterEstimate> _estimates = new();
    List<NoiseSource> _noise = new();

    SurveyProject _project = new();
    string? _projectPath;
    GeoTransform? _geo;
    SurveyPoint? _selected;
    Point? _pendingAnchor, _pendingCapture, _lastAutoPos;
    CancellationTokenSource? _captureCts, _renderCts;
    (double Min, double Max) _range = (0, 1);
    AnalysisContext _ctx;
    bool _ready, _dirty, _capturing, _updatingFilters, _recoveryOffered;
    StabilityRecorder? _walk;
    readonly Stack<(string Label, Action Restore)> _undo = new();
    readonly DispatcherTimer _autosaveTimer = new() { Interval = TimeSpan.FromMinutes(2) };
    StackPanel? _busyList;
    CaptureProgress? _busyProgress;
    double? _markerScaleOverride;   // set during image export so markers aren't zoom-scaled

    public SurveyView()
    {
        InitializeComponent();
        _ctx = BuildContext();
        foreach (var m in Materials.All) MaterialBox.Items.Add($"{m.Name} (about {m.Db:0} dB)");
        MaterialBox.SelectedIndex = 0;
        foreach (var m in Enum.GetValues<HeatmapMetric>())
            MetricBox.Items.Add(new ComboBoxItem { Content = MetricInfo.Get(m).Title, Tag = m });
        MetricBox.SelectedIndex = 0;

        _svc.PointsChanged += () => Dispatcher.InvokeAsync(() =>
        {
            MarkChanged();
            UpdateSelectedInfo();
            RefreshHeatmap();
        });
        _gpsTimer.Tick += GpsTimer_Tick;
        _gpsTimer.Start();
        _autosaveTimer.Tick += (_, _) => Autosave();
        _autosaveTimer.Start();
        LoadTargets();
        Loaded += (_, _) => OfferRecovery();

        LoadProjectIntoUi();
        _ready = true;
        RefreshHeatmap();
    }

    // ================= Project =================

    public bool ConfirmDiscard()
    {
        if (!_dirty) return true;
        var r = MessageBox.Show("Save changes to this survey first?", "Wi-Fi Surveyor",
            MessageBoxButton.YesNoCancel, MessageBoxImage.Question);
        if (r == MessageBoxResult.Cancel) return false;
        return r == MessageBoxResult.No || SaveProject(saveAs: false);
    }

    void New_Click(object sender, RoutedEventArgs e)
    {
        if (!ConfirmDiscard()) return;
        _project = new SurveyProject();
        _projectPath = null;
        _dirty = false;
        LoadProjectIntoUi();
    }

    void Open_Click(object sender, RoutedEventArgs e)
    {
        if (!ConfirmDiscard()) return;
        var dlg = new OpenFileDialog { Filter = ProjectStore.FileFilter };
        if (dlg.ShowDialog() != true) return;
        try
        {
            _project = ProjectStore.Load(dlg.FileName);
            _projectPath = dlg.FileName;
            _dirty = false;
            LoadProjectIntoUi();
        }
        catch (Exception ex)
        {
            MessageBox.Show($"Couldn't open the project: {ex.Message}", "Wi-Fi Surveyor");
        }
    }

    void Save_Click(object sender, RoutedEventArgs e) => SaveProject(saveAs: false);
    void SaveAs_Click(object sender, RoutedEventArgs e) => SaveProject(saveAs: true);

    bool SaveProject(bool saveAs)
    {
        if (saveAs || _projectPath == null)
        {
            var dlg = new SaveFileDialog { Filter = ProjectStore.FileFilter, FileName = _project.Name };
            if (dlg.ShowDialog() != true) return false;
            _projectPath = dlg.FileName;
            _project.Name = Path.GetFileNameWithoutExtension(dlg.FileName);
        }
        try
        {
            ProjectStore.Save(_projectPath, _project);
            _dirty = false;
            Recovery.Clear();
            UpdateProjectInfo();
            return true;
        }
        catch (Exception ex)
        {
            MessageBox.Show($"Couldn't save: {ex.Message}", "Wi-Fi Surveyor");
            return false;
        }
    }

    void LoadPlan_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new OpenFileDialog { Filter = "Images|*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.tif;*.tiff" };
        if (dlg.ShowDialog() != true) return;
        try
        {
            var bytes = File.ReadAllBytes(dlg.FileName);
            var bmp = DecodeImage(bytes);
            if (_project.Points.Count > 0 &&
                MessageBox.Show("Existing points keep their pixel positions on the new image. Continue?",
                    "Wi-Fi Surveyor", MessageBoxButton.OKCancel) != MessageBoxResult.OK)
                return;

            _project.FloorPlanBase64 = Convert.ToBase64String(bytes);
            _project.FloorPlanWidth = bmp.PixelWidth;
            _project.FloorPlanHeight = bmp.PixelHeight;
            _project.MetersPerPixel = null;
            if (_project.Points.Count == 0) _project.GeoRef.Anchors.Clear();
            _dirty = true;
            LoadProjectIntoUi();
            ModeCalibrate.IsChecked = true;
            CaptureStatus.Text = "Plan loaded. Next, set the scale by clicking both ends of a known distance.";
        }
        catch (Exception ex)
        {
            MessageBox.Show($"Couldn't read that image: {ex.Message}", "Wi-Fi Surveyor");
        }
    }

    static BitmapImage DecodeImage(byte[] bytes)
    {
        var bmp = new BitmapImage();
        using var ms = new MemoryStream(bytes);
        bmp.BeginInit();
        bmp.CacheOption = BitmapCacheOption.OnLoad;
        bmp.StreamSource = ms;
        bmp.EndInit();
        bmp.Freeze();
        return bmp;
    }

    static BitmapSource BlankPlan(int w, int h)
    {
        var visual = new DrawingVisual();
        using (var dc = visual.RenderOpen())
        {
            dc.DrawRectangle(Brushes.White, null, new Rect(0, 0, w, h));
            var minor = new Pen(Draw.Brush(226, 232, 240), 1);
            var major = new Pen(Draw.Brush(203, 213, 225), 1);
            for (int x = 0; x <= w; x += 50) dc.DrawLine(x % 250 == 0 ? major : minor, new Point(x, 0), new Point(x, h));
            for (int y = 0; y <= h; y += 50) dc.DrawLine(y % 250 == 0 ? major : minor, new Point(0, y), new Point(w, y));
        }
        var rtb = new RenderTargetBitmap(w, h, 96, 96, PixelFormats.Pbgra32);
        rtb.Render(visual);
        rtb.Freeze();
        return rtb;
    }

    void LoadProjectIntoUi()
    {
        int w = _project.FloorPlanWidth, h = _project.FloorPlanHeight;
        PlanImage.Source = _project.FloorPlanBase64 is { } b64
            ? DecodeImage(Convert.FromBase64String(b64))
            : BlankPlan(w, h);

        foreach (var el in new FrameworkElement[] { PlanImage, Overlay })
        {
            el.Width = w;
            el.Height = h;
        }
        HeatmapImage.Source = null;
        _selected = null;
        _calibClicks.Clear();
        _pendingAnchor = null;
        _lastAutoPos = null;
        _geo = GeoTransform.TryCreate(_project.GeoRef.Anchors);
        _ctx = BuildContext();
        _walk?.Dispose();          // a recorder belongs to the project it was started on
        _walk = null;
        WalkButton.Content = "Start the run";

        RebuildFilterLists();
        UpdateProjectInfo();
        UpdateGeorefInfo();
        UpdateWallInfo();
        UpdateWalkInfo();
        UpdateSelectedInfo();
        if (_ready) Dispatcher.InvokeAsync(FitToWindow, DispatcherPriority.Loaded);
        RefreshHeatmap();
    }

    void UpdateProjectInfo()
    {
        var scale = _project.MetersPerPixel is double mpp
            ? $"{1 / mpp:0.##} pixels per meter ({_project.FloorPlanWidth * mpp:0.#} × {_project.FloorPlanHeight * mpp:0.#} m)"
            : "scale not set";
        var plan = _project.FloorPlanBase64 == null ? "blank grid" : $"{_project.FloorPlanWidth} × {_project.FloorPlanHeight} px plan";
        ProjectInfo.Text = $"{_project.Name}{(_dirty ? " (unsaved)" : "")}\n{plan}, {scale}\n{_project.Points.Count} measurement points";
    }

    // ================= Autosave and recovery =================

    void Autosave()
    {
        if (!_dirty || _project.Points.Count == 0) return;
        try
        {
            ProjectStore.Save(Recovery.ProjectPath, _project);
            File.WriteAllText(Recovery.MarkerPath, _projectPath ?? "");
        }
        catch { /* a failed autosave must never interrupt a survey */ }
    }

    void OfferRecovery()
    {
        if (_recoveryOffered || _project.Points.Count > 0) return;
        _recoveryOffered = true;
        if (Recovery.Describe() is not { } info) return;

        var where = info.OriginalPath is { Length: > 0 } path
            ? $"It was last saved as {Path.GetFileName(path)}."
            : "It had never been saved to a file.";
        if (MessageBox.Show(
                $"A survey was recovered from {info.When:g}, from a session that didn't close normally. " +
                $"{where}\n\nOpen it?",
                "Wi-Fi Surveyor", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes)
        {
            Recovery.Clear();
            return;
        }

        try
        {
            _project = ProjectStore.Load(Recovery.ProjectPath);
            _projectPath = info.OriginalPath is { Length: > 0 } original && File.Exists(original)
                ? original : null;
            _dirty = true;
            LoadProjectIntoUi();
            CaptureStatus.Text = "Recovered. Save it somewhere before carrying on.";
        }
        catch (Exception ex)
        {
            MessageBox.Show("Couldn't open the recovered survey: " + ex.Message, "Wi-Fi Surveyor");
        }
    }

    // ================= Targets =================

    void LoadTargets()
    {
        var s = _svc.Settings;
        TargetRssi.Text = s.TargetRssi.ToString("0", CultureInfo.CurrentCulture);
        TargetSnr.Text = s.TargetSnr.ToString("0", CultureInfo.CurrentCulture);
        TargetAirtime.Text = s.TargetAirtime.ToString("0", CultureInfo.CurrentCulture);
        TargetPhy.Text = s.TargetPhyRate.ToString("0", CultureInfo.CurrentCulture);
    }

    void Targets_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        var s = _svc.Settings;
        if (TryParseNumber(TargetRssi.Text, out var rssi)) s.TargetRssi = Math.Clamp(rssi, -95, -30);
        if (TryParseNumber(TargetSnr.Text, out var snr)) s.TargetSnr = Math.Clamp(snr, 0, 50);
        if (TryParseNumber(TargetAirtime.Text, out var airtime)) s.TargetAirtime = Math.Clamp(airtime, 5, 100);
        if (TryParseNumber(TargetPhy.Text, out var phy)) s.TargetPhyRate = Math.Clamp(phy, 1, 5000);
        s.Save();
        LoadTargets();

        _ctx = BuildContext();
        InvalidateEstimates();
        RefreshHeatmap();
    }

    AnalysisContext BuildContext()
    {
        var s = _svc.Settings;
        return new AnalysisContext(_project, new QualityTargets
        {
            MinRssi = s.TargetRssi,
            MinSnr = s.TargetSnr,
            MaxOccupancy = s.TargetAirtime / 100,
            MinPhyRateMbps = s.TargetPhyRate
        });
    }

    // ================= Modes and clicks =================

    void Mode_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        SelectPanel.Visibility = ModeSelect.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
        CalibratePanel.Visibility = ModeCalibrate.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
        GeorefPanel.Visibility = ModeGeoref.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
        WallPanel.Visibility = ModeWall.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
        WalkPanel.Visibility = ModeWalk.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
        _calibClicks.Clear();
        _wallClicks.Clear();
        _selectedWall = null;
        _pendingAnchor = null;
        DrawOverlay();
    }

    async void Overlay_MouseLeftButtonDown(object sender, MouseButtonEventArgs e) =>
        await HandleTapAsync(e.GetPosition(Overlay));

    async Task HandleTapAsync(Point p)
    {
        if (ModeSurvey.IsChecked == true)
        {
            await CaptureAtAsync(p.X, p.Y);
        }
        else if (ModeSelect.IsChecked == true)
        {
            double limit = 14 / Zoom;
            _selected = _project.Points
                .Select(pt => (pt, d: Math.Sqrt((pt.X - p.X) * (pt.X - p.X) + (pt.Y - p.Y) * (pt.Y - p.Y))))
                .Where(t => t.d <= limit)
                .OrderBy(t => t.d)
                .Select(t => t.pt)
                .FirstOrDefault();
            _svc.SelectedPoint = _selected;
            UpdateSelectedInfo();
            DrawOverlay();
        }
        else if (ModeCalibrate.IsChecked == true)
        {
            if (_calibClicks.Count >= 2) _calibClicks.Clear();
            _calibClicks.Add(p);
            DrawOverlay();
        }
        else if (ModeGeoref.IsChecked == true)
        {
            _pendingAnchor = p;
            DrawOverlay();
        }
        else if (ModeWalk.IsChecked == true)
        {
            if (_walk is not { Running: true })
            {
                WalkInfo.Text = "Start the run first, then click to mark where you are.";
                return;
            }
            _walk.AddBreadcrumb(p.X, p.Y);
            MarkChanged();
            UpdateWalkInfo();
            DrawOverlay();
        }
        else if (ModeWall.IsChecked == true)
        {
            if (_wallClicks.Count == 0 && HitWall(p) is { } hit)
            {
                _selectedWall = hit.Wall;
                _dragHandle = hit.Handle;
                _dragFrom = p;
                if (Mouse.LeftButton == MouseButtonState.Pressed) Overlay.CaptureMouse();
                SyncMaterialBox();
                DrawOverlay();
                return;
            }
            _wallClicks.Add(p);
            if (_wallClicks.Count == 2)
            {
                var material = Materials.All[Math.Max(0, MaterialBox.SelectedIndex)];
                _project.Barriers.Add(new Barrier
                {
                    X1 = _wallClicks[0].X, Y1 = _wallClicks[0].Y,
                    X2 = _wallClicks[1].X, Y2 = _wallClicks[1].Y,
                    Material = material.Name, Db = material.Db
                });
                _wallClicks.Clear();
                MarkChanged();
                UpdateWallInfo();
                InvalidateEstimates();
            }
            DrawOverlay();
        }
    }

    void Overlay_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (_dragHandle == 0) return;
        _dragHandle = 0;
        Overlay.ReleaseMouseCapture();
        MarkChanged();
        InvalidateEstimates();
    }

    // ================= touch =================

    Point? _touchStart;
    DateTime _touchStarted;

    void Overlay_TouchDown(object sender, TouchEventArgs e)
    {
        _touchStart = e.GetTouchPoint(Overlay).Position;
        _touchStarted = DateTime.UtcNow;
    }

    async void Overlay_TouchUp(object sender, TouchEventArgs e)
    {
        if (_touchStart is not Point start) return;
        _touchStart = null;

        // A tap is a touch that didn't travel far and didn't linger; a drag is the map panning.
        var end = e.GetTouchPoint(Overlay).Position;
        double movedOnScreen = (end - start).Length * Zoom;
        if (movedOnScreen > 22 || (DateTime.UtcNow - _touchStarted).TotalMilliseconds > 900) return;
        await HandleTapAsync(end);
    }

    void Plan_ManipulationStarting(object sender, ManipulationStartingEventArgs e)
    {
        e.ManipulationContainer = PlanScroller;
        e.Mode = ManipulationModes.All;
    }

    void Plan_ManipulationDelta(object sender, ManipulationDeltaEventArgs e)
    {
        var delta = e.DeltaManipulation;

        double scale = (delta.Scale.X + delta.Scale.Y) / 2;
        if (Math.Abs(scale - 1) > 0.0005) ZoomAround(e.ManipulationOrigin, scale);

        PlanScroller.ScrollToHorizontalOffset(PlanScroller.HorizontalOffset - delta.Translation.X);
        PlanScroller.ScrollToVerticalOffset(PlanScroller.VerticalOffset - delta.Translation.Y);
        e.Handled = true;
    }

    void Plan_InertiaStarting(object sender, ManipulationInertiaStartingEventArgs e)
    {
        // Roughly ten device-independent pixels per squared millisecond: a flick that settles.
        e.TranslationBehavior.DesiredDeceleration = 10.0 * 96 / (1000.0 * 1000.0);
        e.Handled = true;
    }

    void Plan_MouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (Keyboard.Modifiers != ModifierKeys.Control) return;   // plain wheel keeps scrolling
        ZoomAround(e.GetPosition(PlanScroller), e.Delta > 0 ? 1.15 : 1 / 1.15);
        e.Handled = true;
    }

    /// <summary>Zooms while keeping whatever is under the fingers (or pointer) in place.</summary>
    void ZoomAround(Point origin, double factor)
    {
        double current = ZoomSlider.Value;
        double next = Math.Clamp(current * factor, ZoomSlider.Minimum, ZoomSlider.Maximum);
        if (Math.Abs(next - current) < 1e-6) return;

        double contentX = (PlanScroller.HorizontalOffset + origin.X) / current;
        double contentY = (PlanScroller.VerticalOffset + origin.Y) / current;

        ZoomSlider.Value = next;
        PlanScroller.UpdateLayout();
        PlanScroller.ScrollToHorizontalOffset(contentX * next - origin.X);
        PlanScroller.ScrollToVerticalOffset(contentY * next - origin.Y);
    }

    void ZoomIn_Click(object sender, RoutedEventArgs e) => ZoomAround(Centre(), 1.25);
    void ZoomOut_Click(object sender, RoutedEventArgs e) => ZoomAround(Centre(), 1 / 1.25);

    Point Centre() => new(PlanScroller.ViewportWidth / 2, PlanScroller.ViewportHeight / 2);

    void TouchEdit_Click(object sender, RoutedEventArgs e)
    {
        bool editing = (sender as System.Windows.Controls.Primitives.ToggleButton)?.IsChecked == true;
        TouchEdit.IsChecked = editing;
        TouchEditFull.IsChecked = editing;
        // With manipulation off, touches promote to mouse events, so drawing and dragging work.
        PlanScroller.IsManipulationEnabled = !editing;
        CursorInfo.Text = editing
            ? "One finger now draws and drags. Pinch still zooms."
            : "One finger pans the map.";
    }

    void FullScreen_Click(object sender, RoutedEventArgs e) =>
        SetFullScreen(FullScreenBar.Visibility != Visibility.Visible);

    public void SetFullScreen(bool on)
    {
        if (Window.GetWindow(this) is not MainWindow window) return;
        window.SetMapFullScreen(on, MapRoot);
        FullScreenBar.Visibility = on ? Visibility.Visible : Visibility.Collapsed;
        Dispatcher.InvokeAsync(FitToWindow, DispatcherPriority.Loaded);
    }

    void Overlay_MouseMove(object sender, MouseEventArgs e)
    {
        var p = e.GetPosition(Overlay);

        if (_dragHandle != 0 && _selectedWall is { } wall && e.LeftButton == MouseButtonState.Pressed)
        {
            double dx = p.X - _dragFrom.X, dy = p.Y - _dragFrom.Y;
            switch (_dragHandle)
            {
                case 1:
                    wall.X1 = p.X;
                    wall.Y1 = p.Y;
                    break;
                case 2:
                    wall.X2 = p.X;
                    wall.Y2 = p.Y;
                    break;
                default:
                    wall.X1 += dx; wall.Y1 += dy;
                    wall.X2 += dx; wall.Y2 += dy;
                    break;
            }
            _dragFrom = p;
            DrawOverlay();
        }
        var sb = new StringBuilder($"x {p.X:0}, y {p.Y:0} px");
        if (_project.MetersPerPixel is double mpp) sb.Append($"   ({p.X * mpp:0.0}, {p.Y * mpp:0.0} m)");
        if (_geo != null)
        {
            var (lat, lon) = _geo.ToGeo(p.X, p.Y);
            sb.Append($"   {lat:0.000000}, {lon:0.000000}");
        }
        CursorInfo.Text = sb.ToString();
    }

    double Zoom => PlanScale.ScaleX;

    void Zoom_Changed(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (!_ready) return;
        PlanScale.ScaleX = PlanScale.ScaleY = ZoomSlider.Value;
        if (_capturing && _pendingCapture is Point busy) ShowBusy(busy.X, busy.Y);
        DrawOverlay();
    }

    void Fit_Click(object sender, RoutedEventArgs e) => FitToWindow();

    void FitToWindow()
    {
        double vw = PlanScroller.ViewportWidth - 24, vh = PlanScroller.ViewportHeight - 24;
        if (vw <= 0 || vh <= 0) return;
        ZoomSlider.Value = Math.Clamp(Math.Min(vw / _project.FloorPlanWidth, vh / _project.FloorPlanHeight),
            ZoomSlider.Minimum, ZoomSlider.Maximum);
    }

    // ================= Selection =================

    void UpdateSelectedInfo()
    {
        if (_selected is not { } p)
        {
            SelectedInfo.Text = "Click a point to see what was measured there.";
            return;
        }
        int index = _project.Points.IndexOf(p) + 1;
        var sb = new StringBuilder();
        sb.AppendLine($"Point {index}, measured {p.Time:g}");
        if (p.Connection is { } c)
            sb.AppendLine($"Connected to {c.Ssid} ({c.Bssid}), {c.PhyType}, TX {c.TxRateMbps:0} / RX {c.RxRateMbps:0} Mbps, {c.Auth}");
        if (p.Spectrum is { } s)
            sb.AppendLine($"Noise floor: 2.4 GHz {Fmt(s.NoiseFloor24)} dBm, 5 GHz {Fmt(s.NoiseFloor5)} dBm");
        if (p.ThroughputDownMbps != null || p.ThroughputUpMbps != null)
            sb.AppendLine($"iperf3: down {Fmt(p.ThroughputDownMbps)} / up {Fmt(p.ThroughputUpMbps)} Mbps");
        if (p.Gps is { } g)
            sb.AppendLine($"GPS: {g.Lat:0.000000}, {g.Lon:0.000000} ({GeoText.Describe(g).Replace(" (stale)", "")})");
        sb.AppendLine($"{p.Networks.Count} access points, strongest:");
        foreach (var n in p.Networks.Take(10))
            sb.AppendLine($"  {n.Rssi} dBm  ch {n.Channel}  {(string.IsNullOrEmpty(n.Ssid) ? "(hidden)" : n.Ssid)}");
        if (p.Capture is { Frames: > 0 } cap)
        {
            sb.AppendLine($"Captured {cap.Frames:N0} frames on channel {cap.Channel}: " +
                          $"{cap.RetryRate * 100:0.0}% retries, {cap.AirtimeFraction * 100:0.0}% airtime, " +
                          $"{cap.Clients} clients");
            var reported = cap.Bss.FirstOrDefault(b => b.ReportedUtilization != null);
            if (reported != null)
                sb.AppendLine($"{reported.Ssid} reports {reported.ReportedUtilization * 100:0}% utilisation " +
                              $"and {reported.ReportedStations} stations, on a {reported.WidthMHz} MHz channel");
        }

        var diagnosis = Diagnoser.Diagnose(p, GetFilter(), _ctx);
        sb.AppendLine();
        sb.AppendLine($"Verdict: {Verdicts.Label(diagnosis.Verdict)}");
        sb.AppendLine(diagnosis.Summary);
        sb.AppendLine(diagnosis.Advice);
        if (LinkEstimator.Estimate(p, GetFilter(), _ctx) is double est)
            sb.AppendLine($"The air here should support roughly {est:0} Mbps.");
        foreach (var e in EmitterFinder.Find(p, _ctx.OffsetFor(p)))
            sb.AppendLine($"Unexplained: {EmitterFinder.Label(e.Kind)} near {e.CenterMHz:0} MHz, " +
                          $"{e.WidthMHz:0} MHz wide, peak {e.PeakDbm:0} dBm.");
        foreach (var note in p.Notes) sb.AppendLine("Note: " + note);
        SelectedInfo.Text = sb.ToString();
    }

    static string Fmt(double? v) => v is double d ? d.ToString("0.0") : "–";

    void DeletePoint_Click(object sender, RoutedEventArgs e)
    {
        if (_selected == null) return;
        var removed = _selected;
        int at = _project.Points.IndexOf(removed);
        PushUndo("deleted a measurement point", () => _project.Points.Insert(Math.Clamp(at, 0, _project.Points.Count), removed));
        _project.Points.Remove(_selected);
        _selected = null;
        _svc.SelectedPoint = null;
        MarkChanged();
        UpdateSelectedInfo();
        RebuildFilterLists();
        RefreshHeatmap();
    }

    /// <summary>Remembers how to put something back before it's taken away.</summary>
    void PushUndo(string label, Action restore)
    {
        _undo.Push((label, restore));
        while (_undo.Count > 20) _undo.Pop();   // keeps the newest twenty
        UpdateUndoButton();
    }

    void UpdateUndoButton()
    {
        UndoButton.IsEnabled = _undo.Count > 0;
        UndoButton.ToolTip = _undo.Count > 0 ? $"Undo: {_undo.Peek().Label}" : null;
    }

    void Undo_Click(object sender, RoutedEventArgs e)
    {
        if (_undo.Count == 0) return;
        var (label, restore) = _undo.Pop();
        restore();
        MarkChanged();
        RebuildFilterLists();
        UpdateWallInfo();
        UpdateWalkInfo();
        UpdateSelectedInfo();
        RefreshHeatmap();
        UpdateUndoButton();
        CaptureStatus.Text = $"Undone: {label}.";
    }

    void MarkChanged()
    {
        _dirty = true;
        _ctx = BuildContext();   // offsets depend on the measurements
        UpdateProjectInfo();
        InvalidateEstimates();
    }

    // ================= Scale =================

    void ApplyCalibration_Click(object sender, RoutedEventArgs e)
    {
        if (_calibClicks.Count < 2)
        {
            CaptureStatus.Text = "Click two points on the plan first.";
            return;
        }
        if (!TryParseNumber(CalibMeters.Text, out var meters) || meters <= 0)
        {
            CaptureStatus.Text = "Enter the real distance in meters.";
            return;
        }
        double px = (_calibClicks[1] - _calibClicks[0]).Length;
        if (px < 5)
        {
            CaptureStatus.Text = "Those two clicks are too close together. Pick points farther apart.";
            return;
        }
        _project.MetersPerPixel = meters / px;
        _calibClicks.Clear();
        MarkChanged();
        CaptureStatus.Text = "Scale set. Choose \"Measure at this spot\" and click where you're standing.";
        ModeSurvey.IsChecked = true;
        RefreshHeatmap();
    }

    // ================= GPS pins =================

    void UseGpsForAnchor_Click(object sender, RoutedEventArgs e)
    {
        if (_svc.Location?.LastFix is not { IsFresh: true } fix)
        {
            GeorefInfo.Text = "There's no current GPS fix. Start the GPS on the Devices tab.";
            return;
        }
        GeoLat.Text = fix.Lat.ToString("0.0000000", CultureInfo.InvariantCulture);
        GeoLon.Text = fix.Lon.ToString("0.0000000", CultureInfo.InvariantCulture);
        if (!fix.IsGood) GeorefInfo.Text = "The GPS fix is weak, so this pin may be several meters off.";
    }

    void AddAnchor_Click(object sender, RoutedEventArgs e)
    {
        if (_pendingAnchor is not Point p)
        {
            GeorefInfo.Text = "Click the spot on the plan first.";
            return;
        }
        if (!TryParseNumber(GeoLat.Text, out var lat) || !TryParseNumber(GeoLon.Text, out var lon)
            || Math.Abs(lat) > 90 || Math.Abs(lon) > 180)
        {
            GeorefInfo.Text = "Enter latitude and longitude in decimal degrees, like 33.7490 and -84.3880.";
            return;
        }
        _project.GeoRef.Anchors.Add(new GeoAnchor { X = p.X, Y = p.Y, Lat = lat, Lon = lon });
        _pendingAnchor = null;
        _geo = GeoTransform.TryCreate(_project.GeoRef.Anchors);
        if (_geo != null && _project.MetersPerPixel == null) _project.MetersPerPixel = _geo.MetersPerPixel;
        MarkChanged();
        UpdateGeorefInfo();
        DrawOverlay();
    }

    void ClearAnchors_Click(object sender, RoutedEventArgs e)
    {
        var anchors = _project.GeoRef.Anchors.ToList();
        PushUndo($"removed {anchors.Count} GPS pin(s)", () =>
        {
            _project.GeoRef.Anchors.AddRange(anchors);
            _geo = GeoTransform.TryCreate(_project.GeoRef.Anchors);
        });
        _project.GeoRef.Anchors.Clear();
        _geo = null;
        MarkChanged();
        UpdateGeorefInfo();
        DrawOverlay();
    }

    (Barrier Wall, int Handle)? HitWall(Point p)
    {
        double grab = 12 / Math.Max(Zoom, 0.05);
        foreach (var b in _project.Barriers)
        {
            if (Near(p, b.X1, b.Y1, grab)) return (b, 1);
            if (Near(p, b.X2, b.Y2, grab)) return (b, 2);
        }
        foreach (var b in _project.Barriers)
            if (WallDetector.PointToSegment(p.X, p.Y, b.X1, b.Y1, b.X2, b.Y2) <= grab) return (b, 3);
        return null;
    }

    static bool Near(Point p, double x, double y, double limit) =>
        Math.Abs(p.X - x) <= limit && Math.Abs(p.Y - y) <= limit;

    void SyncMaterialBox()
    {
        if (_selectedWall == null) return;
        int index = Array.FindIndex(Materials.All, m => m.Name == _selectedWall.Material);
        if (index >= 0)
        {
            _updatingFilters = true;
            MaterialBox.SelectedIndex = index;
            _updatingFilters = false;
        }
    }

    void Material_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (!_ready || _updatingFilters || _selectedWall == null || MaterialBox.SelectedIndex < 0) return;
        var material = Materials.All[MaterialBox.SelectedIndex];
        _selectedWall.Material = material.Name;
        _selectedWall.Db = material.Db;
        MarkChanged();
        UpdateWallInfo();
        InvalidateEstimates();
        DrawOverlay();
    }

    void DeleteWall_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedWall == null)
        {
            WallInfo.Text = "Click a wall on the plan to select it first.";
            return;
        }
        var wall = _selectedWall;
        PushUndo("deleted a wall", () => _project.Barriers.Add(wall));
        _project.Barriers.Remove(_selectedWall);
        _selectedWall = null;
        MarkChanged();
        UpdateWallInfo();
        InvalidateEstimates();
        DrawOverlay();
    }

    async void InferWalls_Click(object sender, RoutedEventArgs e)
    {
        InferButton.IsEnabled = false;
        WallInfo.Text = "Working out where the signal is being blocked…";
        try
        {
            var project = _project;
            var ctx = _ctx;
            string summary = "";
            var walls = await Task.Run(() =>
            {
                var result = WallInference.Infer(project, ctx, out var text);
                summary = text;
                return result;
            });

            foreach (var wall in walls) _project.Barriers.Add(wall);
            MarkChanged();
            UpdateWallInfo();
            WallInfo.Text = summary;
            InvalidateEstimates();
            DrawOverlay();
        }
        catch (Exception ex)
        {
            WallInfo.Text = "Couldn't work that out: " + ex.Message;
        }
        finally
        {
            InferButton.IsEnabled = true;
        }
    }

    void RemoveInferred_Click(object sender, RoutedEventArgs e)
    {
        var guessed = _project.Barriers.Where(b => b.Inferred).ToList();
        PushUndo($"removed {guessed.Count} guessed wall(s)", () => _project.Barriers.AddRange(guessed));
        _project.Barriers.RemoveAll(b => b.Inferred);
        _selectedWall = null;
        MarkChanged();
        UpdateWallInfo();
        InvalidateEstimates();
        DrawOverlay();
    }

    async void DetectWalls_Click(object sender, RoutedEventArgs e)
    {
        if (PlanImage.Source is not BitmapSource plan)
        {
            WallInfo.Text = "Load a floor plan image first.";
            return;
        }

        var options = DetectDetail.SelectedIndex switch
        {
            0 => new WallDetectOptions(MinLengthPx: 90, MaxGapPx: 6, MaxWalls: 120),
            2 => new WallDetectOptions(MinLengthPx: 25, MaxGapPx: 4, MaxWalls: 400),
            _ => new WallDetectOptions(MinLengthPx: 45, MaxGapPx: 5, MaxWalls: 250)
        };

        DetectButton.IsEnabled = false;
        WallInfo.Text = "Looking for walls…";
        try
        {
            var (gray, width, height, scale) = ToGrayscale(plan, 1400);
            var found = await Task.Run(() => WallDetector.Detect(gray, width, height, options));

            foreach (var wall in _project.Barriers.Where(b => b.Suggested).ToList())
                _project.Barriers.Remove(wall);

            var material = Materials.All[0];
            foreach (var c in found)
                _project.Barriers.Add(new Barrier
                {
                    X1 = c.X1 / scale, Y1 = c.Y1 / scale,
                    X2 = c.X2 / scale, Y2 = c.Y2 / scale,
                    Material = material.Name, Db = material.Db, Suggested = true
                });

            SuggestionButtons.Visibility = found.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
            WallInfo.Text = found.Count == 0
                ? "Nothing wall-like found. Try finer detail, or draw the main walls by hand."
                : $"Found {found.Count} possible walls, shown dashed. They're ignored until you keep them. " +
                  "Delete any that are wrong, then keep the rest.";
            MarkChanged();
            DrawOverlay();
        }
        catch (Exception ex)
        {
            WallInfo.Text = "Couldn't search the image: " + ex.Message;
        }
        finally
        {
            DetectButton.IsEnabled = true;
        }
    }

    /// <summary>Greyscale copy of the plan, scaled down so detection stays quick.</summary>
    static (byte[] Gray, int Width, int Height, double Scale) ToGrayscale(BitmapSource source, int maxDimension)
    {
        double scale = Math.Min(1, maxDimension / (double)Math.Max(source.PixelWidth, source.PixelHeight));
        BitmapSource working = scale < 1
            ? new TransformedBitmap(source, new ScaleTransform(scale, scale))
            : source;
        var grayBitmap = new FormatConvertedBitmap(working, PixelFormats.Gray8, null, 0);

        int width = grayBitmap.PixelWidth, height = grayBitmap.PixelHeight;
        var pixels = new byte[width * height];
        grayBitmap.CopyPixels(pixels, width, 0);
        return (pixels, width, height, scale);
    }

    void KeepSuggestions_Click(object sender, RoutedEventArgs e)
    {
        int kept = 0;
        foreach (var b in _project.Barriers.Where(b => b.Suggested))
        {
            b.Suggested = false;
            kept++;
        }
        SuggestionButtons.Visibility = Visibility.Collapsed;
        MarkChanged();
        UpdateWallInfo();
        InvalidateEstimates();
        DrawOverlay();
        WallInfo.Text = $"Kept {kept} wall(s). They now count toward the source estimates.";
    }

    void DiscardSuggestions_Click(object sender, RoutedEventArgs e)
    {
        var found = _project.Barriers.Where(b => b.Suggested).ToList();
        PushUndo($"discarded {found.Count} found wall(s)", () => _project.Barriers.AddRange(found));
        _project.Barriers.RemoveAll(b => b.Suggested);
        SuggestionButtons.Visibility = Visibility.Collapsed;
        MarkChanged();
        UpdateWallInfo();
        DrawOverlay();
    }

    void RemoveWall_Click(object sender, RoutedEventArgs e)
    {
        if (_project.Barriers.Count == 0) return;
        _project.Barriers.RemoveAt(_project.Barriers.Count - 1);
        MarkChanged();
        UpdateWallInfo();
        InvalidateEstimates();
        DrawOverlay();
    }

    void ClearWalls_Click(object sender, RoutedEventArgs e)
    {
        var walls = _project.Barriers.ToList();
        PushUndo($"removed {walls.Count} wall(s)", () => _project.Barriers.AddRange(walls));
        _project.Barriers.Clear();
        _selectedWall = null;
        SuggestionButtons.Visibility = Visibility.Collapsed;
        MarkChanged();
        UpdateWallInfo();
        InvalidateEstimates();
        DrawOverlay();
    }

    void UpdateWallInfo()
    {
        int suggested = _project.Barriers.Count(b => b.Suggested);
        int inferred = _project.Barriers.Count(b => b.Inferred && !b.Suggested);
        int drawn = _project.Barriers.Count - suggested - inferred;
        var parts = new List<string>();
        if (drawn > 0) parts.Add($"{drawn} drawn");
        if (inferred > 0) parts.Add($"{inferred} guessed from measurements");
        if (suggested > 0) parts.Add($"{suggested} found in the image, awaiting review");
        WallInfo.Text = parts.Count == 0 ? "No walls yet." : string.Join(", ", parts) + ".";
    }

    void UpdateGeorefInfo()
    {
        int n = _project.GeoRef.Anchors.Count;
        GeorefInfo.Text = _geo == null
            ? n == 0 ? "No pins yet." : $"{n} pin placed. Add at least one more, far from the first."
            : $"{n} pins. Plan is linked to GPS; average pin error is {_geo.RmsErrorM:0.0} m" +
              (_project.MetersPerPixel is double mpp && Math.Abs(mpp - _geo.MetersPerPixel) / mpp > 0.1
                  ? $". The pins imply {1 / _geo.MetersPerPixel:0.##} px/m, which differs from your scale."
                  : ".");
    }

    // ================= Capture =================

    CaptureOptions ReadOptions()
    {
        var s = _svc.Settings;
        return new CaptureOptions
        {
            WifiScans = ParseInt(ScansPerPoint.Text, 2, 1, 10),
            UseSpectrum = UseSpectrum.IsChecked == true,
            SweepsPerSegment = ParseInt(SweepsPerSegment.Text, 15, 3, 200),
            DwellSeconds = UseDwell.IsChecked == true ? ParseInt(DwellSecondsBox.Text, 3, 1, 30) : 0,
            PcapSeconds = UsePcap.IsChecked == true ? ParseInt(PcapSecondsBox.Text, 8, 1, 120) : 0,
            PcapDevice = s.PcapDevice,
            PcapAdapterName = s.PcapAdapterName,
            RunIperf = UseIperf.IsChecked == true,
            IperfPath = s.IperfPath,
            IperfServer = s.IperfServer,
            IperfSeconds = s.IperfSeconds
        };
    }

    async Task CaptureAtAsync(double x, double y)
    {
        if (_capturing) return;
        if (x < 0 || y < 0 || x > _project.FloorPlanWidth || y > _project.FloorPlanHeight) return;

        var opts = ReadOptions();

        // Check everything at once and say so on the map: a refusal that only writes to the
        // side panel looks like nothing happened.
        var problems = new List<string>();
        if (opts.UseSpectrum && _svc.Rfe is not { IsOpen: true })
            problems.Add("Record spectrum is on, but the RF Explorer isn't connected (Devices tab).");
        if (opts.RunIperf && string.IsNullOrWhiteSpace(opts.IperfServer))
            problems.Add("Run iperf3 is on, but no server is set (Devices tab).");
        if (opts.PcapSeconds > 0 && string.IsNullOrWhiteSpace(opts.PcapDevice))
            problems.Add("Capture frames is on, but no monitor adapter has been chosen (Capture tab).");
        if (opts.PcapSeconds > 0 && !Capture.NpcapLive.IsInstalled)
            problems.Add("Capture frames is on, but this app does not capture live. Use the Linux app, or open a capture file.");

        if (problems.Count > 0)
        {
            ShowProblems(x, y, problems);
            CaptureStatus.Text = string.Join(" ", problems);
            return;
        }

        _capturing = true;
        _captureCts = new CancellationTokenSource();
        _pendingCapture = new Point(x, y);
        CancelCapture.Visibility = Visibility.Visible;
        DrawOverlay();

        ShowBusy(x, y);
        var progress = new Progress<CaptureProgress>(update =>
        {
            _busyProgress = update;
            UpdateBusyList();
            CaptureStatus.Text = $"{update.Current}…";
        });
        try
        {
            var point = await SurveyEngine.CaptureAsync(x, y, opts, progress, _captureCts.Token);
            _project.Points.Add(point);
            MarkChanged();
            var best = point.Networks.FirstOrDefault();
            CaptureStatus.Text = $"Point {_project.Points.Count} saved: {point.Networks.Count} access points" +
                                 (best != null ? $", strongest {best.Rssi} dBm" : "") +
                                 (point.Notes.Count > 0 ? "\n" + string.Join("\n", point.Notes) : "");
            RebuildFilterLists();
            RefreshHeatmap();
        }
        catch (OperationCanceledException)
        {
            CaptureStatus.Text = "Measurement cancelled. Nothing was saved.";
        }
        catch (Exception ex)
        {
            CaptureStatus.Text = "Measurement failed: " + ex.Message;
        }
        finally
        {
            _capturing = false;
            _pendingCapture = null;
            HideBusy();
            CancelCapture.Visibility = Visibility.Collapsed;
            _captureCts.Dispose();
            _captureCts = null;
            DrawOverlay();
        }
    }

    // ================= busy indicator =================

    void ShowBusy(double x, double y)
    {
        BusyLayer.Width = _project.FloorPlanWidth;
        BusyLayer.Height = _project.FloorPlanHeight;
        BusyLayer.Children.Clear();
        _busyProgress = null;

        double k = 1 / Math.Max(Zoom, 0.05);
        double size = 34 * k;

        var ring = new Shapes.Ellipse
        {
            Width = size,
            Height = size,
            Stroke = PendingBrush,
            StrokeThickness = 3.5 * k,
            StrokeDashArray = new DoubleCollection { 9, 5 },
            StrokeDashCap = PenLineCap.Round,
            RenderTransformOrigin = new Point(0.5, 0.5)
        };
        var spin = new RotateTransform();
        ring.RenderTransform = spin;
        spin.BeginAnimation(RotateTransform.AngleProperty,
            new DoubleAnimation(0, 360, new Duration(TimeSpan.FromSeconds(1.2)))
            {
                RepeatBehavior = RepeatBehavior.Forever
            });
        Canvas.SetLeft(ring, x - size / 2);
        Canvas.SetTop(ring, y - size / 2);
        BusyLayer.Children.Add(ring);

        _busyList = new StackPanel();
        var card = new Border
        {
            Background = Draw.Brush(255, 255, 255, 235),
            BorderBrush = PendingBrush,
            BorderThickness = new Thickness(1 * k),
            CornerRadius = new CornerRadius(4 * k),
            Padding = new Thickness(9 * k, 7 * k, 11 * k, 7 * k),
            Child = _busyList
        };
        Canvas.SetLeft(card, x + size * 0.75);
        Canvas.SetTop(card, y - size * 0.75);
        BusyLayer.Children.Add(card);

        UpdateBusyList();
    }

    void UpdateBusyList()
    {
        if (_busyList == null) return;
        double k = 1 / Math.Max(Zoom, 0.05);
        _busyList.Children.Clear();

        var plan = _busyProgress?.Plan ?? new List<string> { "Starting" };
        int active = _busyProgress?.Index ?? 0;

        for (int i = 0; i < plan.Count; i++)
        {
            bool done = i < active, running = i == active;
            var line = new TextBlock
            {
                FontSize = 12 * k,
                Margin = new Thickness(0, 0, 0, 2 * k),
                Foreground = done ? Draw.Brush(120, 130, 145) : running ? PendingBrush : Draw.Brush(90, 100, 115),
                FontWeight = running ? FontWeights.SemiBold : FontWeights.Normal,
                Text = (done ? "✓  " : running ? "▶  " : "•  ") + plan[i] +
                       (running && _busyProgress != null ? $" — {_busyProgress.Current}" : "")
            };
            _busyList.Children.Add(line);
        }
    }

    /// <summary>Shows why a measurement didn't start, next to where the user clicked.</summary>
    void ShowProblems(double x, double y, IReadOnlyList<string> problems)
    {
        BusyLayer.Width = _project.FloorPlanWidth;
        BusyLayer.Height = _project.FloorPlanHeight;
        BusyLayer.Children.Clear();
        _busyList = null;
        _busyProgress = null;

        double k = 1 / Math.Max(Zoom, 0.05);
        var stack = new StackPanel();
        stack.Children.Add(new TextBlock
        {
            Text = "Nothing measured here",
            FontSize = 13 * k,
            FontWeight = FontWeights.SemiBold,
            Foreground = Draw.Brush(185, 28, 28),
            Margin = new Thickness(0, 0, 0, 4 * k)
        });
        foreach (var problem in problems)
            stack.Children.Add(new TextBlock
            {
                Text = "• " + problem,
                FontSize = 12 * k,
                TextWrapping = TextWrapping.Wrap,
                MaxWidth = 340 * k,
                Margin = new Thickness(0, 0, 0, 2 * k),
                Foreground = Draw.Brush(70, 80, 95)
            });
        stack.Children.Add(new TextBlock
        {
            Text = "Turn those off, or set them up, then click again.",
            FontSize = 11 * k,
            Foreground = Draw.Brush(120, 130, 145),
            Margin = new Thickness(0, 4 * k, 0, 0)
        });

        var card = new Border
        {
            Background = Draw.Brush(255, 255, 255, 240),
            BorderBrush = Draw.Brush(185, 28, 28),
            BorderThickness = new Thickness(1.5 * k),
            CornerRadius = new CornerRadius(4 * k),
            Padding = new Thickness(11 * k, 9 * k, 13 * k, 9 * k),
            Child = stack
        };
        Canvas.SetLeft(card, x + 14 * k);
        Canvas.SetTop(card, y - 12 * k);
        BusyLayer.Children.Add(card);

        var cross = new Shapes.Ellipse
        {
            Width = 16 * k, Height = 16 * k,
            Stroke = Draw.Brush(185, 28, 28),
            StrokeThickness = 2.5 * k
        };
        Canvas.SetLeft(cross, x - 8 * k);
        Canvas.SetTop(cross, y - 8 * k);
        BusyLayer.Children.Add(cross);
    }

    void HideBusy()
    {
        BusyLayer.Children.Clear();
        _busyList = null;
        _busyProgress = null;
    }

    void CancelCapture_Click(object sender, RoutedEventArgs e) => _captureCts?.Cancel();

    async void CaptureAtGps_Click(object sender, RoutedEventArgs e)
    {
        if (_geo == null)
        {
            CaptureStatus.Text = "Add at least two GPS pins to the plan first.";
            return;
        }
        if (_svc.Location?.LastFix is not { IsFresh: true } fix)
        {
            CaptureStatus.Text = "There's no current GPS fix. Start the GPS on the Devices tab.";
            return;
        }
        if (!fix.IsGood &&
            MessageBox.Show("The GPS fix is weak, so the point may land in the wrong place. Measure anyway?",
                "Wi-Fi Surveyor", MessageBoxButton.YesNo) != MessageBoxResult.Yes)
            return;

        var (x, y) = _geo.ToPixel(fix.Lat, fix.Lon);
        if (!InsidePlan(x, y))
        {
            CaptureStatus.Text = "Your GPS position is outside the floor plan.";
            return;
        }
        await CaptureAtAsync(x, y);
    }

    bool InsidePlan(double x, double y) =>
        x >= 0 && y >= 0 && x <= _project.FloorPlanWidth && y <= _project.FloorPlanHeight;

    async void GpsTimer_Tick(object? sender, EventArgs e)
    {
        var loc = _svc.Location;
        GpsInfo.Text = loc == null
            ? "GPS is off. Start it on the Devices tab."
            : $"GPS: {GeoText.Describe(loc.LastFix)}";

        if (_geo != null && loc != null) DrawOverlay();   // live position marker

        if (AutoGps.IsChecked != true || _capturing || _geo == null) return;
        if (loc?.LastFix is not { IsFresh: true, IsGood: true } fix) return;

        var (x, y) = _geo.ToPixel(fix.Lat, fix.Lon);
        if (!InsidePlan(x, y)) return;

        double mpp = _project.MetersPerPixel ?? _geo.MetersPerPixel;
        double spacing = ParseInt(AutoSpacing.Text, 5, 1, 100);
        var here = new Point(x, y);
        if (_lastAutoPos is Point last && (here - last).Length * mpp < spacing) return;

        _lastAutoPos = here;
        await CaptureAtAsync(x, y);
    }

    // ================= Heatmap =================

    HeatmapMetric SelectedMetric =>
        (MetricBox.SelectedItem as ComboBoxItem)?.Tag is HeatmapMetric m ? m : HeatmapMetric.SignalStrength;

    MetricFilter GetFilter() => new(
        (SsidBox.SelectedItem as FilterItem)?.Value,
        (BssidBox.SelectedItem as FilterItem)?.Value,
        (BandBox.SelectedItem as ComboBoxItem)?.Content as string ?? "All",
        int.TryParse((ChannelBox.SelectedItem as FilterItem)?.Value, out var ch) ? ch : null);

    void HeatmapOption_Changed(object sender, RoutedEventArgs e)
    {
        if (_ready && !_updatingFilters) RefreshHeatmap();
    }

    void Ssid_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (!_ready || _updatingFilters) return;
        _updatingFilters = true;
        try { RebuildBssidList(); }
        finally { _updatingFilters = false; }
        RefreshHeatmap();
    }

    void Opacity_Changed(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (_ready) HeatmapImage.Opacity = OpacitySlider.Value;
    }

    void ShowPoints_Changed(object sender, RoutedEventArgs e)
    {
        if (_ready) DrawOverlay();
    }

    void RebuildFilterLists()
    {
        _updatingFilters = true;
        try
        {
            var prevSsid = (SsidBox.SelectedItem as FilterItem)?.Value;
            SsidBox.Items.Clear();
            SsidBox.Items.Add(new FilterItem("Any network (strongest)", null));
            foreach (var s in _project.Points.SelectMany(p => p.Networks).Select(n => n.Ssid)
                         .Where(s => !string.IsNullOrEmpty(s)).Distinct()
                         .OrderBy(s => s, StringComparer.OrdinalIgnoreCase))
                SsidBox.Items.Add(new FilterItem(s, s));
            SelectByValue(SsidBox, prevSsid);

            RebuildBssidList();

            var prevCh = (ChannelBox.SelectedItem as FilterItem)?.Value;
            ChannelBox.Items.Clear();
            ChannelBox.Items.Add(new FilterItem("Any", null));
            var channels = _project.Points.SelectMany(p => p.Networks).Select(n => n.Channel)
                .Concat(_project.Points.Where(p => p.Spectrum != null)
                    .SelectMany(p => p.Spectrum!.Segments)
                    .SelectMany(s => s.ChannelOccupancy.Keys))
                .Where(c => WifiChannels.Find(c) != null)
                .Distinct().OrderBy(c => c);
            foreach (var c in channels)
                ChannelBox.Items.Add(new FilterItem(c.ToString(CultureInfo.InvariantCulture),
                    c.ToString(CultureInfo.InvariantCulture)));
            SelectByValue(ChannelBox, prevCh);
        }
        finally
        {
            _updatingFilters = false;
        }
    }

    void RebuildBssidList()
    {
        var prev = (BssidBox.SelectedItem as FilterItem)?.Value;
        var ssid = (SsidBox.SelectedItem as FilterItem)?.Value;
        BssidBox.Items.Clear();
        BssidBox.Items.Add(new FilterItem("Any access point", null));
        foreach (var g in _project.Points.SelectMany(p => p.Networks)
                     .Where(n => ssid == null || n.Ssid == ssid)
                     .GroupBy(n => n.Bssid)
                     .OrderBy(g => g.First().Channel))
        {
            var n = g.First();
            var vendor = string.IsNullOrEmpty(n.Vendor) ? "" : $", {n.Vendor}";
            var name = ssid == null ? $"{(string.IsNullOrEmpty(n.Ssid) ? "(hidden)" : n.Ssid)}: " : "";
            BssidBox.Items.Add(new FilterItem($"{name}{n.Bssid} (ch {n.Channel}{vendor})", n.Bssid));
        }
        SelectByValue(BssidBox, prev);
    }

    static void SelectByValue(ComboBox box, string? value)
    {
        var match = box.Items.OfType<FilterItem>().FirstOrDefault(i => i.Value == value);
        box.SelectedItem = match ?? box.Items[0];
    }

    async void RefreshHeatmap()
    {
        if (!_ready) return;

        var metric = SelectedMetric;
        var info = MetricInfo.Get(metric);
        var filter = GetFilter();
        var samples = _project.Points
            .Select(p => (p, v: MetricExtractor.Value(p, metric, filter, _ctx)))
            .Where(t => t.v.HasValue)
            .Select(t => new HeatSample(t.p.X, t.p.Y, t.v!.Value))
            .ToList();

        _range = info.AutoRange ? AutoRange(samples) : (info.Min, info.Max);
        UpdateLegend(info);

        if (metric is HeatmapMetric.ChannelPower or HeatmapMetric.ChannelOccupancy && filter.Channel == null)
            HeatmapHint.Text = "Choose a channel to see this map.";
        else if (samples.Count == 0 && _project.Points.Count > 0)
            HeatmapHint.Text = $"No data for this map yet. It needs {info.Needs}.";
        else if (_project.MetersPerPixel == null && _project.Points.Count > 0)
            HeatmapHint.Text = "Scale isn't set, so the fill distance is a guess.";
        else
            HeatmapHint.Text = "";

        _renderCts?.Cancel();
        if (samples.Count == 0)
        {
            HeatmapImage.Source = null;
            DrawOverlay();
            return;
        }

        var cts = _renderCts = new CancellationTokenSource();
        int w = _project.FloorPlanWidth, h = _project.FloorPlanHeight;
        double radiusMeters = TryParseNumber(RadiusBox.Text, out var r) && r > 0 ? r : 8;
        double radiusPx = _project.MetersPerPixel is double mpp ? radiusMeters / mpp : Math.Max(w, h) * 0.12;
        var (min, max) = _range;

        try
        {
            var result = await Task.Run(() => info.Categorical
                ? HeatmapRenderer.ComputeCategorical(w, h, samples, radiusPx, cts.Token)
                : HeatmapRenderer.Compute(w, h, samples, min, max, info.HigherIsBetter, radiusPx, cts.Token), cts.Token);
            if (cts.IsCancellationRequested) return;
            HeatmapImage.Source = HeatmapRenderer.ToBitmap(result);
            HeatmapImage.Width = result.Width * result.Cell;
            HeatmapImage.Height = result.Height * result.Cell;
            HeatmapImage.Opacity = OpacitySlider.Value;
        }
        catch (OperationCanceledException)
        {
            return;
        }
        DrawOverlay();
    }

    static (double, double) AutoRange(List<HeatSample> samples)
    {
        if (samples.Count == 0) return (0, 1);
        double lo = samples.Min(s => s.Value), hi = samples.Max(s => s.Value);
        if (hi - lo < 1) hi = lo + 1;
        return (Math.Min(0, lo), hi);
    }

    void UpdateLegend(MetricInfo info)
    {
        LegendTitle.Text = info.Title;
        LegendGradient.Visibility = info.Categorical ? Visibility.Collapsed : Visibility.Visible;
        LegendCategories.Visibility = info.Categorical ? Visibility.Visible : Visibility.Collapsed;

        if (info.Categorical)
        {
            LegendCategories.ItemsSource = Verdicts.All.Select(v => new
            {
                Swatch = (Brush)new SolidColorBrush(Color.FromRgb(v.R, v.G, v.B)),
                Text = v.Label
            }).ToList();
            return;
        }
        LegendBar.Background = HeatmapRenderer.LegendBrush(info.HigherIsBetter);
        LegendMin.Text = $"{_range.Min:0.#} {info.Unit}";
        LegendMax.Text = $"{_range.Max:0.#} {info.Unit}";
    }

    double Normalize(double v, bool higherIsBetter)
    {
        double span = _range.Max - _range.Min;
        double t = span <= 0 ? 0 : (v - _range.Min) / span;
        return higherIsBetter ? t : 1 - t;
    }

    // ================= Overlay drawing =================

    void DrawOverlay()
    {
        Overlay.Children.Clear();
        double k = _markerScaleOverride ?? 1 / Math.Max(Zoom, 0.05);   // keep markers the same on-screen size

        if (ShowPoints.IsChecked == true)
        {
            var metric = SelectedMetric;
            var info = MetricInfo.Get(metric);
            var filter = GetFilter();
            int index = 0;
            foreach (var p in _project.Points)
            {
                index++;
                var v = MetricExtractor.Value(p, metric, filter, _ctx);
                bool selected = ReferenceEquals(p, _selected);
                Brush fill = v is double d
                    ? new SolidColorBrush(HeatmapRenderer.ToColor(info.Categorical
                        ? HeatmapRenderer.VerdictColor(d, 255)
                        : HeatmapRenderer.ColorFor(Normalize(d, info.HigherIsBetter), 255)))
                    : Brushes.LightGray;
                var dot = new Shapes.Ellipse
                {
                    Width = (selected ? 16 : 11) * k,
                    Height = (selected ? 16 : 11) * k,
                    Fill = fill,
                    Stroke = selected ? Brushes.Black : Brushes.White,
                    StrokeThickness = (selected ? 2.5 : 1.5) * k,
                    ToolTip = $"Point {index}: " + (v is double dv
                                  ? info.Categorical
                                      ? Verdicts.Label((Verdict)(int)dv)
                                      : $"{dv:0.#} {info.Unit}"
                                  : "no data") +
                              $"\n{p.Networks.Count} access points, {p.Time:t}"
                };
                Place(dot, p.X, p.Y);
            }
        }

        if (ShowGaps.IsChecked == true && _project.Points.Count > 0)
        {
            double reach = _project.MetersPerPixel is double mpp && TryParseNumber(RadiusBox.Text, out var metres)
                ? metres / mpp
                : Math.Max(_project.FloorPlanWidth, _project.FloorPlanHeight) * 0.12;
            double cell = Math.Max(reach, 24);

            for (double gx = 0; gx < _project.FloorPlanWidth; gx += cell)
            for (double gy = 0; gy < _project.FloorPlanHeight; gy += cell)
            {
                double cx = gx + cell / 2, cy = gy + cell / 2;
                bool covered = _project.Points.Any(p =>
                    (p.X - cx) * (p.X - cx) + (p.Y - cy) * (p.Y - cy) <= reach * reach);
                if (covered) continue;

                var patch = new Shapes.Rectangle
                {
                    Width = Math.Min(cell, _project.FloorPlanWidth - gx),
                    Height = Math.Min(cell, _project.FloorPlanHeight - gy),
                    Fill = Draw.Brush(71, 85, 105, 30),
                    Stroke = Draw.Brush(71, 85, 105, 60),
                    StrokeThickness = 1 * k,
                    StrokeDashArray = new DoubleCollection { 3, 3 },
                    ToolTip = "Nothing measured within reach of here"
                };
                Canvas.SetLeft(patch, gx);
                Canvas.SetTop(patch, gy);
                Overlay.Children.Add(patch);
            }
        }

        if (ShowWalk.IsChecked == true && (_project.Track.Count > 0 || _project.Issues.Count > 0))
        {
            if (_project.Track.Count > 1)
                Draw.Poly(Overlay, _project.Track.Select(t => new Point(t.X, t.Y)),
                    Draw.WithAlpha(WalkBrush, 150), 2 * k);
            foreach (var mark in _project.Track)
                Place(new Shapes.Ellipse
                {
                    Width = 6 * k, Height = 6 * k,
                    Fill = WalkBrush,
                    ToolTip = $"Position mark, {mark.Time:T}"
                }, mark.X, mark.Y);

            double clusterRadius = _project.MetersPerPixel is double mpp ? 3 / mpp : 25;
            foreach (var cluster in StabilityAnalysis.Cluster(_project, clusterRadius))
            {
                var (r, g, b) = StabilityAnalysis.Color(cluster.Worst);
                var brush = Draw.Brush(r, g, b);
                double size = Math.Min(46, 16 + 4 * cluster.Count) * k;
                Place(new Shapes.Ellipse
                {
                    Width = size, Height = size,
                    Fill = Draw.WithAlpha(brush, 60),
                    Stroke = brush,
                    StrokeThickness = 2 * k,
                    ToolTip = $"{cluster.Count} issue(s) here: {cluster.Summary}"
                }, cluster.X, cluster.Y);
                var label = Draw.Text(Overlay, cluster.Count.ToString(), cluster.X, cluster.Y - 7 * k,
                    brush, 12 * k, center: true);
                label.FontWeight = FontWeights.SemiBold;
            }
        }

        if (_pendingCapture is Point pc)
        {
            var ring = new Shapes.Ellipse
            {
                Width = 22 * k, Height = 22 * k,
                Stroke = PendingBrush, StrokeThickness = 2.5 * k,
                StrokeDashArray = new DoubleCollection { 2, 2 }
            };
            Place(ring, pc.X, pc.Y);
        }

        foreach (var c in _calibClicks) Cross(c, PendingBrush, k);
        if (_calibClicks.Count == 2)
        {
            Draw.Line(Overlay, _calibClicks[0].X, _calibClicks[0].Y, _calibClicks[1].X, _calibClicks[1].Y,
                PendingBrush, 2 * k);
            var mid = new Point((_calibClicks[0].X + _calibClicks[1].X) / 2, (_calibClicks[0].Y + _calibClicks[1].Y) / 2);
            Draw.Text(Overlay, $"{(_calibClicks[1] - _calibClicks[0]).Length:0} px", mid.X + 6 * k, mid.Y, PendingBrush, 12 * k);
        }

        foreach (var b in _project.Barriers)
        {
            bool selected = ReferenceEquals(b, _selectedWall);
            var brush = b.Suggested ? SuggestedWallBrush : b.Inferred ? InferredWallBrush : WallBrush;
            var line = Draw.Line(Overlay, b.X1, b.Y1, b.X2, b.Y2, brush,
                Math.Max((selected ? 5 : 3) * k, 2), dashed: b.Suggested);
            if (b.Inferred) line.StrokeDashArray = new DoubleCollection { 6, 2 };
            line.StrokeStartLineCap = PenLineCap.Round;
            line.StrokeEndLineCap = PenLineCap.Round;
            line.Opacity = b.Suggested ? 0.9 : 0.75;
            line.ToolTip = $"{b.Material}, about {b.Db:0} dB each way" +
                           (b.Suggested ? "\nFound in the plan image; keep it to use it"
                               : b.Inferred ? "\nGuessed from the measurements" : "");

            if (!selected) continue;
            foreach (var end in new[] { (b.X1, b.Y1), (b.X2, b.Y2) })
                Place(new Shapes.Rectangle
                {
                    Width = 10 * k, Height = 10 * k,
                    Fill = Brushes.White, Stroke = brush, StrokeThickness = 2 * k
                }, end.Item1, end.Item2);
        }
        foreach (var c in _wallClicks) Cross(c, WallBrush, k);

        foreach (var e in _estimates) DrawEstimate(e, k);

        foreach (var a in _project.GeoRef.Anchors) Pin(new Point(a.X, a.Y), AnchorBrush, k);
        if (_pendingAnchor is Point pa) Pin(pa, PendingBrush, k);

        if (_geo != null && _svc.Location?.LastFix is { IsFresh: true } fix)
        {
            var (x, y) = _geo.ToPixel(fix.Lat, fix.Lon);
            if (InsidePlan(x, y))
            {
                double mpp = _project.MetersPerPixel ?? _geo.MetersPerPixel;
                double errM = fix.AccuracyM ?? (fix.Hdop ?? 5) * 2.5;   // rough 1-sigma for standalone GPS
                double ringPx = Math.Max(errM / mpp, 10 * k);
                Place(new Shapes.Ellipse
                {
                    Width = ringPx * 2, Height = ringPx * 2,
                    Fill = Draw.WithAlpha(GpsBrush, 40), Stroke = GpsBrush, StrokeThickness = 1 * k
                }, x, y);
                Place(new Shapes.Ellipse
                {
                    Width = 10 * k, Height = 10 * k, Fill = GpsBrush,
                    Stroke = Brushes.White, StrokeThickness = 2 * k,
                    ToolTip = "Your GPS position"
                }, x, y);
            }
        }
    }

    void DrawEstimate(TransmitterEstimate e, double k)
    {
        var brush = e.Kind == "interference" ? InterferenceBrush : ApEstimateBrush;
        double radius = Math.Max(e.RadiusPx, 12 * k);

        // The circle is the honest part: it's where the source plausibly is, not a pinpoint.
        var area = new Shapes.Ellipse
        {
            Width = radius * 2,
            Height = radius * 2,
            Fill = Draw.WithAlpha(brush, 26),
            Stroke = brush,
            StrokeThickness = 1.5 * k,
            StrokeDashArray = new DoubleCollection { 4, 3 },
            ToolTip = $"{e.Label}\n{e.Note}"
        };
        Place(area, e.X, e.Y);

        double arm = 11 * k;
        Draw.Line(Overlay, e.X - arm, e.Y, e.X + arm, e.Y, brush, 2.5 * k);
        Draw.Line(Overlay, e.X, e.Y - arm, e.X, e.Y + arm, brush, 2.5 * k);
        var centre = new Shapes.Ellipse
        {
            Width = 12 * k, Height = 12 * k,
            Fill = e.LowConfidence ? Brushes.White : brush,
            Stroke = brush,
            StrokeThickness = 2.5 * k,
            ToolTip = $"{e.Label}\n{e.Note}"
        };
        Place(centre, e.X, e.Y);

        var label = Draw.Text(Overlay, e.LowConfidence ? e.Label + " (rough)" : e.Label,
            e.X + 14 * k, e.Y - 8 * k, brush, 12 * k);
        label.FontWeight = FontWeights.SemiBold;
    }

    void Place(FrameworkElement el, double x, double y)
    {
        Canvas.SetLeft(el, x - el.Width / 2);
        Canvas.SetTop(el, y - el.Height / 2);
        Overlay.Children.Add(el);
    }

    void Cross(Point p, Brush b, double k)
    {
        Draw.Line(Overlay, p.X - 7 * k, p.Y, p.X + 7 * k, p.Y, b, 2 * k);
        Draw.Line(Overlay, p.X, p.Y - 7 * k, p.X, p.Y + 7 * k, b, 2 * k);
    }

    void Pin(Point p, Brush b, double k)
    {
        Overlay.Children.Add(new Shapes.Polygon
        {
            Points = new PointCollection
            {
                new Point(p.X, p.Y),
                new Point(p.X - 6 * k, p.Y - 14 * k),
                new Point(p.X + 6 * k, p.Y - 14 * k)
            },
            Fill = b,
            Stroke = Brushes.White,
            StrokeThickness = 1 * k
        });
    }

    void Estimates_Changed(object sender, RoutedEventArgs e) => RefreshEstimates();

    void InvalidateEstimates()
    {
        _estimates = new List<TransmitterEstimate>();
        if (_ready) RefreshEstimates();
    }

    async void RefreshEstimates()
    {
        if (!_ready) return;
        bool wantAps = ShowApEstimates.IsChecked == true;
        bool wantInterference = ShowInterferenceEstimate.IsChecked == true;
        if (!wantAps && !wantInterference)
        {
            _estimates = new List<TransmitterEstimate>();
            _noise = new List<NoiseSource>();
            EstimateInfo.Text = "";
            DrawOverlay();
            return;
        }

        var filter = GetFilter();
        var project = _project;
        var ctx = _ctx;
        EstimateInfo.Text = "Fitting…";

        List<NoiseSource> noise = new();
        var results = await Task.Run(() =>
        {
            var found = new List<TransmitterEstimate>();
            if (wantAps)
                foreach (var (bssid, ssid, _) in Localizer.Candidates(project, filter.Ssid, filter.Bssid))
                {
                    var label = string.IsNullOrEmpty(ssid) ? bssid : $"{ssid} ({bssid[^5..]})";
                    if (Localizer.Estimate(project, Localizer.ApSamples(project, bssid), label, "ap") is { } e)
                        found.Add(e);
                }
            if (wantInterference)
            {
                noise = NoiseFinder.Find(project, ctx);
                foreach (var source in noise)
                    if (source.Estimate is { } e)
                        found.Add(e);
            }
            return found;
        });

        _estimates = results;
        _noise = noise;
        EstimateInfo.Text = BuildEstimateText(wantInterference);
        DrawOverlay();
    }

    string BuildEstimateText(bool wantInterference)
    {
        var sections = new List<string>();
        if (wantInterference) sections.Add(NoiseFinder.Summarize(_noise, _project));

        if (_estimates.Count == 0)
        {
            sections.Add("Not enough spread-out measurements to place a source yet. " +
                         "Measure from several sides of it.");
            return string.Join("\n\n", sections);
        }

        var lines = new List<string>();
        foreach (var e in _estimates)
        {
            double? metres = _project.MetersPerPixel is double mpp ? e.RadiusPx * mpp : null;
            lines.Add($"{e.Label}: within about {(metres is double m ? $"{m:0.#} m" : $"{e.RadiusPx:0} px")}" +
                      (e.LowConfidence ? " (rough)" : "") + $". {e.Note}");
        }
        sections.Add(string.Join("\n\n", lines));
        return string.Join("\n\n", sections);
    }

    // ================= Walk test =================

    void Walk_Click(object sender, RoutedEventArgs e)
    {
        if (_walk is { Running: true })
        {
            _walk.Stop();
            WalkButton.Content = "Start the run";
            MarkChanged();
            UpdateWalkInfo();
            DrawOverlay();
            return;
        }

        if (_walk == null)
        {
            _walk = new StabilityRecorder(_project, CurrentGpsPixel);
            _walk.Updated += () => Dispatcher.InvokeAsync(() =>
            {
                UpdateWalkInfo();
                DrawOverlay();
            });
        }

        var clients = new List<ClientTarget>();
        if (WalkThroughput.IsChecked == true)
        {
            var settings = _svc.Settings;
            foreach (var iface in WlanConnection.ListInterfaces())
            {
                var address = iface.LocalIPv4();
                if (address == null || string.IsNullOrWhiteSpace(settings.IperfServer)) continue;
                clients.Add(new ClientTarget(iface.Description, address,
                    new[] { (settings.IperfServer.Trim(), settings.IperfPort) }));
            }
            if (clients.Count == 0)
                WalkInfo.Text = "No adapter has both an address and a server, so speed tests are off for this run.";
        }

        _walk.Start(new WalkOptions(clients.Count > 0, ParseInt(WalkSeconds.Text, 3, 1, 30), clients));
        WalkButton.Content = "Stop the run";
        UpdateWalkInfo();
    }

    (double X, double Y)? CurrentGpsPixel()
    {
        if (_geo == null || _svc.Location?.LastFix is not { IsFresh: true, IsGood: true } fix) return null;
        var (x, y) = _geo.ToPixel(fix.Lat, fix.Lon);
        return InsidePlan(x, y) ? (x, y) : null;
    }

    void UpdateWalkInfo()
    {
        int issues = _project.Issues.Count;
        int marks = _project.Track.Count;
        var running = _walk is { Running: true };
        var waiting = _walk?.PendingCount ?? 0;

        WalkInfo.Text = (running ? "Recording. " : "") +
                        $"{marks} position mark(s), {issues} issue(s) logged." +
                        (waiting > 0 ? $" {waiting} waiting for your next click to be placed." : "") +
                        (running && _walk!.LastThroughput.Length > 0 ? $"\n{_walk.LastThroughput}" : "");
    }

    void ClearWalk_Click(object sender, RoutedEventArgs e)
    {
        var track = _project.Track.ToList();
        var issues = _project.Issues.ToList();
        PushUndo($"cleared {issues.Count} walk issue(s)", () =>
        {
            _project.Track.AddRange(track);
            _project.Issues.AddRange(issues);
        });
        _project.Track.Clear();
        _project.Issues.Clear();
        MarkChanged();
        UpdateWalkInfo();
        DrawOverlay();
    }

    void Analysis_Click(object sender, RoutedEventArgs e)
    {
        if (_project.Points.Count == 0)
        {
            AnalysisHint.Text = "Measure a few points first.";
            return;
        }
        AnalysisHint.Text = "";
        new AnalysisWindow(_project, GetFilter(), _ctx, _estimates, _geo) { Owner = Window.GetWindow(this) }.Show();
    }

    // ================= Export =================

    void ExportCsv_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog { Filter = "CSV (*.csv)|*.csv", FileName = _project.Name + ".csv" };
        if (dlg.ShowDialog() != true) return;
        try
        {
            Exporters.WriteCsv(dlg.FileName, _project, _geo);
            CaptureStatus.Text = "CSV exported.";
        }
        catch (Exception ex) { MessageBox.Show("Export failed: " + ex.Message, "Wi-Fi Surveyor"); }
    }

    void ExportKml_Click(object sender, RoutedEventArgs e)
    {
        if (_geo == null && !_project.Points.Any(p => p.Gps != null))
        {
            MessageBox.Show("KML needs GPS positions. Pin the plan to GPS, or measure with a GPS running.",
                "Wi-Fi Surveyor");
            return;
        }
        var dlg = new SaveFileDialog { Filter = "KML (*.kml)|*.kml", FileName = _project.Name + ".kml" };
        if (dlg.ShowDialog() != true) return;
        try
        {
            var metric = SelectedMetric;
            var info = MetricInfo.Get(metric);
            var filter = GetFilter();
            Exporters.WriteKml(dlg.FileName, _project, _geo,
                p => MetricExtractor.Value(p, metric, filter, _ctx), info.Title, info.Unit);
            CaptureStatus.Text = "KML exported. Open it in Google Earth or QGIS.";
        }
        catch (Exception ex) { MessageBox.Show("Export failed: " + ex.Message, "Wi-Fi Surveyor"); }
    }

    void ExportReport_Click(object sender, RoutedEventArgs e)
    {
        if (_project.Points.Count == 0)
        {
            CaptureStatus.Text = "Measure a few points first.";
            return;
        }
        var dlg = new SaveFileDialog { Filter = "Web page (*.html)|*.html", FileName = _project.Name + ".html" };
        if (dlg.ShowDialog() != true) return;

        try
        {
            byte[]? map = null;
            if (RenderMap() is { } bitmap)
            {
                var encoder = new PngBitmapEncoder();
                encoder.Frames.Add(BitmapFrame.Create(bitmap));
                using var stream = new MemoryStream();
                encoder.Save(stream);
                map = stream.ToArray();
            }

            ReportBuilder.Write(dlg.FileName, _project, _ctx, GetFilter(), MetricInfo.Get(SelectedMetric),
                map, _estimates, _noise, _geo);
            CaptureStatus.Text = "Report written. Opening it…";
            Services.Installer.OpenUrl(dlg.FileName);
        }
        catch (Exception ex)
        {
            MessageBox.Show("Couldn't write the report: " + ex.Message, "Wi-Fi Surveyor");
        }
    }

    void ExportPng_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog { Filter = "PNG image (*.png)|*.png", FileName = _project.Name + ".png" };
        if (dlg.ShowDialog() != true) return;
        try
        {
            if (RenderMap() is not { } rendered) return;
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(rendered));
            using var file = File.Create(dlg.FileName);
            encoder.Save(file);
            CaptureStatus.Text = "Image exported.";
        }
        catch (Exception ex) { MessageBox.Show("Export failed: " + ex.Message, "Wi-Fi Surveyor"); }
    }

    /// <summary>Draws the plan, heatmap, overlay, and legend into one bitmap.</summary>
    RenderTargetBitmap? RenderMap()
    {
        try
        {
            int w = _project.FloorPlanWidth, h = _project.FloorPlanHeight;
            const int legendH = 44;
            _markerScaleOverride = Math.Max(1, Math.Max(w, h) / 1600.0);
            DrawOverlay();
            Overlay.UpdateLayout();
            var visual = new DrawingVisual();
            using (var dc = visual.RenderOpen())
            {
                dc.DrawRectangle(Brushes.White, null, new Rect(0, 0, w, h + legendH));
                if (PlanImage.Source != null) dc.DrawImage(PlanImage.Source, new Rect(0, 0, w, h));
                if (HeatmapImage.Source != null)
                {
                    dc.PushOpacity(HeatmapImage.Opacity);
                    dc.DrawImage(HeatmapImage.Source, new Rect(0, 0, HeatmapImage.Width, HeatmapImage.Height));
                    dc.Pop();
                }
                dc.DrawRectangle(new VisualBrush(Overlay)
                {
                    Viewbox = new Rect(0, 0, w, h),
                    ViewboxUnits = BrushMappingMode.Absolute,
                    Stretch = Stretch.Fill
                }, null, new Rect(0, 0, w, h));

                var typeface = new Typeface("Segoe UI");
                double dpi = VisualTreeHelper.GetDpi(this).PixelsPerDip;
                FormattedText Label(string text) => new(text, CultureInfo.CurrentCulture,
                    FlowDirection.LeftToRight, typeface, 13, Brushes.Black, dpi);

                if (MetricInfo.Get(SelectedMetric).Categorical)
                {
                    var title = Label(LegendTitle.Text);
                    dc.DrawText(title, new Point(10, h + 12));
                    double x = 20 + title.Width;
                    foreach (var v in Verdicts.All)
                    {
                        var swatch = new SolidColorBrush(Color.FromRgb(v.R, v.G, v.B));
                        dc.DrawRectangle(swatch, null, new Rect(x, h + 15, 12, 12));
                        var text = Label(v.Label);
                        dc.DrawText(text, new Point(x + 17, h + 12));
                        x += 17 + text.Width + 14;
                    }
                }
                else
                {
                    var title = Label($"{LegendTitle.Text}   {LegendMin.Text}");
                    dc.DrawText(title, new Point(10, h + 12));
                    double barX = 20 + title.Width;
                    dc.DrawRectangle(LegendBar.Background, null, new Rect(barX, h + 14, 240, 14));
                    dc.DrawText(Label(LegendMax.Text), new Point(barX + 250, h + 12));
                }
            }
            var bitmap = new RenderTargetBitmap(w, h + legendH, 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(visual);
            bitmap.Freeze();
            return bitmap;
        }
        catch (Exception ex)
        {
            MessageBox.Show("Couldn't draw the map: " + ex.Message, "Wi-Fi Surveyor");
            return null;
        }
        finally
        {
            _markerScaleOverride = null;
            DrawOverlay();
        }
    }

    // ================= Helpers =================

    static bool TryParseNumber(string text, out double value) =>
        double.TryParse(text.Trim(), NumberStyles.Float, CultureInfo.CurrentCulture, out value) ||
        double.TryParse(text.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out value);

    static int ParseInt(string text, int fallback, int min, int max) =>
        int.TryParse(text.Trim(), out var v) ? Math.Clamp(v, min, max) : fallback;
}
