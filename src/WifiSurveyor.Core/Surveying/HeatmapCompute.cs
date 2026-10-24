namespace WifiSurveyor.Surveying;

public readonly record struct HeatSample(double X, double Y, double Value);

public sealed record HeatmapResult(int Width, int Height, int Cell, int[] Pixels);

public static class HeatmapRenderer
{
    // Poor → good
    public static readonly (double T, byte R, byte G, byte B)[] Ramp =
    {
        (0.00, 220, 38, 38),
        (0.30, 249, 115, 22),
        (0.55, 250, 204, 21),
        (0.78, 132, 204, 22),
        (1.00, 21, 128, 61)
    };

    // Spectrum waterfall: quiet → loud
    static readonly (double T, byte R, byte G, byte B)[] SpectrumRamp =
    {
        (0.00, 0, 0, 0),
        (0.25, 20, 30, 140),
        (0.50, 0, 170, 220),
        (0.75, 250, 220, 30),
        (1.00, 240, 40, 30)
    };

    public static int ColorFor(double t, byte alpha) => Lerp(Ramp, t, alpha);

    /// <summary>Colour for a diagnosis verdict, used by the categorical map.</summary>
    public static int VerdictColor(double value, byte alpha)
    {
        var (r, g, b) = Verdicts.Color((Verdict)(int)Math.Round(value));
        return unchecked((alpha << 24) | (r << 16) | (g << 8) | b);
    }
    public static int SpectrumColor(double t) => Lerp(SpectrumRamp, t, 255);

    static int Lerp((double T, byte R, byte G, byte B)[] ramp, double t, byte alpha)
    {
        t = Math.Clamp(t, 0, 1);
        int i = 1;
        while (i < ramp.Length - 1 && t > ramp[i].T) i++;
        var (t0, r0, g0, b0) = ramp[i - 1];
        var (t1, r1, g1, b1) = ramp[i];
        double k = (t - t0) / (t1 - t0);
        int r = (int)(r0 + (r1 - r0) * k), g = (int)(g0 + (g1 - g0) * k), b = (int)(b0 + (b1 - b0) * k);
        return unchecked((alpha << 24) | (r << 16) | (g << 8) | b);
    }

    /// <summary>
    /// Inverse distance weighting on a coarse grid. Cells farther than radiusPx from every
    /// sample stay transparent so the map doesn't invent coverage.
    /// </summary>
    public static HeatmapResult Compute(int imgW, int imgH, IReadOnlyList<HeatSample> pts,
        double min, double max, bool higherIsBetter, double radiusPx, CancellationToken ct)
    {
        int cell = Math.Max(2, (int)Math.Ceiling(Math.Max(imgW, imgH) / 500.0));
        int gw = (imgW + cell - 1) / cell, gh = (imgH + cell - 1) / cell;
        var pixels = new int[gw * gh];
        double r2 = radiusPx * radiusPx, influence2 = 9 * r2;
        double span = max - min <= 0 ? 1 : max - min;

        Parallel.For(0, gh, new ParallelOptions { CancellationToken = ct }, gy =>
        {
            double y = (gy + 0.5) * cell;
            for (int gx = 0; gx < gw; gx++)
            {
                double x = (gx + 0.5) * cell;
                double num = 0, den = 0, nearest = double.MaxValue;
                foreach (var p in pts)
                {
                    double dx = x - p.X, dy = y - p.Y, d2 = dx * dx + dy * dy;
                    if (d2 < nearest) nearest = d2;
                    if (d2 > influence2) continue;
                    double w = 1 / Math.Max(d2, 1e-6);   // power 2
                    num += w * p.Value;
                    den += w;
                }
                if (nearest > r2 || den == 0) continue;   // transparent

                double t = ((num / den) - min) / span;
                if (!higherIsBetter) t = 1 - t;
                double edge = Math.Sqrt(nearest) / radiusPx;
                double fade = edge < 0.75 ? 1 : 1 - (edge - 0.75) / 0.25;
                pixels[gy * gw + gx] = ColorFor(t, (byte)(255 * Math.Clamp(fade, 0, 1)));
            }
        });

        return new HeatmapResult(gw, gh, cell, pixels);
    }

    /// <summary>
    /// Categorical map: no interpolation, since averaging "weak coverage" with "congestion"
    /// would be meaningless. The nearest measurement wins its surroundings.
    /// </summary>
    public static HeatmapResult ComputeCategorical(int imgW, int imgH, IReadOnlyList<HeatSample> pts,
        double radiusPx, CancellationToken ct)
    {
        int cell = Math.Max(2, (int)Math.Ceiling(Math.Max(imgW, imgH) / 500.0));
        int gw = (imgW + cell - 1) / cell, gh = (imgH + cell - 1) / cell;
        var pixels = new int[gw * gh];
        double r2 = radiusPx * radiusPx;

        Parallel.For(0, gh, new ParallelOptions { CancellationToken = ct }, gy =>
        {
            double y = (gy + 0.5) * cell;
            for (int gx = 0; gx < gw; gx++)
            {
                double x = (gx + 0.5) * cell, nearest = double.MaxValue, value = 0;
                foreach (var p in pts)
                {
                    double dx = x - p.X, dy = y - p.Y, d2 = dx * dx + dy * dy;
                    if (d2 >= nearest) continue;
                    nearest = d2;
                    value = p.Value;
                }
                if (nearest > r2) continue;
                double edge = Math.Sqrt(nearest) / radiusPx;
                double fade = edge < 0.75 ? 1 : 1 - (edge - 0.75) / 0.25;
                pixels[gy * gw + gx] = VerdictColor(value, (byte)(220 * Math.Clamp(fade, 0, 1)));
            }
        });

        return new HeatmapResult(gw, gh, cell, pixels);
    }


}
