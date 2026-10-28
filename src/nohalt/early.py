"""Starting dependants early, on a synthetic base (SCH-10).

``14: 13`` makes #14 wait until #13 is CLOSED -- a full release cycle per
dependency hop, while #13's PR may have been green for hours. With
``[agents] early_start``, a dependant whose every dependency is closed or has
a PR in the chosen state (``early_start_on``: green, or approved past the
review gate) starts now, on a synthetic base: the base branch with each
unmerged dependency's PR head merged in, in dependency order, through the
merge drivers. A conflict in code means no early start: it waits as before.

Keeping it honest:

* the dependency heads used are recorded on the dependant (``early_on``); a
  dependency that moves leaves a note for the dependant's next launch to merge
  ``origin/<its branch>`` again;
* a dependant never integrates before its dependencies (``held``): the train
  batches it only with them or after them, ``direct`` merges it only after them.
  A dependency closed unmerged or red leaves it working but unable to integrate;
* once every dependency has merged, the dependant's branch is refreshed onto
  the real base (``integrate/refresh.py``), and it is an ordinary PR again.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .forge import CLOSED, OPEN, ForgeError, PullRequest
from .forge import verdict as ci_verdict
from .forge.base import GREEN as V_GREEN
from .forge.base import RED as V_RED
from .queue import Entry
from .state import IN_REVIEW, READY, RUNNING, WAITING_CI, IssueRecord
from .util import run
from .worktree import GitError, git

if TYPE_CHECKING:
    from .supervisor import Supervisor

# What a dependency is, as a dependant sees it.
CLOSED_ = "closed"
UNKNOWN_ = "forge unreachable"
NO_PR = "no PR"
CLOSED_UNMERGED = "PR closed unmerged"
PENDING = "PR pending"
RED = "PR red"
GREEN = "PR green"  # green, but its review gate is not passed yet
APPROVED = "PR approved"  # green, and approved (or no review needed)

IDENT = ["-c", "user.name=nohalt", "-c", "user.email=nohalt@localhost"]


class NoEarlyStart(GitError):
    """The synthetic base could not be built: the dependant waits as usual."""


@dataclass
class Dep:
    issue: int
    state: str
    pr: PullRequest | None = None

    def ready(self, on: str) -> bool:
        return self.state == APPROVED or (on == "green" and self.state == GREEN)

    @property
    def label(self) -> str:
        return GREEN if self.state == APPROVED else self.state  # "approved" only matters to the gate


def _git(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", str(tree), "-c", "core.hooksPath=/dev/null", *IDENT, *args],
        timeout=300,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


class Early:
    def __init__(self, sup: Supervisor):
        self.sup = sup
        self.cfg = sup.cfg
        self._deps: dict[int, Dep] = {}  # per tick
        self._plans: dict[int, list[Dep]] = {}  # dependant -> the dependencies to merge at its launch
        self._said: dict[int, str] = {}  # dependant -> the last "cannot integrate" reason logged

    # -- what a dependency is --------------------------------------------------------------------
    def dep(self, n: int) -> Dep:
        if n not in self._deps:
            self._deps[n] = self._read(n)
        return self._deps[n]

    def _read(self, n: int) -> Dep:
        sup = self.sup
        iss = sup.issue(n)
        if iss.state == CLOSED:
            return Dep(n, CLOSED_)
        if iss.state != OPEN:
            return Dep(n, UNKNOWN_)
        rec = sup.state.get(n)
        known, pr = sup.pr_for(rec)
        if not known:
            return Dep(n, UNKNOWN_)
        if pr is None:
            return Dep(n, CLOSED_UNMERGED if rec.pr else NO_PR)
        try:
            checks = sup.forge.checks(pr.head_sha)
        except ForgeError:
            return Dep(n, UNKNOWN_, pr)
        ignored = set(self.cfg.integrate.ignore_checks)
        v = ci_verdict([c for c in checks if c.name not in ignored], self.cfg.integrate.required_checks)
        if v == V_RED:
            return Dep(n, RED, pr)
        if v != V_GREEN:
            return Dep(n, PENDING, pr)
        approved = not sup.reviews.needed(iss) or rec.review_approved == pr.head_sha
        return Dep(n, APPROVED if approved else GREEN, pr)

    def waiting_for(self, pending: list[int], rec: IssueRecord) -> str:
        """'waiting for #5 (no PR), #6 (PR red)', for the log and `nohalt status`."""
        text = "waiting for " + ", ".join(f"#{d} ({self.dep(d).label})" for d in pending)
        if rec.early_refused and rec.early_refused[0] == self._fingerprint([self.dep(d) for d in pending]):
            text += f"; no early start: {rec.early_refused[1]}"
        return text

    # -- 1. starting early -----------------------------------------------------------------------------
    def tick(self) -> None:
        """A new tick: forget last tick's reads, then follow every early dependant."""
        self._deps = {}
        self._plans = {}
        if self.sup.dry_run:
            return
        for rec in self.sup.state.all():
            if rec.early_on and rec.status not in ("done", "blocked"):
                self._follow(rec)

    def can_start(self, rec: IssueRecord, pending: list[int], entries: list[Entry]) -> bool:
        """May this dependant start although ``pending`` dependencies are still open?"""
        if rec.early_on:
            return True  # it started early: it keeps working whatever its dependencies do
        if not self.cfg.agents.early_start or rec.started_at:
            return False  # only a first launch is built on a synthetic base
        deps = [self.dep(d) for d in pending]
        if not all(d.ready(self.cfg.agents.early_start_on) and d.pr for d in deps):
            return False
        if rec.early_refused and rec.early_refused[0] == self._fingerprint(deps):
            return False  # these very heads conflicted: wait for one to move, or to merge
        self._plans[rec.issue] = self._ordered(deps, entries)
        return True

    @staticmethod
    def _fingerprint(deps: list[Dep]) -> str:
        return " ".join(f"{d.issue}@{d.pr.head_sha if d.pr else ''}" for d in deps)

    @staticmethod
    def _ordered(deps: list[Dep], entries: list[Entry]) -> list[Dep]:
        """Dependency order: a dependency that needs another comes after it (queue order otherwise)."""
        graph = {e.issue: e.deps for e in entries}
        order = _topo([d.issue for d in deps], lambda n: graph.get(n, []))
        return sorted(deps, key=lambda d: order.index(d.issue))

    def build(self, issue: int, tree: Path) -> list[dict[str, Any]]:
        """Merge the planned dependency heads into a fresh worktree. [] when the launch is not early."""
        plan = self._plans.pop(issue, [])
        if not plan:
            return []
        if self.sup.trees.dirty(tree):
            raise NoEarlyStart("its worktree has uncommitted changes")
        start = git(tree, "rev-parse", "HEAD")
        branch = self.sup.trees.current_branch(tree)
        used: list[dict[str, Any]] = []
        from .integrate.mergers import effective, resolve

        mergers = effective(self.cfg.integrate.mergers, self.cfg.integrate.train.version_files)
        for d in plan:
            assert d.pr is not None
            pr = d.pr
            if _git(tree, "cat-file", "-e", f"{pr.head_sha}^{{commit}}").returncode != 0:
                git(tree, "fetch", "-q", self.cfg.project.remote, pr.head_ref, check=False)
            msg = f"merge: {pr.head_ref} (#{d.issue}, {self.sup.pr(pr.number)}) into {branch} (nohalt early start)"
            cp = _git(tree, "merge", "-q", "--no-ff", "--no-edit", "-m", msg, pr.head_sha)
            if cp.returncode != 0:
                conflicted = git(tree, "diff", "--name-only", "--diff-filter=U", check=False).split()
                left = resolve(tree, conflicted, mergers) if conflicted else []
                if conflicted and not left and _git(tree, "commit", "-q", "--no-edit").returncode == 0:
                    pass
                else:
                    _git(tree, "merge", "--abort")
                    _git(tree, "reset", "-q", "--hard", start)
                    what = f"conflicts in code ({', '.join(left)})" if left else (cp.stderr or cp.stdout).strip()[:200]
                    raise NoEarlyStart(f"#{d.issue} ({pr.head_ref}) {what}")
            used.append({"issue": d.issue, "pr": pr.number, "branch": pr.head_ref, "sha": pr.head_sha})
        return used

    def refused(self, rec: IssueRecord, pending: list[int], why: str) -> None:
        """No early start on these heads: wait as usual, and say why once."""
        rec.early_refused = [self._fingerprint([self.dep(d) for d in pending]), why]
        rec.event(f"no early start: {why}; waiting for its dependencies as usual")
        self.sup.state.put(rec)
        self.sup.say(f"#{rec.issue}: no early start -- {why}; waiting for {', '.join('#' + str(d) for d in pending)} as usual")

    def brief(self, rec: IssueRecord, used: list[dict[str, Any]]) -> str:
        """What the agent is told about its synthetic base, and about dependencies that moved."""
        base = self.cfg.project.base_branch
        text = ""
        if used:
            lines = "\n".join(f"- #{u['issue']}: `{u['branch']}` ({self.sup.pr(u['pr'])}, at {u['sha'][:8]})" for u in used)
            text += (
                "\n## You start early, on dependencies that are not merged yet\n\n"
                f"Your branch was cut from `origin/{base}` with these dependencies' pull requests merged in. "
                f"They are **not merged into `{base}` yet**:\n\n{lines}\n\n"
                "Build on them, but do not change or revert their work in your PR: if one is wrong, say so on its "
                "issue. Until they merge, your PR's diff includes their commits; that is expected. Your PR "
                f"integrates only after they have merged, and nohalt merges `{base}` into your branch once they have. "
                "If one of them moves, you will be told to merge `origin/<its branch>` again.\n"
            )
        if rec.early_note:
            text += f"\n## Your dependencies changed\n\n{rec.early_note}\n"
            rec.early_note = ""
        return text

    # -- 2. following an early dependant ------------------------------------------------------------
    def _follow(self, rec: IssueRecord) -> None:
        open_ = [u for u in rec.early_on if self.sup.issue(u["issue"]).state != CLOSED]
        if not open_:
            return self._onto_base(rec)
        changed = False
        for u in open_:
            d = self.dep(u["issue"])
            if d.pr is None or d.pr.head_sha == u["sha"]:
                continue
            note = (
                f"Dependency #{u['issue']} moved: `{d.pr.head_ref}` is at {d.pr.head_sha[:8]} now (you were given "
                f"{u['sha'][:8]}). Merge `origin/{d.pr.head_ref}` into your branch, keeping both sides' behaviour, "
                "re-run your tests, and push."
            )
            rec.early_note = (rec.early_note + "\n\n" + note).strip()
            rec.event(f"dependency #{u['issue']} moved to {d.pr.head_sha[:8]}; noted for the next launch")
            self.sup.say(f"#{rec.issue}: dependency #{u['issue']} moved to {d.pr.head_sha[:8]} -- the agent is told to merge it")
            u.update(sha=d.pr.head_sha, branch=d.pr.head_ref, pr=d.pr.number)
            changed = True
        if changed:
            self.sup.state.put(rec)

    def _onto_base(self, rec: IssueRecord) -> None:
        """Every dependency merged: refresh the branch onto the real base, then it is an ordinary PR."""
        from .integrate.refresh import CONFLICT, DEFERRED, REFRESHED, record_conflicts, refresh

        sup, base = self.sup, self.cfg.project.base_branch
        deps = ", ".join(f"#{u['issue']}" for u in rec.early_on)
        if rec.status == RUNNING:
            return  # never under a running agent: after it stops
        how = ""
        if rec.pr and rec.status in (WAITING_CI, IN_REVIEW, READY):
            known, pr = sup.pr_for(rec)
            if not known:
                return
            if pr is not None:
                out = refresh(sup, rec, pr, clean=True)
                if out.status == DEFERRED:
                    return
                if out.status == REFRESHED:
                    how = f"refreshed onto {base} as {out.sha[:8]}"
                elif out.status == CONFLICT:
                    record_conflicts(sup.state, rec.issue, out.files)
                    sup.hand_back(
                        rec.issue,
                        f"dependencies {deps} merged; {sup.pr(pr.number)} conflicts with {base}",
                        f"Your dependencies ({deps}) have merged. Merge `origin/{base}` into your branch (it conflicts in "
                        f"`{', '.join(out.files)}`), keeping both sides' behaviour, verify, and push.",
                    )
                    how = "handed back to merge the base"
                elif "already contains" in out.why:
                    how = f"already on {base}"
        rec = sup.state.get(rec.issue)  # refresh and hand_back saved their own changes
        if not how:
            rec.early_note = (
                rec.early_note + f"\n\nYour dependencies ({deps}) have merged into `{base}`. Merge `origin/{base}` into "
                "your branch before you continue, re-run your tests, and push."
            ).strip()
            how = "the agent is told to merge the base"
        rec.early_on = []
        rec.early_refused = []
        rec.event(f"dependencies {deps} merged; {how}")
        sup.state.put(rec)
        self._said.pop(rec.issue, None)
        sup.say(f"#{rec.issue}: its dependencies {deps} have merged -- {how}; it integrates as usual now")

    # -- 3. integration order ------------------------------------------------------------------------------
    def held(self, issue: int, batch: set[int] | None = None) -> str:
        """Why this issue may not integrate now ("" when it may). ``batch``: issues going in with it."""
        rec = self.sup.state.get(issue)
        if not rec.early_on:
            return ""
        batch = batch or set()
        waits = []
        for u in rec.early_on:
            n = u["issue"]
            if n in batch or self.sup.issue(n).state == CLOSED:
                continue
            waits.append(f"#{n} ({self.dep(n).label})")
        if waits:
            why = "waits for its dependencies to merge: " + ", ".join(waits)
        elif not batch & {u["issue"] for u in rec.early_on}:
            why = f"its dependencies merged; its branch is refreshed onto {self.cfg.project.base_branch} first"
        else:
            why = ""
        if why and self._said.get(issue) != why:
            self._said[issue] = why
            self.sup.say(f"#{issue}: started early, cannot integrate yet -- {why}")
        return why

    def order(self, cands: list[tuple[int, PullRequest]]) -> list[tuple[int, PullRequest]]:
        """Batch order with each early dependant after its dependencies (otherwise unchanged)."""
        early = {i: [u["issue"] for u in self.sup.state.get(i).early_on] for i, _ in cands}
        if not any(early.values()):
            return cands
        order = _topo([i for i, _ in cands], lambda n: [u["issue"] for u in self.sup.state.get(n).early_on])
        return sorted(cands, key=lambda c: order.index(c[0]))


def _topo(items: list[int], deps: Callable[[int], list[int]]) -> list[int]:
    """``items`` in their order, except that each comes after everything it (transitively) depends on."""

    def needs(a: int, b: int, seen: set[int]) -> bool:
        return any(d == b or (d not in seen and needs(d, b, seen | {d})) for d in deps(a))

    out: list[int] = []
    for n in items:
        at = next((i for i, x in enumerate(out) if needs(x, n, {x})), len(out))
        out.insert(at, n)
    return out
