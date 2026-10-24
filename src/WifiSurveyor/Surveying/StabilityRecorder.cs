using WifiSurveyor.Models;
using WifiSurveyor.Services;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

public sealed record WalkOptions(bool RunThroughput, int Seconds, IReadOnlyList<ClientTarget> Clients);

/// <summary>
/// Records a walk rather than a set of standing measurements. Adapters stay associated (and
/// optionally keep running short throughput tests) while you move, and every drop, roam, or
/// failed test is stamped with where you were at the time.
///
/// Position comes from GPS when there's a good fix; indoors you drop breadcrumbs by clicking
/// the plan as you pass known spots, and events in between are placed by interpolating along
/// the time between breadcrumbs.
/// </summary>
public sealed class StabilityRecorder : IDisposable
{
    readonly SurveyProject _project;
    readonly Func<(double X, double Y)?> _gpsPosition;
    readonly ClientMonitor _monitor = new();
    readonly List<StabilityEvent> _pending = new();
    readonly object _lock = new();
    CancellationTokenSource? _cts;
    WalkOptions _options = new(false, 3, Array.Empty<ClientTarget>());

    public StabilityRecorder(SurveyProject project, Func<(double X, double Y)?> gpsPosition)
    {
        _project = project;
        _gpsPosition = gpsPosition;
        _monitor.Updated += OnMonitorUpdate;
    }

    public bool Running { get; private set; }
    public int PendingCount { get { lock (_lock) return _pending.Count; } }
    public string LastThroughput { get; private set; } = "";

    /// <summary>Raised on a background thread whenever something was recorded.</summary>
    public event Action? Updated;

    public void Start(WalkOptions options)
    {
        if (Running) return;
        _options = options;
        _cts = new CancellationTokenSource();
        Running = true;
        _monitor.Start();

        if (options.RunThroughput && options.Clients.Count > 0)
            foreach (var client in options.Clients)
                _ = Task.Run(() => ThroughputLoopAsync(client, _cts.Token));
    }

    public void Stop()
    {
        Running = false;
        _monitor.Stop();
        _cts?.Cancel();
        _cts = null;
        lock (_lock)
        {
            // Anything still unplaced goes to the last known spot rather than being thrown away.
            var last = _project.Track.LastOrDefault();
            if (last != null)
                foreach (var e in _pending)
                {
                    e.X = last.X;
                    e.Y = last.Y;
                    _project.Issues.Add(e);
                }
            _pending.Clear();
        }
        Updated?.Invoke();
    }

    /// <summary>Marks where you are now, and places any events recorded since the last mark.</summary>
    public void AddBreadcrumb(double x, double y)
    {
        var now = DateTime.Now;
        var previous = _project.Track.LastOrDefault();
        _project.Track.Add(new TrackPoint { X = x, Y = y, Time = now, FromGps = false });

        lock (_lock)
        {
            foreach (var e in _pending)
            {
                if (previous == null || now <= previous.Time)
                {
                    e.X = x;
                    e.Y = y;
                }
                else
                {
                    // Assume a steady walking pace between the two marks.
                    double t = Math.Clamp((e.Time - previous.Time).TotalSeconds /
                                          (now - previous.Time).TotalSeconds, 0, 1);
                    e.X = previous.X + (x - previous.X) * t;
                    e.Y = previous.Y + (y - previous.Y) * t;
                }
                _project.Issues.Add(e);
            }
            _pending.Clear();
        }
        Updated?.Invoke();
    }

    void OnMonitorUpdate(List<(WlanInterfaceInfo Interface, ConnectionInfo? Connection)> links, List<LinkEvent> fresh)
    {
        if (!Running || fresh.Count == 0) return;
        foreach (var e in fresh)
        {
            var issue = e.Kind switch
            {
                LinkEventKind.Disconnected => StabilityIssue.Disconnect,
                LinkEventKind.Roamed => StabilityIssue.Roam,
                LinkEventKind.RateDrop => StabilityIssue.RateDrop,
                LinkEventKind.Connected => StabilityIssue.Reconnect,
                _ => StabilityIssue.RateDrop
            };
            Record(new StabilityEvent
            {
                Time = e.Time,
                Adapter = e.Adapter,
                Issue = issue,
                Detail = e.Detail
            });
        }
    }

    async Task ThroughputLoopAsync(ClientTarget client, CancellationToken ct)
    {
        var settings = AppServices.Instance.Settings;
        while (!ct.IsCancellationRequested)
        {
            var started = DateTime.Now;
            try
            {
                var results = await Iperf3Runner.RunParallelAsync(settings.IperfPath, _options.Seconds,
                    new[] { client }, ct);
                var result = results.FirstOrDefault();
                if (result?.Result is { } throughput)
                {
                    LastThroughput = $"{client.Adapter}: {throughput.DownMbps:0} Mbps down";
                    if (throughput.DownMbps < 1)
                        Record(new StabilityEvent
                        {
                            Time = started,
                            Adapter = client.Adapter,
                            Issue = StabilityIssue.Stall,
                            Detail = $"Throughput collapsed to {throughput.DownMbps:0.0} Mbps",
                            Mbps = throughput.DownMbps
                        });
                }
                else
                {
                    Record(new StabilityEvent
                    {
                        Time = started,
                        Adapter = client.Adapter,
                        Issue = StabilityIssue.ThroughputFail,
                        Detail = result?.Error ?? "The test didn't complete"
                    });
                }
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (Exception ex)
            {
                Record(new StabilityEvent
                {
                    Time = started,
                    Adapter = client.Adapter,
                    Issue = StabilityIssue.ThroughputFail,
                    Detail = ex.Message
                });
                try { await Task.Delay(2000, ct); } catch { return; }
            }
        }
    }

    void Record(StabilityEvent e)
    {
        if (_gpsPosition() is { } position)
        {
            e.X = position.X;
            e.Y = position.Y;
            lock (_lock) _project.Issues.Add(e);
        }
        else
        {
            lock (_lock) _pending.Add(e);   // placed when the next breadcrumb lands
        }
        Updated?.Invoke();
    }

    public void Dispose()
    {
        Stop();
        _monitor.Dispose();
    }
}

public static class StabilityAnalysis
{
    public sealed record IssueCluster(double X, double Y, int Count, StabilityIssue Worst, string Summary);

    /// <summary>Groups nearby events so the map shows trouble spots rather than a confetti of pins.</summary>
    public static List<IssueCluster> Cluster(SurveyProject project, double radiusPx)
    {
        var clusters = new List<List<StabilityEvent>>();
        foreach (var e in project.Issues)
        {
            var match = clusters.FirstOrDefault(c =>
            {
                double cx = c.Average(x => x.X), cy = c.Average(x => x.Y);
                return (cx - e.X) * (cx - e.X) + (cy - e.Y) * (cy - e.Y) <= radiusPx * radiusPx;
            });
            if (match == null) clusters.Add(new List<StabilityEvent> { e });
            else match.Add(e);
        }

        return clusters.Select(c =>
        {
            var worst = c.Select(x => x.Issue).OrderBy(Severity).First();
            var counts = c.GroupBy(x => x.Issue)
                .OrderByDescending(g => g.Count())
                .Select(g => $"{g.Count()} × {Label(g.Key)}");
            return new IssueCluster(c.Average(x => x.X), c.Average(x => x.Y), c.Count, worst,
                string.Join(", ", counts));
        }).OrderByDescending(c => c.Count).ToList();
    }

    static int Severity(StabilityIssue issue) => issue switch
    {
        StabilityIssue.Disconnect => 0,
        StabilityIssue.ThroughputFail => 1,
        StabilityIssue.Stall => 2,
        StabilityIssue.RateDrop => 3,
        StabilityIssue.Roam => 4,
        _ => 5
    };

    public static string Label(StabilityIssue issue) => issue switch
    {
        StabilityIssue.Disconnect => "dropped",
        StabilityIssue.Reconnect => "rejoined",
        StabilityIssue.Roam => "roamed",
        StabilityIssue.RateDrop => "rate collapse",
        StabilityIssue.ThroughputFail => "test failed",
        _ => "stalled"
    };

    public static (byte R, byte G, byte B) Color(StabilityIssue issue) => issue switch
    {
        StabilityIssue.Disconnect => (220, 38, 38),
        StabilityIssue.ThroughputFail => (194, 65, 12),
        StabilityIssue.Stall => (202, 138, 4),
        StabilityIssue.RateDrop => (13, 148, 136),
        StabilityIssue.Roam => (37, 99, 235),
        _ => (100, 116, 139)
    };
}
