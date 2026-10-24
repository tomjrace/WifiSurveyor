using System.Windows;

namespace WifiSurveyor;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        DispatcherUnhandledException += (_, args) =>
        {
            MessageBox.Show(args.Exception.Message, "Wi-Fi Surveyor", MessageBoxButton.OK, MessageBoxImage.Error);
            args.Handled = true;
        };
    }
}
