using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Xml.Linq;
using WifiSurveyor.Models;
using WifiSurveyor.Positioning;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

public static class ProjectStore
{
    public const string FileFilter = "Wi-Fi survey (*.wsurvey)|*.wsurvey";

    static readonly JsonSerializerOptions Options = new()
    {
        Converters = { new JsonStringEnumConverter() },
        NumberHandling = JsonNumberHandling.AllowNamedFloatingPointLiterals
    };

    public static void Save(string path, SurveyProject project)
    {
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(project, Options));
        File.Move(tmp, path, overwrite: true);   // never leave a half-written project
    }

    public static SurveyProject Load(string path) =>
        JsonSerializer.Deserialize<SurveyProject>(File.ReadAllText(path), Options)
        ?? throw new InvalidDataException("This file isn't a Wi-Fi survey project.");
}

public static class Exporters
{
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    public static (double? Lat, double? Lon) PositionOf(SurveyPoint p, GeoTransform? geo)
    {
        if (geo != null)
        {
            var (lat, lon) = geo.ToGeo(p.X, p.Y);
            return (lat, lon);
        }
        return p.Gps != null ? (p.Gps.Lat, p.Gps.Lon) : (null, null);
    }

    public static void WriteCsv(string path, SurveyProject project, GeoTransform? geo)
    {
        using var w = new StreamWriter(path, false, new UTF8Encoding(true));
        w.WriteLine(string.Join(",",
            "point", "time", "x_px", "y_px", "x_m", "y_m", "lat", "lon", "gps_quality", "gps_hdop",
            "ssid", "bssid", "vendor", "band", "channel", "freq_mhz", "rssi_dbm", "link_quality", "secured",
            "connected_ssid", "connected_bssid", "tx_mbps", "rx_mbps", "phy",
            "noise24_dbm", "noise5_dbm", "down_mbps", "up_mbps", "notes"));

        int index = 0;
        foreach (var p in project.Points)
        {
            index++;
            var (lat, lon) = PositionOf(p, geo);
            double? mpp = project.MetersPerPixel;
            var common = new object?[]
            {
                index, p.Time.ToString("s", Inv), p.X, p.Y,
                mpp * p.X, mpp * p.Y, lat, lon, p.Gps?.Quality.ToString(), p.Gps?.Hdop
            };
            var tail = new object?[]
            {
                p.Connection?.Ssid, p.Connection?.Bssid, p.Connection?.TxRateMbps, p.Connection?.RxRateMbps,
                p.Connection?.PhyType, p.Spectrum?.NoiseFloor24, p.Spectrum?.NoiseFloor5,
                p.ThroughputDownMbps, p.ThroughputUpMbps, string.Join(" | ", p.Notes)
            };

            if (p.Networks.Count == 0)
                w.WriteLine(Row(common.Concat(new object?[9]).Concat(tail)));

            foreach (var n in p.Networks)
                w.WriteLine(Row(common
                    .Concat(new object?[]
                    {
                        n.Ssid, n.Bssid, n.Vendor, WifiChannels.BandLabel(n.FrequencyMHz), n.Channel,
                        n.FrequencyMHz, n.Rssi, n.LinkQuality, n.Secured
                    })
                    .Concat(tail)));
        }
    }

    public static void WriteKml(string path, SurveyProject project, GeoTransform? geo,
        Func<SurveyPoint, double?> value, string metricTitle, string unit)
    {
        XNamespace k = "http://www.opengis.net/kml/2.2";
        var doc = new XElement(k + "Document", new XElement(k + "name", project.Name));
        int index = 0;
        foreach (var p in project.Points)
        {
            index++;
            var (lat, lon) = PositionOf(p, geo);
            if (lat == null || lon == null) continue;
            var v = value(p);
            var label = v.HasValue ? $"{v.Value.ToString("0.#", Inv)} {unit}" : "no data";
            doc.Add(new XElement(k + "Placemark",
                new XElement(k + "name", label),
                new XElement(k + "description",
                    $"Point {index} at {p.Time:g}. {metricTitle}: {label}. {p.Networks.Count} BSSIDs seen."),
                new XElement(k + "Point",
                    new XElement(k + "coordinates",
                        $"{lon.Value.ToString("0.0000000", Inv)},{lat.Value.ToString("0.0000000", Inv)},0"))));
        }
        new XDocument(new XElement(k + "kml", doc)).Save(path);
    }

    static string Row(IEnumerable<object?> values) => string.Join(",", values.Select(Cell));

    static string Cell(object? v) => v switch
    {
        null => "",
        double d => d.ToString("0.######", Inv),
        bool b => b ? "true" : "false",
        string s when s.IndexOfAny(new[] { ',', '"', '\n', '\r' }) >= 0 => "\"" + s.Replace("\"", "\"\"") + "\"",
        string s => s,
        IFormattable f => f.ToString(null, Inv),
        _ => v.ToString() ?? ""
    };
}
