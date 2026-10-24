using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using WifiSurveyor.Surveying;

namespace WifiSurveyor.Views;

/// <summary>Turns the shared heatmap maths into WPF images and brushes.</summary>
public static class HeatmapImages
{
    public static Color ToColor(int argb) =>
        Color.FromArgb((byte)(argb >> 24), (byte)(argb >> 16), (byte)(argb >> 8), (byte)argb);

    public static LinearGradientBrush LegendBrush(bool higherIsBetter)
    {
        var stops = new GradientStopCollection();
        foreach (var s in HeatmapRenderer.Ramp)
            stops.Add(new GradientStop(Color.FromRgb(s.R, s.G, s.B), higherIsBetter ? s.T : 1 - s.T));
        var brush = new LinearGradientBrush(stops, new Point(0, 0.5), new Point(1, 0.5));
        brush.Freeze();
        return brush;
    }

    public static WriteableBitmap ToBitmap(HeatmapResult r)
    {
        var bmp = new WriteableBitmap(r.Width, r.Height, 96, 96, PixelFormats.Bgra32, null);
        bmp.WritePixels(new Int32Rect(0, 0, r.Width, r.Height), r.Pixels, r.Width * 4, 0);
        bmp.Freeze();
        return bmp;
    }
}
