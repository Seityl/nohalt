"""The review gate: green is not enough.

An issue selected by ``[review]`` does not go from a green PR to ``ready``.
It goes to ``in_review``, and a second agent -- the reviewer, on a model of
its own -- checks the PR against the issue in a detached checkout of the PR
head. Its verdict is the last line of its review:

* ``NOHALT-REVIEW: approve``  -> ``ready``; the approval covers that head SHA only;
* ``NOHALT-REVIEW: changes``  -> handed back to the author with the review
  (like a red CI), up to ``max_rounds`` times, then blocked for a human.

Rules that hold throughout:

* A reviewer never changes the PR: its guard shims run read-only, and an
  approval counts only if the PR head is still the commit it reviewed.
* No verdict is never an approval. A reviewer that dies, hangs, runs out of
  time or ends without a verdict is retried, then goes to a human; an endpoint
  failure is retried with backoff and costs nothing.
* A reviewer runs in its own session, like an agent: a restarted supervisor
  adopts it from the issue record.
"""

from __future__ import annotations

import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import agents
from . import prompt as prompt_mod
from .config import Config
from .forge import ForgeError, Issue, PullRequest
from .state import BLOCKED, DONE, IN_REVIEW, READY, RUNNING, IssueRecord
from .util import kill_tree, mem_available_mb, now, pid_alive, tail
from .worktree import GitError

if TYPE_CHECKING:
    from .supervisor import Supervisor

APPROVE, CHANGES = "approve", "changes"

# The verdict line: alone on its line (markdown emphasis allowed), so a sentence
# that merely quotes the format -- the prompt does -- is never read as one.
VERDICT = re.compile(r"^[ \t>*_`#-]*NOHALT-REVIEW:\s*(approve|changes)[ \t*_`.]*$", re.I | re.M)
LAUNCH_MARK = "\n--- nohalt review "


def parse_verdict(text: str) -> tuple[str, str]:
    """(verdict, review text) from a review; ("", "") when it has no verdict line."""
    found = list(VERDICT.finditer(text or ""))
    if not found:
        return "", ""
    return found[-1].group(1).lower(), text.strip()


def reviewer_models(cfg: Config, author: str = "") -> list[str]:
    """Reviewer models to try, in order, for a PR written on ``author``.

    ``[review] models`` maps the author's model (the longest matching prefix) to its reviewer, then
    come ``model`` (default: ``[models] default``) and ``fallbacks``. With ``distinct``, the author's
    own model is never among them: an empty list means nothing may review this author's work."""
    r = cfg.review
    mapped = ""
    if author:
        prefix = max((p for p in r.models if author.startswith(p)), key=len, default=None)
        mapped = r.models[prefix] if prefix is not None else ""
    out: list[str] = []
    for m in (mapped, r.model or cfg.models.default, *r.fallbacks):
        if m and m not in out and not (r.distinct and author and m == author):
            out.append(m)
    return out


class Reviews:
    def __init__(self, sup: Supervisor):
        self.sup = sup
        self.cfg = sup.cfg
        self._procs: dict[int, subprocess.Popen[bytes]] = {}

    # -- selection -----------------------------------------------------------------------------------
    def needed(self, issue: Issue) -> bool:
        r = self.cfg.review
        if not r.enabled:
            return False
        if r.all:
            return True
        if r.labels and issue.has_label(*r.labels):
            return True
        return bool(r.title and re.search(r.title, issue.title or "", re.I))

    def models(self, author: str = "") -> list[str]:
        return reviewer_models(self.cfg, author)

    # -- the gate, called by the supervisor when a PR is green --------------------------------------------
    def gate(self, rec: IssueRecord, pr: PullRequest) -> None:
        """A green PR becomes ``ready`` -- or ``in_review`` when its issue needs a review this head has not passed."""
        sup = self.sup
        if self.needed(sup.issue(rec.issue)) and rec.review_approved != pr.head_sha:
            if rec.status == IN_REVIEW:
                return
            if rec.review_verdict == CHANGES and rec.review_sha == pr.head_sha:
                # Handed back, and nothing was pushed: the review still stands.
                return self._changes(rec, pr.number, rec.review_text, repeat=True)
            rec.status = IN_REVIEW
            rec.review_not_before = 0
            rec.event(f"{sup.pr(pr.number)} green on {pr.head_sha[:8]}; review needed")
            sup.state.put(rec)
            again = " again (it changed since the last review)" if rec.review_sha else ""
            sup.say(f"#{rec.issue}: {sup.pr(pr.number)} green -- in review{again}")
            return
        if rec.status == READY:
            return
        reviewed = rec.review_approved == pr.head_sha and bool(rec.review_approved)
        rec.status = READY
        rec.event(f"{sup.pr(pr.number)} green on {pr.head_sha[:8]}" + (" and approved" if reviewed else ""))
        sup.state.put(rec)
        sup.say(f"#{rec.issue}: {sup.pr(pr.number)} green{' and approved' if reviewed else ''} -- ready to integrate")

    # -- each tick ---------------------------------------------------------------------------------------
    def tick(self) -> None:
        for rec in self.sup.state.all():
            if rec.review_pid and (rec.status != IN_REVIEW or rec.pr_sha != rec.review_sha):
                # The review is moot: the PR moved, went red, or the issue closed. Its verdict would be
                # about a commit that is no longer the PR.
                if self._alive(rec):
                    self._stop(rec, f"review of {rec.review_sha[:8]} no longer applies")
                rec.review_pid = 0
                self.sup.state.put(rec)
            if rec.status in (DONE, BLOCKED) and self.sup.trees.review_path(rec.issue).exists():
                self._cleanup(rec.issue)
        waiting = [r for r in self.sup.state.all() if r.status == IN_REVIEW]
        for rec in waiting:
            if rec.review_pid:
                self._reap(rec)
        stopping = self.sup.state.has("STOP") or self.sup.state.has("PAUSE")
        for rec in self.sup.state.all():
            if rec.status != IN_REVIEW or rec.review_pid or stopping or rec.review_not_before > now():
                continue
            self._start(rec)

    def in_use(self) -> dict[str, int]:
        """Running reviewers per model pool: they take slots like agents."""
        out: dict[str, int] = {}
        for rec in self.sup.state.all():
            if rec.status == IN_REVIEW and rec.review_pid:
                p = self.sup._pool_of(rec.review_model)
                out[p] = out.get(p, 0) + 1
        return out

    def stop_all(self) -> list[int]:
        stopped = []
        for rec in self.sup.state.all():
            if rec.review_pid and self._alive(rec):
                self._stop(rec, "stop requested", reason="stopped")
                stopped.append(rec.issue)
        return stopped

    # -- launching ------------------------------------------------------------------------------------------
    def _slots_free(self, model: str) -> bool:
        sup = self.sup
        in_use = self.in_use()
        for r in sup.state.all():
            if r.status == RUNNING:
                p = sup._pool_of(r.model)
                in_use[p] = in_use.get(p, 0) + 1
        return sup._has_slot(model, in_use)

    def _choose(self, models: list[str]) -> str | None:
        for m in models:
            if self._slots_free(m) and self.sup.router.prober.answers(m):
                return m
        return None

    def _start(self, rec: IssueRecord) -> None:
        sup = self.sup
        free = mem_available_mb()
        if free is not None and free < self.cfg.agents.min_free_mb:
            sup.state.log(f"#{rec.issue} review waiting: {free} MB available, need {self.cfg.agents.min_free_mb}")
            return
        models = self.models(rec.model)
        if not models:
            return sup._block(
                rec,
                f"review: no reviewer model differs from the author's ({rec.model}); add one to [review] models or "
                "fallbacks, or set [review] distinct = false",
            )
        model = self._choose(models)
        if not model:
            sup.state.log(f"#{rec.issue} review waiting: no reviewer model has a free slot and answers")
            return
        try:
            self.launch(rec, model)
        except (GitError, prompt_mod.PromptError, OSError, ValueError) as e:
            rec.review_not_before = now() + 600
            rec.event(f"review launch failed: {e}")
            sup.state.put(rec)
            sup.say(f"#{rec.issue}: review launch failed: {e}")

    def review_file(self, issue: int) -> Path:
        return self.sup.state.root / "reviews" / f"{issue}.md"

    def render_prompt(self, rec: IssueRecord, issue: Issue, tree: Path) -> str:
        sup = self.sup
        lane = prompt_mod.find_lane(self.cfg, rec.lane)
        text = prompt_mod.review_template(self.cfg, sup.stack.prompt_addendum, lane)
        slug = self.cfg.project.repo
        if self.cfg.project.forge == "gitlab":
            pr_view = f"glab mr view {rec.pr} --repo {slug} --comments"
        else:
            pr_view = f"gh pr view {rec.pr} --repo {slug} --comments"
        extra = {
            "PR_REF": sup.pr_long(rec.pr),
            "PR_NUMBER": str(rec.pr),
            "PR_VIEW": pr_view,
            "HEAD": rec.pr_sha,
            "REVIEW_FILE": str(self.review_file(rec.issue)),
        }
        values = prompt_mod.values(
            self.cfg, issue=issue.number, title=issue.title, branch=rec.branch, worktree=tree, extra=extra, lane=lane
        )
        body = prompt_mod.render(text, values)
        try:
            body += f"\n## The issue, as it read when you were launched\n\n{sup.forge.issue_text(issue.number)}\n"
        except ForgeError as e:
            sup.state.log(f"#{issue.number}: could not inline the issue text for the reviewer ({e})")
        if rec.review_verdict and rec.review_sha and rec.review_text:
            what = "approved" if rec.review_verdict == APPROVE else "asked for changes on"
            body += (
                f"\n## The previous review\n\nA previous review {what} `{rec.review_sha[:12]}`; the PR has changed since. "
                "Review the **whole** PR again, not only the new commits, and check that every item below was addressed.\n\n"
                f"{rec.review_text}\n"
            )
        return body

    def launch(self, rec: IssueRecord, model: str) -> None:
        sup = self.sup
        issue = sup.issue(rec.issue)
        name, mid = agents.split_model(model)
        agent = agents.get(name, self.cfg.agents.options.get(name))
        sup.settle_lane(rec, issue)  # an adopted PR's issue never launched: choose its lane now
        tree = sup.trees.prepare_review(rec.issue, rec.pr_sha, rec.branch)
        out_file = self.review_file(rec.issue)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.unlink(missing_ok=True)
        data_dir = sup.state.agent_data / f"review-{rec.issue}"
        shutil.rmtree(data_dir, ignore_errors=True)  # every review is a fresh session
        data_dir.mkdir(parents=True, exist_ok=True)
        for f in self.cfg.agents.copy_files:
            src = Path(f).expanduser()
            dst = data_dir / ("opencode" if name == "opencode" else "") / src.name
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        text = self.render_prompt(rec, issue, tree)
        spec = agent.launch(
            mid, text, title=f"{self.cfg.agents.title_prefix} review #{rec.issue}", resume=False, data_dir=data_dir
        )
        env = sup.agent_env(rec.issue, spec)
        # Read-only guards: no push, no forge writes. And the approval binds to the reviewed SHA anyway.
        env.update({"NOHALT_READONLY": "1", "NOHALT_ROLE": "review", "NOHALT_REVIEW_FILE": str(out_file)})

        log = sup.state.logs_dir / f"review-{rec.issue}.log"
        sup._rotate(log)
        with open(log, "ab") as out:
            out.write(f"{LAUNCH_MARK}{time.strftime('%F %T')} model={model} head={rec.pr_sha} ---\n".encode())
            out.flush()
            proc = subprocess.Popen(
                spec.argv,
                cwd=tree,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        self._procs[rec.issue] = proc
        pids = sup.state.load("pids.json", {})
        pids[f"review-{rec.issue}"] = _pid_start(proc.pid)
        sup.state.save("pids.json", pids)
        rec.review_pid, rec.review_model, rec.review_started = proc.pid, model, now()
        rec.review_sha, rec.review_exit = rec.pr_sha, ""
        rec.event(f"reviewer launched on {model} for {rec.pr_sha[:8]}" + (f" (author {rec.model})" if rec.model else ""))
        sup.state.put(rec)
        sup.say(f"#{rec.issue}: reviewer launched on {model} for {sup.pr(rec.pr)} at {rec.pr_sha[:8]}")

    # -- reaping -------------------------------------------------------------------------------------------
    def _alive(self, rec: IssueRecord) -> bool:
        proc = self._procs.get(rec.issue)
        if proc is not None and proc.poll() is not None:
            self._procs.pop(rec.issue, None)
            return False
        if not pid_alive(rec.review_pid):
            return False
        start = self.sup.state.load("pids.json", {}).get(f"review-{rec.issue}")
        return not start or start == _pid_start(rec.review_pid)

    def _stop(self, rec: IssueRecord, text: str, reason: str = "") -> None:
        if reason:
            rec.review_exit = reason
        rec.event(f"reviewer stopped: {text}")
        self.sup.state.put(rec)
        self.sup.say(f"#{rec.issue}: stopping the reviewer -- {text}")
        kill_tree(rec.review_pid, signal.SIGTERM)
        for _ in range(20):
            if not pid_alive(rec.review_pid):
                break
            time.sleep(0.5)
        else:
            kill_tree(rec.review_pid, signal.SIGKILL)
        proc = self._procs.pop(rec.issue, None)
        if proc:
            proc.poll()

    def _log(self, rec: IssueRecord) -> Path:
        return self.sup.state.logs_dir / f"review-{rec.issue}.log"

    def _reap(self, rec: IssueRecord) -> None:
        if self._alive(rec):
            elapsed = now() - rec.review_started
            log = self._log(rec)
            idle = now() - log.stat().st_mtime if log.exists() else 0
            if elapsed > self.cfg.review.timeout:
                self._stop(rec, f"ran out of time ({self.cfg.review.timeout // 60} min)", reason="timeout")
            elif idle > self.cfg.agents.hung_after and not self.sup._busy(rec.review_pid):
                self._stop(rec, f"no output for {int(idle // 60)} min and no command running", reason="hung")
            else:
                return
        self._judge(rec)

    def _judge(self, rec: IssueRecord) -> None:
        """The reviewer is gone. Read its verdict; anything else is a retry, never an approval."""
        sup = self.sup
        a = self.cfg.agents
        log_tail = tail(self._log(rec), 20000).rsplit(LAUNCH_MARK, 1)[-1]
        try:
            written = self.review_file(rec.issue).read_text()
        except OSError:
            written = ""
        verdict, text = parse_verdict(written)
        if not verdict:
            verdict, text = parse_verdict("\n".join(ln for ln in log_tail.splitlines()[1:] if not ln.startswith("[agent]")))
            text = text[-8000:]
        exit_reason = rec.review_exit
        agent = sup._agent(rec.review_model)
        ran_for = now() - rec.review_started if rec.review_started else 0

        if verdict and exit_reason != "stopped":
            # Only a verdict on the PR as it still is: a head that moved (a push by anyone -- the
            # reviewer included) voids it.
            try:
                head = sup.forge.pull(rec.pr).head_sha
            except ForgeError:
                return  # asked again next tick; the verdict file is still there
            rec.review_pid = 0
            if head != rec.review_sha:
                rec.review_exit = ""
                rec.event(f"review of {rec.review_sha[:8]} discarded: the PR head is now {head[:8]}")
                sup.state.put(rec)
                sup.say(f"#{rec.issue}: review discarded -- {sup.pr(rec.pr)} moved to {head[:8]} during the review")
                return
            rec.review_failures = rec.review_retries = 0
            if verdict == APPROVE:
                return self._approve(rec, text)
            return self._changes(rec, rec.pr, text, post=True)

        rec.review_pid = 0
        rec.review_exit = ""
        if exit_reason == "stopped":
            return self._retry(rec, "the reviewer was stopped", 0)
        if agent and agent.usage_limited(log_tail):
            sup.router.prober.mark_limited(rec.review_model)
            return self._retry(rec, f"the reviewer's plan hit its usage limit ({rec.review_model})", 900)
        if not exit_reason and agent and (agent.provider_failed(log_tail) or (ran_for < 300 and agent.setup_failed(log_tail))):
            if agent.setup_failed(log_tail):
                sup.router.prober.mark_limited(rec.review_model)
            rec.review_retries += 1
            if rec.review_retries > a.provider_retries:
                return sup._block(
                    rec,
                    f"review: the reviewer model {rec.review_model} failed {rec.review_retries} times "
                    "(endpoint or setup); this was not the PR's fault",
                )
            return self._retry(
                rec,
                f"reviewer endpoint/setup error, not counted ({rec.review_retries}/{a.provider_retries})",
                a.provider_backoff * rec.review_retries,
            )
        rec.review_failures += 1
        why = {"timeout": "ran out of time", "hung": "hung"}.get(exit_reason, "ended without a verdict")
        if rec.review_failures >= self.cfg.review.attempts:
            return sup._block(rec, f"review: the reviewer {why} {rec.review_failures} time(s); no verdict, so no approval")
        self._retry(rec, f"the reviewer {why} ({rec.review_failures}/{self.cfg.review.attempts}); reviewing again", 0)

    def _retry(self, rec: IssueRecord, why: str, delay: float) -> None:
        rec.review_pid = 0
        rec.review_not_before = now() + delay
        rec.event(why)
        self.sup.state.put(rec)
        self.sup.say(f"#{rec.issue}: {why}" + (f"; again in {int(delay // 60)} min" if delay >= 60 else ""))

    # -- verdicts ------------------------------------------------------------------------------------------
    def _post(self, rec: IssueRecord, verdict: str, text: str) -> None:
        sup = self.sup
        head = f"**nohalt review: {verdict}** -- reviewer `{rec.review_model}` on `{rec.review_sha[:12]}`"
        if rec.model:
            head += f", author `{rec.model}`"
        if verdict == CHANGES:
            head += f" (round {rec.review_rounds}/{self.cfg.review.max_rounds})"
        body = f"{head}\n\n{text}"
        formal = getattr(sup.forge, "review_pull", None)
        if formal is not None:
            try:
                formal(rec.pr, body, approve=verdict == APPROVE)
                return
            except ForgeError as e:
                sup.state.log(f"#{rec.issue}: no formal review on {sup.pr(rec.pr)} ({e}); commenting instead")
        try:
            sup.forge.comment_pull(rec.pr, body)
        except ForgeError as e:
            sup.state.log(f"#{rec.issue}: could not post the review on {sup.pr(rec.pr)}: {e}")

    def _approve(self, rec: IssueRecord, text: str) -> None:
        sup = self.sup
        rec.review_verdict, rec.review_text, rec.review_approved = APPROVE, text, rec.review_sha
        rec.status = READY
        rec.event(f"review approved {rec.review_sha[:8]} ({rec.review_model})")
        sup.state.put(rec)
        sup.say(f"#{rec.issue}: {sup.pr(rec.pr)} approved by the reviewer -- ready to integrate")
        self._post(rec, APPROVE, text)
        self._cleanup(rec.issue)

    def _changes(self, rec: IssueRecord, pr: int, text: str, *, post: bool = False, repeat: bool = False) -> None:
        sup = self.sup
        rec.review_verdict, rec.review_text = CHANGES, text
        rec.review_rounds += 1
        if post:
            self._post(rec, CHANGES, text)
        self._cleanup(rec.issue)
        r = self.cfg.review
        if rec.review_rounds > r.max_rounds:
            return sup._block(rec, f"review: changes still requested after {r.max_rounds} round(s)")
        lead = (
            "You stopped without pushing anything since the review below; it still stands."
            if repeat
            else f"A reviewer checked your {sup.pr_long(pr)} against the issue and asked for changes."
        )
        note = (
            f"{lead} Address every numbered item, run the tests you touched, push, and stop. If you are sure an item "
            "is wrong, say why with evidence in the PR body instead of changing it -- the next review reads it. "
            f"The whole PR is reviewed again after your push.\n\n{text}"
        )
        sup._requeue(
            rec,
            f"{sup.pr(pr)}: review asked for changes; handed back ({rec.review_rounds}/{r.max_rounds})",
            resume=True,
            note=note,
        )

    def _cleanup(self, issue: int) -> None:
        try:
            self.sup.trees.remove_review(issue)
        except GitError as e:
            self.sup.state.log(f"#{issue}: review tree not removed: {e}")
        shutil.rmtree(self.sup.state.agent_data / f"review-{issue}", ignore_errors=True)


def _pid_start(pid: int) -> str:
    from .supervisor import _pid_start as start

    return start(pid)
