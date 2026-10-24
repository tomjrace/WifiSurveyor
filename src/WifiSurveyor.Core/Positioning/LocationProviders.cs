using System.Diagnostics;
using System.Globalization;
using System.IO.Ports;
using WifiSurveyor.Models;

namespace WifiSurveyor.Positioning;

public interface ILocationProvider : IDisposable
{
    string Name { get; }
    GeoFix? LastFix { get; }
    event Action<GeoFix>? FixReceived;
    event Action<string>? StatusChanged;
    void Start();
}

/// <summary>NMEA 0183 sentence parsing (GGA for position/quality, RMC for speed/course).</summary>
public static class Nmea
{
    public static bool ChecksumOk(string line)
    {
        int star = line.IndexOf('*');
        if (!line.StartsWith('$') || star < 0 || star + 3 > line.Length) return false;
        byte sum = 0;
        for (int i = 1; i < star; i++) sum ^= (byte)line[i];
        return string.Equals(sum.ToString("X2"), line.Substring(star + 1, 2), StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>Returns a fix for a valid GGA sentence with a position, otherwise null.</summary>
    public static GeoFix? ParseGga(string line, string source)
    {
        if (!ChecksumOk(line)) return null;
        var f = line[1..line.IndexOf('*')].Split(',');
        if (f.Length < 10 || f[0].Length < 5 || !f[0].EndsWith("GGA", StringComparison.Ordinal)) return null;
        if (!int.TryParse(f[6], out var q) || q == 0) return null;
        if (f[2].Length == 0 || f[4].Length == 0) return null;

        return new GeoFix(
            Lat: ToDegrees(f[2], f[3]),
            Lon: ToDegrees(f[4], f[5]),
            AltM: ParseD(f[9]),
            Quality: (FixQuality)q,
            Satellites: int.TryParse(f[7], out var s) ? s : 0,
            Hdop: ParseD(f[8]),
            AccuracyM: null,
            UtcTime: DateTime.UtcNow,
            Source: source);
    }

    /// <summary>NMEA uses ddmm.mmmm (lat) and dddmm.mmmm (lon).</summary>
    static double ToDegrees(string value, string hemisphere)
    {
        double raw = double.Parse(value, CultureInfo.InvariantCulture);
        double deg = Math.Floor(raw / 100) + (raw % 100) / 60;
        return hemisphere is "S" or "W" ? -deg : deg;
    }

    static double? ParseD(string s) =>
        double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var d) ? d : null;
}

public sealed class NmeaGpsProvider : ILocationProvider
{
    readonly SerialPort _port;
    volatile bool _running;

    public NmeaGpsProvider(string portName, int baud)
    {
        _port = new SerialPort(portName, baud) { NewLine = "\n", ReadTimeout = 2000 };
    }

    public string PortName => _port.PortName;
    public string Name => $"USB GPS ({_port.PortName} @ {_port.BaudRate})";
    public GeoFix? LastFix { get; private set; }
    public event Action<GeoFix>? FixReceived;
    public event Action<string>? StatusChanged;

    public void Start()
    {
        _port.Open();
        _running = true;
        new Thread(ReadLoop) { IsBackground = true, Name = "NMEA reader" }.Start();
    }

    void ReadLoop()
    {
        while (_running)
        {
            try
            {
                var line = _port.ReadLine().Trim();
                var fix = Nmea.ParseGga(line, Name);
                if (fix == null) continue;
                LastFix = fix;
                FixReceived?.Invoke(fix);
            }
            catch (TimeoutException)
            {
                StatusChanged?.Invoke("No data from GPS");
            }
            catch (Exception ex) when (ex is FormatException or ArgumentException)
            {
                /* garbled sentence: skip */
            }
            catch (Exception ex)
            {
                if (_running) StatusChanged?.Invoke("GPS disconnected: " + ex.Message);
                _running = false;
            }
        }
    }

    public void Dispose()
    {
        _running = false;
        try { if (_port.IsOpen) _port.Close(); } catch { /* ignore */ }
        _port.Dispose();
    }
}

public static class GpsPortDetector
{
    static readonly int[] Bauds = { 9600, 4800, 38400, 115200 };

    /// <summary>Finds the first COM port producing valid NMEA sentences.</summary>
    public static async Task<(string Port, int Baud)?> DetectAsync(IEnumerable<string> exclude)
    {
        var skip = new HashSet<string>(exclude, StringComparer.OrdinalIgnoreCase);
        foreach (var port in SerialPort.GetPortNames().Where(p => !skip.Contains(p)))
            foreach (var baud in Bauds)
                if (await Task.Run(() => Probe(port, baud)))
                    return (port, baud);
        return null;
    }

    static bool Probe(string port, int baud)
    {
        try
        {
            using var sp = new SerialPort(port, baud) { NewLine = "\n", ReadTimeout = 1500 };
            sp.Open();
            var sw = Stopwatch.StartNew();
            int good = 0;
            while (sw.ElapsedMilliseconds < 2500)
            {
                var line = sp.ReadLine().Trim();
                if (line.StartsWith("$G", StringComparison.Ordinal) && Nmea.ChecksumOk(line) && ++good >= 2)
                    return true;
            }
        }
        catch { /* busy port, timeout, wrong baud */ }
        return false;
    }
}

public static class GeoText
{
    public static string Describe(GeoFix? f)
    {
        if (f == null) return "no fix";
        var parts = new List<string> { f.Quality.ToString() };
        if (f.Satellites > 0) parts.Add($"{f.Satellites} sats");
        if (f.Hdop is double h) parts.Add($"HDOP {h:0.0}");
        if (f.AccuracyM is double a) parts.Add($"±{a:0} m");
        var text = string.Join(", ", parts);
        if (!f.IsFresh) text += " (stale)";
        else if (!f.IsGood) text += " (weak)";
        return text;
    }
}
