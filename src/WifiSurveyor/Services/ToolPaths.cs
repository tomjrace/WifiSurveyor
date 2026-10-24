namespace WifiSurveyor.Services;

/// <summary>
/// Finds the tools that ship beside the application. iperf3 is bundled when a copy was
/// placed in tools\iperf3 at build time; otherwise the app falls back to the system path.
/// </summary>
public static class ToolPaths
{
    public static string BundledFolder => Path.Combine(AppContext.BaseDirectory, "iperf3");

    public static string? BundledIperf3
    {
        get
        {
            var path = Path.Combine(BundledFolder, "iperf3.exe");
            return File.Exists(path) ? path : null;
        }
    }

    /// <summary>The iperf3 licence text, when a bundled copy brought one along.</summary>
    public static string? BundledIperf3License
    {
        get
        {
            if (!Directory.Exists(BundledFolder)) return null;
            return Directory.EnumerateFiles(BundledFolder)
                .FirstOrDefault(f => Path.GetFileName(f)
                    .StartsWith("LICENSE", StringComparison.OrdinalIgnoreCase) ||
                    Path.GetFileName(f).Equals("COPYING", StringComparison.OrdinalIgnoreCase));
        }
    }

    public static bool BundledIperf3NeedsCygwin =>
        File.Exists(Path.Combine(BundledFolder, "cygwin1.dll"));

    public static string? NoticesFile
    {
        get
        {
            var path = Path.Combine(AppContext.BaseDirectory, "THIRD-PARTY-NOTICES.md");
            return File.Exists(path) ? path : null;
        }
    }

    /// <summary>Prefers a configured path that exists, then the bundled copy, then the system path.</summary>
    public static string ResolveIperf3(string? configured)
    {
        if (!string.IsNullOrWhiteSpace(configured))
        {
            var trimmed = configured.Trim();
            bool looksLikePath = trimmed.Contains(Path.DirectorySeparatorChar) ||
                                 trimmed.Contains(Path.AltDirectorySeparatorChar);
            if (!looksLikePath || File.Exists(trimmed)) return trimmed;
        }
        return BundledIperf3 ?? "iperf3.exe";
    }
}
