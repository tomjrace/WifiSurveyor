"""
Real throughput at a spot: iperf3 to a server on the network, a few seconds each way, over
the survey adapter. The number users relate to, where signal and noise are only the cause.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable

from .wifi import have, run

SECONDS = 4
MAX_ADAPTERS = 6
QUEUE_LIMIT_S = 120     # how long a test waits for its turn on a shared server port
RANDOM = "random"       # matches iperf_servers.RANDOM, without importing the list here
_bind_dev: bool | None = None


def can_bind_device() -> bool:
    """
    Whether iperf3 can hold its traffic to one interface (--bind-dev, iperf3 3.13+). With
    several adapters connected, binding only the source address isn't enough: the kernel
    routes by destination and can send one adapter's test out of another.
    """
    global _bind_dev
    if _bind_dev is None:
        _bind_dev = have("iperf3") and "--bind-dev" in run("iperf3", "--help")[1]
    return _bind_dev


def address(device: str) -> str | None:
    """The adapter's IPv4 address, to bind iperf3's traffic to it."""
    code, text = run("ip", "-4", "-o", "addr", "show", "dev", device)
    found = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", text) if code == 0 else None
    return found.group(1) if found else None


def _mbps(output: str) -> float:
    report = json.loads(output)
    if report.get("error"):
        raise RuntimeError(report["error"])
    total = report.get("end", {}).get("sum_received") or report.get("end", {}).get("sum") or {}
    return float(total.get("bits_per_second", 0)) / 1e6


def test(device: str, server: str, seconds: int = SECONDS, slots=None) -> dict:
    """
    Download then upload Mbps, or {"error"} saying what went wrong. With ``slots`` (an
    iperf_servers.Slots), the server can be RANDOM or a bare host, and a port that's busy with
    someone else's test is swapped for another, up to four tries.
    """
    if slots is None:
        return _test_once(device, server, seconds)
    result: dict = {"server": server, "error": "no free server port to test against"}
    waited = 0.0
    for _attempt in range(4):
        key = slots.take(server)
        # A server of your own runs one test at a time: adapters aimed at it take turns
        while key is None and server != RANDOM and waited < QUEUE_LIMIT_S:
            time.sleep(0.5)
            waited += 0.5
            key = slots.take(server)
        if key is None:
            break
        result = _test_once(device, key, seconds)
        busy = "busy" in result.get("error", "")
        slots.give_back(key, busy=busy)
        if not busy:
            break
        time.sleep(2)
    return result


def _test_once(device: str, server: str, seconds: int) -> dict:
    result: dict = {"server": server}
    if not have("iperf3"):
        result["error"] = "iperf3 isn't installed (sudo apt install iperf3)"
        return result
    bind = address(device)
    if bind is None:
        result["error"] = f"{device} has no IP address: is it connected?"
        return result
    host, _, port = server.partition(":")
    for key, reverse in (("down_mbps", True), ("up_mbps", False)):
        args = ["iperf3", "-c", host, "-B", bind, "-t", str(seconds), "-J", "--connect-timeout", "3000"]
        if can_bind_device():
            args += ["--bind-dev", device]
        if port:
            args += ["-p", port]
        if reverse:
            args.append("-R")
        code, output = run(*args, timeout=seconds + 15)
        try:
            result[key] = round(_mbps(output[output.find("{"):]), 1)
        except (ValueError, RuntimeError, KeyError) as error:
            result["error"] = f"iperf3 to {server}: {error if str(error) else 'no result'}"
            break
    return result


def run_plan(plan: list[dict], fallback_server: str, slots, seconds: int = SECONDS,
             say: Callable[[str], None] | None = None,
             heard: list[dict] | None = None) -> list[dict]:
    """
    Joins and tests each network in ``plan`` ({"ssid", "adapter", "server"}) on its adapter.
    Different adapters run at once; networks sharing an adapter take turns on it, since an
    adapter is on one network at a time. Results come back in plan order. With ``heard`` (the
    networks scanned at this point), a network that isn't heard here, isn't encrypted or has
    no saved password is skipped, as {"ssid", "adapter", "skipped": why}, not tried.
    """
    from .connectivity import join, why_not_test

    results: list[dict | None] = [None] * len(plan)
    by_adapter: dict[str, list[int]] = {}
    for index, row in enumerate(plan):
        by_adapter.setdefault(row["adapter"], []).append(index)
    lock = threading.Lock()
    done = [0]

    def adapter_worker(adapter: str, indexes: list[int]) -> None:
        for index in indexes:
            row = plan[index]
            server = (row.get("server") or "").strip() or fallback_server
            outcome = {"ssid": row["ssid"], "adapter": adapter}
            skip = why_not_test(row["ssid"], heard) if heard is not None else None
            if skip:
                outcome["skipped"] = skip
            elif not server:
                outcome["error"] = "no iperf3 server set for it"
            else:
                trouble = join(adapter, row["ssid"])
                if trouble:
                    outcome["error"] = trouble
                else:
                    outcome.update(test(adapter, server, seconds, slots))
            with lock:
                results[index] = outcome
                done[0] += 1
                if say:
                    say(f"Speed tests: {done[0]} of {len(plan)} done")

    threads = [threading.Thread(target=adapter_worker, args=item, daemon=True)
               for item in by_adapter.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [r for r in results if r is not None]


def describe(result: dict | None) -> str:
    if not result:
        return ""
    parts = []
    if result.get("down_mbps") is not None:
        parts.append(f"{result['down_mbps']:.0f} Mbps down")
    if result.get("up_mbps") is not None:
        parts.append(f"{result['up_mbps']:.0f} Mbps up")
    if result.get("error"):
        parts.append(result["error"])
    if result.get("skipped"):
        parts.append(f"skipped, {result['skipped']}")
    where = f" on {result['ssid']}" if result.get("ssid") else ""
    return f"Throughput{where}: " + ", ".join(parts)
