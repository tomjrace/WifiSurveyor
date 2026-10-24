"""The one way this app talks to the internet, and what it will never send.

Every web request goes through here (a test fails if any code goes round it), so these hold
for all of them:

  - The app identifies itself only as "WifiSurveyor/<version>": never with an email address,
    a name, or anything else about the person using it. Callers can't change that; the only
    other identity is the fixed browser-like one the IEEE's server insists on.
  - Nothing that looks like an email address goes out, in the address, a header or the body,
    and neither does the surveyor's name or contact from Report details. The request is
    refused, with PersonalDataError, before anything is sent.
  - It only talks to the hosts listed in HOSTS, each with what it's for and what's sent
    there (PRIVACY.md is the same list, for people), and to this network's own private
    addresses (satellites). A redirect to anywhere else is refused too.

Wi-Fi Surveyor never needs to tell anyone who its user is. A new service must be added to
HOSTS, with what it receives, before the app can reach it.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.error
import urllib.parse
import urllib.request

from .. import __version__

USER_AGENT = f"WifiSurveyor/{__version__}"
# The IEEE's registry server turns away requests that don't look like a browser's
BROWSER_LIKE = f"Mozilla/5.0 (X11; Linux) WifiSurveyor/{__version__}"
BROWSER_LIKE_HOSTS = {"standards-oui.ieee.org"}

# host: (who runs it, what the app sends there). Kept the same as PRIVACY.md by a test.
HOSTS = {
    "tile.openstreetmap.org": ("OpenStreetMap Foundation", "map tile numbers (the area shown)"),
    "nominatim.openstreetmap.org": ("OpenStreetMap Foundation", "the address typed into Site map"),
    "overpass-api.de": ("FOSSGIS e.V.", "a map position and radius, for building outlines"),
    "overpass.kumi.systems": ("Kumi Systems", "a map position and radius (if overpass-api.de fails)"),
    "overpass.private.coffee": ("private.coffee", "a map position and radius (if the others fail)"),
    "basemap.nationalmap.gov": ("US Geological Survey", "map tile numbers (the area shown)"),
    "api.beacondb.net": ("BeaconDB", "nearby access points' MAC addresses and signal levels, "
                                     "only after asking"),
    "standards-oui.ieee.org": ("IEEE", "nothing: downloads the manufacturer registry"),
    "export.iperf3serverlist.net": ("iperf3serverlist.net", "nothing: downloads the server list"),
    "api.github.com": ("GitHub", "nothing: looks up the newest HackRF and RTL-SDR releases"),
    "github.com": ("GitHub", "nothing: downloads a HackRF or RTL-SDR release"),
    "codeload.github.com": ("GitHub", "nothing: GitHub's source downloads"),
    "objects.githubusercontent.com": ("GitHub", "nothing: GitHub's release downloads"),
    "release-assets.githubusercontent.com": ("GitHub", "nothing: GitHub's release downloads"),
    "athome.kaashoek.com": ("the tinySA's author", "nothing: tinySA firmware list and download"),
    "version.rf-explorer.com": ("RF Explorer", "nothing: downloads RF Explorer's Uploader"),
}

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


class PersonalDataError(ValueError):
    """A request would have carried something about the person using the app: never sent."""


class HostNotAllowed(PermissionError):
    """A request to somewhere not in HOSTS (or a redirect there): never sent."""


def personal_values() -> list[str]:
    """
    What this machine knows about its user that must never leave: the surveyor and contact
    from Report details. Short values are left out (they'd match ordinary words).
    """
    try:
        from ..core import settings

        saved = settings.load()
    except Exception:
        return []
    found = []
    for key in ("report_surveyor", "report_contact"):
        value = str(saved.get(key) or "").strip()
        for part in [value, *re.split(r"[\s,;/|]+", value)]:
            if len(part) >= 5 and not part.isdigit():
                found.append(part)
    return sorted(set(found), key=len, reverse=True)


def check_text(text: str, where: str) -> None:
    """Raises PersonalDataError when ``text`` holds an email address or the user's details."""
    if not text:
        return
    decoded = urllib.parse.unquote_plus(text)
    for candidate in (text, decoded):
        if EMAIL.search(candidate):
            raise PersonalDataError(f"Refused to send a request: its {where} contains an email "
                                    "address. Wi-Fi Surveyor never sends personal details.")
        lowered = candidate.lower()
        for value in personal_values():
            if re.search(rf"(?<![a-z0-9]){re.escape(value.lower())}(?![a-z0-9])", lowered):
                raise PersonalDataError(f"Refused to send a request: its {where} contains the "
                                        "surveyor's name or contact from Report details.")


def _private(host: str) -> bool:
    """This network's own addresses (a satellite on the LAN), and this machine."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # localhost, mDNS names, and a machine's bare name on this network ("benchpi")
        return host == "localhost" or host.endswith(".local") or "." not in host
    return address.is_private or address.is_loopback or address.is_link_local


def check_url(url: str) -> str:
    """The URL's host, when the app may contact it; raises otherwise."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise HostNotAllowed(f"Refused to open {url!r}: not a web address.")
    if host not in HOSTS and not _private(host):
        raise HostNotAllowed(f"Refused to contact {host}: it isn't one of the services Wi-Fi "
                             "Surveyor uses (net/outbound.py, PRIVACY.md).")
    if parts.username or parts.password:
        raise PersonalDataError("Refused to send a request with a login in its address.")
    check_text(url, "address")
    return host


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to a host the app may contact."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def build(url: str, data: bytes | None = None, headers: dict | None = None,
          method: str | None = None) -> urllib.request.Request:
    """A checked request: allowed host, nothing personal, and the app's own fixed identity."""
    host = check_url(url)
    extra = {}
    for name, value in (headers or {}).items():
        if name.lower() in ("user-agent", "from", "cookie", "authorization", "referer"):
            continue                         # who the app is, and nothing more, is decided here
        check_text(str(value), f"{name} header")
        extra[name] = str(value)
    if data:
        check_text(data.decode("utf-8", "replace"), "body")
    agent = BROWSER_LIKE if host in BROWSER_LIKE_HOSTS else USER_AGENT
    return urllib.request.Request(url, data=data, method=method,
                                  headers={**extra, "User-Agent": agent})


def open_url(url: str, data: bytes | None = None, headers: dict | None = None,
             timeout: float = 30, method: str | None = None):
    """Opens a checked request; use as a context manager, like urllib's urlopen."""
    return _opener.open(build(url, data, headers, method), timeout=timeout)


def fetch(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: float = 30, method: str | None = None) -> bytes:
    with open_url(url, data, headers, timeout, method) as answer:
        return answer.read()


# Re-exported so callers catch errors without importing urllib themselves
HTTPError = urllib.error.HTTPError
URLError = urllib.error.URLError
