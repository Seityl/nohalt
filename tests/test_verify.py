"""The verification lock (LCK-*)."""

from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
from pathlib import Path

from nohalt import config as config_mod
from nohalt.verify import run_verify


def make_cfg(tmp: Path, repo: Path, **verify):
    data = {
        "project": {"repo": "local:" + str(tmp / "f.json"), "path": str(repo), "state_dir": str(tmp / "state")},
        "verify": verify,
    }
    return config_mod.from_dict(data, root=tmp)


def wt(cfg) -> Path:
    p = cfg.worktree_root / "7"
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_refuses_outside_worktrees(tmp_path, repo):
    cfg = make_cfg(tmp_path, repo)
    err = io.StringIO()
    assert run_verify(cfg, tmp_path, "true", out=io.StringIO(), err=err) == 2
    assert "not under" in err.getvalue()


def test_refused_commands_are_logged(tmp_path, repo):
    cfg = make_cfg(tmp_path, repo, refuse=["drop-site"])
    err = io.StringIO()
    assert run_verify(cfg, wt(cfg), "bench drop-site x", out=io.StringIO(), err=err) == 2
    assert "refused" in err.getvalue()
    assert "drop-site" in (cfg.state_dir / "logs" / "refused.log").read_text()


def test_hooks_and_env_reach_the_command(tmp_path, repo):
    cfg = make_cfg(tmp_path, repo, up="true", reset="echo reset > reset.txt", env="echo DB_PORT=55432")
    out = io.StringIO()
    tree = wt(cfg)
    assert run_verify(cfg, tree, 'test "$DB_PORT" = 55432 && test -f reset.txt', out=out, err=io.StringIO()) == 0
    assert "reset to this commit" in out.getvalue()


def test_failed_hook_is_reported_not_run(tmp_path, repo):
    cfg = make_cfg(tmp_path, repo, up="exit 3")
    err = io.StringIO()
    assert run_verify(cfg, wt(cfg), "true", out=io.StringIO(), err=err) == 3
    assert "could not prepare" in err.getvalue()


def test_exit_code_is_the_commands(tmp_path, repo):
    cfg = make_cfg(tmp_path, repo)
    assert run_verify(cfg, wt(cfg), "exit 5", out=io.StringIO(), err=io.StringIO()) == 5


def test_one_slot_excludes(tmp_path, repo):
    """LCK-01: agents may edit in parallel; they may not verify in parallel."""
    cfg = make_cfg(tmp_path, repo)
    tree = wt(cfg)
    stamps: list[tuple[float, float]] = []

    def go() -> None:
        t0 = time.time()
        run_verify(cfg, tree, "sleep 0.6", out=io.StringIO(), err=io.StringIO())
        stamps.append((t0, time.time()))

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    (a0, _a1), (b0, b1) = sorted(stamps, key=lambda s: s[1])
    assert b1 - min(a0, b0) >= 1.2  # serialised, not overlapped


def test_slot_pool_runs_in_parallel_and_substitutes(tmp_path, repo):
    cfg = make_cfg(
        tmp_path,
        repo,
        slots=[{"name": "a", "env": {"SITE": "site-a"}}, {"name": "b", "env": {"SITE": "site-b"}}],
        placeholder="agents.test",
    )
    tree = wt(cfg)
    outs = [io.StringIO(), io.StringIO()]
    t0 = time.time()
    ts = [
        threading.Thread(
            target=run_verify, args=(cfg, tree, "echo on agents.test; sleep 0.6"), kwargs={"out": o, "err": io.StringIO()}
        )
        for o in outs
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert time.time() - t0 < 1.2  # two slots: concurrent
    # placeholder substituted by each slot's site (printed by the command via bash)
    assert {"a", "b"} == {o.getvalue().split("slot ")[1].split()[0] for o in outs}


def test_backgrounded_child_does_not_hold_the_lock(tmp_path, repo):
    """LCK-02: a server the command backgrounds once held the lock for hours."""
    cfg = make_cfg(tmp_path, repo)
    tree = wt(cfg)
    assert run_verify(cfg, tree, "(sleep 5 &) ; true", out=io.StringIO(), err=io.StringIO()) == 0
    t0 = time.time()
    assert run_verify(cfg, tree, "true", out=io.StringIO(), err=io.StringIO()) == 0
    assert time.time() - t0 < 3


def test_cli_verify_passes_the_command(tmp_path, repo):
    cfg_file = tmp_path / "nohalt.toml"
    cfg_file.write_text(f'[project]\nrepo = "local:{tmp_path}/f.json"\npath = "{repo}"\nstate_dir = "{tmp_path}/state"\n')
    tree = tmp_path / "state" / "work" / "3"
    tree.mkdir(parents=True)
    cp = subprocess.run(
        [sys.executable, "-m", "nohalt", "--config", str(cfg_file), "verify", str(tree), "--", "echo", "hi"],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr
    assert "hi" in cp.stdout
