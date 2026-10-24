"""RF Explorer serial driver.

Protocol, as published in RF Explorer's UART API:
  to the device   '#', total length byte, ASCII command.
                  "C0" asks for the configuration,
                  "C2-F:SSSSSSS,EEEEEEE,-TTT,-BBB" sets the sweep in kHz and dBm,
                  "CM" plus 0x00/0x01 switches between the main and expansion modules.
  from the device "#C2-F:..." configuration, "$S" + count byte + count bytes of sweep
                  data where dBm = -byte/2, and "$s" + byte where count = (byte+1)*16.
"""

from __future__ import annotations

import threading
import time

import serial

from .analyzer import (  # noqa: F401  (re-exported: the rest of the app imports them from here)
    WIFI_RANGES, Analyzer, Config, PortPermissionError, SpanError, denied as _denied,
)

SEGMENTS = tuple((start, end) for _name, start, end in WIFI_RANGES)

# The device menu offers these two; 500000 is the factory setting.
BAUD_RATES = (500000, 2400)


def probe(port: str, timeout: float = 1.2) -> int | None:
    """
    The baud rate at which the port answers the RF Explorer configuration request, or None.
    Raises PortPermissionError when the port can't be opened for lack of rights, so that
    "not an analyzer" and "not allowed to look" never look the same.
    """
    for baud in BAUD_RATES:
        try:
            link = serial.Serial(port, baud, timeout=0.2, write_timeout=0.5)
        except (serial.SerialException, OSError) as error:
            if _denied(error):
                raise PortPermissionError(port) from error
            return None
        try:
            body = b"C0"
            link.reset_input_buffer()
            link.write(b"#" + bytes([len(body) + 2]) + body)
            deadline = time.monotonic() + timeout
            buffer = b""
            while time.monotonic() < deadline:
                chunk = link.read(128)
                if not chunk:
                    continue
                buffer += chunk
                if b"C2-F" in buffer or b"C2-M" in buffer or b"$S" in buffer:
                    return baud
        except (serial.SerialException, OSError):
            pass
        finally:
            try:
                link.close()
            except Exception:
                pass
    return None


class RfExplorer(Analyzer):
    name = "RF Explorer"
    # The lowest any RF Explorer's main module reaches (the 6G Combo's: 15-2700 MHz)
    LOW_MHZ = 15.0

    def __init__(self, port: str, baud: int = 500000) -> None:
        super().__init__(port)
        try:
            self.port = serial.Serial(port, baud, timeout=0.5, write_timeout=1)
        except (serial.SerialException, OSError) as error:
            if _denied(error):
                raise PortPermissionError(port) from error
            raise
        self.baud = baud
        self._lock = threading.Lock()
        self._config_event = threading.Event()
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.request_config()

    @property
    def summary(self) -> str:
        return f"{self.name} on {self.device} at {self.baud} baud"

    @property
    def min_mhz(self) -> float:
        return self.LOW_MHZ

    @property
    def firmware(self) -> str:
        """The firmware version it reported ("C2-M:main,expansion,version"), once it has."""
        parts = self.model.split(",")
        return parts[2].strip() if len(parts) >= 3 else ""

    # ---------- commands ----------

    def _send(self, body: bytes) -> None:
        message = b"#" + bytes([len(body) + 2]) + body
        with self._lock:
            self.port.write(message)

    def request_config(self) -> None:
        self._send(b"C0")

    def set_sweep(self, start_mhz: float, end_mhz: float, top: int = -10, bottom: int = -120) -> None:
        command = f"C2-F:{int(round(start_mhz * 1000)):07d},{int(round(end_mhz * 1000)):07d},{top:04d},{bottom:04d}"
        self._send(command.encode("ascii"))

    def switch_module(self, expansion: bool) -> None:
        self._send(b"CM" + bytes([1 if expansion else 0]))

    @property
    def alive(self) -> bool:
        """False once the reader has given up, which is what unplugging looks like."""
        return self._running and self._reader.is_alive()

    def close(self) -> None:
        self._running = False
        try:
            self.port.close()
        except Exception:
            pass

    # ---------- reading ----------

    def _read_loop(self) -> None:
        while self._running:
            try:
                first = self.port.read(1)
                if not first:
                    continue
                if first == b"$":
                    kind = self.port.read(1)
                    if kind == b"S":
                        count = self.port.read(1)[0]
                    elif kind == b"s":
                        count = (self.port.read(1)[0] + 1) * 16
                    else:
                        continue
                    data = self.port.read(count)
                    if len(data) == count:
                        self._add_sweep([-b / 2 for b in data])
                elif first == b"#":
                    self._handle(self.port.readline().decode("ascii", "replace").strip())
            except (serial.SerialException, OSError):
                # The port went away (unplugged, or the driver reset): stop, and say so via alive.
                self._running = False
            except Exception:
                # A frame cut short by a read timeout. Resynchronise on the next marker.
                continue
    def _handle(self, line: str) -> None:
        if line.startswith("C2-F:"):
            parts = line[5:].split(",")

            def number(index: int, fallback: float) -> float:
                try:
                    return float(parts[index])
                except (IndexError, ValueError):
                    return fallback

            self.config = Config(
                start_mhz=number(0, 0) / 1000,
                step_mhz=number(1, 0) / 1e6,
                points=int(number(4, 112)),
                expansion=number(5, 0) == 1,
                min_mhz=number(7, 0) / 1000,
                max_mhz=number(8, 0) / 1000,
                max_span_mhz=number(9, 600000) / 1000,
                rbw_khz=number(10, 600) or 600,
            )
            self._config_event.set()
        elif line.startswith("C2-M:"):
            self.model = line[5:]

    # ---------- capture ----------

    def _wait_for_config(self, matches, timeout: float) -> Config | None:
        deadline = threading.Event()
        end = timeout
        step = 0.3
        while end > 0:
            config = self.config
            if config and matches(config):
                return config
            deadline.wait(step)
            end -= step
        return None

    def _configure(self, start_mhz: float, end_mhz: float, timeout: float) -> Config:
        if not self.alive:
            raise self._gone()
        config = self.config or self._wait_for_config(lambda c: True, 5)
        if config is None:
            raise TimeoutError("The RF Explorer isn't answering. Check the port and baud rate.")

        if not config.covers(start_mhz, end_mhz):
            target = not config.expansion
            self.switch_module(target)
            config = self._wait_for_config(lambda c: c.expansion == target, timeout)
            if config is None or not config.covers(start_mhz, end_mhz):
                raise ValueError(f"Neither module covers {start_mhz:.0f}-{end_mhz:.0f} MHz.")

        if end_mhz - start_mhz > config.max_span_mhz + 0.5:
            raise SpanError(end_mhz - start_mhz, config.max_span_mhz)

        self.set_sweep(start_mhz, end_mhz)
        config = self._wait_for_config(
            lambda c: abs(c.start_mhz - start_mhz) <= max(1.0, c.step_mhz * 2), timeout
        )
        if config is None:
            raise TimeoutError("The RF Explorer didn't confirm the new range.")
        return config
