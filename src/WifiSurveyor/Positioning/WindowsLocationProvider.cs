using Windows.Devices.Geolocation;
using WifiSurveyor.Models;

namespace WifiSurveyor.Positioning;

/// <summary>Windows Location API: built-in GNSS on tablets, otherwise Wi-Fi/IP positioning.</summary>
public sealed class WindowsLocationProvider : ILocationProvider
{
    Geolocator? _geo;

    public string Name => "Windows Location";
    public GeoFix? LastFix { get; private set; }
    public event Action<GeoFix>? FixReceived;
    public event Action<string>? StatusChanged;

    public void Start() => _ = StartAsync();

    async Task StartAsync()
    {
        try
        {
            var access = await Geolocator.RequestAccessAsync();
            if (access != GeolocationAccessStatus.Allowed)
            {
                StatusChanged?.Invoke("Location access is turned off in Windows settings.");
                return;
            }
            _geo = new Geolocator { DesiredAccuracy = PositionAccuracy.High, ReportInterval = 1000 };
            _geo.PositionChanged += OnPositionChanged;
        }
        catch (Exception ex)
        {
            StatusChanged?.Invoke("Windows Location failed: " + ex.Message);
        }
    }

    void OnPositionChanged(Geolocator sender, PositionChangedEventArgs args)
    {
        var c = args.Position.Coordinate;
        var p = c.Point.Position;
        var fix = new GeoFix(
            p.Latitude, p.Longitude, p.Altitude,
            c.PositionSource == PositionSource.Satellite ? FixQuality.Gps : FixQuality.Network,
            Satellites: 0,
            Hdop: c.SatelliteData?.HorizontalDilutionOfPrecision,
            AccuracyM: c.Accuracy,
            UtcTime: DateTime.UtcNow,
            Source: Name);
        LastFix = fix;
        FixReceived?.Invoke(fix);
    }

    public void Dispose()
    {
        if (_geo != null) _geo.PositionChanged -= OnPositionChanged;
        _geo = null;
    }
}
