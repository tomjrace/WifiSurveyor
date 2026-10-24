using System.IO.Ports;
using System.Management;
using System.Text.Json;
using System.Text.RegularExpressions;
using WifiSurveyor.Models;
using WifiSurveyor.Positioning;
using WifiSurveyor.RF;

namespace WifiSurveyor.Services;

public static class Recovery
{
    public static string ProjectPath => Path.Combine(AppPaths.DataDir, "autosave.wsurvey");
    public static string MarkerPath => Path.Combine(AppPaths.DataDir, "autosave.txt");

    public static bool Exists => File.Exists(ProjectPath);

    public static (DateTime When, string? OriginalPath)? Describe()
    {
        try
        {
            if (!Exists) return null;
            var when = File.GetLastWriteTime(ProjectPath);
            string? original = File.Exists(MarkerPath) ? File.ReadAllText(MarkerPath).Trim() : null;
            return (when, string.IsNullOrWhiteSpace(original) ? null : original);
        }
        catch
        {
            return null;
        }
    }

    public static void Clear()
    {
        foreach (var path in new[] { ProjectPath, MarkerPath })
            try { if (File.Exists(path)) File.Delete(path); } catch { /* best effort */ }
    }
}

public class AppSettings
{
    public string IperfPath { get; set; } = "iperf3.exe";
    public string IperfServer { get; set; } = "";
    public int IperfSeconds { get; set; } = 3;
    public int IperfPort { get; set; } = 5201;
    public string? RfePort { get; set; }
    public int RfeBaud { get; set; } = 500000;
    public string? GpsPort { get; set; }
    public int GpsBaud { get; set; } = 9600;
    public string? PcapDevice { get; set; }
    public string PcapAdapterName { get; set; } = "Wi-Fi";
    public int PcapSeconds { get; set; } = 8;
    public string? IperfRegion { get; set; }

    /// <summary>Public servers ticked for the multi-adapter load test, as host:port.</summary>
    public List<string> ServerPool { get; set; } = new();

    // What "good" means here. Every verdict and score is measured against these.
    /// <summary>Keep one reading per BSSID when several adapters scan at once.</summary>
    public bool MergeAdapterScans { get; set; } = true;

    public double TargetRssi { get; set; } = -70;
    public double TargetSnr { get; set; } = 20;
    public double TargetAirtime { get; set; } = 45;
    public double TargetPhyRate { get; set; } = 100;

    static string FilePath => Path.Combine(AppPaths.DataDir, "settings.json");

    public static AppSettings Load()
    {
        AppSettings settings;
        try
        {
            settings = File.Exists(FilePath)
                ? JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(FilePath)) ?? new()
                : new();
        }
        catch
        {
            settings = new();   // fall back to defaults
        }

        // Point at the bundled copy when the user hasn't chosen one of their own.
        if (string.IsNullOrWhiteSpace(settings.IperfPath) || settings.IperfPath == "iperf3.exe")
            settings.IperfPath = ToolPaths.ResolveIperf3(null);
        return settings;
    }

    public void Save()
    {
        try
        {
            File.WriteAllText(FilePath,
                JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { /* settings are best-effort */ }
    }
}

/// <summary>
/// Holds the connected devices so every tab uses the same instances.
/// Events may fire on background threads; subscribers marshal to the UI.
/// </summary>
public sealed class AppServices
{
    public static AppServices Instance { get; } = new();

    public AppSettings Settings { get; } = AppSettings.Load();
    public RfExplorerDevice? Rfe { get; private set; }
    public ILocationProvider? Location { get; private set; }
    public IReadOnlyList<WifiReading> LastScan { get; set; } = Array.Empty<WifiReading>();

    /// <summary>Adapters in monitor mode: this app's scans leave them alone.</summary>
    public HashSet<Guid> ScanExclusions { get; } = new();

    /// <summary>The point selected on the Survey tab, so a capture can be attached to it.</summary>
    public SurveyPoint? SelectedPoint { get; set; }

    /// <summary>Held by whoever is reconfiguring or reading the analyzer (survey capture or live cycling).</summary>
    public SemaphoreSlim AnalyzerLock { get; } = new(1, 1);

    /// <summary>True while a survey capture is driving the analyzer; the live view pauses.</summary>
    public bool SurveyUsingSpectrum { get; set; }

    public event Action? DevicesChanged;
    public event Action? PointsChanged;
    public event Action? SpectrumReleased;

    public void SetRfe(RfExplorerDevice? device)
    {
        if (!ReferenceEquals(Rfe, device)) Rfe?.Dispose();
        Rfe = device;
        DevicesChanged?.Invoke();
    }

    public void SetLocation(ILocationProvider? provider)
    {
        if (!ReferenceEquals(Location, provider)) Location?.Dispose();
        Location = provider;
        DevicesChanged?.Invoke();
    }

    public void NotifyPointsChanged() => PointsChanged?.Invoke();

    public void ReleaseSpectrum()
    {
        SurveyUsingSpectrum = false;
        SpectrumReleased?.Invoke();
    }

    public void Shutdown()
    {
        Rfe?.Dispose();
        Location?.Dispose();
        Settings.Save();
    }
}

public record PortInfo(string Port, string Display)
{
    public override string ToString() => Display;
}

public static class SerialPorts
{
    /// <summary>COM ports with friendly names (e.g. "Silicon Labs CP210x … (COM5)").</summary>
    public static List<PortInfo> List()
    {
        var friendly = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT Name FROM Win32_PnPEntity WHERE Name LIKE '%(COM%'");
            foreach (var obj in searcher.Get())
            {
                if (obj["Name"] is not string name) continue;
                var m = Regex.Match(name, @"\((COM\d+)\)");
                if (m.Success) friendly[m.Groups[1].Value] = name;
            }
        }
        catch { /* WMI unavailable: plain names are fine */ }

        return SerialPort.GetPortNames()
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderBy(p => p.Length).ThenBy(p => p, StringComparer.OrdinalIgnoreCase)
            .Select(p => new PortInfo(p, friendly.TryGetValue(p, out var f) ? f : p))
            .ToList();
    }
}
