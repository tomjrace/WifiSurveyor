using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using WifiSurveyor.Capture;
using WifiSurveyor.Models;
using WifiSurveyor.RF;
using WifiSurveyor.Services;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

public sealed class CaptureOptions
{
    public int WifiScans { get; set; } = 2;
    public bool UseSpectrum { get; set; }
    public int SweepsPerSegment { get; set; } = 15;
    /// <summary>Seconds parked on the serving channel for a reliable airtime figure. 0 disables it.</summary>
    public int DwellSeconds { get; set; }

    /// <summary>Seconds of monitor-mode capture on the serving channel. 0 disables it.</summary>
    public int PcapSeconds { get; set; }
    public string? PcapDevice { get; set; }
    public string PcapAdapterName { get; set; } = "Wi-Fi";

    public bool RunIperf { get; set; }
    public string IperfPath { get; set; } = "iperf3.exe";
    public string IperfServer { get; set; } = "";
    public int IperfSeconds { get; set; } = 3;
}

/// <summary>What a capture is doing right now, and the whole list of steps it will take.</summary>
public sealed record CaptureProgress(string Current, IReadOnlyList<string> Plan, int Index);

public static class SurveyEngine
{
    public static async Task<SurveyPoint> CaptureAsync(double x, double y, CaptureOptions o,
        IProgress<CaptureProgress> progress, CancellationToken ct)
    {
        var svc = AppServices.Instance;
        var point = new SurveyPoint { X = x, Y = y, Time = DateTime.Now };

        // Work out the whole plan up front so the UI can show what's coming, not just what's running.
        bool spectrum = o.UseSpectrum && svc.Rfe is { IsOpen: true };
        bool willCaptureFrames = o.PcapSeconds > 0 && !string.IsNullOrWhiteSpace(o.PcapDevice);
        var plan = new List<string>
        {
            o.WifiScans > 1 ? $"Scanning access points ({o.WifiScans} passes)" : "Scanning access points"
        };
        if (spectrum) plan.Add("Measuring the RF spectrum");
        if (spectrum && o.DwellSeconds > 0) plan.Add("Listening to the busy channel");
        if (willCaptureFrames) plan.Add("Capturing frames");
        if (o.RunIperf) plan.Add("Testing throughput");

        int step = 0;
        void Report(string detail) => progress.Report(new CaptureProgress(detail, plan, step));

        if (svc.Location?.LastFix is { IsFresh: true } fix) point.Gps = fix;

        // Wi-Fi: several scans, averaged per BSSID
        var readings = new List<WifiReading>();
        for (int i = 0; i < o.WifiScans; i++)
        {
            ct.ThrowIfCancellationRequested();
            Report(o.WifiScans > 1 ? $"Wi-Fi scan {i + 1} of {o.WifiScans}" : "Scanning");
            // Each pass is already merged across adapters, so this averages over time, not radios.
            readings.AddRange(await Task.Run(() => WifiScanner.ScanAsync(svc.Settings.MergeAdapterScans), ct));
            if (i < o.WifiScans - 1) await Task.Delay(TimeSpan.FromSeconds(1), ct);
        }
        point.Networks = readings
            .GroupBy(r => r.Bssid)
            .Select(g => g.Last() with { Rssi = (int)Math.Round(g.Average(r => r.Rssi)) })
            .OrderByDescending(r => r.Rssi)
            .ToList();
        if (point.Networks.Count == 0)
            point.Notes.Add("The Wi-Fi scan returned no networks. Check that Location access is on.");
        svc.LastScan = point.Networks;
        point.Links = WlanConnection.AllLinks()
            .Where(l => l.Connection != null)
            .Select(l => l.Connection!)
            .ToList();
        point.Connection = point.Links.FirstOrDefault();

        // Spectrum
        if (o.UseSpectrum)
        {
            var rfe = svc.Rfe;
            if (rfe is { IsOpen: true })
            {
                step++;
                Report("Waiting for the analyzer");
                await svc.AnalyzerLock.WaitAsync(ct);
                svc.SurveyUsingSpectrum = true;
                try
                {
                    var cap = new SpectrumCapture();
                    foreach (var (start, end) in SpectrumPlan.Segments)
                    {
                        Report($"Sweeping {start:0}–{end:0} MHz");
                        try
                        {
                            cap.Segments.Add(await rfe.CaptureSegmentAsync(start, end, o.SweepsPerSegment, ct));
                        }
                        catch (Exception ex) when (ex is TimeoutException or InvalidOperationException)
                        {
                            point.Notes.Add($"Spectrum {start:0}–{end:0} MHz skipped: {ex.Message}");
                        }
                    }
                    cap.NoiseFloor24 = SpectrumAnalysis.BandNoiseFloor(cap, "2.4");
                    cap.NoiseFloor5 = SpectrumAnalysis.BandNoiseFloor(cap, "5");

                    // Park on the channel actually in use: a swept analyzer revisits a wide range
                    // too slowly to see short bursts, so wideband airtime figures undercount.
                    if (o.DwellSeconds > 0 && SurveyEngineHelpers.ServingChannel(point) is { } serving)
                    {
                        step++;
                        Report($"Channel {serving.Number}, {o.DwellSeconds} s");
                        try
                        {
                            double? floor = serving.Band == "2.4" ? cap.NoiseFloor24 : cap.NoiseFloor5;
                            cap.Dwell = await rfe.CaptureDwellAsync(serving.CenterMHz,
                                TimeSpan.FromSeconds(o.DwellSeconds), ct, floor);
                            cap.DwellCenterMHz = serving.CenterMHz;
                        }
                        catch (Exception ex) when (ex is TimeoutException or InvalidOperationException)
                        {
                            point.Notes.Add($"Channel {serving.Number} listen skipped: {ex.Message}");
                        }
                    }

                    if (cap.Segments.Count > 0) point.Spectrum = cap;
                }
                finally
                {
                    svc.AnalyzerLock.Release();
                    svc.ReleaseSpectrum();
                }
            }
            else
            {
                point.Notes.Add("RF Explorer isn't connected, so the spectrum was skipped.");
            }
        }

        // Monitor-mode capture: retries, airtime, and client counts come from the frames themselves
        if (o.PcapSeconds > 0 && !string.IsNullOrWhiteSpace(o.PcapDevice) &&
            SurveyEngineHelpers.ServingChannel(point) is { } captureChannel)
        {
            step++;
            Report($"Channel {captureChannel.Number}, {o.PcapSeconds} s");
            try
            {
                NpcapLive.SetMonitorMode(o.PcapAdapterName, true, captureChannel.Number);
                var frames = await NpcapLive.CaptureAsync(o.PcapDevice!, TimeSpan.FromSeconds(o.PcapSeconds),
                    true, null, ct);
                var summary = CaptureAnalyzer.Analyze(frames, o.PcapDevice!);
                if (summary.Frames > 0) point.Capture = summary;
                else point.Notes.Add("The capture adapter returned no frames; monitor mode may not be active.");
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                point.Notes.Add("Frame capture skipped: " + ex.Message);
            }
        }

        // Throughput
        if (o.RunIperf)
        {
            step++;
            Report("Running iperf3");
            try
            {
                var t = await Iperf3Runner.RunAsync(o.IperfPath, o.IperfServer, o.IperfSeconds, ct);
                point.ThroughputDownMbps = t.DownMbps;
                point.ThroughputUpMbps = t.UpMbps;
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                point.Notes.Add("iperf3: " + ex.Message);
            }
        }

        return point;
    }
}

public static class SurveyEngineHelpers
{
    /// <summary>The channel worth listening to: the connected one, else the strongest network.</summary>
    public static WifiChannels.WifiChannel? ServingChannel(SurveyPoint p)
    {
        var connected = p.Connection != null
            ? p.Networks.FirstOrDefault(n => n.Bssid == p.Connection.Bssid)
            : null;
        var target = connected ?? p.Networks.MaxBy(n => n.Rssi);
        return target == null ? null : RfAnalysis.ChannelOf(target);
    }
}

public sealed record ThroughputResult(double DownMbps, double UpMbps);

/// <summary>One adapter and the servers it should try, in order.</summary>
public sealed record ClientTarget(string Adapter, string BindAddress, IReadOnlyList<(string Host, int Port)> Servers);

public sealed record ClientResult(string Adapter, ThroughputResult? Result, string? Error, string Server);

public static class Iperf3Runner
{
    public static async Task<ThroughputResult> RunAsync(string exe, string server, int seconds,
        CancellationToken ct, string? bindAddress = null, int port = 5201)
    {
        if (string.IsNullOrWhiteSpace(server))
            throw new InvalidOperationException("Set an iperf3 server on the Devices tab.");
        double up = await RunOnceAsync(exe, server, seconds, reverse: false, ct, bindAddress, port);
        double down = await RunOnceAsync(exe, server, seconds, reverse: true, ct, bindAddress, port);
        return new ThroughputResult(down, up);
    }

    /// <summary>
    /// Runs one test per adapter at the same time, so the numbers show how the cell behaves
    /// under several clients rather than one. Each adapter works down its own list of servers
    /// until one answers, which keeps a single unreachable public server from spoiling the run.
    /// </summary>
    public static async Task<List<ClientResult>> RunParallelAsync(string exe, int seconds,
        IReadOnlyList<ClientTarget> clients, CancellationToken ct)
    {
        var tasks = clients.Select(async client =>
        {
            string? lastError = null;
            foreach (var (host, port) in client.Servers)
            {
                ct.ThrowIfCancellationRequested();
                try
                {
                    var result = await RunAsync(exe, host, seconds, ct, client.BindAddress, port);
                    return new ClientResult(client.Adapter, result, null, $"{host}:{port}");
                }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    lastError = ex.Message;
                }
            }
            return new ClientResult(client.Adapter, null, lastError ?? "No server to test against.", "");
        });
        return (await Task.WhenAll(tasks)).ToList();
    }

    static async Task<double> RunOnceAsync(string exe, string server, int seconds, bool reverse,
        CancellationToken ct, string? bindAddress = null, int port = 5201)
    {
        var psi = new ProcessStartInfo(exe)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        foreach (var arg in new[] { "-c", server.Trim(), "-t", seconds.ToString(CultureInfo.InvariantCulture),
                     "-p", port.ToString(CultureInfo.InvariantCulture), "-J" })
            psi.ArgumentList.Add(arg);
        if (reverse) psi.ArgumentList.Add("-R");
        if (!string.IsNullOrWhiteSpace(bindAddress))
        {
            // Binding to the adapter's own address is what forces traffic onto that radio.
            psi.ArgumentList.Add("-B");
            psi.ArgumentList.Add(bindAddress);
        }

        Process proc;
        try
        {
            proc = Process.Start(psi) ?? throw new InvalidOperationException("iperf3 did not start.");
        }
        catch (Win32Exception)
        {
            throw new InvalidOperationException($"iperf3 wasn't found at \"{exe}\".");
        }

        using (proc)
        using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct))
        {
            cts.CancelAfter(TimeSpan.FromSeconds(seconds + 15));
            var stdout = proc.StandardOutput.ReadToEndAsync(cts.Token);
            var stderr = proc.StandardError.ReadToEndAsync(cts.Token);
            try
            {
                await proc.WaitForExitAsync(cts.Token);
            }
            catch (OperationCanceledException)
            {
                try { proc.Kill(entireProcessTree: true); } catch { /* already exited */ }
                if (ct.IsCancellationRequested) throw;
                throw new TimeoutException("iperf3 didn't finish in time.");
            }

            var json = await stdout;
            if (string.IsNullOrWhiteSpace(json))
                throw new InvalidOperationException((await stderr).Trim());

            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.TryGetProperty("error", out var err))
                throw new InvalidOperationException(err.GetString());

            var end = root.GetProperty("end");
            var sum = end.TryGetProperty("sum_received", out var r) ? r : end.GetProperty("sum");
            return sum.GetProperty("bits_per_second").GetDouble() / 1e6;
        }
    }
}
