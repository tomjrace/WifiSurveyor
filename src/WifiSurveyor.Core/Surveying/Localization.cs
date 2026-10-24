using WifiSurveyor.Models;
using WifiSurveyor.RF;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

public readonly record struct Sample(double X, double Y, double Dbm);

public sealed record TransmitterEstimate(
    string Label,
    string Kind,          // "ap" or "interference"
    double X,
    double Y,
    double P0Dbm,         // signal at one metre implied by the fit
    double PathLossExponent,
    double RmsDb,
    int Points,
    double RadiusPx,
    bool LowConfidence,
    string Note);

public static class Materials
{
    /// <summary>Typical one-way attenuation at 5 GHz. Rough, but the ranking is what matters.</summary>
    public static readonly (string Name, double Db)[] All =
    {
        ("Drywall", 3),
        ("Wood door", 4),
        ("Glass window", 2),
        ("Brick", 8),
        ("Concrete", 12),
        ("Metal or lift shaft", 20),
        ("Bookcase or clutter", 5)
    };

    public static double DbFor(string name) =>
        All.FirstOrDefault(m => m.Name == name).Db is var db && db > 0 ? db : 3;
}

/// <summary>
/// Estimates where a transmitter is from the signal measured around it, using a log-distance
/// path-loss model: received = P0 − 10·n·log10(distance) − walls crossed.
/// The position, P0, and the exponent are all unknown, so it searches a grid of positions and
/// exponents, solving P0 in closed form at each, and keeps the fit with the smallest residual.
/// </summary>
public static class Localizer
{
    const int Grid = 64;
    const int RefineSteps = 12;

    public static TransmitterEstimate? Estimate(SurveyProject project, IReadOnlyList<Sample> samples,
        string label, string kind)
    {
        if (samples.Count < 4) return null;

        double spreadX = samples.Max(s => s.X) - samples.Min(s => s.X);
        double spreadY = samples.Max(s => s.Y) - samples.Min(s => s.Y);
        if (Math.Sqrt(spreadX * spreadX + spreadY * spreadY) < 40)
            return null;   // measurements too clustered to triangulate anything

        double mpp = project.MetersPerPixel ?? 0.05;
        double w = project.FloorPlanWidth, h = project.FloorPlanHeight;
        double minX = -0.1 * w, maxX = 1.1 * w, minY = -0.1 * h, maxY = 1.1 * h;

        var best = Search(project, samples, mpp, minX, maxX, minY, maxY, Grid, out var rmsGrid, out double cellW,
            out double cellH);
        if (best == null) return null;

        // Refine in the neighbourhood of the coarse winner
        var refined = Search(project, samples, mpp,
            best.Value.X - cellW, best.Value.X + cellW,
            best.Value.Y - cellH, best.Value.Y + cellH,
            RefineSteps, out _, out _, out _) ?? best;

        // Confidence: how much of the plan fits nearly as well
        double threshold = best.Value.Rms + 3;
        int plausible = rmsGrid.Count(v => v <= threshold);
        double area = plausible * cellW * cellH;
        double radius = Math.Sqrt(area / Math.PI);

        bool onEdge = refined.Value.X <= minX + cellW || refined.Value.X >= maxX - cellW ||
                      refined.Value.Y <= minY + cellH || refined.Value.Y >= maxY - cellH;
        bool lowConfidence = refined.Value.Rms > 6 || onEdge ||
                             radius > 0.25 * Math.Sqrt(w * w + h * h);

        var note = $"From {samples.Count} points, path-loss exponent {refined.Value.N:0.0}, " +
                   $"residual {refined.Value.Rms:0.0} dB" +
                   (onEdge ? ". The best fit sits at the edge of the plan, so the source is probably outside it" :
                       lowConfidence ? ". Measurements are too one-sided for a tight fix; walk around the far side" : "");

        return new TransmitterEstimate(label, kind, refined.Value.X, refined.Value.Y, refined.Value.P0,
            refined.Value.N, refined.Value.Rms, samples.Count, radius, lowConfidence, note);
    }

    readonly record struct Fit(double X, double Y, double P0, double N, double Rms);

    static Fit? Search(SurveyProject project, IReadOnlyList<Sample> samples, double mpp,
        double minX, double maxX, double minY, double maxY, int steps,
        out double[] rmsGrid, out double cellW, out double cellH)
    {
        cellW = (maxX - minX) / steps;
        cellH = (maxY - minY) / steps;
        rmsGrid = new double[steps * steps];
        Array.Fill(rmsGrid, double.MaxValue);

        int n = samples.Count;
        // Distance and wall loss don't depend on the exponent, so compute them once.
        var logDistance = new double[steps * steps * n];
        var wallLoss = new double[steps * steps * n];
        for (int cell = 0; cell < steps * steps; cell++)
        {
            double cx = minX + (cell % steps + 0.5) * cellW;
            double cy = minY + (cell / steps + 0.5) * cellH;
            for (int i = 0; i < n; i++)
            {
                double dx = samples[i].X - cx, dy = samples[i].Y - cy;
                double metres = Math.Max(0.5, Math.Sqrt(dx * dx + dy * dy) * mpp);
                logDistance[cell * n + i] = 10 * Math.Log10(metres);
                wallLoss[cell * n + i] = BarrierLoss(project, samples[i].X, samples[i].Y, cx, cy);
            }
        }

        Fit? best = null;
        for (double exponent = 2.0; exponent <= 4.01; exponent += 0.25)
        {
            var current = new double[steps * steps];
            for (int cell = 0; cell < steps * steps; cell++)
            {
                // With the position and exponent fixed, the best P0 is just the mean offset.
                double sum = 0;
                for (int i = 0; i < n; i++)
                    sum += samples[i].Dbm + exponent * logDistance[cell * n + i] + wallLoss[cell * n + i];
                double p0 = sum / n;

                double error = 0;
                for (int i = 0; i < n; i++)
                {
                    double predicted = p0 - exponent * logDistance[cell * n + i] - wallLoss[cell * n + i];
                    double diff = samples[i].Dbm - predicted;
                    error += diff * diff;
                }
                double rms = Math.Sqrt(error / n);
                current[cell] = rms;

                if (best == null || rms < best.Value.Rms)
                    best = new Fit(minX + (cell % steps + 0.5) * cellW,
                        minY + (cell / steps + 0.5) * cellH, p0, exponent, rms);
            }
            for (int cell = 0; cell < current.Length; cell++)
                if (current[cell] < rmsGrid[cell]) rmsGrid[cell] = current[cell];
        }
        return best;
    }

    /// <summary>Total attenuation of the walls a straight path crosses.</summary>
    public static double BarrierLoss(SurveyProject project, double x1, double y1, double x2, double y2)
    {
        double loss = 0;
        foreach (var b in project.Barriers)
            if (!b.Suggested && Intersects(x1, y1, x2, y2, b.X1, b.Y1, b.X2, b.Y2))
                loss += b.Db;
        return loss;
    }

    static bool Intersects(double ax, double ay, double bx, double by,
        double cx, double cy, double dx, double dy)
    {
        double d1 = Cross(cx, cy, dx, dy, ax, ay);
        double d2 = Cross(cx, cy, dx, dy, bx, by);
        double d3 = Cross(ax, ay, bx, by, cx, cy);
        double d4 = Cross(ax, ay, bx, by, dx, dy);
        return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) &&
               ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
    }

    static double Cross(double ax, double ay, double bx, double by, double px, double py) =>
        (bx - ax) * (py - ay) - (by - ay) * (px - ax);

    // ---------- sample sets ----------

    public static List<Sample> ApSamples(SurveyProject project, string bssid) =>
        project.Points
            .Select(p => (p, reading: p.Networks.FirstOrDefault(n =>
                string.Equals(n.Bssid, bssid, StringComparison.OrdinalIgnoreCase))))
            .Where(t => t.reading != null)
            .Select(t => new Sample(t.p.X, t.p.Y, t.reading!.Rssi))
            .ToList();

    /// <summary>
    /// Power at each point that the visible networks don't explain, in dBm. Subtracting the
    /// expected power in linear terms leaves just the unexplained emitter's contribution.
    /// </summary>
    public static List<Sample> InterferenceSamples(SurveyProject project, int channel, AnalysisContext ctx)
    {
        var info = WifiChannels.Find(channel);
        if (info == null) return new List<Sample>();

        var samples = new List<Sample>();
        int strong = 0;
        foreach (var p in project.Points)
        {
            if (ctx.OffsetFor(p) is not double offset) continue;
            if (RfAnalysis.MeasuredPeakDbm(p, info) is not double measured) continue;

            double floor = RfAnalysis.NoiseFloor(p, info.Band) ?? -95;
            double expected = RfAnalysis.WifiPowerDbm(p, info) is double wifi
                ? Math.Max(wifi + offset, floor)
                : floor;
            double unexplainedMw = SpectrumAnalysis.ToMw(measured) - SpectrumAnalysis.ToMw(expected);
            if (unexplainedMw <= 0) continue;

            double dbm = SpectrumAnalysis.ToDbm(unexplainedMw);
            if (measured - expected >= RfAnalysis.UnexplainedDb) strong++;
            samples.Add(new Sample(p.X, p.Y, dbm));
        }
        return strong >= 3 ? samples : new List<Sample>();
    }

    /// <summary>Access points worth estimating: those seen from enough spread-out points.</summary>
    public static List<(string Bssid, string Ssid, int Points)> Candidates(SurveyProject project,
        string? ssid, string? bssid, int limit = 6) =>
        project.Points
            .SelectMany(p => p.Networks)
            .Where(n => (bssid == null || string.Equals(n.Bssid, bssid, StringComparison.OrdinalIgnoreCase))
                        && (ssid == null || n.Ssid == ssid))
            .GroupBy(n => n.Bssid, StringComparer.OrdinalIgnoreCase)
            .Select(g => (Bssid: g.Key, Ssid: g.First().Ssid, Points: g.Count()))
            .Where(t => t.Points >= 4)
            .OrderByDescending(t => t.Points)
            .Take(limit)
            .ToList();
}
