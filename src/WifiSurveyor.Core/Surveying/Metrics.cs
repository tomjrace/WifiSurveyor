using WifiSurveyor.Models;
using WifiSurveyor.RF;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

public enum HeatmapMetric
{
    SignalStrength,
    SignalToNoise,
    NoiseFloor,
    ApCount,
    PhyRate,
    ThroughputDown,
    ThroughputUp,
    ChannelPower,
    ChannelOccupancy,
    UnexplainedEnergy,
    EstimatedThroughput,
    RetryRate,
    MeasuredAirtime,
    ClientCount,
    Diagnosis
}

public sealed record MetricInfo(
    HeatmapMetric Metric,
    string Title,
    string Unit,
    double Min,
    double Max,
    bool HigherIsBetter,
    bool AutoRange,
    string Needs,
    bool Categorical = false)
{
    public static MetricInfo Get(HeatmapMetric m) => m switch
    {
        HeatmapMetric.SignalStrength => new(m, "Signal level", "dBm", -85, -45, true, false,
            "a Wi-Fi scan"),
        HeatmapMetric.SignalToNoise => new(m, "Signal-to-noise ratio", "dB", 5, 40, true, false,
            "RF Explorer spectrum captures"),
        HeatmapMetric.NoiseFloor => new(m, "Noise floor (per 20 MHz)", "dBm", -95, -75, false, false,
            "RF Explorer spectrum captures"),
        HeatmapMetric.ApCount => new(m, "Overlapping access points (−75 dBm or stronger)", "APs", 0, 12, false, false,
            "a Wi-Fi scan"),
        HeatmapMetric.PhyRate => new(m, "PHY rate of the connected link", "Mbps", 0, 0, true, true,
            "a connected Wi-Fi link"),
        HeatmapMetric.ThroughputDown => new(m, "Download throughput (iperf3)", "Mbps", 0, 0, true, true,
            "iperf3 tests"),
        HeatmapMetric.ThroughputUp => new(m, "Upload throughput (iperf3)", "Mbps", 0, 0, true, true,
            "iperf3 tests"),
        HeatmapMetric.ChannelPower => new(m, "Channel power (spectrum)", "dBm", -95, -45, false, false,
            "spectrum captures and a selected channel"),
        HeatmapMetric.ChannelOccupancy => new(m, "Channel occupancy (spectrum)", "%", 0, 100, false, false,
            "spectrum captures and a selected channel"),
        HeatmapMetric.UnexplainedEnergy => new(m, "Unexplained energy (not from any network)", "dB", 0, 20, false, false,
            "a Wi-Fi scan and spectrum captures at the same points"),
        HeatmapMetric.EstimatedThroughput => new(m, "Estimated usable throughput", "Mbps", 0, 0, true, true,
            "a Wi-Fi scan and spectrum captures at the same points"),
        HeatmapMetric.RetryRate => new(m, "Retry rate (captured frames)", "%", 0, 30, false, false,
            "a monitor-mode capture at each point"),
        HeatmapMetric.MeasuredAirtime => new(m, "Measured airtime (captured frames)", "%", 0, 100, false, false,
            "a monitor-mode capture at each point"),
        HeatmapMetric.ClientCount => new(m, "Clients heard on the channel", "clients", 0, 25, false, false,
            "a monitor-mode capture at each point"),
        HeatmapMetric.Diagnosis => new(m, "What's limiting this spot", "", 0, 7, true, false,
            "a Wi-Fi scan, and spectrum or throughput data for the finer verdicts", true),
        _ => throw new ArgumentOutOfRangeException(nameof(m))
    };
}

/// <summary>Ssid/Bssid null = any; Band "All", "2.4" or "5"; Channel optional.</summary>
public sealed record MetricFilter(string? Ssid, string? Bssid, string Band, int? Channel);

public static class MetricExtractor
{
    const int NoCoverageDbm = -100;

    public static double? Value(SurveyPoint p, HeatmapMetric m, MetricFilter f, AnalysisContext? ctx = null)
    {
        switch (m)
        {
            case HeatmapMetric.SignalStrength:
            {
                var best = Best(p, f);
                if (best != null) return best.Rssi;
                return p.Networks.Count > 0 ? NoCoverageDbm : null;
            }
            case HeatmapMetric.SignalToNoise:
            {
                var best = Best(p, f);
                string band = best != null ? WifiChannels.BandLabel(best.FrequencyMHz) : f.Band;
                var noise = NoiseFor(p, band);
                if (noise == null) return null;
                return Math.Max(0, (best?.Rssi ?? NoCoverageDbm) - noise.Value);
            }
            case HeatmapMetric.NoiseFloor:
                return NoiseFor(p, f.Band == "5" ? "5" : "2.4");
            case HeatmapMetric.ApCount:
                return p.Networks
                    .Where(n => BandOk(n, f.Band) && (f.Channel == null || n.Channel == f.Channel) && n.Rssi >= -75)
                    .Select(n => n.Bssid).Distinct().Count();
            case HeatmapMetric.PhyRate:
                return p.Connection?.TxRateMbps;
            case HeatmapMetric.ThroughputDown:
                return p.ThroughputDownMbps;
            case HeatmapMetric.ThroughputUp:
                return p.ThroughputUpMbps;
            case HeatmapMetric.ChannelPower:
            {
                if (f.Channel is not int ch || WifiChannels.Find(ch) is not { } info) return null;
                var seg = SpectrumAnalysis.FindSegment(p.Spectrum, info.CenterMHz);
                return seg == null ? null
                    : SpectrumAnalysis.ChannelPower(seg.Avg, seg.StartMHz, seg.StepMHz, seg.RbwKHz, info.CenterMHz);
            }
            case HeatmapMetric.ChannelOccupancy:
            {
                if (f.Channel is not int ch || p.Spectrum == null) return null;
                // A dwell capture on this channel measures airtime far better than a wide sweep.
                var info = WifiChannels.Find(ch);
                if (info != null && RfAnalysis.Occupancy(p, info) is double dwell) return dwell * 100;
                foreach (var seg in p.Spectrum.Segments)
                    if (seg.ChannelOccupancy.TryGetValue(ch, out var occ)) return occ * 100;
                return null;
            }
            case HeatmapMetric.UnexplainedEnergy:
            {
                if (ctx?.OffsetFor(p) is not double offset) return null;
                if (f.Channel is int only)
                {
                    var info = WifiChannels.Find(only);
                    return info == null ? null : RfAnalysis.Residual(p, info, offset);
                }
                return RfAnalysis.WorstUnexplained(p, f.Band, offset)?.Db;
            }
            case HeatmapMetric.RetryRate:
                return p.Capture is { RetryEligible: > 20 } c1 ? c1.RetryRate * 100 : null;
            case HeatmapMetric.MeasuredAirtime:
                return p.Capture is { Frames: > 0 } c2 ? c2.AirtimeFraction * 100 : null;
            case HeatmapMetric.ClientCount:
                return p.Capture is { Frames: > 0 } c3 ? c3.Clients : null;
            case HeatmapMetric.EstimatedThroughput:
                return ctx == null ? null : LinkEstimator.Estimate(p, f, ctx);
            case HeatmapMetric.Diagnosis:
                return ctx == null ? null : (double)(int)Diagnoser.Diagnose(p, f, ctx).Verdict;
            default:
                return null;
        }
    }

    public static WifiReading? Best(SurveyPoint p, MetricFilter f) =>
        p.Networks
            .Where(n => (f.Ssid == null || n.Ssid == f.Ssid)
                        && (f.Bssid == null || n.Bssid == f.Bssid)
                        && BandOk(n, f.Band))
            .MaxBy(n => n.Rssi);

    static bool BandOk(WifiReading n, string band) =>
        band == "All" || WifiChannels.BandLabel(n.FrequencyMHz) == band;

    static double? NoiseFor(SurveyPoint p, string band) => band switch
    {
        "5" => p.Spectrum?.NoiseFloor5,
        _ => p.Spectrum?.NoiseFloor24
    };
}
