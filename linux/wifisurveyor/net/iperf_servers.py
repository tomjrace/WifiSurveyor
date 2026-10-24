"""
The public iperf3 server list from iperf3serverlist.net, as the Windows app uses it: the
project's parsed JSON export, refreshed hourly at the source and cached here for six hours.
Offline, the last list downloaded is used, since yesterday's list beats no list.

A public server runs one test per port at a time, so adapters testing in parallel are spread
across hosts and ports (``Slots``), and a port that answers busy is skipped for the next one.
"""

from __future__ import annotations

import json
import random
import threading
import time
from . import outbound
from dataclasses import asdict, dataclass

from ..core.project import data_dir, hand_back

SOURCE_URL = "https://export.iperf3serverlist.net/listed_iperf3_servers.json"
MAX_AGE_S = 6 * 3600
RANDOM = "random"            # a server column set to this picks from the list for each test


@dataclass(slots=True)
class Server:
    host: str
    first_port: int = 5201
    last_port: int = 5201
    options: str = ""
    speed: str = ""
    continent: str = ""
    country: str = ""
    site: str = ""
    provider: str = ""

    @property
    def ports(self) -> range:
        return range(self.first_port, self.last_port + 1)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.first_port}"

    @property
    def display(self) -> str:
        speed = f" ({self.speed} Gb/s)" if self.speed else ""
        ports = f"-{self.last_port}" if self.last_port > self.first_port else ""
        return f"{self.country} {self.site} · {self.provider}{speed} — {self.host}:{self.first_port}{ports}"


def _cache_path():
    return data_dir() / "iperf-servers.json"


def cached() -> tuple[list[Server], float | None]:
    try:
        payload = json.loads(_cache_path().read_text())
        return [Server(**s) for s in payload["servers"]], float(payload["fetched"])
    except (OSError, ValueError, KeyError, TypeError):
        return [], None


def get(force: bool = False) -> tuple[list[Server], float | None]:
    """The list and when it was fetched (epoch seconds): the cache when recent, else fresh."""
    servers, fetched = cached()
    if not force and servers and fetched and time.time() - fetched < MAX_AGE_S:
        return servers, fetched
    try:
        with outbound.open_url(SOURCE_URL, timeout=25) as response:
            fresh = parse(response.read().decode("utf-8", "replace"))
        if not fresh:
            raise ValueError("the list came back empty")
    except Exception:
        if servers:
            return servers, fetched       # offline: the last list downloaded
        raise
    now = time.time()
    try:
        path = _cache_path()
        path.write_text(json.dumps({"fetched": now, "servers": [asdict(s) for s in fresh]}))
        hand_back(path)
    except OSError:
        pass                              # a cache that can't be written isn't worth failing over
    return fresh, now


def parse(text: str) -> list[Server]:
    try:
        rows = json.loads(text)
    except ValueError:
        return []
    servers = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        host = str(row.get("IP/HOST") or "").strip()
        if not host:
            continue
        first, last = _ports(str(row.get("PORT") or ""))
        servers.append(Server(
            host=host, first_port=first, last_port=last,
            options=str(row.get("OPTIONS") or "").strip(), speed=str(row.get("GB/S") or "").strip(),
            continent=str(row.get("CONTINENT") or "").strip(),
            country=str(row.get("COUNTRY") or "").strip(), site=str(row.get("SITE") or "").strip(),
            provider=str(row.get("PROVIDER") or "").strip()))
    return servers


def _ports(text: str) -> tuple[int, int]:
    """PORT is "5201" or a range such as "5201-5209"."""
    parts = [p.strip() for p in text.split("-") if p.strip()]
    try:
        first = int(parts[0])
    except (IndexError, ValueError):
        return 5201, 5201
    try:
        last = int(parts[1]) if len(parts) > 1 else first
    except ValueError:
        last = first
    return first, max(first, last)


def continents(servers: list[Server]) -> list[str]:
    return sorted({s.continent for s in servers if s.continent})


class Slots:
    """
    Hands out host:port pairs so tests running at once never share a port. A random choice
    takes a random server in the region, preferring one nobody's using; a busy port (someone
    else's test in progress) is avoided for the rest of the measurement.
    """

    def __init__(self, servers: list[Server], continent: str | None = None) -> None:
        pool = [s for s in servers if not continent or s.continent == continent]
        self.servers = pool or list(servers)
        self._lock = threading.Lock()
        self._taken: set[str] = set()
        self._busy: set[str] = set()

    def take(self, wanted: str) -> str | None:
        """A free host:port for ``wanted`` (a host, host:port, or RANDOM); None if none is free."""
        with self._lock:
            if wanted != RANDOM:
                host, _, port = wanted.partition(":")
                known = next((s for s in self.servers if s.host == host), None)
                ports = [int(port)] if port else (list(known.ports) if known else [5201])
                # A server you chose stays in use when it's busy: its free ports first, then
                # the busy ones again, since the other test will finish
                keys = [f"{host}:{number}" for number in ports]
                for key in sorted(keys, key=lambda k: k in self._busy):
                    if key not in self._taken:
                        self._taken.add(key)
                        return key
                return None
            hosts_in_use = {key.rpartition(":")[0] for key in self._taken}
            choices = [s for s in self.servers if s.host not in hosts_in_use] or self.servers
            for server in random.sample(choices, len(choices)):
                for number in server.ports:
                    key = f"{server.host}:{number}"
                    if key not in self._taken and key not in self._busy:
                        self._taken.add(key)
                        return key
            return None

    def give_back(self, key: str, busy: bool = False) -> None:
        with self._lock:
            self._taken.discard(key)
            if busy:
                self._busy.add(key)
