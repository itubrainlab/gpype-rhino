"""Show the Unicorn Hybrid Black in a g.Pype time-series scope, and record
or replay sessions with g.Pype's native record & replay.

    python scope.py [--device UN-2024.08.41] [--save-as raw]
    python scope.py --load-from raw_20261022_153000_123

--device is the headset's Bluetooth name; the first paired one if omitted.

--save-as records what the headset delivered, before any filtering, into a
run directory named after the stem plus the start time (manifest.json and
one HDF5 file per source). --load-from runs the same pipeline again with
that recording in place of the headset; no Bluetooth is touched. See
https://gpype.gtec.at/content/4_advanced_topics/record_replay.html. Both
need ``pip install "gpype[formats]"``.

By default the EEG is band-passed 1-30 Hz with a 50 Hz notch; --raw shows
it unfiltered. --seconds quits after that long; --snapshot also saves the
window to a PNG first, for a run nobody is watching.
"""
import argparse
from pathlib import Path

import gpype as gp

from gpype_rhino import RhinoHybridBlack

#: The source's name. A replay finds its stream by this name, so a run
#: recorded under one name only replays into a source of the same name.
SOURCE_NAME = "rhino"

parser = argparse.ArgumentParser()
parser.add_argument("--device", default=None,
                    help="headset name, e.g. UN-2024.08.41")
parser.add_argument("--port", default=None)
replay = parser.add_mutually_exclusive_group()
replay.add_argument("--save-as", metavar="STEM", default=None,
                    help="record the run into STEM_<start time>/")
replay.add_argument("--load-from", metavar="RUN", default=None,
                    help="replay a run directory recorded by --save-as")
parser.add_argument("--raw", action="store_true")
parser.add_argument("--notch", type=float, default=50.0)
parser.add_argument("--amplitude", type=float, default=50.0)
parser.add_argument("--seconds", type=float, default=None,
                    help="quit after this long")
parser.add_argument("--snapshot", default=None,
                    help="save the window to this PNG before quitting")
args = parser.parse_args()
if args.load_from and not Path(args.load_from).exists():
    parser.error(f"{args.load_from}: no such run")
if args.snapshot and args.seconds is None:
    args.seconds = 15.0

# Read while the sources are built, so it must come before any node.
gp.LaunchConfig.configure(save_as=args.save_as, load_from=args.load_from)

app = gp.MainApp()
p = gp.Pipeline()

source = RhinoHybridBlack(serial=args.device, port=args.port, name=SOURCE_NAME)
scope = gp.TimeSeriesScope(amplitude_limit=args.amplitude, time_window=10)

if args.raw:
    p.connect(source, scope)
else:
    bandpass = gp.Bandpass(f_lo=1, f_hi=30)
    notch = gp.Bandstop(f_lo=args.notch - 2, f_hi=args.notch + 2)
    p.connect(source, bandpass)
    p.connect(bandpass, notch)
    p.connect(notch, scope)

app.add_widget(scope)

if args.seconds is not None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    def snapshot_and_quit():
        if args.snapshot:
            windows = [w for w in QApplication.topLevelWidgets()
                       if w.isVisible()]
            windows[0].grab().save(args.snapshot)
            print(f"saved {args.snapshot}")
        QApplication.quit()

    QTimer.singleShot(int(args.seconds * 1000), snapshot_and_quit)

p.start()
app.run()
p.stop()

if args.save_as:
    # g.Pype named the run directory STEM_<start time>; report the newest.
    stem = Path(args.save_as)
    runs = sorted((d for d in stem.parent.glob(f"{stem.name}_*")
                   if d.is_dir()), key=lambda d: d.stat().st_mtime)
    if runs:
        print(f"recorded run {runs[-1]}")
