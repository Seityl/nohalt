"""The agent guard shims (SAF-*)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nohalt import shims


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    d = shims.install(tmp_path / "bin")
    # A fake "real gh" that just echoes, so nothing reaches GitHub.
    real = tmp_path / "real"
    real.mkdir()
    (real / "gh").write_text('#!/bin/sh\necho REAL-GH "$@"\n')
    (real / "gh").chmod(0o755)
    return d


def run(bin_dir: Path, tmp: Path, *argv: str, cwd: Path | None = None, extra: dict[str, str] | None = None):
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{tmp / 'real'}:{os.environ['PATH']}",
        "NOHALT_GUARD_LOG": str(tmp / "guard.log"),
        "NOHALT_BASE_BRANCH": "main",
        **(extra or {}),
    }
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "pr", "merge", "5"],
        ["gh", "pr", "close", "5"],
        ["gh", "issue", "close", "7"],
        ["gh", "release", "create", "v1"],
        ["gh", "api", "repos/o/r/pulls/5/merge", "-X", "PUT"],
        ["gh", "api", "repos/o/r/issues/7", "-X", "PATCH", "-f", "state=closed"],
    ],
)
def test_gh_refuses_accepting_or_declaring_work(bin_dir, tmp_path, argv):
    cp = run(bin_dir, tmp_path, *argv)
    assert cp.returncode == 1
    assert "refused" in cp.stderr
    assert "REFUSED" in (tmp_path / "guard.log").read_text()


@pytest.mark.parametrize(
    "argv", [["gh", "pr", "create", "--fill"], ["gh", "issue", "view", "7"], ["gh", "api", "repos/o/r/issues/7"]]
)
def test_gh_passes_the_agents_own_work_through(bin_dir, tmp_path, argv):
    cp = run(bin_dir, tmp_path, *argv)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.startswith("REAL-GH")


def test_gh_shim_never_execs_itself(bin_dir, tmp_path):
    """LIV-08: a shim that resolved itself as the real binary looped at 100% CPU for days."""
    cp = run(bin_dir, tmp_path, "gh", "issue", "view", "1", extra={"NOHALT_REAL_GH": str(bin_dir / "gh")})
    assert cp.returncode == 0
    assert cp.stdout.startswith("REAL-GH")


def test_gh_token_use_is_logged(bin_dir, tmp_path):
    run(bin_dir, tmp_path, "gh", "auth", "token")
    assert "TOKEN-TAKEN" in (tmp_path / "guard.log").read_text()


def test_git_refuses_stash_but_allows_list(bin_dir, tmp_path, repo):
    """GIT-05: worktrees share one stash stack."""
    cp = run(bin_dir, tmp_path, "git", "stash", cwd=repo)
    assert cp.returncode == 1 and "stash" in cp.stderr
    cp = run(bin_dir, tmp_path, "git", "-C", str(repo), "stash", "push", "-m", "x")
    assert cp.returncode == 1
    assert run(bin_dir, tmp_path, "git", "stash", "list", cwd=repo).returncode == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push", "origin", "main"],
        ["git", "push", "origin", "HEAD:main"],
        ["git", "push", "origin", "+feature:refs/heads/main"],
        ["git", "push", "--tags"],
        ["git", "push", "origin", "refs/tags/v1"],
    ],
)
def test_git_refuses_pushing_the_base_or_tags(bin_dir, tmp_path, repo, argv):
    cp = run(bin_dir, tmp_path, *argv, cwd=repo)
    assert cp.returncode == 1, cp.stdout
    assert "refused" in cp.stderr


def test_git_allows_pushing_a_branch(bin_dir, tmp_path, repo):
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature/1-x"], check=True)
    cp = run(bin_dir, tmp_path, "git", "push", "-q", "origin", "feature/1-x", cwd=repo)
    assert cp.returncode == 0, cp.stderr


def test_bare_push_on_base_is_refused(bin_dir, tmp_path, repo):
    cp = run(bin_dir, tmp_path, "git", "push", cwd=repo)
    assert cp.returncode == 1


def test_pkill_only_touches_the_callers_worktree(bin_dir, tmp_path):
    """SAF-04: a broad pkill once killed CI builds on four runners."""
    if not shutil.which("pgrep"):
        pytest.skip("pgrep not available")
    inside = tmp_path / "wt"
    outside = tmp_path / "other"
    inside.mkdir()
    outside.mkdir()
    subprocess.run(["git", "init", "-q", str(inside)], check=True)
    marker = "nohalt-pkill-test-sleep"
    a = subprocess.Popen(["bash", "-c", f"exec -a {marker} sleep 30"], cwd=inside)
    b = subprocess.Popen(["bash", "-c", f"exec -a {marker} sleep 30"], cwd=outside)
    try:
        cp = run(bin_dir, tmp_path, "pkill", "-f", f"python|{marker}", cwd=inside)
        assert "signalled 1" in cp.stderr, cp.stderr
        a.wait(timeout=5)
        assert b.poll() is None  # the process outside the worktree survives
    finally:
        for p in (a, b):
            if p.poll() is None:
                p.kill()
