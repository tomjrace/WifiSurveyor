using System.Collections.ObjectModel;
using System.Reflection;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using WifiSurveyor.Services;

namespace WifiSurveyor.Views;

public sealed class DependencyRow
{
    public DependencyRow(DependencyStatus status)
    {
        Status = status;
        Dot = status.State switch
        {
            DependencyState.Ok => Draw.Brush(21, 128, 61),
            DependencyState.Warning => Draw.Brush(202, 138, 4),
            DependencyState.Missing => Draw.Brush(220, 38, 38),
            _ => Draw.Brush(148, 163, 184)
        };
    }

    public DependencyStatus Status { get; }
    public string Name => Status.Name;
    public string Detail => Status.Detail;
    public Brush Dot { get; }

    public string ActionLabel => Status.WingetId != null && Status.ActionLabel == "Install"
        ? "Install"
        : Status.ActionLabel ?? "";

    public Visibility ActionVisibility =>
        Status.ActionLabel == null ? Visibility.Collapsed : Visibility.Visible;
}

public partial class SetupView : UserControl
{
    readonly ObservableCollection<DependencyRow> _rows = new();
    readonly Progress<string> _log;
    bool _busy;

    public SetupView()
    {
        InitializeComponent();
        StatusList.ItemsSource = _rows;
        _log = new Progress<string>(Append);

        var version = Assembly.GetExecutingAssembly().GetName().Version;
        VersionText.Text = $"Version {version?.ToString(3) ?? "0.1.0"} · .NET 8 · Windows. " +
                           "Wi-Fi site surveys with heatmaps, spectrum analysis, GPS, frame capture, " +
                           "and multi-adapter testing.";

        ShowLicences();
        UpdateConsent();
        Loaded += async (_, _) =>
        {
            if (_rows.Count == 0) await CheckAsync();
        };
    }

    void Append(string line)
    {
        LogText.AppendText($"{DateTime.Now:HH:mm:ss}  {line}\n");
        LogText.ScrollToEnd();
    }

    // ================= checks =================

    async void Check_Click(object sender, RoutedEventArgs e) => await CheckAsync();

    async Task CheckAsync()
    {
        CheckButton.IsEnabled = false;
        SummaryText.Text = "Looking…";
        try
        {
            var statuses = await DependencyChecker.CheckAllAsync();
            _rows.Clear();
            foreach (var status in statuses) _rows.Add(new DependencyRow(status));

            int missing = statuses.Count(s => s.State == DependencyState.Missing);
            int warnings = statuses.Count(s => s.State == DependencyState.Warning);
            SummaryText.Text = missing == 0 && warnings == 0
                ? "Everything the app needs is in place."
                : $"{missing} missing, {warnings} optional or unconfirmed.";
        }
        catch (Exception ex)
        {
            SummaryText.Text = "The check failed: " + ex.Message;
        }
        finally
        {
            CheckButton.IsEnabled = true;
            UpdateConsent();
        }
    }

    // ================= actions =================

    void Consent_Changed(object sender, RoutedEventArgs e) => UpdateConsent();

    void UpdateConsent()
    {
        bool consented = ConsentBox.IsChecked == true;
        FixAllButton.IsEnabled = consented && !_busy &&
                                 _rows.Any(r => r.Status.WingetId != null || r.Status.Task is "oui" or "axml-drivers");
    }

    async void Action_Click(object sender, RoutedEventArgs e)
    {
        if (_busy || (sender as FrameworkElement)?.DataContext is not DependencyRow row) return;
        await RunAsync(row.Status);
        await CheckAsync();
    }

    async void FixAll_Click(object sender, RoutedEventArgs e)
    {
        if (_busy) return;
        var jobs = _rows.Select(r => r.Status)
            .Where(s => s.State != DependencyState.Ok)
            .Where(s => s.WingetId != null || s.Task is "oui" or "axml-drivers")
            .ToList();

        if (jobs.Count == 0)
        {
            Append("Nothing here can be installed unattended. The rest need their own pages, " +
                   "which the buttons open.");
            return;
        }

        foreach (var job in jobs) await RunAsync(job);
        await CheckAsync();
    }

    async Task RunAsync(DependencyStatus status)
    {
        _busy = true;
        UpdateConsent();
        try
        {
            if (status.Task == "restart")
            {
                if (MessageBox.Show("Restart Windows in 60 seconds? Save your work first.",
                        "Wi-Fi Surveyor", MessageBoxButton.OKCancel, MessageBoxImage.Warning)
                    == MessageBoxResult.OK)
                    Installer.Restart(_log);
                return;
            }

            if (status.Task == "axml-drivers")
            {
                if (!Consented(status.Name)) return;
                Append("Fetching the Alfa AWUS036AXML drivers (Wi-Fi is a 62 MB download), " +
                       "then starting each installer in turn.");
                await DriverInstaller.InstallAsync(
                    new[] { DriverInstaller.AxmlWifi, DriverInstaller.AxmlBluetooth },
                    new[] { "7961", "AWUS036AXML", "MT7921" },
                    _log, CancellationToken.None);
                Append("Downloads kept in " + DriverInstaller.DownloadFolder);
                return;
            }

            if (status.Task == "oui")
            {
                if (!Consented(status.Name)) return;
                await Installer.DownloadAsync("https://standards-oui.ieee.org/oui/oui.csv",
                    Path.Combine(AppPaths.DataDir, "oui.csv"), _log, CancellationToken.None);
                return;
            }

            if (status.WingetId is { } packageId)
            {
                if (!Consented(status.Name)) return;
                Append($"Installing {status.Name}…");
                bool ok = await Installer.InstallWithWingetAsync(packageId, _log, CancellationToken.None);
                if (!ok && status.Url != null)
                {
                    Append("Opening the vendor's page so you can install it yourself.");
                    Installer.OpenUrl(status.Url);
                }
                return;
            }

            if (status.Url != null)
            {
                Append($"Opening {status.Url}");
                Installer.OpenUrl(status.Url);
            }
        }
        catch (Exception ex)
        {
            Append("Failed: " + ex.Message);
        }
        finally
        {
            _busy = false;
            UpdateConsent();
        }
    }

    bool Consented(string what)
    {
        if (ConsentBox.IsChecked == true) return true;
        Append($"{what}: tick the agreement box first.");
        return false;
    }

    // ================= licences =================

    void ShowLicences()
    {
        if (ToolPaths.BundledIperf3 is { } iperf)
        {
            BundledText.Text =
                "iperf3 — BSD 3-Clause, from the ESnet and Lawrence Berkeley National Laboratory project. " +
                "Run as a separate program for throughput tests, not linked into this application.\n" +
                $"Bundled with this build: {iperf}" +
                (ToolPaths.BundledIperf3NeedsCygwin
                    ? "\nThis build also carries cygwin1.dll, which is LGPLv3. If you pass this application " +
                      "on, the LGPL applies to that library: ship its licence text and let people replace it."
                    : "");
        }
        else
        {
            BundledText.Text =
                "iperf3 — BSD 3-Clause. No copy is bundled with this build, so throughput tests use the one " +
                "on your system path or the one set on the Devices tab. To include it, run " +
                "tools\\fetch-iperf3.ps1 and rebuild.";
        }

        if (ToolPaths.BundledIperf3License is { } licenceFile)
        {
            LicenseHeader.Text = $"iperf3 licence, from {licenceFile}";
            try
            {
                LicenseText.Text = File.ReadAllText(licenceFile);
            }
            catch (Exception ex)
            {
                LicenseText.Text = "Couldn't read the licence file: " + ex.Message;
            }
        }
        else
        {
            LicenseHeader.Text = "iperf3 licence";
            LicenseText.Text =
                "iperf3 is distributed under the BSD 3-Clause licence, which requires its copyright notice, " +
                "conditions, and disclaimer to accompany any redistribution. The text is in the LICENSE file " +
                "of the iperf3 distribution: https://github.com/esnet/iperf\n\n" +
                "Bundle iperf3 with this application and that file, kept beside the binary, is shown here.";
        }
    }

    void OpenNotices_Click(object sender, RoutedEventArgs e)
    {
        if (ToolPaths.NoticesFile is { } path) Installer.OpenUrl(path);
        else Append("THIRD-PARTY-NOTICES.md isn't in the program folder; it's in the source tree root.");
    }

    void OpenFolder_Click(object sender, RoutedEventArgs e) => Installer.OpenUrl(AppContext.BaseDirectory);
    void OpenData_Click(object sender, RoutedEventArgs e) => Installer.OpenUrl(AppPaths.DataDir);
}
