"""The verification lock: agents may edit in parallel, not verify in parallel.

``nohalt verify <worktree> -- '<command>'`` takes a free slot from the pool
(one exclusive lock per slot), prepares the test environment for the
worktree's commit, runs the command, and releases the slot. Why it exists
differs by project -- a shared test database, a framework that builds assets
for every site at once, a machine with two cores -- so write your reason in
the config. A lock whose reason is not written down gets deleted by the next
person who wants things faster.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import stacks
from .config import Config
from .state import FileLock


def run_verify(cfg: Config, worktree: Path, command: str, *, out=sys.stdout, err=sys.stderr) -> int:
    worktree = worktree.resolve()
    root = cfg.worktree_root.resolve()
    if worktree != root and root not in worktree.parents:
        print(f"verify: {worktree} is not under {root}; verify runs from an agent worktree", file=err)
        return 2
    stack = stacks.get(cfg)
    reason = stack.check_command(command)
    if reason:
        print(f"verify: refused -- {reason}", file=err)
        _note(cfg, f"REFUSED verify in {worktree}: {command}")
        return 2

    slots = cfg.verify.slots
    locks_dir = cfg.state_dir / "locks"
    started = time.time()
    held: FileLock | None = None
    slot = slots[0]
    said = False
    while held is None:
        for s in slots:
            lock = FileLock(locks_dir / f"verify-{s.name}.lock")
            if lock.acquire(blocking=False):
                held, slot = lock, s
                break
        if held is None:
            if not said:
                print("verify: every test slot is in use; waiting...", file=err, flush=True)
                said = True
            time.sleep(3)
    try:
        waited = int(time.time() - started)
        if waited > 5:
            print(f"verify: waited {waited}s for a test slot", file=err)
        print(f"verify: slot {slot.name} is yours -- {worktree}", file=out, flush=True)
        if cfg.verify.placeholder and slot.env.get("SITE"):
            command = command.replace(cfg.verify.placeholder, slot.env["SITE"])
        if cfg.verify.clean_pycache and cfg.stack.name == "generic":
            for cache in worktree.rglob("__pycache__"):
                if ".venv" not in cache.parts and "node_modules" not in cache.parts:
                    shutil.rmtree(cache, ignore_errors=True)
        try:
            prepared = stack.prepare(worktree, slot)
        except Exception as e:
            print(f"verify: could not prepare the test environment: {e}", file=err)
            return 3
        for note in prepared.notes:
            print(f"verify: {note}", file=out)
        env = {**os.environ, **prepared.env, "NOHALT_SLOT": slot.name, "NOHALT_WORKTREE": str(worktree)}
        if cfg.verify.pythonpath_worktree:
            env["PYTHONPATH"] = str(worktree) + (f":{os.environ['PYTHONPATH']}" if os.environ.get("PYTHONPATH") else "")
        print(f"verify: running -- {command}", file=out, flush=True)
        # close_fds (the default) keeps the lock's descriptor out of the child:
        # a server the command backgrounds must not hold the slot for hours.
        rc = subprocess.call(["bash", "-lc", command], cwd=prepared.cwd, env=env, close_fds=True)
        print(f"verify: command exited {rc}", file=out)
        return rc
    finally:
        held.release()
        print("verify: released", file=out)


def _note(cfg: Config, text: str) -> None:
    from .util import stamp

    p = cfg.state_dir / "logs" / "refused.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(f"{stamp()}  {text}\n")
