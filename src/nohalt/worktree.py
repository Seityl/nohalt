"""One git worktree per issue, and never discarding an agent's work.

Two agents in one working tree is how you get a half-committed branch, so
every issue gets its own tree, cut from the base branch. Work an agent leaves
behind is preserved, not cleaned: uncommitted changes are committed to a
``nohalt/rescued-*`` branch before a tree is removed.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .config import Config
from .util import run, slugify, stamp


class GitError(RuntimeError):
    pass


def git(repo: Path | str, *args: str, timeout: int = 300, check: bool = True) -> str:
    # Hooks are the project's, written for humans at a terminal; the
    # supervisor's own bookkeeping commits must not trip them.
    cp = run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and cp.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(cp.stderr or cp.stdout).strip()[:400]}")
    return cp.stdout.strip()


class Worktrees:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.repo = cfg.repo_path
        self.root = cfg.worktree_root
        self.remote = cfg.project.remote
        self.base = cfg.project.base_branch

    def path(self, issue: int) -> Path:
        return self.root / str(issue)

    def branch_for(self, issue: int, title: str, labels: list[str]) -> str:
        """An existing ``<prefix>/<issue>-*`` branch, or a new name for one."""
        refs = git(
            self.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads", f"refs/remotes/{self.remote}", check=False
        )
        for ref in refs.splitlines():
            name = ref.removeprefix(f"{self.remote}/")
            parts = name.split("/", 1)
            if len(parts) == 2 and parts[1].startswith(f"{issue}-"):
                return name
        prefix = self.cfg.project.default_prefix
        low = {lab.lower() for lab in labels}
        for label, pfx in self.cfg.project.branch_prefixes.items():
            if label.lower() in low:
                prefix = pfx
                break
        return f"{prefix}/{issue}-{slugify(title)}"

    def prepare(self, issue: int, branch: str, base_ref: str = "") -> Path:
        tree = self.path(issue)
        git(self.repo, "fetch", "-q", self.remote, check=False)
        if (tree / ".git").exists():
            return tree
        self.root.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "prune", check=False)
        if self._has_ref(f"refs/heads/{branch}"):
            git(self.repo, "worktree", "add", "-q", str(tree), branch)
        elif self._has_ref(f"refs/remotes/{self.remote}/{branch}"):
            git(self.repo, "worktree", "add", "-q", "-b", branch, str(tree), f"{self.remote}/{branch}")
        else:
            start = base_ref or f"{self.remote}/{self.base}"
            git(self.repo, "worktree", "add", "-q", "--no-track", "-b", branch, str(tree), start)
            # A branch cut from the base must not track it: the agent's plain
            # `git push` would aim at the base branch and be refused (or worse).
            git(tree, "branch", "--unset-upstream", check=False)
        git(tree, "config", "push.default", "current", check=False)
        self._link_shared(tree)
        return tree

    def merged(self, branch: str) -> bool:
        """Is everything on ``branch`` already in the base? (A merge commit or a fast-forward;
        a squash merge leaves no trace here -- ask the forge about the PR for that.)"""
        git(self.repo, "fetch", "-q", self.remote, check=False)
        for ref in (f"refs/heads/{branch}", f"refs/remotes/{self.remote}/{branch}"):
            if self._has_ref(ref):
                cp = run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", ref, f"{self.remote}/{self.base}"])
                return cp.returncode == 0
        return False

    def next_branch(self, branch: str) -> str:
        """A new branch name beside ``branch`` that no local or remote ref uses:
        ``<branch>-reopened``, then ``-reopened-2``, ... It keeps the ``<prefix>/<issue>-``
        shape, so the issue's PR is still found by its branch name."""
        stem = re.sub(r"-reopened(?:-\d+)?$", "", branch)
        k = 1
        while True:
            name = f"{stem}-reopened" + (f"-{k}" if k > 1 else "")
            if not self._has_ref(f"refs/heads/{name}") and not self._has_ref(f"refs/remotes/{self.remote}/{name}"):
                return name
            k += 1

    def _link_shared(self, tree: Path) -> None:
        for rel in self.cfg.project.shared_paths:
            src, dst = self.repo / rel, tree / rel
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(src)

    # -- review trees: a detached checkout of a PR head, for a reviewer that must not push ---------
    def review_path(self, issue: int) -> Path:
        return self.root / "_review" / str(issue)

    def prepare_review(self, issue: int, sha: str, branch: str = "") -> Path:
        """A fresh, detached tree at ``sha``. Nothing in it is ever kept: it is cut again for every review."""
        tree = self.review_path(issue)
        git(self.repo, "fetch", "-q", self.remote, check=False)
        if not self._has_commit(sha):
            if branch:
                git(self.repo, "fetch", "-q", self.remote, branch, check=False)
            if not self._has_commit(sha):
                raise GitError(f"the PR head {sha[:12]} is not in the clone after fetching")
        self.remove_review(issue)
        tree.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-q", "--detach", str(tree), sha)
        self._link_shared(tree)
        return tree

    def remove_review(self, issue: int) -> None:
        tree = self.review_path(issue)
        if tree.exists():
            git(self.repo, "worktree", "remove", "--force", str(tree), check=False)
            if tree.exists():
                shutil.rmtree(tree, ignore_errors=True)
            git(self.repo, "worktree", "prune", check=False)

    def _has_commit(self, sha: str) -> bool:
        return run(["git", "-C", str(self.repo), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0

    def _has_ref(self, ref: str) -> bool:
        return run(["git", "-C", str(self.repo), "show-ref", "--verify", "--quiet", ref]).returncode == 0

    def dirty(self, tree: Path) -> bool:
        if not (tree / ".git").exists():
            return False
        out = git(tree, "status", "--porcelain", "--untracked-files=normal", check=False)
        shared = set(self.cfg.project.shared_paths)
        return any(line[3:].rstrip("/") not in shared for line in out.splitlines())

    def rescue(self, issue: int, tree: Path) -> str:
        """Commit whatever is uncommitted to a rescue branch; return its name."""
        if not self.dirty(tree):
            return ""
        name = f"nohalt/rescued-{issue}-{stamp().replace(' ', 'T').replace(':', '').replace('-', '')}"
        current = git(tree, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        git(tree, "checkout", "-q", "-b", name)
        for rel in self.cfg.project.shared_paths:
            git(tree, "rm", "-q", "--cached", "--ignore-unmatch", rel, check=False)
        git(tree, "add", "-A")
        for rel in self.cfg.project.shared_paths:
            git(tree, "reset", "-q", "--", rel, check=False)
        git(
            tree,
            "-c",
            "user.name=nohalt",
            "-c",
            "user.email=nohalt@localhost",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            f"nohalt: work left uncommitted on {current} by #{issue}",
        )
        return name

    def remove(self, issue: int) -> str:
        """Remove an issue's worktree, rescuing uncommitted work first."""
        tree = self.path(issue)
        rescued = ""
        if (tree / ".git").exists():
            rescued = self.rescue(issue, tree)
            git(self.repo, "worktree", "remove", "--force", str(tree), check=False)
        git(self.repo, "worktree", "prune", check=False)
        return rescued

    def commits_ahead(self, tree: Path) -> int:
        out = git(tree, "rev-list", "--count", f"{self.remote}/{self.base}..HEAD", check=False)
        return int(out) if out.isdigit() else 0

    def head(self, tree: Path) -> str:
        return git(tree, "rev-parse", "HEAD", check=False)

    def current_branch(self, tree: Path) -> str:
        return git(tree, "branch", "--show-current", check=False)
