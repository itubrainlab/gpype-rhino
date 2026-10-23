# gpype_rhino

A licence-free g.Pype source for the g.tec **Unicorn Hybrid Black** EEG
headset over Bluetooth. It talks to the headset's Bluetooth serial channel
directly, without g.tec's `gtec_unicorn` driver.

## Install

```bash
pip install gpype_rhino
```

From a checkout:

```bash
pip install -e ".[test]"
```

## Use

`RhinoHybridBlack` drops into a g.Pype pipeline in place of `gp.HybridBlack`:

```python
import gpype as gp
from gpype_rhino import RhinoHybridBlack

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

`UnicornDevice` reads the headset without g.Pype:

```python
from gpype_rhino import UnicornDevice

device = UnicornDevice("UN-2024.08.41")
device.start()
data = device.get_data(250)   # one second: EEG, accel, gyro, battery, counter, valid
device.stop()
```

Pair the headset in the system Bluetooth settings first. On Linux, bind it
with `rfcomm bind` and pass the port (`port="/dev/rfcomm0"`). On macOS the
connection goes through a small helper app, `UnicornConnect`. It is built
from the bundled Swift source the first time it is needed, which requires
the Xcode command line tools, and is placed in
`~/Library/Caches/gpype_rhino` when the install directory is read-only.

More scripts are in `examples/`.

## Tests

The tests ship inside the package and need no headset:

```bash
pytest --pyargs gpype_rhino
```
