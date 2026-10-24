using WifiSurveyor.Services;

namespace WifiSurveyor.Wifi;

public static class WifiChannels
{
    public record WifiChannel(int Number, double CenterMHz, string Band);

    public static readonly IReadOnlyList<WifiChannel> Band24 =
        Enumerable.Range(1, 13)
            .Select(n => new WifiChannel(n, 2412 + 5 * (n - 1), "2.4"))
            .Append(new WifiChannel(14, 2484, "2.4"))
            .ToList();

    static readonly int[] Numbers5 =
    {
        36, 40, 44, 48, 52, 56, 60, 64,
        100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
        149, 153, 157, 161, 165, 169, 173, 177
    };

    public static readonly IReadOnlyList<WifiChannel> Band5 =
        Numbers5.Select(n => new WifiChannel(n, 5000 + 5 * n, "5")).ToList();

    /// <summary>
    /// 6 GHz 20 MHz channels 1–233. Kept out of <see cref="All"/> because the numbers
    /// overlap 2.4 GHz channel numbers; look these up by frequency, not number.
    /// </summary>
    public static readonly IReadOnlyList<WifiChannel> Band6 =
        Enumerable.Range(0, 59).Select(i => 1 + 4 * i)
            .Select(n => new WifiChannel(n, 5950 + 5 * n, "6"))
            .ToList();

    /// <summary>6 GHz preferred scanning channels (5, 21, 37, …).</summary>
    public static bool IsPsc(int channel6) => channel6 >= 5 && (channel6 - 5) % 16 == 0;

    public static IEnumerable<WifiChannel> All => Band24.Concat(Band5);

    public static WifiChannel? Find(int number) => All.FirstOrDefault(c => c.Number == number);

    public static string BandLabel(double frequencyMHz) =>
        frequencyMHz < 3000 ? "2.4" : frequencyMHz < 5925 ? "5" : "6";
}

/// <summary>Vendor names from the IEEE OUI list (oui.csv, optional).</summary>
public static class OuiLookup
{
    static Dictionary<string, string>? _map;
    static string? _source;

    public static string Lookup(string bssid)
    {
        _map ??= Load();
        var hex = new string(bssid.Where(Uri.IsHexDigit).Take(6).ToArray()).ToUpperInvariant();
        if (hex.Length < 6) return "";
        // Locally administered bit: virtual BSSIDs on multi-SSID APs, or randomized MACs.
        if ((Convert.ToInt32(hex[..2], 16) & 0x02) != 0) return "(virtual BSSID)";
        return _map.TryGetValue(hex, out var v) ? v : "";
    }

    public static string Describe()
    {
        _map ??= Load();
        return _source == null
            ? "Vendor database not found. Download oui.csv from https://standards-oui.ieee.org/oui/oui.csv " +
              $"and place it next to WifiSurveyor.exe or in {AppPaths.DataDir}."
            : $"Vendor database: {_map.Count:N0} entries from {_source}";
    }

    static Dictionary<string, string> Load()
    {
        var map = new Dictionary<string, string>();
        var path = new[]
        {
            Path.Combine(AppContext.BaseDirectory, "oui.csv"),
            Path.Combine(AppPaths.DataDir, "oui.csv")
        }.FirstOrDefault(File.Exists);
        if (path == null) return map;

        try
        {
            // Columns: Registry, Assignment, Organization Name, Organization Address
            foreach (var line in File.ReadLines(path).Skip(1))
            {
                var f = SplitCsv(line);
                if (f.Count >= 3 && f[1].Length == 6) map[f[1].ToUpperInvariant()] = f[2].Trim();
            }
            _source = path;
        }
        catch { /* unreadable file: no vendor names */ }
        return map;
    }

    static List<string> SplitCsv(string line)
    {
        var fields = new List<string>();
        var sb = new System.Text.StringBuilder();
        bool quoted = false;
        for (int i = 0; i < line.Length; i++)
        {
            char c = line[i];
            if (quoted)
            {
                if (c == '"' && i + 1 < line.Length && line[i + 1] == '"') { sb.Append('"'); i++; }
                else if (c == '"') quoted = false;
                else sb.Append(c);
            }
            else if (c == '"') quoted = true;
            else if (c == ',') { fields.Add(sb.ToString()); sb.Clear(); if (fields.Count == 3) return fields; }
            else sb.Append(c);
        }
        fields.Add(sb.ToString());
        return fields;
    }
}
