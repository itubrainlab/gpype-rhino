"""g.Pype source node for the Unicorn Hybrid Black, without gtec_unicorn.

Same shape as g.Pype's own ``HybridBlack`` -- same channel layout,
roles, units and arrival stamp -- so it drops into any pipeline built
for that node. The difference is the driver: :class:`RhinoDevice`
talks to the headset's Bluetooth serial port directly.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np
from gpype.backend.core.o_port import OPort
from gpype.backend.sources.base.amplifier_source import (
    AmplifierSource, apply_channel_units)
from gpype.common._private import channels
from gpype.common.constants import Constants

from . import protocol
from .protocol import RhinoDevice

PORT_OUT = Constants.Defaults.PORT_OUT


class RhinoHybridBlack(AmplifierSource):
    """Unicorn Hybrid Black over Bluetooth serial, 8 EEG channels at 250 Hz.

    Output columns, in order and each block only if enabled: EEG (uV),
    accelerometer (g), gyroscope (deg/s), then battery (%), counter and
    validation (1 when no scan was lost before this one), and finally the
    arrival stamp g.Pype's Sync reads.
    """

    SAMPLING_RATE = protocol.SAMPLING_RATE
    NUM_EEG_CHANNELS = protocol.NUM_EEG
    NUM_ACCEL_CHANNELS = protocol.NUM_ACCEL
    NUM_GYRO_CHANNELS = protocol.NUM_GYRO
    NUM_AUX_CHANNELS = 3
    NUM_TIMELINE_CHANNELS = 1
    #: Consecutive failed reads tolerated before acquisition is given up.
    NUM_ACQUISITION_ERRORS_ALLOWED = 3

    class Configuration(AmplifierSource.Configuration):
        """Configuration class for RhinoHybridBlack parameters."""

        class Keys(AmplifierSource.Configuration.Keys):
            """Configuration keys for RhinoHybridBlack settings."""

            EEG_CHANNEL_COUNT = "eeg_channel_count"
            INCLUDE_ACCEL = "include_accel"
            INCLUDE_GYRO = "include_gyro"
            INCLUDE_AUX = "include_aux"

        class OptionalKeys(AmplifierSource.Configuration.OptionalKeys):
            """Optional configuration keys."""

            #: Name of the headset, e.g. UN-2024.08.41; absent means the
            #: first paired one.
            SERIAL = "serial"
            #: Serial port of the headset, where the name cannot find it.
            PORT = "port"
            #: "auto", "serial", "bluetooth" or "bridge"; see RhinoDevice.
            TRANSPORT = "transport"

    def __init__(
        self,
        serial: Optional[str] = None,
        port: Optional[str] = None,
        channel_count: Optional[int] = None,
        frame_size: Optional[int] = None,
        include_accel: bool = False,
        include_gyro: bool = False,
        include_aux: bool = False,
        transport: str = "auto",
        **kwargs,
    ):
        """Initialize the source. Nothing reaches the device until start().

        Args:
            serial: Name of the headset as the Bluetooth settings list
                it, e.g. ``UN-2024.08.41`` (the same value g.Pype's
                HybridBlack takes). The first paired one if None.
            port: Serial port of the headset, e.g. ``COM3``. Not needed
                on Linux or macOS.
            transport: How to reach it: ``"serial"`` reads the port,
                ``"bluetooth"`` opens an RFCOMM socket (Linux),
                ``"bridge"`` uses the RhinoConnect helper (macOS), and
                ``"auto"`` picks the socket on Linux unless a port is
                given, the bridge on macOS, the port elsewhere.
            channel_count: Number of EEG channels (1-8). Defaults to 8.
            frame_size: Samples per processing frame.
            include_accel: Include accelerometer channels (3 channels).
            include_gyro: Include gyroscope channels (3 channels).
            include_aux: Include battery, counter and validation channels.
            **kwargs: Additional arguments for parent AmplifierSource.
        """
        keys = self.Configuration.Keys
        channel_count = self.scalar(channel_count)
        frame_size = self.scalar(frame_size)
        restored = self.scalar(kwargs.pop(keys.EEG_CHANNEL_COUNT, None))
        if restored is not None:
            channel_count = restored
        if channel_count is None:
            channel_count = self.NUM_EEG_CHANNELS
        channel_count = max(1, min(channel_count, self.NUM_EEG_CHANNELS))

        # Indices of the decoded scan's columns this node passes on.
        columns = list(range(channel_count))
        offset = self.NUM_EEG_CHANNELS
        for present, count in ((include_accel, self.NUM_ACCEL_CHANNELS),
                               (include_gyro, self.NUM_GYRO_CHANNELS),
                               (include_aux, self.NUM_AUX_CHANNELS)):
            if present:
                columns += range(offset, offset + count)
            offset += count
        total_channels = len(columns)

        kwargs.setdefault(keys.OUTPUT_PORTS, [OPort.Configuration()])
        kwargs[self.Configuration.OptionalKeys.SERIAL] = serial or None
        kwargs[self.Configuration.OptionalKeys.PORT] = port or None
        kwargs[self.Configuration.OptionalKeys.TRANSPORT] = transport
        kwargs.pop(keys.SAMPLING_RATE, None)
        kwargs.pop(keys.DECIMATION_FACTOR, None)

        # One cycle carries one whole frame, so nothing is decimated.
        super().__init__(
            channel_count=total_channels,
            eeg_channel_count=channel_count,
            sampling_rate=self.SAMPLING_RATE,
            frame_size=frame_size,
            decimation_factor=1,
            include_accel=bool(include_accel),
            include_gyro=bool(include_gyro),
            include_aux=bool(include_aux),
            **kwargs,
        )

        self._frame_size = self.config[keys.FRAME_SIZE][0]
        self._serial = serial or None
        self._port = port
        self._transport = transport
        self._columns = np.array(columns)
        self._eeg_channel_count = channel_count
        self._total_channels = total_channels
        self._include_accel = include_accel
        self._include_gyro = include_gyro
        self._include_aux = include_aux

        self._device: Optional[RhinoDevice] = None
        self._running = False
        self._acquisition_thread: Optional[threading.Thread] = None
        self._current_frame: Optional[np.ndarray] = None
        self._acquisition_error_counter = 0

    def start(self) -> None:
        """Connect to the headset, start streaming and the read thread.

        Raises:
            ConnectionError: If no headset is found or it does not answer.
        """
        self._current_frame = None
        self._acquisition_error_counter = 0

        if self._device is None:
            device = RhinoDevice(self._serial, port=self._port,
                                   transport=self._transport)
            device.start()
            self._device = device
            self.log(f"Connected to Unicorn {device.serial_number}")

        super().start()

        if not self._running:
            self._running = True
            self._acquisition_thread = threading.Thread(
                target=self._acquisition_function, daemon=True)
            self._acquisition_thread.start()

    def stop(self) -> None:
        """Stop the read thread, then stop and close the headset."""
        if self._running:
            self._running = False
            thread = self._acquisition_thread
            if thread and thread.is_alive():
                thread.join(timeout=5)
        if self._device is not None and self._device.lost_scans:
            self.log(f"{self._device.lost_scans} scans were lost in "
                     f"transmission.", type=Constants.LogTypes.WARNING)
        self._release_device()
        super().stop()

    def device_serial(self) -> str:
        if self._device is not None:
            return self._device.serial_number
        return self._serial or self._port or ""

    def channel_units(self) -> Optional[list]:
        units = ["uV"] * self._eeg_channel_count
        if self._include_accel:
            units += ["g"] * self.NUM_ACCEL_CHANNELS
        if self._include_gyro:
            units += ["deg/s"] * self.NUM_GYRO_CHANNELS
        if self._include_aux:
            units += ["%", "-", "-"]
        units += [None] * self.NUM_TIMELINE_CHANNELS
        return units

    def setup(self, data: dict[str, np.ndarray],
              port_context_in: dict[str, dict]) -> dict[str, dict]:
        """Describe the channel layout, with the arrival stamp after it."""
        port_context_out = super().setup(data, port_context_in)

        roles = [Constants.ChannelRoles.SIGNAL] * self._eeg_channel_count
        for present, count in ((self._include_accel, self.NUM_ACCEL_CHANNELS),
                               (self._include_gyro, self.NUM_GYRO_CHANNELS),
                               (self._include_aux, self.NUM_AUX_CHANNELS)):
            if present:
                roles += [Constants.ChannelRoles.AUXILIARY] * count
        stamped = self.NUM_TIMELINE_CHANNELS
        roles += [Constants.ChannelRoles.TIMESTAMP] * stamped

        units = self.channel_units()
        for context in port_context_out.values():
            total = channels.channel_count(context)
            context[Constants.Keys.CHANNEL_COUNT] = total + stamped
            context.update(channels.describe(roles))
            apply_channel_units(context, units)
        return port_context_out

    def step(self, data: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Emit the frame the acquisition thread just read."""
        if not self.is_decimation_step():
            return None
        if self._current_frame is not None:
            frame, self._current_frame = self._current_frame, None
            return {PORT_OUT: frame}
        # No frame: emit zeros, but stamped with the real instant so Sync
        # does not read the epoch as a timestamp.
        frame = np.zeros(
            (self._frame_size,
             self._total_channels + self.NUM_TIMELINE_CHANNELS),
            dtype=Constants.DATA_TYPE)
        frame[:, self._total_channels:] = channels.wrap_time(
            channels.stamp_clock())
        return {PORT_OUT: frame}

    def _acquisition_function(self) -> None:
        """Background thread: read a block, stamp it, cycle the pipeline."""
        device = self._device
        if device is None:
            return
        frame_length = self._frame_size
        width = self._total_channels
        frame_period_s = frame_length / float(self.SAMPLING_RATE)

        while self._running:
            try:
                block = device.get_data(frame_length)
                arrival = channels.wrap_time(channels.stamp_clock())

                frame = np.empty(
                    (frame_length, width + self.NUM_TIMELINE_CHANNELS),
                    dtype=Constants.DATA_TYPE)
                frame[:, :width] = block[:, self._columns]
                frame[:, width:] = arrival

                self._current_frame = frame
                self.cycle()
                self._acquisition_error_counter = 0

            except Exception as error:  # noqa: BLE001
                if not self._running:
                    break
                self._acquisition_error_counter += 1
                remaining = (self.NUM_ACQUISITION_ERRORS_ALLOWED
                             - self._acquisition_error_counter)
                if remaining > 0:
                    self.log(f"A read from the Unicorn failed ({error}); "
                             f"{remaining} more consecutive failure(s) "
                             f"will end the acquisition.",
                             type=Constants.LogTypes.WARNING)
                    time.sleep(frame_period_s)
                    continue
                self._running = False
                self._imp._condition = Constants.Conditions.ERROR
                self.log(f"The Unicorn stopped delivering data: {error}. "
                         f"Check that it is switched on and in range, then "
                         f"start the pipeline again.",
                         type=Constants.LogTypes.ERROR)
                break

    @staticmethod
    def get_available_devices() -> list[str]:
        """Serial ports of paired Unicorn headsets."""
        return protocol.find_ports()
