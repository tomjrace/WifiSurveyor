using WifiSurveyor.Models;
using WifiSurveyor.RF;
using WifiSurveyor.Wifi;
using Channel = WifiSurveyor.Wifi.WifiChannels.WifiChannel;

namespace WifiSurveyor.Surveying;

public sealed record QualityTargets
{
    public double MinRssi { get; init; } = -70;
    public double MinSnr { get; init; } = 20;
    public double MaxOccupancy { get; init; } = 0.45;
    public double MinPhyRateMbps { get; init; } = 100;

    /// <summary>Measured throughput below this fraction of the estimate points away from the air.</summary>
    public double MinThroughputRatio { get; init; } = 0.4;

    /// <summary>Retry rate above this points at collisions rather than weak signal.</summary>
    public double MaxRetryRate { get; init; } = 0.20;
}

/// <summary>
/// Per-project analysis state. The offset is the key piece: the Wi-Fi card and the
/// analyzer use different antennas and receivers, so their dBm values can't be compared
/// until they're referenced to each other.
/// </summary>
public sealed class AnalysisContext
{
    readonly Dictionary<Guid, double?> _offsets = new();

    public AnalysisContext(SurveyProject project, QualityTargets? targets = null)
    {
        Project = project;
        Targets = targets ?? new QualityTargets();
        var values = project.Points.Select(RfAnalysis.PointOffset).Where(v => v.HasValue).Select(v => v!.Value).ToList();
        ProjectOffset = values.Count > 0 ? RfAnalysis.Median(values) : null;
    }

    public SurveyProject Project { get; }
    public QualityTargets Targets { get; }

    /// <summary>Median offset across the whole survey; used where a point can't calibrate itself.</summary>
    public double? ProjectOffset { get; }

    public double? OffsetFor(SurveyPoint p)
    {
        if (!_offsets.TryGetValue(p.Id, out var v)) _offsets[p.Id] = v = RfAnalysis.PointOffset(p);
        return v ?? ProjectOffset;
    }
}

public static class RfAnalysis
{
    /// <summary>Residual above this is energy the visible networks don't account for.</summary>
    public const double UnexplainedDb = 6;

    public static double Median(IList<double> values)
    {
        var sorted = values.OrderBy(v => v).ToArray();
        int n = sorted.Length;
        if (n == 0) return double.NaN;
        return n % 2 == 1 ? sorted[n / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
    }

    /// <summary>Fraction of a 20 MHz channel mask covered by a transmitter centred elsewhere.</summary>
    public static double Overlap(double freqMHz, double centerMHz, double widthMHz = 20) =>
        Math.Max(0, widthMHz - Math.Abs(freqMHz - centerMHz)) / widthMHz;

    /// <summary>Combined power of every scanned network that lands on this channel.</summary>
    public static double? WifiPowerDbm(SurveyPoint p, Channel ch)
    {
        double mw = 0;
        foreach (var n in p.Networks)
        {
            if (WifiChannels.BandLabel(n.FrequencyMHz) != ch.Band) continue;
            double o = Overlap(n.FrequencyMHz, ch.CenterMHz);
            if (o > 0) mw += SpectrumAnalysis.ToMw(n.Rssi) * o;
        }
        return mw <= 0 ? null : SpectrumAnalysis.ToDbm(mw);
    }

    /// <summary>The segment to use for a channel: the dwell capture if it covers it, else a sweep.</summary>
    public static SpectrumSegment? SegmentFor(SurveyPoint p, Channel ch)
    {
        var cap = p.Spectrum;
        if (cap == null) return null;
        if (cap.Dwell is { } dwell && Math.Abs((cap.DwellCenterMHz ?? double.MinValue) - ch.CenterMHz) < 1) return dwell;
        return SpectrumAnalysis.FindSegment(cap, ch.CenterMHz);
    }

    /// <summary>
    /// Channel power from the max-hold trace. Max hold is the right comparison for scan RSSI,
    /// which is measured from beacons rather than averaged over idle time.
    /// </summary>
    public static double? MeasuredPeakDbm(SurveyPoint p, Channel ch)
    {
        var seg = SegmentFor(p, ch);
        return seg == null || seg.Max.Length == 0
            ? null
            : SpectrumAnalysis.ChannelPower(seg.Max, seg.StartMHz, seg.StepMHz, seg.RbwKHz, ch.CenterMHz);
    }

    public static double? NoiseFloor(SurveyPoint p, string band) => band switch
    {
        "2.4" => p.Spectrum?.NoiseFloor24,
        "5" => p.Spectrum?.NoiseFloor5,
        _ => null
    };

    public static double? Occupancy(SurveyPoint p, Channel ch)
    {
        var seg = SegmentFor(p, ch);
        return seg != null && seg.ChannelOccupancy.TryGetValue(ch.Number, out var o) ? o : null;
    }

    /// <summary>
    /// dB to add to summed scan RSSI so it can be compared with analyzer readings at this point.
    /// Taken from channels where a strong, visible network dominates.
    /// </summary>
    public static double? PointOffset(SurveyPoint p)
    {
        var diffs = new List<double>();
        foreach (var ch in WifiChannels.All)
        {
            if (WifiPowerDbm(p, ch) is not double wifi || wifi < -70) continue;
            if (MeasuredPeakDbm(p, ch) is not double meas) continue;
            diffs.Add(meas - wifi);
        }
        return diffs.Count >= 2 ? Median(diffs) : null;
    }

    /// <summary>
    /// Energy measured on a channel beyond what the visible networks and the noise floor explain.
    /// Around 0 dB means accounted for; well above means something is transmitting that the
    /// Wi-Fi card can't attribute.
    /// </summary>
    public static double? Residual(SurveyPoint p, Channel ch, double offset)
    {
        if (MeasuredPeakDbm(p, ch) is not double measured) return null;
        double floor = NoiseFloor(p, ch.Band) ?? -95;
        double expected = WifiPowerDbm(p, ch) is double wifi ? Math.Max(wifi + offset, floor) : floor;
        return measured - expected;
    }

    /// <summary>
    /// Unexplained power at any frequency span, in dBm. Quiet points return a floor-level
    /// value rather than nothing, because they still tell a fit where a source isn't.
    /// </summary>
    public static double? UnexplainedAt(SurveyPoint p, double centerMHz, double widthMHz, double offset)
    {
        double span = Math.Max(2, widthMHz);
        var seg = p.Spectrum?.Segments.FirstOrDefault(s =>
            centerMHz - span / 2 >= s.StartMHz - 0.5 && centerMHz + span / 2 <= s.EndMHz + 0.5);
        if (seg == null || seg.Max.Length == 0) return null;
        if (SpectrumAnalysis.ChannelPower(seg.Max, seg.StartMHz, seg.StepMHz, seg.RbwKHz, centerMHz, span)
            is not double measured) return null;

        double wifiMw = 0;
        foreach (var n in p.Networks)
        {
            double o = Overlap(n.FrequencyMHz, centerMHz, Math.Max(20, span));
            if (o > 0) wifiMw += SpectrumAnalysis.ToMw(n.Rssi + offset) * o;
        }

        double floorDbm = NoiseFloor(p, WifiChannels.BandLabel(centerMHz)) ?? -95;
        double floorMw = SpectrumAnalysis.ToMw(floorDbm) * (span / 20);
        double unexplainedMw = SpectrumAnalysis.ToMw(measured) - Math.Max(wifiMw, floorMw);
        return SpectrumAnalysis.ToDbm(Math.Max(unexplainedMw, floorMw * 0.25));
    }

    public static (Channel Channel, double Db)? WorstUnexplained(SurveyPoint p, string band, double offset)
    {
        (Channel Channel, double Db)? worst = null;
        foreach (var ch in WifiChannels.All.Where(c => band == "All" || c.Band == band))
        {
            if (Residual(p, ch, offset) is not double db) continue;
            if (worst == null || db > worst.Value.Db) worst = (ch, db);
        }
        return worst;
    }

    public static Channel? ChannelOf(WifiReading r) =>
        WifiChannels.All.FirstOrDefault(c => Math.Abs(c.CenterMHz - r.FrequencyMHz) < 1);
}

// ================= Emitter fingerprints =================

public enum EmitterKind { BurstyPackets, ContinuousEmitter, MicrowaveOven, WidebandNoise }

public sealed record Emitter(EmitterKind Kind, double CenterMHz, double WidthMHz, double PeakDbm,
    double PeakToAvgDb, string Description);

public static class EmitterFinder
{
    /// <summary>
    /// Looks for runs of raised energy in the max-hold trace that the scanned networks don't
    /// explain, and guesses at their character from shape and steadiness.
    /// </summary>
    public static IReadOnlyList<Emitter> Find(SurveyPoint p, double? offset)
    {
        var found = new List<Emitter>();
        if (p.Spectrum == null) return found;

        foreach (var seg in p.Spectrum.Segments)
        {
            if (seg.Max.Length < 4 || seg.StepMHz <= 0) continue;
            double floor = SpectrumAnalysis.Percentile(seg.Min, 0.10);
            double enter = floor + 12, stay = floor + 6;

            int i = 0;
            while (i < seg.Max.Length)
            {
                if (seg.Max[i] < enter) { i++; continue; }
                int start = i, peakIndex = i;
                double peak = double.MinValue;
                while (i < seg.Max.Length && seg.Max[i] >= stay)
                {
                    if (seg.Max[i] > peak) { peak = seg.Max[i]; peakIndex = i; }
                    i++;
                }
                double startF = seg.StartMHz + start * seg.StepMHz;
                double endF = seg.StartMHz + (i - 1) * seg.StepMHz;
                double width = Math.Max(seg.StepMHz, endF - startF);
                double center = (startF + endF) / 2;
                double avgAtPeak = peakIndex < seg.Avg.Length ? seg.Avg[peakIndex] : peak;
                double peakToAvg = peak - avgAtPeak;

                if (width >= 1 && !ExplainedByWifi(p, center, peak, offset))
                    found.Add(Classify(center, width, peak, peakToAvg, seg.Band));
            }
        }
        return found.OrderByDescending(e => e.PeakDbm).Take(5).ToList();
    }

    static bool ExplainedByWifi(SurveyPoint p, double centerMHz, double peakDbm, double? offset)
    {
        foreach (var n in p.Networks)
        {
            if (RfAnalysis.Overlap(n.FrequencyMHz, centerMHz, 40) <= 0) continue;
            if (offset is not double k) { if (n.Rssi > -80) return true; continue; }
            if (Math.Abs(peakDbm - (n.Rssi + k)) <= 10) return true;
        }
        return false;
    }

    static Emitter Classify(double center, double width, double peak, double peakToAvg, string band)
    {
        if (band == "2.4" && center is > 2430 and < 2485 && width >= 8 && peakToAvg >= 10)
            return new(EmitterKind.MicrowaveOven, center, width, peak, peakToAvg,
                "Looks like a microwave oven: a wide hump in the top half of 2.4 GHz that comes and goes. " +
                "Brutal while it runs, but only while it runs.");
        if (width >= 40)
            return new(EmitterKind.WidebandNoise, center, width, peak, peakToAvg,
                "Wideband energy across more than 40 MHz. Could be a jammer, a faulty device, or a very wide " +
                "channel from a network too weak to decode.");
        if (peakToAvg <= 5)
            return new(EmitterKind.ContinuousEmitter, center, width, peak, peakToAvg,
                "Constant carrier that never stops, such as an analog camera, video sender, or cordless device. " +
                "This is the worst kind of interference because it never yields airtime.");
        return new(EmitterKind.BurstyPackets, center, width, peak, peakToAvg,
            "Bursty, packet-like transmissions. Probably a network too weak to decode, or a device like " +
            "Bluetooth or Zigbee.");
    }

    public static string Label(EmitterKind kind) => kind switch
    {
        EmitterKind.MicrowaveOven => "Microwave oven",
        EmitterKind.ContinuousEmitter => "Continuous emitter",
        EmitterKind.WidebandNoise => "Wideband energy",
        _ => "Bursty transmitter"
    };
}

// ================= Link estimates =================

public static class LinkEstimator
{
    // 802.11ax, 20 MHz, one spatial stream, 800 ns guard interval
    static readonly double[] Rates20 = { 8.6, 17.2, 25.8, 34.4, 51.6, 68.8, 77.4, 86.0, 103.2, 114.7, 129.0, 143.4 };
    static readonly double[] SnrNeeded = { 2, 5, 9, 11, 15, 18, 20, 25, 29, 31, 33, 35 };

    /// <summary>Rough PHY rate a link should reach at this SNR. Not a measurement.</summary>
    public static double? PhyRateMbps(double snrDb, int widthMHz, int streams)
    {
        int mcs = -1;
        for (int i = 0; i < SnrNeeded.Length; i++)
            if (snrDb >= SnrNeeded[i]) mcs = i;
        if (mcs < 0) return null;

        double widthFactor = widthMHz switch { 160 => 8.67, 80 => 4.33, 40 => 2.07, _ => 1 };
        return Rates20[mcs] * widthFactor * Math.Max(1, streams);
    }

    /// <summary>PHY rate discounted for busy airtime and protocol overhead.</summary>
    public static double UsableMbps(double phyRate, double occupancy) =>
        phyRate * Math.Max(0.05, 1 - occupancy) * 0.55;

    /// <summary>Estimated usable throughput at a point for the filtered network.</summary>
    public static double? Estimate(SurveyPoint p, MetricFilter filter, AnalysisContext ctx)
    {
        var best = MetricExtractor.Best(p, filter);
        if (best == null) return null;
        string band = WifiChannels.BandLabel(best.FrequencyMHz);
        if (RfAnalysis.NoiseFloor(p, band) is not double noise) return null;

        int width = band == "2.4" ? 20 : 80;   // assumed; Windows doesn't report the real width
        if (PhyRateMbps(best.Rssi - noise, width, streams: 2) is not double phy) return 0;

        var ch = RfAnalysis.ChannelOf(best);
        double occupancy = ch != null ? RfAnalysis.Occupancy(p, ch) ?? 0 : 0;
        return UsableMbps(phy, occupancy);
    }
}

// ================= Diagnosis =================

public enum Verdict
{
    NoData = 0,
    Good = 1,
    Coverage = 2,
    NonWifiInterference = 3,
    CoChannelInterference = 4,
    Congestion = 5,
    ClientOrApConfig = 6,
    Backhaul = 7,
    Collisions = 8
}

public sealed record Diagnosis(Verdict Verdict, string Summary, string Advice);

public static class Verdicts
{
    public static readonly (Verdict Verdict, string Label, byte R, byte G, byte B)[] All =
    {
        (Verdict.Good, "Fine here", 21, 128, 61),
        (Verdict.Coverage, "Weak coverage", 220, 38, 38),
        (Verdict.NonWifiInterference, "Non-Wi-Fi interference", 147, 51, 234),
        (Verdict.CoChannelInterference, "Neighbouring networks", 234, 88, 12),
        (Verdict.Congestion, "Airtime congestion", 202, 138, 4),
        (Verdict.ClientOrApConfig, "Link configuration", 37, 99, 235),
        (Verdict.Collisions, "Retries and collisions", 13, 148, 136),
        (Verdict.Backhaul, "Beyond the Wi-Fi", 100, 116, 139),
        (Verdict.NoData, "Not enough data", 148, 163, 184)
    };

    public static (byte R, byte G, byte B) Color(Verdict v)
    {
        foreach (var e in All)
            if (e.Verdict == v) return (e.R, e.G, e.B);
        return (148, 163, 184);
    }

    public static string Label(Verdict v)
    {
        foreach (var e in All)
            if (e.Verdict == v) return e.Label;
        return v.ToString();
    }
}

public static class Diagnoser
{
    /// <summary>
    /// Walks the checks in order and stops at the first that fails, so the answer names a
    /// cause and an action instead of a number.
    /// </summary>
    public static Diagnosis Diagnose(SurveyPoint p, MetricFilter filter, AnalysisContext ctx)
    {
        var t = ctx.Targets;
        if (p.Networks.Count == 0)
            return new(Verdict.NoData, "No Wi-Fi scan here.", "Measure this point again.");

        var best = MetricExtractor.Best(p, filter);
        if (best == null)
            return new(Verdict.Coverage, "The selected network wasn't seen at all here.",
                "This is outside coverage. Move an access point closer or add one.");

        if (best.Rssi < t.MinRssi)
            return new(Verdict.Coverage, $"Signal is {best.Rssi} dBm, below the {t.MinRssi:0} dBm target.",
                "Coverage problem: move or add an access point, or raise the power a little.");

        string band = WifiChannels.BandLabel(best.FrequencyMHz);
        var ch = RfAnalysis.ChannelOf(best);
        double? noise = RfAnalysis.NoiseFloor(p, band);
        double? offset = ctx.OffsetFor(p);
        double? occupancy = ch != null ? RfAnalysis.Occupancy(p, ch) : null;
        double? residual = ch != null && offset is double k ? RfAnalysis.Residual(p, ch, k) : null;

        if (noise is double n)
        {
            double snr = best.Rssi - n;
            if (snr < t.MinSnr)
            {
                if (residual is double r && r >= RfAnalysis.UnexplainedDb)
                    return new(Verdict.NonWifiInterference,
                        $"SNR is {snr:0} dB and {r:0} dB of the energy on channel {ch!.Number} isn't from any network here.",
                        "Something non-Wi-Fi is transmitting. Use the unexplained energy map to walk toward it, " +
                        "or move this network to a cleaner channel.");
                return new(Verdict.CoChannelInterference,
                    $"SNR is {snr:0} dB and the energy is explained by other networks.",
                    "Neighbouring networks are the problem. Change channel, or reduce overlap between your own " +
                    "access points.");
            }
        }

        // A capture measures retries directly; high retries with a good signal means frames are
        // colliding rather than failing for want of signal.
        if (p.Capture is { RetryEligible: > 50 } capture && capture.RetryRate >= t.MaxRetryRate)
            return new(Verdict.Collisions,
                $"{capture.RetryRate * 100:0}% of frames on channel {capture.Channel} are retries, " +
                $"with {capture.Clients} clients heard.",
                "Frames are colliding despite a usable signal. Suspect a hidden node, an overloaded cell, " +
                "or too much overlap between access points on this channel.");

        // Measured airtime from a capture beats the analyzer's sampled duty cycle; the AP's own
        // BSS Load figure is a third opinion.
        double? airtime = p.Capture is { Frames: > 0 } cap ? cap.AirtimeFraction : occupancy;
        if (airtime is double busy && busy >= t.MaxOccupancy)
        {
            var reported = p.Capture?.Bss
                .Where(b => b.ReportedUtilization != null)
                .Select(b => b.ReportedUtilization!.Value)
                .DefaultIfEmpty(double.NaN).Max();
            var extra = reported is double rv && !double.IsNaN(rv)
                ? $" The access point reports {rv * 100:0}% utilisation itself."
                : "";
            return new(Verdict.Congestion,
                $"The channel is busy {busy * 100:0}% of the time.{extra}",
                "Signal is fine but the airtime is spoken for. Change channel, add capacity, or move clients " +
                "to 5 or 6 GHz.");
        }

        bool connectedHere = p.Connection != null &&
                             (filter.Ssid == null || p.Connection.Ssid == filter.Ssid);
        if (connectedHere && p.Connection!.TxRateMbps > 0 && p.Connection.TxRateMbps < t.MinPhyRateMbps)
            return new(Verdict.ClientOrApConfig,
                $"The link is only negotiating {p.Connection.TxRateMbps:0} Mbps despite a usable signal.",
                "Look at the configuration rather than the RF: channel width, legacy rate support, band " +
                "steering, or the client adapter itself.");

        if (p.ThroughputDownMbps is double measured &&
            LinkEstimator.Estimate(p, filter, ctx) is double estimate && estimate > 0 &&
            measured < estimate * t.MinThroughputRatio)
            return new(Verdict.Backhaul,
                $"Throughput is {measured:0} Mbps where the air should support roughly {estimate:0} Mbps.",
                "The bottleneck probably isn't Wi-Fi. Check the uplink, the wired path, or the test server.");

        return new(Verdict.Good, "Signal, noise, and airtime all pass here.", "Nothing to fix at this point.");
    }
}

// ================= Channel planning and scores =================

public sealed record ChannelScore(int Channel, string Band, double Score, double? WifiDbm, double? Occupancy,
    double? WorstResidual, int ApCount, string Comment);

public static class ChannelPlanner
{
    /// <summary>
    /// Ranks channels across every measured point, lowest score first. Because the points are
    /// positions, a channel that's clean in one room and busy in another is scored honestly.
    /// </summary>
    public static List<ChannelScore> Rank(SurveyProject project, string band, AnalysisContext ctx)
    {
        var channels = band == "2.4"
            ? WifiChannels.Band24.Where(c => c.Number is 1 or 6 or 11).ToList()
            : WifiChannels.Band5.ToList();

        var scores = new List<ChannelScore>();
        foreach (var ch in channels)
        {
            var wifi = new List<double>();
            var occupancy = new List<double>();
            double? worstResidual = null;
            var bssids = new HashSet<string>();

            foreach (var p in project.Points)
            {
                if (RfAnalysis.WifiPowerDbm(p, ch) is double w) wifi.Add(w);
                if (RfAnalysis.Occupancy(p, ch) is double o) occupancy.Add(o);
                if (ctx.OffsetFor(p) is double k && RfAnalysis.Residual(p, ch, k) is double r)
                    if (worstResidual == null || r > worstResidual) worstResidual = r;
                foreach (var n in p.Networks)
                    if (RfAnalysis.Overlap(n.FrequencyMHz, ch.CenterMHz) > 0 && n.Rssi >= -80)
                        bssids.Add(n.Bssid);
            }

            double? medianWifi = wifi.Count > 0 ? RfAnalysis.Median(wifi) : null;
            double? meanOcc = occupancy.Count > 0 ? occupancy.Average() : null;

            double score =
                0.50 * Norm(medianWifi ?? -95, -95, -50) +
                0.25 * (meanOcc ?? 0) +
                0.25 * Norm(worstResidual ?? 0, 0, 20);

            var comment = Comment(medianWifi, meanOcc, worstResidual, bssids.Count);
            scores.Add(new ChannelScore(ch.Number, ch.Band, score, medianWifi, meanOcc, worstResidual,
                bssids.Count, comment));
        }
        return scores.OrderBy(s => s.Score).ToList();
    }

    static string Comment(double? wifi, double? occ, double? residual, int aps)
    {
        if (residual is double r && r >= RfAnalysis.UnexplainedDb)
            return $"{r:0} dB of unexplained energy somewhere in the survey.";
        if (occ is double o && o >= 0.45) return $"Busy {o * 100:0}% of the time.";
        if (wifi is double w && w >= -60) return $"Crowded: {aps} access points, median {w:0} dBm.";
        if (aps == 0) return "Nothing else heard on this channel.";
        return $"{aps} access points, median {(wifi is double v ? $"{v:0} dBm" : "unknown")}.";
    }

    static double Norm(double v, double min, double max) => Math.Clamp((v - min) / (max - min), 0, 1);
}

public sealed record SurveyScore(int Points, int PointsWithSpectrum, double CoveragePct, double QualityPct,
    double AirtimePct, Dictionary<Verdict, int> Verdicts)
{
    public double Overall => new[] { CoveragePct, QualityPct, AirtimePct }.Min();

    public string Grade => Overall switch
    {
        >= 95 => "Excellent",
        >= 85 => "Good",
        >= 70 => "Patchy",
        _ => "Poor"
    };
}

public static class SurveyScorer
{
    public static SurveyScore Score(SurveyProject project, MetricFilter filter, AnalysisContext ctx)
    {
        var t = ctx.Targets;
        int coverage = 0, quality = 0, airtime = 0, qualityKnown = 0, airtimeKnown = 0, withSpectrum = 0;
        var verdicts = new Dictionary<Verdict, int>();

        foreach (var p in project.Points)
        {
            var best = MetricExtractor.Best(p, filter);
            if (best != null && best.Rssi >= t.MinRssi) coverage++;
            if (p.Spectrum != null) withSpectrum++;

            if (best != null)
            {
                string band = WifiChannels.BandLabel(best.FrequencyMHz);
                if (RfAnalysis.NoiseFloor(p, band) is double noise)
                {
                    qualityKnown++;
                    if (best.Rssi - noise >= t.MinSnr) quality++;
                }
                var ch = RfAnalysis.ChannelOf(best);
                if (ch != null && RfAnalysis.Occupancy(p, ch) is double occ)
                {
                    airtimeKnown++;
                    if (occ < t.MaxOccupancy) airtime++;
                }
            }

            var v = Diagnoser.Diagnose(p, filter, ctx).Verdict;
            verdicts[v] = verdicts.GetValueOrDefault(v) + 1;
        }

        int n = Math.Max(1, project.Points.Count);
        return new SurveyScore(
            project.Points.Count,
            withSpectrum,
            100.0 * coverage / n,
            qualityKnown > 0 ? 100.0 * quality / qualityKnown : 100,
            airtimeKnown > 0 ? 100.0 * airtime / airtimeKnown : 100,
            verdicts);
    }
}
