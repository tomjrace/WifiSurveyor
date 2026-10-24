using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using WifiSurveyor.Models;

namespace WifiSurveyor.Wifi;

public sealed record WlanInterfaceInfo(Guid Id, string Description, int State)
{
    public bool IsConnected => State == 1;

    public string StateText => State switch
    {
        0 => "Not ready",
        1 => "Connected",
        2 => "Ad hoc",
        3 => "Disconnecting",
        4 => "Disconnected",
        5 => "Associating",
        6 => "Discovering",
        7 => "Authenticating",
        _ => "Unknown"
    };

    /// <summary>Local IPv4 address, needed to bind iperf3 to this adapter.</summary>
    public string? LocalIPv4()
    {
        try
        {
            var match = NetworkInterface.GetAllNetworkInterfaces()
                .FirstOrDefault(n => Guid.TryParse(n.Id.Trim('{', '}'), out var g) && g == Id);
            return match?.GetIPProperties().UnicastAddresses
                .FirstOrDefault(a => a.Address.AddressFamily == AddressFamily.InterNetwork)?.Address.ToString();
        }
        catch
        {
            return null;
        }
    }
}

/// <summary>
/// Reads WLAN interfaces and their current connections straight from wlanapi.dll.
/// Offsets follow WLAN_CONNECTION_ATTRIBUTES / WLAN_INTERFACE_INFO_LIST in wlanapi.h.
/// </summary>
public static class WlanConnection
{
    const int OpcodeCurrentConnection = 7;   // wlan_intf_opcode_current_connection
    const int StateConnected = 1;
    const int InterfaceInfoSize = 532;       // GUID(16) + WCHAR[256](512) + state(4)

    // WLAN_CONNECTION_ATTRIBUTES offsets
    const int OffSsidLen = 520, OffSsid = 524, OffBssid = 560, OffPhy = 568,
              OffQuality = 576, OffRx = 580, OffTx = 584, OffAuth = 596;

    public static List<WlanInterfaceInfo> ListInterfaces() =>
        WithClient(ListFrom, new List<WlanInterfaceInfo>());

    /// <summary>The current connection, for one adapter or the first connected one.</summary>
    public static ConnectionInfo? GetCurrent(Guid? interfaceId = null) =>
        WithClient<ConnectionInfo?>(client =>
        {
            foreach (var iface in ListFrom(client))
            {
                if (interfaceId != null && iface.Id != interfaceId) continue;
                if (Query(client, iface) is { } info) return info;
            }
            return null;
        }, null);

    /// <summary>Every adapter's link state, including the ones that aren't connected.</summary>
    public static List<(WlanInterfaceInfo Interface, ConnectionInfo? Connection)> AllLinks() =>
        WithClient(client => ListFrom(client).Select(i => (i, Query(client, i))).ToList(),
            new List<(WlanInterfaceInfo, ConnectionInfo?)>());

    /// <summary>
    /// Asks the chosen adapters to scan, leaving the rest alone. A scan request on an adapter
    /// in monitor mode knocks it back to normal operation, so this is how monitoring survives
    /// the app's own background scanning.
    /// </summary>
    public static int RequestScans(IEnumerable<Guid> interfaces) =>
        WithClient(client =>
        {
            int requested = 0;
            foreach (var id in interfaces)
            {
                var guid = id;
                if (WlanScan(client, ref guid, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero) == 0) requested++;
            }
            return requested;
        }, 0);

    static T WithClient<T>(Func<IntPtr, T> body, T fallback)
    {
        IntPtr client = IntPtr.Zero;
        try
        {
            if (WlanOpenHandle(2, IntPtr.Zero, out _, out client) != 0) return fallback;
            return body(client);
        }
        catch (DllNotFoundException)
        {
            return fallback;   // no WLAN service on this machine
        }
        finally
        {
            if (client != IntPtr.Zero) WlanCloseHandle(client, IntPtr.Zero);
        }
    }

    static List<WlanInterfaceInfo> ListFrom(IntPtr client)
    {
        var interfaces = new List<WlanInterfaceInfo>();
        if (WlanEnumInterfaces(client, IntPtr.Zero, out var list) != 0) return interfaces;
        try
        {
            int count = Marshal.ReadInt32(list, 0);
            for (int i = 0; i < count; i++)
            {
                IntPtr item = list + 8 + i * InterfaceInfoSize;
                var id = Marshal.PtrToStructure<Guid>(item);
                string description = (Marshal.PtrToStringUni(item + 16, 256) ?? "").TrimEnd('\0').Trim();
                interfaces.Add(new WlanInterfaceInfo(id,
                    description.Length == 0 ? "WLAN adapter" : description,
                    Marshal.ReadInt32(item, 528)));
            }
        }
        finally
        {
            WlanFreeMemory(list);
        }
        return interfaces;
    }

    static ConnectionInfo? Query(IntPtr client, WlanInterfaceInfo iface)
    {
        var guid = iface.Id;
        if (WlanQueryInterface(client, ref guid, OpcodeCurrentConnection, IntPtr.Zero,
                out _, out var data, out _) != 0)
            return null;
        try
        {
            if (Marshal.ReadInt32(data, 0) != StateConnected) return null;

            int ssidLen = Math.Clamp(Marshal.ReadInt32(data, OffSsidLen), 0, 32);
            var ssid = new byte[ssidLen];
            Marshal.Copy(data + OffSsid, ssid, 0, ssidLen);
            var bssid = new byte[6];
            Marshal.Copy(data + OffBssid, bssid, 0, 6);

            uint rxKbps = (uint)Marshal.ReadInt32(data, OffRx);
            uint txKbps = (uint)Marshal.ReadInt32(data, OffTx);

            return new ConnectionInfo(
                Encoding.UTF8.GetString(ssid),
                MacFormat.Format(bssid),
                Marshal.ReadInt32(data, OffQuality),
                rxKbps / 1000.0,
                txKbps / 1000.0,
                PhyName(Marshal.ReadInt32(data, OffPhy)),
                AuthName(Marshal.ReadInt32(data, OffAuth)),
                iface.Description);
        }
        finally
        {
            WlanFreeMemory(data);
        }
    }

    static string PhyName(int phy) => phy switch
    {
        2 => "802.11 (DSSS)",
        4 => "802.11a",
        5 => "802.11b",
        6 => "802.11g",
        7 => "802.11n (Wi-Fi 4)",
        8 => "802.11ac (Wi-Fi 5)",
        10 => "802.11ax (Wi-Fi 6/6E)",
        11 => "802.11be (Wi-Fi 7)",
        _ => $"PHY {phy}"
    };

    static string AuthName(int auth) => auth switch
    {
        1 => "Open",
        2 => "WEP (shared key)",
        3 => "WPA-Enterprise",
        4 => "WPA-Personal",
        6 => "WPA2-Enterprise",
        7 => "WPA2-Personal",
        8 => "WPA3-Enterprise 192-bit",
        9 => "WPA3-Personal (SAE)",
        10 => "OWE",
        11 => "WPA3-Enterprise",
        _ => $"Auth {auth}"
    };

    [DllImport("wlanapi.dll")]
    static extern uint WlanOpenHandle(uint clientVersion, IntPtr reserved,
        out uint negotiatedVersion, out IntPtr clientHandle);

    [DllImport("wlanapi.dll")]
    static extern uint WlanCloseHandle(IntPtr clientHandle, IntPtr reserved);

    [DllImport("wlanapi.dll")]
    static extern uint WlanEnumInterfaces(IntPtr clientHandle, IntPtr reserved, out IntPtr interfaceList);

    [DllImport("wlanapi.dll")]
    static extern uint WlanQueryInterface(IntPtr clientHandle, ref Guid interfaceGuid, int opCode,
        IntPtr reserved, out uint dataSize, out IntPtr data, out int opcodeValueType);

    [DllImport("wlanapi.dll")]
    static extern void WlanFreeMemory(IntPtr memory);

    [DllImport("wlanapi.dll")]
    static extern uint WlanScan(IntPtr clientHandle, ref Guid interfaceGuid, IntPtr ssid, IntPtr ieData,
        IntPtr reserved);
}
