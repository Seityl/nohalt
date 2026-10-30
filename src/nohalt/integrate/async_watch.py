"""Async workflows: watched on the base branch, never gating (DEP-11, DEP-12).

A suite that takes two hours cannot gate every release, and a suite nobody
watches goes red unseen. ``[integrate] async_workflows`` names workflows that
nohalt follows on the base branch without ever waiting for them:

* the newest completed run of each is read every tick;
* red, with no open issue for that workflow: one issue is opened at the front
  of the queue (``async_label``), with the run, its failing jobs and tests;
* red again while that issue is open: a comment on it, with what changed --
  never a second issue (one root cause once filed six);
* green: a comment, and the issue is closed. An unmerged PR of its own is
  left open, and the comment says so.

An agent working such an issue may find the cause already fixed on the base
by a newer commit. It says so with ``NOHALT-RESULT: no-change -- <reason>``,
and the issue waits for the next green run (``awaiting_green``) instead of
being blocked. A red run with failures the agent has not seen sends it back.

State spans ticks and restarts in ``async.json``, one entry per workflow.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .. import queue
from ..forge import OPEN, UNKNOWN, ForgeError, Run
from ..state import AWAITING_GREEN, RUNNING, IssueRecord

if TYPE_CHECKING:
    from ..supervisor import Supervisor

# Runs that are not the base branch's own, even when their branch name says so
# (a pull request from a fork's `main`, forge-managed jobs).
_NOT_BASE = {"pull_request", "pull_request_target", "dynamic"}
_NO_VERDICT = {"cancelled", "skipped", "neutral", "stale", ""}
_OK = {"success", "skipped", "neutral", "cancelled"}

# Failing tests in a failed log: pytest's short summary, unittest's headers, and Frappe's runner's (a
# coloured ` ERROR ` / ` FAIL ` badge, no colon). Colour codes are stripped first.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PYTEST = re.compile(r"(?:^|\s)(?:FAILED|ERROR)\s+(\S+::\S+)", re.M)
_UNITTEST = re.compile(r"(?:^|\s)(?:FAIL|ERROR):?\s+(\w+) \(([\w.]+\.\w+)\)", re.M)
# What the runner says it found: unittest's `FAILED (failures=N, errors=M, ...)`, pytest's `== N failed, ... in 5s ==`.
_UNITTEST_SUMMARY = re.compile(r"\bFAILED \(([^)]*)\)")
_PYTEST_SUMMARY = re.compile(r"=+ ([^=]*\bin [\d.]+s\b[^=]*) =+")
_COUNT = re.compile(r"\b(?:failures|errors)=(\d+)|\b(\d+) (?:failed|errors?)\b")
# A GitHub `--log-failed` line: `<job>\t<step>\t<timestamp> <text>`. Logs without it are one job.
_JOB = re.compile(r"^([^\t\n]+)\t[^\t\n]*\t\d{4}-\d\d-\d\dT")
LOG_BYTES = 2_000_000  # read at most this much of a failed log
MAX_LISTED = 30  # failing tests listed in an issue or comment
EXCERPT_LINES = 40


def _tests(log: str) -> set[str]:
    found = {m.group(1).rstrip(",") for m in _PYTEST.finditer(log)}
    found |= {
        f"{m.group(2)}.{m.group(1)}" if not m.group(2).endswith(m.group(1)) else m.group(2) for m in _UNITTEST.finditer(log)
    }
    return found


def _reported(log: str) -> int:
    """Failures plus errors the runner's own summary lines report (0 when it printed none)."""
    n = 0
    for summary in [*_UNITTEST_SUMMARY.findall(log), *_PYTEST_SUMMARY.findall(log)]:
        n += sum(int(a or b) for a, b in _COUNT.findall(summary))
    return n


def read_failures(log: str) -> tuple[list[str], int, int]:
    """(failing tests, how many of them were read, how many the runners' summaries report).

    Counted per job, so a test failing in each of a matrix's jobs is one test but one failure per
    job. Fewer read than reported means the log has failures in a format nohalt does not recognise.
    """
    log = _ANSI.sub("", log[-LOG_BYTES:])
    jobs: dict[str, list[str]] = {}
    for line in log.splitlines():
        m = _JOB.match(line)
        jobs.setdefault(m.group(1) if m else "", []).append(line)
    found: set[str] = set()
    read = reported = 0
    for lines in jobs.values():
        text = "\n".join(lines)
        tests, said = _tests(text), _reported(text)
        found |= tests
        read += len(tests)
        reported += max(said, len(tests))
    return sorted(found)[:200], read, reported


def failing_tests(log: str) -> list[str]:
    return read_failures(log)[0]


def _bullets(items: list[str], limit: int = MAX_LISTED) -> str:
    out = "\n".join(f"- `{t}`" for t in items[:limit])
    if len(items) > limit:
        out += f"\n- ... and {len(items) - limit} more"
    return out


class AsyncWatch:
    def __init__(self, sup: Supervisor):
        self.sup = sup
        self.cfg = sup.cfg

    def _st(self) -> dict[str, Any]:
        return self.sup.state.load("async.json", {})

    def _save(self, st: dict[str, Any]) -> None:
        self.sup.state.save("async.json", st)

    def owns(self, issue: int) -> str:
        """The async workflow whose red run opened this issue, or ''."""
        for wf, w in self._st().items():
            if w.get("issue") == issue:
                return wf
        return ""

    def tick(self) -> None:
        for wf in self.cfg.integrate.async_workflows:
            try:
                self._one(wf)
            except ForgeError as e:
                self.sup.state.log(f"async workflow {wf}: forge unreachable ({e}); trying again next tick")

    def _one(self, wf: str) -> None:
        base = self.cfg.project.base_branch
        runs = [
            r
            for r in self.sup.forge.runs(branch=base, workflow=wf)
            if r.status == "completed" and r.event not in _NOT_BASE and r.branch in (base, "")
        ]
        if not runs:
            return
        run = max(runs, key=lambda r: r.id)  # ids grow on both forges; list order differs
        st = self._st()
        w = st.setdefault(wf, {})
        key = f"{run.id}:{run.conclusion}"  # a re-run keeps its id (GitHub) but may change its verdict
        if w.get("last") == key:
            return
        if run.conclusion in _NO_VERDICT:
            done = True  # no verdict: the last red or green still stands
        elif run.conclusion == "success":
            done = self._green(wf, w, run)
        else:
            done = self._red(wf, w, run)
        if done:
            w["last"] = key
            w["sha"], w["url"] = run.head_sha, run.url
            self._save(st)

    # -- green ------------------------------------------------------------------------------
    def _green(self, wf: str, w: dict[str, Any], run: Run) -> bool:
        base = self.cfg.project.base_branch
        n = int(w.get("issue") or 0)
        if not n:
            if w.get("failures"):
                self.sup.say(f"async workflow {wf} green on {base} at {run.head_sha[:7]}")
            w["failures"] = []
            return True
        issue = self.sup.issue(n, fresh=True)
        if issue.state == UNKNOWN:
            return False
        if issue.state == OPEN:
            rec = self.sup.state.get(n)
            note = ""
            known, pr = self.sup.pr_for(rec)
            if known and pr is not None:
                note = (
                    f"\n\n{self.sup.pr(pr.number)} is still open and not merged. nohalt leaves it open: merge it if it is "
                    "still worth having, or close it."
                )
            self.sup.forge.comment(
                n,
                f"**nohalt:** `{wf}` is green on `{base}` at `{run.head_sha}` ([run]({run.url})). "
                f"Closing: the workflow this issue was about passes again.{note}",
            )
            self.sup.forge.close_issue(n)
            self.sup._issues.pop(n, None)  # the launch pass must see it closed, not the cached OPEN
            self.sup.say(f"#{n}: {wf} green on {base} at {run.head_sha[:7]} -- closed" + (" (its PR left open)" if note else ""))
            if rec.status == RUNNING and self.sup._alive(rec):
                self.sup._kill(rec, "done", f"{wf} is green again; the issue is closed")
        w.pop("issue", None)
        w["failures"], w["acknowledged"] = [], []
        return True

    # -- red --------------------------------------------------------------------------------------
    def _failures(self, run: Run) -> tuple[list[str], list[str], str, str]:
        """(failing tests, failing jobs, a short excerpt of the failed log, a shortfall note). Best effort, bounded.

        The note is set when the runners' summaries report more failures than were read from the log:
        the tests are then not the whole story, and a red run must not pass for "the same failures".
        """
        jobs: list[str] = []
        try:
            jobs = sorted(
                {
                    c.name
                    for c in self.sup.forge.checks(run.head_sha)
                    if (not run.suite or c.suite == run.suite) and c.status == "completed" and c.conclusion not in _OK
                }
            )
        except ForgeError:
            pass
        try:
            log = _ANSI.sub("", self.sup.forge.failed_log(run.id)[-LOG_BYTES:])
        except ForgeError:
            log = ""
        lines = [ln for ln in log.splitlines() if ln.strip()][-EXCERPT_LINES:]
        excerpt = "\n".join(ln[:300] for ln in lines)[-4000:]
        tests, read, reported = read_failures(log)
        short = f"Parsed {read} of {reported} failures; the log format may be unrecognised." if reported > read else ""
        return tests, jobs, excerpt, short

    def _red(self, wf: str, w: dict[str, Any], run: Run) -> bool:
        base = self.cfg.project.base_branch
        tests, jobs, excerpt, short = self._failures(run)
        sig = tests or [f"job: {j}" for j in jobs]
        n = int(w.get("issue") or 0)
        state = self.sup.issue(n, fresh=True).state if n else ""
        if state == UNKNOWN:
            return False  # never a second issue because the first could not be read
        if state == OPEN:
            self._again(wf, w, run, n, sig, short)
            w["failures"] = sig
            return True
        what = ", ".join(f"`{j}`" for j in jobs) or "see the run"
        body = (
            f"The `{wf}` workflow is red on `{base}` at `{run.head_sha}`: [run]({run.url}).\n\n"
            f"Failing jobs: {what}\n\n"
            + (f"Failing tests ({len(tests)}):\n{_bullets(tests)}\n\n" if tests else "")
            + (f"{short} Read the run's log for the rest.\n\n" if short else "")
            + (f"<details><summary>End of the failed log</summary>\n\n```\n{excerpt}\n```\n</details>\n\n" if excerpt else "")
            + f"`{wf}` does not gate merges (`[integrate] async_workflows`), so `{base}` has moved on since. "
            f"**First check whether the newest `{base}` still fails**: a later commit may already have fixed it. "
            "If it has, change nothing: end your session with a line\n\n"
            "    NOHALT-RESULT: no-change -- <the commit or reason that makes a change unnecessary>\n\n"
            f"and nohalt closes this issue when the next `{wf}` run on `{base}` is green. Otherwise fix it forward in a "
            "pull request.\n\n"
            f"nohalt keeps this one issue for `{wf}`: later red runs are added here as comments, and a green run closes it.\n\n"
            f"## Done when\n- [ ] A `{wf}` run on `{base}` is green"
        )
        labels = list(dict.fromkeys([self.cfg.integrate.async_label, "bug"]))
        try:
            n = self.sup.forge.create_issue(f"{wf} red on {base} at {run.head_sha[:7]}", body, labels=labels)
        except ForgeError as e:
            self.sup.say(f"async workflow {wf} red on {base}, but no issue could be opened yet: {e}")
            return False
        w.update(issue=n, failures=sig, acknowledged=[])
        try:
            queue.append(self.cfg.queue_file, n, front=True)
        except queue.QueueError as e:
            self.sup.say(f"async workflow {wf} red on {base}: opened #{n}, but could not queue it ({e}); add it by hand")
            return True
        self.sup.say(f"async workflow {wf} red on {base} at {run.head_sha[:7]}: opened #{n} at the front of the queue")
        return True

    def _again(self, wf: str, w: dict[str, Any], run: Run, n: int, sig: list[str], short: str = "") -> None:
        """A further red run while the workflow's issue is open: comment, and relaunch if the agent has not seen it.

        A run whose log reports more failures than could be read (``short``) counts as having new ones:
        "the same failures" would only be the ones nohalt can read.
        """
        before = set(w.get("failures") or [])
        new, gone = sorted(set(sig) - before), sorted(before - set(sig))
        if not sig:
            delta = "No failing tests could be read from its log."
        elif not new and not gone:
            delta = f"The same {len(sig)} failure(s) as the last red run" + (
                ", among those that could be read." if short else "."
            )
        else:
            delta = (f"New failures:\n{_bullets(new)}\n\n" if new else "") + (
                f"No longer failing:\n{_bullets(gone)}\n\n" if gone else ""
            )
            delta += f"{len(sig)} failing in all."
        if short:
            delta += f"\n\n{short}"
        rec = self.sup.state.get(n)
        unseen = sorted(set(sig) - set(w.get("acknowledged") or []))
        relaunch = rec.status == AWAITING_GREEN and bool(unseen or short)
        tail = ""
        if rec.status == AWAITING_GREEN:
            tail = (
                "\n\nThe agent's earlier finding (nothing to change) does not cover these failures: an agent is launched again."
                if relaunch
                else "\n\nStill waiting for a green run: nothing here that the agent has not already looked at."
            )
        self.sup.forge.comment(
            n,
            f"**nohalt:** `{wf}` is red again on `{self.cfg.project.base_branch}` at `{run.head_sha}` ([run]({run.url})).\n\n{delta}{tail}",
        )
        self.sup.say(
            f"#{n}: {wf} red again at {run.head_sha[:7]} -- commented"
            + (f"; {short[0].lower()}{short[1:]}" if short else "")
            + ("; relaunching" if relaunch else "")
        )
        if relaunch:
            self.sup._requeue(
                rec,
                f"{wf} red again at {run.head_sha[:7]} with failures the agent has not seen"
                + (" (some unreadable in its log)" if short else ""),
                resume=False,
                fresh=True,
                note=f"`{wf}` went red again on `{self.cfg.project.base_branch}` at `{run.head_sha}` ({run.url})"
                + (f", with failures your earlier no-change finding did not cover:\n{_bullets(unseen)}" if unseen else ".")
                + (f"\n\n{short} Read the run's log for failures not listed here." if short else "")
                + "\n\nLook at these on the newest base. Fix them forward in a pull request, or, if they too are already fixed, "
                "end with `NOHALT-RESULT: no-change -- <reason>` again.",
            )

    # -- an agent's "nothing to change" ------------------------------------------------------------
    def no_change(self, rec: IssueRecord, reason: str) -> None:
        """The agent found nothing to change on an async-red issue: wait for green, spend no attempt."""
        wf = self.owns(rec.issue)
        st = self._st()
        w = st.setdefault(wf, {})
        w["acknowledged"] = list(w.get("failures") or [])
        self._save(st)
        rec.status = AWAITING_GREEN
        rec.pid = 0
        rec.last_exit = "no-change"
        rec.event(f"agent says no change needed: {reason}; awaiting a green {wf} run")
        self.sup.state.put(rec)
        base = self.cfg.project.base_branch
        try:
            self.sup.forge.comment(
                rec.issue,
                f"**nohalt:** the agent found nothing to change: {reason}\n\n"
                f"This issue now waits for the next `{wf}` run on `{base}`: green closes it; red with failures other "
                "than the ones above sends an agent back.",
            )
        except ForgeError:
            pass
        self.sup.say(f"#{rec.issue}: agent says no change needed ({reason[:120]}) -- awaiting a green {wf} run")
