"""Licence-free g.Pype source for the Unicorn Hybrid Black over Bluetooth."""
from .protocol import UnicornDevice, find_ports
from .transport import DeviceNotFound

__version__ = "0.1.0"
__all__ = ["RhinoHybridBlack", "UnicornDevice", "DeviceNotFound", "find_ports"]


def __getattr__(name):
    # The g.Pype node is imported lazily, so the bare driver works
    # without g.Pype installed.
    if name == "RhinoHybridBlack":
        from .source import RhinoHybridBlack
        return RhinoHybridBlack
    raise AttributeError(name)
