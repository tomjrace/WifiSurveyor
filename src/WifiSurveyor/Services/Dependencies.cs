using System.Diagnostics;
using System.Management;
using Microsoft.Win32;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Services;

public enum DependencyState { Ok, Warning, Missing, Unknown }

public sealed record DependencyStatus(
    string Name,
    DependencyState State,
    string Detail,
    string? ActionLabel = null,
    string? Url = null,
    string? WingetId = null,
    string? Task = null)
{
    public bool Required { get; init; }
}

/// <summary>
/// Works out what's present on this machine. Everything here is read-only: it looks at
/// devices, files, and registry keys, and never changes anything.
/// </summary>
public static class DependencyChecker
{
    public static Task<List<DependencyStatus>> CheckAllAsync() => Task.Run(() => new List<DependencyStatus>
    {
        CheckWifiAdapters(),
        CheckLocationPermission(),
        CheckIperf(),
        CheckCp210x(),
        CheckGps(),
        CheckAdapter("Alfa AWUS036ACM (MediaTek MT7612U)",
            new[] { "7612", "AWUS036ACM", "MT7612" },
            "https://www.alfa.com.tw/products/awus036acm"),
        CheckAdapter("Alfa AWUS036AXML (MediaTek MT7921AU)",
            // Names vary by driver: MT7921/7922, Wi-Fi 6E, AXE3000, and the USB product IDs
            new[] { "7961", "7922", "7902", "AWUS036AXML", "MT7921", "MT7922", "AXE3000", "AX3000", "Wi-Fi 6E" },
            "https://www.alfa.com.tw/products/awus036axml", "axml-drivers"),
        CheckOui(),
        CheckWinget(),
        CheckPendingRestart()
    });

    static DependencyStatus CheckWifiAdapters()
    {
        var interfaces = WlanConnection.ListInterfaces();
        return interfaces.Count == 0
            ? new DependencyStatus("Wi-Fi adapter", DependencyState.Missing,
                "Windows reports no WLAN adapter. Nothing in this app will work without one.") { Required = true }
            : new DependencyStatus("Wi-Fi adapter", DependencyState.Ok,
                $"{interfaces.Count} adapter(s): " + string.Join(", ", interfaces.Select(i => i.Description)))
            { Required = true };
    }

    static DependencyStatus CheckLocationPermission()
    {
        // Windows hides scan results from apps without location access.
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(
                @"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location");
            var value = key?.GetValue("Value") as string;
            if (string.Equals(value, "Allow", StringComparison.OrdinalIgnoreCase))
                return new DependencyStatus("Location access", DependencyState.Ok,
                    "Allowed, so Wi-Fi scans return results.") { Required = true };

            return new DependencyStatus("Location access", DependencyState.Missing,
                "Off or unknown. Windows returns empty scan results until desktop apps may use location.",
                "Open location settings", "ms-settings:privacy-location") { Required = true };
        }
        catch
        {
            return new DependencyStatus("Location access", DependencyState.Unknown,
                "Couldn't read the setting. Check it by hand if scans come back empty.",
                "Open location settings", "ms-settings:privacy-location");
        }
    }

    static DependencyStatus CheckIperf()
    {
        if (ToolPaths.BundledIperf3 is { } bundled)
            return new DependencyStatus("iperf3", DependencyState.Ok, $"Bundled with this build: {bundled}");

        var configured = AppServices.Instance.Settings.IperfPath;
        if (!string.IsNullOrWhiteSpace(configured) && File.Exists(configured))
            return new DependencyStatus("iperf3", DependencyState.Ok, configured);

        foreach (var directory in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            try
            {
                if (directory.Length > 0 && File.Exists(Path.Combine(directory, "iperf3.exe")))
                    return new DependencyStatus("iperf3", DependencyState.Ok,
                        Path.Combine(directory, "iperf3.exe"));
            }
            catch { /* a malformed PATH entry is not worth failing over */ }
        }

        return new DependencyStatus("iperf3", DependencyState.Warning,
            "Not found. Throughput maps and load tests need it; everything else works without it.",
            "Install", "https://github.com/ar51an/iperf3-win-builds", "ar51an.iPerf3");
    }

    static DependencyStatus CheckCp210x()
    {
        var devices = FindDevices("CP210", "VID_10C4");
        if (devices.Count == 0)
            return new DependencyStatus("RF Explorer driver (Silicon Labs CP210x)", DependencyState.Warning,
                "No CP210x device seen. Plug the RF Explorer in and switch it on; if no COM port appears, " +
                "install the driver.",
                "Open the driver page",
                "https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers");

        var broken = devices.FirstOrDefault(d => d.Problem);
        return broken != null
            ? new DependencyStatus("RF Explorer driver (Silicon Labs CP210x)", DependencyState.Missing,
                $"{broken.Name} is present but has no working driver (code {broken.ErrorCode}).",
                "Open the driver page",
                "https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers")
            : new DependencyStatus("RF Explorer driver (Silicon Labs CP210x)", DependencyState.Ok,
                string.Join(", ", devices.Select(d => d.Name)));
    }

    static DependencyStatus CheckGps()
    {
        var devices = FindDevices("GPS", "GNSS", "u-blox", "ublox", "Prolific", "CH340", "GlobalSat", "Garmin");
        if (devices.Count > 0)
        {
            var broken = devices.FirstOrDefault(d => d.Problem);
            return broken != null
                ? new DependencyStatus("GPS receiver", DependencyState.Missing,
                    $"{broken.Name} has no working driver (code {broken.ErrorCode}). Most dongles use a " +
                    "Prolific, Silicon Labs, or CH340 serial chip; install that chip's driver.")
                : new DependencyStatus("GPS receiver", DependencyState.Ok,
                    string.Join(", ", devices.Select(d => d.Name)));
        }

        var ports = SerialPorts.List();
        return ports.Count > 0
            ? new DependencyStatus("GPS receiver", DependencyState.Warning,
                "No obvious GPS device, but there are COM ports. Use \"Find GPS port\" on the Devices tab; " +
                "many dongles appear only as a generic serial port.")
            : new DependencyStatus("GPS receiver", DependencyState.Warning,
                "No serial ports at all. Plug the dongle in; Windows usually supplies the driver itself.");
    }

    static DependencyStatus CheckAdapter(string name, string[] patterns, string url, string? task = null)
    {
        var devices = FindDevices(patterns);
        bool canInstall = task != null;

        if (devices.Count == 0)
        {
            // Say what wireless hardware is actually here, so a name we don't recognise is obvious.
            var others = WirelessDevices();
            var seen = others.Count > 0
                ? " Wireless devices Windows reports: " + string.Join("; ", others.Select(d => d.Name)) + "."
                : "";
            return new DependencyStatus(name, DependencyState.Warning,
                "Not plugged in, or its name isn't one this check recognises." + seen +
                (canInstall ? " If it's working already, ignore this row." : ""),
                canInstall ? "Install drivers" : "Open the product page", url, null, task);
        }

        var broken = devices.FirstOrDefault(d => d.Problem);
        return broken != null
            ? new DependencyStatus(name, DependencyState.Missing,
                $"Present but without a working driver (code {broken.ErrorCode}). " +
                (canInstall
                    ? "The vendor's Wi-Fi and Bluetooth drivers can be fetched and installed from here."
                    : "Windows Update usually has the MediaTek driver; otherwise take it from the product page."),
                canInstall ? "Install drivers" : "Open the product page", url, null, task)
            : new DependencyStatus(name, DependencyState.Ok, string.Join(", ", devices.Select(d => d.Name)));
    }

    /// <summary>Every network-class device, used to report what's present when a match fails.</summary>
    static List<Device> WirelessDevices()
    {
        var found = new List<Device>();
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT Name, ConfigManagerErrorCode FROM Win32_PnPEntity WHERE PNPClass='Net'");
            foreach (var item in searcher.Get())
            {
                var deviceName = item["Name"] as string ?? "";
                if (deviceName.Length == 0) continue;
                // Skip the virtual clutter Windows always has
                if (deviceName.Contains("Virtual", StringComparison.OrdinalIgnoreCase) ||
                    deviceName.Contains("Loopback", StringComparison.OrdinalIgnoreCase) ||
                    deviceName.Contains("WAN Miniport", StringComparison.OrdinalIgnoreCase) ||
                    deviceName.Contains("Bluetooth Device", StringComparison.OrdinalIgnoreCase))
                    continue;
                int code = item["ConfigManagerErrorCode"] is uint value ? (int)value : 0;
                found.Add(new Device(deviceName, code));
            }
        }
        catch { /* WMI unavailable */ }
        return found.Take(8).ToList();
    }

    /// <summary>True when a matching device is present and Windows reports no driver problem.</summary>
    public static bool DeviceWorking(params string[] patterns)
    {
        var devices = FindDevices(patterns);
        return devices.Count > 0 && devices.All(d => !d.Problem);
    }

    static DependencyStatus CheckOui()
    {
        var path = new[]
        {
            Path.Combine(AppContext.BaseDirectory, "oui.csv"),
            Path.Combine(AppPaths.DataDir, "oui.csv")
        }.FirstOrDefault(File.Exists);

        return path != null
            ? new DependencyStatus("Vendor database (oui.csv)", DependencyState.Ok, path)
            : new DependencyStatus("Vendor database (oui.csv)", DependencyState.Warning,
                "Not present, so access points show without a vendor name.",
                "Download", "https://standards-oui.ieee.org/oui/oui.csv", null, "oui");
    }

    static DependencyStatus CheckWinget()
    {
        try
        {
            using var process = Process.Start(new ProcessStartInfo("winget", "--version")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            });
            if (process == null) throw new InvalidOperationException();
            var output = process.StandardOutput.ReadToEnd().Trim();
            process.WaitForExit(5000);
            return new DependencyStatus("Windows Package Manager (winget)", DependencyState.Ok,
                $"Available ({output}), so this page can install tools for you.");
        }
        catch
        {
            return new DependencyStatus("Windows Package Manager (winget)", DependencyState.Warning,
                "Not available, so installs have to be done by hand from the linked pages. " +
                "It ships with App Installer from the Microsoft Store.",
                "Open the Store page", "https://apps.microsoft.com/detail/9nblggh4nns1");
        }
    }

    static DependencyStatus CheckPendingRestart()
    {
        bool pending = false;
        try
        {
            using (var cbs = Registry.LocalMachine.OpenSubKey(
                       @"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"))
                pending |= cbs != null;
            using (var update = Registry.LocalMachine.OpenSubKey(
                       @"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"))
                pending |= update != null;
            using (var session = Registry.LocalMachine.OpenSubKey(
                       @"SYSTEM\CurrentControlSet\Control\Session Manager"))
                pending |= session?.GetValue("PendingFileRenameOperations") != null;
        }
        catch { /* not fatal */ }

        return pending
            ? new DependencyStatus("Restart", DependencyState.Warning,
                "Windows is waiting for a restart. Drivers installed just now may not work until then.",
                "Restart now", null, null, "restart")
            : new DependencyStatus("Restart", DependencyState.Ok, "Nothing is waiting on a restart.");
    }

    sealed record Device(string Name, int ErrorCode)
    {
        public bool Problem => ErrorCode != 0;
    }

    static List<Device> FindDevices(params string[] patterns)
    {
        var found = new List<Device>();
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT Name, DeviceID, ConfigManagerErrorCode FROM Win32_PnPEntity");
            foreach (var item in searcher.Get())
            {
                var name = item["Name"] as string ?? "";
                var id = item["DeviceID"] as string ?? "";
                if (!patterns.Any(p => name.Contains(p, StringComparison.OrdinalIgnoreCase) ||
                                       id.Contains(p, StringComparison.OrdinalIgnoreCase)))
                    continue;
                int code = item["ConfigManagerErrorCode"] is uint value ? (int)value : 0;
                found.Add(new Device(name, code));
            }
        }
        catch { /* WMI unavailable: report nothing rather than guessing */ }
        return found;
    }
}
