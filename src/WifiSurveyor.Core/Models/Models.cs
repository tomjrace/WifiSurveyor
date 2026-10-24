using System.Text.Json.Serialization;

namespace WifiSurveyor.Models;

/// <summary>One BSSID as seen by a Windows Wi-Fi scan.</summary>
public record WifiReading(
    string Ssid,
    string Bssid,
    int Rssi,
    int LinkQuality,
    int Channel,
    double FrequencyMHz,
    double BandGHz,
    bool? Secured,
    string Vendor);

/// <summary>The link the laptop is currently associated with.</summary>
public record ConnectionInfo(
    string Ssid,
    string Bssid,
    int SignalQuality,
    double RxRateMbps,
    double TxRateMbps,
    string PhyType,
    string Auth,
    string? Adapter = null)
{
    /// <summary>Windows maps 0–100 quality linearly onto −100…−50 dBm.</summary>
    [JsonIgnore] public int EstimatedRssi => SignalQuality / 2 - 100;
}

/// <summary>Summary of several RF Explorer sweeps over one frequency range.</summary>
public class SpectrumSegment
{
    public double StartMHz { get; set; }
    public double StepMHz { get; set; }
    public double RbwKHz { get; set; }
    public int SweepCount { get; set; }
    public double[] Avg { get; set; } = Array.Empty<double>();
    public double[] Max { get; set; } = Array.Empty<double>();
    public double[] Min { get; set; } = Array.Empty<double>();

    /// <summary>Wi-Fi channel number → fraction (0–1) of sweeps where the channel was busy.</summary>
    public Dictionary<int, double> ChannelOccupancy { get; set; } = new();

    [JsonIgnore] public double EndMHz => StartMHz + StepMHz * Math.Max(0, Avg.Length - 1);
    [JsonIgnore] public string Band => StartMHz < 3000 ? "2.4" : "5";
}

public class SpectrumCapture
{
    public List<SpectrumSegment> Segments { get; set; } = new();

    /// <summary>Noise floor normalised to a 20 MHz channel, in dBm.</summary>
    public double? NoiseFloor24 { get; set; }
    public double? NoiseFloor5 { get; set; }

    /// <summary>
    /// Optional long capture of a single 20 MHz channel. A swept analyzer revisits a wide
    /// range too slowly to catch short bursts, so airtime figures from this are far more
    /// trustworthy than those from the wideband sweeps.
    /// </summary>
    public SpectrumSegment? Dwell { get; set; }
    public double? DwellCenterMHz { get; set; }
}

public enum FixQuality
{
    None = 0,
    Gps = 1,
    Dgps = 2,
    Pps = 3,
    RtkFixed = 4,
    RtkFloat = 5,
    DeadReckoning = 6,
    Network = 98,
    Manual = 99
}

public record GeoFix(
    double Lat,
    double Lon,
    double? AltM,
    FixQuality Quality,
    int Satellites,
    double? Hdop,
    double? AccuracyM,
    DateTime UtcTime,
    string Source)
{
    /// <summary>Good enough to place a survey point automatically.</summary>
    [JsonIgnore]
    public bool IsGood =>
        Quality is not FixQuality.None &&
        ((Hdop is double h && h <= 2.0 && Satellites >= 5) ||
         (AccuracyM is double a && a <= 5));

    [JsonIgnore] public bool IsFresh => DateTime.UtcNow - UtcTime < TimeSpan.FromSeconds(5);
}

public class SurveyPoint
{
    public Guid Id { get; set; } = Guid.NewGuid();

    /// <summary>Position in floor-plan image pixels.</summary>
    public double X { get; set; }
    public double Y { get; set; }

    public DateTime Time { get; set; }
    public List<WifiReading> Networks { get; set; } = new();
    public ConnectionInfo? Connection { get; set; }

    /// <summary>Every WLAN adapter's link at this point, for multi-client testing.</summary>
    public List<ConnectionInfo> Links { get; set; } = new();
    public SpectrumCapture? Spectrum { get; set; }
    public GeoFix? Gps { get; set; }
    public double? ThroughputDownMbps { get; set; }
    public double? ThroughputUpMbps { get; set; }
    /// <summary>Monitor-mode capture taken at this point, when one was made.</summary>
    public CaptureSummary? Capture { get; set; }

    public List<string> Notes { get; set; } = new();
}

/// <summary>Per-BSS figures from a monitor-mode capture.</summary>
public class BssStat
{
    public string Bssid { get; set; } = "";
    public string Ssid { get; set; } = "";
    public int Channel { get; set; }
    public int WidthMHz { get; set; }
    public int? Streams { get; set; }
    public string Generation { get; set; } = "";
    public string Security { get; set; } = "";
    public bool? ProtectedManagementFrames { get; set; }

    /// <summary>Channel utilisation as the AP itself reports it, from the BSS Load element.</summary>
    public double? ReportedUtilization { get; set; }
    public int? ReportedStations { get; set; }
    public bool SupportsFastRoaming { get; set; }
    public bool SupportsRrm { get; set; }

    public int Frames { get; set; }
    public int Retries { get; set; }
    public double RetryRate { get; set; }
    public double AirtimeFraction { get; set; }
    public int Clients { get; set; }
    public double? AvgRssi { get; set; }
}

/// <summary>
/// What a monitor-mode capture saw on one channel. Airtime and retries here are measured
/// from the frames themselves rather than inferred from energy.
/// </summary>
public class CaptureSummary
{
    public string? Source { get; set; }
    public DateTime Start { get; set; }
    public double Seconds { get; set; }
    public int Channel { get; set; }
    public double? CenterMHz { get; set; }

    public int Frames { get; set; }
    public int Management { get; set; }
    public int Control { get; set; }
    public int Data { get; set; }
    public int BadFcs { get; set; }
    public int AckFrames { get; set; }
    public int Retries { get; set; }
    public int RetryEligible { get; set; }
    public int Deauths { get; set; }
    public int Disassocs { get; set; }

    public double RetryRate { get; set; }
    public double AirtimeFraction { get; set; }
    public int Clients { get; set; }
    public List<BssStat> Bss { get; set; } = new();
}

/// <summary>A wall or obstacle drawn on the plan, with the loss it adds to a crossing path.</summary>
public class Barrier
{
    public double X1 { get; set; }
    public double Y1 { get; set; }
    public double X2 { get; set; }
    public double Y2 { get; set; }
    public string Material { get; set; } = "Drywall";
    public double Db { get; set; } = 3;

    /// <summary>Found automatically and not yet confirmed; ignored by the path-loss maths.</summary>
    public bool Suggested { get; set; }

    /// <summary>Worked out from the measurements rather than drawn or found in the image.</summary>
    public bool Inferred { get; set; }
}

public enum StabilityIssue { Disconnect, Reconnect, Roam, RateDrop, ThroughputFail, Stall }

/// <summary>Something that went wrong on a walk, and where it happened.</summary>
public class StabilityEvent
{
    public double X { get; set; }
    public double Y { get; set; }
    public DateTime Time { get; set; }
    public string Adapter { get; set; } = "";
    public StabilityIssue Issue { get; set; }
    public string Detail { get; set; } = "";
    public double? Mbps { get; set; }
}

public class TrackPoint
{
    public double X { get; set; }
    public double Y { get; set; }
    public DateTime Time { get; set; }
    public bool FromGps { get; set; }
}

public class GeoAnchor
{
    public double X { get; set; }
    public double Y { get; set; }
    public double Lat { get; set; }
    public double Lon { get; set; }
}

public class GeoReference
{
    public List<GeoAnchor> Anchors { get; set; } = new();
}

public class SurveyProject
{
    public int FormatVersion { get; set; } = 1;
    public string Name { get; set; } = "Untitled survey";

    /// <summary>Original image bytes (PNG/JPEG/…); null means a blank grid canvas.</summary>
    public string? FloorPlanBase64 { get; set; }
    public int FloorPlanWidth { get; set; } = 1600;
    public int FloorPlanHeight { get; set; } = 1000;

    public double? MetersPerPixel { get; set; }
    public GeoReference GeoRef { get; set; } = new();
    public List<Barrier> Barriers { get; set; } = new();

    /// <summary>The path walked during stability runs, and what failed along it.</summary>
    public List<TrackPoint> Track { get; set; } = new();
    public List<StabilityEvent> Issues { get; set; } = new();
    public List<SurveyPoint> Points { get; set; } = new();
}
