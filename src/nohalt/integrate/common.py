"""Helpers both integrators use."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from .. import queue
from ..forge import CLOSED, OPEN, ForgeError, verdict
from ..forge.base import GREEN, RED
from ..util import now

if TYPE_CHECKING:
    from ..supervisor import Supervisor


def run_deploy(sup: Supervisor, what: str) -> bool:
    cmd = sup.cfg.integrate.deploy
    if not cmd:
        return True
    log = sup.state.logs_dir / "deploy.log"
    with open(log, "a") as out:
        out.write(f"\n--- deploy {what} ---\n")
        out.flush()
        rc = subprocess.call(["bash", "-c", cmd], cwd=sup.cfg.repo_path, stdout=out, stderr=subprocess.STDOUT)
    sup.say(f"deploy of {what}: " + ("done" if rc == 0 else f"FAILED (exit {rc}; see {log})"))
    if rc != 0:
        sup.state.set_flag("DEPLOY_BLOCKED", f"deploy of {what} exited {rc}\n")
    return rc == 0


def open_fix_forward(sup: Supervisor, after: str, sha: str, verdict: str) -> int | None:
    """A red pipeline becomes the next thing worked on, ahead of everything."""
    base = sup.cfg.project.base_branch
    try:
        n = sup.forge.create_issue(
            f"Pipeline red after {after} ({sha[:7]}) -- restore it before anything else merges",
            f"nohalt merged {after} as `{sha}` and the pipeline on `{base}` did not complete: **{verdict}**.\n\n"
            "Nothing else merges on top of a pipeline that cannot deploy. Find the cause, fix it forward in a "
            f"pull request, and confirm the next run on `{base}` is green.\n\n"
            "## Done when\n- [ ] The pipeline run for the fixing merge is green",
            labels=["bug"],
        )
    except ForgeError as e:
        sup.say(f"could not open a fix-forward issue: {e}")
        return None
    queue.append(sup.cfg.queue_file, n, front=True)
    sup.say(f"opened #{n} to restore the pipeline; it goes to the front of the queue")
    return n


def is_network_failure(sup: Supervisor, run_ids: list[int]) -> bool:
    """A red that is only the network (DNS, resets) is not the code's fault."""
    pats = sup.cfg.integrate.network_errors
    for rid in run_ids:
        try:
            log = sup.forge.failed_log(rid)
        except ForgeError:
            continue
        if any(p in log for p in pats):
            return True
    return False


def cancel_superseded(sup: Supervisor) -> int:
    """Cancel queued/running CI for commits no open PR or the base branch points at.

    Agents push several times while a run is still queued; runners otherwise
    spend the night testing commits nobody will merge. Runs on the base branch
    are never cancelled: they are the post-merge check.
    """
    pulls = sup.pulls()
    if pulls is None:
        return 0
    base = sup.cfg.project.base_branch
    heads = {p.head_sha for p in pulls}
    n = 0
    for status in ("queued", "in_progress"):
        try:
            runs = sup.forge.runs(status=status)
        except ForgeError:
            return n
        for r in runs:
            if r.branch == base or r.head_sha in heads:
                continue
            try:
                sup.forge.cancel_run(r.id)
                n += 1
            except ForgeError:
                pass
    if n:
        sup.state.log(f"cancelled {n} superseded CI run(s)")
    return n


class PipelineGate:
    """After a merge, nothing else merges until the base branch's pipeline is green.

    Shared by both integrators; its state (``integrate.json``) spans ticks and
    restarts. A red or timed-out pipeline opens a fix-forward issue at the
    front of the queue, and merging resumes when that issue closes. A failed
    deploy (``DEPLOY_BLOCKED``) also stops merging until a human clears it.
    """

    def __init__(self, sup: Supervisor):
        self.sup = sup
        self.cfg = sup.cfg

    def _st(self) -> dict[str, Any]:
        return self.sup.state.load("integrate.json", {})

    def _save(self, st: dict[str, Any]) -> None:
        self.sup.state.save("integrate.json", st)

    def open(self) -> bool:
        """May the integrator merge now?"""
        if self.sup.state.has("DEPLOY_BLOCKED"):
            if not self.sup.state.has("deploy-blocked-said"):
                self.sup.state.set_flag("deploy-blocked-said")
                self.sup.say("merging stopped: a deploy failed (clear <state>/DEPLOY_BLOCKED once it is fixed)")
            return False
        self.sup.state.clear_flag("deploy-blocked-said")
        st = self._st()
        if st.get("fix_issue"):
            if self.sup.issue(st["fix_issue"], fresh=True).state != CLOSED:
                return False
            self.sup.say(f"pipeline fix #{st['fix_issue']} closed; merging resumes")
            st.pop("fix_issue")
            self._save(st)
        if st.get("watch"):
            return self._check(st)
        return True

    def merged(self, what: str, deploy_after: bool) -> None:
        """Record a merge: watch the base branch's new head."""
        try:
            sha = self.sup.forge.branch_sha(self.cfg.project.base_branch)
        except ForgeError:
            sha = ""
        if not sha:
            if deploy_after:
                run_deploy(self.sup, what)
            return
        st = self._st()
        st["watch"] = {"sha": sha, "what": what, "since": now(), "deploy": deploy_after}
        self._save(st)

    def _pipeline(self, sha: str) -> str:
        """'success', 'failure', 'ci-failure', 'pending' or 'none' (nothing runs on this commit)."""
        ic = self.cfg.integrate
        f = self.sup.forge
        if not f.runs(head_sha=sha) and not f.checks(sha):
            return "none"
        if ic.cd_workflow:
            runs = f.runs(head_sha=sha, workflow=ic.cd_workflow)
            done = [r for r in runs if r.status == "completed"]
            if done:
                return "success" if done[0].conclusion == "success" else "failure"
            # CD is skipped when CI on the base fails: catch that instead of waiting it out.
            if not runs and ic.ci_workflow:
                ci = [r for r in f.runs(head_sha=sha, workflow=ic.ci_workflow) if r.status == "completed"]
                if ci and ci[0].conclusion not in ("success", "skipped", "neutral"):
                    return "ci-failure"
            return "pending"
        if ic.ci_workflow:
            done = [r for r in f.runs(head_sha=sha, workflow=ic.ci_workflow) if r.status == "completed"]
            if done:
                return "success" if done[0].conclusion == "success" else "failure"
            return "pending"
        v = verdict(f.checks(sha), ic.required_checks)
        return {GREEN: "success", RED: "failure"}.get(v, "pending")

    def _check(self, st: dict[str, Any]) -> bool:
        w = st["watch"]
        try:
            result = self._pipeline(w["sha"])
        except ForgeError:
            return False
        if result == "none":
            # Many integration branches run no pipeline at all after a merge. Give
            # one time to appear; if none does, there is nothing to watch -- not a red.
            if now() - w["since"] < self.cfg.integrate.base_pipeline_grace:
                return False
            self.sup.say(f"no pipeline ran on {w['sha'][:7]} after {w['what']} within the grace; nothing to watch, carrying on")
            if w.get("deploy") and not self.cfg.integrate.cd_workflow:
                run_deploy(self.sup, f"{w['sha'][:7]} ({w['what']})")
            st.pop("watch")
            self._save(st)
            return not self.sup.state.has("DEPLOY_BLOCKED")
        elif result == "pending":
            if now() - w["since"] < self.cfg.integrate.cd_timeout:
                return False
            result = "timeout"
        if result == "success":
            self.sup.say(f"pipeline green on {w['sha'][:7]} after {w['what']}")
            if w.get("deploy") and not self.cfg.integrate.cd_workflow:
                run_deploy(self.sup, f"{w['sha'][:7]} ({w['what']})")
            st.pop("watch")
            self._save(st)
            return not self.sup.state.has("DEPLOY_BLOCKED")
        self.sup.say(f"pipeline NOT green after {w['what']}: {result}")
        st.pop("watch")
        if self.cfg.integrate.fix_forward:
            n = open_fix_forward(self.sup, w["what"], w["sha"], result)
            if n:
                st["fix_issue"] = n
        self._save(st)
        return False


def close_merged(sup: Supervisor, issues: list[int], via: str) -> None:
    """A merge finishes an issue. Close it if the forge did not.

    Forges honour closing keywords only on merges into the default branch, so
    with any other base -- and when a body forgot the keyword -- an issue would
    stay open and be worked again.
    """
    for n in issues:
        if sup.issue(n, fresh=True).state != OPEN:
            continue
        try:
            sup.forge.close_issue(n)
            sup.forge.comment(n, f"**nohalt:** closed by the merge of {via}.")
            sup.say(f"#{n}: closed after the merge of {via}")
        except ForgeError as e:
            sup.say(f"#{n}: merged by {via} but could not be closed yet: {e}")
