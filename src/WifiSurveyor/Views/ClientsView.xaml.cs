using System.Collections.ObjectModel;
using System.Globalization;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using WifiSurveyor.Services;
using WifiSurveyor.Models;
using WifiSurveyor.Surveying;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Views;

public enum ServerStatus { Unknown, Checking, Up, Down }

public sealed class ServerChoice : System.ComponentModel.INotifyPropertyChanged
{
    bool _selected;
    ServerStatus _status;
    string _detail = "";

    public ServerStatus Status
    {
        get => _status;
        set
        {
            if (_status == value) return;
            _status = value;
            Raise(nameof(Status));
            Raise(nameof(StatusText));
            Raise(nameof(StatusBrush));
        }
    }

    public string Detail
    {
        get => _detail;
        set
        {
            _detail = value;
            Raise(nameof(StatusText));
        }
    }

    public string StatusText => _status switch
    {
        ServerStatus.Checking => "  checking…",
        ServerStatus.Up => $"  up{(_detail.Length > 0 ? $", {_detail}" : "")}",
        ServerStatus.Down => $"  down{(_detail.Length > 0 ? $": {_detail}" : "")}",
        _ => ""
    };

    public System.Windows.Media.Brush StatusBrush => _status switch
    {
        ServerStatus.Up => Draw.Brush(21, 128, 61),
        ServerStatus.Down => Draw.Brush(185, 28, 28),
        _ => Draw.Brush(120, 130, 145)
    };

    void Raise(string name) =>
        PropertyChanged?.Invoke(this, new System.ComponentModel.PropertyChangedEventArgs(name));

    public ServerChoice(IperfServer server) => Server = server;

    public IperfServer Server { get; }
    public string Label => Server.Display;

    public bool Selected
    {
        get => _selected;
        set
        {
            if (_selected == value) return;
            _selected = value;
            PropertyChanged?.Invoke(this, new System.ComponentModel.PropertyChangedEventArgs(nameof(Selected)));
        }
    }

    public event System.ComponentModel.PropertyChangedEventHandler? PropertyChanged;
}

public partial class ClientsView : UserControl
{
    readonly AppServices _svc = AppServices.Instance;
    readonly ObservableCollection<ClientRow> _rows = new();
    readonly ObservableCollection<LinkEvent> _events = new();
    readonly ClientMonitor _monitor = new();
    readonly ObservableCollection<ServerChoice> _choices = new();
    readonly Dictionary<Guid, List<(string Host, int Port)>> _assignments = new();
    List<IperfServer> _servers = new();
    CancellationTokenSource? _loadCts;
    bool _watching, _updatingPool;

    public ClientsView()
    {
        InitializeComponent();
        ClientGrid.ItemsSource = _rows;
        EventGrid.ItemsSource = _events;
        _monitor.Updated += (links, fresh) => Dispatcher.InvokeAsync(() => Apply(links, fresh));
        ServerList.ItemsSource = _choices;
        Refresh();
        _ = LoadServersAsync(force: false);
    }

    void Refresh_Click(object sender, RoutedEventArgs e) => Refresh();

    void Refresh()
    {
        var interfaces = WlanConnection.ListInterfaces();
        foreach (var iface in interfaces)
            if (_rows.All(r => r.Id != iface.Id))
                _rows.Add(new ClientRow(iface));
        foreach (var stale in _rows.Where(r => interfaces.All(i => i.Id != r.Id)).ToList())
            _rows.Remove(stale);

        WatchStatus.Text = _rows.Count switch
        {
            0 => "No WLAN adapters found.",
            1 => "1 WLAN adapter. Plug in more to use them as witnesses or load generators.",
            _ => $"{_rows.Count} WLAN adapters."
        };
        Apply(WlanConnection.AllLinks(), new List<LinkEvent>());
    }

    void Watch_Click(object sender, RoutedEventArgs e)
    {
        if (_watching)
        {
            _monitor.Stop();
            _watching = false;
            WatchButton.Content = "Start watching";
            WatchStatus.Text = $"Stopped. {_events.Count} events logged.";
            return;
        }
        _monitor.Start();
        _watching = true;
        WatchButton.Content = "Stop watching";
        WatchStatus.Text = "Watching every adapter once a second…";
    }

    void Apply(List<(WlanInterfaceInfo Interface, Models.ConnectionInfo? Connection)> links, List<LinkEvent> fresh)
    {
        foreach (var (iface, connection) in links)
        {
            var row = _rows.FirstOrDefault(r => r.Id == iface.Id);
            if (row == null)
            {
                row = new ClientRow(iface);
                _rows.Add(row);
            }
            row.State = iface.StateText;
            row.Ssid = connection?.Ssid ?? "";
            row.Bssid = connection?.Bssid ?? "";
            row.Phy = connection?.PhyType ?? "";
            row.Signal = connection?.EstimatedRssi ?? 0;
            row.TxMbps = connection?.TxRateMbps ?? 0;
            row.RxMbps = connection?.RxRateMbps ?? 0;

            // The scan list is the only place the band of the connected BSSID is available.
            var seen = _svc.LastScan.FirstOrDefault(n =>
                string.Equals(n.Bssid, connection?.Bssid, StringComparison.OrdinalIgnoreCase));
            row.Band = seen != null ? WifiChannels.BandLabel(seen.FrequencyMHz) : "";
        }

        foreach (var ev in fresh)
        {
            _events.Insert(0, ev);
            var row = _rows.FirstOrDefault(r => r.Adapter == ev.Adapter);
            if (row == null) continue;
            row.LastChange = ev.Time;
            if (ev.Kind == LinkEventKind.Disconnected) row.Drops++;
            if (ev.Kind == LinkEventKind.Roamed) row.Roams++;
        }
        while (_events.Count > 2000) _events.RemoveAt(_events.Count - 1);
    }

    void Clear_Click(object sender, RoutedEventArgs e)
    {
        _events.Clear();
        _monitor.Events.Clear();
        foreach (var row in _rows) { row.Drops = 0; row.Roams = 0; }
    }

    void Export_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog { Filter = "CSV (*.csv)|*.csv", FileName = "link-events.csv" };
        if (dlg.ShowDialog() != true) return;
        try
        {
            var sb = new StringBuilder("time,adapter,event,detail\n");
            foreach (var ev in _events.Reverse())
            {
                var adapter = Quote(ev.Adapter);
                var detail = Quote(ev.Detail);
                sb.AppendLine($"{ev.Time:s},{adapter},{ev.KindText},{detail}");
            }
            File.WriteAllText(dlg.FileName, sb.ToString());
        }
        catch (Exception ex)
        {
            MessageBox.Show("Couldn't save: " + ex.Message, "Wi-Fi Surveyor");
        }
    }

    static string Quote(string text) => "\"" + text.Replace("\"", "\"\"") + "\"";

    async void RefreshServers_Click(object sender, RoutedEventArgs e) => await LoadServersAsync(force: true);

    async Task LoadServersAsync(bool force)
    {
        PoolInfo.Text = "Loading the public server list…";
        try
        {
            _servers = await IperfServerDirectory.GetAsync(force, CancellationToken.None);
            _updatingPool = true;
            PoolRegion.Items.Clear();
            PoolRegion.Items.Add("All regions");
            foreach (var region in _servers.Select(s => s.Continent).Distinct().OrderBy(r => r))
                PoolRegion.Items.Add(region);
            PoolRegion.SelectedItem = _svc.Settings.IperfRegion is string saved && PoolRegion.Items.Contains(saved)
                ? saved
                : PoolRegion.Items[0];
            _updatingPool = false;
            FillChoices();
        }
        catch (Exception ex)
        {
            PoolInfo.Text = "Couldn't load the list: " + ex.Message;
        }
    }

    void PoolRegion_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (!_updatingPool) FillChoices();
    }

    void PoolSearch_Changed(object sender, RoutedEventArgs e)
    {
        if (!_updatingPool) FillChoices();
    }

    void FillChoices()
    {
        var ticked = new HashSet<string>(_choices.Where(c => c.Selected).Select(c => c.Server.Key));
        foreach (var key in _svc.Settings.ServerPool) ticked.Add(key);

        var region = PoolRegion.SelectedItem as string;
        var search = PoolSearch.Text.Trim();

        _choices.Clear();
        foreach (var server in _servers
                     .Where(s => region is null or "All regions" || s.Continent == region)
                     .Where(s => search.Length == 0
                                 || s.Country.Contains(search, StringComparison.OrdinalIgnoreCase)
                                 || s.Site.Contains(search, StringComparison.OrdinalIgnoreCase)
                                 || s.Provider.Contains(search, StringComparison.OrdinalIgnoreCase)
                                 || s.Host.Contains(search, StringComparison.OrdinalIgnoreCase))
                     .OrderBy(s => s.Country).ThenBy(s => s.Site)
                     .Take(300))
            _choices.Add(new ServerChoice(server) { Selected = ticked.Contains(server.Key) });

        PoolInfo.Text = $"{_choices.Count} shown, {ticked.Count} ticked.";
    }

    void TickSome_Click(object sender, RoutedEventArgs e)
    {
        int wanted = Math.Max(1, _rows.Count(r => r.Selected)) + 2;   // two spares for failover
        int ticked = 0;
        // Prefer servers with a port range: one host can then take several adapters if needed.
        foreach (var choice in _choices.OrderByDescending(c => c.Server.PortCount))
        {
            choice.Selected = ticked < wanted;
            if (choice.Selected) ticked++;
        }
        SavePool();
        PoolInfo.Text = $"Ticked {ticked} server(s): one per adapter and two spares.";
    }

    void SavePool()
    {
        _svc.Settings.ServerPool = _choices.Where(c => c.Selected).Select(c => c.Server.Key).ToList();
        _svc.Settings.Save();
    }

    void Assign_Click(object sender, RoutedEventArgs e)
    {
        SavePool();
        var pool = BuildPool();
        var adapters = _rows.Where(r => r.Selected).ToList();
        if (adapters.Count == 0)
        {
            PoolInfo.Text = "Tick the adapters to test in the table above.";
            return;
        }
        if (pool.Count == 0)
        {
            PoolInfo.Text = "Tick at least one server, or set a server on the Devices tab.";
            return;
        }

        _assignments.Clear();
        // One distinct host each where possible; when a host has a port range, extra adapters
        // get their own port on it rather than colliding.
        var slots = new List<(string Host, int Port)>();
        foreach (var server in pool) slots.Add((server.Host, server.FirstPort));
        int round = 1;
        while (slots.Count < adapters.Count)
        {
            bool added = false;
            foreach (var server in pool.Where(s => s.PortCount > round))
            {
                slots.Add((server.Host, server.FirstPort + round));
                added = true;
                if (slots.Count >= adapters.Count) break;
            }
            round++;
            if (!added) break;
        }

        for (int i = 0; i < adapters.Count; i++)
        {
            var mine = new List<(string Host, int Port)>();
            if (i < slots.Count) mine.Add(slots[i]);
            // Anything left over becomes a fallback, so one dead server doesn't lose the run.
            mine.AddRange(slots.Where((_, index) => index != i).Take(2));
            _assignments[adapters[i].Id] = mine;
            adapters[i].Server = mine.Count > 0 ? $"{mine[0].Host}:{mine[0].Port}" : "none";
        }

        PoolInfo.Text = slots.Count >= adapters.Count
            ? $"{adapters.Count} adapter(s), each with its own server and {Math.Min(2, slots.Count - 1)} fallback(s)."
            : $"Only {slots.Count} distinct server slot(s) for {adapters.Count} adapters. Tick more servers, " +
              "or expect some to share and compete.";
    }

    List<IperfServer> BuildPool()
    {
        var pool = _choices.Where(c => c.Selected).Select(c => c.Server).ToList();
        if (UseLanServer.IsChecked == true && !string.IsNullOrWhiteSpace(_svc.Settings.IperfServer))
            pool.Insert(0, new IperfServer
            {
                Host = _svc.Settings.IperfServer.Trim(),
                FirstPort = _svc.Settings.IperfPort,
                LastPort = _svc.Settings.IperfPort + 9,   // assume the usual one-per-adapter setup
                Site = "your network",
                Country = "LAN",
                Provider = "local"
            });
        return pool;
    }

    async void LoadTest_Click(object sender, RoutedEventArgs e)
    {
        var settings = _svc.Settings;
        SavePool();
        var pool = BuildPool();
        if (pool.Count == 0)
        {
            LoadStatus.Text = "Tick at least one server, or set one on the Devices tab.";
            return;
        }

        var clients = new List<(string Adapter, string BindAddress)>();
        var skipped = new List<string>();
        var interfaces = WlanConnection.ListInterfaces();
        foreach (var row in _rows.Where(r => r.Selected))
        {
            var address = interfaces.FirstOrDefault(i => i.Id == row.Id)?.LocalIPv4();
            if (address == null) skipped.Add(row.Adapter);
            else clients.Add((row.Adapter, address));
        }
        if (clients.Count == 0)
        {
            LoadStatus.Text = "No ticked adapter has an IP address. Connect them to the network first.";
            return;
        }

        // Servers already known to be down go to the back of the queue rather than the front.
        var ordered = pool
            .OrderBy(server => _choices.FirstOrDefault(c => c.Server.Key == server.Key)?.Status == ServerStatus.Down)
            .ToList();
        var allocator = new ServerAllocator(ordered);
        if (allocator.Total < clients.Count)
            LoadResults.Text = $"Only {allocator.Total} server slot(s) for {clients.Count} adapters; " +
                               "tick more servers so every adapter gets one, with spares.\n";
        else
            LoadResults.Text = "";

        int seconds = int.TryParse(LoadSeconds.Text, out var s) ? Math.Clamp(s, 1, 120) : 5;
        _loadCts = new CancellationTokenSource();
        LoadButton.IsEnabled = false;
        LoadStatus.Text = $"Testing {clients.Count} adapter(s) at once…";

        var log = new Progress<string>(line =>
        {
            LoadResults.AppendText($"{DateTime.Now:HH:mm:ss}  {line}\n");
            LoadResults.ScrollToEnd();
        });

        try
        {
            var results = await LoadTester.RunAsync(settings.IperfPath, seconds, clients, allocator, log,
                _loadCts.Token);

            foreach (var host in allocator.DeadHosts)
                foreach (var choice in _choices.Where(c => c.Server.Host.Equals(host, StringComparison.OrdinalIgnoreCase)))
                    choice.Status = ServerStatus.Down;

            var sb = new StringBuilder("\n");
            double totalDown = 0, totalUp = 0;
            foreach (var r in results)
            {
                var row = _rows.FirstOrDefault(x => x.Adapter == r.Adapter);
                if (r.Result is { } t)
                {
                    totalDown += t.DownMbps;
                    totalUp += t.UpMbps;
                    if (row != null) row.Server = r.Server;
                    sb.AppendLine($"{Trim(r.Adapter),-36} {r.Server,-30} down {t.DownMbps,7:0.0}  up {t.UpMbps,7:0.0} Mbps");
                }
                else
                {
                    if (row != null) row.Server = "none worked";
                    sb.AppendLine($"{Trim(r.Adapter),-36} {r.Error}");
                }
            }
            sb.AppendLine(new string('-', 96));
            sb.AppendLine($"{"Total",-36} {"",-30} down {totalDown,7:0.0}  up {totalUp,7:0.0} Mbps");
            if (allocator.DeadHosts.Count > 0)
                sb.AppendLine($"Written off as down: {string.Join(", ", allocator.DeadHosts)}");
            if (skipped.Count > 0)
                sb.AppendLine("Skipped (no IP address): " + string.Join(", ", skipped));

            LoadResults.AppendText(sb.ToString());
            LoadResults.ScrollToEnd();
            LoadStatus.Text = results.All(r => r.Result != null)
                ? "Done."
                : "Done, but some adapters found no working server. Tick more servers and run again.";
        }
        catch (OperationCanceledException)
        {
            LoadStatus.Text = "Cancelled.";
        }
        catch (Exception ex)
        {
            LoadStatus.Text = "Test failed: " + ex.Message;
        }
        finally
        {
            LoadButton.IsEnabled = true;
            _loadCts?.Dispose();
            _loadCts = null;
        }
    }

    static string Trim(string text) => text.Length > 35 ? text[..34] + "…" : text;

    async void CheckServers_Click(object sender, RoutedEventArgs e)
    {
        var ticked = _choices.Where(c => c.Selected).ToList();
        if (ticked.Count == 0)
        {
            PoolInfo.Text = "Tick some servers first.";
            return;
        }

        CheckButton.IsEnabled = false;
        PoolInfo.Text = $"Checking {ticked.Count} server(s)…";
        foreach (var choice in ticked) choice.Status = ServerStatus.Checking;

        // A handful at a time: probing dozens of hosts at once can trip rate limits.
        using var gate = new SemaphoreSlim(6);
        var probes = ticked.Select(async choice =>
        {
            await gate.WaitAsync();
            try
            {
                var result = await ServerProbe.ProbeAsync(choice.Server.Host, choice.Server.FirstPort,
                    TimeSpan.FromSeconds(3), CancellationToken.None);
                choice.Status = result.Up ? ServerStatus.Up : ServerStatus.Down;
                choice.Detail = result.Up ? $"{result.Milliseconds:0} ms" : result.Error ?? "";
            }
            finally
            {
                gate.Release();
            }
        });
        await Task.WhenAll(probes);

        int up = ticked.Count(c => c.Status == ServerStatus.Up);
        PoolInfo.Text = $"{up} of {ticked.Count} answered. Down servers are tried last, and only if the rest run out.";
        CheckButton.IsEnabled = true;
    }

    void CancelLoad_Click(object sender, RoutedEventArgs e) => _loadCts?.Cancel();
}
