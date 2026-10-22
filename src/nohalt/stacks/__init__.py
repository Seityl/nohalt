"""Stacks: what a project's test environment and bookkeeping look like.

The core knows nothing about any framework. A stack fills in defaults
(verify slots, refused commands, merge drivers), contributes a prompt
addendum, and prepares the test environment inside the verification lock.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config, ConfigError, VerifySlot
from ..util import run


@dataclass
class Prepared:
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Stack:
    name = "base"
    prompt_addendum = ""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def apply_defaults(self) -> None:
        pass

    def check_command(self, command: str) -> str | None:
        """A reason to refuse ``command``, or None."""
        for bad in self.cfg.verify.refuse:
            if bad and bad in command:
                return f"`{bad}` reaches past this agent's test environment"
        return None

    def prepare(self, worktree: Path, slot: VerifySlot) -> Prepared:
        raise NotImplementedError


class Generic(Stack):
    """Hooks are shell commands: ``up``, ``reset`` and ``env`` in ``[verify]``.

    ``env`` prints KEY=VALUE lines; they are exported to the agent's command.
    Keep the test environment identical to CI's service definitions: a local
    environment that disagrees with CI gives confident wrong answers.
    """

    name = "generic"

    def _hook(self, cmd: str, worktree: Path, slot: VerifySlot, what: str) -> str:
        env = {**os.environ, **slot.env, "NOHALT_WORKTREE": str(worktree), "NOHALT_SLOT": slot.name}
        cp = run(["bash", "-c", cmd], cwd=worktree, env=env, timeout=1800)
        if cp.returncode != 0:
            raise RuntimeError(f"verify {what} hook failed ({cp.returncode}): {(cp.stderr or cp.stdout).strip()[-800:]}")
        return cp.stdout

    def prepare(self, worktree: Path, slot: VerifySlot) -> Prepared:
        v = self.cfg.verify
        p = Prepared(cwd=worktree, env=dict(slot.env))
        if v.up:
            self._hook(v.up, worktree, slot, "up")
            p.notes.append("test environment up")
        if v.reset:
            self._hook(v.reset, worktree, slot, "reset")
            p.notes.append("test environment reset to this commit")
        if v.env:
            for line in self._hook(v.env, worktree, slot, "env").splitlines():
                line = line.strip().removeprefix("export ").strip()
                if "=" in line and not line.startswith("#"):
                    k, _, val = line.partition("=")
                    p.env[k.strip()] = shlex.split(val)[0] if val.strip() else ""
        return p


def get(cfg: Config) -> Stack:
    name = cfg.stack.name
    if name == "generic":
        stack: Stack = Generic(cfg)
    elif name == "frappe":
        from .frappe import Frappe

        stack = Frappe(cfg)
    else:
        raise ConfigError(f"[stack] unknown stack {name!r} (known: generic, frappe)")
    stack.apply_defaults()
    return stack
