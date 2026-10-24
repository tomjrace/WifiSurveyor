using Avalonia;

namespace WifiSurveyor.Gui;

public static class Program
{
    // Avalonia needs this before any control is touched, so keep it free of other work.
    [STAThread]
    public static void Main(string[] args) =>
        BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);

    public static AppBuilder BuildAvaloniaApp() =>
        AppBuilder.Configure<App>()
            .UsePlatformDetect()
            .WithInterFont()
            .LogToTrace();
}
