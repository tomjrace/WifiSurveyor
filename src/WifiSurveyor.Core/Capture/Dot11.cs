using System.Buffers.Binary;
using System.Text;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Capture;

public readonly record struct Dot11Frame(
    int Type,          // 0 management, 1 control, 2 data
    int Subtype,
    bool Retry,
    bool ToDs,
    bool FromDs,
    string? Addr1,
    string? Addr2,
    string? Addr3,
    int HeaderLength,
    int Length)
{
    public bool IsBeacon => Type == 0 && Subtype == 8;
    public bool IsProbeResponse => Type == 0 && Subtype == 5;
    public bool IsDeauth => Type == 0 && Subtype == 12;
    public bool IsDisassoc => Type == 0 && Subtype == 10;
    public bool IsData => Type == 2;
    public bool IsControl => Type == 1;

    /// <summary>Which BSS this frame belongs to, from the To/From DS bits.</summary>
    public string? Bssid => (ToDs, FromDs) switch
    {
        (true, false) => Addr1,
        (false, true) => Addr2,
        (false, false) => Type == 0 ? Addr3 : Addr3,
        _ => null   // wireless distribution system
    };
}

public static class Dot11
{
    public static Dot11Frame? Parse(byte[] data, int offset, int length)
    {
        if (length < 10) return null;
        ushort frameControl = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset, 2));
        int type = (frameControl >> 2) & 0x03;
        int subtype = (frameControl >> 4) & 0x0F;
        bool toDs = (frameControl & 0x0100) != 0;
        bool fromDs = (frameControl & 0x0200) != 0;
        bool retry = (frameControl & 0x0800) != 0;

        // Control frames carry fewer addresses; ACK and CTS carry only the receiver.
        bool addr1Only = type == 1 && subtype is 0x0C or 0x0D;
        string? a1 = length >= 10 ? Mac(data, offset + 4) : null;
        string? a2 = !addr1Only && length >= 16 ? Mac(data, offset + 10) : null;
        string? a3 = type != 1 && length >= 22 ? Mac(data, offset + 16) : null;

        int header = type == 1 ? (addr1Only ? 10 : 16) : 24;
        if (type == 2 && (subtype & 0x08) != 0) header += 2;   // QoS control
        if (toDs && fromDs) header += 6;                        // fourth address

        return new Dot11Frame(type, subtype, retry, toDs, fromDs, a1, a2, a3, header, length);
    }

    static string Mac(byte[] data, int offset) =>
        string.Join(":", Enumerable.Range(0, 6).Select(i => data[offset + i].ToString("X2")));

    public static bool IsGroupAddress(string? mac) =>
        mac != null && mac.Length >= 2 && Convert.ToInt32(mac[..2], 16) % 2 == 1;

    /// <summary>
    /// Airtime a frame occupied, in microseconds. Approximate: preamble plus payload at the
    /// reported rate, plus the acknowledgement exchange for unicast data and management frames.
    /// </summary>
    public static double AirtimeUs(int bytes, double? rateMbps, bool isDsss, bool expectsAck)
    {
        double rate = rateMbps is > 0 ? rateMbps.Value : 6;   // assume the slowest OFDM rate
        double preamble = isDsss ? 192 : 20;
        double time = preamble + bytes * 8.0 / rate;
        if (expectsAck) time += 16 + 28;                      // SIFS plus an ACK at a basic rate
        return time;
    }
}

/// <summary>Everything worth having from a beacon's information elements.</summary>
public sealed class BeaconInfo
{
    public string? Ssid { get; set; }
    public int? Channel { get; set; }
    public int? WidthMHz { get; set; }
    public int? Streams { get; set; }

    /// <summary>The AP's own channel utilisation, 0–1, from the BSS Load element.</summary>
    public double? Utilization { get; set; }
    public int? Stations { get; set; }
    public string? Security { get; set; }
    public bool? ProtectedManagementFrames { get; set; }
    public string Generation { get; set; } = "";
    public bool SupportsRrm { get; set; }          // 802.11k
    public bool SupportsBssTransition { get; set; } // 802.11v
    public bool SupportsFastRoaming { get; set; }   // 802.11r
    public int NeighborReports { get; set; }        // 6 GHz APs advertised by this beacon
}

public static class InformationElements
{
    /// <summary>Parses the IE list that follows a beacon or probe response's fixed fields.</summary>
    public static BeaconInfo Parse(byte[] data, int offset, int end)
    {
        var info = new BeaconInfo();
        bool ht = false, vht = false, he = false, eht = false;
        int? htWidth = null, vhtWidth = null;

        int pos = offset;
        while (pos + 2 <= end)
        {
            int id = data[pos], length = data[pos + 1];
            int body = pos + 2;
            if (body + length > end) break;

            switch (id)
            {
                case 0:   // SSID
                    info.Ssid = Encoding.UTF8.GetString(data, body, length).TrimEnd('\0');
                    break;
                case 3:   // DS parameter set
                    if (length >= 1) info.Channel = data[body];
                    break;
                case 7:   // country
                    break;
                case 11:  // BSS Load: the AP's own view of how busy the channel is
                    if (length >= 5)
                    {
                        info.Stations = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(body, 2));
                        info.Utilization = data[body + 2] / 255.0;
                    }
                    break;
                case 45:  // HT capabilities
                    ht = true;
                    if (length >= 21)
                    {
                        ushort caps = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(body, 2));
                        htWidth = (caps & 0x0002) != 0 ? 40 : 20;
                        int streams = 0;
                        for (int i = 0; i < 4; i++)
                            if (data[body + 3 + i] != 0) streams++;
                        if (streams > 0) info.Streams = streams;
                    }
                    break;
                case 48:  // RSN
                    ReadRsn(data, body, length, info);
                    break;
                case 61:  // HT operation
                    if (length >= 2 && (data[body + 1] & 0x04) != 0) htWidth = 40;
                    break;
                case 70:  // RM enabled capabilities (802.11k)
                    info.SupportsRrm = true;
                    break;
                case 54:  // mobility domain (802.11r)
                    info.SupportsFastRoaming = true;
                    break;
                case 191: // VHT capabilities
                    vht = true;
                    if (length >= 6)
                    {
                        uint caps = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(body, 4));
                        vhtWidth = ((caps >> 2) & 0x03) switch { 1 => 160, 2 => 160, _ => 80 };
                        ushort rxMap = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(body + 4, 2));
                        int streams = 0;
                        for (int i = 0; i < 8; i++)
                            if (((rxMap >> (i * 2)) & 0x03) != 0x03) streams++;
                        if (streams > 0) info.Streams = Math.Max(info.Streams ?? 0, streams);
                    }
                    break;
                case 192: // VHT operation
                    if (length >= 1)
                        vhtWidth = data[body] switch { 1 => 80, 2 => 160, 3 => 160, _ => vhtWidth ?? 40 };
                    break;
                case 201: // reduced neighbor report: often the same AP's 6 GHz radios
                    info.NeighborReports++;
                    break;
                case 221: // vendor specific
                    if (length >= 4 && data[body] == 0x00 && data[body + 1] == 0x50 &&
                        data[body + 2] == 0xF2 && data[body + 3] == 0x01)
                        info.Security ??= "WPA";
                    break;
                case 255: // element extension
                    if (length >= 1)
                    {
                        int ext = data[body];
                        if (ext is 35 or 36) he = true;
                        if (ext is 106 or 108) eht = true;
                    }
                    break;
            }

            if (id == 60 || id == 71) { /* extended channel switch, multiple BSSID */ }
            pos = body + length;
        }

        info.WidthMHz = vhtWidth ?? htWidth ?? 20;
        info.Generation = eht ? "Wi-Fi 7 (be)" : he ? "Wi-Fi 6/6E (ax)" : vht ? "Wi-Fi 5 (ac)" : ht ? "Wi-Fi 4 (n)" : "legacy";
        info.Security ??= "Open";
        return info;
    }

    static void ReadRsn(byte[] data, int body, int length, BeaconInfo info)
    {
        if (length < 8) return;
        int pos = body + 2;                                   // skip version
        pos += 4;                                             // group cipher
        if (pos + 2 > body + length) return;
        int pairwise = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(pos, 2));
        pos += 2 + pairwise * 4;
        if (pos + 2 > body + length) return;
        int akmCount = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(pos, 2));
        pos += 2;

        var names = new List<string>();
        for (int i = 0; i < akmCount && pos + 4 <= body + length; i++, pos += 4)
        {
            names.Add(data[pos + 3] switch
            {
                1 => "WPA2-Enterprise",
                2 => "WPA2-Personal",
                3 or 4 or 9 => "WPA2-Personal (FT)",
                5 => "WPA2-Enterprise (FT)",
                8 => "WPA3-Personal (SAE)",
                11 or 12 or 13 => "WPA3-Enterprise",
                18 => "OWE",
                _ => $"AKM {data[pos + 3]}"
            });
        }
        if (names.Count > 0) info.Security = string.Join(", ", names.Distinct());
        if (names.Any(n => n.Contains("FT"))) info.SupportsFastRoaming = true;

        if (pos + 2 <= body + length)
        {
            ushort caps = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(pos, 2));
            info.ProtectedManagementFrames = (caps & 0x0040) != 0 || (caps & 0x0080) != 0;
        }
    }

    public static double? CenterFrequency(int? channel)
    {
        if (channel is not int c) return null;
        var match = WifiChannels.All.FirstOrDefault(x => x.Number == c);
        return match?.CenterMHz;
    }
}
