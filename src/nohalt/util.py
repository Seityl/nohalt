"""Small helpers shared by every module: time, files, processes."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def parse_duration(value: Any) -> int:
    """Seconds from ``90``, ``"90s"``, ``"30m"``, ``"4h"`` or ``"2d"``."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    m = _DURATION.match(str(value))
    if not m:
        raise ValueError(f"not a duration: {value!r} (use e.g. 90, '90s', '30m', '4h')")
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def now() -> float:
    return time.time()


def stamp(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else now()))


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def tail(path: Path, max_bytes: int = 4000) -> str:
    """The last ``max_bytes`` of a file, ANSI stripped; empty if unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            return strip_ansi(f.read().decode("utf-8", "replace"))
    except OSError:
        return ""


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie is dead for our purposes: it has finished and only awaits reaping.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def children(pid: int) -> list[int]:
    """Direct child pids, read from /proc (Linux)."""
    out: list[int] = []
    for p in Path("/proc").glob("[0-9]*"):
        try:
            stat = (p / "stat").read_text()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if len(fields) > 1 and fields[1] == str(pid):
            out.append(int(p.name))
    return out


def descendants(pid: int) -> list[int]:
    found: list[int] = []
    stack = [pid]
    while stack:
        for c in children(stack.pop()):
            found.append(c)
            stack.append(c)
    return found


def kill_tree(pid: int, sig: int = signal.SIGTERM) -> None:
    """Signal a process, its process group and every descendant.

    Agents start in their own session, so the group usually suffices; the
    descendant walk catches anything that called setsid itself.
    """
    targets = [*descendants(pid), pid]
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass
    for p in targets:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def mem_available_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def run(
    argv: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = 60,
    env: dict[str, str] | None = None,
    check: bool = False,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, capture text output. A timeout returns rc 124, never hangs."""
    try:
        cp = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        cp = subprocess.CompletedProcess(argv, 124, _text(e.stdout), _text(e.stderr) + "\n[timed out]")
    except FileNotFoundError as e:
        cp = subprocess.CompletedProcess(argv, 127, "", str(e))
    if check and cp.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {cp.returncode}: {cp.stderr.strip()[:500]}")
    return cp


def _text(v: bytes | str | None) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "work"
