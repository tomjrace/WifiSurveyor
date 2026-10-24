using System.Globalization;
using WifiSurveyor.Capture;
using WifiSurveyor.Models;
using WifiSurveyor.Platform;
using WifiSurveyor.Positioning;
using WifiSurveyor.RF;
using WifiSurveyor.Services;
using WifiSurveyor.Surveying;

namespace WifiSurveyor;

public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        if (args.Length == 0 || args[0] is "-h" or "--help" or "help")
        {
            Usage();
            return 0;
        }

        try
        {
            return args[0].ToLowerInvariant() switch
            {
                "check" => await CheckAsync(),
                "interfaces" => await InterfacesAsync(),
                "scan" => await ScanAsync(Option(args, "--dev") ?? await FirstDeviceAsync()),
                "monitor" => await MonitorAsync(args),
                "restore" => await RestoreAsync(args),
                "capture" => await CaptureAsync(args),
                "measure" => await MeasureAsync(args),
                "report" => await ReportAsync(args),
                _ => Fail($"Unknown command \"{args[0]}\". Try --help.")
            };
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("Failed: " + ex.Message);
            return 1;
        }
    }

    static void Usage() => Console.WriteLine("""
        wifisurveyor — Wi-Fi surveying on Linux, sharing its file formats and analysis with the
        Windows application. Projects written here open there, and vice versa.

          check                        what's installed and usable on this machine
          interfaces                   wireless interfaces, their drivers, and monitor support
          scan [--dev wlan0]           list nearby access points
          monitor --dev wlan1 [--channel 36] [--keep-nm]
                                       put an interface into monitor mode
          restore --dev wlan1          hand it back to normal use
          capture --dev wlan1 --seconds 15 [--write out.pcapng]
                                       capture frames and report retries, airtime, and clients
          measure --project site.wsurvey --x 120 --y 340 [options]
                                       take a survey measurement and append it to the project
          report --project site.wsurvey --out report.html
                                       write the analysis report for a project

        measure options:
          --dev wlan0                  interface to scan with
          --scans 2                    Wi-Fi scan passes to average
          --rfe /dev/ttyUSB0           RF Explorer serial port, adds spectrum
          --dwell 3                    seconds parked on the busy channel (needs --rfe)
          --gps /dev/ttyACM0           NMEA GPS serial port
          --capture wlan1 --capture-seconds 10
                                       monitor interface for frame capture
        """);

    // ================= commands =================

    static async Task<int> CheckAsync()
    {
        Console.WriteLine($"Root: {(Shell.IsRoot ? "yes" : "no — scanning with iw, monitor mode, and capture need it")}");
        foreach (var tool in new[] { "iw", "ip", "nmcli", "tcpdump" })
            Console.WriteLine($"{tool,-10} {(Shell.Exists(tool) ? "found" : "missing")}");
        Console.WriteLine($"libpcap    {(LinuxPcap.IsAvailable ? "found" : "missing — install libpcap0.8 or libpcap")}");

        var interfaces = await LinuxWifi.InterfacesAsync();
        Console.WriteLine($"\nWireless interfaces: {interfaces.Count}");
        foreach (var iface in interfaces) Console.WriteLine("  " + iface);

        var busy = await LinuxMonitor.InterferingAsync();
        if (busy.Count > 0)
            Console.WriteLine("\nRunning and liable to interfere with monitor mode: " + string.Join(", ", busy) +
                              "\n  \"monitor\" releases the interface from NetworkManager for you.");

        foreach (var port in SerialPortsLinux.List())
            Console.WriteLine($"Serial: {port}");
        Console.WriteLine("\nVendor database: " + Wifi.OuiLookup.Describe());
        return 0;
    }

    static async Task<int> InterfacesAsync()
    {
        foreach (var iface in await LinuxWifi.InterfacesAsync()) Console.WriteLine(iface);
        return 0;
    }

    static async Task<int> ScanAsync(string device)
    {
        var readings = await LinuxWifi.ScanAsync(device);
        Console.WriteLine($"{readings.Count} access points on {device}\n");
        Console.WriteLine($"{"SSID",-28} {"BSSID",-18} {"Ch",4} {"Signal",7}  Vendor");
        foreach (var r in readings.OrderByDescending(r => r.Rssi))
            Console.WriteLine($"{Cut(r.Ssid.Length == 0 ? "(hidden)" : r.Ssid, 28),-28} {r.Bssid,-18} " +
                              $"{r.Channel,4} {r.Rssi,4} dBm  {Cut(r.Vendor, 24)}");
        return 0;
    }

    static async Task<int> MonitorAsync(string[] args)
    {
        var device = Option(args, "--dev") ?? await FirstDeviceAsync();
        int? channel = int.TryParse(Option(args, "--channel"), out var c) ? c : null;
        bool ok = await LinuxMonitor.EnableAsync(device, channel, !args.Contains("--keep-nm"), Log());
        return ok ? 0 : 1;
    }

    static async Task<int> RestoreAsync(string[] args)
    {
        await LinuxMonitor.RestoreAsync(Option(args, "--dev") ?? await FirstDeviceAsync(), Log());
        return 0;
    }

    static async Task<int> CaptureAsync(string[] args)
    {
        var device = Option(args, "--dev") ?? await FirstDeviceAsync();
        int seconds = int.TryParse(Option(args, "--seconds"), out var s) ? s : 15;

        Console.WriteLine($"Capturing on {device} for {seconds} s…");
        var frames = await LinuxPcap.CaptureAsync(device, TimeSpan.FromSeconds(seconds),
            new Progress<string>(line => Console.Write($"\r{line}   ")), CancellationToken.None);
        Console.WriteLine();

        var summary = CaptureAnalyzer.Analyze(frames, device);
        PrintCapture(summary);

        if (Option(args, "--write") is { Length: > 0 } path)
        {
            PcapWriter.Write(path, frames);
            Console.WriteLine($"\nWritten to {path} — it opens on the Capture tab of the Windows app.");
        }
        return 0;
    }

    static async Task<int> MeasureAsync(string[] args)
    {
        var projectPath = Option(args, "--project")
                          ?? throw new ArgumentException("--project is required.");
        var project = File.Exists(projectPath) ? ProjectStore.Load(projectPath) : new SurveyProject
        {
            Name = Path.GetFileNameWithoutExtension(projectPath)
        };

        var point = await LinuxSurvey.MeasureAsync(new LinuxSurvey.Options(
            Device: Option(args, "--dev") ?? await FirstDeviceAsync(),
            X: double.TryParse(Option(args, "--x"), NumberStyles.Float, CultureInfo.InvariantCulture, out var x) ? x : 0,
            Y: double.TryParse(Option(args, "--y"), NumberStyles.Float, CultureInfo.InvariantCulture, out var y) ? y : 0,
            Scans: int.TryParse(Option(args, "--scans"), out var scans) ? scans : 2,
            RfePort: Option(args, "--rfe"),
            DwellSeconds: int.TryParse(Option(args, "--dwell"), out var dwell) ? dwell : 0,
            GpsPort: Option(args, "--gps"),
            CaptureDevice: Option(args, "--capture"),
            CaptureSeconds: int.TryParse(Option(args, "--capture-seconds"), out var cs) ? cs : 10),
            Log());

        project.Points.Add(point);
        ProjectStore.Save(projectPath, project);
        Console.WriteLine($"\nPoint {project.Points.Count} saved to {projectPath}.");
        foreach (var note in point.Notes) Console.WriteLine("  note: " + note);
        return 0;
    }

    static Task<int> ReportAsync(string[] args)
    {
        var projectPath = Option(args, "--project") ?? throw new ArgumentException("--project is required.");
        var outPath = Option(args, "--out") ?? Path.ChangeExtension(projectPath, ".html");
        var project = ProjectStore.Load(projectPath);
        var ctx = new AnalysisContext(project);
        var filter = new MetricFilter(Option(args, "--ssid"), null, "All", null);

        var estimates = new List<TransmitterEstimate>();
        foreach (var (bssid, ssid, _) in Localizer.Candidates(project, filter.Ssid, null))
            if (Localizer.Estimate(project, Localizer.ApSamples(project, bssid),
                    ssid.Length > 0 ? ssid : bssid, "ap") is { } estimate)
                estimates.Add(estimate);
        var noise = NoiseFinder.Find(project, ctx);
        estimates.AddRange(noise.Select(n => n.Estimate).OfType<TransmitterEstimate>());

        ReportBuilder.Write(outPath, project, ctx, filter,
            MetricInfo.Get(HeatmapMetric.SignalStrength), null, estimates, noise, null);
        Console.WriteLine($"Report written to {outPath}");
        Console.WriteLine(NoiseFinder.Summarize(noise, project));
        return Task.FromResult(0);
    }

    // ================= helpers =================

    static void PrintCapture(CaptureSummary summary)
    {
        Console.WriteLine($"{summary.Frames:N0} frames over {summary.Seconds:0.0} s" +
                          (summary.Channel > 0 ? $", channel {summary.Channel}" : ""));
        Console.WriteLine($"  retries        {summary.RetryRate * 100:0.0}% of {summary.RetryEligible:N0}");
        Console.WriteLine($"  airtime        {summary.AirtimeFraction * 100:0.0}%");
        Console.WriteLine($"  clients heard  {summary.Clients}");
        if (summary.Deauths + summary.Disassocs > 0)
            Console.WriteLine($"  deauth/disassoc {summary.Deauths}/{summary.Disassocs}");

        if (summary.Bss.Count == 0) return;
        Console.WriteLine($"\n{"SSID",-24} {"BSSID",-18} {"Ch",4} {"Width",6} {"Retries",8} {"Airtime",8} Clients");
        foreach (var bss in summary.Bss.Take(15))
            Console.WriteLine($"{Cut(bss.Ssid, 24),-24} {bss.Bssid,-18} {bss.Channel,4} " +
                              $"{bss.WidthMHz,4} MHz {bss.RetryRate * 100,6:0.0}% {bss.AirtimeFraction * 100,6:0.0}% " +
                              $"{bss.Clients,6}");
    }

    static async Task<string> FirstDeviceAsync()
    {
        var interfaces = await LinuxWifi.InterfacesAsync();
        return interfaces.FirstOrDefault()?.Name
               ?? throw new InvalidOperationException("No wireless interface found.");
    }

    static string? Option(string[] args, string name)
    {
        int index = Array.FindIndex(args, a => a.Equals(name, StringComparison.OrdinalIgnoreCase));
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }

    static string Cut(string text, int width) =>
        text.Length <= width ? text : text[..(width - 1)] + "…";

    static Progress<string> Log() => new(line => Console.WriteLine("  " + line));

    static int Fail(string message)
    {
        Console.Error.WriteLine(message);
        return 1;
    }
}
