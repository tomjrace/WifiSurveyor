using WifiSurveyor.Models;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.RF;

public static class SpectrumPlan
{
    /// <summary>Ranges captured at each survey point. Each stays under the 600 MHz span limit.</summary>
    public static readonly (double Start, double End)[] Segments =
    {
        (2400, 2500),
        (5150, 5500),
        (5470, 5900)
    };
}

public static class SpectrumAnalysis
{
    /// <summary>A channel counts as busy when its power is this far above the noise floor.</summary>
    public const double OccupancyThresholdDb = 10;

    public static double ToMw(double dbm) => Math.Pow(10, dbm / 10);
    public static double ToDbm(double mw) => mw <= 0 ? -150 : 10 * Math.Log10(mw);

    /// <summary>
    /// Integrates sweep points across a channel. Each point measures power in one RBW,
    /// so the sum is scaled by step/RBW to avoid double counting overlapping filters.
    /// </summary>
    public static double? ChannelPower(double[] trace, double startMHz, double stepMHz, double rbwKHz,
        double centerMHz, double widthMHz = 20)
    {
        if (stepMHz <= 0 || rbwKHz <= 0) return null;
        double lo = centerMHz - widthMHz / 2, hi = centerMHz + widthMHz / 2, mw = 0;
        int bins = 0;
        for (int i = 0; i < trace.Length; i++)
        {
            double f = startMHz + i * stepMHz;
            if (f < lo || f > hi) continue;
            mw += ToMw(trace[i]);
            bins++;
        }
        if (bins == 0) return null;
        return ToDbm(mw * stepMHz / (rbwKHz / 1000.0));
    }

    public static double Percentile(IEnumerable<double> values, double p)
    {
        var sorted = values.OrderBy(v => v).ToArray();
        if (sorted.Length == 0) return double.NaN;
        return sorted[Math.Clamp((int)(sorted.Length * p), 0, sorted.Length - 1)];
    }

    /// <summary>
    /// Noise floor from a min-hold trace, converted from the analyzer's RBW to a 20 MHz
    /// channel so it can be subtracted from Wi-Fi RSSI. Example: −105 dBm in 600 kHz ≈ −90 dBm per 20 MHz.
    /// </summary>
    public static double NoiseFloorPer20MHz(double[] minHold, double rbwKHz) =>
        Percentile(minHold, 0.10) + 10 * Math.Log10(20_000 / Math.Max(1, rbwKHz));

    public static double? BandNoiseFloor(SpectrumCapture cap, string band)
    {
        var floors = cap.Segments
            .Where(s => s.Band == band && s.Min.Length > 0)
            .Select(s => NoiseFloorPer20MHz(s.Min, s.RbwKHz))
            .ToList();
        return floors.Count == 0 ? null : floors.Min();
    }

    public static SpectrumSegment? FindSegment(SpectrumCapture? cap, double centerMHz) =>
        cap?.Segments.FirstOrDefault(s => Covers(s, centerMHz));

    /// <summary>True when a 20 MHz channel at this centre fits inside the segment.</summary>
    public static bool Covers(SpectrumSegment s, double centerMHz) =>
        centerMHz - 10 >= s.StartMHz - 0.5 && centerMHz + 10 <= s.EndMHz + 0.5;

    public static SpectrumSegment BuildSegment(RfExplorerConfig cfg, List<double[]> sweeps,
        double? noiseFloorOverride = null)
    {
        if (sweeps.Count == 0) throw new InvalidOperationException("No sweeps were captured.");

        int n = sweeps.Min(s => s.Length);
        var avgMw = new double[n];
        var max = Enumerable.Repeat(-200.0, n).ToArray();
        var min = Enumerable.Repeat(0.0, n).ToArray();
        for (int i = 0; i < n; i++) min[i] = double.MaxValue;

        foreach (var s in sweeps)
            for (int i = 0; i < n; i++)
            {
                avgMw[i] += ToMw(s[i]);
                if (s[i] > max[i]) max[i] = s[i];
                if (s[i] < min[i]) min[i] = s[i];
            }

        var seg = new SpectrumSegment
        {
            StartMHz = cfg.StartMHz,
            StepMHz = cfg.StepMHz,
            RbwKHz = cfg.RbwKHz > 0 ? cfg.RbwKHz : 600,
            SweepCount = sweeps.Count,
            Avg = avgMw.Select(v => ToDbm(v / sweeps.Count)).ToArray(),
            Max = max,
            Min = min
        };

        // A narrow dwell capture sees only one busy channel, so its own min-hold is a poor
        // noise reference; the caller passes the floor measured over the wider sweep instead.
        double floor = noiseFloorOverride ?? NoiseFloorPer20MHz(min, seg.RbwKHz);
        foreach (var ch in WifiChannels.All.Where(c => Covers(seg, c.CenterMHz)))
        {
            int busy = sweeps.Count(s =>
                (ChannelPower(s, seg.StartMHz, seg.StepMHz, seg.RbwKHz, ch.CenterMHz) ?? -200)
                > floor + OccupancyThresholdDb);
            seg.ChannelOccupancy[ch.Number] = (double)busy / sweeps.Count;
        }
        return seg;
    }
}
