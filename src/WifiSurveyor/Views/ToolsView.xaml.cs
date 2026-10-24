using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using WifiSurveyor.Capture;
using WifiSurveyor.Services;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Views;

public partial class ToolsView : UserControl
{
    static readonly string[] WiresharkFolders =
    {
        @"C:\Program Files\Wireshark",
        @"C:\Program Files (x86)\Wireshark"
    };

    string? _wireshark, _tshark;

    public ToolsView()
    {
        InitializeComponent();
        Refresh();
    }

    void Refresh_Click(object sender, RoutedEventArgs e) => Refresh();

    void Refresh()
    {
        AdapterBox.ItemsSource = WlanConnection.ListInterfaces();
        AdapterBox.DisplayMemberPath = nameof(WlanInterfaceInfo.Description);
        if (AdapterBox.Items.Count > 0) AdapterBox.SelectedIndex = 0;

        _wireshark = CommandRunner.Locate("Wireshark.exe", WiresharkFolders);
        _tshark = CommandRunner.Locate("tshark.exe", WiresharkFolders);
        WiresharkState.Text = _tshark != null
            ? $"Found at {Path.GetDirectoryName(_tshark)}."
            : "Not found. Install it with: winget install WiresharkFoundation.Wireshark";
        TsharkCaptureButton.IsEnabled = _tshark != null;
    }

    // ================= output =================

    void Write(string title, string text)
    {
        OutputTitle.Text = title;
        OutputText.Text = text.Length > 0 ? text : "(nothing came back)";
        OutputText.ScrollToHome();
    }

    void Append(string text) => OutputText.AppendText("\n" + text);

    async Task RunAsync(string title, string file, string arguments, TimeSpan? timeout = null)
    {
        Write(title, "Running…");
        var (_, output) = await CommandRunner.RunAsync(file, arguments,
            timeout ?? TimeSpan.FromSeconds(45), CancellationToken.None);
        Write(title, output);
    }

    void Copy_Click(object sender, RoutedEventArgs e)
    {
        try { Clipboard.SetText(OutputText.Text); }
        catch (Exception ex) { MessageBox.Show("Couldn't copy: " + ex.Message, "Wi-Fi Surveyor"); }
    }

    void Clear_Click(object sender, RoutedEventArgs e) => Write("Output", "");

    // ================= monitor mode support =================

    async void ShowDrivers_Click(object sender, RoutedEventArgs e)
    {
        Write("netsh wlan show drivers", "Asking the driver…");
        var (_, output) = await CommandRunner.RunAsync("netsh", "wlan show drivers",
            TimeSpan.FromSeconds(30), CancellationToken.None);

        // The interesting line is easy to miss in a page of output, so pull it out first.
        var verdicts = output.Split('\n')
            .Where(line => line.Contains("monitor", StringComparison.OrdinalIgnoreCase))
            .Select(line => line.Trim())
            .ToList();

        var summary = verdicts.Count > 0
            ? "What the driver says about monitor mode:\n  " + string.Join("\n  ", verdicts) +
              "\n\nIf that says No, live capture on this adapter won't work no matter what else is " +
              "installed. Capture on Linux with the same adapter and open the file on the Capture tab.\n" +
              new string('-', 60) + "\n"
            : "No line about monitor mode came back, which usually means an older driver.\n" +
              new string('-', 60) + "\n";

        Write("netsh wlan show drivers", summary + output);
    }

    // ================= Npcap =================

    void ListCapture_Click(object sender, RoutedEventArgs e)
    {
        if (!NpcapLive.IsInstalled)
        {
            Write("Npcap", "Npcap isn't installed. Live capture is not part of this app.");
            return;
        }
        try
        {
            var devices = NpcapLive.ListDevices();
            Write("Capture adapters", devices.Count == 0
                ? "Npcap is installed but lists no adapters."
                : string.Join("\n", devices.Select(d => $"{d.Description}\n    {d.Name}")));
        }
        catch (Exception ex)
        {
            Write("Capture adapters", ex.Message);
        }
    }

    async void WlanHelperModes_Click(object sender, RoutedEventArgs e)
    {
        var helper = Path.Combine(NpcapLive.NpcapDirectory, "WlanHelper.exe");
        if (!File.Exists(helper))
        {
            Write("WlanHelper", "WlanHelper.exe isn't there. Reinstall Npcap with raw 802.11 support.");
            return;
        }
        await RunAsync("WlanHelper: supported modes", helper, $"\"{ConnectionName.Text.Trim()}\" modes");
    }

    void MonitorOn_Click(object sender, RoutedEventArgs e)
    {
        int? channel = int.TryParse(ChannelBox.Text, out var c) ? c : null;
        Write("Monitor mode", NpcapLive.SetMonitorMode(ConnectionName.Text.Trim(), true, channel));
        Append("\nCheck it took by listing capture adapters and capturing on the Capture tab.");
    }

    void MonitorOff_Click(object sender, RoutedEventArgs e) =>
        Write("Managed mode", NpcapLive.SetMonitorMode(ConnectionName.Text.Trim(), false, null));

    void SetChannel_Click(object sender, RoutedEventArgs e)
    {
        if (!int.TryParse(ChannelBox.Text, out var channel))
        {
            Write("Channel", "Enter a channel number first.");
            return;
        }
        Write("Channel", NpcapLive.SetMonitorMode(ConnectionName.Text.Trim(), true, channel));
    }

    // ================= Wireshark =================

    void Wireshark_Click(object sender, RoutedEventArgs e)
    {
        if (_wireshark == null)
        {
            Write("Wireshark", "Not installed. Install it with:\n  winget install WiresharkFoundation.Wireshark");
            return;
        }
        Installer.OpenUrl(_wireshark);
    }

    async void TsharkList_Click(object sender, RoutedEventArgs e)
    {
        if (_tshark == null)
        {
            Write("tshark", "Not installed.");
            return;
        }
        await RunAsync("tshark -D", _tshark, "-D");
    }

    async void TsharkCapture_Click(object sender, RoutedEventArgs e)
    {
        if (_tshark == null) return;
        if (AdapterBox.SelectedItem is not WlanInterfaceInfo)
        {
            Write("tshark", "Choose an adapter first.");
            return;
        }

        int seconds = int.TryParse(TsharkSeconds.Text, out var s) ? Math.Clamp(s, 1, 600) : 15;
        var file = Path.Combine(AppPaths.DataDir,
            $"capture-{DateTime.Now:yyyyMMdd-HHmmss}.pcapng");

        // tshark numbers its interfaces, and the number rarely matches the adapter order here.
        Write("tshark", "Finding the interface number…");
        var (_, list) = await CommandRunner.RunAsync(_tshark, "-D", TimeSpan.FromSeconds(30),
            CancellationToken.None);

        var wanted = ConnectionName.Text.Trim();
        var match = list.Split('\n')
            .FirstOrDefault(line => line.Contains(wanted, StringComparison.OrdinalIgnoreCase));
        var number = match?.Split('.').FirstOrDefault()?.Trim();

        if (number == null)
        {
            Write("tshark", "Couldn't match the connection name to a tshark interface.\n\n" + list +
                            "\n\nUse the number from that list with:\n" +
                            $"  \"{_tshark}\" -i <number> -a duration:{seconds} -w \"{file}\"");
            return;
        }

        Write("tshark", $"Capturing for {seconds} s on interface {number} ({wanted})…");
        var (code, output) = await CommandRunner.RunAsync(_tshark,
            $"-i {number} -a duration:{seconds.ToString(CultureInfo.InvariantCulture)} -w \"{file}\"",
            TimeSpan.FromSeconds(seconds + 30), CancellationToken.None);

        Write("tshark", output.Length > 0 ? output : $"Finished with code {code}.");
        if (File.Exists(file))
            Append($"\nSaved: {file}\nOpen it on the Capture tab to get retries, airtime, clients, " +
                   "and beacon details out of it.");
    }

    // ================= Windows Wi-Fi tools =================

    async void ShowInterfaces_Click(object sender, RoutedEventArgs e) =>
        await RunAsync("netsh wlan show interfaces", "netsh", "wlan show interfaces");

    async void ShowNetworks_Click(object sender, RoutedEventArgs e) =>
        await RunAsync("netsh wlan show networks mode=bssid", "netsh", "wlan show networks mode=bssid");

    async void ShowProfiles_Click(object sender, RoutedEventArgs e) =>
        await RunAsync("netsh wlan show profiles", "netsh", "wlan show profiles");

    async void WlanReport_Click(object sender, RoutedEventArgs e)
    {
        Write("Wi-Fi history report", "Building it. This needs administrator rights and takes a moment…");
        var (_, output) = await CommandRunner.RunAsync("netsh", "wlan show wlanreport",
            TimeSpan.FromMinutes(2), CancellationToken.None);
        Write("Wi-Fi history report", output);

        var report = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "Microsoft", "Windows", "WlanReport", "wlan-report-latest.html");
        if (File.Exists(report))
        {
            Append($"\nOpening {report}");
            Installer.OpenUrl(report);
        }
        else
        {
            Append("\nThe report wasn't written. This command needs an administrator command prompt: " +
                   "run \"netsh wlan show wlanreport\" there.");
        }
    }
}
