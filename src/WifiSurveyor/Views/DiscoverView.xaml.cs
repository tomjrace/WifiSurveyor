using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Media;
using System.Windows.Threading;
using WifiSurveyor.Models;
using WifiSurveyor.Services;
using WifiSurveyor.Wifi;
using Shapes = System.Windows.Shapes;

namespace WifiSurveyor.Views;

public partial class DiscoverView : UserControl
{
    static readonly TimeSpan HistoryWindow = TimeSpan.FromMinutes(5);
    static readonly TimeSpan HideAfter = TimeSpan.FromMinutes(2);

    readonly ObservableCollection<NetworkRow> _rows = new();
    readonly Dictionary<string, NetworkRow> _byBssid = new();
    readonly Dictionary<string, List<(DateTime T, int Rssi)>> _history = new();
    readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(5) };
    readonly ICollectionView _view;
    bool _scanning, _ready;

    public DiscoverView()
    {
        InitializeComponent();
        NetworkGrid.ItemsSource = _rows;
        _view = CollectionViewSource.GetDefaultView(_rows);
        _view.Filter = o => o is NetworkRow r && RowVisible(r);
        if (_view is ICollectionViewLiveShaping live)
        {
            live.IsLiveFiltering = true;
            live.LiveFilteringProperties.Add(nameof(NetworkRow.Stale));
            live.IsLiveSorting = true;
            foreach (var p in new[] { nameof(NetworkRow.Rssi), nameof(NetworkRow.Ssid), nameof(NetworkRow.Channel) })
                live.LiveSortingProperties.Add(p);
        }

        MergeAdapters.IsChecked = AppServices.Instance.Settings.MergeAdapterScans;
        _timer.Tick += async (_, _) => await ScanAsync();
        Loaded += async (_, _) =>
        {
            if (AutoRefresh.IsChecked == true) _timer.Start();
            await ScanAsync();
        };
        Unloaded += (_, _) => _timer.Stop();
        _ready = true;
    }

    async void ScanButton_Click(object sender, RoutedEventArgs e) => await ScanAsync();

    async void Merge_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        var settings = AppServices.Instance.Settings;
        settings.MergeAdapterScans = MergeAdapters.IsChecked == true;
        settings.Save();

        // Rows from the previous mode are stale either way, so start clean.
        _rows.Clear();
        _byBssid.Clear();
        await ScanAsync();
    }

    void AutoRefresh_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        if (AutoRefresh.IsChecked == true) _timer.Start(); else _timer.Stop();
    }

    void Filter_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        _view.Refresh();
        Redraw();
    }

    void NetworkGrid_SelectionChanged(object sender, SelectionChangedEventArgs e) => DrawHistory();
    void Chart_SizeChanged(object sender, SizeChangedEventArgs e) => Redraw();

    bool RowVisible(NetworkRow r)
    {
        if (r.Stale) return false;
        var band = (BandFilter.SelectedItem as ComboBoxItem)?.Content as string ?? "All";
        if (band != "All" && r.Band != band) return false;
        var q = SearchBox.Text.Trim();
        return q.Length == 0
               || r.Ssid.Contains(q, StringComparison.OrdinalIgnoreCase)
               || r.Bssid.Contains(q, StringComparison.OrdinalIgnoreCase)
               || r.Vendor.Contains(q, StringComparison.OrdinalIgnoreCase);
    }

    async Task ScanAsync()
    {
        if (_scanning) return;
        _scanning = true;
        ScanButton.IsEnabled = false;
        ScanStatus.Text = "Scanning…";
        try
        {
            bool merge = MergeAdapters.IsChecked == true;
            var results = await Task.Run(() => WifiScanner.ScanAsync(merge));
            AppServices.Instance.LastScan = results;
            var now = DateTime.Now;

            foreach (var r in results)
            {
                if (!_byBssid.TryGetValue(r.Bssid, out var row))
                {
                    row = new NetworkRow(r.Bssid);
                    _byBssid[r.Bssid] = row;
                    _rows.Add(row);
                }
                row.Update(r, now);

                if (!_history.TryGetValue(r.Bssid, out var h)) _history[r.Bssid] = h = new();
                h.Add((now, r.Rssi));
            }
            foreach (var h in _history.Values) h.RemoveAll(p => now - p.T > HistoryWindow);
            foreach (var row in _rows) row.Stale = now - row.LastSeen > HideAfter;

            int radios = WifiScanner.LastRadioCount;
            var merged = merge && WifiScanner.LastRawCount > results.Count
                ? $", merged from {radios} radios"
                : "";
            ScanStatus.Text = results.Count == 0
                ? "No networks found. Turn on location access for desktop apps, then scan again."
                : $"{results.Count} access points (BSSIDs){merged}, updated {now:T}";
            Redraw();
        }
        catch (Exception ex)
        {
            ScanStatus.Text = "Scan failed: " + ex.Message;
        }
        finally
        {
            _scanning = false;
            ScanButton.IsEnabled = true;
        }
    }

    void Redraw()
    {
        DrawHistory();
        var visible = _rows.Where(RowVisible).Select(r => r.Latest).OfType<WifiReading>().ToList();
        DrawChannelMap(Channels24Canvas, visible, 2400, 2495, WifiChannels.Band24, "2.4 GHz");
        DrawChannelMap(Channels5Canvas, visible, 5160, 5895, WifiChannels.Band5, "5 GHz");
        DrawChannelMap(Channels6Canvas, visible, 5945, 7125, WifiChannels.Band6, "6 GHz");
    }

    void DrawHistory()
    {
        var c = HistoryCanvas;
        c.Children.Clear();
        if (c.ActualWidth < 80 || c.ActualHeight < 60) return;

        var now = DateTime.Now;
        var plot = new PlotArea(c, -HistoryWindow.TotalSeconds, 0, -95, -25);
        Draw.YGrid(c, plot, 10);
        for (int s = -300; s <= 0; s += 60)
            Draw.Text(c, s == 0 ? "now" : $"{s / 60} min", plot.X(s), plot.Rect.Bottom + 4, center: true);

        var rows = NetworkGrid.SelectedItems.Cast<NetworkRow>().Take(8).ToList();
        bool auto = rows.Count == 0;
        if (auto) rows = _rows.Where(RowVisible).OrderByDescending(r => r.Rssi).Take(5).ToList();

        int i = 0;
        foreach (var row in rows)
        {
            if (!_history.TryGetValue(row.Bssid, out var h) || h.Count == 0) continue;
            var brush = Draw.Series(i);
            Draw.Poly(c, h.Select(q => new Point(
                plot.X(-(now - q.T).TotalSeconds),
                plot.Y(Math.Clamp(q.Rssi, -95, -25)))), brush, 2);
            Draw.Text(c, $"{row.Ssid}  {row.Bssid}  {h[^1].Rssi} dBm",
                plot.Rect.Left + 8, plot.Rect.Top + 2 + i * 15, brush);
            i++;
        }
        Draw.Text(c, auto ? "Strongest 5 shown. Select rows to compare specific access points." : "",
            plot.Rect.Right, 1, size: 11, right: true);
    }

    static void DrawChannelMap(Canvas c, List<WifiReading> nets, double fMin, double fMax,
        IEnumerable<WifiChannels.WifiChannel> channels, string title)
    {
        c.Children.Clear();
        if (c.ActualWidth < 80 || c.ActualHeight < 60) return;

        var plot = new PlotArea(c, fMin, fMax, -95, -25);
        Draw.YGrid(c, plot, 10);
        Draw.Text(c, title, plot.Rect.Right, 1, Brushes.Black, 12, right: true);

        double lastLabel = double.NegativeInfinity;
        foreach (var ch in channels)
        {
            double x = plot.X(ch.CenterMHz);
            if (x < plot.Rect.Left || x > plot.Rect.Right || x - lastLabel < 20) continue;
            Draw.Text(c, ch.Number.ToString(), x, plot.Rect.Bottom + 4, size: 10, center: true);
            lastLabel = x;
        }

        // Weakest first so strong networks draw on top. Width is drawn as 20 MHz;
        // the real channel width needs beacon IE parsing (see README).
        foreach (var n in nets.Where(n => n.FrequencyMHz >= fMin && n.FrequencyMHz <= fMax).OrderBy(n => n.Rssi))
        {
            var brush = Draw.SeriesFor(n.Ssid + n.Bssid);
            double f = n.FrequencyMHz, top = plot.Y(Math.Clamp(n.Rssi, -95, -25)), bottom = plot.Y(-95);
            c.Children.Add(new Shapes.Polygon
            {
                Points = new PointCollection
                {
                    new Point(plot.X(f - 10), bottom), new Point(plot.X(f - 7), top),
                    new Point(plot.X(f + 7), top), new Point(plot.X(f + 10), bottom)
                },
                Stroke = brush,
                StrokeThickness = 1.5,
                Fill = Draw.WithAlpha(brush, 28),
                ToolTip = $"{n.Ssid}\n{n.Bssid}\nChannel {n.Channel}, {n.Rssi} dBm"
            });
            if (n.Rssi > -80)
                Draw.Text(c, string.IsNullOrEmpty(n.Ssid) ? "(hidden)" : n.Ssid,
                    plot.X(f), top - 15, brush, 10, center: true);
        }
    }
}

public sealed class NetworkRow : INotifyPropertyChanged
{
    string _ssid = "", _vendor = "", _band = "", _security = "";
    int _channel, _rssi, _min, _max, _quality;
    DateTime _lastSeen;
    bool _seen, _stale;

    public NetworkRow(string bssid) => Bssid = bssid;

    public string Bssid { get; }
    public WifiReading? Latest { get; private set; }
    public string Ssid { get => _ssid; private set => Set(ref _ssid, value); }
    public string Vendor { get => _vendor; private set => Set(ref _vendor, value); }
    public string Band { get => _band; private set => Set(ref _band, value); }
    public string Security { get => _security; private set => Set(ref _security, value); }
    public int Channel { get => _channel; private set => Set(ref _channel, value); }
    public int Rssi { get => _rssi; private set => Set(ref _rssi, value); }
    public int MinRssi { get => _min; private set => Set(ref _min, value); }
    public int MaxRssi { get => _max; private set => Set(ref _max, value); }
    public int LinkQuality { get => _quality; private set => Set(ref _quality, value); }
    public DateTime LastSeen { get => _lastSeen; private set => Set(ref _lastSeen, value); }

    /// <summary>Not seen recently; hidden from the list.</summary>
    public bool Stale { get => _stale; set => Set(ref _stale, value); }

    public void Update(WifiReading r, DateTime now)
    {
        Latest = r;
        Ssid = string.IsNullOrEmpty(r.Ssid) ? "(hidden)" : r.Ssid;
        Vendor = r.Vendor;
        Band = WifiChannels.BandLabel(r.FrequencyMHz);
        Channel = r.Channel;
        Rssi = r.Rssi;
        MinRssi = _seen ? Math.Min(MinRssi, r.Rssi) : r.Rssi;
        MaxRssi = _seen ? Math.Max(MaxRssi, r.Rssi) : r.Rssi;
        LinkQuality = r.LinkQuality;
        Security = r.Secured switch { true => "Secured", false => "Open", null => "Unknown" };
        LastSeen = now;
        _seen = true;
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    void Set<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return;
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}
