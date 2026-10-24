namespace WifiSurveyor.Surveying;

public sealed record WallCandidate(double X1, double Y1, double X2, double Y2, double LengthPx);

public sealed record WallDetectOptions(int MinLengthPx = 40, int MaxGapPx = 5, int MaxWalls = 250);

/// <summary>
/// Finds straight lines in a floor-plan image with a Hough transform, then walks each line to
/// pull out the runs that are actually drawn. Floor plans are line art, so this works well on
/// them; photographs and heavily shaded renders give messier results.
/// </summary>
public static class WallDetector
{
    public static List<WallCandidate> Detect(byte[] gray, int width, int height, WallDetectOptions options)
    {
        if (width < 8 || height < 8) return new List<WallCandidate>();

        var ink = Threshold(gray, width, height);
        int inkCount = 0;
        foreach (var v in ink) if (v) inkCount++;
        if (inkCount < options.MinLengthPx) return new List<WallCandidate>();

        // Very dense images (dark backgrounds, photos) would swamp the accumulator.
        int stride = Math.Max(1, inkCount / 150_000);

        const int Thetas = 180;
        double diagonal = Math.Sqrt(width * width + height * height);
        int rhoBins = (int)(2 * diagonal) + 1;
        int rhoOffset = (int)diagonal;

        var cos = new double[Thetas];
        var sin = new double[Thetas];
        for (int t = 0; t < Thetas; t++)
        {
            double angle = t * Math.PI / Thetas;
            cos[t] = Math.Cos(angle);
            sin[t] = Math.Sin(angle);
        }

        var votes = new int[Thetas * rhoBins];
        int seen = 0;
        for (int y = 0; y < height; y++)
        for (int x = 0; x < width; x++)
        {
            if (!ink[y * width + x]) continue;
            if (seen++ % stride != 0) continue;
            for (int t = 0; t < Thetas; t++)
            {
                int rho = (int)Math.Round(x * cos[t] + y * sin[t]) + rhoOffset;
                if (rho >= 0 && rho < rhoBins) votes[t * rhoBins + rho]++;
            }
        }

        int minVotes = Math.Max(12, options.MinLengthPx / (2 * stride));
        var peaks = new List<(int Theta, int Rho, int Votes)>();
        for (int t = 0; t < Thetas; t++)
        for (int r = 1; r < rhoBins - 1; r++)
        {
            int v = votes[t * rhoBins + r];
            if (v < minVotes) continue;
            if (v < votes[t * rhoBins + r - 1] || v < votes[t * rhoBins + r + 1]) continue;   // local peak only
            peaks.Add((t, r, v));
        }

        var accepted = new List<(int Theta, int Rho)>();
        foreach (var peak in peaks.OrderByDescending(p => p.Votes))
        {
            if (accepted.Count >= options.MaxWalls * 2) break;
            bool duplicate = accepted.Any(a =>
                AngleDistance(a.Theta, peak.Theta, Thetas) <= 2 && Math.Abs(a.Rho - peak.Rho) <= 8);
            if (!duplicate) accepted.Add((peak.Theta, peak.Rho));
        }

        var segments = new List<WallCandidate>();
        foreach (var (theta, rho) in accepted)
            segments.AddRange(ExtractSegments(ink, width, height, cos[theta], sin[theta], rho - rhoOffset, options));

        return Merge(segments)
            .OrderByDescending(s => s.LengthPx)
            .Take(options.MaxWalls)
            .ToList();
    }

    /// <summary>Otsu's method, inverted when the drawing turns out to be light on dark.</summary>
    static bool[] Threshold(byte[] gray, int width, int height)
    {
        var histogram = new int[256];
        foreach (var value in gray) histogram[value]++;
        int total = gray.Length;

        double sum = 0;
        for (int i = 0; i < 256; i++) sum += i * (double)histogram[i];

        double sumBackground = 0, best = -1;
        int weightBackground = 0, threshold = 128;
        for (int i = 0; i < 256; i++)
        {
            weightBackground += histogram[i];
            if (weightBackground == 0) continue;
            int weightForeground = total - weightBackground;
            if (weightForeground == 0) break;

            sumBackground += i * (double)histogram[i];
            double meanBackground = sumBackground / weightBackground;
            double meanForeground = (sum - sumBackground) / weightForeground;
            double between = (double)weightBackground * weightForeground *
                             (meanBackground - meanForeground) * (meanBackground - meanForeground);
            if (between > best)
            {
                best = between;
                threshold = i;
            }
        }

        var ink = new bool[gray.Length];
        int dark = 0;
        for (int i = 0; i < gray.Length; i++)
        {
            ink[i] = gray[i] <= threshold;
            if (ink[i]) dark++;
        }
        if (dark > gray.Length * 0.5)             // light lines on a dark background
            for (int i = 0; i < ink.Length; i++) ink[i] = !ink[i];
        return ink;
    }

    static IEnumerable<WallCandidate> ExtractSegments(bool[] ink, int width, int height,
        double cos, double sin, double rho, WallDetectOptions options)
    {
        // Walk the line one pixel at a time, allowing small gaps for doorways and dashes.
        double px = rho * cos, py = rho * sin;
        double dx = -sin, dy = cos;
        double limit = Math.Sqrt(width * width + height * height);

        var found = new List<WallCandidate>();
        double? runStart = null;
        double lastInk = 0;

        for (double t = -limit; t <= limit; t += 1)
        {
            double x = px + dx * t, y = py + dy * t;
            if (x < 0 || y < 0 || x >= width || y >= height)
            {
                if (runStart != null) Close(ref runStart, lastInk);
                continue;
            }

            bool hit = false;
            for (int offset = -1; offset <= 1 && !hit; offset++)
            {
                int ix = (int)Math.Round(x + dy * offset * 0.8);
                int iy = (int)Math.Round(y - dx * offset * 0.8);
                if (ix >= 0 && iy >= 0 && ix < width && iy < height && ink[iy * width + ix]) hit = true;
            }

            if (hit)
            {
                runStart ??= t;
                lastInk = t;
            }
            else if (runStart != null && t - lastInk > options.MaxGapPx)
            {
                Close(ref runStart, lastInk);
            }
        }
        if (runStart != null) Close(ref runStart, lastInk);
        return found;

        void Close(ref double? start, double end)
        {
            double length = end - start!.Value;
            if (length >= options.MinLengthPx)
                found.Add(new WallCandidate(
                    px + dx * start.Value, py + dy * start.Value,
                    px + dx * end, py + dy * end, length));
            start = null;
        }
    }

    /// <summary>
    /// Walls are usually drawn as two parallel lines, so near-duplicates are collapsed into
    /// one line down the middle.
    /// </summary>
    static List<WallCandidate> Merge(List<WallCandidate> segments)
    {
        var merged = new List<WallCandidate>();
        foreach (var s in segments.OrderByDescending(s => s.LengthPx))
        {
            bool absorbed = false;
            for (int i = 0; i < merged.Count && !absorbed; i++)
            {
                var m = merged[i];
                if (Angle(s) is var a1 && Angle(m) is var a2 &&
                    AngleDistance(a1, a2) > 5 * Math.PI / 180) continue;

                double distance = Math.Min(
                    PointToSegment(s.X1, s.Y1, m.X1, m.Y1, m.X2, m.Y2),
                    PointToSegment(s.X2, s.Y2, m.X1, m.Y1, m.X2, m.Y2));
                if (distance > 14) continue;

                merged[i] = new WallCandidate(
                    (m.X1 + s.X1) / 2, (m.Y1 + s.Y1) / 2,
                    (m.X2 + s.X2) / 2, (m.Y2 + s.Y2) / 2,
                    Math.Max(m.LengthPx, s.LengthPx));
                absorbed = true;
            }
            if (!absorbed) merged.Add(s);
        }
        return merged;
    }

    static double Angle(WallCandidate s)
    {
        double a = Math.Atan2(s.Y2 - s.Y1, s.X2 - s.X1);
        return a < 0 ? a + Math.PI : a;
    }

    static double AngleDistance(double a, double b)
    {
        double d = Math.Abs(a - b) % Math.PI;
        return Math.Min(d, Math.PI - d);
    }

    static int AngleDistance(int a, int b, int wrap)
    {
        int d = Math.Abs(a - b) % wrap;
        return Math.Min(d, wrap - d);
    }

    public static double PointToSegment(double px, double py, double x1, double y1, double x2, double y2)
    {
        double dx = x2 - x1, dy = y2 - y1;
        double lengthSquared = dx * dx + dy * dy;
        if (lengthSquared < 1e-9) return Math.Sqrt((px - x1) * (px - x1) + (py - y1) * (py - y1));
        double t = Math.Clamp(((px - x1) * dx + (py - y1) * dy) / lengthSquared, 0, 1);
        double cx = x1 + t * dx, cy = y1 + t * dy;
        return Math.Sqrt((px - cx) * (px - cx) + (py - cy) * (py - cy));
    }
}
