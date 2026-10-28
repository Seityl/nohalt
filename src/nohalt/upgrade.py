"""A new nohalt installed under a running supervisor (LIV-14).

A process that has the old modules in memory must never import a new file
against them: an agent module first used after ``pip install -U nohalt``
imported names the old ``nohalt.agents`` did not have, and the supervisor
crashed on every restart by keepalive until someone ran ``nohalt restart``.

Two rules keep the code a process runs from one version:

* ``preload`` imports every nohalt module when the supervisor starts, so no
  lazy import reads the disk mid-run.
* ``Watch`` notices that the package on disk changed. Once it has stayed the
  same for ``SETTLE`` seconds (an install in progress is not an upgrade yet) and
  a fresh interpreter imports all of it, the supervisor re-executes on it.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import pkgutil
import subprocess
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import __version__
from .util import now

PACKAGE = Path(__file__).resolve().parent
SETTLE = 5.0  # seconds the package must stay unchanged before it is switched to


def installed_version() -> str:
    try:
        return version("nohalt")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def fingerprint(root: Path = PACKAGE, ver: str = "") -> str:
    """The installed version plus every file of the package (path, size, mtime, inode).

    Byte-code caches are left out: they are written by whoever imports the package.
    A file that vanishes while it is read (an install in progress) counts as changed.
    """
    h = hashlib.sha256(ver.encode())
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        try:
            st = path.stat()
        except OSError:
            h.update(f"{path}\0gone\n".encode())
            continue
        if path.is_file():
            h.update(f"{path.relative_to(root)}\0{st.st_size}\0{st.st_mtime_ns}\0{st.st_ino}\n".encode())
    return h.hexdigest()


def preload() -> list[str]:
    """Import every nohalt module now, so none is read from disk later."""
    import nohalt

    names = []
    for mod in pkgutil.walk_packages(nohalt.__path__, "nohalt."):
        if mod.name != "nohalt.__main__":  # runs the CLI
            importlib.import_module(mod.name)
            names.append(mod.name)
    return names


def imports_cleanly(root: Path = PACKAGE, timeout: float = 120) -> str:
    """Does a fresh interpreter import all of the package at ``root``? "" if so, else why not."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(root.parent), env.get("PYTHONPATH", "")) if p)
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import nohalt.upgrade as u; u.preload()"],
            cwd=root.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if r.returncode == 0:
        return ""
    lines = r.stderr.strip().splitlines()
    return lines[-1] if lines else f"exit {r.returncode}"


class Watch:
    """Has the package on disk changed since this process loaded it, and settled?"""

    def __init__(
        self,
        root: Path = PACKAGE,
        *,
        clock: Callable[[], float] = now,
        settle: float = SETTLE,
        installed: Callable[[], str] = installed_version,
        check: Callable[[Path], str] = imports_cleanly,
    ):
        self.root = root
        self.clock = clock
        self.settle = settle
        self.installed = installed
        self.check = check
        self.running = __version__
        self.loaded = fingerprint(root, installed())
        self._pending: tuple[str, float] | None = None  # (fingerprint, first seen)
        self.rejected: set[str] = set()  # fingerprints that did not import

    def settled(self) -> bool:
        """A different package has been on disk, unchanged, for ``settle`` seconds."""
        seen = fingerprint(self.root, self.installed())
        if seen == self.loaded or seen in self.rejected:
            self._pending = None
            return False
        if self._pending is None or self._pending[0] != seen:
            self._pending = (seen, self.clock())
        return self.clock() - self._pending[1] >= self.settle

    def broken(self) -> str:
        """Why the settled package cannot be switched to ("" when it can). Asked once per change."""
        if self._pending is None:
            return "nothing to switch to"
        why = self.check(self.root)
        if why:
            self.rejected.add(self._pending[0])
            self._pending = None
        return why
