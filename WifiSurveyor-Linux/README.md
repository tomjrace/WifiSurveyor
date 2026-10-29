# Wi-Fi Surveyor for Linux

A GTK4 application for Wi-Fi site surveys: heatmaps over a floor plan, spectrum analysis with
an RF Explorer or a tinySA Ultra/Ultra+, 802.11 capture in monitor mode, and GPS.

This is a Linux program, not a port. It uses what Linux already provides rather than carrying
Windows-shaped abstractions: `iw` and NetworkManager for scanning and monitor mode, libpcap
through Scapy for 802.11, gpsd or a plain NMEA serial port for position, NumPy for the
heatmap maths, and Cairo for drawing. Monitor mode here is a property of the interface, so
adapters that fight you on Windows, the Alfa AWUS036ACM and AWUS036AXML among them, simply
work.

**Privacy:** no accounts, telemetry or API keys, and it never sends anything about you. Every
web request goes through one checked door; [PRIVACY.md](PRIVACY.md) lists each service it can
contact and exactly what that service receives.

## Installing

```
sudo apt install git python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    iw iproute2 libpcap0.8
git clone https://github.com/tomjrace/WifiSurveyor.git
cd WifiSurveyor/WifiSurveyor-Linux
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
.venv/bin/wifisurveyor
```

The virtual environment keeps the app's Python packages apart from the system's (current
Ubuntu refuses `pip install --user`), and `--system-site-packages` lets it use the GTK
bindings apt installed.

Fedora: `sudo dnf install python3-gobject gtk4 libadwaita iw iproute libpcap`.
Arch: `sudo pacman -S python-gobject gtk4 libadwaita iw iproute2 libpcap`.

Needs libadwaita 1.5 or later (Ubuntu 24.04, Fedora 40, and newer).

Optional, for the radios beyond the spectrum analyzer: `sudo apt install hackrf rtl-sdr
dfu-util` (HackRF, RTL-SDR, and flashing tinySA firmware).

Scanning works unprivileged through NetworkManager, but NetworkManager only reports a 0–100
quality, which the app maps onto dBm as an estimate. Every point records which it got, and
the status line says so when a measurement is estimated. For true dBm, and for monitor mode
and capture, run as root, or use the orange **Relaunch as root** button (in the header, and on the
start screen; it asks you to save first):

```
sudo env WAYLAND_DISPLAY=$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY .venv/bin/wifisurveyor
```

(`sudo` drops the variables that say where the desktop's display is, so it's passed on
explicitly; *Relaunch as root* does the same.)

### The spectrum analyzer: RF Explorer or tinySA

Two kinds are supported: the RF Explorer (6G Combo and friends) and the tinySA Ultra family
(ZS405 Ultra, ZS406 and ZS407 Ultra+). Plug one in and the app finds it: every couple of
seconds it checks the USB serial ports, and when one appears it asks each port that isn't the
GPS whether it's an analyzer. A tinySA shows up as `/dev/ttyACM*` (USB id 0483:5740) and is
asked first there; elsewhere the RF Explorer question comes first, at 500000 baud and then
2400. The one that answers is connected, selected on the Setup and Spectrum tabs, and a toast
says which it is and which port it's on. Unplug it and the app notices, says so, and waits for
it to come back.

**How far each reaches.** The RF Explorer 6G Combo stops at 6100 MHz, so it sees only the
bottom of 6 GHz (channels 1–29). A tinySA Ultra+ ZS407 reaches 7.3 GHz, so with one connected
the survey and the Spectrum tab sweep **all of US 6 GHz** (5925–7125 MHz) and 6 GHz points get
a measured noise floor and SNR too. The app asks the tinySA how far it goes when it connects
(it asks for a 12 GHz stop and reads back where the firmware clamps it), and Setup shows the
answer. A ZS406 tops out at 5.4 GHz, so it covers 2.4 GHz and UNII-1/2A but not upper 5 GHz;
those ranges are reported as out of its reach rather than drawn as quiet.

**How the tinySA is driven.** It's a text shell over USB rather than a streaming device, so
the app keeps running `scan` on whatever range it's tuned to: 450 points per sweep, with the
resolution bandwidth set to the narrowest of 3/10/30/100/300/600/850 kHz that covers the point
spacing (300 kHz across 2.4 GHz, 100 kHz while watching a single channel). Ranges wider than
500 MHz are swept in pieces, so all of 6 GHz is three. It switches the tinySA into its ultra
mode and low input, which Wi-Fi frequencies need, and leaves its own sweep settings as found.

Serial ports belong to the `dialout` group, and most desktop users aren't in it, so the first
time the app will probably say it can't open the port. Press **Grant serial access** on the
Setup tab: after one password prompt it installs a udev rule
(`/etc/udev/rules.d/70-wifisurveyor-serial.rules`) that gives the logged-in user that USB
serial device, straight away and every time it's plugged in. Nothing else changes. The
alternative is `sudo usermod -aG dialout $USER` and logging in again.

### The HackRF One

A HackRF One adds what a swept analyzer can't do. It's optional and works alongside the
RF Explorer or tinySA. Install the tools with `sudo apt install hackrf`; the package's udev rule
also lets you open the device without root. Plug it in and the app finds it (USB id 1d50:6089),
like the analyzer, and **Setup → HackRF** says what it is and how far it tunes. It's
specified for 1–6000 MHz. Recent hackrf tools tune it to 7250 MHz, where it's much less
sensitive, and the app checks which kind of tools you have.

- **Naming what's on a channel.** At each point (*Name what's on the busy channels* under
  Measuring, on by default), the HackRF records a quarter of a second of raw I/Q at 20 MS/s on
  each of the busiest channels and on any unexplained energy the sweep found. It sees every
  transmission whole: its width, its length, and when the next one comes. From that it names
  Wi-Fi (including networks too weak for the card to list), Bluetooth (narrow and hopping),
  Zigbee/Thread (2 MHz wide on an 802.15.4 channel), microwave ovens (on and off with the
  mains), carriers and video senders that never stop, and radar pulse trains. It also gives
  the channel's true airtime, which a sweep undercounts. The noise-source markers, the
  "non-Wi-Fi interference" verdict, each point's improvements and the report use these
  names, each with a fix suited to what it is. **Setup → What's on a channel** does the same
  on demand.
- **Radar on the DFS channels.** *Review → Radar watch* listens on each DFS channel (52–64,
  100–144) in turn for the minutes you choose, weather-radar channels 120–128 first, looking
  for pulses a few µs long at an exact interval. Wi-Fi frames are never that short. Whatever
  it hears is kept with the survey. The configuration audit flags any radio on those
  channels, the channel plan steers away from them, and the report lists them. A quiet watch
  is reported as quiet, not as proof: radar that sweeps past rarely can still be missed.
  Stopping the watch, or the HackRF failing partway, keeps what was heard until then. Channels
  where the HackRF overloaded, or where the computer fell behind its 20 MS/s and lost samples,
  are marked less certain in the report.
- **Cellular coverage.** *Measure cellular bands* (under Measuring, off by default) sweeps
  every US cellular downlink band in one pass, plus 900 MHz ISM (Wi-Fi HaLow, LoRa) and DECT 6.0,
  and records the strongest 5 MHz in each. The *Cellular downlink* map shows the strongest
  band at each spot, or one band you choose. As a rough guide, −65 dBm per 5 MHz is about what a
  phone reports as RSRP −90. The report's cellular section says where phones will struggle.
  It can't tell carriers apart.
- **Measuring walls with a test tone.** *Planning → Test tone* marks where a HackRF sends a
  steady carrier (2477 MHz, above channel 11, or 5732 MHz, between channels 144 and 149). Start
  the tone from **Setup → HackRF → Test tone** on the machine the HackRF is plugged into. That's
  usually a second laptop or a Raspberry Pi with the app, since the HackRF sending can't also
  listen. Each point you measure then records the tone's level with the analyzer, and the
  *Test tone level* map shows it fading through the building. *Wall losses from the tone → Fit*
  solves every marked wall's loss and the path-loss exponent together by least squares, and
  *Apply* sets the walls to them, so predictions and source locations use measured walls rather
  than guessed ones. The tone stays in the unlicensed ISM bands at a low level, never uses the
  HackRF's amplifier, and turns itself off after 30 minutes. With **Setup → Surveying → Ask
  what each tap is**, tapping the plan asks whether it's a scanning point or where the tone
  generator sits, and placing the generator shows how to set the tone up with whatever is
  connected (HackRF, analyzer, both, or neither), with the command to copy. **Easy mode** turns
  that on and adds a guide across the top of the map: place the generator, start the tone,
  measure behind each wall, fit. Each point also says straight away whether it heard the tone.
  While the tone is on, a banner across the top of every tab counts down to when it turns
  itself off and has a button to stop it. If the tone stops by itself (the HackRF dropped off
  USB, say), the app says why, and sweeping picks up again where it was.
- **Fast sweeps.** *Sweep with the HackRF* (Setup) makes the HackRF the sweeping analyzer,
  for sweeps fast enough to catch short bursts. It's off by default when an RF Explorer or
  tinySA is connected, since those read true dBm and the tinySA Ultra+ covers all of 6 GHz.
  With nothing else connected, the HackRF sweeps anyway, and the Spectrum tab lists it with the
  serial ports.

The HackRF reads levels relative to its own full scale, not dBm. **Calibration** turns them
into dBm: *Match the Wi-Fi card* compares its readings with the card's on strong networks, and
*Match the analyzer* compares them with an RF Explorer or tinySA. Its response differs across
frequency, so 2.4 and 5 GHz each get their own offset when there are two channels to work it
out from; everything else (cellular, 6 GHz) uses the overall one. Until it's calibrated, every
measurement it makes is marked as an estimate, here and in the report.

LNA and VGA gain are set in Setup too, and take effect straight away, even mid-sweep. The
calibration remembers the gains it was made at and follows any change (8 dB more LNA gain takes
8 dB off the offset), though calibrating again is surer. Lower them if a nearby access point
overloads it: whenever more than 0.1% of a capture sits at full scale, the point's notes and
Setup say so, since overload can show signals that aren't there. *Antenna power (bias tee)*
puts 3.3 V on the antenna port for an active antenna or inline amplifier; it's always off while
sending the tone.

**Antenna check.** Many HackRF antennas only cover 2.4 GHz. When the card hears a strong network
in a band and the HackRF hears none of them above its floor, calibration leaves that band out
and says why, the point's notes say so when the HackRF did the sweeping, and Setup shows it
until the band is heard properly again.

**Raw I/Q recordings.** When the HackRF hears something it can't name, or radar, it keeps the
raw I/Q as a SigMF recording (ci8, with what it heard as annotations) in
`~/.local/share/wifisurveyor/iq/`, for a closer look in inspectrum or GNU Radio. For radar it
keeps the ~100 ms with the most pulses. The oldest recordings are deleted past 1 GB, and
**Setup → HackRF → Keep raw I/Q** turns it off.

### The RTL-SDR

An RTL-SDR dongle (RTL2832U with an R820T/R828D tuner, 24-1766 MHz; an RTL-SDR Blog V4 reaches
down to 0.5 MHz) is found by itself when it's plugged in, through the `rtl-sdr` package's
`rtl_power` and `rtl_eeprom`. The tools take the dongle from the kernel's DVB-T driver while
they run, so nothing needs blacklisting. It can't reach Wi-Fi, so it never sweeps Wi-Fi: it
takes the bands below 1.8 GHz (wireless mics and IEMs, sub-GHz industrial, business radio),
which leaves the tinySA, RF Explorer and HackRF free for the rest. It appears in the Spectrum
page's device list like the HackRF.

Like the HackRF, its levels are relative until it's calibrated. **Setup → RTL-SDR** has a fixed
gain (automatic gain would make readings incomparable), a frequency correction in ppm (cheap
dongles' clocks are off by up to 50 ppm, which is 25 kHz at 500 MHz and matters for mic
coordination), and **Match the analyzer**, which sweeps FM broadcast with a tinySA or RF
Explorer (or a calibrated HackRF) and sets the RTL-SDR's offset from the strong stations. The
offset is kept per dongle with the gain it was set at, so changing the gain moves it to match.
Mic coordination doesn't depend on calibration: it compares each frequency with the rest of
the same scan.

Many RTL-SDR Blog V3s report the same generic USB strings as any dongle ("Realtek
RTL2838UHIDIR", serial 00000001), so **Setup → RTL-SDR → Model** says what it is. A V3 works with
Ubuntu's `rtl-sdr` as it is, and its TCXO means the ppm correction can stay at 0. Its antenna
power (**Antenna power (bias tee)**, for an active antenna or an LNA) needs the RTL-SDR Blog
tools' `rtl_biast`: *Device software → RTL-SDR* offers to build them when the model is a Blog
V3 or V4 (a V4 needs them anyway).

**Reception check.** Each RTL-SDR is checked when it's opened, and again with *Test reception*:
a quarter-second capture at 100 MHz. A tuner whose PLL never locks has no local oscillator, so
almost nothing reaches its ADC whatever antenna is on it, and every band would look quiet. Such
a dongle is marked NOT RECEIVING in Setup and left out of band scans, mic coordination and
surveys, so it can't report a busy band as clear. Replugging into another port is worth one
try; past that the dongle is faulty. A dongle's own clock harmonics (multiples of 28.8 MHz,
such as 460.8 MHz) show up as narrow carriers even on a working one.

The kernel's DVB-T driver claims these dongles at boot. The rtl tools take them from it as they
start, but the usual advice (and what this machine now has) is to keep it off them entirely:
`/etc/modprobe.d/blacklist-rtlsdr.conf` with `blacklist dvb_usb_rtl28xxu`, `rtl2832_sdr`,
`rtl2832` and `r820t`.

### Beyond Wi-Fi: production, industrial and business bands

The bands are in `rf/bands.py`, each a US allocation with what uses it, whether it's
unlicensed, licensed, listen-only or to be kept clear, and the rule. They're grouped the way a
site would ask for them:

- **Wireless mics & IEMs**: the UHF TV band (470-608 MHz) and VHF TV band, the 600 MHz guard
  band (614-616) and duplex gap (653-657 licensed, 657-663 unlicensed), 900 MHz, 941-960 MHz,
  1435-1525 MHz, DECT, 2.4 and 5.8 GHz, and 6875-7125 MHz. Channel 37 (608-614, radio astronomy
  and medical telemetry) and the 600 MHz downlink (617-652, illegal for mics since July 2020)
  are listed as keep-out.
- **Production**: wireless intercom (DECT, 900 MHz, 2.4 GHz), wireless DMX, 5 GHz video links,
  IFB/cue (450-451 and 455-456 MHz), production two-way radio.
- **Sub-GHz & industrial**: 315/319.5/345/390/433.92 MHz remotes and sensors, 900 MHz ISM
  (LoRaWAN, Z-Wave, Zigbee 915, Wi-Fi HaLow, RFID, SCADA radios, meters), 217-220 MHz
  telemetry, 928 MHz licensed SCADA, 941-960 MHz links, 2.4 GHz (WirelessHART, ISA100.11a).
- **Business radio & paging**: VHF and UHF two-way, MURS, FRS/GMRS, 800 MHz public safety and
  SMR (and the in-building DAS that carries it), 900 MHz private LTE, paging.
- **Venue & public services**: assistive listening (72-76 and 216-217 MHz, which the ADA
  requires in venues), FM and TV broadcast, aviation, NOAA weather, hospital medical telemetry.

Where they show up:

- **Spectrum** has two checkbox menus, each with *All* at the top. *Bands* picks the sets:
  Wi-Fi, each group above, and cellular; ticking a set ticks its ranges. *Ranges* lists every
  range in the ticked sets (and a wide overview where one helps, such as 470-663 MHz for mics);
  what's ticked there is what's swept. The four Wi-Fi ranges alone give the familiar 2.4/5/6 GHz
  view. One range on its own can be zoomed. Several are drawn as panels side by side, in rows,
  with ranges close together sharing a panel (900 MHz ISM and 941-960 MHz, say). The choice is
  remembered. Every panel has its frequencies in MHz across the top, at round steps that get
  finer as you zoom (down to 0.1 MHz and below), with faint lines down the plot. Bands in view
  are labelled and coloured by licence (green unlicensed, amber licensed, red keep out, grey
  listen only), with TV channel numbers along the bottom.
- **Frequencies** is a new tab with four parts. *Band scan* sweeps every band in a group (or all
  of them), shared between every analyzer here at once, and lists for each the strongest signal,
  how far it stands over the band's floor, how much of the band is busy, a red/amber/green
  verdict and what to do about it; for TV bands, which channels carry stations and which are
  open. *Mic frequencies* coordinates wireless mics and IEMs (below). *Link test* and
  *Satellites* are below too.
- **Survey**: the bands a point measures are chosen in the survey setup (below). Each point
  sweeps the ticked ranges with whatever reaches each band, and records them with the point.
  The heatmap gains *Activity in other bands* (one band, or the busiest at each point), each
  point's facts in the report name its busiest other band, and the report gets an *Other bands*
  section: per band, the quietest and busiest readings, the points where it's busy, and what to
  improve, with TV stations and the channels open at every point.

### Survey setup

A new survey opens with **Survey setup**, and **Survey → Measuring → Survey setup → Edit…**
opens it any time. It says what each point measures, so a survey can be as thorough as the
site needs:

- **Survey type** fills in everything below it: *Quick Wi-Fi coverage*, *Standard Wi-Fi*,
  *Thorough Wi-Fi* (interference and capacity: longer sweeps and dwells, throughput, Bluetooth,
  cellular), *Live event or production* (wireless mics and IEMs, production and venue bands),
  *Warehouse or industrial* (sub-GHz and business radio, cellular), *Office or business*, and
  *Security sweep* (finder tags, Bluetooth, unknown transmitters). Changing anything makes it
  *Custom*.
- **Wi-Fi**: which spectrum ranges are swept (checkboxes with *All*: 2.4 GHz, both halves of
  5 GHz, 6 GHz), sweeps per range, channels watched closely and for how long, the connection
  check, joining the network, throughput.
- **Other radios**: Bluetooth, finder tags, the HackRF's channel identification, cellular.
- **Other bands**: band groups and the ranges in them, each a checkbox list with *All*, the way
  the Spectrum page has them.

It also says about how long a point will take with what's connected, and how many points an
hour that makes. The setup is saved with the survey, so reopening it, or starting a follow-up
of the same site, measures the same way, and the report's *How this survey was made* names the
survey type and what it measured. The rows on the survey page still change single settings;
the setup's summary follows them.

### Placing the plan on a map

**Survey → Site map…** finds the site on a map, lays the blueprint over its building, and takes
the plan's scale from the map, so there's no need to measure a known distance with *Set scale*.
A web map knows its own scale: at zoom z a pixel covers cos(latitude) × 40,075 km / (256 × 2^z).
A blueprint laid over its building takes that scale with it. (Checked against the Empire State
Building: fitted to OpenStreetMap's outline, the plan came out 425 × 198 ft; its footprint is
424 × 197 ft.)

- **Find the site** with the **Address** box, the **GPS** (Setup → GPS; indoors it may need a
  window), or **Nearby Wi-Fi**. Dragging the map past its edge fetches more around the new
  centre.
- **The building's outline comes with its address.** Type the street address (or the building's
  name) and press Find: the map centres on it and OpenStreetMap's outline of that building is
  downloaded and selected, its size on the map shown. If a blueprint is already loaded, it's
  fitted to the outline straight away. When OpenStreetMap has the address only as a point, the
  outline at that point is found among its buildings instead.
- **The map** is OpenStreetMap's (worldwide, to zoom 19) or **aerial** imagery from USGS The
  National Map (public domain, the US only; it's sharp to zoom 16 and enlarged beyond).
- **Buildings** draws OpenStreetMap's building outlines. This can take half a minute, as the
  public servers are busy at times.
- **Blueprint…** loads the plan image over the map. Drag it, pinch it to size and twist it to
  turn (or use the *Size*, *Turn* and *See-through* sliders). Tap its building and **Fit to
  building** lines it up by itself: its longer side along the building's length, sized to span
  it.
- **Use blueprint** makes the blueprint the survey's plan with its scale set. **Use map** makes
  the map itself the plan (with the blueprint on it, if placed), with its scale and the map's
  credit. Either way the survey keeps where it is on the Earth (centre, turn, scale, how it was
  found), for its follow-ups too.

**No API keys.** Google's geolocation and Maps APIs both need a key, so they aren't used.
Everything here is a free public service without an account, used only when you press its
button and within its usage policy: OpenStreetMap tiles (identified, and kept on disk for 30
days so a map is never fetched twice), Nominatim for addresses, Overpass for buildings, USGS
for imagery, and **BeaconDB** (beacondb.net, the community successor to Mozilla's Location
Service) for Wi-Fi positioning. Nearby Wi-Fi asks before it first sends anything: it sends the
MAC addresses and levels of the access points heard, never their names, and leaves out
networks named `…_nomap` and randomised addresses. BeaconDB's coverage depends on its
contributors, so it may not know a site yet; the GPS and the address always work. Blueprints
need to be images (PNG, JPEG, SVG…); export a PDF page as an image first.

### Wireless mic and IEM coordination

**Frequencies → Mic frequencies** scans the equipment's tuning range at 25 kHz resolution,
peak-held over several passes, and picks frequencies that are quiet (within 6 dB of the floor
across the transmitter's bandwidth), inside US wireless mic spectrum (the licensed parts only
when *I hold a Part 74 licence* is on), not on a TV channel carrying a station, spaced from
each other, and clear of the set's two-transmitter 3rd and 5th order and three-transmitter 3rd
order intermodulation products. Frequencies already in use can be typed in: the picks steer
clear of them and their products, and **Check in use** tests them against the scan, naming
each problem (too close to another, on an intermod product, on a TV station, outside mic
spectrum, needs a licence, signal on it).

The equipment list has Shure's common US bands (ULX-D/QLX-D G50, H50, J50A; SLX-D G58, H55,
J52; Axient Digital G57 and X55; 900 MHz), the whole UHF and VHF TV bands, the duplex gap, and
a custom range. The spacing profiles (analog, digital, digital high-density, IEM) are typical
starting points, not any manufacturer's figures: check them against Wireless Workbench or WSM
before the show. Scan where the receivers will be, with everything that will be on during the
event switched on.

### Satellites and the link test

A **satellite** is a tinySA on a Raspberry Pi (a Pi Zero 2 W is plenty) elsewhere on the
network, which sends test signals when the app asks. Copy the `satellite` folder to the Pi and
run `sh install.sh` as the normal user: it installs python3-serial, gives serial access, sets
up a service that starts at boot, opens port 8737 if ufw is on, and prints a pairing code. In
**Frequencies → Satellites**, *Find* lists satellites on the network (or type an address), and
*Pair* takes the code. `python3 wifisurveyor_satellite.py --fake` runs one with a pretend tinySA
to try it out.

Commands go over HTTP, each signed with an HMAC of the pairing code with a timestamp and a
nonce, so only a paired tablet can make it transmit and a recorded command can't be replayed.
Being found (a UDP broadcast) needs no code and reveals only the name and what's attached.

What may be sent is enforced by the app and again by the satellite (`rf/txrules.py` and its copy
in the agent, kept equal by a test): over the air only the US ISM bands (902-928 MHz,
2400-2483.5 MHz, 5725-5850 MHz) at -20 dBm or less into the antenna; any other frequency, the TV
band included, only with **Cabled** on, meaning the generator goes into the receiver through a
cable and attenuator (or a shielded box), at 0 dBm or less. A signal stops by itself after 30
minutes, and a satellite also stops when the app hasn't checked in for 90 seconds, so a tablet
that walks out of range doesn't leave it transmitting.

**Frequencies → Link test** has a generator (a tinySA plugged into this tablet, or a satellite)
send at each of a range of frequencies in turn while an analyzer here (any other one that
reaches them) parks on each and measures it: the level heard, the floor beside it, the margin,
and the path loss. Over the air it shows how a 900 MHz or 2.4 GHz link fares between two
places; cabled, it checks a receiver or an analyzer's calibration across any band. *Steady
signal* leaves the generator on one frequency while you walk with the Spectrum page.

### Updating the radios

**Setup → Device software** has *Check* for each kind of radio. A check only reads; anything it
finds to install is a button of its own, and firmware asks first.

- **HackRF**: the `hackrf` tools and firmware files from Ubuntu, the firmware in the HackRF
  (from `hackrf_info`), and the newest release on GitHub. It offers to install a newer Ubuntu
  package, to flash the installed firmware image if the HackRF's is older (`hackrf_spiflash -w`,
  which refuses an image made for another board, then `-R` to restart it), or to download the
  newest release from Great Scott Gadgets and flash its firmware.
- **RTL-SDR**: has no firmware; its driver and tools update. It offers a newer Ubuntu package,
  or to build the newest osmocom release (the RTL-SDR Blog fork for a Blog V4) into
  `/usr/local`.
- **tinySA**: compares its firmware with the newest in the author's download folder
  (athome.kaashoek.com). Installing downloads it, checks the file names that version, lets go
  of the tinySA, waits up to two minutes for it in DFU mode (hold the jog button while
  switching on an Ultra; CONFIG → DFU on the original), and flashes it with `dfu-util`.
- **RF Explorer**: shows its firmware version. Its firmware only installs through RF Explorer's
  own Uploader, which looks up the latest itself, so this downloads the Linux Uploader, installs
  Mono if needed, lets go of the RF Explorer, and opens the Uploader as you (not root, as RF
  Explorer asks).

Installing uses sudo when it needs no password, otherwise polkit asks. Downloads go to
`~/.local/share/wifisurveyor/updates/`.

### Several adapters and analyzers

Any mix works, from none of anything to several of each, and each device is found by itself
when it's plugged in. More devices share the work rather than repeating it, and something
heard by more than one device is counted once:

- **Wi-Fi adapters.** With *Setup → Surveying → Scan with every Wi-Fi adapter* (on by default),
  every adapter not in monitor mode shares each point's scan, up to six. The one kept for
  monitor mode stays free for captures. Each adapter scans its own channels, and the same
  channels go to the same adapter at every point, so an access point is read by one adapter
  throughout. An adapter can still decode a beacon from the next channel over, reading it a
  few dB low; when two adapters hear the same access point, the one on its own channel wins.
  When a point's two scans (passive, then active) both hear an access point, the level kept is
  the mean of the two readings, not the stronger, which would read high. Line the adapters up
  with each other in Setup (*Match*) so their levels agree.
- **Bluetooth adapters.** Device and finder-tag scans run on every Bluetooth adapter at once.
  A device heard by more than one is listed once, at its strongest reading, with any name or
  class one of them caught. A tag counts as away from its owner if any adapter heard it say so.
- **Spectrum analyzers.** Every RF Explorer and tinySA plugged in is opened. At each point they
  sweep at the same time, the Wi-Fi ranges shared between them: ranges only one can reach go to
  it, and the rest are balanced. The same set of analyzers always shares the same way, so each
  range is measured by the same analyzer at every point and the maps compare like with like.
  The channels watched closely are shared the same way. The HackRFs join in when *Sweep with the
  HackRF* is on, or when there's no other analyzer. The live Spectrum tab lists them all.
- **HackRFs.** Every HackRF is opened, each with its own calibration and its own record of what
  its antenna hears (by serial number), while gain and antenna power settings apply to all. The
  close look at each point's channels and the radar watch share the channels between the
  HackRFs that aren't sending, so the radar watch gives each channel proportionally longer. With
  two, one can send the test tone while the other listens; *Which HackRF* in Setup chooses the
  one that calibration, *What's on a channel* and the tone apply to.

The report's *How this survey was made* says which adapters, analyzers and HackRFs took part,
which analyzer swept each band, each HackRF's calibration as it was at the time (per band,
gains, antenna power, whether its antenna missed a band), where it overloaded, how many raw
recordings were kept, and how many repeat readings were merged. Each point's HackRF table
marks overloaded captures and names any recording. The cellular section warns when its levels
came from an uncalibrated HackRF.

## The views

**Survey** is the map. Load a floor plan or work on a blank grid, then tap where you're
standing to measure: an averaged Wi-Fi scan, the current link, and, when an analyzer is
connected, sweeps of 2.4 GHz, both halves of 5 GHz and as much of 6 GHz as it reaches with the noise floor worked out per
20 MHz channel. Choose what the heatmap shows: signal, signal-to-noise, noise floor,
overlapping access points, channel occupancy, retry rate, measured airtime, unexplained
energy, or a verdict map that names what limits each spot rather than colouring a number.

With an analyzer connected, every point also sweeps the same US Wi-Fi ranges ("Spectrum
sweeps per band" in the map settings sets how many). The Wi-Fi scan and the sweeps run at the
same time when the app runs as root: the card then scans passively, listening for beacons
instead of sending probe requests, so the analyzer never records the card's own
transmissions as someone else's energy. Without root, NetworkManager's scans do send probes,
so the two take turns.

After the sweep, and once the Wi-Fi scan has finished, the analyzer watches the busiest
channels closely (**Channels to watch closely**, default 3, with the network chosen under
Heatmap going first; **Seconds per channel**, default 4). Parked on one channel's full width it
sweeps many times a second, so it catches transmissions the wide sweep misses. For each channel
it keeps the power while something transmits, which is directly comparable with the card's
RSSI, how much of the time the channel was busy, and the noise floor per 20 MHz from the quiet
moments. The noise, signal-to-noise, occupancy, unexplained-energy and verdict maps use these
wherever a channel was watched. It only listens, so it adds a few seconds per channel to each
point and nothing to the air.

Why the analyzer's trace sits so far below a card's RSSI: the card measures a whole 20 MHz (or
wider) channel, and only while a frame arrives. The analyzer measures one narrow slice at a
time (about 600 kHz on a wide sweep, some 15 dB less for the same signal), and averaged over
time, idle moments included, an access point sending only beacons is on air 1–2% of the time.
Watching a channel closely, and adding up its full width, closes that gap.

Each point with a sweep is drawn as a spectrum ring inside its heatmap circle, growing and
shrinking with it as you zoom (turn it off with **Spectrum at each point**; zoomed far out,
points go back to plain markers). Frequency runs clockwise from the top through 2.4, 5 and
6 GHz, marked by coloured arcs outside the rim, and level runs from −100 dBm at the centre to
−40 dBm at the rim, which is in the point's heatmap colour. The trace is power per 20 MHz,
the same measure as an access point's signal, so each access point's dot sits on top of its
own energy. Unswept stretches are grey. Each access point heard there is a dot on its primary
channel at the level it was heard (on the rim if it's stronger than −40 dBm), with an arc
across its whole channel width, in a colour of its own. Point at a dot, or tap it, for the
network's name, band, channel, width, strength and manufacturer. A channel the analyzer
watched closely also gets a dashed arc at the power it measured while something transmitted,
and its details give that power, how busy the channel was and its noise floor. The report lists
the channels watched at each point.

Each sweep also keeps a picture of the whole spectrum as the Spectrum page would draw it,
with the access points heard there marked. **Export report…** in the main menu writes one
self-contained HTML file: what's limiting coverage across the survey, the map with its rings,
and for each point its diagnosis, the access points heard and that spectrum picture. Points
measured before pictures were kept, or on Windows, get one drawn from their sweeps. Each
point's tables list every BSSID heard, so several access points of one network on the same
channel each get a row, and tapping a column heading sorts by it (again to reverse).

Under Heatmap, **Network** filters the map to one SSID and **Access point** narrows it further
to a single BSSID (each listed with its manufacturer). The **Signal quality** panel below
summarises the chosen network or access point across the whole survey: a one-word grade, how
many points heard it, the share that were usable (at or above -70 dBm), and its weakest,
strongest and median signal. When a chosen map has nothing to colour (for example Measured
airtime before any capture is attached), the status line says why rather than showing a blank
plan.

**Requirement** (under Heatmap) sets what the survey is judged against: Data, Voice, Video or
Location (RTLS). Each asks for a signal level from the serving access point, a second (and for
location a third) access point strong enough to roam to or triangulate from, a signal-to-noise
ratio, at most so many other radios on the same channel, and a limit on how busy the channel
is. The **Meets the requirement** map shows pass or fail at each spot, holding a point opens
what fails there, and the verdicts judge against the same levels. With **All bands** each band
is judged on its own and a spot passes if any band does, since clients use whichever serves
them best. Two more maps go with it:

- **Second access point (roaming)**: the second-strongest radio of the network in the same
  band. Voice and video need it near −67 dBm so calls survive a move between access points.
- **Co-channel contention**: how many other radios are heard above −82 dBm on any part of the
  serving radio's channel, wide channels included. Each makes the others wait their turn.

An access point broadcasts several SSIDs from each radio, each with its own BSSID; these are
grouped back into radios so one access point isn't counted as five.

**Review → Configuration audit** looks at how the networks heard are set up: 802.11b rates
(1–11 Mbps) still required on 2.4 GHz, the share of each channel's time spent on beacons,
too many SSIDs on one radio, 40 MHz or overlapping channels on 2.4 GHz, and, within each
network, access points that differ in security, basic rates or 802.11k/v/r roaming support.
**Channel plan** suggests channels for the network chosen under Heatmap: it keeps the
network's own radios apart wherever a point hears two of them above −82 dBm, then away from
other networks, and changes as few radios as it can: it starts from the channels as they are
and moves a radio only when that clears enough clashes to be worth the work, so it suggests a
handful of changes rather than reshuffling the site for nearly the same result. Access points
near each other on different channels is right; the plan's job is to find which channels. DFS
channels are left out unless **Include DFS channels** is on. Both also go in the report, where
the recommendation lists each change as a step to tick off: the access point (numbered as in
the access point table), its BSSID, the point it's heard strongest at, the channel now and
suggested, and how many clashes it has before and after. The changes are meant to be made
together, with the channels then fixed so automatic channel selection doesn't undo them.

With **Check the connection** on (the default) and the scanning adapter connected, each point
also records latency, jitter and packet loss to the gateway and how long a DNS lookup takes,
after the spectrum is measured. Points where that failed, or lost 10% or more, get a red "!"
on the map, and holding a point offers **Check connection again**. Latency and loss have maps
of their own. It needs `ping` (the iputils-ping package, installed on Ubuntu by default).

**Scan Bluetooth at each point** (under Measuring, off by default, needs root) lists every
Bluetooth device sharing 2.4 GHz at each point — printers, scanners, audio bridges,
peripherals, phones, wearables — by listening for what they already broadcast, through BlueZ's
`btmgmt`. Nothing is paired with or connected to. It finds equipment that adds to the
interference, and any device that shouldn't be there (a phone in a no-phone area, or one on a
charging stand pulling updates). Each device's kind comes from the class it announces and its
maker from the same IEEE register the Wi-Fi side uses. A device with a fixed address heard at
three or more points is placed on the map from how its signal fades (a blue badge, a printer
glyph for printers); one heard at fewer points is shown where it was strongest. A device using
a rotating private address can't be placed — those are summarised in the report by kind. Holding
a device shows what it is, and the report lists them all. It needs the `bluez` package.

The map marks where things probably are, worked out from how the signal falls off across the
points:

- **Access points** get a plain blue circle (dashed when the fix is rough), labelled with the SSIDs heard from it and
  badges for the bands they're on (2.4G, 5G, 6G). The radios of one access point land in the
  same place, so they're shown together. Each needs to have been heard at four or more points.
- **Noise sources** get an orange static badge: what kind of emitter it looks like (steady
  carrier, bursty, wideband, microwave oven), its frequency, band and the channels it lands
  on, and its peak level. The search covers every captured range for energy the visible
  networks don't account for, groups it by frequency across the survey, and locates each
  group. A source needs to have been seen at three or more points to be placed.

A dashed ring shows how far off each estimate could be, and an estimate that's only rough is
drawn faded. Touch and hold an icon for the details. Both layers can be turned off under "On
the map", and the band filter applies to them too.

**Band** and **Channel** narrow the map. The channel matters for occupancy, unexplained
energy, and overlapping access points; left on Auto, each point uses the channel of its
strongest network (or of the network you picked), so the occupancy map shows how busy the
channel people are actually on is.

The map is worked out in the background, so large plans and many walls don't freeze the
window.

It's built for a touchscreen tablet first:

| Gesture | What it does |
| --- | --- |
| Tap | Measure there (after a moment, so a double-tap never measures by accident). On a numbered point or a map icon, its details instead: every access point heard, and what to improve there |
| Drag | Pan; flick and it coasts. In wall mode, draws a wall |
| Pinch | Zoom, and pan with the same two fingers |
| Double-tap | Zoom in on that spot |
| Two-finger tap | Fit the whole plan |
| Touch and hold | Details of a point, access point or noise source, or delete a point or wall (Undo brings it back) |
| Swipe in from the right | The map settings, when the window is narrow |
| Set scale, then tap twice | Marks two spots a known distance apart; enter it in feet |

**Scale.** Turn on *Set scale*, tap both ends of something you know the length of (a wall, a
doorway), and enter the distance in feet. The plan then shows a scale bar, each spectrum ring is
drawn 10 ft across, the heatmap reaches its true 8 m, access point locations use real distances,
and the report adds access point reach, distances to the nearest access point, and gaps between
measurements. Choosing a new floor plan clears the scale.

**Access points.** Tap an access point's icon to name it, or *Move to its real position* and
tap where it is: its reach and every distance in the advice then use the true position (a
square icon), instead of the estimate (a circle). Access points heard at too few points to
locate appear beside the point where they were strongest, ready to be moved. *Back to the
estimate* undoes a move.

**Planning.** Under *Planning*, *Add* a planned access point and tap where it would go (a dashed
purple diamond). Choose *Predicted signal with planned access points* under *Map* to see the
result, predicted from the path loss measured in this building, walls included. Tap it to
rename it, choose its bands, move or delete it.

**Measuring.** While the analyzer sweeps, the Wi-Fi card only listens (so its probes don't
show up in the spectrum), which misses many access points; a normal scan follows once the
analyzer is done and the two are merged. *Networks to survey* (Choose…) picks the networks the
survey is about: at every point, each of them that's heard is joined in turn (strongest first,
through a temporary copy of its saved connection, so the computer's own Wi-Fi stays connected),
its connection checked and, with *Test throughput* on, its speed tested. Only those networks are
joined; one without a saved password is skipped with a note (connect to it once from the
system's Wi-Fi settings), and the chooser says which have one. The report then judges each point
for the strongest of them, adds a *Networks surveyed* table (coverage, checks that worked,
latency, speed, and what went wrong, per network) and plans their channels. With none chosen,
*Join the network first* connects to the one network chosen under *Heatmap*, as before. *Test throughput* runs
iperf3 to the server you give (install `iperf3` here, and run `iperf3 -s` on a wired computer).
*Phone allowance* raises every signal target, so "meets" means on a phone. Tap a point for *Measure
again here* (replaces its readings; Undo restores them) and a note.

**Reports.** *Report details…* sets the cover: company, surveyor, contact and logo (kept on this
computer) and client, site and purpose (kept with the survey). *Export report…* writes the full report: an overview of recommendations, the map
with numbered pins, and every point's findings, spectrum and access points. *Export overview…*
writes only the recommendations, checklist, maps and networks: a short page for whoever makes
the changes. Both include floor-area coverage, maps of signal, signal-to-noise, roaming overlap,
channel sharing, throughput and (with planned access points) predicted signal, a comparison
with the survey being compared against (if any), and how the survey was made. The full report
adds the access point table and each point's details.
The overview groups what to do by theme (roaming, noise and interference, coverage, channels
and capacity, connection quality, security, and the survey's own gaps), says where on the plan,
and says what users will notice afterwards.

With a mouse, right-click does what holding does, the wheel pans, and Ctrl and the wheel zoom
about the pointer. The ⓘ button on the map lists the gestures.

The map settings (what the heatmap shows, the adapter, wall loss) sit in a panel beside the
map, which the button at the right of the toolbar hides. Under about 1000 pixels wide the panel
slides over the map instead, and under about 760 the tabs move to the bottom of the window. On
a small screen the window opens maximized; **F11** or the button in the header goes full
screen. Controls are sized for fingers (about 44 pixels, roughly 6 mm on a 7-inch 1280×800
panel).

Three-finger swipes aren't used: GNOME keeps those for switching workspaces.

**Discover** lists nearby access points, with who made each one, and rescans every five
seconds.

Manufacturers come from the IEEE's register of MAC address blocks (all four: MA-L, MA-M, MA-S
and IAB). **Setup → Manufacturers → Download** fetches the latest into the app's data folder;
until then the copy from Ubuntu's `ieee-data` package is used, when it's installed. A second
SSID on an access point usually has a made-up (locally administered) BSSID; its maker is
found from the real address it was made from and marked "virtual BSSID". A phone or laptop
using a private address shows as "Private address".

**Watch** stays with one network's access point for as long as you leave it, for the network
that drops every client now and then and nobody can say why. Choose it from the start screen
(*Watch a network*) or the Watch tab. It's drawn like a patient monitor, in four lanes: a
heartbeat per check (taller for a quicker internet reply, flat and red while it's down), a pulse
for the share of the access point's beacons arriving, how busy the channel is, and how loaded
the access point is. Beside them are the vital signs: internet round trip, signal, channel, clients,
link rate, the access point's uptime, availability and incidents. Below are the channel as the
analyzer sees it (only that channel, swept fast, with a waterfall), the whole session as a
strip, and every incident with its most likely cause.

**Both bands at once.** When the network's SSID is on 2.4 GHz and 5 GHz, a second row offers to
watch the other band too, with adapters of its own, picked for you: an adapter that only does
2.4 GHz joins there, leaving one that does both for 5 GHz, and the best monitor-mode adapter
listens. Any of them can be changed. While both run, the 5 GHz traces sit under the 2.4 GHz ones
on one time axis (dragging, zooming or tapping either moves both), their spectrum views sit side
by side, the session strip has a row per band, and the incidents of both are in one list, each
tagged with its band. When both drop within a minute of each other, the list says to look past the
radios (the access point itself, its power, the line behind it); when only one does, that radio,
its channel or its clients. One spectrum analyzer takes turns between the two channels.

**The spectrum on the same clock.** With an analyzer watching the channel, a fifth lane,
SPECTRUM, shows it as a waterfall lying on its side: time across, on the same axis as the other
lanes (it pans and zooms with them), frequency up the side, quiet blue to strong red. When the
access point drops, its channel goes quiet in the same columns, which tells an access point that
stopped transmitting from a link that failed while it carried on.

**Reports and the survey.** *Save report…* writes the watch as a report of its own and opens it:
availability per band, the AP's signal, gateway latency, retries and clients, any pattern in the
dropouts, each dropout with its most likely cause and what to do, and a timeline of the whole
watch at 30 s a column (wider for a watch over a day and a bit): each band's status, the signal,
the gateway's reply time, the channel's power and each band's spectrum, one under the other on
one time axis, the dropouts marked through all of them. *Add to survey* puts it on a survey: the
one open, a recent one or any survey file (opened for you); tap where you watched from, and it's
a W pin on the map (green, amber or red by how much of the time the network was usable) and a
*Networks watched* section, timeline included, in the survey's report. *Earlier watches*, under
the incidents, lists every watch saved on this computer with the same *Report…* and *Add to
survey*, and a bin to delete one. The access point list is in signal order, strongest first.

**What's kept.** While watching, everything goes into a plain log as it happens, so a crash
loses seconds. Once a watch is over (or at the next start, for one that ended in a crash), its
log is compacted: incidents, events, channel readings and the spectrum (a row per 30 s, average
and peak per bin, a byte each) stay whole, every check stays for ten minutes either side of a
dropout and around any moment that wasn't healthy, and the long healthy stretches become a
summary a minute (lowest, median and highest of each figure). Then it's gzipped: about an
eighth of the size, with every report figure the same. A dropout's frame capture keeps its first
two minutes and the half minute before it recovered, so an outage overnight isn't hours of
frames. A survey keeps a watch's summary and timeline only (tens of kilobytes an hour), so it
reports the same after the logs are deleted.

**Colours mean what a user would notice.** Red is down, amber is slow (the gateway or internet
taking over 250 ms). Beacons missed while everything still works are drawn lower, not red: a
client saving power skips some, and nobody using the network notices. The beacon trace turns red
only when the Wi-Fi link itself failed with them. Tap any moment for the numbers behind it.

An adapter joins the access point, and only that one: it isn't allowed to roam, and
NetworkManager isn't allowed to move it to another network while this one is down. Every
check (1, 2 or 5 s apart) asks whether it's still associated and how many beacons arrived,
pings the gateway and the internet (three public DNS resolvers, by address, so DNS trouble
can't look like an outage), reads the link's transmit and receive rates and resends, and every
few checks looks up a name through each of the network's DNS servers. Two failed checks in a
row, or the link dropping, is an incident. One lost ping to the gateway isn't: it takes two.

**The access point's load.** Many access points say in their beacons how many clients they
have and how busy they find the channel (the BSS Load element); a monitor adapter reads it, and
the AP LOAD lane and the CLIENTS tile show it. Without it, the lane counts the clients the
monitor adapter heard in the last five minutes. The monitor adapter also counts the share of the
access point's frames sent twice (a crowded or noisy channel) and the other networks on the
channel.

**The channel's busy figure** is the share of single measurements (one frequency, one moment)
well above the noise floor, not the share of sweeps: a tinySA takes about two seconds over a
channel, so every sweep catches a beacon somewhere and would read 100%. An analyzer is less
sensitive than an access point's radio, so expect its figure below the access point's own.

Each incident gets the evidence that tells its causes apart, and is listed with the most likely
one, how sure that is, why, what to do about it, what else it could be, and what evidence would
have made it surer:

| What happened | How it shows |
|---|---|
| The access point went down (rebooted, lost power) | Every radio of the unit goes silent; afterwards their clocks (TSF) have started again from zero |
| One radio restarted | That radio goes silent or its clock restarts while the unit's other radios keep going |
| A radio is failing | The same radio keeps restarting, or misses beacons and weakens beforehand |
| Forged deauthentication | Deauthentications while the real access point keeps beaconing, from a different signal level, out of step with its sequence numbers, or unprotected where protection is required |
| The access point disconnected everyone on purpose | Genuine deauthentications; the reason code says why (15 and 16: the group key rekey failing; 14: TKIP countermeasures; 1: a controller or schedule) |
| It changed channel | A channel switch announced in its beacons, or it comes back on another channel (radar on a DFS channel) |
| Interference | The analyzer sees the channel flooded while beacons are lost |
| Only this adapter | The access point kept beaconing and nobody else was disconnected |
| This computer's adapter dropped out | The adapter itself fell off USB and came back (it reappears as a new interface): not the network, and not counted against its availability |
| Upstream, gateway, DHCP or DNS | Still connected, and the checks say which stage stopped answering |

The evidence comes from up to three places, and the page says which you have and what's
missing:

- **The joined adapter** (always): the kernel's reason for each disconnect, whether the access
  point sent it, forged deauthentications rejected under protected management frames, the
  beacon counters, and the link rates. While the link is down it scans for the access point and
  the unit's other radios (found from their BSSIDs) and reads their clocks, which shows a
  restart. As root, that is; NetworkManager's scans don't report the clock.
- **A second adapter in monitor mode** (needs root): parked on the access point's channel, it
  hears every beacon and every deauthentication, from anyone to anyone. This is what tells a
  forged deauthentication from a real one and a silent access point from a lost client. The
  frames from 30 seconds before each incident to its end are saved as a `.pcap` beside the
  session, to open on the Capture page or in Wireshark. Some drivers (the Alfa AWUS036ACM's
  MT7612U after working on 5 GHz) go deaf in monitor mode on 2.4 GHz; if it hears nothing at
  all, the page listens through a fresh monitor interface on the same radio instead.
- **A spectrum analyzer** (a tinySA or RF Explorer): parked on the channel's full width, it shows
  whether the channel went quiet or was flooded.

Several adapters on one network each have an address on the same subnet, and Linux lets any of
them answer for any of the computer's addresses, so the gateway can send one adapter's replies
to another. While watching, each adapter answers only for its own address (`arp_ignore 1`,
`arp_announce 2`), and the previous settings are put back afterwards.

**Worth fixing, outage or not** lists what turns up between incidents: a DNS server that fails
lookups, forged deauthentications rejected, beacon loss while connected, and an adapter that
keeps dropping off USB. The incidents list also says what they have in common: a regular
interval, a time of day, or a radio that restarts after the same uptime each time.

Each session, one per band, is written as it goes to its own folder under
`~/.local/share/wifisurveyor/monitor/`: `session.jsonl` with every check and incident, and the
incident captures. **Export incidents…** saves them as a spreadsheet, every band in one file.
While a session runs, its adapters are left alone by the other pages, and closing the app asks
first. While watching, the app keeps the computer from sleeping (through logind, so it works run
as root or not), letting go on battery at 15% or below. If the computer sleeps anyway (low
battery, the lid, the suspend menu), the watch first ends any outage in progress at that moment,
writes its log safely to disk and pauses; on waking it rejoins the network and carries on in the
same session, without counting the sleep or the rejoin as an outage.

**Spectrum** draws the live trace and peak hold from the analyzer with Wi-Fi channels
marked. **All Wi-Fi bands (US)**, the default, goes round every US Wi-Fi range in turn and shows
them together, one panel per band: 2.4 GHz (2400–2500 MHz), 5 GHz as UNII-1/2A (5150–5350) and
UNII-2C/3/4 (5470–5925), and all of 6 GHz (5925–7125), greyed out above the 6100 MHz the
6G Combo reaches (channels 1–29); a tinySA Ultra+ fills it in. *6 GHz, all (tinySA Ultra+)*
in the range list watches the whole band on its own. The analyzer sweeps one range at a time (at most 600 MHz on an RF Explorer, 500 on a tinySA), so it averages **Sweeps
per range** sweeps of each before moving on; a yellow bar marks the range being swept. The
5350–5470 MHz gap isn't Wi-Fi in the US and isn't swept. An analyzer without 6 GHz says so on
that panel and carries on with the rest. The analyzer is let go between ranges, so a survey
measurement never waits for a whole pass.

The access points a Wi-Fi scan finds are marked on the panels, each in its own colour, as a
bar across its whole channel (40, 80, 160 or 320 MHz from `iw`; NetworkManager scans only
give the primary 20 MHz) at the level the adapter reported, with a dot on the primary
channel. The page rescans every ten seconds while it's showing, passively when running as
root; the **APs** switch turns this off. The adapter's level is the whole channel's power
while the trace is the power in one resolution bandwidth, so an access point sits well above
its bump in the trace.

**Waterfall** (on by default) runs recent sweeps down under each panel, newest at the top and
coloured from dark blue (quiet) through yellow to red. Interference that comes and goes shows
its pattern there: a microwave oven as blocks switching on and off, a frequency-hopping
device as a trail of short dashes, a busy Wi-Fi channel as speckle across its width.

**Record** saves every sweep, and the access points scanned every ten seconds, to a file for
as long as it runs, so the laptop and analyzer can be left somewhere overnight. It starts the
sweeping if it isn't already. **Recordings…** opens one: the whole span as one waterfall with
clock times down the side, the busiest channels with how often they were busy, in how many
separate bursts and around what time of day, and every access point with the share of scans
it was heard in and its weakest, average and strongest signal, which shows one that drops out.
A day's recording is a few tens of megabytes, in `~/.local/share/wifisurveyor/recordings`.

Drag a finger across it (or hover) for the frequency and level under it. On a single range,
pinch to zoom the frequency axis and double-tap to see the whole range again.

**Capture** switches an interface into monitor mode with `iw`, releasing it from
NetworkManager first so nothing scans on it, then captures and reports what the frames say:
retry rate, airtime measured from frame durations, clients heard, and per-access-point
beacon details including channel width, generation, security, and the BSS Load element that
carries the access point's own view of how busy the channel is. It also opens a pcap captured
elsewhere.

Each access point's manufacturer is shown beside it, and **Devices** lists every other address
heard talking to one (phones, laptops, cameras, printers) with its manufacturer and the
network it was on.

**Attach to point** stores a capture's retry rate, airtime and client count with the survey
point you choose (the newest, unless you pick another). That's what fills in the retry and
airtime maps and the "retries and collisions" verdict. Nothing is attached until you press it.

**Setup** checks what's installed and connects the spectrum analyzer and GPS. Each adapter has a
**dB correction**: adapters read the same signal several dB apart, and this is added to every
reading the adapter's model makes, so surveys taken with different adapters line up. With two
adapters plugged in, **Match** scans with both at the same moment and sets the correction from
the access points both hear (it needs root, for iw's true dBm).

Scans keep only the access points actually heard during that scan. The kernel remembers
results for about thirty seconds, so without this a point would inherit readings from
wherever you stood before.

**Where surveys are kept.** New surveys save to `~/Documents/Wi-Fi Surveys` (your desktop's
Documents folder). That holds whether the app runs as you or as root through sudo/pkexec. The
root copy hands every file it writes back to you, so either can open what the other saved. Each
survey records who saved it and when. When the app opens, it lists recent surveys: those in that
folder, in the older `~/.local/share/wifisurveyor`, anywhere you've saved or opened one, and
(when running as root) in root's own folders. One from root's folder is saved to the shared
folder from then on. It also offers unsaved work to recover. **Recent surveys…** in the main
menu shows the list again; Setup → Surveying turns it off at start.

**The Advanced network tests table belongs to the survey**, so a new survey starts with
nothing to test and one site's networks are never tried at another. A network in it is only
joined and speed-tested when it's heard at the point, encrypted, and its password is saved on
the computer. Otherwise it's skipped, and the status line says why.

**Timeline.** *Start a follow-up survey* (main menu) begins a new survey of the same site: the
plan, scale, walls, placed and planned access points, tone source and test table carry over,
the points start empty. Surveys of one site (follow-ups, or any surveys of an identical floor
plan) line up on the **Timeline** (toolbar). Its slider stops at each survey by date. *Change
since then* colours the map green where it's better now and red where it's worse, and says in
words what changed: for example, "Since Mon 13 Oct (3 days): median signal up 10 dB (−74 → −64
dBm); points meeting Data: 0% → 100%; 11 fewer high-priority issues". *As it was* shows that
survey's own map, for whichever measure is chosen. The report adds a **History of this site**
section whenever there are earlier surveys: what changed since the previous survey and since the
first, a table with ▲▼ against the survey before, a small chart per measure, and the signal map
at each survey. Its change maps compare against the previous survey automatically.

**Compare with another survey…** (main menu) opens an earlier survey of the same floor plan,
from before a change, and maps what changed for whichever map is chosen: green where it's
better now, red where it's worse, only where both surveys measured nearby. The status line
sums it up (how much of the area got better or worse, and the average change), and the older
survey's points show as hollow circles. The verdict map can't be compared; the others can.

## What the analysis does

- **Offset calibration.** The Wi-Fi card and the analyzer read different absolute levels, so
  they're referenced to each other on channels where one strong network dominates.
- **Unexplained energy.** Measured channel power minus what the visible networks and the
  noise floor account for. Well above zero means something is transmitting that the card
  can't attribute.
- **Verdict per point.** The checks run in order and stop at the first failure, so the answer
  names a cause and an action: weak coverage, non-Wi-Fi interference, neighbouring networks,
  collisions, airtime congestion, or link configuration.
- **Source location.** A log-distance path-loss model fitted over a grid of positions and
  exponents, counting any walls a path crosses, then again on a finer grid around the best,
  returning an area rather than a false pinpoint. Every access point heard is located: SSIDs a
  radio sends side by side (same channel, addresses a few bits apart, the same level everywhere)
  are located together. Once the plan has a scale, other people's access points are looked for
  up to twice the plan's size past its edges, and the level at 1 m is held to what a real
  transmitter can manage, so flat readings can't explain a far-off radio as an impossibly loud
  one. One placed off the plan is drawn where it is, with a dashed line to the plan's edge and
  its distance and direction (by the compass, and with coordinates, once the plan is on a map).
  The networks to survey are kept to the property. A fit that runs to the end of the search is
  shown in the middle of where it was heard instead, marked as such.

## Saving, recovery, and the Windows app

The title shows a dot while there are unsaved changes, and New, Open, closing the window, and
relaunching as root all ask before throwing them away. Unsaved work is also written to a
recovery file every two minutes (`~/.local/share/wifisurveyor/recovery.wsurvey`); if the app
stops unexpectedly, the next start offers it back.

A survey saved by the Windows app opens here as it is. **Save a copy for the Windows app**
writes one it can open. What only the Windows app uses (GPS pins, walk tracks, iperf3 results)
is carried through untouched. A file that isn't a survey is refused with a reason rather than
opened as an empty one.

## Tests

```
python -m unittest discover -s tests
```

They include a simulated RF Explorer and a simulated tinySA Ultra+ on pseudo-terminals, so
each driver's detection,
sweeping, sharing between the live view and a survey, and unplugging are all exercised without
the hardware. The HackRF driver runs against stand-in `hackrf_sweep` and `hackrf_transfer`
programs, and the I/Q classifier against synthetic Wi-Fi, Bluetooth, Zigbee, microwave-oven,
carrier and radar captures. The RTL-SDR driver runs against stand-in `rtl_power` and
`rtl_eeprom`; the satellite agent runs for real on a local port with its pretend tinySA, with
pairing, replay protection, the transmit rules on both ends, the lease and discovery tested;
the mic coordinator's picks are checked against every intermod product by brute force; and the
updater runs with the network, apt and dfu-util faked.

## Licences

Wi-Fi Surveyor itself is MIT, copyright Tom Race. See [LICENSE](../LICENSE).

Nothing is bundled. `iw`, `iproute2`, libpcap, NetworkManager and gpsd come from your
distribution; Scapy (GPLv2), NumPy (BSD), PyGObject (LGPL) and pyserial (BSD) come from
Python packaging. The survey file is plain JSON. The optional radio tools (`hackrf`, `rtl-sdr`,
`dfu-util`, GPL) come from your distribution; firmware is fetched from its makers only when you
press install (HackRF and tinySA firmware are GPL; RF Explorer's Uploader is RF Explorer's own,
downloaded from them, and Mono comes from your distribution).
