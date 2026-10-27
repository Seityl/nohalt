from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nohalt import config as config_mod
from nohalt.forge.local import LocalForge
from nohalt.state import RUNNING
from nohalt.supervisor import Supervisor

HERE = Path(__file__).resolve().parent


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def git(cwd: Path, *args: str) -> str:
    return sh("git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args, cwd=cwd)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone of a bare 'origin' with one commit on main."""
    origin = tmp_path / "origin.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    sh("git", "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "-b", "main")
    (clone / "README.md").write_text("hello\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "init")
    git(clone, "push", "-q", "-u", "origin", "main")
    return clone


class World:
    """A configured project: repo, forge, queue, scripted agents."""

    def __init__(self, tmp: Path, repo: Path, **overrides: Any):
        self.tmp = tmp
        self.repo = repo
        self.forge_path = tmp / "forge.json"
        self.forge = LocalForge(self.forge_path)
        self.scripts = tmp / "scripts"
        self.scripts.mkdir()
        self.queue = tmp / "queue.txt"
        self.queue.write_text("")
        data: dict[str, Any] = {
            "project": {"repo": f"local:{self.forge_path}", "path": str(repo), "state_dir": str(tmp / "state")},
            "queue": {"file": str(self.queue), "tick": 1, "poll_when_idle": 1},
            "agents": {
                "max_parallel": 2,
                "min_free_mb": 0,
                "provider_backoff": 0,
                "revive_grace": 0,
                "options": {
                    "command": {
                        "argv": [
                            sys.executable,
                            str(HERE / "fake_agent.py"),
                            str(self.forge_path),
                            str(self.scripts),
                            "{prompt}",
                        ],
                    }
                },
            },
            "models": {"default": "command:fake", "probe": False},
        }
        for section, values in overrides.items():
            data.setdefault(section, {}).update(values)
        self.data = data
        self.cfg = config_mod.from_dict(data, root=tmp)
        self.messages: list[str] = []
        self.sup = Supervisor(self.cfg, forge=self.forge, say=self.messages.append)

    def issue(self, n: int, *script: str, deps: list[int] | None = None, labels: list[str] | None = None) -> None:
        self.forge.add_issue(n, f"Issue {n}", labels=labels)
        (self.scripts / f"{n}.txt").write_text("\n".join(script) + "\n")
        with open(self.queue, "a") as f:
            f.write(f"{n}: {' '.join(map(str, deps or []))}\n")

    def wait_agents(self, timeout: float = 30) -> None:
        end = time.time() + timeout
        while time.time() < end:
            running = [r for r in self.sup.state.all() if r.status == RUNNING and self.sup._alive(r)]
            running += [r for r in self.sup.state.all() if r.review_pid and self.sup.reviews._alive(r)]
            if not running:
                return
            time.sleep(0.1)
        raise AssertionError("agents still running")

    def drive(self, ticks: int = 10) -> None:
        """Tick, letting launched agents finish between ticks."""
        for _ in range(ticks):
            self.sup.tick()
            self.wait_agents()
            for r in self.sup.state.all():  # skip backoffs in tests
                if r.not_before or r.review_not_before:
                    r.not_before = r.review_not_before = 0
                    self.sup.state.put(r)

    def rec(self, n: int):  # type: ignore[no-untyped-def]
        return self.sup.state.get(n)

    def review(self, n: int, *script: str) -> None:
        (self.scripts / f"{n}.review.txt").write_text("\n".join(script) + "\n")

    def review_prompts(self, n: int) -> list[str]:
        p = self.scripts / f"{n}.review-prompts"
        return [x for x in p.read_text().split("\n=====\n") if x.strip()] if p.exists() else []

    def prompts(self, n: int) -> list[str]:
        p = self.scripts / f"{n}.prompts"
        return [x for x in p.read_text().split("\n=====\n") if x.strip()] if p.exists() else []


@pytest.fixture
def world(tmp_path: Path, repo: Path):  # type: ignore[no-untyped-def]
    def make(**overrides: Any) -> World:
        return World(tmp_path, repo, **overrides)

    return make
