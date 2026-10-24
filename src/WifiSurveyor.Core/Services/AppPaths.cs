namespace WifiSurveyor.Services;

/// <summary>
/// Where settings, caches, and recovery files live. LocalApplicationData resolves to
/// %LOCALAPPDATA% on Windows and ~/.local/share on Linux, so both builds agree.
/// </summary>
public static class AppPaths
{
    public static string DataDir
    {
        get
        {
            var dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WifiSurveyor");
            Directory.CreateDirectory(dir);
            return dir;
        }
    }
}
