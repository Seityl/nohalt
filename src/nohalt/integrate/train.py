"""The release train: batch green PRs, resolve bookkeeping, version, merge on green.

Merging agent PRs one at a time re-based every sibling and restarted its CI;
batching them by hand cost a night. So, on every tick:

1. An open ``release/*`` PR that is green and mergeable is merged, tagged and
   deployed. Red once: its failed jobs are re-run (load flakes happen). Red on
   the network: re-run again, never charged to the PRs it carries. Red twice:
   it is closed so later batches are not blocked, the carried PRs' head SHAs
   are parked until their agents push a fix, and each issue is handed back.
2. With no release PR open, every READY issue's green PR is merged into a
   fresh release branch off the base. Bookkeeping conflicts are resolved by
   the configured merge drivers; a conflict in real code is not -- that PR is
   left out and its issue handed back to rebase. The branch gets the next
   free version, and a PR that closes the issues it carries. CI's full suite
   on the batch is the gate.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import TYPE_CHECKING, Any

from .. import queue
from ..forge import CLOSED, ForgeError, PullRequest, verdict
from ..forge.base import GREEN, RED
from ..state import READY
from ..worktree import GitError, git
from .common import PipelineGate, cancel_superseded, close_merged, is_network_failure, run_deploy
from .mergers import effective, resolve
from .refresh import REFRESHED, record_conflicts, refresh

if TYPE_CHECKING:
    from ..supervisor import Supervisor

NETWORK_RERUNS = 3


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


class Train:
    def __init__(self, sup: Supervisor):
        self.sup = sup
        self.cfg = sup.cfg
        self.t = sup.cfg.integrate.train
        self.tree = sup.cfg.worktree_root / "_release"
        self.gate = PipelineGate(sup)

    def _st(self) -> dict[str, Any]:
        return self.sup.state.load("train.json", {"releases": {}, "parked": [], "reruns": {}})

    def _save(self, st: dict[str, Any]) -> None:
        self.sup.state.save("train.json", st)

    def tick(self) -> None:
        if self.cfg.integrate.cancel_superseded:
            cancel_superseded(self.sup)
        if not self.gate.open():
            return
        pulls = self.sup.pulls()
        if pulls is None:
            return
        releases = [p for p in pulls if p.head_ref.startswith(self.t.branch_prefix)]
        for p in releases:
            self._advance(p)
        if not releases:
            self._build()

    # -- 1. an open release ----------------------------------------------------------------------
    def _advance(self, p: PullRequest) -> None:
        f = self.sup.forge
        try:
            v = verdict(f.checks(p.head_sha), self.cfg.integrate.required_checks, self.cfg.integrate.ignore_checks)
        except ForgeError:
            return
        if v == RED:
            return self._red(p)
        if v != GREEN:
            return
        try:
            pr = f.pull(p.number)
        except ForgeError:
            return
        if pr.mergeable is None:
            return
        st = self._st()
        rel = st["releases"].get(p.head_ref, {})
        if pr.mergeable is False:
            f.close_pull(
                p.number,
                "nohalt: this release no longer merges cleanly (the base moved). Closed; its PRs go into the next batch.",
            )
            st["releases"].pop(p.head_ref, None)
            self._save(st)
            self.sup.say(f"release {self.sup.pr(p.number)} conflicts with the base now -- closed, rebatching")
            return
        if not f.merge_pull(p.number, "merge"):
            self.sup.say(f"merge of release {self.sup.pr(p.number)} refused; will retry")
            return
        version = rel.get("version", "")
        self.sup.say(f"release {self.sup.pr(p.number)} ({p.head_ref}) merged" + (f" as {version}" if version else ""))
        if version:
            self._tag(version)
        carried = rel.get("carried", [])
        close_merged(self.sup, [int(c[0]) for c in carried], f"release {self.sup.pr(p.number)}")
        # The release merged each PR's branch, not the PR itself: close the PRs,
        # or every one of them stays open forever.
        for _issue, pr_number, _sha in carried:
            try:
                f.close_pull(int(pr_number), f"nohalt: merged into {version or p.head_ref} by release {self.sup.pr(p.number)}.")
            except ForgeError as e:
                self.sup.say(f"could not close carried {self.sup.pr(int(pr_number))}: {e}")
        run_deploy(self.sup, version or p.head_ref)
        st["releases"].pop(p.head_ref, None)
        self._save(st)
        self.gate.merged(version or p.head_ref, deploy_after=False)

    def _red(self, p: PullRequest) -> None:
        f = self.sup.forge
        st = self._st()
        count = st["reruns"].get(p.head_sha, 0)
        try:
            failed = [r for r in f.runs(head_sha=p.head_sha) if r.conclusion in ("failure", "timed_out")]
        except ForgeError:
            return
        ids = [r.id for r in failed]
        if count < self.t.red_retries or (count < self.t.red_retries + NETWORK_RERUNS and is_network_failure(self.sup, ids)):
            for rid in ids:
                try:
                    f.rerun_failed(rid)
                except ForgeError:
                    pass
            st["reruns"][p.head_sha] = count + 1
            self._save(st)
            self.sup.say(f"release {self.sup.pr(p.number)} red on {p.head_sha[:8]} -- re-running the failed jobs ({count + 1})")
            return
        urls = " ".join(r.url for r in failed if r.url)
        f.close_pull(
            p.number,
            f"nohalt: red after {count} re-run(s) on {p.head_sha[:8]} ({urls}). Closed so later "
            "batches are not blocked; the PRs it carried are parked until they push a fix.",
        )
        rel = st["releases"].pop(p.head_ref, {})
        for issue, pr, sha in rel.get("carried", []):
            st["parked"].append(sha)
            self.sup.hand_back(
                issue,
                f"release {self.sup.pr(p.number)} carrying {self.sup.pr(pr)} failed CI",
                f"Release {self.sup.pr(p.number)}, which carried your {self.sup.pr(pr)}, failed CI ({urls}). Read the "
                f"failing tests; if they are yours, fix them on your branch (merge "
                f"`origin/{self.cfg.project.base_branch}` first -- the release ran against it), "
                "verify, and push. Your PR is parked until its head moves.",
            )
        st["parked"] = st["parked"][-500:]
        self._save(st)
        self.sup.say(f"release {self.sup.pr(p.number)} red after re-runs -- closed; carried PRs handed back")

    # -- 2. a new batch ----------------------------------------------------------------------------
    def _paused(self) -> bool:
        # A short hold so a critical PR can catch up; a forgotten pause expires after an hour.
        age = self.sup.state.flag_age(self.t.batch_pause_file)
        return age is not None and age < 3600

    def _candidates(self) -> list[tuple[int, PullRequest]]:
        st = self._st()
        parked = set(st["parked"])
        try:
            order = [e.issue for e in queue.load(self.cfg.queue_file)]
        except queue.QueueError:
            order = []
        priority = [int(x) for x in self.sup.state.read_lines("priority") if x.isdigit()]
        held = {int(x) for x in self.sup.state.read_lines("hold") if x.isdigit()}

        def rank(issue: int) -> tuple[int, int]:
            if issue in priority:
                return (0, priority.index(issue))
            return (1, order.index(issue) if issue in order else len(order))

        out = []
        recs = sorted((r for r in self.sup.state.all() if r.status == READY and r.pr), key=lambda r: rank(r.issue))
        if self.gate.only is not None:
            recs = [r for r in recs if r.issue == self.gate.only]
        for rec in recs:
            if rec.issue in held:
                continue
            known, pr = self.sup.pr_for(rec)
            if not known or pr is None or pr.head_sha in parked:
                continue
            if self.sup.issue(rec.issue).state == CLOSED:
                continue
            try:
                if (
                    verdict(
                        self.sup.forge.checks(pr.head_sha), self.cfg.integrate.required_checks, self.cfg.integrate.ignore_checks
                    )
                    != GREEN
                ):
                    continue
            except ForgeError:
                continue
            out.append((rec.issue, pr))
        return out

    def _git(self, *args: str, check: bool = True) -> str:
        return git(self.tree, *args, check=check)

    def _ensure_tree(self) -> None:
        repo = self.cfg.repo_path
        if not (self.tree / ".git").exists():
            self.tree.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "prune", check=False)
            git(
                repo,
                "worktree",
                "add",
                "-q",
                "--detach",
                str(self.tree),
                f"{self.cfg.project.remote}/{self.cfg.project.base_branch}",
            )

    def _build(self) -> None:
        if self._paused():
            return
        cands = self._candidates()
        if not cands:
            return
        remote, base = self.cfg.project.remote, self.cfg.project.base_branch
        try:
            self._ensure_tree()
            self._git("fetch", "-q", remote)
            self._git("merge", "--abort", check=False)
            self._git("reset", "-q", "--hard")
            self._git("clean", "-qfd")
            branch = f"{self.t.branch_prefix}auto-{time.strftime('%m%d-%H%M%S')}"
            self._git("checkout", "-q", "-B", branch, f"{remote}/{base}")
        except GitError as e:
            self.sup.say(f"integrator: cannot prepare the release tree: {e}")
            return
        merged: list[tuple[int, int, str]] = []
        ident = ["-c", "user.name=nohalt", "-c", "user.email=nohalt@localhost"]
        for issue, pr in cands:
            ok = (
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.tree),
                        "-c",
                        "core.hooksPath=/dev/null",
                        *ident,
                        "merge",
                        "-q",
                        "--no-ff",
                        "--no-edit",
                        f"{remote}/{pr.head_ref}",
                    ],
                    capture_output=True,
                    text=True,
                ).returncode
                == 0
            )
            if not ok:
                conflicted = self._git("diff", "--name-only", "--diff-filter=U", check=False).split()
                mergers = effective(self.cfg.integrate.mergers, self.t.version_files)
                left = resolve(self.tree, conflicted, mergers) if conflicted else ["(merge failed)"]
                if not left:
                    ok = (
                        subprocess.run(
                            ["git", "-C", str(self.tree), "-c", "core.hooksPath=/dev/null", *ident, "commit", "-q", "--no-edit"],
                            capture_output=True,
                        ).returncode
                        == 0
                    )
                if not ok:
                    self._git("merge", "--abort", check=False)
                    files = ", ".join(left)
                    out = refresh(self.sup, self.sup.state.get(issue), pr)
                    if out.status == REFRESHED:
                        self.sup.say(
                            f"  ~ #{issue} ({self.sup.pr(pr.number)}) conflicted with {base} only in "
                            f"{', '.join(out.files)}: refreshed as {out.sha[:8]}, joins a later batch once green"
                        )
                        continue
                    if out.handled:
                        self.sup.state.log(f"  ~ #{issue} ({self.sup.pr(pr.number)}) left out, refresh deferred: {out.why}")
                        continue
                    record_conflicts(self.sup.state, issue, out.files or [f for f in left if not f.startswith("(")])
                    self.sup.say(f"  - #{issue} ({self.sup.pr(pr.number)}) conflicts in code: {files} -- handed back to rebase")
                    self.sup.hand_back(
                        issue,
                        f"{self.sup.pr(pr.number)} conflicts in code",
                        f"Your {self.sup.pr_long(pr.number)} conflicts with `{base}` in code (`{files}`), so it "
                        f"was left out of this release. Merge `origin/{base}` into your branch, keeping both "
                        "sides' behaviour, verify, and push. You will be picked up in the next batch once "
                        "your checks are green again.",
                    )
                    continue
            merged.append((issue, pr.number, pr.head_sha))
            self.sup.say(f"  + #{issue} ({self.sup.pr(pr.number)})")
        if not merged:
            self.sup.say("integrator: nothing merged cleanly")
            return
        version = self._stamp_version()
        issues = " ".join(f"#{i}" for i, _, _ in merged)
        try:
            self._git("add", "-A")
            msg = f"chore(release): {version} -- {issues}" if version else f"chore(release): {issues}"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.tree),
                    "-c",
                    "core.hooksPath=/dev/null",
                    *ident,
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    msg,
                ],
                check=True,
                capture_output=True,
            )
            self._git("push", "-q", "-f", "-u", remote, branch, check=True)
        except (GitError, subprocess.CalledProcessError) as e:
            self.sup.say(f"integrator: could not push {branch}: {e}")
            return
        closes = "\n".join(f"Closes #{i}" for i, _, _ in merged)
        prs = " ".join(self.sup.pr(p) for _, p, _ in merged)
        try:
            n = self.sup.forge.create_pull(
                f"release: {version or branch} -- {issues}",
                branch,
                base,
                f"Automated batch by nohalt. Pull requests merged in, each green: {prs}.\n\n"
                "Bookkeeping conflicts were resolved mechanically; code conflicts were left out and handed back. "
                "CI on this branch is the gate: it merges once green.\n\n" + closes,
            )
        except ForgeError as e:
            self.sup.say(f"integrator: could not open the release PR: {e}")
            return
        st = self._st()
        st["releases"][branch] = {"version": version, "carried": [list(m) for m in merged], "pr": n}
        self._save(st)
        self.sup.say(f"opened release {self.sup.pr(n)} ({version or branch}) carrying {issues}")

    # -- versions ------------------------------------------------------------------------------------
    def next_version(self) -> str:
        prefix = self.t.tag_prefix
        tags = self._git("tag", "-l", f"{prefix}[0-9]*", check=False).split()
        versions = sorted(
            (t.removeprefix(prefix) for t in tags if re.fullmatch(r"\d+\.\d+\.\d+", t.removeprefix(prefix))), key=_vkey
        )
        current = versions[-1] if versions else self._file_version()
        if not current:
            return "0.1.0"
        a, b, c = _vkey(current)
        return f"{a}.{b}.{c + 1}"

    def _file_version(self) -> str:
        for vf in self.t.version_files:
            p = self.tree / vf["path"]
            if p.exists():
                m = re.search(vf["pattern"], p.read_text(), re.M)
                if m:
                    return m.group(1)
        return ""

    def _stamp_version(self) -> str:
        if not self.t.version_files:
            return ""
        self._git("fetch", "-q", self.cfg.project.remote, "--tags", check=False)
        v = self.next_version()
        for vf in self.t.version_files:
            p = self.tree / vf["path"]
            if not p.exists():
                continue
            text = p.read_text()
            pat = re.compile(vf["pattern"], re.M)
            m = pat.search(text)
            if m:
                text = text[: m.start(1)] + v + text[m.end(1) :]
                p.write_text(text)
        if self.t.after_version:
            subprocess.run(["bash", "-c", self.t.after_version], cwd=self.tree, capture_output=True, timeout=1800)
        return f"{self.t.tag_prefix}{v}"

    def _tag(self, version: str) -> None:
        remote, base = self.cfg.project.remote, self.cfg.project.base_branch
        try:
            self._git("fetch", "-q", remote, base)
            self._git("tag", version, "FETCH_HEAD")
            self._git("push", "-q", remote, version)
            self.sup.say(f"tagged {version}")
        except GitError as e:
            self.sup.say(f"could not tag {version}: {e}")
