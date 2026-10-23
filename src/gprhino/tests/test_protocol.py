import numpy as np
import pytest

from gprhino import protocol
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
    device._publish([make_frame(c) for c in (10, 11, 14, 15)])
    assert device.lost_scans == 2
    scans = list(device._scans)
    assert [s[AUX + 2] for s in scans] == [1.0, 1.0, 0.0, 1.0]
    # Continuity carries over into the next batch.
    device._publish([make_frame(17)])
    assert device.lost_scans == 3
    assert device._scans[-1][AUX + 2] == 0.0


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


def test_no_paired_device(monkeypatch):
    monkeypatch.setattr(protocol, "find_ports", lambda: [])
    with pytest.raises(protocol.DeviceNotFound):
        RhinoDevice()
