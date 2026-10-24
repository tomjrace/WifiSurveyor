using System.ComponentModel;
using System.Runtime.CompilerServices;
using WifiSurveyor.Models;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

public enum LinkEventKind { Connected, Disconnected, Roamed, BandChanged, RateDrop }

public sealed record LinkEvent(DateTime Time, string Adapter, LinkEventKind Kind, string Detail)
{
    public string KindText => Kind switch
    {
        LinkEventKind.Connected => "Connected",
        LinkEventKind.Disconnected => "Dropped",
        LinkEventKind.Roamed => "Roamed",
        LinkEventKind.BandChanged => "Changed band",
        _ => "Rate drop"
    };
}

/// <summary>One adapter's live state, shaped for the grid.</summary>
public sealed class ClientRow : INotifyPropertyChanged
{
    string _state = "", _ssid = "", _bssid = "", _band = "", _phy = "", _server = "";
    int _signal, _drops, _roams;
    double _tx, _rx;
    DateTime _lastChange = DateTime.Now;

    public ClientRow(WlanInterfaceInfo iface)
    {
        Id = iface.Id;
        Adapter = iface.Description;
    }

    public Guid Id { get; }
    public string Adapter { get; }
    public bool Selected { get; set; } = true;
    public string Server { get => _server; set => Set(ref _server, value); }
    public string State { get => _state; set => Set(ref _state, value); }
    public string Ssid { get => _ssid; set => Set(ref _ssid, value); }
    public string Bssid { get => _bssid; set => Set(ref _bssid, value); }
    public string Band { get => _band; set => Set(ref _band, value); }
    public string Phy { get => _phy; set => Set(ref _phy, value); }
    public int Signal { get => _signal; set => Set(ref _signal, value); }
    public double TxMbps { get => _tx; set => Set(ref _tx, value); }
    public double RxMbps { get => _rx; set => Set(ref _rx, value); }
    public int Drops { get => _drops; set => Set(ref _drops, value); }
    public int Roams { get => _roams; set => Set(ref _roams, value); }
    public DateTime LastChange { get => _lastChange; set => Set(ref _lastChange, value); }

    public event PropertyChangedEventHandler? PropertyChanged;

    void Set<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return;
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}

/// <summary>
/// Polls every WLAN adapter once a second and reports drops, roams, and band changes.
/// Cheap adapters make good witnesses: they hold associations while the survey runs and
/// record exactly when and where a link failed.
/// </summary>
public sealed class ClientMonitor : IDisposable
{
    readonly Dictionary<Guid, ConnectionInfo?> _previous = new();
    readonly Dictionary<Guid, double> _bestRate = new();
    readonly System.Threading.Timer _timer;
    bool _running;

    public ClientMonitor()
    {
        _timer = new System.Threading.Timer(_ => Poll(), null, Timeout.Infinite, Timeout.Infinite);
    }

    public List<LinkEvent> Events { get; } = new();

    /// <summary>Raised on a background thread with the current links and any new events.</summary>
    public event Action<List<(WlanInterfaceInfo Interface, ConnectionInfo? Connection)>, List<LinkEvent>>? Updated;

    public void Start()
    {
        _running = true;
        _timer.Change(TimeSpan.Zero, TimeSpan.FromSeconds(1));
    }

    public void Stop()
    {
        _running = false;
        _timer.Change(Timeout.Infinite, Timeout.Infinite);
    }

    void Poll()
    {
        if (!_running) return;
        List<(WlanInterfaceInfo Interface, ConnectionInfo? Connection)> links;
        try { links = WlanConnection.AllLinks(); }
        catch { return; }

        var now = DateTime.Now;
        var fresh = new List<LinkEvent>();

        foreach (var (iface, current) in links)
        {
            _previous.TryGetValue(iface.Id, out var before);
            _previous[iface.Id] = current;

            if (before == null && current != null)
                fresh.Add(new LinkEvent(now, iface.Description, LinkEventKind.Connected,
                    $"Joined {current.Ssid} on {current.Bssid}"));
            else if (before != null && current == null)
                fresh.Add(new LinkEvent(now, iface.Description, LinkEventKind.Disconnected,
                    $"Lost {before.Ssid} ({iface.StateText})"));
            else if (before != null && current != null)
            {
                if (!string.Equals(before.Bssid, current.Bssid, StringComparison.OrdinalIgnoreCase))
                    fresh.Add(new LinkEvent(now, iface.Description, LinkEventKind.Roamed,
                        $"{before.Bssid} to {current.Bssid} on {current.Ssid}"));

                // A big drop in negotiated rate is worth a mark even when the link survives.
                double best = _bestRate.GetValueOrDefault(iface.Id);
                if (current.TxRateMbps > best) _bestRate[iface.Id] = current.TxRateMbps;
                else if (best > 0 && current.TxRateMbps > 0 && current.TxRateMbps < best * 0.4 &&
                         before.TxRateMbps >= best * 0.4)
                    fresh.Add(new LinkEvent(now, iface.Description, LinkEventKind.RateDrop,
                        $"{current.TxRateMbps:0} Mbps, down from {best:0} Mbps"));
            }
        }

        if (fresh.Count > 0)
        {
            Events.AddRange(fresh);
            if (Events.Count > 5000) Events.RemoveRange(0, Events.Count - 5000);
        }
        Updated?.Invoke(links, fresh);
    }

    public void Dispose()
    {
        _running = false;
        _timer.Dispose();
    }
}
