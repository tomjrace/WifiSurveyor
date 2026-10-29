# Wi-Fi Surveyor

Wi-Fi Surveyor is a Linux app for finding out what's really happening on a wireless network.
Walk a building with a tablet and it maps coverage, interference and capacity over the floor
plan, then writes a report that says what to fix and where. Leave it watching one network and
it catches every dropout with its most likely cause. Plug in a spectrum analyzer or a
software-defined radio and it sees the energy a Wi-Fi card can't: microwave ovens, Bluetooth,
video senders, radar, wireless mics. Tom Race and AI tools vibe coded it.

It's a GTK4 app built for a touchscreen first, written for Linux rather than ported to it: `iw`
and NetworkManager for scanning and monitor mode, libpcap through Scapy for 802.11 frames,
NumPy for the maths, Cairo for drawing. It needs no accounts, no API keys and no cloud.

The app lives in [`WifiSurveyor-Linux/`](WifiSurveyor-Linux/). Its
[full manual](WifiSurveyor-Linux/README.md) covers every page and setting in detail.

## What it does

**Survey.** Load a floor plan (or place the building from a map, which sets the scale for you)
and tap where you're standing to measure. Each point takes an averaged Wi-Fi scan, the current
link, a connection check (latency, jitter, loss, DNS), and, with an analyzer connected, sweeps of
2.4, 5 and 6 GHz plus a close look at the busiest channels. The heatmap shows signal, signal to
noise, noise floor, overlapping access points, channel occupancy, roaming coverage, co-channel
contention, latency, throughput, or a verdict map that names what limits each spot. Judge the
site against Data, Voice, Video or Location requirements, plan new access points and see the
predicted coverage, and compare against an earlier survey of the same site to see what changed.

**Reports.** One self-contained HTML file: what to improve, grouped by theme and placed on the
plan, the maps, the access points, and every point's findings and spectrum, with tables you can
sort by any column. A suggested channel plan changes as few radios as it can and lists each
change as its own step: which access point, where to find it, and what moving it fixes. An
overview version gives whoever makes the changes just the to-do list and maps.

**Watch.** Stays with one network for hours or days, drawn like a patient monitor: a heartbeat
per check, beacons, channel use and the access point's load, with the vital signs beside them.
It watches both bands of a network at once with separate adapters, stacked on one time axis, so
you can see whether a drop hit one radio or the whole unit. Each incident is diagnosed from the
evidence: the access point rebooting, one radio restarting, forged deauthentications, a channel
change, interference, the gateway, DHCP, DNS or the internet behind it, or just this computer's
adapter. Red and amber are kept for what someone using the network would notice. It keeps the
computer awake while it watches, and if the computer sleeps anyway it pauses cleanly and picks up
on waking.

**Discover.** Nearby access points with their manufacturers, refreshed every few seconds.

**Spectrum.** The live spectrum of every US Wi-Fi band at once, with each access point drawn
across its channel and a waterfall underneath, or any other band: wireless mics and IEMs,
production, industrial and sub-GHz, business radio, venue services. **Record** keeps it running
overnight and shows what came and went.

**Frequencies.** Scans whole groups of bands and grades each, coordinates wireless mic and IEM
frequencies clear of TV stations and intermodulation, and runs link tests with a tinySA on a
Raspberry Pi elsewhere on the network.

**Capture.** Puts an adapter into monitor mode and reports what the frames say: retry rate,
airtime measured from frame durations, the clients heard, and each access point's real
channel width, generation, security and its own BSS Load figure. It also opens `.pcap` files
from anywhere else.

**Setup.** Finds what's plugged in, lines adapters up with each other, calibrates the radios
and updates their firmware.

## Hardware

Everything is optional except a Wi-Fi adapter, and any mix works. Devices are found when
they're plugged in, and several of a kind share the work.

| Device | What it adds |
|---|---|
| Any Wi-Fi adapter | Scans and connection checks. The computer's own is enough to start |
| A monitor-mode adapter (Alfa AWUS036ACM or AWUS036AXML) | Frame capture, and on the Watch page every beacon and deauthentication on the channel |
| tinySA Ultra / Ultra+ | Spectrum to 5.3 or 7.3 GHz: noise floor, interference and channel use. The Ultra+ covers all of US 6 GHz |
| RF Explorer (6G Combo and others) | Spectrum to 6.1 GHz |
| HackRF One | Names what's on a channel (Wi-Fi, Bluetooth, Zigbee, microwave ovens, carriers, radar), watches DFS channels for radar, measures cellular coverage, and sends a test tone to measure walls |
| RTL-SDR | Bands below 1.8 GHz: wireless mics, sub-GHz industrial, business radio |
| GPS (gpsd or NMEA serial) | Position outdoors |
| Raspberry Pi with a tinySA | A "satellite" that sends test signals for link tests |

## Installing

Ubuntu 24.04 or newer (or another distribution with GTK 4 and libadwaita 1.5 or later) and
Python 3.10 or newer.

```
sudo apt install git python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    iw iproute2 libpcap0.8
git clone https://github.com/tomjrace/WifiSurveyor.git
cd WifiSurveyor/WifiSurveyor-Linux
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
.venv/bin/wifisurveyor
```

For the radios: `sudo apt install hackrf rtl-sdr dfu-util`. Fedora and Arch packages, and the
details of each device, are in the [manual](WifiSurveyor-Linux/README.md#installing).

Scanning works as a normal user. Monitor mode, frame capture, true dBm readings and joining
networks for checks need root: use **Relaunch as root** on the start screen or in the menu, or
start it as root yourself, telling it where your desktop's display is:

```
sudo env WAYLAND_DISPLAY=$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY .venv/bin/wifisurveyor
```

## Getting started

The app opens on a start screen:

- **New survey** asks for the site's name and what to measure (a quick coverage check, a
  thorough interference and capacity survey, a live event, a warehouse, a security sweep), then
  opens the map. Load the floor plan and tap where you stand.
- **Watch a network** opens the Watch page. Pick the network; if it's on two bands, the second
  is offered with its adapters already chosen. Press **Start watching** and leave it.
- Recent surveys are listed underneath, along with any unsaved work to recover.

## Your data stays yours

Wi-Fi Surveyor never sends anything about you, and keeps everything it measures on your
computer, in your own folders, even when it runs as root:

- surveys and reports in `~/Documents/Wi-Fi Surveys/`
- settings, Watch sessions, captures and recordings in `~/.local/share/wifisurveyor/`

Nothing is written inside the app's folder, so a copy of the code carries no scan results. Wi-Fi
passwords stay in NetworkManager; the app never reads them. The few web services it can use
(maps, manufacturer lists, firmware downloads) are each listed in
[PRIVACY.md](WifiSurveyor-Linux/PRIVACY.md) with exactly what they receive, and each is used
only when you press its button.

## In this repository

```
WifiSurveyor-Linux/    the app (Python, GTK4): its manual, tests and the satellite agent
src/                   an earlier, separate Windows version (WPF), see src/README.md
archive/               unmaintained experiments that don't build
tools/                 helpers for the Windows version
```

## Tests

```
cd WifiSurveyor-Linux
.venv/bin/python -m unittest discover -s tests
```

About 390 tests, which run without any hardware. They include simulated tinySA and RF Explorer
analyzers on pseudo-terminals, stand-in HackRF and RTL-SDR tools, and Watch sessions played out
on a fake clock.

## Licence

MIT, copyright Tom Race. See [LICENSE](LICENSE). Nothing third-party is bundled; Scapy, NumPy,
PyGObject and pyserial come from Python packaging and the system tools from your distribution.
See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
