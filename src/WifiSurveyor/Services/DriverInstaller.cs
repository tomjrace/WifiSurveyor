using System.Diagnostics;

namespace WifiSurveyor.Services;

/// <summary>
/// Downloads vendor driver packages and starts their installers, one after the next, exactly
/// as double-clicking them would. Each installer shows its own licence and prompts for
/// administrator rights itself.
/// </summary>
public static class DriverInstaller
{
    public sealed record DriverPackage(string Name, string Url, string FileName);

    const string AlfaRoot = "https://files.alfa.com.tw/%5B1%5D%20WiFi%20USB%20adapter/AWUS036AXML/Windows";

    public const string AxmlFolderPage =
        "https://files.alfa.com.tw/?dir=%5B1%5D%20WiFi%20USB%20adapter/AWUS036AXML/Windows";

    public static readonly DriverPackage AxmlWifi = new(
        "AWUS036AXML Wi-Fi driver",
        $"{AlfaRoot}/WiFi/AWUS036AXMLsetup.exe",
        "AWUS036AXMLsetup.exe");

    public static readonly DriverPackage AxmlBluetooth = new(
        "AWUS036AXML Bluetooth driver",
        $"{AlfaRoot}/Bluetooth/AX3000%20WIN%20BT%20setup.exe",
        "AX3000 WIN BT setup.exe");

    public static async Task InstallAsync(IEnumerable<DriverPackage> packages, string[] devicePatterns,
        IProgress<string> log, CancellationToken ct)
    {
        var folder = Path.Combine(Path.GetTempPath(), "WifiSurveyor-drivers");
        Directory.CreateDirectory(folder);

        foreach (var package in packages)
        {
            ct.ThrowIfCancellationRequested();
            log.Report($"— {package.Name} —");

            var file = Path.Combine(folder, package.FileName);
            if (!await Installer.DownloadAsync(package.Url, file, log, ct))
            {
                log.Report("Download failed. Opening the vendor's folder so you can fetch it yourself.");
                Installer.OpenUrl(AxmlFolderPage);
                continue;
            }

            log.Report("Starting the installer. Work through its windows, including the administrator prompt.");
            try
            {
                using var process = Process.Start(new ProcessStartInfo(file) { UseShellExecute = true });
                if (process == null)
                {
                    log.Report("Windows didn't start it. Run it yourself from " + file);
                    continue;
                }
                await process.WaitForExitAsync(ct);
                log.Report($"{package.Name}: the installer closed.");
            }
            catch (Exception ex)
            {
                log.Report("Couldn't start it: " + ex.Message + ". The file is at " + file);
            }
        }

        log.Report(DependencyChecker.DeviceWorking(devicePatterns)
            ? "Windows now reports a working driver for the adapter."
            : "Windows still reports no working driver. These usually need a restart; unplugging and " +
              "replugging the adapter afterwards helps too.");
    }

    /// <summary>Where the downloads are kept, so the user can run them again by hand.</summary>
    public static string DownloadFolder => Path.Combine(Path.GetTempPath(), "WifiSurveyor-drivers");
}
