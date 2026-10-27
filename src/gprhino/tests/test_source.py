"""The g.Pype node, with RhinoDevice replaced by one on a FakeLink."""
import time

import numpy as np
import pytest

pytest.importorskip("gpype")

from gprhino import RhinoHybridBlack, source  # noqa: E402
from gprhino.protocol import RhinoDevice  # noqa: E402

from .frames import FakeLink, make_frame  # noqa: E402


def test_importable_from_package():
    import gprhino
    assert gprhino.RhinoHybridBlack is source.RhinoHybridBlack


@pytest.mark.parametrize("kwargs, columns, units", [
    ({}, list(range(8)), ["uV"] * 8),
    ({"channel_count": 4}, [0, 1, 2, 3], ["uV"] * 4),
    ({"channel_count": 20}, list(range(8)), ["uV"] * 8),
    ({"channel_count": 2, "include_accel": True},
     [0, 1, 8, 9, 10], ["uV"] * 2 + ["g"] * 3),
    ({"channel_count": 1, "include_gyro": True, "include_aux": True},
     [0, 11, 12, 13, 14, 15, 16], ["uV"] + ["deg/s"] * 3 + ["%", "-", "-"]),
])
def test_channel_layout(kwargs, columns, units):
    node = RhinoHybridBlack(serial="UN-2024.08.41", **kwargs)
    assert list(node._columns) == columns
    assert node.channel_units() == units + [None]
    assert node.device_serial() == "UN-2024.08.41"


def test_acquisition_emits_selected_columns(monkeypatch):
    link = FakeLink()

    def fake_device(serial, port=None, transport="auto", **kwargs):
        device = RhinoDevice(serial, port="/dev/null", transport="serial",
                             **kwargs)
        device._link = link
        return device

    monkeypatch.setattr(source, "RhinoDevice", fake_device)
    node = RhinoHybridBlack(serial="UN-2024.08.41", channel_count=2,
                     include_aux=True, frame_size=5)
    frames = []
    monkeypatch.setattr(node, "cycle",
                        lambda: frames.append(node._current_frame))
    monkeypatch.setattr(source.AmplifierSource, "start", lambda self: None)
    monkeypatch.setattr(source.AmplifierSource, "stop", lambda self: None)

    node.start()
    try:
        link.feed(b"".join(make_frame(c, eeg=(c, -c) + (0,) * 6)
                           for c in range(10)))
        deadline = time.monotonic() + 3
        while len(frames) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        node.stop()

    assert len(frames) >= 2
    frame = np.vstack(frames[:2])
    # 2 EEG + battery, counter, validation + arrival stamp.
    assert frame.shape == (10, 6)
    np.testing.assert_array_equal(frame[:, 3], np.arange(10))
    assert frame[:, 0] == pytest.approx(
        np.arange(10) * source.protocol.EEG_UV_PER_COUNT, rel=1e-5)
    assert np.all(frame[:, 4] == 1.0)


def test_stamp_is_acquisition_not_arrival(monkeypatch):
    """Each frame is stamped when its last scan was acquired: seen, backed
    off by its place in the burst, less the link's latency."""
    link = FakeLink()

    def fake_device(serial, port=None, transport="auto", clock=None):
        device = RhinoDevice(serial, port="/dev/null", transport="serial",
                             clock=lambda: 100.0)
        device._link = link
        return device

    monkeypatch.setattr(source, "RhinoDevice", fake_device)
    node = RhinoHybridBlack(serial="UN-2024.08.41", channel_count=1,
                            frame_size=5, latency_ms=25)
    frames = []
    monkeypatch.setattr(node, "cycle",
                        lambda: frames.append(node._current_frame))
    monkeypatch.setattr(source.AmplifierSource, "start", lambda self: None)
    monkeypatch.setattr(source.AmplifierSource, "stop", lambda self: None)

    node.start()
    try:
        # One burst of ten scans, seen at 100 s.
        link.feed(b"".join(make_frame(c) for c in range(10)))
        deadline = time.monotonic() + 3
        while len(frames) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        node.stop()

    assert len(frames) >= 2
    period = 1.0 / source.protocol.SAMPLING_RATE
    for frame, behind in zip(frames, (5, 0)):
        expected = source.channels.wrap_time(100.0 - behind * period - 0.025)
        assert np.all(frame[:, -1] == np.float32(expected))


def test_latency_defaults_to_device_latency():
    from gpype.backend.sources.hybrid_black import HybridBlack
    assert RhinoHybridBlack.DEVICE_LATENCY_MS == HybridBlack.DEVICE_DELAY_MS
    node = RhinoHybridBlack(serial="UN-2024.08.41")
    assert node._latency_s == RhinoHybridBlack.DEVICE_LATENCY_MS / 1000
    assert RhinoHybridBlack(serial="UN-2024.08.41",
                            latency_ms=0)._latency_s == 0
    with pytest.raises(ValueError):
        RhinoHybridBlack(serial="UN-2024.08.41", latency_ms=-1)
