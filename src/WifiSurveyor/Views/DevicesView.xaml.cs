using System.Diagnostics;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using WifiSurveyor.Positioning;
using WifiSurveyor.RF;
using WifiSurveyor.Services;
using WifiSurveyor.Surveying;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Views;

public partial class DevicesView : UserControl
{
    readonly AppServices _svc = AppServices.Instance;
    readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(1) };
    string? _gpsMessage;

    public DevicesView()
    {
        InitializeComponent();
        var s = _svc.Settings;
        IperfPath.Text = s.IperfPath;
        if (ToolPaths.BundledIperf3 is { } bundled && s.IperfPath == bundled)
            IperfResult.Text = "Using the copy of iperf3 that ships with this build.";
        IperfServer.Text = s.IperfServer;
        IperfSeconds.Text = s.IperfSeconds.ToString(CultureInfo.CurrentCulture);
        IperfPort.Text = s.IperfPort.ToString(CultureInfo.CurrentCulture);
        _ = LoadServersAsync(force: false);
        SelectContent(RfeBaud, s.RfeBaud.ToString(CultureInfo.InvariantCulture));
        SelectContent(GpsBaud, s.GpsBaud.ToString(CultureInfo.InvariantCulture));
        RefreshPorts();
        OuiInfo.Text = OuiLookup.Describe();

        _timer.Tick += (_, _) => UpdateFix();
        _timer.Start();
    }

    static void SelectContent(ComboBox box, string content)
    {
        foreach (var item in box.Items.OfType<ComboBoxItem>())
            if ((item.Content as string) == content) box.SelectedItem = item;
    }

    static int SelectedInt(ComboBox box, int fallback) =>
        int.TryParse((box.SelectedItem as ComboBoxItem)?.Content as string, out var v) ? v : fallback;

    void RefreshPorts_Click(object sender, RoutedEventArgs e) => RefreshPorts();

    void RefreshPorts()
    {
        var ports = SerialPorts.List();
        RfePort.ItemsSource = ports;
        GpsPort.ItemsSource = ports;
        RfePort.SelectedItem = ports.FirstOrDefault(p => p.Port == _svc.Settings.RfePort)
                               ?? ports.FirstOrDefault(p => p.Display.Contains("CP210", StringComparison.OrdinalIgnoreCase));
        GpsPort.SelectedItem = ports.FirstOrDefault(p => p.Port == _svc.Settings.GpsPort);
    }

    // ---------- RF Explorer ----------

    async void RfeConnect_Click(object sender, RoutedEventArgs e)
    {
        if (RfePort.SelectedItem is not PortInfo port)
        {
            RfeStatus.Text = "Choose a COM port first.";
            return;
        }
        if (_svc.Location is NmeaGpsProvider gps && gps.PortName == port.Port)
        {
            RfeStatus.Text = "That port is in use by the GPS.";
            return;
        }

        int baud = SelectedInt(RfeBaud, 500000);
        _svc.SetRfe(null);
        RfeStatus.Text = $"Opening {port.Port}…";
        var dev = new RfExplorerDevice(port.Port, baud);
        try
        {
            await Task.Run(dev.Open);
        }
        catch (Exception ex)
        {
            dev.Dispose();
            RfeStatus.Text = $"Couldn't open {port.Port}: {ex.Message}";
            return;
        }

        dev.Disconnected += msg => Dispatcher.InvokeAsync(() =>
        {
            if (!ReferenceEquals(_svc.Rfe, dev)) return;
            _svc.SetRfe(null);
            RfeStatus.Text = "The analyzer disconnected: " + msg;
        });
        _svc.SetRfe(dev);
        _svc.Settings.RfePort = port.Port;
        _svc.Settings.RfeBaud = baud;
        _svc.Settings.Save();

        var cfg = await dev.GetConfigAsync(TimeSpan.FromSeconds(4));
        RfeStatus.Text = cfg == null
            ? $"{port.Port} is open, but the device isn't responding. Check that the baud rate matches the device menu."
            : $"Connected on {port.Port}. Active module covers {cfg.MinMHz:0}–{cfg.MaxMHz:0} MHz " +
              $"(max span {cfg.MaxSpanMHz:0} MHz, {cfg.SweepPoints} points). " +
              (dev.ModelInfo is { } m ? $"Model info: {m}" : "");
    }

    void RfeDisconnect_Click(object sender, RoutedEventArgs e)
    {
        _svc.SetRfe(null);
        RfeStatus.Text = "Disconnected.";
    }

    // ---------- GPS ----------

    async void GpsDetect_Click(object sender, RoutedEventArgs e)
    {
        FixText.Text = "Looking for NMEA data on each COM port. This can take up to a minute…";
        var exclude = new List<string>();
        if (_svc.Rfe != null) exclude.Add(_svc.Rfe.PortName);
        if (_svc.Location is NmeaGpsProvider running) exclude.Add(running.PortName);

        var found = await GpsPortDetector.DetectAsync(exclude);
        if (found is not { } f)
        {
            FixText.Text = "No GPS found. Check that the dongle is plugged in and its driver is installed.";
            return;
        }
        RefreshPorts();
        GpsPort.SelectedItem = GpsPort.Items.OfType<PortInfo>().FirstOrDefault(p => p.Port == f.Port);
        SelectContent(GpsBaud, f.Baud.ToString(CultureInfo.InvariantCulture));
        GpsSource.SelectedIndex = 1;
        FixText.Text = $"Found GPS data on {f.Port} at {f.Baud} baud. Press Start GPS.";
    }

    void GpsStart_Click(object sender, RoutedEventArgs e)
    {
        _gpsMessage = null;
        ILocationProvider provider;
        switch (GpsSource.SelectedIndex)
        {
            case 1:
                if (GpsPort.SelectedItem is not PortInfo port)
                {
                    FixText.Text = "Choose the GPS COM port, or use Find GPS port.";
                    return;
                }
                if (_svc.Rfe?.PortName == port.Port)
                {
                    FixText.Text = "That port is in use by the RF Explorer.";
                    return;
                }
                int baud = SelectedInt(GpsBaud, 9600);
                provider = new NmeaGpsProvider(port.Port, baud);
                _svc.Settings.GpsPort = port.Port;
                _svc.Settings.GpsBaud = baud;
                _svc.Settings.Save();
                break;
            case 2:
                provider = new WindowsLocationProvider();
                break;
            default:
                _svc.SetLocation(null);
                return;
        }

        provider.StatusChanged += msg => _gpsMessage = msg;
        _svc.SetLocation(null);
        try
        {
            provider.Start();
            _svc.SetLocation(provider);
            FixText.Text = "Waiting for a fix…";
        }
        catch (Exception ex)
        {
            provider.Dispose();
            FixText.Text = "Couldn't start GPS: " + ex.Message;
        }
    }

    void GpsStop_Click(object sender, RoutedEventArgs e) => _svc.SetLocation(null);

    void UpdateFix()
    {
        var loc = _svc.Location;
        if (loc == null)
        {
            FixDot.Fill = Brushes.Gray;
            if (!FixText.Text.StartsWith("Look", StringComparison.Ordinal) &&
                !FixText.Text.StartsWith("Found", StringComparison.Ordinal) &&
                !FixText.Text.StartsWith("No GPS", StringComparison.Ordinal) &&
                !FixText.Text.StartsWith("Couldn't", StringComparison.Ordinal))
                FixText.Text = "GPS is off.";
            return;
        }

        var f = loc.LastFix;
        if (f == null)
        {
            FixDot.Fill = Brushes.IndianRed;
            FixText.Text = $"{loc.Name}: no fix yet. {_gpsMessage}";
            return;
        }

        FixDot.Fill = !f.IsFresh ? Brushes.IndianRed : f.IsGood ? Brushes.SeaGreen : Brushes.Orange;
        FixText.Text =
            $"{loc.Name}\n{f.Lat:0.0000000}, {f.Lon:0.0000000}" +
            (f.AltM is double alt ? $", altitude {alt:0.0} m" : "") +
            $"\n{GeoText.Describe(f)}" +
            (_gpsMessage != null && !f.IsFresh ? $"\n{_gpsMessage}" : "");
    }

    // ---------- iperf3 ----------

    // ---------- public server directory ----------

    List<IperfServer> _servers = new();
    bool _updatingServerBoxes;

    async void RefreshServers_Click(object sender, RoutedEventArgs e) => await LoadServersAsync(force: true);

    async Task LoadServersAsync(bool force)
    {
        RefreshServersButton.IsEnabled = false;
        ServerListInfo.Text = force ? "Downloading the public server list…" : "";
        try
        {
            _servers = await IperfServerDirectory.GetAsync(force, CancellationToken.None);
            ServerListInfo.Text = _servers.Count == 0
                ? "Couldn't reach the public server list. Enter a server by hand, or try Update list again."
                : $"{_servers.Count} public servers" +
                  (IperfServerDirectory.CachedAt is DateTime when
                      ? $", list from {when.ToLocalTime():g}" : "") +
                  ". Source: iperf3serverlist.net";
            FillRegions();
        }
        catch (Exception ex)
        {
            ServerListInfo.Text = "Couldn't load the list: " + ex.Message;
        }
        finally
        {
            RefreshServersButton.IsEnabled = true;
        }
    }

    void FillRegions()
    {
        _updatingServerBoxes = true;
        RegionBox.Items.Clear();
        RegionBox.Items.Add("All regions");
        foreach (var region in _servers.Select(s => s.Continent).Distinct().OrderBy(r => r))
            RegionBox.Items.Add(region);
        RegionBox.SelectedItem = _svc.Settings.IperfRegion is string saved && RegionBox.Items.Contains(saved)
            ? saved
            : RegionBox.Items[0];
        _updatingServerBoxes = false;
        FillCountries();
    }

    void FillCountries()
    {
        _updatingServerBoxes = true;
        var region = RegionBox.SelectedItem as string;
        CountryBox.Items.Clear();
        CountryBox.Items.Add("All");
        foreach (var country in InRegion(region).Select(s => s.Country).Distinct().OrderBy(c => c))
            CountryBox.Items.Add(country);
        CountryBox.SelectedIndex = 0;
        _updatingServerBoxes = false;
        FillServers();
    }

    void FillServers()
    {
        _updatingServerBoxes = true;
        var region = RegionBox.SelectedItem as string;
        var country = CountryBox.SelectedItem as string;
        PublicServerBox.Items.Clear();
        foreach (var server in InRegion(region)
                     .Where(s => country is null or "All" || s.Country == country)
                     .OrderBy(s => s.Country).ThenBy(s => s.Site))
            PublicServerBox.Items.Add(server);
        _updatingServerBoxes = false;
    }

    IEnumerable<IperfServer> InRegion(string? region) =>
        region is null or "All regions" ? _servers : _servers.Where(s => s.Continent == region);

    void Region_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_updatingServerBoxes) return;
        _svc.Settings.IperfRegion = RegionBox.SelectedItem as string;
        FillCountries();
    }

    void Country_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (!_updatingServerBoxes) FillServers();
    }

    void PublicServer_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_updatingServerBoxes || PublicServerBox.SelectedItem is not IperfServer server) return;
        IperfServer.Text = server.Host;
        IperfPort.Text = server.FirstPort.ToString(CultureInfo.CurrentCulture);
        SaveIperf();
        IperfResult.Text = $"Using {server.Site}, {server.Country} ({server.Provider}). " +
                           (server.PortCount > 1
                               ? $"Ports {server.FirstPort}-{server.LastPort} are open, so several adapters can test at once."
                               : "Only one port is open here, so run one adapter at a time against it.");
    }

    // ---------- iperf3 ----------

    void SaveIperf()
    {
        var s = _svc.Settings;
        s.IperfPath = IperfPath.Text.Trim();
        s.IperfServer = IperfServer.Text.Trim();
        s.IperfPort = int.TryParse(IperfPort.Text, out var port) ? Math.Clamp(port, 1, 65535) : 5201;
        s.IperfSeconds = int.TryParse(IperfSeconds.Text, out var sec) ? Math.Clamp(sec, 1, 60) : 3;
        IperfSeconds.Text = s.IperfSeconds.ToString(CultureInfo.CurrentCulture);
        IperfPort.Text = s.IperfPort.ToString(CultureInfo.CurrentCulture);
        _ = LoadServersAsync(force: false);
        s.Save();
    }

    void IperfBrowse_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new OpenFileDialog { Filter = "iperf3|iperf3.exe|Programs (*.exe)|*.exe" };
        if (dlg.ShowDialog() == true)
        {
            IperfPath.Text = dlg.FileName;
            SaveIperf();
        }
    }

    async void IperfTest_Click(object sender, RoutedEventArgs e)
    {
        SaveIperf();
        var s = _svc.Settings;
        IperfResult.Text = $"Testing against {s.IperfServer}…";
        try
        {
            var r = await Iperf3Runner.RunAsync(s.IperfPath, s.IperfServer, s.IperfSeconds,
                CancellationToken.None, null, s.IperfPort);
            IperfResult.Text = $"Download {r.DownMbps:0.0} Mbps, upload {r.UpMbps:0.0} Mbps";
        }
        catch (Exception ex)
        {
            IperfResult.Text = "Test failed: " + ex.Message;
        }
    }

    // ---------- misc ----------

    void LocationSettings_Click(object sender, RoutedEventArgs e) =>
        Process.Start(new ProcessStartInfo("ms-settings:privacy-location") { UseShellExecute = true });

    void DataFolder_Click(object sender, RoutedEventArgs e) =>
        Process.Start(new ProcessStartInfo(AppPaths.DataDir) { UseShellExecute = true });
}
