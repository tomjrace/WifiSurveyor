using System.Diagnostics;
using System.Runtime.InteropServices;

namespace WifiSurveyor.Capture;

public sealed record CaptureDevice(string Name, string Description)
{
    public override string ToString() => Description.Length > 0 ? Description : Name;
}

/// <summary>
/// Live 802.11 capture through Npcap's wpcap.dll.
///
/// Windows monitor mode goes through the deprecated Native WiFi interface, and whether it
/// works depends entirely on the adapter's driver. MediaTek MT7612U parts (Alfa AWUS036ACM,
/// Netgear A6210) are the ones most often reported working, usually without per-frame signal
/// strength or channel in the Radiotap header. If live capture fails, capture elsewhere
/// (Linux and this same adapter are reliable) and import the file instead.
/// </summary>
public static class NpcapLive
{
    const int PcapErrorBufferSize = 256;
    static bool _resolverSet;

    public static string NpcapDirectory => Path.Combine(Environment.SystemDirectory, "Npcap");

    public static bool IsInstalled => File.Exists(Path.Combine(NpcapDirectory, "wpcap.dll"));

    /// <summary>Npcap installs its DLLs outside the normal search path.</summary>
    static void EnsureResolver()
    {
        if (_resolverSet) return;
        _resolverSet = true;
        NativeLibrary.SetDllImportResolver(typeof(NpcapLive).Assembly, (name, assembly, path) =>
        {
            if (!name.Equals("wpcap", StringComparison.OrdinalIgnoreCase)) return IntPtr.Zero;
            var full = Path.Combine(NpcapDirectory, "wpcap.dll");
            return File.Exists(full) && NativeLibrary.TryLoad(full, out var handle) ? handle : IntPtr.Zero;
        });
    }

    public static List<CaptureDevice> ListDevices()
    {
        EnsureResolver();
        var devices = new List<CaptureDevice>();
        var error = new byte[PcapErrorBufferSize];
        if (pcap_findalldevs(out var list, error) != 0 || list == IntPtr.Zero) return devices;
        try
        {
            for (IntPtr p = list; p != IntPtr.Zero;)
            {
                var entry = Marshal.PtrToStructure<PcapInterface>(p);
                devices.Add(new CaptureDevice(
                    Marshal.PtrToStringAnsi(entry.Name) ?? "",
                    Marshal.PtrToStringAnsi(entry.Description) ?? ""));
                p = entry.Next;
            }
        }
        finally
        {
            pcap_freealldevs(list);
        }
        return devices;
    }

    /// <summary>
    /// Asks Npcap's helper to put an adapter into monitor mode and park it on a channel.
    /// Uses the Windows adapter name shown in Network Connections, not the pcap device name.
    /// </summary>
    public static string SetMonitorMode(string adapterName, bool monitor, int? channel)
    {
        var helper = Path.Combine(NpcapDirectory, "WlanHelper.exe");
        if (!File.Exists(helper)) return "WlanHelper.exe wasn't found; install Npcap with raw 802.11 support.";

        var output = new List<string>();
        foreach (var args in channel is int c && monitor
                     ? new[] { $"\"{adapterName}\" mode monitor", $"\"{adapterName}\" channel {c}" }
                     : new[] { $"\"{adapterName}\" mode {(monitor ? "monitor" : "managed")}" })
        {
            try
            {
                using var process = Process.Start(new ProcessStartInfo(helper, args)
                {
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                    CreateNoWindow = true
                });
                if (process == null) return "Couldn't run WlanHelper.exe.";
                output.Add(process.StandardOutput.ReadToEnd().Trim());
                output.Add(process.StandardError.ReadToEnd().Trim());
                process.WaitForExit(5000);
            }
            catch (Exception ex)
            {
                return ex.Message;
            }
        }
        return string.Join(" ", output.Where(o => o.Length > 0));
    }

    public static Task<List<CapturedFrame>> CaptureAsync(string deviceName, TimeSpan duration,
        bool monitorMode, IProgress<string>? progress, CancellationToken ct) =>
        Task.Run(() => Capture(deviceName, duration, monitorMode, progress, ct), ct);

    static List<CapturedFrame> Capture(string deviceName, TimeSpan duration, bool monitorMode,
        IProgress<string>? progress, CancellationToken ct)
    {
        EnsureResolver();
        var error = new byte[PcapErrorBufferSize];
        IntPtr handle = pcap_create(deviceName, error);
        if (handle == IntPtr.Zero) throw new InvalidOperationException("Couldn't open the adapter: " + Text(error));

        try
        {
            pcap_set_snaplen(handle, 2048);
            pcap_set_promisc(handle, 1);
            pcap_set_timeout(handle, 100);
            if (monitorMode && pcap_can_set_rfmon(handle) == 1) pcap_set_rfmon(handle, 1);
            else if (monitorMode) progress?.Report("This adapter reports that it can't be set to monitor mode.");

            int status = pcap_activate(handle);
            if (status < 0)
                throw new InvalidOperationException("Couldn't start the capture: " +
                                                    (Marshal.PtrToStringAnsi(pcap_geterr(handle)) ?? status.ToString()));

            int linkType = pcap_datalink(handle);
            if (linkType != PcapReader.LinkRadiotap && linkType != PcapReader.LinkDot11)
                throw new InvalidOperationException(
                    "The adapter is delivering Ethernet frames, not 802.11. Monitor mode isn't active.");

            var frames = new List<CapturedFrame>();
            var deadline = DateTime.UtcNow + duration;
            var lastReport = DateTime.UtcNow;

            while (DateTime.UtcNow < deadline && !ct.IsCancellationRequested && frames.Count < 500_000)
            {
                int result = pcap_next_ex(handle, out var headerPtr, out var dataPtr);
                if (result == 0) continue;            // timeout, no frame this round
                if (result < 0) break;

                var header = Marshal.PtrToStructure<PcapPacketHeader>(headerPtr);
                int length = (int)Math.Min(header.CaptureLength, 65_535);
                if (length <= 0) continue;

                var data = new byte[length];
                Marshal.Copy(dataPtr, data, 0, length);
                frames.Add(new CapturedFrame(
                    DateTimeOffset.FromUnixTimeSeconds(header.Seconds).UtcDateTime
                        .AddTicks(header.Microseconds * 10),
                    data, linkType));

                if (progress != null && (DateTime.UtcNow - lastReport).TotalMilliseconds > 500)
                {
                    lastReport = DateTime.UtcNow;
                    progress.Report($"{frames.Count} frames…");
                }
            }
            return frames;
        }
        finally
        {
            pcap_close(handle);
        }
    }

    static string Text(byte[] buffer)
    {
        int end = Array.IndexOf(buffer, (byte)0);
        return System.Text.Encoding.ASCII.GetString(buffer, 0, end < 0 ? buffer.Length : end);
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PcapInterface
    {
        public IntPtr Next;
        public IntPtr Name;
        public IntPtr Description;
        public IntPtr Addresses;
        public uint Flags;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PcapPacketHeader
    {
        public int Seconds;        // struct timeval on Windows uses 32-bit fields
        public int Microseconds;
        public uint CaptureLength;
        public uint Length;
    }

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_findalldevs(out IntPtr alldevs, byte[] errbuf);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern void pcap_freealldevs(IntPtr alldevs);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    static extern IntPtr pcap_create(string source, byte[] errbuf);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_set_snaplen(IntPtr p, int snaplen);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_set_promisc(IntPtr p, int promisc);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_set_timeout(IntPtr p, int milliseconds);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_can_set_rfmon(IntPtr p);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_set_rfmon(IntPtr p, int rfmon);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_activate(IntPtr p);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_datalink(IntPtr p);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern int pcap_next_ex(IntPtr p, out IntPtr header, out IntPtr data);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern IntPtr pcap_geterr(IntPtr p);

    [DllImport("wpcap", CallingConvention = CallingConvention.Cdecl)]
    static extern void pcap_close(IntPtr p);
}
