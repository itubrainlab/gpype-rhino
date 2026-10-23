# gpype-rhino

A licence-free g.Pype source for the g.tec **Unicorn Hybrid Black** EEG
headset over Bluetooth. It talks to the headset's Bluetooth serial channel
directly, without g.tec's `gtec_unicorn` driver.

## Install

```bash
pip install gpype-rhino
```

From a checkout:

```bash
pip install -e ".[test]"
```

## Use

`RhinoHybridBlack` drops into a g.Pype pipeline in place of `gp.HybridBlack`:

```python
import gpype as gp
from gprhino import RhinoHybridBlack

app = gp.MainApp()
p = gp.Pipeline()
source = RhinoHybridBlack(serial="UN-2024.08.41")   # None: first paired headset
scope = gp.TimeSeriesScope(amplitude_limit=50, time_window=10)
p.connect(source, scope)
app.add_widget(scope)
p.start()
app.run()
p.stop()
```

`RhinoDevice` reads the headset without g.Pype:

```python
from gprhino import RhinoDevice

device = RhinoDevice("UN-2024.08.41")
device.start()
data = device.get_data(250)   # one second: EEG, accel, gyro, battery, counter, valid
device.stop()
```

Pair the headset in the system Bluetooth settings first. On Linux the
headset is found by name among BlueZ's paired devices (via `bluetoothctl`)
and reached over an RFCOMM socket: no `rfcomm bind`, no root and no
`dialout` group needed. The `UN-` prefix may be left out of the name
(`"2024.10.18"`), and a Bluetooth address works too. To pair from a
terminal:

```bash
bluetoothctl
[bluetooth]# scan on          # wait for UN-XXXX.XX.XX to appear
[bluetooth]# pair A4:6D:D4:17:BF:35
[bluetooth]# trust A4:6D:D4:17:BF:35
```

A `/dev/rfcommN` bound by hand still works: pass it as `port`. On macOS the
connection goes through a small helper app, `RhinoConnect`. It is built
from the bundled Swift source the first time it is needed, which requires
the Xcode command line tools, and is placed in
`~/Library/Caches/gprhino` when the install directory is read-only.
macOS asks once for Bluetooth access the first time `RhinoConnect` runs.

## Examples

- `examples/scope.py` shows the headset in a g.Pype time-series scope, and
  records (`--save-as`) or replays (`--load-from`) sessions with g.Pype's
  record & replay, which needs `pip install "gpype[formats]"`.
- `examples/probe.py` reads a few seconds without g.Pype and prints a
  summary of each signal.

## Tests

The tests ship inside the package and need no headset:

```bash
pytest --pyargs gprhino
```

## License

MIT, © 2026 ITU brAIn lab. See [LICENSE](LICENSE).
