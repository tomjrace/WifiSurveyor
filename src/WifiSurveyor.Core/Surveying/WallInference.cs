using WifiSurveyor.Models;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

/// <summary>
/// Works out where obstructions probably are from the measurements alone.
///
/// The idea: once an access point's position is estimated, the signal at every point can be
/// predicted from distance. Where two nearby points disagree sharply with that prediction, and
/// several access points agree about it, something solid is standing between them. That's
/// enough to place a barrier without knowing what it's made of.
/// </summary>
public static class WallInference
{
    /// <summary>Unexplained difference between neighbouring points that counts as an obstruction.</summary>
    public const double ExcessDb = 8;

    public static List<Barrier> Infer(SurveyProject project, AnalysisContext ctx, out string summary)
    {
        summary = "";
        if (project.Points.Count < 6)
        {
            summary = "Not enough measurements yet. Six or more, spread across the space, gives it something to work with.";
            return new List<Barrier>();
        }

        // Estimate the access points first, ignoring anything previously inferred so the
        // guesses don't feed on themselves.
        var previous = project.Barriers.Where(b => b.Inferred).ToList();
        foreach (var b in previous) project.Barriers.Remove(b);

        var transmitters = new List<(TransmitterEstimate Estimate, Dictionary<Guid, double> Rssi)>();
        foreach (var (bssid, _, _) in Localizer.Candidates(project, null, null, limit: 10))
        {
            var samples = Localizer.ApSamples(project, bssid);
            if (Localizer.Estimate(project, samples, bssid, "ap") is not { } estimate) continue;
            if (estimate.RmsDb > 9) continue;   // a fit this loose can't say anything about walls

            var byPoint = new Dictionary<Guid, double>();
            foreach (var p in project.Points)
            {
                var reading = p.Networks.FirstOrDefault(n =>
                    string.Equals(n.Bssid, bssid, StringComparison.OrdinalIgnoreCase));
                if (reading != null) byPoint[p.Id] = reading.Rssi;
            }
            transmitters.Add((estimate, byPoint));
        }

        if (transmitters.Count == 0)
        {
            summary = "No access point could be placed confidently enough to reason about walls. " +
                      "Measure from more directions around them.";
            return new List<Barrier>();
        }

        double mpp = project.MetersPerPixel ?? 0.05;
        double diagonal = Math.Sqrt(project.FloorPlanWidth * (double)project.FloorPlanWidth +
                                    project.FloorPlanHeight * (double)project.FloorPlanHeight);
        double maxPairPx = project.MetersPerPixel is double m ? 6 / m : diagonal * 0.12;
        int required = transmitters.Count >= 3 ? 2 : 1;

        var segments = new List<(double X1, double Y1, double X2, double Y2, double Db)>();
        var points = project.Points;

        for (int i = 0; i < points.Count; i++)
        for (int j = i + 1; j < points.Count; j++)
        {
            var a = points[i];
            var b = points[j];
            double dx = b.X - a.X, dy = b.Y - a.Y;
            double separation = Math.Sqrt(dx * dx + dy * dy);
            if (separation < 8 || separation > maxPairPx) continue;

            var excesses = new List<double>();
            foreach (var (estimate, rssi) in transmitters)
            {
                if (!rssi.TryGetValue(a.Id, out var rssiA) || !rssi.TryGetValue(b.Id, out var rssiB)) continue;

                double excessA = Predict(estimate, project, a.X, a.Y, mpp) - rssiA;
                double excessB = Predict(estimate, project, b.X, b.Y, mpp) - rssiB;
                double differential = excessB - excessA;   // extra loss one point suffers and the other doesn't
                if (Math.Abs(differential) >= ExcessDb) excesses.Add(Math.Abs(differential));
            }

            if (excesses.Count < required) continue;

            // Something blocks one point but not the other, so it crosses the line between them.
            double midX = (a.X + b.X) / 2, midY = (a.Y + b.Y) / 2;
            double nx = -dy / separation, ny = dx / separation;
            double half = separation * 0.5;
            segments.Add((midX - nx * half, midY - ny * half, midX + nx * half, midY + ny * half,
                Math.Clamp(RfAnalysis.Median(excesses), 3, 20)));
        }

        var merged = MergeSegments(segments);
        var barriers = merged.Select(s => new Barrier
        {
            X1 = s.X1, Y1 = s.Y1, X2 = s.X2, Y2 = s.Y2,
            Material = "Obstruction (inferred)",
            Db = Math.Round(s.Db),
            Inferred = true
        }).Take(80).ToList();

        summary = barriers.Count == 0
            ? $"Signal behaves smoothly between neighbouring points, so nothing obvious is blocking it. " +
              $"({transmitters.Count} access point(s) used.)"
            : $"Placed {barriers.Count} likely obstruction(s) from {transmitters.Count} access point(s). " +
              "These are where the signal drops more than distance explains; drag or delete any that look wrong.";
        return barriers;
    }

    static double Predict(TransmitterEstimate estimate, SurveyProject project, double x, double y, double mpp)
    {
        double dx = x - estimate.X, dy = y - estimate.Y;
        double metres = Math.Max(0.5, Math.Sqrt(dx * dx + dy * dy) * mpp);
        return estimate.P0Dbm - estimate.PathLossExponent * 10 * Math.Log10(metres)
               - Localizer.BarrierLoss(project, x, y, estimate.X, estimate.Y);
    }

    /// <summary>Collapses overlapping votes along the same line into single walls.</summary>
    static List<(double X1, double Y1, double X2, double Y2, double Db)> MergeSegments(
        List<(double X1, double Y1, double X2, double Y2, double Db)> segments)
    {
        var result = new List<(double X1, double Y1, double X2, double Y2, double Db)>();

        foreach (var s in segments.OrderByDescending(s => Length(s)))
        {
            bool absorbed = false;
            for (int i = 0; i < result.Count && !absorbed; i++)
            {
                var r = result[i];
                if (AngleGap(Angle(s), Angle(r)) > 20 * Math.PI / 180) continue;

                double gap = Math.Min(
                    WallDetector.PointToSegment(s.X1, s.Y1, r.X1, r.Y1, r.X2, r.Y2),
                    WallDetector.PointToSegment(s.X2, s.Y2, r.X1, r.Y1, r.X2, r.Y2));
                if (gap > Math.Max(12, Length(r) * 0.3)) continue;

                // Extend along the shared direction rather than averaging ends together
                double angle = (Angle(r) + Angle(s)) / 2;
                double ux = Math.Cos(angle), uy = Math.Sin(angle);
                double cx = (r.X1 + r.X2 + s.X1 + s.X2) / 4, cy = (r.Y1 + r.Y2 + s.Y1 + s.Y2) / 4;

                var projections = new[]
                {
                    (r.X1 - cx) * ux + (r.Y1 - cy) * uy,
                    (r.X2 - cx) * ux + (r.Y2 - cy) * uy,
                    (s.X1 - cx) * ux + (s.Y1 - cy) * uy,
                    (s.X2 - cx) * ux + (s.Y2 - cy) * uy
                };
                double lo = projections.Min(), hi = projections.Max();
                result[i] = (cx + ux * lo, cy + uy * lo, cx + ux * hi, cy + uy * hi, Math.Max(r.Db, s.Db));
                absorbed = true;
            }
            if (!absorbed) result.Add(s);
        }
        return result;
    }

    static double Length((double X1, double Y1, double X2, double Y2, double Db) s) =>
        Math.Sqrt((s.X2 - s.X1) * (s.X2 - s.X1) + (s.Y2 - s.Y1) * (s.Y2 - s.Y1));

    static double Angle((double X1, double Y1, double X2, double Y2, double Db) s)
    {
        double a = Math.Atan2(s.Y2 - s.Y1, s.X2 - s.X1);
        return a < 0 ? a + Math.PI : a;
    }

    static double AngleGap(double a, double b)
    {
        double d = Math.Abs(a - b) % Math.PI;
        return Math.Min(d, Math.PI - d);
    }
}
