using System.Globalization;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using WifiSurveyor.Capture;
using WifiSurveyor.Models;
using WifiSurveyor.Services;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Views;

public sealed class CheckRow
{
    public CheckRow(MonitorCheck check)
    {
        Title = check.Title;
        Detail = check.Detail;
        (Mark, MarkBrush) = check.State switch
        {
            CheckState.Pass => ("✓", (System.Windows.Media.Brush)Draw.Brush(21, 128, 61)),
            CheckState.Warn => ("!", Draw.Brush(202, 138, 4)),
            CheckState.Fail => ("✕", Draw.Brush(185, 28, 28)),
            _ => ("·", Draw.Brush(120, 130, 145))
        };
    }

    public string Title { get; }
    public string Detail { get; }
    public string Mark { get; }
    public System.Windows.Media.Brush MarkBrush { get; }
    public Visibility DetailVisibility => Detail.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
}

public partial class CaptureView : UserControl
{
    readonly AppServices _svc = AppServices.Instance;
    readonly Progress<string> _monitorLog;
    CancellationTokenSource? _cts;
    CaptureSummary? _summary;
    string _connectionName = "Wi-Fi";

    public CaptureView()
    {
        InitializeComponent();
        _monitorLog = new Progress<string>(line =>
        {
            MonitorLog.AppendText($"{DateTime.Now:HH:mm:ss}  {line}\n");
            MonitorLog.ScrollToEnd();
        });
        _connectionName = _svc.Settings.PcapAdapterName;
        SecondsBox.Text = _svc.Settings.PcapSeconds.ToString(CultureInfo.CurrentCulture);
        ElevateButton.Visibility = MonitorMode.IsAdministrator ? Visibility.Collapsed : Visibility.Visible;
        Refresh();
    }

    void Refresh_Click(object sender, RoutedEventArgs e) => Refresh();

    void Refresh()
    {
        var wlan = WlanConnection.ListInterfaces();
        WlanBox.ItemsSource = wlan;
        WlanBox.DisplayMemberPath = nameof(WlanInterfaceInfo.Description);
        // Prefer the adapter used last time; otherwise one that isn't connected, which is the
        // likely spare radio, rather than the one carrying your connection.
        WlanBox.SelectedItem = wlan.FirstOrDefault(i => MonitorMode.ConnectionName(i) == _connectionName)
                               ?? wlan.FirstOrDefault(i => !i.IsConnected)
                               ?? wlan.FirstOrDefault();

        if (!NpcapLive.IsInstalled)
        {
            CaptureStatus.Text = "Npcap isn't installed, so live capture is unavailable. You can still open a capture file.";
            CaptureButton.IsEnabled = false;
            return;
        }
        try
        {
            DeviceBox.ItemsSource = NpcapLive.ListDevices();
            SelectMatchingDevice();
            CaptureStatus.Text = "";
        }
        catch (Exception ex)
        {
            CaptureStatus.Text = "Couldn't list adapters: " + ex.Message;
            CaptureButton.IsEnabled = false;
        }
    }

    // ================= monitor mode =================

    WlanInterfaceInfo? SelectedWlan => WlanBox.SelectedItem as WlanInterfaceInfo;

    void WlanBox_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (SelectedWlan is not { } iface) return;
        _connectionName = MonitorMode.ConnectionName(iface);
        AdapterName.Text = $"Windows calls it \"{_connectionName}\"" + (iface.IsConnected ? ", connected" : "");
        SelectMatchingDevice();
        CheckList.ItemsSource = null;
        Verdict.Text = "";
    }

    /// <summary>Npcap names devices after the interface GUID, so the right one can be chosen for you.</summary>
    void SelectMatchingDevice()
    {
        if (DeviceBox.ItemsSource is not IEnumerable<CaptureDevice> devices) return;
        var list = devices.ToList();
        var wanted = SelectedWlan is { } iface ? MonitorMode.NpcapDeviceName(iface) : null;
        DeviceBox.SelectedItem =
            list.FirstOrDefault(d => wanted != null && d.Name.Equals(wanted, StringComparison.OrdinalIgnoreCase))
            ?? list.FirstOrDefault(d => d.Description.Contains("Acrylic", StringComparison.OrdinalIgnoreCase))
            ?? list.FirstOrDefault();
    }

    async void CheckMonitor_Click(object sender, RoutedEventArgs e) => await RunChecksAsync();

    async Task RunChecksAsync()
    {
        if (SelectedWlan is not { } iface)
        {
            Verdict.Text = "Choose an adapter first.";
            return;
        }
        CheckMonitorButton.IsEnabled = false;
        Verdict.Text = "Checking…";
        try
        {
            var checks = await MonitorMode.DiagnoseAsync(iface);
            CheckList.ItemsSource = checks.Select(c => new CheckRow(c)).ToList();
            Verdict.Text = Conclude(checks);
        }
        finally
        {
            CheckMonitorButton.IsEnabled = true;
        }
    }

    static string Conclude(IReadOnlyList<MonitorCheck> checks)
    {
        bool driverSaysNo = checks.Any(c => c.State == CheckState.Fail && c.Title.StartsWith("Driver"));
        bool thirdParty = checks.Any(c => c.State == CheckState.Pass && c.Title.StartsWith("A third-party"));
        if (driverSaysNo && !thirdParty)
            return "Bottom line: this adapter's current driver has no monitor mode, so no setting here or in " +
                   "Windows can switch it on. Three ways round it: capture with the same adapter on Linux and open " +
                   "the file here (the mt76 driver there does monitor mode well); try an older or vendor driver for " +
                   "it on Windows, since support varies by version; or install a third-party monitor driver such " +
                   "as the one Acrylic ships, whose capture interface this tab will then find.";
        if (checks.Any(c => c.State == CheckState.Fail))
            return "Fix the items marked ✕ first; the rest may then fall into place.";
        if (checks.Any(c => c.State == CheckState.Warn))
            return "Nothing rules monitor mode out. \"Prepare and enable\" deals with the items marked !.";
        return "Everything checks out. Capture below.";
    }

    async void Prepare_Click(object sender, RoutedEventArgs e)
    {
        if (SelectedWlan is not { } iface) return;
        PrepareButton.IsEnabled = false;
        try
        {
            if (CloseTools.IsChecked == true)
            {
                var running = MonitorMode.FindInterferingProcesses();
                if (running.Count > 0 &&
                    MessageBox.Show(
                        "Close these programs? Save anything open in them first.\n\n" +
                        string.Join("\n", running.Select(p => p.ProcessName).Distinct()),
                        "Wi-Fi Surveyor", MessageBoxButton.OKCancel, MessageBoxImage.Warning) == MessageBoxResult.OK)
                    MonitorMode.CloseProcesses(running, _monitorLog);
            }

            int? channel = int.TryParse(ChannelBox.Text, out var c) ? c : null;
            var ok = await MonitorMode.PrepareAsync(iface,
                new MonitorMode.PrepareOptions(StopWindows.IsChecked == true, PauseScans.IsChecked == true, channel),
                _monitorLog);

            _svc.Settings.PcapAdapterName = _connectionName;
            if (DeviceBox.SelectedItem is CaptureDevice device) _svc.Settings.PcapDevice = device.Name;
            _svc.Settings.Save();

            await RunChecksAsync();
            if (ok) Verdict.Text = "In monitor mode. Capture below, or tick \"Capture frames\" on the Survey tab.";
        }
        finally
        {
            PrepareButton.IsEnabled = true;
        }
    }

    async void Restore_Click(object sender, RoutedEventArgs e)
    {
        if (SelectedWlan is not { } iface) return;
        await MonitorMode.RestoreAsync(iface, _monitorLog);
        await RunChecksAsync();
    }

    void Elevate_Click(object sender, RoutedEventArgs e)
    {
        if (Window.GetWindow(this) is MainWindow window) window.RestartElevated();
    }

    async void Capture_Click(object sender, RoutedEventArgs e)
    {
        if (DeviceBox.SelectedItem is not CaptureDevice device)
        {
            CaptureStatus.Text = "Choose an adapter first.";
            return;
        }
        int seconds = int.TryParse(SecondsBox.Text, out var s) ? Math.Clamp(s, 1, 300) : 10;
        _svc.Settings.PcapDevice = device.Name;
        _svc.Settings.PcapAdapterName = _connectionName;
        _svc.Settings.PcapSeconds = seconds;
        _svc.Settings.Save();

        _cts = new CancellationTokenSource();
        CaptureButton.IsEnabled = false;
        CaptureStatus.Text = "Capturing…";
        try
        {
            var progress = new Progress<string>(m => CaptureStatus.Text = m);
            var frames = await NpcapLive.CaptureAsync(device.Name, TimeSpan.FromSeconds(seconds), true,
                progress, _cts.Token);
            Show(CaptureAnalyzer.Analyze(frames, device.ToString()));
        }
        catch (OperationCanceledException)
        {
            CaptureStatus.Text = "Stopped.";
        }
        catch (Exception ex)
        {
            CaptureStatus.Text = ex.Message;
        }
        finally
        {
            CaptureButton.IsEnabled = true;
            _cts?.Dispose();
            _cts = null;
        }
    }

    void Stop_Click(object sender, RoutedEventArgs e) => _cts?.Cancel();

    async void OpenFile_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new OpenFileDialog { Filter = "Captures (*.pcap;*.pcapng;*.cap)|*.pcap;*.pcapng;*.cap" };
        if (dlg.ShowDialog() != true) return;
        CaptureStatus.Text = "Reading…";
        try
        {
            var frames = await Task.Run(() => PcapReader.Read(dlg.FileName));
            var summary = await Task.Run(() => CaptureAnalyzer.Analyze(frames, Path.GetFileName(dlg.FileName)));
            Show(summary);
        }
        catch (Exception ex)
        {
            CaptureStatus.Text = "Couldn't read that file: " + ex.Message;
        }
    }

    void Show(CaptureSummary summary)
    {
        _summary = summary;
        BssGrid.ItemsSource = summary.Bss;

        if (summary.Frames == 0)
        {
            Summary.Text = "No frames captured. If the adapter is in monitor mode and parked on a busy " +
                           "channel, this usually means the driver isn't delivering 802.11 frames.";
            CaptureStatus.Text = "Nothing captured.";
            return;
        }

        var sb = new StringBuilder();
        sb.AppendLine($"{summary.Frames:N0} frames over {summary.Seconds:0.0} s from {summary.Source}" +
                      (summary.Channel > 0 ? $", channel {summary.Channel}" : ""));
        sb.AppendLine($"Management {summary.Management:N0}   control {summary.Control:N0}   data {summary.Data:N0}" +
                      (summary.BadFcs > 0 ? $"   bad FCS {summary.BadFcs:N0}" : ""));
        sb.AppendLine($"Retry rate {summary.RetryRate * 100:0.0}% of {summary.RetryEligible:N0} frames");
        sb.AppendLine($"Measured airtime {summary.AirtimeFraction * 100:0.0}%   clients heard {summary.Clients}");
        if (summary.Deauths > 0 || summary.Disassocs > 0)
            sb.AppendLine($"Deauthentications {summary.Deauths}, disassociations {summary.Disassocs}");
        Summary.Text = sb.ToString().TrimEnd();
        CaptureStatus.Text = "Done.";
    }

    void Attach_Click(object sender, RoutedEventArgs e)
    {
        if (_summary == null)
        {
            CaptureStatus.Text = "Capture or open a file first.";
            return;
        }
        if (_svc.SelectedPoint is not { } point)
        {
            CaptureStatus.Text = "Select a measurement point on the Survey tab first.";
            return;
        }
        point.Capture = _summary;
        _svc.NotifyPointsChanged();
        CaptureStatus.Text = $"Attached to the point measured at {point.Time:t}. " +
                             "The retry, airtime, and client maps can use it now.";
    }
}
