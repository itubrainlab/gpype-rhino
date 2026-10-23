"""Read a few seconds from the Unicorn without g.Pype and print a summary.

    python probe_unicorn.py [device-name] [seconds]

device-name is the headset's Bluetooth name, e.g. UN-2024.08.41; the
first paired one if omitted.
"""
import sys

import numpy as np

from gpype_rhino import UnicornDevice, find_ports
from gpype_rhino.protocol import SAMPLING_RATE

name = sys.argv[1] if len(sys.argv) > 1 else None
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
print("ports:", find_ports())

device = UnicornDevice(name)
print("connecting to", device.serial_number)
device.start()
try:
    n = int(seconds * SAMPLING_RATE)
    data = device.get_data(n, timeout=seconds + 5)
finally:
    device.stop()

np.set_printoptions(precision=2, suppress=True, linewidth=140)
print(f"{n} scans, lost {device.lost_scans}, bad frames {device.bad_frames}")
print("counter first/last:", data[0, 15], data[-1, 15])
print("battery %:", data[-1, 14])
print("EEG mean uV:", data[:, :8].mean(0))
print("EEG std  uV:", data[:, :8].std(0))
print("accel g    :", data[:, 8:11].mean(0),
      "|a| =", np.linalg.norm(data[:, 8:11].mean(0)).round(3))
print("gyro deg/s :", data[:, 11:14].mean(0))
