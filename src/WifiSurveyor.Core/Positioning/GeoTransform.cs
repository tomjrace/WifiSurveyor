using WifiSurveyor.Models;

namespace WifiSurveyor.Positioning;

public static class GeoMath
{
    public const double EarthRadiusM = 6_371_000;
    static double Rad(double deg) => deg * Math.PI / 180;

    /// <summary>Meters east/north of a reference point (flat-earth; fine for building-sized areas).</summary>
    public static (double E, double N) ToLocalMeters(double lat, double lon, double refLat, double refLon) =>
        (Rad(lon - refLon) * EarthRadiusM * Math.Cos(Rad(refLat)),
         Rad(lat - refLat) * EarthRadiusM);

    public static (double Lat, double Lon) FromLocalMeters(double e, double n, double refLat, double refLon) =>
        (refLat + n / EarthRadiusM * 180 / Math.PI,
         refLon + e / (EarthRadiusM * Math.Cos(Rad(refLat))) * 180 / Math.PI);
}

/// <summary>
/// Least-squares similarity transform (scale, rotation, offset) from anchor points.
/// Local coordinates use (east, −north) so north-up plans don't need a mirror.
/// </summary>
public sealed class GeoTransform
{
    readonly double _refLat, _refLon, _a, _b, _tx, _ty;

    GeoTransform(double refLat, double refLon, double a, double b, double tx, double ty)
    {
        (_refLat, _refLon, _a, _b, _tx, _ty) = (refLat, refLon, a, b, tx, ty);
    }

    public double MetersPerPixel => 1 / Math.Sqrt(_a * _a + _b * _b);
    public double RmsErrorM { get; private set; }

    public static GeoTransform? TryCreate(IReadOnlyList<GeoAnchor> anchors)
    {
        if (anchors.Count < 2) return null;

        double refLat = anchors.Average(a => a.Lat), refLon = anchors.Average(a => a.Lon);
        var u = anchors.Select(a =>
        {
            var (e, n) = GeoMath.ToLocalMeters(a.Lat, a.Lon, refLat, refLon);
            return (x: e, y: -n);
        }).ToList();

        double ux = u.Average(q => q.x), uy = u.Average(q => q.y);
        double px = anchors.Average(a => a.X), py = anchors.Average(a => a.Y);

        double sxx = 0, dot = 0, cross = 0;
        for (int i = 0; i < anchors.Count; i++)
        {
            double ax = u[i].x - ux, ay = u[i].y - uy;
            double bx = anchors[i].X - px, by = anchors[i].Y - py;
            sxx += ax * ax + ay * ay;
            dot += ax * bx + ay * by;
            cross += ax * by - ay * bx;
        }
        if (sxx < 1e-6) return null;   // anchors at the same location

        double a = dot / sxx, b = cross / sxx;
        if (a * a + b * b < 1e-12) return null;

        var t = new GeoTransform(refLat, refLon, a, b,
            px - (a * ux - b * uy),
            py - (b * ux + a * uy));

        double err = 0;
        foreach (var anchor in anchors)
        {
            var (x, y) = t.ToPixel(anchor.Lat, anchor.Lon);
            err += (x - anchor.X) * (x - anchor.X) + (y - anchor.Y) * (y - anchor.Y);
        }
        t.RmsErrorM = Math.Sqrt(err / anchors.Count) * t.MetersPerPixel;
        return t;
    }

    public (double X, double Y) ToPixel(double lat, double lon)
    {
        var (e, n) = GeoMath.ToLocalMeters(lat, lon, _refLat, _refLon);
        double ex = e, ey = -n;
        return (_a * ex - _b * ey + _tx, _b * ex + _a * ey + _ty);
    }

    public (double Lat, double Lon) ToGeo(double x, double y)
    {
        double dx = x - _tx, dy = y - _ty, det = _a * _a + _b * _b;
        double ex = (_a * dx + _b * dy) / det;
        double ey = (-_b * dx + _a * dy) / det;
        return GeoMath.FromLocalMeters(ex, -ey, _refLat, _refLon);
    }
}
