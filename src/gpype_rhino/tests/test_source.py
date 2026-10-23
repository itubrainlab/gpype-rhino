"""The g.Pype node, with RhinoDevice replaced by one on a FakeLink."""
import time

import numpy as np
import pytest

pytest.importorskip("gpype")

from gpype_rhino import RhinoHybridBlack, source  # noqa: E402
from gpype_rhino.protocol import RhinoDevice  # noqa: E402

from .frames import FakeLink, make_frame  # noqa: E402


def test_importable_from_package():
    import gpype_rhino
    assert gpype_rhino.RhinoHybridBlack is source.RhinoHybridBlack


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

    def fake_device(serial, port=None, transport="auto"):
        device = RhinoDevice(serial, port="/dev/null", transport="serial")
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
