# Archived

These don't build and aren't maintained. They're kept here rather than deleted so nothing is lost.

- `WifiSurveyor.Cli/` and `WifiSurveyor.Gui/`: a C# command-line tool and an Avalonia GUI for
  Linux. Both reference `src/WifiSurveyor.Linux/WifiSurveyor.Linux.csproj`, which was never in
  this folder, and the GUI's main window references views that don't exist.
- `README-linux.md`: the instructions for that command-line tool.

The Linux application is the GTK4 one in `linux/`. If a C# Linux build is ever wanted again,
the missing platform project (`Platform/` namespace: `iw`/`ip` monitor mode, libpcap capture,
nmcli scanning) has to be written first.
