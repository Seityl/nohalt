"""Refresh a conflicting PR branch mechanically before handing it back (INT-12).

A PR that conflicts with the base gets no CI on most forges, and handing it
back costs an agent round -- three of them, and the issue went to a human,
when every conflicted file was a version stamp, a generated reference or a
translations list. So before any hand-back for conflicts, the integrator tries
what the human did: merge the base into the PR branch, settle each conflicted
file with its merge driver, re-run the regenerate command, commit, push.

* It works in its own detached tree (``<worktrees>/_refresh``), never in the
  agent's, and never while the issue's agent is running (that would race its
  pushes): it waits for a later tick instead.
* It merges in the same direction as a release (the base checked out, the PR
  merged in), so every driver means the same thing in both places, then
  records the result as a merge of the base into the branch (first parent:
  the PR head), so the push is a fast-forward. Nothing is force-pushed.
* If any file is left in conflict, a driver fails, the regenerate command
  fails, or the push is refused, nothing is pushed and the caller hands the PR
  back exactly as before.

Refreshes are counted in ``refresh.json``, apart from hand-back rounds. Files
left in conflict at a hand-back are kept in ``conflicts.json`` for
``nohalt doctor``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..forge import PullRequest
from ..state import RUNNING, WAITING_CI, IssueRecord, StateStore
from ..util import now, run
from ..worktree import GitError, git
from .mergers import driver_for, effective, resolve

if TYPE_CHECKING:
    from ..config import Config
    from ..supervisor import Supervisor

REFRESHED = "refreshed"  # pushed: the PR goes through CI again, no hand-back
DEFERRED = "deferred"  # not now (its agent is running, the branch moved): ask again next tick
CONFLICT = "conflict"  # files no driver settles: hand back
FAILED = "failed"  # anything else (disabled, not our branch, push refused): hand back

IDENT = ["-c", "user.name=nohalt", "-c", "user.email=nohalt@localhost"]
HISTORY_DAYS = 14


@dataclass
class Outcome:
    status: str
    why: str = ""
    files: list[str] = field(default_factory=list)  # left in conflict
    sha: str = ""  # the new head, when refreshed

    @property
    def handled(self) -> bool:
        """No hand-back now: refreshed, or deferred to a later tick."""
        return self.status in (REFRESHED, DEFERRED)


def _git(tree: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", str(tree), "-c", "core.hooksPath=/dev/null", *IDENT, *args],
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def refresh(sup: Supervisor, rec: IssueRecord, pr: PullRequest, *, clean: bool = False) -> Outcome:
    """Merge the base into ``pr``'s branch if the merge drivers settle every conflict.

    ``clean``: a merge without conflicts is pushed too (an early dependant moving
    onto the real base once its dependencies merged, SCH-10). Otherwise there is
    nothing a refresh can fix in it.
    """
    cfg = sup.cfg
    base, remote = cfg.project.base_branch, cfg.project.remote
    if not cfg.integrate.refresh_conflicts or cfg.integrate.mode == "none":
        return Outcome(FAILED, "refresh_conflicts is off")
    if rec.status == RUNNING:
        return Outcome(DEFERRED, "its agent is running")
    branch = pr.head_ref
    if (
        not branch
        or branch == base
        or branch.startswith(cfg.integrate.train.branch_prefix)
        or (pr.base_ref and pr.base_ref != base)
    ):
        return Outcome(FAILED, f"{branch} is not an agent branch into {base}")
    tree = cfg.worktree_root / "_refresh"
    try:
        _ensure_tree(cfg, tree)
        git(tree, "fetch", "-q", remote)
        _git(tree, "merge", "--abort")
        git(tree, "reset", "-q", "--hard")
        git(tree, "clean", "-qfd")
        head = git(tree, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{branch}", check=False)
        base_sha = git(tree, "rev-parse", f"refs/remotes/{remote}/{base}")
        if not head:
            return Outcome(FAILED, f"{remote}/{branch} not found (a fork?)")
        if head != pr.head_sha:
            return Outcome(DEFERRED, f"{branch} moved to {head[:8]} since the forge was asked")
        if _git(tree, "merge-base", "--is-ancestor", base_sha, head).returncode == 0:
            return Outcome(FAILED, f"{branch} already contains {base}: nothing to refresh")
        git(tree, "checkout", "-q", "--detach", base_sha)
    except GitError as e:
        return Outcome(FAILED, f"cannot prepare the refresh tree: {e}")
    merged = _git(tree, "merge", "-q", "--no-ff", "--no-commit", head)
    if merged.returncode != 0:
        conflicted = git(tree, "diff", "--name-only", "--diff-filter=U", check=False).split()
        if not conflicted:
            _git(tree, "merge", "--abort")
            return Outcome(FAILED, f"merge failed: {(merged.stderr or merged.stdout).strip()[:200]}")
        left = resolve(tree, conflicted, effective(cfg.integrate.mergers, cfg.integrate.train.version_files))
        if left:
            _git(tree, "merge", "--abort")
            return Outcome(CONFLICT, "conflicts no merge driver settles", files=left)
        settled = conflicted
    elif clean:
        settled = []
    else:
        # git merged it cleanly, so the forge's conflict is nothing a refresh can fix.
        _git(tree, "merge", "--abort")
        return Outcome(FAILED, f"{branch} merges {base} cleanly here")

    try:
        snapshot = git(tree, "write-tree")
        msg = f"merge: {base} into {branch} (nohalt refresh)"
        if settled:
            msg += f"\n\nSettled by merge drivers: {', '.join(settled)}"
        commit = git(tree, *IDENT, "commit-tree", snapshot, "-p", head, "-p", base_sha, "-m", msg)
        git(tree, "reset", "-q", "--hard", commit)
    except GitError as e:
        _git(tree, "merge", "--abort")
        return Outcome(FAILED, f"cannot commit the merge: {e}")
    regen = cfg.integrate.train.after_version
    if regen:
        cp = run(["bash", "-c", regen], cwd=tree, timeout=1800)
        if cp.returncode != 0:
            return Outcome(FAILED, f"`{regen}` exited {cp.returncode}: {(cp.stderr or cp.stdout).strip()[-200:]}")
        outputs = _changed(tree)
        if outputs:
            try:
                git(tree, "add", "--", *outputs)
                git(tree, *IDENT, "commit", "-q", "-m", f"chore: regenerate after merging {base} (nohalt refresh)")
            except GitError as e:
                return Outcome(FAILED, f"cannot commit the regenerated files: {e}")
    new = git(tree, "rev-parse", "HEAD", check=False)
    pushed = _git(tree, "push", "-q", remote, f"HEAD:refs/heads/{branch}", timeout=600)
    if pushed.returncode != 0:
        now_at = git(tree, "ls-remote", remote, f"refs/heads/{branch}", check=False).split("\t")[0]
        if now_at and now_at != head:
            return Outcome(DEFERRED, f"{branch} moved while refreshing")
        return Outcome(FAILED, f"push refused: {(pushed.stderr or pushed.stdout).strip()[:200]}")

    _count(sup.state, rec.issue, new)
    _follow(sup, rec, branch)
    rec.pr_sha = new
    rec.status = WAITING_CI
    rec.event(f"{sup.pr(pr.number)} refreshed: merged {base} ({', '.join(settled) or 'cleanly'}) as {new[:8]}")
    sup.state.put(rec)
    return Outcome(REFRESHED, sha=new, files=settled)


def before_hand_back(sup: Supervisor, rec: IssueRecord, pr: PullRequest) -> bool:
    """Try a refresh before handing ``pr`` back for conflicts. True: do not hand it back now."""
    out = refresh(sup, rec, pr)
    base = sup.cfg.project.base_branch
    if out.status == REFRESHED:
        n = sup.state.load("refresh.json", {}).get(str(rec.issue), {}).get("count", 0)
        sup.say(
            f"#{rec.issue}: {sup.pr(pr.number)} conflicted with {base} only in {', '.join(out.files)} -- "
            f"refreshed by nohalt as {out.sha[:8]} (refresh {n}; not handed back)"
        )
    elif out.status == DEFERRED:
        sup.state.log(f"#{rec.issue}: refresh of {sup.pr(pr.number)} deferred: {out.why}")
    else:
        if out.files:
            record_conflicts(sup.state, rec.issue, out.files)
        sup.state.log(f"#{rec.issue}: no refresh for {sup.pr(pr.number)} ({out.why}); handing back")
    return out.handled


def _ensure_tree(cfg: Config, tree: Path) -> None:
    if (tree / ".git").exists():
        return
    tree.parent.mkdir(parents=True, exist_ok=True)
    git(cfg.repo_path, "worktree", "prune", check=False)
    git(cfg.repo_path, "worktree", "add", "-q", "--detach", str(tree), f"{cfg.project.remote}/{cfg.project.base_branch}")


def _changed(tree: Path) -> list[str]:
    """Paths the regenerate command changed or created."""
    out = _git(tree, "status", "--porcelain", "-z", "--untracked-files=all").stdout  # unstripped: columns matter
    paths = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        paths.append(e[3:])
        if e[0] in "RC":
            i += 1  # a rename's source follows
    return paths


def _follow(sup: Supervisor, rec: IssueRecord, branch: str) -> None:
    """Fast-forward the agent's own worktree, if it has one, so its next push is not refused."""
    tree = Path(rec.worktree) if rec.worktree else sup.trees.path(rec.issue)
    if not (tree / ".git").exists() or sup.trees.current_branch(tree) != branch:
        return
    cp = _git(tree, "merge", "-q", "--ff-only", f"refs/remotes/{sup.cfg.project.remote}/{branch}")
    if cp.returncode != 0:
        sup.state.log(f"#{rec.issue}: worktree not fast-forwarded to the refresh ({cp.stderr.strip()[:200]})")


def _count(state: StateStore, issue: int, sha: str) -> None:
    st: dict[str, Any] = state.load("refresh.json", {})
    e = st.setdefault(str(issue), {"count": 0})
    e["count"] = e.get("count", 0) + 1
    e["sha"], e["at"] = sha, now()
    state.save("refresh.json", st)


# -- conflict history (nohalt doctor) --------------------------------------------------------------
def record_conflicts(state: StateStore, issue: int, files: list[str]) -> None:
    """Files left in conflict at a hand-back."""
    st: dict[str, Any] = state.load("conflicts.json", {"events": []})
    st["events"].append({"at": now(), "issue": issue, "files": sorted(set(files))})
    st["events"] = st["events"][-200:]
    state.save("conflicts.json", st)


def recurring_conflicts(cfg: Config, state: StateStore, times: int = 2, days: int = HISTORY_DAYS) -> list[tuple[str, int, str]]:
    """(path, hand-backs, suggested driver) for files that keep conflicting and have no driver."""
    since = now() - days * 86400
    seen: dict[str, int] = {}
    for ev in state.load("conflicts.json", {"events": []}).get("events", []):
        if ev.get("at", 0) < since:
            continue
        for path in ev.get("files", []):
            seen[path] = seen.get(path, 0) + 1
    mergers = effective(cfg.integrate.mergers, cfg.integrate.train.version_files)
    return [
        (path, n, suggest(path))
        for path, n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
        if n >= times and driver_for(mergers, path) is None
    ]


def suggest(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name.endswith((".lock", "-lock.json", "-lock.yaml")):
        return '"fail" (a lockfile is regenerated by its tool, never merged)'
    if "generated" in path or path.startswith(("dist/", "build/")):
        return '"take_theirs", then regenerate with [integrate.train] after_version'
    if name == "package.json" or name.endswith(".json"):
        return '"package_json" (three-way per key) if it is a manifest'
    if name.endswith((".csv", ".txt", ".po")):
        return '"keep_both_lines" if both sides only append lines'
    if name.endswith((".md", ".rst")):
        return '"keep_both_blocks" if both sides only add sections'
    return "a driver if it is bookkeeping; if it is code, it is a hot file (LESSONS: hot-file rebase races)"
