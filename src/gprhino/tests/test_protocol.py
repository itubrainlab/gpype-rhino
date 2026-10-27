import subprocess

import numpy as np
import pytest

from gprhino import protocol, transport
from gprhino.protocol import RhinoDevice, decode, plain_name

from .frames import FakeLink, make_frame

AUX = protocol.NUM_EEG + protocol.NUM_ACCEL + protocol.NUM_GYRO


def as_array(*frames: bytes) -> np.ndarray:
    raw = np.frombuffer(b"".join(frames), dtype=np.uint8)
    return raw.reshape(len(frames), protocol.FRAME_LENGTH)


@pytest.fixture
def device(monkeypatch):
    """A RhinoDevice on a FakeLink, connecting without delay."""
    monkeypatch.setattr(protocol.time, "sleep", lambda s: None)
    dev = RhinoDevice("UN-2024.08.41", port="/dev/null",
                        transport="serial")
    dev._link = FakeLink()
    yield dev
    dev.stop()


@pytest.mark.parametrize("name, plain", [
    ("UN-2024.08.41", "un-20240841"),
    ("/dev/cu.UN-20240841", "un-20240841"),
    ("/dev/tty.UN-2024.08.41", "un-20240841"),
    ("UN-20240841", "un-20240841"),
])
def test_plain_name(name, plain):
    assert plain_name(name) == plain


def test_decode_eeg_signs_and_scale():
    eeg = (0, 1, -1, 0x7FFFFF, -0x800000, 1000, -1000, 42)
    out = decode(as_array(make_frame(7, eeg=eeg)), np.ones(1))
    assert out.shape == (1, protocol.NUM_CHANNELS)
    assert out.dtype == np.float32
    np.testing.assert_allclose(
        out[0, :8], np.array(eeg) * protocol.EEG_UV_PER_COUNT, rtol=1e-6)


def test_decode_motion_battery_counter_valid():
    frame = make_frame(123456, accel=(4096, -4096, 0),
                       gyro=(328, -328, 0), battery=15)
    out = decode(as_array(frame), np.array([0.0]))[0]
    np.testing.assert_allclose(out[8:11], [1.0, -1.0, 0.0])
    np.testing.assert_allclose(out[11:14], [10.0, -10.0, 0.0], rtol=1e-5)
    assert out[AUX] == pytest.approx(100.0)
    assert out[AUX + 1] == 123456
    assert out[AUX + 2] == 0.0


def test_battery_uses_low_nibble_only():
    frame = bytearray(make_frame(0, battery=0))
    frame[2] = 0xF3
    out = decode(as_array(bytes(frame)), np.ones(1))[0]
    assert out[AUX] == pytest.approx(3 * 100.0 / 15.0)


def test_extract_frames_whole_and_partial(device):
    a, b = make_frame(1), make_frame(2)
    buffer = bytearray(a + b[:20])
    assert device._extract_frames(buffer) == [a]
    assert buffer == b[:20]
    buffer += b[20:]
    assert device._extract_frames(buffer) == [b]
    assert not buffer
    assert device.bad_frames == 0


def test_extract_frames_resyncs_after_garbage(device):
    a, b = make_frame(1), make_frame(2)
    buffer = bytearray(b"\x01\x02\x03" + a + b"\xff" * 5 + b)
    assert device._extract_frames(buffer) == [a, b]
    assert device.bad_frames == 2


def test_publish_counts_lost_scans(device):
    device._publish([make_frame(c) for c in (10, 11, 14, 15)], 0.0)
    assert device.lost_scans == 2
    scans = list(device._scans)
    assert [s[AUX + 2] for s in scans] == [1.0, 1.0, 0.0, 1.0]
    # Continuity carries over into the next batch.
    device._publish([make_frame(17)], 0.0)
    assert device.lost_scans == 3
    assert device._scans[-1][AUX + 2] == 0.0


def test_scans_seen_backed_off_by_place_in_burst(device):
    """A scan early in a burst was acquired before the burst arrived."""
    period = 1.0 / protocol.SAMPLING_RATE
    device._publish([make_frame(c) for c in range(4)], 10.0)
    assert list(device._seen) == pytest.approx(
        [10.0 - 3 * period, 10.0 - 2 * period, 10.0 - period, 10.0])
    device._running = True
    device.get_data(2)
    assert device.last_seen == pytest.approx(10.0 - 2 * period)
    device.get_data(2)
    assert device.last_seen == 10.0
    assert not device._seen

def test_start_stream_and_stop(device):
    link = device._link
    device.start()
    assert link.written[:2] == [protocol.CMD_STOP, protocol.CMD_START]
    link.feed(b"".join(make_frame(c, eeg=(c,) * 8) for c in range(10)))
    data = device.get_data(10, timeout=2.0)
    assert data.shape == (10, protocol.NUM_CHANNELS)
    np.testing.assert_array_equal(data[:, AUX + 1], np.arange(10))
    assert device.lost_scans == 0
    device.stop()
    assert link.written[-1] == protocol.CMD_STOP
    assert link.closed == 1


def test_get_data_times_out(device):
    device.start()
    with pytest.raises(TimeoutError):
        device.get_data(5, timeout=0.1)


def test_get_data_after_stop_raises(device):
    device.start()
    device.stop()
    with pytest.raises(ConnectionError):
        device.get_data(1, timeout=0.1)


def test_start_gives_up_without_acknowledgement(device, monkeypatch):
    device._link = FakeLink(acknowledge=False)
    monkeypatch.setattr(RhinoDevice, "CONNECT_TIMEOUT_S", 0.0)
    monkeypatch.setattr(RhinoDevice, "ACK_TIMEOUT_S", 0.01)
    with pytest.raises(ConnectionError, match="did not answer"):
        device.start()
    assert device._link.closed == device._link.opened


@pytest.mark.parametrize("transport", ["serial", "bluetooth"])
def test_no_paired_device(monkeypatch, transport):
    monkeypatch.setattr(protocol, "find_ports", lambda: [])
    monkeypatch.setattr(protocol, "paired_devices", lambda: [])
    with pytest.raises(protocol.DeviceNotFound):
        RhinoDevice(transport=transport)


PAIRED = [("78:5E:A2:BD:0D:5E", "WILLEN"),
          ("A4:6D:D4:17:BF:35", "UN-2024.10.18")]


@pytest.mark.parametrize("platform, port, link", [
    ("linux", None, transport.BluetoothLink),
    ("linux", "/dev/rfcomm0", transport.SerialLink),
    ("darwin", None, transport.BridgeLink),
    ("win32", "COM3", transport.SerialLink),
])
def test_auto_transport(monkeypatch, platform, port, link):
    monkeypatch.setattr(protocol.sys, "platform", platform)
    assert isinstance(RhinoDevice("UN-2024.10.18", port=port)._link, link)


def test_bluetooth_picks_first_paired_headset(monkeypatch):
    monkeypatch.setattr(protocol, "paired_devices", lambda: PAIRED)
    device = RhinoDevice(transport="bluetooth")
    assert device.serial_number == "UN-2024.10.18"


@pytest.mark.parametrize("name", [
    "UN-2024.10.18", "UN-20241018", "2024.10.18", "un-2024.10.18"])
def test_address_for(monkeypatch, name):
    monkeypatch.setattr(transport, "paired_devices", lambda: PAIRED)
    assert transport.address_for(name) == "A4:6D:D4:17:BF:35"


def test_address_for_unknown_name(monkeypatch):
    monkeypatch.setattr(transport, "paired_devices", lambda: PAIRED)
    with pytest.raises(transport.DeviceNotFound):
        transport.address_for("UN-2024.08.41")


def test_bluetooth_link_takes_an_address():
    link = transport.BluetoothLink("A4:6D:D4:17:BF:35")
    assert link.address == "A4:6D:D4:17:BF:35"


def test_paired_devices_parses_bluetoothctl(monkeypatch):
    output = ("Device A4:6D:D4:17:BF:35 UN-2024.10.18\n"
              "Device 78:5E:A2:BD:0D:5E Some Name With Spaces\n"
              "[bluetooth]# noise\n")
    monkeypatch.setattr(transport.shutil, "which", lambda _: "/bin/x")
    monkeypatch.setattr(transport.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, output, ""))
    assert transport.paired_devices() == [
        ("A4:6D:D4:17:BF:35", "UN-2024.10.18"),
        ("78:5E:A2:BD:0D:5E", "Some Name With Spaces")]


def test_sockaddr_rc_matches_linux():
    """struct sockaddr_rc: family, address least significant byte first,
    channel, padded to 10 bytes."""
    import ctypes
    bdaddr = bytes.fromhex("A46DD417BF35")[::-1]
    addr = transport._SockaddrRc(
        transport._AF_BLUETOOTH,
        (ctypes.c_ubyte * 6).from_buffer_copy(bdaddr), 1)
    assert ctypes.sizeof(addr) == 10
    assert bytes(addr)[:9] == (bytes([31, 0]) + bytes.fromhex("35BF17D46DA4")
                               + bytes([1]))


# -- Windows: Bluetooth serial ports ------------------------------------------

SPP = "BTHENUM\\{00001101-0000-1000-8000-00805F9B34FB}"
#: pyserial's view of a Windows PC with the headset and another device
#: paired: each has an outgoing port, and the PC offers an incoming one.
COMPORTS = [
    ("COM3", "Communications Port (COM3)", "ACPI\\PNP0501\\0"),
    ("COM4", "Standard Serial over Bluetooth link (COM4)",
     f"{SPP}_LOCALMFG&0000\\7&1C5E7B1A&0&000000000000_00000000"),
    ("COM5", "Standard Serial over Bluetooth link (COM5)",
     f"{SPP}_LOCALMFG&0002\\7&2F0E3D5B&0&A46DD417BF35_C00000000"),
    ("COM6", "Standard Serial over Bluetooth link (COM6)",
     f"{SPP}_LOCALMFG&0002\\7&2F0E3D5B&0&785EA2BD0D5E_C00000000"),
]
#: The registry's BTHPORT\Parameters\Devices\<address>\Name values.
NAMES = {"a46dd417bf35": b"UN-2024.10.18\x00", "785ea2bd0d5e": b"WILLEN\x00"}


@pytest.fixture
def windows(monkeypatch):
    import sys
    import types

    from serial.tools import list_ports
    from serial.tools.list_ports_common import ListPortInfo

    def comports():
        ports = []
        for device, description, hwid in COMPORTS:
            info = ListPortInfo(device, skip_link_detection=True)
            info.description, info.hwid = description, hwid
            ports.append(info)
        return ports

    class Key:
        def __init__(self, path):
            self.address = path.rsplit("\\", 1)[-1]
            if self.address not in NAMES:
                raise FileNotFoundError(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    winreg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=lambda root, path: Key(path),
        QueryValueEx=lambda key, value: (NAMES[key.address], 3))
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setattr(list_ports, "comports", comports)
    monkeypatch.setattr(protocol.sys, "platform", "win32")


def test_windows_bluetooth_ports(windows):
    assert transport.windows_bluetooth_ports() == [
        ("COM5", "A4:6D:D4:17:BF:35", "UN-2024.10.18"),
        ("COM6", "78:5E:A2:BD:0D:5E", "WILLEN")]


def test_windows_finds_the_headset(windows):
    assert protocol.find_headsets() == ["UN-2024.10.18"]
    assert protocol.find_ports() == ["COM5"]


@pytest.mark.parametrize("name", [
    "UN-2024.10.18", "UN-20241018", "2024.10.18", "A4:6D:D4:17:BF:35"])
def test_windows_port_for(windows, name):
    assert protocol.port_for(name) == "COM5"


def test_windows_port_for_unknown_name(windows):
    with pytest.raises(protocol.DeviceNotFound):
        protocol.port_for("UN-2024.08.41")


def test_windows_picks_first_paired_headset(windows):
    device = RhinoDevice()
    assert (device.serial_number, device.port) == ("UN-2024.10.18", "COM5")
    assert isinstance(device._link, transport.SerialLink)


def test_windows_lists_unnamed_bluetooth_ports(windows, monkeypatch):
    """Where the registry cannot be read, the port is still offered."""
    monkeypatch.setattr(transport, "_windows_device_name", lambda raw: "")
    assert protocol.find_headsets() == []
    assert protocol.find_ports() == ["COM5", "COM6"]
