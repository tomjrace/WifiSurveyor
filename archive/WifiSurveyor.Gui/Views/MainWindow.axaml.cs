using Avalonia.Controls;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using WifiSurveyor.Gui.Services;

namespace WifiSurveyor.Gui.Views;

public partial class MainWindow : Window
{
    readonly DispatcherTimer _status = new() { Interval = TimeSpan.FromSeconds(1) };

    public MainWindow()
    {
        InitializeComponent();
        _status.Tick += (_, _) => StatusText.Text = GuiState.Instance.StatusLine();
        _status.Start();
        Closed += (_, _) => GuiState.Instance.Shutdown();
    }

    void InitializeComponent() => AvaloniaXamlLoader.Load(this);
}
