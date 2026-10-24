using WifiSurveyor.Models;
using WifiSurveyor.RF;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

public sealed record NoiseSource(
    double CenterMHz,
    double WidthMHz,
    EmitterKind Kind,
    int PointsSeen,
    double PeakDbm,
    string Band,
    TransmitterEstimate? Estimate,
    string Description);

/// <summary>
/// Finds noise sources across the whole captured spectrum, rather than on one chosen channel:
/// unexplained emissions are grouped by frequency, then each group is located from how its
/// power falls off across the survey.
/// </summary>
public static class NoiseFinder
{
    public static List<NoiseSource> Find(SurveyProject project, AnalysisContext ctx, int max = 5)
    {
        var sightings = new List<(SurveyPoint Point, Emitter Emitter)>();
        foreach (var p in project.Points)
            foreach (var emitter in EmitterFinder.Find(p, ctx.OffsetFor(p)))
                sightings.Add((p, emitter));

        if (sightings.Count == 0) return new List<NoiseSource>();

        // Group sightings that sit at the same frequency
        var clusters = new List<List<(SurveyPoint Point, Emitter Emitter)>>();
        foreach (var sighting in sightings.OrderBy(s => s.Emitter.CenterMHz))
        {
            var cluster = clusters.FirstOrDefault(c =>
            {
                double center = c.Average(x => x.Emitter.CenterMHz);
                double width = Math.Max(12, c.Max(x => x.Emitter.WidthMHz));
                return Math.Abs(center - sighting.Emitter.CenterMHz) <= width;
            });
            if (cluster == null) clusters.Add(new List<(SurveyPoint, Emitter)> { sighting });
            else cluster.Add(sighting);
        }

        var sources = new List<NoiseSource>();
        foreach (var cluster in clusters.OrderByDescending(c => c.Max(x => x.Emitter.PeakDbm)).Take(max))
        {
            double center = cluster.Average(x => x.Emitter.CenterMHz);
            double width = cluster.Max(x => x.Emitter.WidthMHz);
            var kind = cluster.GroupBy(x => x.Emitter.Kind).OrderByDescending(g => g.Count()).First().Key;
            int pointsSeen = cluster.Select(x => x.Point.Id).Distinct().Count();
            double peak = cluster.Max(x => x.Emitter.PeakDbm);

            // Measure this frequency at every point, including where it's quiet: the quiet
            // points are what pin down the direction it falls off in.
            var samples = new List<Sample>();
            foreach (var p in project.Points)
            {
                if (ctx.OffsetFor(p) is not double offset) continue;
                if (RfAnalysis.UnexplainedAt(p, center, width, offset) is double dbm)
                    samples.Add(new Sample(p.X, p.Y, dbm));
            }

            var label = $"Noise near {center:0} MHz";
            var estimate = pointsSeen >= 3
                ? Localizer.Estimate(project, samples, label, "interference")
                : null;

            var description = cluster.OrderByDescending(x => x.Emitter.PeakDbm).First().Emitter.Description;
            sources.Add(new NoiseSource(center, width, kind, pointsSeen, peak,
                WifiChannels.BandLabel(center), estimate, description));
        }
        return sources;
    }

    public static string Summarize(IReadOnlyList<NoiseSource> sources, SurveyProject project)
    {
        if (sources.Count == 0)
            return project.Points.Any(p => p.Spectrum != null)
                ? "No unexplained emissions found. Everything on the air is accounted for by the networks you can see."
                : "No spectrum captures yet, so there's nothing to search. Turn on spectrum recording and measure a few points.";

        var lines = new List<string>();
        foreach (var s in sources)
        {
            var where = s.Estimate == null
                ? "seen at too few points to place"
                : s.Estimate.LowConfidence
                    ? "rough position marked"
                    : "position marked";
            lines.Add($"{EmitterFinder.Label(s.Kind)} near {s.CenterMHz:0} MHz " +
                      $"({s.WidthMHz:0} MHz wide, peak {s.PeakDbm:0} dBm, {s.PointsSeen} point(s), {where}).");
        }
        return string.Join("\n", lines);
    }
}
