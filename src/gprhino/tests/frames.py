"""Synthetic Unicorn frames and a link that serves them, for tests that
need no headset."""
from __future__ import annotations

import struct
import threading
from typing import Sequence

from gprhino import protocol


def make_frame(counter: int, eeg: Sequence[int] = (0,) * 8,
               accel: Sequence[int] = (0, 0, 0),
               gyro: Sequence[int] = (0, 0, 0), battery: int = 15) -> bytes:
    """One 45-byte frame from raw counts."""
    body = bytearray(protocol.HEADER)
    body.append(battery & 0x0F)
    for value in eeg:
        body += (value & 0xFFFFFF).to_bytes(3, "big")
    body += struct.pack("<3h", *accel)
    body += struct.pack("<3h", *gyro)
    body += struct.pack("<I", counter)
    body += protocol.FOOTER
    assert len(body) == protocol.FRAME_LENGTH
    return bytes(body)


class FakeLink:
    """Stands in for SerialLink / BridgeLink: acknowledges the start
    command, then serves whatever was queued with :meth:`feed`."""

    def __init__(self, acknowledge: bool = True):
        self.acknowledge = acknowledge
        self.written: list[bytes] = []
        self.opened = 0
        self.closed = 0
        self._pending = bytearray()
        self._ready = threading.Condition()

    def feed(self, data: bytes) -> None:
        with self._ready:
            self._pending += data
            self._ready.notify_all()

    def open(self, timeout: float) -> None:
        self.opened += 1

    def write(self, data: bytes) -> None:
        self.written.append(data)
        if data == protocol.CMD_START and self.acknowledge:
            self.feed(b"\x00\x00\x00")

    def read(self, size: int, timeout: float) -> bytes:
        with self._ready:
            self._ready.wait_for(lambda: len(self._pending) >= size, timeout)
            data = bytes(self._pending[:size])
            del self._pending[:size]
            return data

    def read_some(self, timeout: float) -> bytes:
        with self._ready:
            self._ready.wait_for(lambda: self._pending, timeout)
            data = bytes(self._pending)
            self._pending.clear()
            return data

    def drain(self) -> None:
        with self._ready:
            self._pending.clear()

    def close(self) -> None:
        self.closed += 1
