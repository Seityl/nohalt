"""Guard shims put first on every agent's PATH.

They are speed bumps with a log, not walls: branch protection on the forge is
the hard control. See docs/SAFETY.md.
"""

from __future__ import annotations

import os
import shutil
import stat
from importlib import resources
from pathlib import Path

SHIMS = {
    "gh": "gh",
    "glab": "glab",
    "git": "git",
    "pkill": "pkill",
    "killall": "pkill",
    # Package managers: refuse installs that would write through a shared_paths link.
    **{pm: "pkgguard" for pm in ("npm", "pnpm", "yarn", "pip", "pip3", "uv")},
}


def shared_targets(repo: Path, shared_paths: list[str]) -> str:
    """The real paths of the shared folders, for the package-manager guard."""
    return ":".join(str((repo / p).resolve()) for p in shared_paths if (repo / p).exists())


def install(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    pkg = resources.files("nohalt.shims")
    for name, src in SHIMS.items():
        dst = bin_dir / name
        text = pkg.joinpath(src).read_text()
        if not dst.exists() or dst.read_text() != text:
            tmp = dst.with_suffix(".tmp")
            tmp.write_text(text)
            tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.replace(tmp, dst)
    return bin_dir


def real(name: str, path: str | None = None) -> str:
    """Resolve the real binary BEFORE the shim directory goes on PATH."""
    found = shutil.which(name, path=path)
    return str(Path(found).resolve()) if found else ""
