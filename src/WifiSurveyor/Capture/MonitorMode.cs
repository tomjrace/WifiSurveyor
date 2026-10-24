using System.Diagnostics;
using System.Management;
using System.Net.NetworkInformation;
using System.Security.Principal;
using Microsoft.Win32;
using WifiSurveyor.Services;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Capture;

public enum CheckState { Pass, Warn, Fail, Info }

public sealed record MonitorCheck(CheckState State, string Title, string Detail);

/// <summary>
/// Everything around monitor mode on Windows that an application can actually control, and
/// an honest account of what it can't.
///
/// Monitor mode on Windows exists only if the adapter's driver implements the optional NDIS
/// monitor extension. Nothing an application does can add it to a driver that lacks it;
/// tools that manage it anyway ship a driver of their own. What an application can do is
/// clear the obstacles that stop a capable driver from staying in monitor mode: missing
/// administrator rights, Npcap without raw 802.11 support, Windows scanning and reconnecting
/// the adapter, and other programs scanning through it.
/// </summary>
public static class MonitorMode
{
    static readonly string[] InterferingTools =
    {
        "inSSIDer", "NetSpot", "WiFiAnalyzer", "Homedale", "WirelessMon", "Ekahau", "HeatMapper",
        "Vistumbler", "NetStumbler", "WifiInfoView", "RaUI", "MtkWlanUtility", "Acrylic",
        "Wireshark", "dumpcap", "tshark"
    };

    public static bool IsAdministrator
    {
        get
        {
            try
            {
                using var identity = WindowsIdentity.GetCurrent();
                return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
            }
            catch
            {
                return false;
            }
        }
    }

    /// <summary>The Network Connections name ("Wi-Fi 2") for a WLAN interface.</summary>
    public static string ConnectionName(WlanInterfaceInfo iface)
    {
        try
        {
            var match = NetworkInterface.GetAllNetworkInterfaces()
                .FirstOrDefault(n => Guid.TryParse(n.Id.Trim('{', '}'), out var g) && g == iface.Id);
            if (match != null) return match.Name;
        }
        catch { /* fall through */ }
        return iface.Id.ToString("B");   // WlanHelper and netsh both accept the GUID
    }

    /// <summary>Npcap names its devices after the interface GUID.</summary>
    public static string NpcapDeviceName(WlanInterfaceInfo iface) =>
        @"\Device\NPF_" + iface.Id.ToString("B").ToUpperInvariant();

    // ================= checks =================

    public static async Task<List<MonitorCheck>> DiagnoseAsync(WlanInterfaceInfo iface)
    {
        var checks = new List<MonitorCheck>();
        var name = ConnectionName(iface);

        checks.Add(IsAdministrator
            ? new MonitorCheck(CheckState.Pass, "Running as administrator",
                "Changing an adapter's mode needs it, and so does capturing through Npcap by default.")
            : new MonitorCheck(CheckState.Fail, "Not running as administrator",
                "Switching modes will be refused. Use \"Restart as administrator\"."));

        checks.Add(CheckNpcap());
        checks.Add(CheckDriver(iface));
        checks.Add(await CheckDriverClaimAsync(name));
        checks.Add(await CheckSupportedModesAsync(name));
        checks.Add(await CheckCurrentModeAsync(name));

        checks.Add(iface.IsConnected
            ? new MonitorCheck(CheckState.Warn, "Adapter is connected to a network",
                "Windows keeps managing a connected adapter and will pull it back to normal mode. " +
                "\"Prepare and enable\" disconnects it and stops Windows from reconnecting it.")
            : new MonitorCheck(CheckState.Pass, "Adapter isn't connected", "Nothing is holding it in normal mode."));

        var tools = FindInterferingProcesses();
        checks.Add(tools.Count == 0
            ? new MonitorCheck(CheckState.Pass, "No other Wi-Fi tools running", "")
            : new MonitorCheck(CheckState.Warn, "Other Wi-Fi tools are running",
                string.Join(", ", tools.Select(p => p.ProcessName).Distinct()) +
                ". Anything that scans can knock the adapter out of monitor mode."));

        checks.Add(AppServices.Instance.ScanExclusions.Contains(iface.Id)
            ? new MonitorCheck(CheckState.Pass, "This app isn't scanning on it", "Its scans skip this adapter.")
            : new MonitorCheck(CheckState.Info, "This app still scans on it",
                "Discover and survey scans include it until you prepare it for monitor mode."));

        checks.Add(CheckThirdPartyCaptureDriver());
        return checks;
    }

    static MonitorCheck CheckNpcap()
    {
        if (!NpcapLive.IsInstalled)
            return new MonitorCheck(CheckState.Fail, "Npcap isn't installed",
                "Install it with \"Support raw 802.11 traffic (and monitor mode) for wireless adapters\" ticked.");

        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Services\npcap\Parameters");
            var dot11 = key?.GetValue("Dot11Support");
            var adminOnly = key?.GetValue("AdminOnly");
            if (dot11 is int d && d == 1)
                return new MonitorCheck(CheckState.Pass, "Npcap has raw 802.11 support",
                    adminOnly is int a && a == 1 ? "Restricted to administrators." : "");
            return new MonitorCheck(CheckState.Fail, "Npcap was installed without raw 802.11 support",
                "Reinstall Npcap and tick \"Support raw 802.11 traffic (and monitor mode) for wireless adapters\". " +
                "Without it, no adapter can capture in monitor mode through Npcap.");
        }
        catch
        {
            return new MonitorCheck(CheckState.Info, "Npcap installed", "Couldn't read its options.");
        }
    }

    static MonitorCheck CheckDriver(WlanInterfaceInfo iface)
    {
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT DeviceName, DriverProviderName, DriverVersion, DriverDate FROM Win32_PnPSignedDriver " +
                "WHERE DeviceClass='NET'");
            foreach (var item in searcher.Get())
            {
                var device = item["DeviceName"] as string ?? "";
                if (!device.Equals(iface.Description, StringComparison.OrdinalIgnoreCase)) continue;

                var provider = item["DriverProviderName"] as string ?? "unknown";
                var version = item["DriverVersion"] as string ?? "";
                var date = item["DriverDate"] as string;
                var when = date is { Length: >= 8 } ? $", dated {date[..4]}-{date[4..6]}-{date[6..8]}" : "";
                bool inbox = provider.Contains("Microsoft", StringComparison.OrdinalIgnoreCase);
                return new MonitorCheck(inbox ? CheckState.Warn : CheckState.Info,
                    $"Driver: {provider} {version}{when}",
                    inbox
                        ? "This is Windows' generic driver. Vendor drivers are more likely to include monitor mode, " +
                          "so if the vendor offers one, try it."
                        : "Monitor mode lives or dies in this driver. A different version (older ones sometimes " +
                          "kept it) can change the answer.");
            }
        }
        catch { /* fall through */ }
        return new MonitorCheck(CheckState.Info, "Driver details unavailable", "");
    }

    static async Task<MonitorCheck> CheckDriverClaimAsync(string name)
    {
        var (_, output) = await CommandRunner.RunAsync("netsh", $"wlan show drivers interface=\"{name}\"",
            TimeSpan.FromSeconds(20), CancellationToken.None);
        var line = output.Split('\n')
            .Select(l => l.Trim())
            .FirstOrDefault(l => l.Contains("monitor", StringComparison.OrdinalIgnoreCase));

        if (line == null)
            return new MonitorCheck(CheckState.Info, "Windows didn't report on monitor mode",
                "This build of netsh doesn't say either way; the supported-modes check below is the better test.");
        bool yes = line.EndsWith("Yes", StringComparison.OrdinalIgnoreCase);
        return new MonitorCheck(yes ? CheckState.Pass : CheckState.Fail,
            yes ? "Driver says it supports monitor mode" : "Driver says it does not support monitor mode",
            line);
    }

    static async Task<MonitorCheck> CheckSupportedModesAsync(string name)
    {
        var helper = WlanHelperPath;
        if (helper == null)
            return new MonitorCheck(CheckState.Info, "Supported modes unknown", "WlanHelper.exe comes with Npcap.");

        var (_, output) = await CommandRunner.RunAsync(helper, $"\"{name}\" modes", TimeSpan.FromSeconds(20),
            CancellationToken.None);
        var text = output.Trim();
        if (text.Contains("monitor", StringComparison.OrdinalIgnoreCase))
            return new MonitorCheck(CheckState.Pass, "Driver offers monitor mode", text);
        if (text.Contains("not supported", StringComparison.OrdinalIgnoreCase) ||
            text.Contains("managed", StringComparison.OrdinalIgnoreCase))
            return new MonitorCheck(CheckState.Fail, "Driver doesn't offer monitor mode",
                (text.Length > 0 ? text + "\n" : "") +
                "This is the \"adapter not supported\" you're seeing, and no setting in Windows or this app " +
                "changes it. The ways round it are below.");
        return new MonitorCheck(CheckState.Info, "Supported modes", text.Length > 0 ? text : "No answer.");
    }

    static async Task<MonitorCheck> CheckCurrentModeAsync(string name)
    {
        var helper = WlanHelperPath;
        if (helper == null) return new MonitorCheck(CheckState.Info, "Current mode unknown", "");
        var (_, output) = await CommandRunner.RunAsync(helper, $"\"{name}\" mode", TimeSpan.FromSeconds(20),
            CancellationToken.None);
        var text = output.Trim();
        return new MonitorCheck(
            text.Contains("monitor", StringComparison.OrdinalIgnoreCase) ? CheckState.Pass : CheckState.Info,
            "Current mode", text.Length > 0 ? text : "No answer.");
    }

    static MonitorCheck CheckThirdPartyCaptureDriver()
    {
        // Acrylic's NDIS monitor driver shows up as its own capture interface when installed.
        try
        {
            if (NpcapLive.IsInstalled &&
                NpcapLive.ListDevices().FirstOrDefault(d =>
                    d.Description.Contains("Acrylic", StringComparison.OrdinalIgnoreCase) ||
                    d.Description.Contains("Tarlogic", StringComparison.OrdinalIgnoreCase)) is { } device)
                return new MonitorCheck(CheckState.Pass, "A third-party monitor driver is installed",
                    $"{device.Description}. Choose it in the capture adapter list and capture as administrator.");
        }
        catch { /* fall through */ }
        return new MonitorCheck(CheckState.Info, "No third-party monitor driver found",
            "Tools that do monitor mode on adapters whose own driver lacks it install a driver of their own. " +
            "If you install one, its capture interface appears in the adapter list here.");
    }

    public static List<Process> FindInterferingProcesses()
    {
        var found = new List<Process>();
        int self = Environment.ProcessId;
        foreach (var process in Process.GetProcesses())
        {
            try
            {
                if (process.Id == self) continue;
                if (InterferingTools.Any(t => process.ProcessName.Contains(t, StringComparison.OrdinalIgnoreCase)))
                    found.Add(process);
            }
            catch { /* process exited or access denied */ }
        }
        return found;
    }

    static string? WlanHelperPath
    {
        get
        {
            var path = Path.Combine(NpcapLive.NpcapDirectory, "WlanHelper.exe");
            return File.Exists(path) ? path : null;
        }
    }

    // ================= enable and restore =================

    public sealed record PrepareOptions(bool StopWindowsManaging, bool PauseOurScans, int? Channel);

    public static async Task<bool> PrepareAsync(WlanInterfaceInfo iface, PrepareOptions options, IProgress<string> log)
    {
        var name = ConnectionName(iface);
        if (!IsAdministrator)
        {
            log.Report("Not running as administrator, so Windows will refuse the mode change. " +
                       "Use \"Restart as administrator\" first.");
            return false;
        }

        if (options.PauseOurScans)
        {
            AppServices.Instance.ScanExclusions.Add(iface.Id);
            log.Report("This app's scans now skip the adapter.");
        }

        if (options.StopWindowsManaging)
        {
            await Run(log, "netsh", $"wlan disconnect interface=\"{name}\"", "Disconnecting it");
            await Run(log, "netsh", $"wlan set autoconfig enabled=no interface=\"{name}\"",
                "Stopping Windows from scanning and reconnecting on it");
        }

        if (WlanHelperPath is not { } helper)
        {
            log.Report("WlanHelper.exe isn't installed; it comes with Npcap.");
            return false;
        }

        var (_, result) = await CommandRunner.RunAsync(helper, $"\"{name}\" mode monitor",
            TimeSpan.FromSeconds(20), CancellationToken.None);
        log.Report("Switching to monitor mode: " + (result.Trim().Length > 0 ? result.Trim() : "no reply"));

        if (options.Channel is int channel)
        {
            var (_, channelResult) = await CommandRunner.RunAsync(helper, $"\"{name}\" channel {channel}",
                TimeSpan.FromSeconds(20), CancellationToken.None);
            log.Report($"Channel {channel}: " + (channelResult.Trim().Length > 0 ? channelResult.Trim() : "no reply"));
        }

        var (_, mode) = await CommandRunner.RunAsync(helper, $"\"{name}\" mode", TimeSpan.FromSeconds(20),
            CancellationToken.None);
        bool ok = mode.Contains("monitor", StringComparison.OrdinalIgnoreCase);
        log.Report(ok
            ? "Confirmed: the adapter is in monitor mode."
            : $"The adapter reports \"{mode.Trim()}\". The driver didn't accept monitor mode.");
        return ok;
    }

    public static async Task RestoreAsync(WlanInterfaceInfo iface, IProgress<string> log)
    {
        var name = ConnectionName(iface);
        if (WlanHelperPath is { } helper)
        {
            var (_, result) = await CommandRunner.RunAsync(helper, $"\"{name}\" mode managed",
                TimeSpan.FromSeconds(20), CancellationToken.None);
            log.Report("Back to normal mode: " + (result.Trim().Length > 0 ? result.Trim() : "no reply"));
        }
        await Run(log, "netsh", $"wlan set autoconfig enabled=yes interface=\"{name}\"",
            "Letting Windows manage it again");
        AppServices.Instance.ScanExclusions.Remove(iface.Id);
        log.Report("This app scans on it again. Windows will reconnect it to a saved network by itself.");
    }

    public static int CloseProcesses(IEnumerable<Process> processes, IProgress<string> log)
    {
        int closed = 0;
        foreach (var process in processes)
        {
            try
            {
                var name = process.ProcessName;
                // Ask politely first, so the program can save; only then insist.
                if (!process.CloseMainWindow() || !process.WaitForExit(4000))
                {
                    process.Kill(entireProcessTree: true);
                    process.WaitForExit(3000);
                }
                closed++;
                log.Report($"Closed {name}.");
            }
            catch (Exception ex)
            {
                log.Report($"Couldn't close {process.ProcessName}: {ex.Message}");
            }
        }
        return closed;
    }

    static async Task Run(IProgress<string> log, string file, string arguments, string what)
    {
        var (code, output) = await CommandRunner.RunAsync(file, arguments, TimeSpan.FromSeconds(20),
            CancellationToken.None);
        var text = output.Trim();
        log.Report($"{what}: {(text.Length > 0 ? text : code == 0 ? "done" : $"code {code}")}");
    }

    /// <summary>Starts a second copy elevated. The caller closes this one.</summary>
    public static bool StartElevatedCopy()
    {
        try
        {
            var path = Environment.ProcessPath;
            if (path == null) return false;
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true, Verb = "runas" });
            return true;
        }
        catch
        {
            return false;   // the prompt was dismissed
        }
    }
}
