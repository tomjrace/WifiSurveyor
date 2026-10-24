using System.Net.Http;
using System.Text.Json;

namespace WifiSurveyor.Services;

public sealed class IperfServer
{
    public string Host { get; set; } = "";
    public int FirstPort { get; set; } = 5201;
    public int LastPort { get; set; } = 5201;
    public string Options { get; set; } = "";
    public string Speed { get; set; } = "";
    public string Continent { get; set; } = "";
    public string Country { get; set; } = "";
    public string Site { get; set; } = "";
    public string Provider { get; set; } = "";

    public int PortCount => Math.Max(1, LastPort - FirstPort + 1);
    public string Key => $"{Host}:{FirstPort}";

    public string Display =>
        $"{Country} {Site} · {Provider}" +
        (Speed.Length > 0 ? $" ({Speed} Gb/s)" : "") +
        $" — {Host}:{FirstPort}" + (LastPort > FirstPort ? $"-{LastPort}" : "");

    public override string ToString() => Display;
}

/// <summary>
/// The public list from iperf3serverlist.net. The project publishes a parsed JSON export that
/// refreshes hourly, which is far steadier to read than the README table.
/// </summary>
public static class IperfServerDirectory
{
    public const string SourceUrl = "https://export.iperf3serverlist.net/listed_iperf3_servers.json";

    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(25) };
    static string CachePath => Path.Combine(AppPaths.DataDir, "iperf-servers.json");

    sealed class Cache
    {
        public DateTime Fetched { get; set; }
        public List<IperfServer> Servers { get; set; } = new();
    }

    public static DateTime? CachedAt { get; private set; }

    public static List<IperfServer> LoadCached()
    {
        try
        {
            if (!File.Exists(CachePath)) return new List<IperfServer>();
            var cache = JsonSerializer.Deserialize<Cache>(File.ReadAllText(CachePath));
            if (cache == null) return new List<IperfServer>();
            CachedAt = cache.Fetched;
            return cache.Servers;
        }
        catch
        {
            return new List<IperfServer>();
        }
    }

    /// <summary>Returns the cached list when it's recent, otherwise downloads a fresh one.</summary>
    public static async Task<List<IperfServer>> GetAsync(bool force, CancellationToken ct)
    {
        var cached = LoadCached();
        if (!force && cached.Count > 0 && CachedAt is DateTime when &&
            DateTime.UtcNow - when < TimeSpan.FromHours(6))
            return cached;

        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, SourceUrl);
            request.Headers.Add("User-Agent", "WifiSurveyor");
            using var response = await Http.SendAsync(request, ct);
            response.EnsureSuccessStatusCode();
            var servers = Parse(await response.Content.ReadAsStringAsync(ct));
            if (servers.Count == 0) throw new InvalidDataException("The list came back empty.");

            CachedAt = DateTime.UtcNow;
            try
            {
                File.WriteAllText(CachePath,
                    JsonSerializer.Serialize(new Cache { Fetched = CachedAt.Value, Servers = servers }));
            }
            catch { /* a cache we can't write is not worth failing over */ }
            return servers;
        }
        catch when (cached.Count > 0)
        {
            return cached;   // offline: yesterday's list beats no list
        }
    }

    static List<IperfServer> Parse(string json)
    {
        var servers = new List<IperfServer>();
        using var document = JsonDocument.Parse(json);
        if (document.RootElement.ValueKind != JsonValueKind.Array) return servers;

        foreach (var item in document.RootElement.EnumerateArray())
        {
            string Text(string name) =>
                item.TryGetProperty(name, out var value) ? value.GetString()?.Trim() ?? "" : "";

            var host = Text("IP/HOST");
            if (host.Length == 0) continue;

            var (first, last) = ParsePorts(Text("PORT"));
            servers.Add(new IperfServer
            {
                Host = host,
                FirstPort = first,
                LastPort = last,
                Options = Text("OPTIONS"),
                Speed = Text("GB/S"),
                Continent = Text("CONTINENT"),
                Country = Text("COUNTRY"),
                Site = Text("SITE"),
                Provider = Text("PROVIDER")
            });
        }
        return servers;
    }

    /// <summary>PORT is either "5201" or a range like "5201-5209".</summary>
    static (int First, int Last) ParsePorts(string text)
    {
        if (text.Length == 0) return (5201, 5201);
        var parts = text.Split('-', StringSplitOptions.RemoveEmptyEntries);
        if (!int.TryParse(parts[0].Trim(), out var first)) return (5201, 5201);
        int last = parts.Length > 1 && int.TryParse(parts[1].Trim(), out var parsed) ? parsed : first;
        return (first, Math.Max(first, last));
    }
}
