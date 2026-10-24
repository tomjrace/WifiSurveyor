using System.Globalization;
using System.Net;
using System.Text;
using WifiSurveyor.Models;
using WifiSurveyor.Positioning;
using WifiSurveyor.Wifi;

namespace WifiSurveyor.Surveying;

/// <summary>
/// Writes a single HTML file that holds the whole survey: the map is embedded as a data URI,
/// so the report can be emailed or archived with nothing else alongside it.
/// </summary>
public static class ReportBuilder
{
    public static void Write(string path, SurveyProject project, AnalysisContext ctx, MetricFilter filter,
        MetricInfo metric, byte[]? mapPng, IReadOnlyList<TransmitterEstimate> estimates,
        IReadOnlyList<NoiseSource> noise, GeoTransform? geo)
    {
        var score = SurveyScorer.Score(project, filter, ctx);
        var html = new StringBuilder();

        html.Append("""
            <!doctype html><html lang="en"><head><meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Wi-Fi survey report</title>
            <style>
              :root { color-scheme: light; }
              body { font: 15px/1.55 "Segoe UI", system-ui, sans-serif; color: #1E293B;
                     margin: 0 auto; padding: 32px 28px 60px; max-width: 1000px; }
              h1 { font-size: 26px; margin: 0 0 4px; }
              h2 { font-size: 18px; margin: 32px 0 10px; padding-bottom: 6px;
                   border-bottom: 1px solid #D9DEE5; }
              .muted { color: #5B6778; }
              .grade { display: inline-block; padding: 4px 12px; border-radius: 4px;
                       background: #1D4ED8; color: white; font-weight: 600; }
              .scores { display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0; }
              .score { border: 1px solid #D9DEE5; border-radius: 6px; padding: 12px 16px; min-width: 150px; }
              .score b { display: block; font-size: 24px; }
              table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 14px; }
              th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #E5E9EF; }
              th { background: #F3F5F8; font-weight: 600; }
              td.num { text-align: right; font-variant-numeric: tabular-nums; }
              img.map { max-width: 100%; border: 1px solid #D9DEE5; border-radius: 6px; }
              .swatch { display: inline-block; width: 11px; height: 11px; border-radius: 2px;
                        margin-right: 7px; vertical-align: middle; }
              .note { background: #F3F5F8; border-left: 3px solid #1D4ED8; padding: 10px 14px;
                      margin: 14px 0; }
              @media print { body { padding: 0; } h2 { break-after: avoid; } }
            </style></head><body>
            """);

        html.Append($"<h1>{E(project.Name)}</h1>");
        html.Append($"<p class=\"muted\">{project.Points.Count} measurement points · " +
                    $"{score.PointsWithSpectrum} with spectrum · " +
                    $"judged against {E(filter.Ssid ?? "the strongest network at each point")} · " +
                    $"{DateTime.Now:d MMMM yyyy, HH:mm}</p>");
        html.Append($"<p><span class=\"grade\">{E(score.Grade)}</span></p>");

        html.Append("<div class=\"scores\">");
        html.Append(Score("Coverage", score.CoveragePct, $"at or above {ctx.Targets.MinRssi:0} dBm"));
        html.Append(Score("Quality", score.QualityPct, $"at or above {ctx.Targets.MinSnr:0} dB SNR"));
        html.Append(Score("Airtime", score.AirtimePct,
            $"channel busy under {ctx.Targets.MaxOccupancy * 100:0}%"));
        html.Append("</div>");

        if (mapPng is { Length: > 0 })
        {
            html.Append($"<h2>Map: {E(metric.Title)}</h2>");
            html.Append($"<img class=\"map\" alt=\"Survey map\" src=\"data:image/png;base64,{Convert.ToBase64String(mapPng)}\">");
        }

        // ---- what limits each point ----
        html.Append("<h2>What's limiting each point</h2>");
        if (score.Verdicts.Count == 0)
        {
            html.Append("<p class=\"muted\">No points to judge.</p>");
        }
        else
        {
            html.Append("<table><tr><th>Verdict</th><th class=\"num\">Points</th><th>What it means</th></tr>");
            foreach (var v in Verdicts.All.Where(v => score.Verdicts.GetValueOrDefault(v.Verdict) > 0))
                html.Append($"<tr><td><span class=\"swatch\" style=\"background:rgb({v.R},{v.G},{v.B})\"></span>" +
                            $"{E(v.Label)}</td><td class=\"num\">{score.Verdicts[v.Verdict]}</td>" +
                            $"<td class=\"muted\">{E(Advice(v.Verdict))}</td></tr>");
            html.Append("</table>");
        }

        // ---- channels ----
        html.Append("<h2>Channel ranking</h2>");
        html.Append("<p class=\"muted\">Lower scores are cleaner, weighing other networks, airtime, and " +
                    "unexplained energy across every measured point. Only 1, 6, and 11 are ranked at 2.4 GHz.</p>");
        html.Append("<table><tr><th>Band</th><th class=\"num\">Channel</th><th class=\"num\">Score</th>" +
                    "<th class=\"num\">Other networks</th><th class=\"num\">Busy</th>" +
                    "<th class=\"num\">Unexplained</th><th>Notes</th></tr>");
        foreach (var c in ChannelPlanner.Rank(project, "2.4", ctx).Take(3)
                     .Concat(ChannelPlanner.Rank(project, "5", ctx).Take(8)))
            html.Append($"<tr><td>{E(c.Band)}</td><td class=\"num\">{c.Channel}</td>" +
                        $"<td class=\"num\">{c.Score:0.00}</td>" +
                        $"<td class=\"num\">{(c.WifiDbm is double w ? $"{w:0} dBm" : "–")}</td>" +
                        $"<td class=\"num\">{(c.Occupancy is double o ? $"{o * 100:0}%" : "–")}</td>" +
                        $"<td class=\"num\">{(c.WorstResidual is double r ? $"{r:0} dB" : "–")}</td>" +
                        $"<td class=\"muted\">{E(c.Comment)}</td></tr>");
        html.Append("</table>");

        // ---- sources ----
        if (estimates.Count > 0 || noise.Count > 0)
        {
            html.Append("<h2>Estimated sources</h2>");
            html.Append("<table><tr><th>What</th><th>Position</th><th>Confidence</th><th>Notes</th></tr>");
            foreach (var e in estimates)
            {
                var where = project.MetersPerPixel is double mpp
                    ? $"within about {e.RadiusPx * mpp:0.#} m"
                    : $"within about {e.RadiusPx:0} px";
                var coords = geo != null
                    ? $"{geo.ToGeo(e.X, e.Y).Lat:0.000000}, {geo.ToGeo(e.X, e.Y).Lon:0.000000}"
                    : $"{e.X:0}, {e.Y:0} px on the plan";
                html.Append($"<tr><td>{E(e.Label)}</td><td>{E(coords)}</td>" +
                            $"<td>{E(where)}{(e.LowConfidence ? " (rough)" : "")}</td>" +
                            $"<td class=\"muted\">{E(e.Note)}</td></tr>");
            }
            foreach (var n in noise.Where(n => n.Estimate == null))
                html.Append($"<tr><td>{E(EmitterFinder.Label(n.Kind))} near {n.CenterMHz:0} MHz</td>" +
                            $"<td>–</td><td>seen at {n.PointsSeen} point(s)</td>" +
                            $"<td class=\"muted\">{E(n.Description)}</td></tr>");
            html.Append("</table>");
        }

        // ---- walk test ----
        if (project.Issues.Count > 0)
        {
            html.Append("<h2>Walk test</h2>");
            html.Append($"<p>{project.Issues.Count} issue(s) recorded along {project.Track.Count} position mark(s).</p>");
            html.Append("<table><tr><th>Time</th><th>Adapter</th><th>What happened</th><th>Detail</th></tr>");
            foreach (var issue in project.Issues.OrderBy(i => i.Time).Take(60))
                html.Append($"<tr><td>{issue.Time:HH:mm:ss}</td><td>{E(issue.Adapter)}</td>" +
                            $"<td>{E(StabilityAnalysis.Label(issue.Issue))}</td>" +
                            $"<td class=\"muted\">{E(issue.Detail)}</td></tr>");
            html.Append("</table>");
        }

        // ---- points ----
        html.Append("<h2>Measurement points</h2>");
        html.Append("<table><tr><th class=\"num\">#</th><th>Time</th><th class=\"num\">Best signal</th>" +
                    "<th class=\"num\">APs</th><th class=\"num\">Noise (2.4/5)</th>" +
                    "<th class=\"num\">Throughput</th><th>Verdict</th></tr>");
        int index = 0;
        foreach (var p in project.Points)
        {
            index++;
            var best = MetricExtractor.Best(p, filter);
            var verdict = Diagnoser.Diagnose(p, filter, ctx);
            html.Append($"<tr><td class=\"num\">{index}</td><td>{p.Time:HH:mm:ss}</td>" +
                        $"<td class=\"num\">{(best != null ? $"{best.Rssi} dBm" : "not seen")}</td>" +
                        $"<td class=\"num\">{p.Networks.Count}</td>" +
                        $"<td class=\"num\">{Fmt(p.Spectrum?.NoiseFloor24)} / {Fmt(p.Spectrum?.NoiseFloor5)}</td>" +
                        $"<td class=\"num\">{(p.ThroughputDownMbps is double d ? $"{d:0} Mbps" : "–")}</td>" +
                        $"<td>{E(Verdicts.Label(verdict.Verdict))}</td></tr>");
        }
        html.Append("</table>");

        html.Append("<h2>How to read this</h2><div class=\"note\">");
        html.Append(E("Coverage, quality, and airtime are reported separately because an area can have " +
                      "flawless coverage and still be unusable. Spectrum figures come from an RF Explorer, " +
                      "whose amplitude accuracy is ±3 dB on its 6G module and ±6 dB on WSUB3G, so treat " +
                      "them as bands rather than exact values. Estimated positions are fitted from signal " +
                      "strength and are areas, not pinpoints."));
        html.Append("</div>");

        html.Append($"<p class=\"muted\">Generated by Wi-Fi Surveyor on {DateTime.Now:F}.</p>");
        html.Append("</body></html>");

        File.WriteAllText(path, html.ToString(), new UTF8Encoding(true));
    }

    static string Score(string name, double percent, string detail) =>
        $"<div class=\"score\"><span class=\"muted\">{E(name)}</span><b>{percent:0}%</b>" +
        $"<span class=\"muted\">{E(detail)}</span></div>";

    static string Fmt(double? value) =>
        value is double d ? d.ToString("0", CultureInfo.InvariantCulture) : "–";

    static string E(string? text) => WebUtility.HtmlEncode(text ?? "");

    static string Advice(Verdict verdict) => verdict switch
    {
        Verdict.Good => "Signal, noise, and airtime all pass.",
        Verdict.Coverage => "Too little signal: move or add an access point.",
        Verdict.NonWifiInterference => "Energy no visible network explains: track it down or change channel.",
        Verdict.CoChannelInterference => "Neighbouring networks are eating the margin: change channel.",
        Verdict.Congestion => "The channel is busy: add capacity or move clients to a higher band.",
        Verdict.Collisions => "Frames are colliding: look for a hidden node or too much overlap.",
        Verdict.ClientOrApConfig => "Check channel width, legacy rates, and the client adapter.",
        Verdict.Backhaul => "Throughput lags what the air supports: look past the Wi-Fi.",
        _ => "Measure these points again."
    };
}
