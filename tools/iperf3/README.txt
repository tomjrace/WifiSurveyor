Put iperf3.exe here (plus cygwin1.dll and LICENSE if your build needs them) and the
build will copy them into the application's iperf3 folder, where the app finds them
automatically.

The easy way:  powershell -ExecutionPolicy Bypass -File tools\fetch-iperf3.ps1

Licensing, briefly:
  iperf3 is BSD-3-Clause, so it may be redistributed as long as its copyright notice
  and disclaimer travel with it. Keep the LICENSE file next to the binary.

  Most Windows builds of iperf3 are compiled with Cygwin and need cygwin1.dll, which
  is LGPLv3. Shipping it is allowed, but LGPL obligations then apply: include the
  LGPL text and let users replace the library. If you would rather avoid that, ask
  users to install iperf3 themselves and leave this folder empty; the app works fine
  either way and will simply look for iperf3 on the path.
