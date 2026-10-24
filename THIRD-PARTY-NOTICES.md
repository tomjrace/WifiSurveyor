# Third-party notices

Wi-Fi Surveyor uses the components below. Each remains under its own licence, and those
licences govern what you may do when redistributing this application.

## Bundled with the application

### iperf3 — BSD 3-Clause

Copyright holders: The Regents of the University of California (Lawrence Berkeley National
Laboratory) and the iperf3 contributors. https://github.com/esnet/iperf

Used as an external program for throughput tests; the app runs `iperf3.exe` and reads its
JSON output. It is not linked into the application.

BSD-3-Clause permits redistribution in binary form provided the copyright notice, the list
of conditions and the disclaimer are included. The full text ships as `iperf3\LICENSE`
beside the binary; keep it there.

**If your iperf3 build is a Cygwin build** (most Windows binaries are) it also needs
`cygwin1.dll`, which is **LGPLv3**. Redistributing it is allowed, but the LGPL then applies
to that library: include the LGPL text and allow users to replace it with their own build.
If you would rather not take that on, ship no iperf3 at all and let users install it
themselves. The app looks for a bundled copy first and falls back to the system path.

## Referenced at runtime, not redistributed

### Npcap — proprietary, not redistributable

https://npcap.com

Live monitor-mode capture is not a supported feature of the Windows app, and it is not
going to be. Leftover code on the Capture tab can still load `wpcap.dll` from an Npcap
installation if one is present. Opening a capture file does not need Npcap.

**The free Npcap licence does not permit redistribution**, and limits installs, so this
application does not bundle it and must not be shipped with it. Distributing Npcap inside a
product requires an Npcap OEM redistribution licence from the Nmap Project.

### IEEE OUI registry (`oui.csv`) — optional download

https://standards-oui.ieee.org/oui/oui.csv

Used to turn BSSIDs into vendor names. Not bundled; download it yourself if you want vendor
names. Check IEEE's terms before redistributing the file.

### Public iperf3 server list — MIT

https://github.com/R0GGER/public-iperf3-servers and https://iperf3serverlist.net

The Devices and Clients tabs read the project's hourly JSON export to offer public test
servers, and cache it locally. Data only; no code is used.

## NuGet packages (linked into the application)

| Package | Licence | Purpose |
| --- | --- | --- |
| ManagedNativeWifi | MIT | Wi-Fi scanning through the Windows native Wi-Fi API |
| System.IO.Ports | MIT (.NET Foundation) | Serial ports for the RF Explorer and NMEA GPS |
| System.Management | MIT (.NET Foundation) | WMI lookups for friendly COM port names |

Licence texts for these are in each package's folder inside your NuGet cache, and on the
projects' repositories.

## Not third-party code

The RF Explorer serial protocol, Radiotap parsing, pcap and pcapng reading, and 802.11
frame and information-element parsing in this application were written from published
format documentation rather than copied from another implementation.
