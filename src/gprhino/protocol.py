"""Licence-free driver for the g.tec Unicorn Hybrid Black.

The headset is a Bluetooth classic device exposing a serial port profile
(RFCOMM). Once it is paired, Windows offers it as a serial port
(``COMn``), Linux opens the channel as a BlueZ socket, and macOS reaches
it through the bundled RhinoConnect helper (see transport.py). The whole
protocol is three bytes to start, three to stop, and fixed 45-byte frames
in between.
Nothing here uses g.tec's libraries.

Frame layout (250 Hz, one frame per sample)::

    offset  size  content
    0       2     header 0xC0 0x00
    2       1     battery, low nibble 0..15
    3       24    8 x EEG, signed 24-bit big-endian
    27      6     3 x accelerometer, signed 16-bit little-endian
    33      6     3 x gyroscope, signed 16-bit little-endian
    39      4     sample counter, unsigned 32-bit little-endian
    43      2     footer 0x0D 0x0A
"""
from __future__ import annotations

import glob
import sys
import threading
import time
from collections import deque
from typing import Optional

import numpy as np
import serial

from .transport import (BluetoothLink, BridgeLink, DeviceNotFound,
                        SerialLink, paired_devices)

SAMPLING_RATE = 250
NUM_EEG = 8
NUM_ACCEL = 3
NUM_GYRO = 3

CMD_START = b"\x61\x7C\x87"
CMD_STOP = b"\x63\x5C\xC5"
ACK_LENGTH = 3

FRAME_LENGTH = 45
HEADER = b"\xC0\x00"
FOOTER = b"\x0D\x0A"

#: Scale factors from raw counts to physical units.
EEG_UV_PER_COUNT = 4500000.0 / 50331642.0
ACCEL_G_PER_COUNT = 1.0 / 4096.0
GYRO_DPS_PER_COUNT = 1.0 / 32.8

#: Columns of a decoded scan: EEG, accel, gyro, battery, counter, validation.
NUM_CHANNELS = NUM_EEG + NUM_ACCEL + NUM_GYRO + 3


def find_ports() -> list[str]:
    """Serial ports that look like a paired Unicorn. On Linux these are
    only the /dev/rfcommN bound by hand; see :func:`find_headsets`."""
    if sys.platform == "darwin":
        return sorted(glob.glob("/dev/cu.UN-*"))
    if sys.platform.startswith("linux"):
        return sorted(glob.glob("/dev/rfcomm*"))
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports()
            if "UN-" in (p.description or "") or "UN-" in (p.name or "")]


def find_headsets() -> list[str]:
    """Names of the paired Unicorns, from BlueZ (Linux only)."""
    return [name for _, name in paired_devices()
            if name.upper().startswith("UN-")]


def plain_name(name: str) -> str:
    """A device or port name reduced for comparison: no path, no cu./tty.
    prefix, no dots, lower case. ``UN-2024.08.41`` and
    ``/dev/cu.UN-20240841`` both become ``un-20240841``."""
    name = name.rsplit("/", 1)[-1]
    for prefix in ("cu.", "tty."):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.replace(".", "").lower()


def port_for(serial_number: str) -> str:
    """The serial port of the paired headset with this name.

    Raises:
        DeviceNotFound: If no port carries the name. Linux names its
            ports /dev/rfcommN, so there the port must be given (or the
            bluetooth transport used, which needs no port).
    """
    wanted = plain_name(serial_number)
    if sys.platform == "win32":
        from serial.tools import list_ports
        for p in list_ports.comports():
            if wanted in plain_name(p.description or ""):
                return p.device
    else:
        for port in find_ports():
            if plain_name(port) == wanted:
                return port
    raise DeviceNotFound(
        f"No serial port for Unicorn {serial_number}. Pair it first"
        + (", and pass its /dev/rfcommN as port." if
           sys.platform.startswith("linux") else "."))


def decode(frames: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Decode whole frames into physical units.

    Args:
        frames: (n, 45) uint8 array, one frame per row.
        valid: (n,) flag per frame, written to the validation column.

    Returns:
        (n, NUM_CHANNELS) float32 array.
    """
    n = frames.shape[0]
    out = np.empty((n, NUM_CHANNELS), dtype=np.float32)

    eeg = frames[:, 3:27].reshape(n, NUM_EEG, 3).astype(np.int32)
    eeg = (eeg[:, :, 0] << 16) | (eeg[:, :, 1] << 8) | eeg[:, :, 2]
    eeg = np.where(eeg & 0x800000, eeg - 0x1000000, eeg)
    out[:, :NUM_EEG] = eeg * EEG_UV_PER_COUNT

    motion = frames[:, 27:39].copy().view("<i2").astype(np.float32)
    col = NUM_EEG
    out[:, col:col + NUM_ACCEL] = motion[:, :3] * ACCEL_G_PER_COUNT
    col += NUM_ACCEL
    out[:, col:col + NUM_GYRO] = motion[:, 3:] * GYRO_DPS_PER_COUNT
    col += NUM_GYRO

    out[:, col] = (frames[:, 2] & 0x0F) * (100.0 / 15.0)
    out[:, col + 1] = frames[:, 39:43].copy().view("<u4")[:, 0]
    out[:, col + 2] = valid
    return out


class RhinoDevice:
    """One Unicorn on a serial port, read on a thread of its own.

    Decoded scans collect in a buffer that :meth:`get_data` drains, so
    the reader keeps pace with the radio however slowly the consumer
    asks.
    """

    #: How long start() keeps trying to reach the device, in seconds.
    CONNECT_TIMEOUT_S = 30.0
    #: How long one attempt waits for the start acknowledgement.
    ACK_TIMEOUT_S = 2.0
    #: Decoded scans kept before the oldest are dropped (60 s).
    BUFFER_SCANS = 60 * SAMPLING_RATE

    def __init__(self, serial_number: Optional[str] = None,
                 port: Optional[str] = None, transport: str = "auto"):
        """
        Args:
            serial_number: The headset's name, e.g. ``UN-2024.08.41``, as
                the Bluetooth settings list it; dots are optional. If
                None, the one on ``port``, else the first paired one.
                On Linux a Bluetooth address also does.
            port: The headset's serial port, e.g. ``COM3``, or a
                ``/dev/rfcommN`` bound by hand on Linux.
            transport: ``"serial"`` reads the port itself,
                ``"bluetooth"`` opens an RFCOMM socket (Linux only),
                ``"bridge"`` goes through the RhinoConnect helper (macOS
                only), and ``"auto"`` picks the bridge on macOS, the
                socket on Linux unless a port is given, the port
                elsewhere.

        Raises:
            DeviceNotFound: If no headset is named, or none is paired.
        """
        serial_number = serial_number or None
        if transport == "auto":
            if sys.platform == "darwin":
                transport = "bridge"
            elif sys.platform.startswith("linux") and port is None:
                transport = "bluetooth"
            else:
                transport = "serial"
        if serial_number is None and transport == "bluetooth":
            headsets = find_headsets()
            if not headsets:
                raise DeviceNotFound(
                    "No paired Unicorn found. Pair the headset first, "
                    "e.g. with bluetoothctl: scan on, pair <address>, "
                    "trust <address>.")
            serial_number = headsets[0]
        if serial_number is None:
            if port is None:
                ports = find_ports()
                if not ports:
                    raise DeviceNotFound(
                        "No paired Unicorn found. Pair the headset in "
                        "the system Bluetooth settings first.")
                port = ports[0]
            serial_number = port.rsplit("/", 1)[-1].split(".", 1)[-1]
        if transport == "serial" and port is None:
            port = port_for(serial_number)
        #: The headset's name, as given.
        self.serial_number = serial_number
        self.port = port
        if transport == "bridge":
            self._link = BridgeLink(serial_number)
        elif transport == "bluetooth":
            self._link = BluetoothLink(serial_number)
        else:
            self._link = SerialLink(port)
        self._link_open = False
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._scans: deque = deque(maxlen=self.BUFFER_SCANS)
        self._available = threading.Condition()
        self._last_counter: Optional[int] = None
        #: Scans the counter says were lost, and frames that failed to parse.
        self.lost_scans = 0
        self.bad_frames = 0
        self.stream_error: Optional[Exception] = None

    def start(self) -> None:
        """Open the link, start acquisition and the reader thread.

        Retries until the headset acknowledges or CONNECT_TIMEOUT_S has
        passed: right after the headset is switched on, or after
        Bluetooth was toggled, the first attempts are often refused.

        Raises:
            ConnectionError: If the headset never acknowledged.
        """
        deadline = time.monotonic() + self.CONNECT_TIMEOUT_S
        attempts = 0
        last_error: Optional[Exception] = None
        while True:
            attempts += 1
            try:
                if self._try_start():
                    break
            except DeviceNotFound:
                # Asking again will not pair it.
                raise
            except (ConnectionError, OSError) as error:
                last_error = error
            if time.monotonic() >= deadline:
                why = f" Last error: {last_error}" if last_error else ""
                raise ConnectionError(
                    f"Unicorn {self.serial_number} did not answer the start "
                    f"command ({attempts} attempts in "
                    f"{self.CONNECT_TIMEOUT_S:.0f} s). Is it switched on "
                    f"and in range?{why}")
            time.sleep(0.5)
        self._last_counter = None
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _try_start(self) -> bool:
        """One attempt: open the link and send start. Leaves the link open
        and returns True only if the headset acknowledged."""
        self._link.open(self.ACK_TIMEOUT_S)
        self._link_open = True
        try:
            self._link.drain()
            # A previous run that died mid-stream leaves the device
            # streaming; stop it so the start acknowledgement is the
            # first thing read.
            self._link.write(CMD_STOP)
            time.sleep(0.2)
            self._link.drain()
            self._link.write(CMD_START)
            ack = self._link.read(ACK_LENGTH, self.ACK_TIMEOUT_S)
            if len(ack) == ACK_LENGTH:
                return True
        except (ConnectionError, OSError, serial.SerialException):
            pass
        self._close_link()
        return False

    def _close_link(self) -> None:
        if self._link_open:
            self._link.close()
            self._link_open = False

    def stop(self) -> None:
        """Stop acquisition and close the link."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._link_open:
            try:
                self._link.write(CMD_STOP)
                time.sleep(0.1)
            except Exception:
                pass
            self._close_link()
        with self._available:
            self._available.notify_all()

    def available_scans(self) -> int:
        return len(self._scans)

    def get_data(self, n: int, timeout: float = 2.0) -> np.ndarray:
        """Block until n scans are available and return them.

        Raises:
            TimeoutError: If they do not arrive within ``timeout``.
            ConnectionError: If the reader has stopped on an error.
        """
        deadline = time.monotonic() + timeout
        with self._available:
            while len(self._scans) < n:
                if self.stream_error is not None:
                    raise ConnectionError(str(self.stream_error)) from self.stream_error
                if not self._running:
                    raise ConnectionError("Unicorn acquisition stopped.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"{n} scans did not arrive within {timeout} s.")
                self._available.wait(remaining)
            return np.stack([self._scans.popleft() for _ in range(n)])

    def _read_loop(self) -> None:
        buffer = bytearray()
        try:
            while self._running:
                chunk = self._link.read_some(1.0)
                if not chunk:
                    continue
                buffer += chunk
                frames = self._extract_frames(buffer)
                if frames:
                    self._publish(frames)
        except Exception as error:  # noqa: BLE001
            if self._running:
                self.stream_error = error
        finally:
            self._running = False
            with self._available:
                self._available.notify_all()

    def _extract_frames(self, buffer: bytearray) -> list[bytes]:
        """Take every whole frame off the front of buffer, resyncing on
        the header if the stream has slipped."""
        frames = []
        while len(buffer) >= FRAME_LENGTH:
            if (buffer[0:2] == HEADER
                    and buffer[FRAME_LENGTH - 2:FRAME_LENGTH] == FOOTER):
                frames.append(bytes(buffer[:FRAME_LENGTH]))
                del buffer[:FRAME_LENGTH]
                continue
            self.bad_frames += 1
            next_header = buffer.find(HEADER, 1)
            if next_header < 0:
                del buffer[:-1]
                break
            del buffer[:next_header]
        return frames

    def _publish(self, frames: list[bytes]) -> None:
        raw = np.frombuffer(b"".join(frames), dtype=np.uint8)
        raw = raw.reshape(len(frames), FRAME_LENGTH)
        counters = raw[:, 39:43].copy().view("<u4")[:, 0].astype(np.int64)

        # A frame is valid when its counter follows the previous one.
        previous = np.concatenate((
            [counters[0] - 1 if self._last_counter is None
             else self._last_counter], counters[:-1]))
        gaps = counters - previous - 1
        self.lost_scans += int(gaps[gaps > 0].sum())
        self._last_counter = int(counters[-1])

        scans = decode(raw, (gaps == 0).astype(np.float32))
        with self._available:
            self._scans.extend(scans)
            self._available.notify_all()
