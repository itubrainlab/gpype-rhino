"""Byte links to the headset's Bluetooth serial channel.

Three ways to the same RFCOMM channel:

- :class:`SerialLink`, the operating system's serial port for the paired
  headset: ``COMn`` on Windows, or a ``/dev/rfcommN`` bound by hand.
- :class:`BluetoothLink`, Linux only: an RFCOMM socket to the headset,
  found by name among BlueZ's paired devices. Needs no ``rfcomm bind``
  and no membership of the ``dialout`` group that owns /dev/rfcommN.
- :class:`BridgeLink`, macOS only: the bundled RhinoConnect helper
  opens the channel through IOBluetooth and relays it over a local TCP
  socket. macOS's ``/dev/cu.UN-*`` port proved unreliable -- after one
  session it stopped reaching the headset at all, until Bluetooth was
  reset -- while the channel opened through bluetoothd kept working.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import re
import select
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import serial

HELPER_DIR = Path(__file__).parent / "macos"
HELPER_NAME = "RhinoConnect.app"
#: Where the helper is built when the package directory is read-only, as
#: an installed package's usually is.
HELPER_CACHE = Path.home() / "Library" / "Caches" / "gprhino"


def _helper_built(directory: Path) -> bool:
    return (directory / HELPER_NAME / "Contents" / "MacOS"
            / "RhinoConnect").exists()


class DeviceNotFound(ConnectionError):
    """No paired headset by that name: retrying cannot help."""


_ADDRESS = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


def is_address(name: str) -> bool:
    """Whether name is a Bluetooth address, e.g. ``A4:6D:D4:17:BF:35``."""
    return _ADDRESS.fullmatch(name) is not None


def paired_devices() -> list[tuple[str, str]]:
    """(address, name) of each device BlueZ has paired, Linux only.

    Asks ``bluetoothctl``, which ships with BlueZ and needs no root.

    Raises:
        DeviceNotFound: If bluetoothctl is not installed.
    """
    if shutil.which("bluetoothctl") is None:
        raise DeviceNotFound(
            "bluetoothctl (BlueZ) is not installed, so paired headsets "
            "cannot be found by name. Install bluez, or give the "
            "headset's Bluetooth address instead.")
    # BlueZ 5.65 and later take "devices Paired"; earlier ones only know
    # "paired-devices" and list every known device for "devices Paired".
    for command in (["devices", "Paired"], ["paired-devices"]):
        result = subprocess.run(["bluetoothctl", *command],
                                capture_output=True, text=True, timeout=10)
        devices = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(" ", 2)
            if (len(parts) == 3 and parts[0] == "Device"
                    and is_address(parts[1])):
                devices.append((parts[1], parts[2]))
        if result.returncode == 0 and "Invalid command" not in result.stdout:
            return devices
    return []


#: The remote device's address in a Windows Bluetooth port's instance ID:
#: BTHENUM\{service}_LOCALMFG&0002\7&2F0E3D5B&0&A46DD417BF35_C00000000.
_BTHENUM_ADDRESS = re.compile(r"&([0-9A-Fa-f]{12})_")
#: Where Windows keeps the name each Bluetooth device was paired under.
_BTHPORT_DEVICES = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"


def windows_bluetooth_ports() -> list[tuple[str, str, str]]:
    """(port, address, name) of each outgoing Bluetooth serial port,
    Windows only.

    Windows describes these ports only as "Standard Serial over Bluetooth
    link (COMn)". The paired device's address is in the port's instance
    ID, and the name it was paired under in the registry; the name is
    empty where the registry cannot be read. Incoming ports, which have
    no device, are left out. A Bluetooth port whose ID does not show the
    device is listed with an empty address and name.
    """
    from serial.tools import list_ports
    found = []
    for p in list_ports.comports():
        hwid = p.hwid or ""
        addresses = _BTHENUM_ADDRESS.findall(hwid)
        if not hwid.upper().startswith("BTHENUM") or not addresses:
            if "bluetooth" in (p.description or "").lower():
                found.append((p.device, "", ""))
            continue
        if int(addresses[-1], 16) == 0:
            continue
        raw = addresses[-1].upper()
        address = ":".join(raw[i:i + 2] for i in range(0, 12, 2))
        found.append((p.device, address, _windows_device_name(raw)))
    return sorted(found)


def _windows_device_name(raw_address: str) -> str:
    """The name a Bluetooth device was paired under, from the registry."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            f"{_BTHPORT_DEVICES}\\{raw_address.lower()}") as key:
            value, _ = winreg.QueryValueEx(key, "Name")
    except (ImportError, OSError):
        return ""
    if isinstance(value, bytes):
        value = value.split(b"\0", 1)[0].decode("utf-8", "replace")
    return str(value).strip()


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


class _SocketLink:
    """Reading and writing a connected stream socket."""

    def __init__(self):
        self._socket: Optional[socket.socket] = None

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
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class BluetoothLink(_SocketLink):
    """The headset's RFCOMM channel as a BlueZ socket, Linux only."""

    #: The headset's serial port profile is on RFCOMM channel 1.
    CHANNEL = 1
    #: How long connecting may take; the first page often takes seconds.
    CONNECT_TIMEOUT_S = 15.0

    def __init__(self, device_name: str):
        """
        Args:
            device_name: The headset's paired name, e.g. ``UN-2024.08.41``
                (or a Bluetooth address). Looked up on open, so a headset
                paired after this is made is still found.
        """
        super().__init__()
        self.name = device_name
        self.address: Optional[str] = (device_name if is_address(device_name)
                                       else None)

    def open(self, timeout: float) -> None:
        if self.address is None:
            self.address = address_for(self.name)
        try:
            self._socket = rfcomm_connect(
                self.address, self.CHANNEL,
                max(timeout, self.CONNECT_TIMEOUT_S))
        except OSError as error:
            raise ConnectionError(
                f"Bluetooth connection to {self.name} ({self.address}) "
                f"failed: {error}") from error


#: Linux's numbers for Bluetooth sockets, for Pythons built without them.
_AF_BLUETOOTH = 31
_BTPROTO_RFCOMM = 3


class _SockaddrRc(ctypes.Structure):
    """Linux's struct sockaddr_rc."""
    _fields_ = [("rc_family", ctypes.c_ushort),
                ("rc_bdaddr", ctypes.c_ubyte * 6),
                ("rc_channel", ctypes.c_ubyte)]


def rfcomm_connect(address: str, channel: int,
                   timeout: float) -> socket.socket:
    """A stream socket connected to an RFCOMM channel, Linux only.

    Python's socket module has Bluetooth only where it was built with
    BlueZ's headers, which relocatable builds such as
    python-build-standalone are not. The kernel has it regardless, so
    without the module's support the socket is made from the kernel's
    numbers and connected through libc; reading and writing it then need
    nothing Bluetooth-specific.

    Raises:
        OSError: If the connection fails or takes longer than timeout.
    """
    if hasattr(socket, "AF_BLUETOOTH"):
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                             socket.BTPROTO_RFCOMM)
        sock.settimeout(timeout)
        try:
            sock.connect((address, channel))
        except OSError:
            sock.close()
            raise
        return sock

    sock = socket.socket(_AF_BLUETOOTH, socket.SOCK_STREAM, _BTPROTO_RFCOMM)
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        # bdaddr_t holds the address least significant byte first.
        bdaddr = bytes.fromhex(address.replace(":", ""))[::-1]
        addr = _SockaddrRc(_AF_BLUETOOTH,
                           (ctypes.c_ubyte * 6).from_buffer_copy(bdaddr),
                           channel)
        sock.setblocking(False)
        if libc.connect(sock.fileno(), ctypes.byref(addr),
                        ctypes.sizeof(addr)) != 0:
            code = ctypes.get_errno()
            if code != errno.EINPROGRESS:
                raise OSError(code, os.strerror(code))
            if not select.select([], [sock], [], timeout)[1]:
                raise TimeoutError(errno.ETIMEDOUT, "timed out")
            code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if code:
                raise OSError(code, os.strerror(code))
        sock.settimeout(timeout)
    except BaseException:
        sock.close()
        raise
    return sock


def address_for(name: str) -> str:
    """The Bluetooth address of the paired device with this name.

    Names compare as :func:`gprhino.protocol.plain_name` does, and the
    ``UN-`` prefix may be left out: ``2024.08.41`` finds ``UN-2024.08.41``.

    Raises:
        DeviceNotFound: If no paired device has the name.
    """
    from .protocol import plain_name
    wanted = plain_name(name)
    for address, device in paired_devices():
        plain = plain_name(device)
        if wanted in (plain, plain.removeprefix("un-")):
            return address
    raise DeviceNotFound(
        f"No paired Bluetooth device is named {name}. Pair the headset "
        f"first, e.g. with bluetoothctl: scan on, pair <address>, "
        f"trust <address>.")


class BridgeLink(_SocketLink):
    """The headset's RFCOMM channel through the RhinoConnect helper."""

    #: How long the helper may take to open the channel, in seconds.
    CHANNEL_TIMEOUT_S = 20.0

    def __init__(self, device_name: str):
        super().__init__()
        self.name = device_name
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
        fd, status = tempfile.mkstemp(prefix="rhino-connect-", suffix=".log")
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
                        "The RhinoConnect helper did not start.")
                time.sleep(0.1)

    def _wait_for(self, word: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while word not in self._status_text():
            self._fail_if_helper_failed()
            if time.monotonic() >= deadline:
                raise ConnectionError(
                    f"The RhinoConnect helper did not report {word}.")
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
                raise ConnectionError(f"RhinoConnect: {line}")

    def close(self) -> None:
        # The helper closes the channel and exits when the client goes.
        super().close()
        if self._status is not None:
            self._status.unlink(missing_ok=True)
            self._status = None


def _free_tcp_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
