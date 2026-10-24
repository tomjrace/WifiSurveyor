using System.Text;
using System.Windows;
using System.Windows.Media;
using Microsoft.Win32;
using WifiSurveyor.Models;
using WifiSurveyor.Surveying;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Views;

public partial class AnalysisWindow : Window
{
    readonly string _report;

    public AnalysisWindow(SurveyProject project, MetricFilter filter, AnalysisContext ctx,
        IReadOnlyList<TransmitterEstimate>? estimates = null, Positioning.GeoTransform? geo = null)
    {
        InitializeComponent();

        var score = SurveyScorer.Score(project, filter, ctx);
        var network = filter.Ssid ?? "the strongest network at each point";

        Headline.Text = $"{project.Name}: {score.Grade}";
        Subhead.Text = $"{score.Points} measurement points, {score.PointsWithSpectrum} with spectrum data. " +
                       $"Judged against {network}. " +
                       (ctx.ProjectOffset is double k
                           ? $"Analyzer and Wi-Fi card differ by about {k:0} dB, measured from strong networks."
                           : "Not enough overlapping data to compare analyzer and Wi-Fi card, so unexplained-energy figures are unavailable.");

        Scores.Text =
            $"Coverage: {score.CoveragePct:0}% of points at or above {ctx.Targets.MinRssi:0} dBm\n" +
            $"Quality: {score.QualityPct:0}% of points at or above {ctx.Targets.MinSnr:0} dB signal-to-noise\n" +
            $"Airtime: {score.AirtimePct:0}% of points with the channel busy less than {ctx.Targets.MaxOccupancy * 100:0}% of the time\n\n" +
            "These are reported separately on purpose: an area can have flawless coverage and still be unusable " +
            "because the airtime is spoken for.";

        VerdictList.ItemsSource = Verdicts.All
            .Where(v => score.Verdicts.GetValueOrDefault(v.Verdict) > 0)
            .Select(v => new
            {
                Swatch = (Brush)new SolidColorBrush(Color.FromRgb(v.R, v.G, v.B)),
                Text = $"{score.Verdicts[v.Verdict]} point(s): {v.Label}. {Explain(v.Verdict)}"
            })
            .ToList();

        var channels = ChannelPlanner.Rank(project, "2.4", ctx).Take(3)
            .Concat(ChannelPlanner.Rank(project, "5", ctx).Take(8))
            .Select(c => new
            {
                c.Band,
                c.Channel,
                c.Score,
                WifiText = c.WifiDbm is double w ? $"{w:0} dBm" : "none heard",
                OccupancyText = c.Occupancy is double o ? $"{o * 100:0}%" : "–",
                ResidualText = c.WorstResidual is double r ? $"{r:0} dB" : "–",
                c.Comment
            })
            .ToList();
        ChannelGrid.ItemsSource = channels;

        var best24 = ChannelPlanner.Rank(project, "2.4", ctx).FirstOrDefault();
        var best5 = ChannelPlanner.Rank(project, "5", ctx).FirstOrDefault();
        ChannelNote.Text =
            "Lower scores are cleaner, weighing other networks, airtime, and unexplained energy across every point " +
            "you measured, so a channel that's quiet in one room but busy in another is scored honestly. " +
            "Only 1, 6, and 11 are ranked at 2.4 GHz because the others overlap. " +
            (best24 != null && best5 != null
                ? $"On this survey the cleanest options are channel {best24.Channel} at 2.4 GHz and channel {best5.Channel} at 5 GHz."
                : "");

        Estimates.Text = DescribeEstimates(project, estimates, geo);
        Interference.Text = DescribeInterference(project, ctx);

        Caveats.Text =
            "The RF Explorer's amplitude accuracy is ±3 dB on the 6G module and ±6 dB on WSUB3G, and its antenna " +
            "differs from the laptop's, so treat these as bands rather than precise figures. The noise floor is " +
            "derived from the analyzer's own sensitivity limit, which makes signal-to-noise figures conservative. " +
            "Throughput estimates assume two spatial streams, 20 MHz channels at 2.4 GHz and 80 MHz at 5 GHz, " +
            "since Windows doesn't report the real channel width. Airtime from wideband sweeps undercounts short " +
            "bursts; turn on the channel listen option for figures you can rely on.";

        _report = BuildText(project, score, ctx, channels.Select(c =>
            $"{c.Band,-4} ch {c.Channel,-4} score {c.Score:0.00}  {c.Comment}"));
    }

    static string Explain(Verdict v) => v switch
    {
        Verdict.Good => "Signal, noise, and airtime all pass.",
        Verdict.Coverage => "Too little signal. Move or add an access point.",
        Verdict.NonWifiInterference => "Energy that no visible network explains. Track it down or change channel.",
        Verdict.CoChannelInterference => "Other networks are eating the signal-to-noise margin. Change channel.",
        Verdict.Congestion => "The channel is simply busy. Add capacity or move clients to a higher band.",
        Verdict.ClientOrApConfig => "The RF is fine but the link negotiates poorly. Check widths, legacy rates, and the client.",
        Verdict.Collisions => "Frames are colliding. Look for a hidden node or too much channel overlap.",
        Verdict.Backhaul => "Throughput lags what the air supports. Look past the Wi-Fi.",
        _ => "Measure these points again."
    };

    static string DescribeEstimates(SurveyProject project, IReadOnlyList<TransmitterEstimate>? estimates,
        Positioning.GeoTransform? geo)
    {
        if (estimates == null || estimates.Count == 0)
            return "No source positions estimated. Tick \"Where the access points are\" on the Survey tab, " +
                   "and measure from several sides of each one.";

        var sb = new StringBuilder();
        foreach (var e in estimates)
        {
            double? metres = project.MetersPerPixel is double mpp ? e.RadiusPx * mpp : null;
            sb.AppendLine($"{e.Label}{(e.LowConfidence ? " (rough)" : "")}");
            sb.Append($"  Plan position {e.X:0}, {e.Y:0} px");
            if (geo != null)
            {
                var (lat, lon) = geo.ToGeo(e.X, e.Y);
                sb.Append($"   {lat:0.000000}, {lon:0.000000}");
            }
            sb.AppendLine(metres is double m ? $"   within about {m:0.#} m" : "");
            sb.AppendLine("  " + e.Note);
            sb.AppendLine();
        }
        sb.Append("Positions are fitted from signal strength, so they're an area rather than a pinpoint. " +
                  "Drawing walls on the plan improves them.");
        return sb.ToString();
    }

    static string DescribeInterference(SurveyProject project, AnalysisContext ctx)
    {
        var findings = new List<(SurveyPoint Point, int Index, Emitter Emitter)>();
        for (int i = 0; i < project.Points.Count; i++)
        {
            var p = project.Points[i];
            foreach (var e in EmitterFinder.Find(p, ctx.OffsetFor(p)))
                findings.Add((p, i + 1, e));
        }
        if (findings.Count == 0)
            return project.Points.Any(p => p.Spectrum != null)
                ? "Nothing showed up that the visible networks don't explain. That's the good answer."
                : "No spectrum captures in this survey, so there's nothing to analyse. Turn on spectrum recording " +
                  "and measure a few points.";

        var sb = new StringBuilder();
        foreach (var group in findings.GroupBy(f => f.Emitter.Kind).OrderByDescending(g => g.Count()))
        {
            var strongest = group.MaxBy(f => f.Emitter.PeakDbm);
            var points = string.Join(", ", group.Select(f => f.Index).Distinct().OrderBy(i => i).Take(8));
            sb.AppendLine($"{EmitterFinder.Label(group.Key)} at point(s) {points}.");
            sb.AppendLine($"  Strongest at point {strongest.Index}: {strongest.Emitter.PeakDbm:0} dBm, " +
                          $"around {strongest.Emitter.CenterMHz:0} MHz, about {strongest.Emitter.WidthMHz:0} MHz wide.");
            sb.AppendLine("  " + strongest.Emitter.Description);
            sb.AppendLine();
        }
        sb.Append("To find a source, switch the map to unexplained energy and walk toward the hottest area.");
        return sb.ToString();
    }

    static string BuildText(SurveyProject project, SurveyScore score, AnalysisContext ctx, IEnumerable<string> channels)
    {
        var sb = new StringBuilder();
        sb.AppendLine($"{project.Name}: {score.Grade}");
        sb.AppendLine(new string('-', 60));
        sb.AppendLine($"Points: {score.Points} ({score.PointsWithSpectrum} with spectrum)");
        sb.AppendLine($"Coverage {score.CoveragePct:0}%  Quality {score.QualityPct:0}%  Airtime {score.AirtimePct:0}%");
        sb.AppendLine();
        foreach (var v in Verdicts.All.Where(v => score.Verdicts.GetValueOrDefault(v.Verdict) > 0))
            sb.AppendLine($"{score.Verdicts[v.Verdict],4}  {v.Label}");
        sb.AppendLine();
        sb.AppendLine("Channel ranking (lower is cleaner)");
        foreach (var c in channels) sb.AppendLine("  " + c);
        sb.AppendLine();
        sb.AppendLine(DescribeInterference(project, ctx));
        return sb.ToString();
    }

    void Save_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog { Filter = "Text (*.txt)|*.txt", FileName = "survey-analysis.txt" };
        if (dlg.ShowDialog() != true) return;
        try { File.WriteAllText(dlg.FileName, _report); }
        catch (Exception ex) { MessageBox.Show("Couldn't save: " + ex.Message, "Wi-Fi Surveyor"); }
    }

    void Copy_Click(object sender, RoutedEventArgs e)
    {
        try { Clipboard.SetText(_report); }
        catch (Exception ex) { MessageBox.Show("Couldn't copy: " + ex.Message, "Wi-Fi Surveyor"); }
    }

    void Close_Click(object sender, RoutedEventArgs e) => Close();
}
