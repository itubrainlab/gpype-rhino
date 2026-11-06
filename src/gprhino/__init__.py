"""Licence-free g.Pype source for the Unicorn Hybrid Black over Bluetooth.

No longer actively developed: g.tec's gtec-unicorn 2.0.0 no longer
requires a licence, so use it with g.Pype's own HybridBlack instead.
"""
from .protocol import RhinoDevice, find_headsets, find_ports
from .transport import DeviceNotFound, windows_bluetooth_ports

__version__ = "0.2.0"
__all__ = ["RhinoHybridBlack", "RhinoDevice", "DeviceNotFound",
           "find_headsets", "find_ports", "windows_bluetooth_ports"]


def __getattr__(name):
    # The g.Pype node is imported lazily, so the bare driver works
    # without g.Pype installed.
    if name == "RhinoHybridBlack":
        from .source import RhinoHybridBlack
        return RhinoHybridBlack
    raise AttributeError(name)
