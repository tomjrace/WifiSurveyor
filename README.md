# Wi-Fi Surveyor

Wi-Fi Surveyor walks a building, measures the wireless, and writes a map and a report. Tom Race wrote it. It is [MIT licensed](LICENSE): you can use it, change it, and ship it, and it comes with no warranty.

**Linux** (`linux/`) is the application to use. It is a GTK4 program: heatmaps, RF Explorer and tinySA spectrum analysis, HackRF, RTL-SDR, 802.11 capture, and GPS. Install and use it from [linux/README.md](linux/README.md).

**Windows** (`src/`) is a separate WPF app for surveys, an RF Explorer, and GPS. It does not capture 802.11 frames live, and that is not planned. Frame capture, retry rates, and airtime measured from frames are in the Linux app. The rest of this file is the Windows app.

## The Windows app

A Windows Wi-Fi site-survey tool in the spirit of NetSpot, with RF Explorer spectrum analysis and USB GPS support.

## What it does

**Discover** lists every access point (BSSID) Windows can see, with SSID, vendor, band, channel, signal, min/max signal, and security. Windows returns its scan results once per WLAN adapter, so with several plugged in every network would otherwise appear several times; the list merges them by default, keeping each BSSID once at the strongest reading any radio heard, and says how many radios it merged. The tick box turns that off if you want the raw per-adapter readings. Survey measurements merge the same way, so averaging across passes never mixes radios of different sensitivity. It rescans every 5 seconds, draws signal over the last five minutes, and shows 2.4 GHz and 5 GHz channel maps so overlapping networks are easy to spot.

**Spectrum** drives the RF Explorer 6G Combo live: current trace, max hold, average, a scrolling waterfall, Wi-Fi channel markers, scanned networks overlaid, and a hover readout.

Presets cover 2.4 GHz, lower and upper 5 GHz, the part of 6 GHz the analyzer can reach (5925–6100 MHz, channels 1–29), and combined views: full 5 GHz, 2.4 + 5 GHz, and 2.4 + 5 + 6 GHz. The analyzer can only sweep one range at a time (600 MHz maximum span, and 2.4 GHz is on a different module), so combined views cycle through their ranges, taking a few sweeps of each ("Sweeps per range"), and show one panel per band. A yellow bar under the plot marks the range being swept. In combined views the waterfall adds one row per full cycle, and a full 4-range cycle typically takes several seconds to tens of seconds depending on sweep speed. 6 GHz preferred scanning channels (5, 21) are marked with an asterisk. Live cycling and survey captures share the analyzer through a lock, so a survey measurement waits for the current range to finish and then takes over.

**Survey** is the heatmap workflow:

1. Load a floor plan image, or survey on a blank grid.
2. Set the scale by clicking both ends of a known distance.
3. Optionally pin two or more spots to GPS coordinates so the plan is georeferenced.
4. Click where you're standing to measure. Each point records a Wi-Fi scan (averaged over several scans), the connected link's PHY rate and security, and optionally an RF Explorer capture of 2.4 GHz and both halves of 5 GHz, plus an iperf3 upload/download test.
   Turn on "Listen to the serving channel" for airtime you can rely on: a swept analyzer revisits a wide range too slowly to catch short bursts, so it undercounts. This parks on the channel in use for a few seconds, which is fast to sweep, and the resulting duty cycle feeds the occupancy map and the congestion check.
5. Outdoors, with a good GPS fix and pins on the plan, the app can measure automatically as you walk.
6. View heatmaps for signal level, signal-to-noise ratio, noise floor, overlapping APs, PHY rate, iperf3 throughput, and per-channel spectrum power and occupancy. Filter by SSID, BSSID, band, and channel.
7. Save the project (`.wsurvey`, with the floor plan embedded) and export CSV, KML (Google Earth, QGIS), a PNG with legend, or a **report**.

**The report** is one self-contained HTML file: the map embedded as an image, the grade and the three scores, a breakdown of what limits each point with advice, the channel ranking, estimated source positions with coordinates, walk-test issues, and a table of every measurement. Nothing else needs to travel with it, and it prints tidily.

**Autosave and recovery.** The survey is written to a recovery file every two minutes while there are unsaved changes. If the app or the machine stops unexpectedly, the next start offers to reopen it, naming the file it came from. Saving normally clears it.

**Undo** covers the destructive actions: deleting a point or wall, clearing walls, guessed walls, GPS pins, or walk data, and discarding found walls. The button names what it will undo.

**"Show where to measure next"** shades the parts of the plan that no measurement reaches, using the same fill distance as the heatmap, which turns "have I covered this floor?" into something you can see.

**"What counts as good"** sets the thresholds every verdict and score is judged against: minimum signal, minimum SNR, maximum airtime, minimum link rate. Set them to whatever the site has to meet and the diagnosis, the scores, and the report all follow.

**Estimated source positions.** The app can work out where a transmitter probably is from the signal measured around it, fitting a log-distance path-loss model (received = P0 − 10·n·log10(distance) − walls crossed) over a grid of candidate positions and exponents. It draws a marker with a dashed circle showing how tightly the fit is pinned down, not a false pinpoint.

- **Access points:** tick "Where the access points are". Works on the selected SSID or BSSID, otherwise the most-measured access points.
- **Noise sources:** tick "Where the noise is coming from". No channel to pick: it searches every captured spectrum segment for emissions the visible networks don't explain, groups them by frequency across the whole survey, and marks where each source probably is. Each one is named by frequency and character ("continuous emitter near 2452 MHz"), and quiet points count too, since they're what tell the fit which direction the signal falls off in.
- **Walls, guessed from the measurements:** "Guess walls from the measurements" needs no floor plan at all. Once access points are located, the signal at every point can be predicted from distance; where two neighbouring points disagree sharply with that prediction and several access points agree about it, something solid stands between them. The app places a barrier across the line between those points, sized by how much loss is unaccounted for. These are drawn in teal with a dashed line, are used in the maths straight away, and can be dragged, retyped, or removed like any other. It doesn't try to identify the material, only that something is in the way.

- **Walls, found in the plan image:** the app can also find them optically. "Find walls in the plan" runs a Hough-transform line search over the floor plan image and proposes wall segments, drawn dashed in amber. They're ignored by the maths until you press "Keep all found", so a bad detection can't quietly skew an estimate. Delete the wrong ones first, or discard the lot.
  Walls are editable either way: click one to select it, drag either end to move it, drag the middle to shift the whole wall, change its material, or delete it. Drawing by hand still works, two clicks per wall. Materials carry typical attenuation (drywall about 3 dB, glass 2, brick 8, concrete 12, metal or lift shaft 20), counted whenever a path crosses them, so a signal weakened by concrete isn't mistaken for a distant one.
  Detection works well on line-art floor plans, which is what most building drawings are. Scans, photographs, and shaded architectural renders give messier results, and the "Detail" setting trades completeness against false positives.

Accuracy depends on geometry. Measure from several sides of a source: points all on one side give a stretched, low-confidence result, which the app labels as rough and explains in the note. If the best fit lands at the edge of the plan, it says the source is probably outside the building.

**Analysis** turns the two data sources into answers. Because the Wi-Fi card only sees energy it can decode into a BSSID while the analyzer sees all energy, the gap between them is diagnostic:

- **Offset calibration.** Card and analyzer use different antennas and receivers, so their dBm values aren't comparable until referenced to each other. The app takes the median difference on channels where a strong visible network dominates, per point, falling back to a survey-wide median.
- **Unexplained energy** (heatmap): measured channel power minus what the visible networks and the noise floor account for. Near 0 dB means accounted for; above about 6 dB means something is transmitting that the card can't attribute. Walk toward the hot area to find it.
- **What's limiting this spot** (heatmap): a decision tree that stops at the first failing check and names a cause and an action, rather than a number: weak coverage, non-Wi-Fi interference, neighbouring networks, airtime congestion, link configuration, or beyond the Wi-Fi. This map is categorical, so points aren't blended.
- **Estimated usable throughput** (heatmap): PHY rate implied by the signal-to-noise ratio, discounted for busy airtime and protocol overhead. Comparing it against an iperf3 result is what separates an RF problem from a backhaul one.
- **Emitter fingerprints:** runs of energy the networks don't explain are classified by shape and steadiness into a microwave oven, a continuous emitter such as an analog camera, wideband energy, or a bursty transmitter. Shown per point and summarised in the report.
- **Channel ranking:** every channel scored across all measured points, weighing other networks, airtime, and unexplained energy. Because measurements carry positions, a channel clean in one room but busy in another scores honestly.
- **Scores:** coverage, quality, and airtime reported separately, since an area can have flawless coverage and still be unusable.

Open "Analysis report…" on the Survey tab for the written summary, channel table, and interference findings, which can be saved or copied.

**Capture of 802.11 frames is not part of this app, and it is not going to be.** Live monitor mode on Windows never became reliable enough to keep. Retry rate, airtime measured from frame durations, clients heard, and beacon details are in the Linux app. The Windows Capture tab can still open a `.pcap` or `.pcapng` you already have; it will not be extended to capture live.

**Walk test.** Standing measurements miss the failures that only happen while moving. On the Survey tab, "Walk test: mark where I am" starts a run: every adapter stays associated, optionally running short speed tests back to back, and each drop, roam, rate collapse, or failed test is recorded with where you were. Indoors you click the plan as you pass known spots, and events recorded in between are placed along your path by time; with GPS pinned and a good fix, positions come automatically. The map then shows your path plus circles marking trouble spots, sized by how many issues happened there and coloured by the worst one, with a tooltip breaking down what went wrong. This is how you find the dead spot in the corridor and the place where clients cling to a distant access point instead of handing off.

**Tools** runs the Windows built-ins (`show interfaces`, `show networks mode=bssid`, saved profiles, and the Wi-Fi history report, which is Windows' own HTML log of connections, disconnects, and errors). Live monitor mode is not a feature of this app.

**Load Test** uses every WLAN adapter on the machine, in four collapsible sections on one scrolling page: adapters, test servers, the run itself, and link events. Each is polled once a second, and drops, roams between access points, and large falls in negotiated rate are logged with a timestamp, so an adapter left associated while you survey becomes a witness to what the network actually did. The same tab runs iperf3 on several adapters at once, each bound to its own address and server port, which measures how a cell holds up under several clients rather than one.

**Devices** connects the RF Explorer and GPS, sets up iperf3, and links to the Windows location settings.

## Building

Requirements: Windows 10 (1809) or Windows 11, and Visual Studio 2022 (17.8 or later) with the ".NET desktop development" workload, or the .NET 8 SDK.

```
dotnet build WifiSurveyor.sln -c Release
dotnet run --project src/WifiSurveyor
```

or open `WifiSurveyor.sln` in Visual Studio and press F5. NuGet restores `ManagedNativeWifi`, `System.IO.Ports`, and `System.Management`.

### Things to check on the first build

This code was written without access to a Windows build machine, so it has not been compiled. Most likely trouble spots:

- **ManagedNativeWifi member names.** The version is floating (`2.*`). All calls are in `Wifi/WifiScanner.cs`. If the build complains, check `BssNetworkPack` for the BSSID, frequency (kHz), band, and channel property names in the version NuGet restored, and fix that one file.
- **Anything else the compiler reports.** Paste the error list back and it can be fixed quickly.

## One-time setup

**Location access.** Windows 11 hides Wi-Fi scan results from apps without location permission. Go to Settings → Privacy & security → Location and turn on "Let desktop apps access your location." The app shows a banner with a shortcut if access is off.

**Vendor names (optional).** Download `oui.csv` from https://standards-oui.ieee.org/oui/oui.csv and put it next to `WifiSurveyor.exe` or in `%LOCALAPPDATA%\WifiSurveyor\`. BSSIDs with the locally administered bit set are shown as "(virtual BSSID)"; these are usually extra SSIDs on the same AP radio.

**iperf3.** The build can bundle it. Run:

```
powershell -ExecutionPolicy Bypass -File tools\fetch-iperf3.ps1
```

That downloads a Windows build into `tools\iperf3`, and everything in that folder is copied into the application's `iperf3` folder at build time, where the app finds it automatically. Leave the folder empty and the app falls back to whatever iperf3 is on your path or the one you set on the Devices tab.

Before passing the result on to anyone else, read `tools\iperf3\README.txt`. iperf3 itself is BSD-3-Clause and redistributable as long as its LICENSE travels with the binary, but most Windows builds are Cygwin builds that also need `cygwin1.dll`, which is LGPLv3 and brings its own obligations. If you would rather not take that on, ship no iperf3 and let users install their own.

Then run `iperf3 -s` on a wired machine on the same network and set the server and port on the Devices tab.

If you have no server of your own, the Devices tab can pull the public list from [iperf3serverlist.net](https://iperf3serverlist.net) (the project behind [R0GGER/public-iperf3-servers](https://github.com/R0GGER/public-iperf3-servers)) and offer it as region, then country, then server. It reads the project's parsed JSON export, which refreshes hourly, and caches it locally so the picker still works offline; "Update list" forces a refresh. Choosing a server fills in its host and first port, and says whether it has a port range, which decides how many adapters can use it at once.

Be clear about what public servers measure: the path to the internet, capped by your uplink and by whatever load the server is under. For mapping Wi-Fi capacity a LAN server is far better. Use public ones for internet reach, or when there's nothing else to hand.

For the multi-adapter load test, the Load Test tab keeps a pool: filter by region or search, tick the servers you're willing to use, and press "Give each adapter its own server". Every adapter gets a distinct host so they don't queue behind each other, and extra adapters get separate ports when a host offers a range.

Public servers go down and get busy, so the run fails over rather than failing. Before testing against a server, each adapter opens a quick TCP connection to its port through its own address; a server that refuses or stays silent is written off within about three seconds, for every adapter at once, and the next unused server is taken instead. A server busy with someone else's test is skipped the same way, though its other ports stay available. "Tick a few" ticks two spares beyond one per adapter for exactly this, and "Check which are up" probes every ticked server in advance, marking each up with its latency or down with the reason, so dead ones are pushed to the back of the queue. Progress streams into the results box as it happens, and the summary lists any servers that were written off. The assigned server shows in the adapter table and in the results.

## RF Explorer 6G Combo

- Install the Silicon Labs CP210x USB-to-UART driver if no COM port appears.
- The default baud rate is 500000; it must match the setting in the device menu (500000 or 2400).
- The 2.4 GHz range is captured on the WSUB3G module (right SMA, 15–2700 MHz) and 5 GHz on the 6G module (left SMA, 4850–6100 MHz). The app switches modules automatically based on the reported frequency limits of the active module.
- Only the bottom ~175 MHz of the 6 GHz band (5925–6100 MHz) is within range.

**Verify the protocol.** `RF/RfExplorerDevice.cs` implements the commands and data formats as I understand them from RF Explorer's UART API: `#C2-F:` config lines, `$S` sweep frames (dBm = −byte/2), `C2-F:` to set the range, and `CM` + 0/1 to switch modules. Check the byte formats against RF Explorer's published API document with your firmware version. Newer firmware can also send larger sweeps in a `$z` frame that isn't handled yet. If the Devices tab says the port opened but the device isn't responding, check the baud rate first.

**About the noise floor.** Windows doesn't report a noise floor, so SNR and noise heatmaps come from the analyzer. The app takes the 10th percentile of the min-hold trace and converts it from the analyzer's resolution bandwidth to a 20 MHz channel. For example, −105 dBm in a 600 kHz RBW becomes about −90 dBm per 20 MHz. That is close to the analyzer's own sensitivity limit, so in quiet environments the measured floor is set by the instrument and the SNR shown is conservative. Laptop RSSI and analyzer power also come from different antennas, and the RF Explorer's amplitude accuracy is ±3 dB (6G) and ±6 dB (WSUB3G), so treat SNR as an indicator rather than a calibrated figure.

## 802.11 capture

Live capture is not supported on Windows, and it will not be added. Use the Linux app, which captures in monitor mode through `iw` and libpcap.

The Windows Capture tab can still open a `.pcap` or `.pcapng` made somewhere else and attach its summary to a survey point. That is the whole of the feature.

## Extra Wi-Fi adapters

Any USB WLAN adapter Windows recognises shows up automatically on the Load Test tab, and its scan results merge into the Discover and Survey data.

**A 6 GHz adapter (for example the Alfa AWUS036AXML, MT7921AUN) is the most useful addition.** The RF Explorer stops at 6100 MHz, so scan data is the only way to see most of the 6 GHz band, and many laptops can't see it at all. With one plugged in, the 6 GHz channel map on the Discover tab fills in and 6 GHz networks appear in survey measurements. Note that spectrum-derived figures (noise floor, signal-to-noise, occupancy) aren't available above 6100 MHz, so 6 GHz points get coverage and rate data only.

**Frame capture for this adapter is on Linux.** The in-kernel mt7921u driver does monitor mode there. This Windows app does not.

**Small 1x1 adapters (such as the EDUP AC600) are load generators and witnesses, not speed meters.** They cap around 433 Mbps PHY on USB 2.0, so an iperf3 result from one measures the adapter rather than the access point. Useful cautions when running several:

- Give each its own iperf3 server port: `iperf3 -s -p 5201`, `-p 5202`, and so on.
- Several adapters on one machine share USB bandwidth and CPU, sit centimetres apart, and report from one position, so they aren't equivalent to clients spread around a room.
- USB 3.0 ports emit noise across 2.4 GHz. Use USB 2.0 ports or shielded extension cables, spread the adapters out, and keep them away from the RF Explorer's antenna.

## GPS

Any receiver that appears as a COM port and outputs NMEA 0183 works, from a cheap dongle to an RTK receiver such as a u-blox ZED-F9P. "Find GPS port" probes each port at 9600, 4800, 38400, and 115200 baud, skipping the RF Explorer's port. The app reads GGA sentences for position, fix quality, satellite count, and HDOP.

A fix counts as good (green) when HDOP is 2 or lower with at least 5 satellites, or when the reported accuracy is 5 m or better. Only good fixes trigger automatic measurement. Indoors, the dongle will usually be orange or red; click to place points as usual, and any fix that exists is still stored with the point.

To swap in a better receiver later, choose its COM port and start it. Nothing else changes. RTK fix types (4 and 5) are recorded and exported. "Windows Location" uses the built-in GNSS on tablets, otherwise Wi-Fi/IP positioning, which is too coarse for placing points.

## Touch screens

The survey map is built for fingers as well as a mouse:

- **One finger drags the map**, with a flick carrying on and settling naturally.
- **Pinch zooms**, keeping whatever is between your fingers in place. Ctrl and the mouse wheel do the same thing with a pointer.
- **Tap to measure**: a touch that doesn't travel far or linger counts as a tap and places a point, so panning never drops measurements by accident.
- **Full screen** hands the whole window to the map, with a floating bar for zoom, fit, and exit. Escape leaves it too.
- **"Drag to edit"** switches one finger from panning to drawing: use it for walls, dragging wall ends, and selecting points. Pinch still zooms while it's on.

## Linux

`linux/` holds a separate GTK4 application written for Linux rather than ported to it: same
job, native tools. See `linux/README.md`. It shares no code with this one deliberately.

The two keep their own survey formats, but surveys move between them: the Linux app opens a
Windows `.wsurvey` directly, and its menu has **Save a copy for the Windows app**, which
writes this app's format. Windows-only data (GPS pins, walk tracks, iperf3 results,
per-adapter links) survives a round trip through Linux untouched.

`archive/` holds an earlier C# command-line tool and Avalonia GUI for Linux. They don't build
(the platform project they depend on was never written) and are kept only for reference.

## Setup and about tab

A **Setup & About** tab checks what this machine has and fills the gaps it can:

- Wi-Fi adapters, Windows location access, iperf3, the Silicon Labs CP210x driver for the RF Explorer, a GPS receiver, the Alfa AWUS036ACM and AWUS036AXML, the vendor database, winget, and whether Windows is waiting on a restart.
- Devices present but without a working driver are reported as such, using the Windows device error code, so "plugged in but broken" doesn't look like "not plugged in".
- Tick the agreement box and it installs what can be installed unattended through winget with the vendor's own switches (`--silent --accept-package-agreements`), and downloads `oui.csv` directly. "Install what's missing" runs the lot.

Two deliberate limits. It won't click through a vendor's licence screen on your behalf, so where there's no unattended installer (the Silicon Labs and MediaTek driver downloads) the button opens their page for you to accept yourself. And it won't restart the machine on its own: if a driver needs one, the restart row asks, then gives you a minute to cancel with `shutdown /a`.

Both Alfa adapters use MediaTek chipsets. Windows Update often has the MT7612U driver for the **AWUS036ACM**, so plug it in, wait, and press Check again. Windows has no driver for the **AWUS036AXML**, so its row downloads Alfa's own Wi-Fi and Bluetooth installers from files.alfa.com.tw and starts each in turn, exactly as double-clicking them would. Work through their windows as usual, including the administrator prompt; the app waits for one to close before starting the next, keeps the downloads in `%TEMP%\WifiSurveyor-drivers` so you can run them again, and afterwards says whether Windows sees a working driver. These drivers usually want a restart. Nothing from Asus is involved.

## Licences

Help → About lists every component, its licence, and how it's used, and shows the bundled iperf3 licence text. The same information is in `THIRD-PARTY-NOTICES.md`, which is copied into the build output.

Two things worth knowing up front:

- **iperf3 is bundled only if you fetch it**, with the licence considerations above.
- **Npcap is not required.** Live capture is not part of this app. Some leftover code can still load `wpcap.dll` if Npcap happens to be installed; do not ship Npcap with the app. See `THIRD-PARTY-NOTICES.md`.

## Project layout

```
src/WifiSurveyor.Core/      platform-neutral code (net8.0)
  Models/        data types saved in projects
  Capture/       pcap/pcapng reading, Radiotap and 802.11 parsing, capture analysis
  RF/            RF Explorer driver and spectrum maths
  Positioning/   NMEA parsing, georeferencing
  Surveying/     diagnosis, heatmaps, source location, walls, report, save/export
  Wifi/          channel tables
src/WifiSurveyor/           the WPF application (Windows only)
  Wifi/          scanning and the current connection (wlanapi P/Invoke)
  Capture/       reading a pcap file; live monitor mode is not supported
  Surveying/     survey engine, iperf3 load tests, walk-test recorder
  Services/      settings, installers, dependency checks
  Views/         the tabs and chart drawing helpers
linux/                      the GTK4 application for Linux (Python)
archive/                    unmaintained projects that don't build
```

Settings are stored in `%LOCALAPPDATA%\WifiSurveyor\settings.json`.

## Not in the Windows app

Live 802.11 capture is out of scope, not a to-do. The Linux app already covers frame capture, predictive planning ("what if an access point went here"), area-weighted scores, and a full US 6 GHz sweep when a tinySA Ultra+ is connected.

Still open on Windows:

- PDF reports.
- Predictive planning (virtual access points with wall attenuation).
- 6 GHz in survey spectrum captures and heatmaps. The RF Explorer only reaches 6100 MHz, so most of the band is scan-only.
- Predicted coverage drawn from an estimated access point position.
- Bearing-based interference hunting with a directional antenna.
- Area-weighted scores (they currently count points, not floor area).
- Multiple floors per project.
