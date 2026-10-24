# Privacy

Wi-Fi Surveyor never tells anyone who you are. It has no accounts, no analytics, no
telemetry and no API keys, and it never sends your name, email address or anything from
*Report details* anywhere. Surveys, reports, settings and recordings stay on this machine
unless you copy them somewhere yourself, always in the home folder of the person running
the app, owned by them, even when it runs as root:

- surveys and reports: `~/Documents/Wi-Fi Surveys/`
- settings, Watch sessions, frame captures, I/Q recordings and downloaded lists:
  `~/.local/share/wifisurveyor/`

None of it is ever written inside the app's own folder, so a copy of the code (a git
clone, a zip) carries no scan results, captures or passwords. Wi-Fi passwords stay in
NetworkManager; the app never reads or stores them.

## How that's enforced

Every web request the app makes goes through one module, `wifisurveyor/net/outbound.py`.
It:

- identifies the app only as `WifiSurveyor/<version>`. Code elsewhere can't change that
  or add identifying headers (`From`, cookies, logins, referrers);
- refuses, before anything is sent, any request whose address, headers or body contain an
  email address, or the surveyor or contact from *Report details*;
- contacts only the hosts listed below, and this network's own addresses (satellites). A
  redirect to anywhere else is refused.

Tests check that no other code opens web requests, that the app contains no email
addresses, and that this list matches the one in the code. A new service has to be added
to both, saying what it receives, before the app can reach it.

## What goes where, and when

Nothing below happens in the background: each runs when you press the button for it.

| Host | Run by | What the app sends |
|---|---|---|
| `tile.openstreetmap.org` | OpenStreetMap Foundation | map tile numbers (the area shown) |
| `nominatim.openstreetmap.org` | OpenStreetMap Foundation | the address typed into Site map |
| `overpass-api.de` | FOSSGIS e.V. | a map position and radius, for building outlines |
| `overpass.kumi.systems` | Kumi Systems | a map position and radius (if overpass-api.de fails) |
| `overpass.private.coffee` | private.coffee | a map position and radius (if the others fail) |
| `basemap.nationalmap.gov` | US Geological Survey | map tile numbers (the area shown) |
| `api.beacondb.net` | BeaconDB | nearby access points' MAC addresses and signal levels, only after asking |
| `standards-oui.ieee.org` | IEEE | nothing: downloads the manufacturer registry |
| `export.iperf3serverlist.net` | iperf3serverlist.net | nothing: downloads the server list |
| `api.github.com` | GitHub | nothing: looks up the newest HackRF and RTL-SDR releases |
| `github.com` | GitHub | nothing: downloads a HackRF or RTL-SDR release |
| `codeload.github.com` | GitHub | nothing: GitHub's source downloads |
| `objects.githubusercontent.com` | GitHub | nothing: GitHub's release downloads |
| `release-assets.githubusercontent.com` | GitHub | nothing: GitHub's release downloads |
| `athome.kaashoek.com` | the tinySA's author | nothing: tinySA firmware list and download |
| `version.rf-explorer.com` | RF Explorer | nothing: downloads RF Explorer's Uploader |

Like any web request, each of these services sees this machine's public IP address.

## Traffic that stays on your network or is your choice

- **Wi-Fi scans** send probe requests over the air, as any Wi-Fi device does; as root the
  app listens passively where it can.
- **The connection check** pings the gateway and looks up a DNS name through the
  network's own resolver.
- **Monitor** does the same while it runs, every few seconds, and also pings three public DNS
  resolvers by address (`1.1.1.1`, `9.9.9.9` and `8.8.8.8`) to see whether the internet
  answers: an ICMP echo, which carries nothing about you.
- **Throughput tests** run iperf3 against the server you type in, or one you pick from
  the public list.
- **Satellites** are found by a UDP broadcast on this network (port 8737) and driven over
  HTTP on it, every command signed with the pairing code.
- **Installing updates** runs `apt-get` (your distribution's mirrors) and, for the RF
  Explorer, its own Uploader, which talks to RF Explorer's servers itself.
- **Bluetooth and finder-tag scans** only listen.
