using ManagedNativeWifi;
using WifiSurveyor.Models;

namespace WifiSurveyor.Wifi;

/// <summary>
/// All ManagedNativeWifi calls live in this file. If a package update renames a
/// member (e.g. BssNetworkPack.Bssid / Frequency), this is the only place to fix.
/// </summary>
public static class WifiScanner
{
    /// <summary>Readings in the last scan before duplicates from other adapters were merged.</summary>
    public static int LastRawCount { get; private set; }

    /// <summary>How many copies of the most-seen BSSID arrived, which is the number of scanning radios.</summary>
    public static int LastRadioCount { get; private set; }

    /// <summary>
    /// Windows returns the BSS list once per WLAN interface, so every extra adapter repeats
    /// every network. With merging on (the default) each BSSID is kept once, at the strongest
    /// reading any radio heard, which is the honest figure for coverage: it's what a client
    /// with the best antenna in the room would get.
    /// </summary>
    public static async Task<List<WifiReading>> ScanAsync(bool mergeDuplicates = true)
    {
        var excluded = Services.AppServices.Instance.ScanExclusions;
        if (excluded.Count == 0)
        {
            try
            {
                // Windows throttles real scans; this waits for fresh results or the timeout.
                await NativeWifi.ScanNetworksAsync(TimeSpan.FromSeconds(10));
            }
            catch (TimeoutException) { /* use whatever is cached */ }
        }
        else
        {
            // An adapter is in monitor mode: scan only the others, or it drops out of monitoring.
            var targets = WlanConnection.ListInterfaces().Select(i => i.Id).Where(id => !excluded.Contains(id));
            if (WlanConnection.RequestScans(targets) > 0)
                await Task.Delay(TimeSpan.FromSeconds(4));   // Windows completes a scan within four seconds
        }

        var secured = new Dictionary<string, bool>();
        foreach (var a in NativeWifi.EnumerateAvailableNetworks())
            secured[a.Ssid.ToString()] = a.IsSecurityEnabled;

        var readings = NativeWifi.EnumerateBssNetworks()
            .Select(b =>
            {
                var ssid = b.Ssid.ToString();
                var bssid = MacFormat.Normalize(b.Bssid.ToString());
                return new WifiReading(
                    Ssid: ssid,
                    Bssid: bssid,
                    Rssi: b.SignalStrength,           // RSSI in dBm
                    LinkQuality: b.LinkQuality,
                    Channel: b.Channel,
                    FrequencyMHz: b.Frequency / 1000.0,   // reported in kHz
                    BandGHz: b.Band,
                    Secured: secured.TryGetValue(ssid, out var s) ? s : null,
                    Vendor: OuiLookup.Lookup(bssid));
            })
            .ToList();

        LastRawCount = readings.Count;
        var byBssid = readings.GroupBy(r => r.Bssid, StringComparer.OrdinalIgnoreCase).ToList();
        LastRadioCount = byBssid.Count == 0 ? 0 : byBssid.Max(g => g.Count());

        if (!mergeDuplicates) return readings;

        return byBssid
            .Select(g => g.MaxBy(r => r.Rssi)!)
            .OrderByDescending(r => r.Rssi)
            .ToList();
    }
}

public static class MacFormat
{
    public static string Format(byte[] bytes) => string.Join(":", bytes.Select(b => b.ToString("X2")));

    public static string Normalize(string raw)
    {
        var hex = new string(raw.Where(Uri.IsHexDigit).ToArray()).ToUpperInvariant();
        if (hex.Length != 12) return raw.ToUpperInvariant();
        return string.Join(":", Enumerable.Range(0, 6).Select(i => hex.Substring(i * 2, 2)));
    }
}
