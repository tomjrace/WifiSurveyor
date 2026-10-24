using System.Diagnostics;

namespace WifiSurveyor.Services;

/// <summary>Runs a console tool and hands back everything it printed.</summary>
public static class CommandRunner
{
    public static async Task<(int Code, string Output)> RunAsync(string file, string arguments,
        TimeSpan timeout, CancellationToken ct)
    {
        var psi = new ProcessStartInfo(file, arguments)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        try
        {
            using var process = Process.Start(psi);
            if (process == null) return (-1, $"Couldn't start {file}.");

            var output = process.StandardOutput.ReadToEndAsync(ct);
            var error = process.StandardError.ReadToEndAsync(ct);
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(timeout);
            try
            {
                await process.WaitForExitAsync(cts.Token);
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                try { process.Kill(true); } catch { }
                return (-1, $"{file} didn't finish within {timeout.TotalSeconds:0} seconds.");
            }

            var text = (await output).TrimEnd();
            var errorText = (await error).Trim();
            if (errorText.Length > 0) text += (text.Length > 0 ? "\n" : "") + errorText;
            return (process.ExitCode, text);
        }
        catch (Exception ex)
        {
            return (-1, ex.Message);
        }
    }

    /// <summary>Finds a program by absolute path, or on PATH.</summary>
    public static string? Locate(string executable, params string[] likelyFolders)
    {
        foreach (var folder in likelyFolders)
        {
            var candidate = Path.Combine(folder, executable);
            if (File.Exists(candidate)) return candidate;
        }
        foreach (var folder in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            try
            {
                if (folder.Length == 0) continue;
                var candidate = Path.Combine(folder, executable);
                if (File.Exists(candidate)) return candidate;
            }
            catch { /* malformed PATH entry */ }
        }
        return null;
    }
}
