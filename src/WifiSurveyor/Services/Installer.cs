using System.Diagnostics;
using System.Net.Http;

namespace WifiSurveyor.Services;

/// <summary>
/// Carries out the setup actions the user asks for.
///
/// Installs go through winget with the vendor's own unattended switches, including
/// --accept-package-agreements, which winget provides precisely so a user can consent once
/// and have the install proceed. That consent is taken in the app before anything runs.
/// Where a vendor has no unattended install (the Silicon Labs and MediaTek driver
/// downloads), this opens their page instead of clicking through their agreement on the
/// user's behalf.
/// </summary>
public static class Installer
{
    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromMinutes(5) };

    public static async Task<bool> InstallWithWingetAsync(string packageId, IProgress<string> log,
        CancellationToken ct)
    {
        log.Report($"winget install {packageId}");
        var psi = new ProcessStartInfo("winget")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        foreach (var argument in new[]
                 {
                     "install", "--id", packageId, "--exact", "--silent",
                     "--accept-package-agreements", "--accept-source-agreements"
                 })
            psi.ArgumentList.Add(argument);

        try
        {
            using var process = Process.Start(psi);
            if (process == null)
            {
                log.Report("Couldn't start winget.");
                return false;
            }

            var output = process.StandardOutput.ReadToEndAsync(ct);
            var error = process.StandardError.ReadToEndAsync(ct);
            await process.WaitForExitAsync(ct);

            foreach (var line in (await output).Split('\n', StringSplitOptions.RemoveEmptyEntries)
                         .Select(l => l.TrimEnd())
                         .Where(l => l.Length > 0 && !l.Contains('\u2588'))   // drop progress bars
                         .TakeLast(6))
                log.Report(line);

            if (process.ExitCode == 0)
            {
                log.Report($"{packageId} installed.");
                return true;
            }

            var errorText = (await error).Trim();
            log.Report($"winget finished with code {process.ExitCode}. " +
                       (errorText.Length > 0 ? errorText : "It may need administrator rights; " +
                                                           "try running this app as administrator."));
            return false;
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            log.Report("winget failed: " + ex.Message);
            return false;
        }
    }

    public static async Task<bool> DownloadAsync(string url, string destination, IProgress<string> log,
        CancellationToken ct)
    {
        log.Report($"Downloading {url}");
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, url);
            request.Headers.Add("User-Agent", "WifiSurveyor");
            using var response = await Http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
            response.EnsureSuccessStatusCode();

            var temporary = destination + ".part";
            await using (var file = File.Create(temporary))
                await response.Content.CopyToAsync(file, ct);
            File.Move(temporary, destination, overwrite: true);

            log.Report($"Saved to {destination} ({new FileInfo(destination).Length / 1024:N0} kB).");
            return true;
        }
        catch (Exception ex)
        {
            log.Report("Download failed: " + ex.Message);
            return false;
        }
    }

    public static void OpenUrl(string url)
    {
        try
        {
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            System.Windows.MessageBox.Show("Couldn't open it: " + ex.Message, "Wi-Fi Surveyor");
        }
    }

    /// <summary>Asks Windows to restart in a minute, which the user can cancel.</summary>
    public static bool Restart(IProgress<string> log)
    {
        try
        {
            Process.Start(new ProcessStartInfo("shutdown",
                "/r /t 60 /c \"Wi-Fi Surveyor: restarting to finish a driver installation\"")
            {
                UseShellExecute = false,
                CreateNoWindow = true
            });
            log.Report("Restarting in 60 seconds. Run \"shutdown /a\" in a command prompt to call it off.");
            return true;
        }
        catch (Exception ex)
        {
            log.Report("Couldn't request a restart: " + ex.Message);
            return false;
        }
    }
}
