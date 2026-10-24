using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using WifiSurveyor.Services;

namespace WifiSurveyor.Surveying;

public static class ServerProbe
{
    public sealed record Result(bool Up, double? Milliseconds, string? Error);

    /// <summary>
    /// Opens a TCP connection to the iperf3 port and closes it. A refused or silent port
    /// shows up in a couple of seconds, instead of iperf3 hanging on it for much longer.
    /// When a local address is given, the probe leaves through that adapter, which is the
    /// route the test itself will take.
    /// </summary>
    public static async Task<Result> ProbeAsync(string host, int port, TimeSpan timeout,
        CancellationToken ct, string? bindAddress = null)
    {
        var watch = Stopwatch.StartNew();
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeout);
        try
        {
            var addresses = await Dns.GetHostAddressesAsync(host, cts.Token);
            var target = addresses.FirstOrDefault(a => a.AddressFamily == AddressFamily.InterNetwork)
                         ?? addresses.FirstOrDefault();
            if (target == null) return new Result(false, null, "the name didn't resolve");

            using var socket = new Socket(target.AddressFamily, SocketType.Stream, ProtocolType.Tcp);
            if (bindAddress != null && target.AddressFamily == AddressFamily.InterNetwork &&
                IPAddress.TryParse(bindAddress, out var local))
                socket.Bind(new IPEndPoint(local, 0));

            await socket.ConnectAsync(target, port, cts.Token);
            return new Result(true, watch.Elapsed.TotalMilliseconds, null);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            return new Result(false, null, $"no answer within {timeout.TotalSeconds:0} s");
        }
        catch (SocketException ex)
        {
            return new Result(false, null, ex.SocketErrorCode switch
            {
                SocketError.ConnectionRefused => "refused the connection",
                SocketError.HostNotFound or SocketError.NoData => "the name didn't resolve",
                SocketError.NetworkUnreachable or SocketError.HostUnreachable => "unreachable",
                SocketError.TimedOut => "timed out",
                _ => ex.SocketErrorCode.ToString()
            });
        }
    }
}

/// <summary>
/// Hands out servers so that no two adapters get the same one, and so that a server found to
/// be down is skipped by every adapter for the rest of the run, not rediscovered by each.
/// </summary>
public sealed class ServerAllocator
{
    readonly List<(string Host, int Port)> _slots = new();
    readonly HashSet<string> _deadHosts = new(StringComparer.OrdinalIgnoreCase);
    readonly HashSet<string> _busy = new(StringComparer.OrdinalIgnoreCase);
    readonly object _lock = new();
    int _next;

    public ServerAllocator(IEnumerable<IperfServer> pool)
    {
        var servers = pool.ToList();
        // Every host's first port before anyone's second, so adapters spread across hosts.
        int widest = servers.Count == 0 ? 0 : servers.Max(s => s.PortCount);
        for (int round = 0; round < widest; round++)
            foreach (var server in servers.Where(s => s.PortCount > round))
                _slots.Add((server.Host, server.FirstPort + round));
    }

    public int Total => _slots.Count;
    public IReadOnlyCollection<string> DeadHosts { get { lock (_lock) return _deadHosts.ToList(); } }

    public (string Host, int Port)? Take()
    {
        lock (_lock)
        {
            while (_next < _slots.Count)
            {
                var slot = _slots[_next++];
                if (_deadHosts.Contains(slot.Host) || _busy.Contains(Key(slot.Host, slot.Port))) continue;
                return slot;
            }
            return null;
        }
    }

    /// <summary>A host that won't connect is written off along with all its ports.</summary>
    public void MarkDown(string host)
    {
        lock (_lock) _deadHosts.Add(host);
    }

    /// <summary>A busy port is someone else's test in progress; the host's other ports may be free.</summary>
    public void MarkBusy(string host, int port)
    {
        lock (_lock) _busy.Add(Key(host, port));
    }

    static string Key(string host, int port) => $"{host}:{port}";
}

public static class LoadTester
{
    /// <summary>
    /// Runs every adapter at once. Each works through servers until one completes: a server
    /// that doesn't answer is probed in seconds and written off for everyone, and a server
    /// busy with someone else's test is skipped, so a dead public server costs a moment
    /// rather than the run.
    /// </summary>
    public static async Task<List<ClientResult>> RunAsync(string exe, int seconds,
        IReadOnlyList<(string Adapter, string BindAddress)> clients, ServerAllocator servers,
        IProgress<string> log, CancellationToken ct)
    {
        var tasks = clients.Select(async client =>
        {
            string? lastError = null;
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                if (servers.Take() is not { } slot)
                    return new ClientResult(client.Adapter, null,
                        lastError == null ? "No servers left to try." : $"No servers left. Last problem: {lastError}",
                        "");

                var endpoint = $"{slot.Host}:{slot.Port}";
                var probe = await ServerProbe.ProbeAsync(slot.Host, slot.Port, TimeSpan.FromSeconds(3), ct,
                    client.BindAddress);
                if (!probe.Up)
                {
                    servers.MarkDown(slot.Host);
                    lastError = $"{endpoint} {probe.Error}";
                    log.Report($"{Short(client.Adapter)}: {endpoint} is down ({probe.Error}). Moving on.");
                    continue;
                }

                log.Report($"{Short(client.Adapter)}: testing against {endpoint} ({probe.Milliseconds:0} ms away)…");
                try
                {
                    var result = await Iperf3Runner.RunAsync(exe, slot.Host, seconds, ct, client.BindAddress, slot.Port);
                    log.Report($"{Short(client.Adapter)}: finished on {endpoint}.");
                    return new ClientResult(client.Adapter, result, null, endpoint);
                }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    lastError = $"{endpoint}: {ex.Message}";
                    if (ex.Message.Contains("busy", StringComparison.OrdinalIgnoreCase))
                    {
                        servers.MarkBusy(slot.Host, slot.Port);
                        log.Report($"{Short(client.Adapter)}: {endpoint} is busy with someone else's test. Moving on.");
                    }
                    else
                    {
                        servers.MarkDown(slot.Host);
                        log.Report($"{Short(client.Adapter)}: {endpoint} failed ({ex.Message}). Moving on.");
                    }
                }
            }
        });
        return (await Task.WhenAll(tasks)).ToList();
    }

    static string Short(string adapter) => adapter.Length > 34 ? adapter[..33] + "…" : adapter;
}
