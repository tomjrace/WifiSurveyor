using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using Windows.Devices.Geolocation;
using WifiSurveyor.Positioning;
using WifiSurveyor.Services;

namespace WifiSurveyor;

public partial class MainWindow : Window
{
    readonly DispatcherTimer _statusTimer = new() { Interval = TimeSpan.FromSeconds(1) };
    Panel? _mapParent;
    int _mapIndex = -1;
    WindowStyle _previousStyle;
    WindowState _previousState;

    public MainWindow()
    {
        InitializeComponent();
        _statusTimer.Tick += (_, _) => UpdateStatus();
        _statusTimer.Start();
        Loaded += MainWindow_Loaded;
        Closing += (_, e) => { if (!_skipCloseCheck && !SurveyTab.ConfirmDiscard()) e.Cancel = true; };
        PreviewKeyDown += (_, e) =>
        {
            if (e.Key == Key.Escape && FullScreenHost.Visibility == Visibility.Visible)
            {
                SurveyTab.SetFullScreen(false);
                e.Handled = true;
            }
        };
        Closed += (_, _) => AppServices.Instance.Shutdown();
    }

    async void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        try
        {
            var access = await Geolocator.RequestAccessAsync();
            LocationBanner.Visibility = access == GeolocationAccessStatus.Allowed
                ? Visibility.Collapsed : Visibility.Visible;
        }
        catch
        {
            LocationBanner.Visibility = Visibility.Visible;
        }
    }

    /// <summary>Moves the map element into a full-window host and strips the chrome around it.</summary>
    public void SetMapFullScreen(bool on, FrameworkElement map)
    {
        if (on)
        {
            if (FullScreenHost.Visibility == Visibility.Visible) return;
            _mapParent = map.Parent as Panel;
            _mapIndex = _mapParent?.Children.IndexOf(map) ?? -1;
            _mapParent?.Children.Remove(map);
            FullScreenHost.Children.Add(map);
            FullScreenHost.Visibility = Visibility.Visible;
            MainLayout.Visibility = Visibility.Collapsed;

            _previousStyle = WindowStyle;
            _previousState = WindowState;
            WindowStyle = WindowStyle.None;
            WindowState = WindowState.Normal;   // a maximize only takes effect after a change
            WindowState = WindowState.Maximized;
        }
        else
        {
            if (FullScreenHost.Visibility != Visibility.Visible) return;
            FullScreenHost.Children.Remove(map);
            if (_mapParent != null)
            {
                if (_mapIndex >= 0 && _mapIndex <= _mapParent.Children.Count)
                    _mapParent.Children.Insert(_mapIndex, map);
                else
                    _mapParent.Children.Add(map);
            }
            FullScreenHost.Visibility = Visibility.Collapsed;
            MainLayout.Visibility = Visibility.Visible;
            WindowStyle = _previousStyle;
            WindowState = _previousState;
        }
    }

    bool _skipCloseCheck;

    /// <summary>Asks to save, starts an elevated copy, then closes this one.</summary>
    public void RestartElevated()
    {
        if (!SurveyTab.ConfirmDiscard()) return;
        if (!Capture.MonitorMode.StartElevatedCopy())
        {
            MessageBox.Show("Windows didn't start the administrator copy (the prompt may have been dismissed).",
                "Wi-Fi Surveyor");
            return;
        }
        _skipCloseCheck = true;
        Close();
    }

    void About_Click(object sender, RoutedEventArgs e) => SetupTab.IsSelected = true;

    void Notices_Click(object sender, RoutedEventArgs e)
    {
        if (Services.ToolPaths.NoticesFile is { } path)
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        else
            SetupTab.IsSelected = true;
    }

    void DataFolder_Click(object sender, RoutedEventArgs e) =>
        Process.Start(new ProcessStartInfo(AppPaths.DataDir) { UseShellExecute = true });

    void OpenLocationSettings_Click(object sender, RoutedEventArgs e) =>
        Process.Start(new ProcessStartInfo("ms-settings:privacy-location") { UseShellExecute = true });

    void UpdateStatus()
    {
        var svc = AppServices.Instance;
        var rfe = svc.Rfe;
        RfeStatusText.Text = rfe is { IsOpen: true }
            ? rfe.Config is { } c
                ? $"RF Explorer: {rfe.PortName}, {c.StartMHz:0.#}–{c.EndMHz:0.#} MHz"
                : $"RF Explorer: {rfe.PortName}, waiting for device"
            : "RF Explorer: not connected";

        GpsStatusText.Text = svc.Location == null
            ? "GPS: off"
            : $"{svc.Location.Name}: {GeoText.Describe(svc.Location.LastFix)}";
    }
}
