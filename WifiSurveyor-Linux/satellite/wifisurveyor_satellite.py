#!/usr/bin/env python3
"""Wi-Fi Surveyor satellite: a tinySA on a Raspberry Pi (or any Linux box), driven over the LAN.

The app on the tablet finds it, then tells it to send a test signal (one frequency, or a plan
that steps through several) or to sweep a range and send the levels back. The tablet's own
analyzers measure the signal meanwhile, so a band can be tested from both ends in real time.

Run it on the Pi:
    python3 wifisurveyor_satellite.py            # prints the pairing code, then serves
    python3 wifisurveyor_satellite.py --fake     # no tinySA: a pretend one, for trying it out
install.sh sets it up as a service that starts at boot.

Network: HTTP on TCP 8737 for commands, UDP 8737 to be found. Every command carries an
HMAC-SHA256 of itself keyed with the pairing code, with a timestamp and a nonce, so only a
tablet that was given the code can make it transmit, and a recorded command can't be replayed.
Being found needs no code and reveals only the name and what's attached.

Safety, enforced here whatever the app asks: over the air only the US ISM bands at -20 dBm or
less; any other frequency only when the request says it's going down a cable, at 0 dBm or less.
The signal stops by itself after 30 minutes, when the app hasn't been heard from for 90
seconds, and when this program exits. Needs only Python 3.9+ and pyserial (python3-serial).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0"
PORT = 8737
DISCOVER = b"WIFISURVEYOR-SATELLITE?"
CLOCK_SKEW_S = 120

# The same rules as the app's rf/txrules.py (a test keeps them equal)
OVER_THE_AIR = ((902.0, 928.0, "900 MHz ISM"), (2400.0, 2483.5, "2.4 GHz ISM"),
                (5725.0, 5850.0, "5.8 GHz ISM"))
EDGE_MHZ = 0.5
MAX_OTA_DBM = -20.0
MAX_CABLED_DBM = 0.0
TX_MINUTES = 30
LEASE_SECONDS = 90

MODULATIONS = ("off", "AM_1kHz", "NFM", "WFM")
PROMPT = b"ch> "


def check(freq_mhz: float, level_dbm: float, cabled: bool) -> str:
    if cabled:
        return f"Down a cable it's capped at {MAX_CABLED_DBM:.0f} dBm." if level_dbm > MAX_CABLED_DBM else ""
    if not any(low + EDGE_MHZ <= freq_mhz <= high - EDGE_MHZ for low, high, _n in OVER_THE_AIR):
        return (f"{freq_mhz:.3f} MHz can't be sent over the air: only the 900 MHz, 2.4 GHz and "
                "5.8 GHz ISM bands. Use a cable for other bands.")
    if level_dbm > MAX_OTA_DBM:
        return f"Over the air it's capped at {MAX_OTA_DBM:.0f} dBm."
    return ""


# ---------- the pairing code ----------

def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "wifisurveyor-satellite"


def make_code() -> str:
    """Twelve characters in three groups, easy to type on a tablet: no 0/O or 1/I."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def load_code(renew: bool = False) -> str:
    path = config_dir() / "code"
    if not renew:
        try:
            code = path.read_text().strip()
            if code:
                return code
        except OSError:
            pass
    code = make_code()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code + "\n")
    os.chmod(path, 0o600)
    return code


def normalise(code: str) -> bytes:
    return code.strip().upper().replace("-", "").replace(" ", "").encode()


def signature(code: str, stamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    message = f"{stamp}:{nonce}:{method}:{path}:".encode() + body
    return hmac.new(normalise(code), message, hashlib.sha256).hexdigest()


# ---------- the tinySA ----------

class TinySa:
    """Just what the satellite needs of the tinySA's USB shell: version, generator, scan."""

    def __init__(self, port: str) -> None:
        import serial                                   # python3-serial

        self.port_name = port
        self.link = serial.Serial(port, 115200, timeout=0.2, write_timeout=1)
        self.io = threading.Lock()
        self.link.write(b"\r")
        time.sleep(0.05)
        self.version = self.command("version").splitlines()[0].strip()
        if "tinySA" not in self.version:
            raise RuntimeError(f"{port} isn't a tinySA ({self.version!r})")
        self.ultra = self.version.startswith("tinySA4")
        self.model = "tinySA Ultra" if self.ultra else "tinySA"
        self.points = 450 if self.ultra else 290
        self.low_out = 800.0 if self.ultra else 350.0
        self.top_out = 4400.0 if self.ultra else 960.0

    def command(self, text: str, timeout: float = 4.0) -> str:
        with self.io:
            self.link.reset_input_buffer()
            self.link.write(text.encode("ascii") + b"\r")
            buffer, deadline = b"", time.monotonic() + timeout
            while time.monotonic() < deadline:
                buffer += self.link.read(max(1, self.link.in_waiting))
                if buffer.endswith(PROMPT) and b"\n" in buffer:
                    break
            else:
                raise TimeoutError(f"The tinySA didn't finish '{text}'.")
        lines = buffer[:-len(PROMPT)].decode("ascii", "replace").replace("\r", "").split("\n")
        if lines and lines[0].strip() == text:
            lines = lines[1:]
        return "\n".join(line for line in lines if line.strip())

    def generate(self, freq_mhz: float, level_dbm: float, modulation: str) -> None:
        if not 0.1 <= freq_mhz <= self.top_out:
            raise ValueError(f"The {self.model} generates 0.1-{self.top_out:.0f} MHz.")
        mode = "low" if freq_mhz <= self.low_out else "high"
        for text in (f"mode {mode} output", f"freq {int(round(freq_mhz * 1e6))}",
                     f"level {level_dbm:g}", f"modulation {modulation}", "output on"):
            self.command(text)

    def retune(self, freq_mhz: float, level_dbm: float) -> None:
        mode = "low" if freq_mhz <= self.low_out else "high"
        self.command(f"mode {mode} output")
        self.command(f"freq {int(round(freq_mhz * 1e6))}")
        self.command(f"level {level_dbm:g}")
        self.command("output on")

    def stop(self) -> None:
        for text in ("output off", "mode low input"):
            try:
                self.command(text)
            except Exception:
                pass

    def scan(self, start_mhz: float, stop_mhz: float, points: int) -> list[float]:
        points = max(10, min(self.points, int(points)))
        text = self.command(f"scan {int(start_mhz * 1e6)} {int(stop_mhz * 1e6)} {points} 2", 30)
        levels = []
        for line in text.splitlines():
            try:
                levels.append(float(line.split()[0]))
            except (ValueError, IndexError):
                continue
        return levels


class FakeTinySa:
    """A pretend tinySA for trying the satellite (and testing it) without the hardware."""

    model, version, points, low_out, top_out, port_name = ("tinySA (pretend)", "fake", 450,
                                                           800.0, 4400.0, "fake")

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def generate(self, freq_mhz, level_dbm, modulation) -> None:
        if not 0.1 <= freq_mhz <= self.top_out:
            raise ValueError(f"The {self.model} generates 0.1-{self.top_out:.0f} MHz.")
        self.sent.append(("generate", freq_mhz, level_dbm, modulation))

    def retune(self, freq_mhz, level_dbm) -> None:
        self.sent.append(("retune", freq_mhz, level_dbm))

    def stop(self) -> None:
        self.sent.append(("stop",))

    def scan(self, start_mhz, stop_mhz, points) -> list[float]:
        import random

        return [-100.0 + random.random() * 3 for _ in range(max(10, min(450, int(points))))]


def find_tinysa(port: str | None = None):
    candidates = [port] if port else sorted(glob.glob("/dev/ttyACM*"))
    errors = []
    for each in candidates:
        try:
            return TinySa(each)
        except Exception as error:
            errors.append(f"{each}: {error}")
    raise RuntimeError("No tinySA found" + (f" ({'; '.join(errors)})" if errors else
                                            " on /dev/ttyACM*. Is it plugged in and switched on?"))


# ---------- what's being sent, and when it stops ----------

class Transmitter:
    def __init__(self, device_getter) -> None:
        self._device_getter = device_getter
        self.lock = threading.RLock()
        self.state: dict | None = None        # what's on air: freq, level, until, plan step
        self.heard = time.monotonic()
        self.note = ""
        self._plan_stop = threading.Event()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def device(self):
        return self._device_getter()

    def renew(self) -> None:
        self.heard = time.monotonic()

    def _watchdog(self) -> None:
        while True:
            time.sleep(1)
            with self.lock:
                state = self.state
                if state is None:
                    continue
                if time.time() >= state["until"]:
                    self._off(f"Stopped by itself after {state['minutes']:g} minutes.")
                elif time.monotonic() - self.heard > LEASE_SECONDS:
                    self._off(f"Stopped: the app hadn't checked in for {LEASE_SECONDS} s.")

    def _off(self, why: str) -> None:
        self._plan_stop.set()
        if self.state is not None:
            try:
                self.device().stop()
            except Exception:
                pass
        self.state = None
        self.note = why

    def stop(self) -> None:
        with self.lock:
            self._off("Stopped.")

    def start(self, freq_mhz: float, level_dbm: float, modulation: str, minutes: float,
              cabled: bool) -> dict:
        trouble = check(freq_mhz, level_dbm, cabled)
        if trouble:
            raise PermissionError(trouble)
        if modulation not in MODULATIONS:
            raise ValueError(f"Modulation must be one of {', '.join(MODULATIONS)}.")
        minutes = max(0.1, min(TX_MINUTES, float(minutes)))
        with self.lock:
            self._off("Replaced.")
            self.device().generate(freq_mhz, level_dbm, modulation)
            self.state = {"freq_mhz": freq_mhz, "level_dbm": level_dbm, "modulation": modulation,
                          "cabled": cabled, "minutes": minutes, "until": time.time() + minutes * 60,
                          "started": time.time()}
            self.note = ""
            self.renew()
            return dict(self.state)

    def plan(self, steps: list[dict], repeat: int, minutes: float, cabled: bool) -> dict:
        """Steps through frequencies, each for its seconds. Every step is checked first."""
        if not steps:
            raise ValueError("The plan has no steps.")
        clean = []
        for step in steps[:500]:
            freq, level = float(step["freq_mhz"]), float(step.get("level_dbm", -30))
            seconds = max(0.2, min(600.0, float(step.get("seconds", 2))))
            trouble = check(freq, level, cabled)
            if trouble:
                raise PermissionError(trouble)
            clean.append((freq, level, seconds))
        minutes = max(0.1, min(TX_MINUTES, float(minutes)))
        with self.lock:
            self._off("Replaced.")
            self._plan_stop = stop = threading.Event()
            first = clean[0]
            self.device().generate(first[0], first[1], "off")
            began = time.time()
            self.state = {"freq_mhz": first[0], "level_dbm": first[1], "modulation": "off",
                          "cabled": cabled, "minutes": minutes, "until": began + minutes * 60,
                          "started": began, "plan": {"steps": len(clean), "step": 0, "pass": 1,
                                                     "repeat": repeat, "began": began,
                                                     "schedule": [list(s) for s in clean]}}
            self.note = ""
            self.renew()
            threading.Thread(target=self._run_plan, args=(clean, max(1, int(repeat)), stop),
                             daemon=True).start()
            return dict(self.state)

    def _run_plan(self, steps, repeat: int, stop: threading.Event) -> None:
        for number in range(repeat):
            for index, (freq, level, seconds) in enumerate(steps):
                if stop.is_set():
                    return
                with self.lock:
                    if stop.is_set() or self.state is None:
                        return
                    if index or number:
                        try:
                            self.device().retune(freq, level)
                        except Exception as error:
                            self._off(f"Stopped: {error}")
                            return
                    self.state.update(freq_mhz=freq, level_dbm=level)
                    self.state["plan"].update(step=index, **{"pass": number + 1},
                                              step_started=time.time())
                if stop.wait(seconds):
                    return
        with self.lock:
            if not stop.is_set():
                self._off("Plan finished.")

    def status(self) -> dict:
        with self.lock:
            state = dict(self.state) if self.state else None
            if state:
                state["left_s"] = max(0.0, state["until"] - time.time())
            return {"sending": state, "note": self.note}


# ---------- the network side ----------

class Satellite:
    def __init__(self, name: str, code: str, device_getter, describe) -> None:
        self.name, self.code = name, code
        self.tx = Transmitter(device_getter)
        self.describe = describe
        self.nonces: dict[str, float] = {}
        self.nonce_lock = threading.Lock()

    def authorised(self, headers, method: str, path: str, body: bytes) -> bool:
        value = headers.get("X-Satellite-Auth", "")
        try:
            stamp, nonce, digest = value.split(":", 2)
            when = float(stamp)
        except ValueError:
            return False
        if abs(time.time() - when) > CLOCK_SKEW_S or not nonce:
            return False
        expected = signature(self.code, stamp, nonce, method, path, body)
        if not hmac.compare_digest(expected, digest):
            return False
        with self.nonce_lock:
            cutoff = time.time() - 2 * CLOCK_SKEW_S
            for old in [n for n, t in self.nonces.items() if t < cutoff]:
                del self.nonces[old]
            if nonce in self.nonces:
                return False                        # a replay
            self.nonces[nonce] = time.time()
        return True

    def info(self) -> dict:
        return {"service": "wifisurveyor-satellite", "version": VERSION, "name": self.name,
                "port": PORT, **self.describe()}

    def handle(self, method: str, path: str, payload: dict) -> dict:
        self.tx.renew()
        if path == "/status":
            return {**self.info(), **self.tx.status()}
        if path == "/stop":
            self.tx.stop()
            return self.tx.status()
        if path == "/tone":
            return {"sending": self.tx.start(float(payload["freq_mhz"]),
                                             float(payload.get("level_dbm", -30)),
                                             str(payload.get("modulation", "off")),
                                             float(payload.get("minutes", TX_MINUTES)),
                                             bool(payload.get("cabled", False)))}
        if path == "/plan":
            return {"sending": self.tx.plan(list(payload.get("steps") or []),
                                            int(payload.get("repeat", 1)),
                                            float(payload.get("minutes", TX_MINUTES)),
                                            bool(payload.get("cabled", False)))}
        if path == "/scan":
            with self.tx.lock:
                if self.tx.state is not None:
                    raise RuntimeError("It's sending a signal; stop it to sweep.")
                start, stop = float(payload["start_mhz"]), float(payload["stop_mhz"])
                levels = self.tx.device().scan(start, stop, int(payload.get("points", 450)))
            return {"start_mhz": start, "stop_mhz": stop, "levels": levels}
        raise LookupError(path)


def make_handler(satellite: Satellite):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"WifiSurveyorSatellite/{VERSION}"

        def log_message(self, *_args) -> None:
            pass

        def _send(self, code: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve(self, method: str) -> None:
            length = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            if path == "/info":
                self._send(200, satellite.info())
                return
            if not satellite.authorised(self.headers, method, path, body):
                self._send(401, {"error": "Wrong pairing code, or this tablet's clock is more "
                                          "than two minutes off the satellite's."})
                return
            try:
                payload = json.loads(body or b"{}")
                self._send(200, satellite.handle(method, path, payload))
            except PermissionError as error:
                self._send(403, {"error": str(error)})
            except LookupError:
                self._send(404, {"error": f"No such command: {path}"})
            except Exception as error:
                self._send(400, {"error": str(error)})

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

    return Handler


def answer_discovery(satellite: Satellite, port: int, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    sock.settimeout(1.0)
    while not stop.is_set():
        try:
            data, sender = sock.recvfrom(256)
        except socket.timeout:
            continue
        except OSError:
            break
        if data.strip() == DISCOVER:
            info = {**satellite.info(), "port": port}
            try:
                sock.sendto(json.dumps(info).encode(), sender)
            except OSError:
                pass
    sock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wi-Fi Surveyor satellite: a tinySA over the LAN")
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--device", help="the tinySA's serial port (default: the first /dev/ttyACM*)")
    parser.add_argument("--fake", action="store_true", help="a pretend tinySA, for trying it out")
    parser.add_argument("--show-code", action="store_true", help="print the pairing code and exit")
    parser.add_argument("--new-code", action="store_true", help="make a new pairing code")
    args = parser.parse_args(argv)

    code = load_code(renew=args.new_code)
    if args.show_code:
        print(code)
        return 0

    holder: dict = {"device": FakeTinySa() if args.fake else None, "error": ""}
    holder_lock = threading.Lock()

    def device():
        with holder_lock:
            if holder["device"] is None:
                try:
                    holder["device"] = find_tinysa(args.device)
                    holder["error"] = ""
                except Exception as error:
                    holder["error"] = str(error)
                    raise RuntimeError(holder["error"]) from error
            return holder["device"]

    def describe() -> dict:
        try:
            found = device()
            return {"generator": {"model": found.model, "version": found.version,
                                  "port": found.port_name, "top_mhz": found.top_out,
                                  "low_out_mhz": found.low_out}}
        except Exception as error:
            return {"generator": None, "problem": str(error)}

    satellite = Satellite(args.name, code, device, describe)
    server = ThreadingHTTPServer(("", args.port), make_handler(satellite))
    stop = threading.Event()
    threading.Thread(target=answer_discovery, args=(satellite, args.port, stop), daemon=True).start()

    def finish(*_args) -> None:
        satellite.tx.stop()
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    print(f"Wi-Fi Surveyor satellite '{args.name}' on port {args.port}. Pairing code: {code}",
          flush=True)
    if describe().get("generator") is None:
        print(f"No tinySA yet ({holder['error']}); it's looked for again on each request.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        satellite.tx.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
