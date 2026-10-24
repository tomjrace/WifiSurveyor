# Wi-Fi Surveyor on Linux

A command-line build that shares its file formats **and its analysis code** with the Windows
application: the models, RF Explorer driver, spectrum maths, Radiotap and 802.11 parsing,
diagnosis, source localization, and the HTML report are the same source files, compiled into
both. A project measured here opens there, and the other way round.

Linux is the better platform for the capture half of this work, because monitor mode is a
first-class feature of the kernel's Wi-Fi stack rather than something each Windows vendor may
or may not have implemented. The Alfa AWUS036ACM (mt76x2u) and AWUS036AXML (mt7921u) both do
monitor mode here.

## Build and run

```
dotnet build src/WifiSurveyor.Linux -c Release
sudo ./src/WifiSurveyor.Linux/bin/Release/net8.0/wifisurveyor check
```

Needs the .NET 8 SDK, `iw`, `ip`, and libpcap (`libpcap0.8` on Debian and Ubuntu,
`libpcap` on Fedora and Arch). `nmcli` is optional but lets scanning work without root.

## Commands

```
wifisurveyor check                     what's installed, which interfaces do monitor mode
wifisurveyor interfaces                wireless interfaces, drivers, current mode
wifisurveyor scan --dev wlan0          nearby access points
wifisurveyor monitor --dev wlan1 --channel 36
wifisurveyor restore --dev wlan1
wifisurveyor capture --dev wlan1 --seconds 15 --write out.pcap
wifisurveyor measure --project site.wsurvey --x 120 --y 340 \
    --dev wlan0 --rfe /dev/ttyUSB0 --dwell 3 --gps /dev/ttyACM0 \
    --capture wlan1 --capture-seconds 10
wifisurveyor report --project site.wsurvey --out report.html
```

`monitor` checks that the driver actually lists monitor among its interface modes before
trying, releases the interface from NetworkManager (which would otherwise scan on it and pull
it back), switches the type with `iw`, sets the channel, and confirms it took. `restore`
undoes all of that.

`measure` runs the same sequence as a click on the Windows map: averaged Wi-Fi scans, the
current link, RF Explorer sweeps of 2.4 and both halves of 5 GHz, an optional dwell on the
busy channel for trustworthy airtime, an optional monitor-mode capture for retries and client
counts, and a GPS fix. It appends the point to the project file.

Positions are given as plan pixels with `--x` and `--y`, the same coordinates the Windows app
uses, so a survey can be started on one and continued on the other.

## What isn't here

The graphical side: the map, heatmaps, drawing walls, the live spectrum display, and the
interactive tabs are WPF and Windows-only. Porting the interface means Avalonia, which reads
the same XAML dialect and runs on Linux; the platform layer this build already provides
(`Platform/`) is what such a port would sit on. Also absent: multi-adapter load testing and
the walk test, both of which lean on Windows-specific link monitoring.
