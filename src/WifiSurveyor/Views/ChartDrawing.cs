using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Shapes = System.Windows.Shapes;

namespace WifiSurveyor.Views;

/// <summary>Maps data coordinates onto a canvas plot rectangle.</summary>
public sealed class PlotArea
{
    public PlotArea(Canvas c, double xMin, double xMax, double yMin, double yMax,
        double left = 44, double right = 10, double top = 18, double bottom = 22)
    {
        (XMin, XMax, YMin, YMax) = (xMin, xMax == xMin ? xMin + 1 : xMax, yMin, yMax);
        Rect = new Rect(left, top,
            Math.Max(1, c.ActualWidth - left - right),
            Math.Max(1, c.ActualHeight - top - bottom));
    }

    public double XMin { get; }
    public double XMax { get; }
    public double YMin { get; }
    public double YMax { get; }
    public Rect Rect { get; }

    public double X(double v) => Rect.Left + (v - XMin) / (XMax - XMin) * Rect.Width;
    public double Y(double v) => Rect.Bottom - (v - YMin) / (YMax - YMin) * Rect.Height;
    public double XValue(double px) => XMin + (px - Rect.Left) / Rect.Width * (XMax - XMin);
}

public static class Draw
{
    public static SolidColorBrush Brush(byte r, byte g, byte b, byte a = 255)
    {
        var brush = new SolidColorBrush(Color.FromArgb(a, r, g, b));
        brush.Freeze();
        return brush;
    }

    static readonly SolidColorBrush[] SeriesBrushes =
    {
        Brush(29, 78, 216), Brush(220, 38, 38), Brush(21, 128, 61), Brush(202, 138, 4),
        Brush(126, 34, 206), Brush(14, 116, 144), Brush(190, 24, 93), Brush(77, 124, 15),
        Brush(67, 56, 202), Brush(194, 65, 12)
    };

    public static SolidColorBrush Series(int i) =>
        SeriesBrushes[((i % SeriesBrushes.Length) + SeriesBrushes.Length) % SeriesBrushes.Length];

    public static SolidColorBrush SeriesFor(string key)
    {
        unchecked
        {
            int h = 17;
            foreach (var ch in key) h = h * 31 + ch;
            return Series(h & 0x7fffffff);
        }
    }

    public static SolidColorBrush WithAlpha(SolidColorBrush b, byte a) => Brush(b.Color.R, b.Color.G, b.Color.B, a);

    public static Shapes.Line Line(Canvas c, double x1, double y1, double x2, double y2, Brush stroke,
        double thickness = 1, bool dashed = false)
    {
        var line = new Shapes.Line
        {
            X1 = x1, Y1 = y1, X2 = x2, Y2 = y2,
            Stroke = stroke,
            StrokeThickness = thickness,
            SnapsToDevicePixels = true
        };
        if (dashed) line.StrokeDashArray = new DoubleCollection { 3, 3 };
        c.Children.Add(line);
        return line;
    }

    public static TextBlock Text(Canvas c, string s, double x, double y, Brush? fg = null,
        double size = 11, bool center = false, bool right = false)
    {
        var t = new TextBlock { Text = s, FontSize = size, Foreground = fg ?? Brushes.DimGray };
        if (center || right)
        {
            t.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
            x -= center ? t.DesiredSize.Width / 2 : t.DesiredSize.Width;
        }
        Canvas.SetLeft(t, x);
        Canvas.SetTop(t, y);
        c.Children.Add(t);
        return t;
    }

    public static Shapes.Polyline Poly(Canvas c, IEnumerable<Point> pts, Brush stroke, double thickness = 1.5)
    {
        var p = new Shapes.Polyline
        {
            Points = new PointCollection(pts),
            Stroke = stroke,
            StrokeThickness = thickness,
            StrokeLineJoin = PenLineJoin.Round
        };
        c.Children.Add(p);
        return p;
    }

    public static void YGrid(Canvas c, PlotArea p, double step, Brush? label = null, Brush? line = null)
    {
        for (double v = Math.Ceiling(p.YMin / step) * step; v <= p.YMax + 1e-9; v += step)
        {
            double y = p.Y(v);
            Line(c, p.Rect.Left, y, p.Rect.Right, y, line ?? Brushes.Gainsboro);
            Text(c, v.ToString("0"), p.Rect.Left - 4, y - 8, label, 10, right: true);
        }
    }

    public static double NiceStep(double raw)
    {
        if (raw <= 0) return 1;
        double mag = Math.Pow(10, Math.Floor(Math.Log10(raw)));
        double n = raw / mag;
        return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
    }
}
