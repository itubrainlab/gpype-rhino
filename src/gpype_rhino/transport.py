"""Byte links to the headset's Bluetooth serial channel.

Two ways to the same RFCOMM channel:

- :class:`SerialLink`, the operating system's serial port for the paired
  headset. Right on Linux (``/dev/rfcommN``) and Windows (``COMn``).
- :class:`BridgeLink`, macOS only: the bundled UnicornConnect helper
  opens the channel through IOBluetooth and relays it over a local TCP
  socket. macOS's ``/dev/cu.UN-*`` port proved unreliable -- after one
  session it stopped reaching the headset at all, until Bluetooth was
  reset -- while the channel opened through bluetoothd kept working.
"""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import serial

HELPER_DIR = Path(__file__).parent / "macos"
HELPER_NAME = "UnicornConnect.app"
#: Where the helper is built when the package directory is read-only, as
#: an installed package's usually is.
HELPER_CACHE = Path.home() / "Library" / "Caches" / "gpype_rhino"


def _helper_built(directory: Path) -> bool:
    return (directory / HELPER_NAME / "Contents" / "MacOS"
            / "UnicornConnect").exists()


class DeviceNotFound(ConnectionError):
    """No paired headset by that name: retrying cannot help."""


class SerialLink:
    """The headset's serial port."""

    def __init__(self, port: str):
        self.name = port
        self._serial: Optional[serial.Serial] = None

    def open(self, timeout: float) -> None:
        try:
            self._serial = serial.Serial(self.name, baudrate=115200,
                                         timeout=timeout)
        except serial.SerialException as error:
            raise ConnectionError(str(error)) from error

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def read(self, size: int, timeout: float) -> bytes:
        """Up to size bytes; fewer, or none, if timeout passes first."""
        self._serial.timeout = timeout
        return self._serial.read(size)

    def read_some(self, timeout: float) -> bytes:
        """Whatever has arrived, waiting up to timeout for the first byte."""
        self._serial.timeout = timeout
        return self._serial.read(self._serial.in_waiting or 1)

    def drain(self) -> None:
        self._serial.reset_input_buffer()

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None


class BridgeLink:
    """The headset's RFCOMM channel through the UnicornConnect helper."""

    #: How long the helper may take to open the channel, in seconds.
    CHANNEL_TIMEOUT_S = 20.0

    def __init__(self, device_name: str):
        self.name = device_name
        self._socket: Optional[socket.socket] = None
        self._status: Optional[Path] = None

    @staticmethod
    def ensure_helper() -> Path:
        """The helper app, built first if it is not there yet (needs the
        Xcode command line tools). It is built next to its source if that
        directory is writable, else in HELPER_CACHE.

        Raises:
            RuntimeError: If it cannot be built.
        """
        for directory in (HELPER_DIR, HELPER_CACHE):
            if _helper_built(directory):
                return directory / HELPER_NAME
        target = HELPER_DIR if os.access(HELPER_DIR, os.W_OK) else HELPER_CACHE
        result = subprocess.run(
            ["sh", str(HELPER_DIR / "build.sh"), str(target)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Could not build {HELPER_NAME} (needs the Xcode "
                f"command line tools): {result.stderr.strip()}")
        return target / HELPER_NAME

    def open(self, timeout: float) -> None:
        helper = self.ensure_helper()
        port = _free_tcp_port()
        fd, status = tempfile.mkstemp(prefix="unicorn-connect-", suffix=".log")
        os.close(fd)
        self._status = Path(status)
        # Launched through LaunchServices, so macOS attributes Bluetooth
        # use to the helper (which declares it) rather than to Python.
        subprocess.run(["open", "-n", "--stdout", status, "--stderr", status,
                        str(helper), "--args", self.name, str(port)],
                       check=True)
        try:
            self._socket = self._connect(port, timeout)
            self._wait_for("CONNECTED", self.CHANNEL_TIMEOUT_S)
        except Exception:
            self.close()
            raise

    def _connect(self, port: int, timeout: float) -> socket.socket:
        deadline = time.monotonic() + timeout
        while True:
            self._fail_if_helper_failed()
            try:
                return socket.create_connection(("127.0.0.1", port),
                                                timeout=1.0)
            except OSError:
                if time.monotonic() >= deadline:
                    raise ConnectionError(
                        "The UnicornConnect helper did not start.")
                time.sleep(0.1)

    def _wait_for(self, word: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while word not in self._status_text():
            self._fail_if_helper_failed()
            if time.monotonic() >= deadline:
                raise ConnectionError(
                    f"The UnicornConnect helper did not report {word}.")
            time.sleep(0.1)

    def _status_text(self) -> str:
        try:
            return self._status.read_text()
        except OSError:
            return ""

    def _fail_if_helper_failed(self) -> None:
        for line in self._status_text().splitlines():
            if line.startswith("ERROR not paired"):
                raise DeviceNotFound(
                    f"No paired Bluetooth device is named {self.name}. "
                    f"Pair the headset in the Bluetooth settings first.")
            if line.startswith(("ERROR", "CLOSED")):
                raise ConnectionError(f"UnicornConnect: {line}")

    def write(self, data: bytes) -> None:
        self._socket.sendall(data)

    def read(self, size: int, timeout: float) -> bytes:
        data = b""
        deadline = time.monotonic() + timeout
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self.read_some(remaining)
            if not chunk:
                break
            data += chunk
        return data

    def read_some(self, timeout: float) -> bytes:
        self._socket.settimeout(timeout)
        try:
            chunk = self._socket.recv(65536)
        except socket.timeout:
            return b""
        if not chunk:
            raise ConnectionError("The Bluetooth channel closed.")
        return chunk

    def drain(self) -> None:
        while self.read_some(0.05):
            pass

    def close(self) -> None:
        # The helper closes the channel and exits when the client goes.
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._status is not None:
            self._status.unlink(missing_ok=True)
            self._status = None


def _free_tcp_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
