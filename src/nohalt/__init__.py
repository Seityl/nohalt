"""nohalt -- autonomous coding agents that keep going until the forge says done."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nohalt")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
