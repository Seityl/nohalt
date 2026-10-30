"""The supervisor: one loop that keeps every issue moving until the forge closes it.

Each tick:

1. **reap**   -- agents that exited, ran out of time, or hung are judged;
2. **watch**  -- finished agents' pull requests are followed through CI; a red
                or conflicting PR is handed back to an agent;
   **review**  -- with ``[review]``, a green PR whose issue needs a review goes
                to a reviewer agent first (review.py); only its approval makes it ready;
3. **integrate** -- green PRs are merged by the integrator (never by agents);
4. **launch** -- queued issues whose dependencies are closed get an agent, as
                slots, memory and a model that answers allow; with
                ``early_start``, also those whose open dependencies have green
                PRs, on a synthetic base (early.py);
5. **budget** -- paid-API spend is checked.

Rules that hold throughout:

* An issue is done when the forge says CLOSED. Nothing else -- not an exit
  code, not an agent's summary -- is evidence.
* A failure of the model endpoint is not an attempt on the issue.
* A forge call that failed is "unknown", never "no PR" or "not found".
* Work is never discarded: worktrees are rescued before removal.
* Agents run in their own sessions. Killing or restarting the supervisor does
  not touch them; a new supervisor adopts them from their records. That is also
  how a changed ``nohalt.toml`` (or ``nohalt restart``) takes effect: the
  supervisor re-executes itself between ticks and adopts its own agents.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import agents, budget, queue, shims, stacks
from . import config as config_mod
from . import early as early_mod
from . import forge as forge_mod
from . import prompt as prompt_mod
from . import upgrade as upgrade_mod
from .config import Config
from .forge import CLOSED, MISSING, OPEN, UNKNOWN, Forge, ForgeError, Issue, PullRequest
from .forge import verdict as ci_verdict
from .models import Router
from .review import Reviews
from .state import (
    ACTIVE,
    AWAITING_GREEN,
    BLOCKED,
    DONE,
    IN_REVIEW,
    QUEUED,
    READY,
    RELEASED,
    RUNNING,
    WAITING_CI,
    FileLock,
    IssueRecord,
    StateStore,
    pause_until,
)
from .util import descendants, kill_tree, mem_available_mb, now, pid_alive, stamp, tail
from .worktree import GitError, Worktrees

EXITED = "exited"  # agent gone; verdict pending (the forge could not be asked)

# Processes that are the agent itself rather than a command it is running: the
# agent CLIs by name, or a node/bun runtime whose command line names one of them.
AGENT_NAMES = ("opencode", "claude")
AGENT_RUNTIMES = {"node", "bun"}


def _is_agent_process(pid: int) -> bool:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return False
    if comm.lstrip(".") in AGENT_NAMES:
        return True
    if comm in AGENT_RUNTIMES:
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return False
        return any(name in cmd for name in AGENT_NAMES)
    return False


def _pid_start(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


@dataclass
class TickReport:
    launched: list[int] = field(default_factory=list)
    finished: list[int] = field(default_factory=list)
    running: list[int] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    blocked: list[int] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    deps: dict[int, str] = field(default_factory=dict)  # issue -> "waiting for #5 (no PR), #6 (PR red)"
    open_: int = 0
    complete: bool = False


class Supervisor:
    def __init__(
        self,
        cfg: Config,
        forge: Forge | None = None,
        *,
        say: Callable[[str], None] | None = None,
        router: Router | None = None,
    ):
        self.cfg = cfg
        self.state = StateStore(cfg.state_dir)
        self.forge: Forge = forge or forge_mod.make(cfg.project)
        self.trees = Worktrees(cfg)
        self.stack = stacks.get(cfg)
        self.router = router or Router(cfg, self.state)
        self.capacity = self.router.prober.capacity  # endpoint-down state and usage cool-downs
        self.capacity.say = self.say
        self._say = say
        self._procs: dict[int, subprocess.Popen[bytes]] = {}
        self._issues: dict[int, tuple[float, Issue]] = {}
        self._pulls: list[PullRequest] | None = None
        self._pulls_ok = False
        self._bad_config = ""  # hash of an invalid nohalt.toml already reported
        self.dry_run = False
        from .integrate import make as make_integrator

        self.integrator = make_integrator(self)
        self.reviews = Reviews(self)
        self.early = early_mod.Early(self)
        from .integrate.async_watch import AsyncWatch

        self.async_watch = AsyncWatch(self)
        self.upgrade = upgrade_mod.Watch()  # the nohalt package this process was loaded from

    # -- forge vocabulary ----------------------------------------------------------------------
    def pr(self, n: int) -> str:
        """'PR #5' on GitHub, 'MR !5' on GitLab (where '#5' would link to an issue)."""
        return f"MR !{n}" if self.cfg.project.forge == "gitlab" else f"PR #{n}"

    def pr_long(self, n: int) -> str:
        return f"merge request !{n}" if self.cfg.project.forge == "gitlab" else f"pull request #{n}"

    # -- output ------------------------------------------------------------------------
    def say(self, text: str, **fields: object) -> None:
        self.state.log(text, **fields)
        if self._say:
            self._say(text)

    # -- forge reads, cached per tick ---------------------------------------------------------
    def issue(self, n: int, fresh: bool = False) -> Issue:
        hit = self._issues.get(n)
        if hit and not fresh:
            ttl = 1800 if hit[1].state in (CLOSED, MISSING) else max(60, self.cfg.queue.tick * 2)
            if now() - hit[0] < ttl:
                return hit[1]
        try:
            iss = self.forge.issue(n)
        except ForgeError as e:
            self.state.log(f"#{n}: forge unreachable ({e}); state unknown")
            return Issue(n, UNKNOWN)
        self._issues[n] = (now(), iss)
        return iss

    def pulls(self) -> list[PullRequest] | None:
        """Open PRs, listed once per tick. None when the forge could not be asked."""
        if self._pulls is None and not self._pulls_ok:
            try:
                self._pulls = self.forge.open_pulls()
            except ForgeError as e:
                self.state.log(f"cannot list pull requests: {e}")
                self._pulls = None
            self._pulls_ok = True
        return self._pulls

    def pr_for(self, rec: IssueRecord) -> tuple[bool, PullRequest | None]:
        """(known, the issue's open PR or None). Not known: the forge could not be asked."""
        pulls = self.pulls()
        if pulls is None:
            return False, None
        if rec.pr:
            for p in pulls:
                if p.number == rec.pr:
                    return True, p
        for p in pulls:
            if rec.branch and p.head_ref == rec.branch:
                return True, p
        prefix = f"{rec.issue}-"
        release = self.cfg.integrate.train.branch_prefix
        for p in pulls:
            mine = p.head_ref.split("/", 1)[-1].startswith(prefix) or p.closes() == rec.issue
            if mine and not p.head_ref.startswith(release):
                return True, p
        return True, None

    # -- the loop ------------------------------------------------------------------------------
    def run(self, *, once: bool = False, exit_when_done: bool = False) -> int:
        lock = FileLock(self.state.root / "supervisor.lock")
        if not lock.acquire(blocking=False):
            print(f"nohalt: a supervisor is already running (pid {lock.held_by()})", file=sys.stderr)
            return 1
        stop = {"now": False}

        def on_signal(signum: int, _frame: object) -> None:
            stop["now"] = True

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        upgrade_mod.preload()  # no lazy import may read a newer nohalt mid-run (LIV-14)
        shims.install(self.state.bin_dir)
        (self.state.logs_dir / "guard.log").touch()  # exists, and stays empty while no agent oversteps
        self.state.clear_flag("CONFIG_INVALID")  # this configuration loaded and validated
        self.state.save("supervisor.json", {"pid": os.getpid(), "started": now(), "config": self.cfg.source_hash})
        self.say(
            f"=== nohalt supervising {self.cfg.project.repo} (base {self.cfg.project.base_branch}, "
            f"up to {self.cfg.agents.max_parallel} agents, integrate={self.cfg.integrate.mode})"
        )
        restart = ""
        try:
            while not stop["now"]:
                restart = self._restart_reason()
                if restart:
                    break
                report = self.tick()
                if self.state.has("STOP") and not report.running:
                    self.state.clear_flag("STOP")
                    self.say("=== stopped between issues (STOP)")
                    return 0
                if report.complete and exit_when_done:
                    self.say("=== complete: every queued issue is closed")
                    return 0
                if once:
                    return 0
                wait = (
                    self.cfg.queue.poll_when_idle
                    if (
                        report.complete
                        or (not report.running and not report.launched and report.open_ and not self._has_waiting())
                    )
                    else self.cfg.queue.tick
                )
                for _ in range(int(wait)):
                    if stop["now"] or self.state.has("WAKE"):
                        self.state.clear_flag("WAKE")
                        break
                    if self._restart_pending():
                        break
                    time.sleep(1)
        finally:
            lock.release()
        if restart:
            self.say(f"=== {restart}: re-executing the supervisor; running agents continue and are adopted")
            self._reexec(exit_when_done)
            return 1  # not reached unless exec failed; keepalive starts a new one
        self.say("=== supervisor exiting; running agents continue and will be adopted on restart")
        return 0

    # -- configuration reload and restart ---------------------------------------------------------
    # A new configuration is applied by re-executing the supervisor, not by swapping
    # objects: the forge, stack, router and integrator are all built from it, and a
    # fresh process is exactly the restart path agents are already adopted through
    # (LIV-01). Running agents keep their model; pools and models apply to new launches.
    def check_config(self) -> bool:
        """Has nohalt.toml changed into a valid configuration to switch to?

        An invalid file is reported once, keeps the running configuration, and
        raises ``CONFIG_INVALID`` until it is fixed.
        """
        src = self.cfg.source
        if src is None:
            return False
        seen = config_mod.fingerprint(src)
        if seen == self.cfg.source_hash:
            if self.state.has("CONFIG_INVALID"):
                self.state.clear_flag("CONFIG_INVALID")
                self.say(f"config: {src} is back to the running configuration")
            return False
        if seen == self._bad_config:
            return False
        try:
            new = config_mod.load(src)
            stacks.get(new)
            if new.state_dir != self.cfg.state_dir:
                raise config_mod.ConfigError(
                    f"[project] state_dir changed to {new.state_dir}: a running supervisor cannot move its state "
                    "(its agents are recorded in the old one); revert it, or stop and start by hand"
                )
        except ValueError as e:  # ConfigError, and anything a stack rejects
            self._bad_config = seen
            self.state.set_flag("CONFIG_INVALID", f"{stamp()}  {e}\n")
            self.say(f"config: {src} is invalid; still running on the last good configuration -- {e}")
            return False
        self._bad_config = ""
        self.state.clear_flag("CONFIG_INVALID")
        if replace(new, source_hash=self.cfg.source_hash) == self.cfg:
            self.cfg.source_hash = new.source_hash  # comments or formatting only
            return False
        return True

    def _restart_reason(self) -> str:
        if self.state.has("RESTART"):
            self.state.clear_flag("RESTART")
            return "restart requested"
        if self._upgraded():
            return "nohalt upgraded on disk"
        if self.check_config():
            return f"{self.cfg.source} changed"
        return ""

    def _restart_pending(self) -> bool:
        """Cheap check between ticks: end the wait early for a restart or a changed config."""
        if self.state.has("RESTART") or self.upgrade.settled():
            return True
        src = self.cfg.source
        return src is not None and config_mod.fingerprint(src) not in (self.cfg.source_hash, self._bad_config)

    def _upgraded(self) -> bool:
        """Is a new nohalt installed, settled and importable? Then re-execute on it (LIV-14).

        One that does not import is reported once and the loaded code keeps running.
        """
        if not self.upgrade.settled():
            return False
        new = self.upgrade.installed()
        why = self.upgrade.broken()
        if why:
            self.say(f"nohalt {new} on disk does not import ({why}); still running {self.upgrade.running}")
            return False
        self.say(f"nohalt on disk changed ({self.upgrade.running} -> {new})")
        return True

    def reexec_argv(self, exit_when_done: bool = False) -> list[str]:
        argv = [sys.executable, "-m", "nohalt"]
        if self.cfg.source:
            argv += ["--config", str(self.cfg.source)]
        argv.append("run")
        if exit_when_done:
            argv.append("--exit-when-done")
        return argv

    def _reexec(self, exit_when_done: bool) -> None:
        argv = self.reexec_argv(exit_when_done)
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.chdir(self.cfg.root)
            os.execv(argv[0], argv)
        except OSError as e:
            self.say(f"=== could not re-execute the supervisor ({e}); exiting, `nohalt keepalive` starts a new one")

    def _has_waiting(self) -> bool:
        return any(r.status in (WAITING_CI, IN_REVIEW, READY, EXITED) for r in self.state.all())

    def tick(self) -> TickReport:
        self._pulls, self._pulls_ok = None, False
        report = TickReport()
        self._pause_expiry()
        records = {r.issue: r for r in self.state.all()}
        for rec in records.values():
            if rec.status in (RUNNING, EXITED):
                self._reap(rec, report)
        for rec in self.state.all():
            if rec.status in (WAITING_CI, IN_REVIEW, READY):
                self._watch(rec, report)
        self._review()
        self.early.tick()  # early dependants: moved dependencies noted, merged ones refreshed onto the base
        if self.cfg.integrate.mode != "none" and not self.dry_run:
            try:
                self.integrator.tick()
            except (ForgeError, GitError) as e:
                self.say(f"integrator: {e}")
        if self.cfg.integrate.async_workflows and not self.dry_run:
            self.async_watch.tick()
        self._capacity_tick()
        self._launch(report)
        if self.cfg.budget.enabled and not self.dry_run:
            self._budget()
        report.running = [r.issue for r in self.state.all() if r.status == RUNNING]
        self._snapshot(report)
        return report

    def _review(self) -> None:
        """The review gate (review.py): reviewers launched, reaped and judged; verdicts applied."""
        if not self.dry_run and (self.cfg.review.enabled or any(r.review_pid for r in self.state.all())):
            self.reviews.tick()

    # -- 1. reap --------------------------------------------------------------------------------
    def _alive(self, rec: IssueRecord) -> bool:
        proc = self._procs.get(rec.issue)
        if proc is not None and proc.poll() is not None:
            self._procs.pop(rec.issue, None)
            return False
        if proc is None and rec.pid:
            # Adopted after a re-exec, the agent is still our child: reap it once it exits.
            try:
                os.waitpid(rec.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if not pid_alive(rec.pid):
            return False
        # A recycled pid is not our agent.
        start = self.state.load("pids.json", {}).get(str(rec.issue))
        return not start or start == _pid_start(rec.pid)

    def timeout_for(self, issue: Issue) -> int:
        a = self.cfg.agents
        return a.long_timeout if a.long_labels and issue.has_label(*a.long_labels) else a.phase_timeout

    def _busy(self, pid: int) -> bool:
        """Is the agent running a command of its own (a test, a sleep while it polls CI)?

        The agent is its root process plus any agent CLI process (or node/bun
        process running one) whose parent is part of the agent. Any other
        descendant -- a shell, a test runner, even `python -m pytest` that a
        shell exec'd into -- is a command the agent is running.
        """
        agent = {pid}
        for p in descendants(pid):  # parents always come before their children
            try:
                ppid = int(Path(f"/proc/{p}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            if ppid in agent and _is_agent_process(p):
                agent.add(p)
            else:
                return True
        return False

    def _reap(self, rec: IssueRecord, report: TickReport) -> None:
        if rec.status == RUNNING and self._alive(rec):
            elapsed = now() - rec.started_at
            issue = self.issue(rec.issue)
            limit = self.timeout_for(issue)
            if elapsed > limit:
                self._kill(rec, "timeout", f"ran out of time ({limit // 60} min)")
            else:
                log = Path(rec.log)
                idle = now() - log.stat().st_mtime if log.exists() else 0
                if idle > self.cfg.agents.hung_after and not self._busy(rec.pid):
                    # Waiting on a model reply that will not come.
                    self._kill(rec, "hung", f"no output for {int(idle // 60)} min and no command running")
                else:
                    return
        self._judge(rec, report)

    def _kill(self, rec: IssueRecord, reason: str, text: str) -> None:
        rec.last_exit = reason
        rec.event(f"stopped: {text}")
        self.state.put(rec)
        self.say(f"#{rec.issue}: stopping the agent -- {text}")
        kill_tree(rec.pid, signal.SIGTERM)
        for _ in range(20):
            if not pid_alive(rec.pid):
                break
            time.sleep(0.5)
        else:
            kill_tree(rec.pid, signal.SIGKILL)
        proc = self._procs.pop(rec.issue, None)
        if proc:
            proc.poll()

    def _judge(self, rec: IssueRecord, report: TickReport) -> None:
        """The agent is gone. Decide what that means -- asking the forge, not the agent."""
        killed = rec.last_exit if rec.last_exit in ("timeout", "hung", "handover", "stopped") else ""
        issue = self.issue(rec.issue, fresh=True)
        if issue.state == UNKNOWN:
            if rec.status != EXITED:
                rec.status = EXITED
                self.state.put(rec)
            return
        if issue.state in (CLOSED, MISSING):
            self._done(rec, report, "closed" if issue.state == CLOSED else "no longer exists")
            return
        a = self.cfg.agents
        # Only this launch's output: an error from an earlier launch is not evidence.
        log_tail = tail(Path(rec.log), 6000).rsplit("\n--- nohalt launch ", 1)[-1]
        agent = self._agent(rec.model)
        ran_for = now() - rec.started_at if rec.started_at else 0
        rec.pid = 0

        if killed == "handover":
            return self._requeue(rec, "moved to another model by the budget guard", resume=True)
        if killed == "stopped":
            return self._requeue(rec, "the supervisor was stopped", resume=True)
        if agent and agent.usage_limited(log_tail):
            return self._usage_limited(rec, agent, log_tail)
        if not killed and agent and ran_for < 300 and agent.setup_failed(log_tail):
            # The agent never got started: not logged in, refused by a model
            # router, given an option it rejects. Retrying the issue on the same
            # setup cannot succeed, so it costs no attempt; the model is marked
            # as not answering (a fallback is used, or launches wait for a probe
            # that passes), and repeated failures go to a human.
            self.router.prober.mark_limited(rec.model)
            rec.launch_failures += 1
            line = next((ln.strip() for ln in log_tail.splitlines() if agents.SETUP_ERROR.search(ln)), "?")
            if rec.launch_failures >= 3:
                return self._block(
                    rec,
                    f"the agent could not start on {rec.model} ({line[:200]}); this is the agent's setup, not the issue -- "
                    "fix its login or configuration, check `nohalt doctor --probe`",
                )
            return self._requeue(
                rec, f"the agent could not start on {rec.model}: {line[:160]} (not counted as an attempt)", resume=False
            )
        if not killed and agent and agent.provider_failed(log_tail):
            if self._endpoint_down(rec):
                return
            rec.provider_retries += 1
            if rec.provider_retries > a.provider_retries:
                last = [ln for ln in log_tail.splitlines() if "rror" in ln][-1:]
                return self._block(
                    rec,
                    f"the model endpoint failed {rec.provider_retries} times on this issue "
                    f"(last: {last[0].strip()[:200] if last else '?'}); this was not the issue's fault",
                )
            return self._requeue(
                rec,
                f"model endpoint error, not counted as an attempt (retry {rec.provider_retries}/{a.provider_retries})",
                resume=True,
                delay=a.provider_backoff * rec.provider_retries,
            )
        if killed == "timeout":
            rec.timeouts += 1
            if rec.timeouts <= a.timeout_continues:
                return self._requeue(
                    rec, f"ran out of time; continuing in the same worktree ({rec.timeouts}/{a.timeout_continues})", resume=True
                )
        if killed == "hung":
            rec.revivals += 1
            if rec.revivals <= a.revive_limit:
                return self._requeue(rec, f"hung; restarted ({rec.revivals}/{a.revive_limit})", resume=True)

        known, pr = self.pr_for(rec)
        if not known:
            rec.status = EXITED
            self.state.put(rec)
            return
        if pr:
            rec.pr, rec.pr_sha = pr.number, pr.head_sha
            rec.status = WAITING_CI
            rec.last_exit = "pr"
            rec.event(f"agent finished with {self.pr(pr.number)}")
            self.reviews.author_finished(rec, log_tail)  # its "updated-description" line, a hint for the gate
            self.state.put(rec)
            self.say(f"#{rec.issue}: agent finished; {self.pr(pr.number)} goes through CI")
            return
        reason = agents.no_change(log_tail)
        if reason:
            return self._no_change(rec, reason)
        rec.attempts += 1
        if rec.attempts >= a.max_attempts:
            return self._block(rec, f"still open after {rec.attempts} attempt(s), with no pull request")
        # A grace period: an agent that stopped to ask a question may get an answer.
        self._requeue(
            rec,
            f"stopped without closing the issue or opening a PR (attempt {rec.attempts}/{a.max_attempts})",
            resume=True,
            delay=a.revive_grace,
            note="You stopped without opening a pull request. If you are blocked, say exactly why in a "
            "comment on the issue and stop; otherwise continue the work and open the PR.",
        )

    def _no_change(self, rec: IssueRecord, reason: str) -> None:
        """The agent says nothing needs changing (AGT-19). Not an attempt, and not left to look like one.

        On an async workflow's red issue the next green run decides (``awaiting_green``).
        Anywhere else a human decides, and the reason is what they read.
        """
        if self.async_watch.owns(rec.issue):
            return self.async_watch.no_change(rec, reason)
        self._block(rec, f"agent says no change needed: {reason}")

    # -- 2. watch PRs through CI -------------------------------------------------------------------------
    def _watch(self, rec: IssueRecord, report: TickReport) -> None:
        issue = self.issue(rec.issue, fresh=rec.status == READY)
        if issue.state in (CLOSED, MISSING):
            self._done(rec, report, "closed")
            return
        if issue.state == UNKNOWN:
            return
        known, pr = self.pr_for(rec)
        if not known:
            return
        if pr is None:
            merged = False
            if rec.pr:
                try:
                    merged = self.forge.pull(rec.pr).merged
                except ForgeError:
                    return
            if merged:
                # Merged but the issue is open: closing keywords act only on the
                # default branch, or the body lacked one. The merge finished it.
                from .integrate.common import close_merged

                close_merged(self, [rec.issue], f"{self.pr(rec.pr)}")
                if self.issue(rec.issue, fresh=True).state == CLOSED:
                    self._done(rec, report, f"closed ({self.pr(rec.pr)} merged)")
                return
            return self._requeue(
                rec,
                f"{self.pr(rec.pr)} was closed without merging",
                resume=True,
                note=f"Your {self.pr_long(rec.pr)} was closed without being merged. Read its "
                "comments, then continue: fix what was asked and open a new PR.",
            )
        if pr.head_sha != rec.pr_sha:
            rec.pr_sha = pr.head_sha
            rec.status = WAITING_CI
            self.state.put(rec)
        try:
            checks = self.forge.checks(pr.head_sha)
        except ForgeError:
            return
        ignored = set(self.cfg.integrate.ignore_checks)
        checks = [c for c in checks if c.name not in ignored]
        v = ci_verdict(checks, self.cfg.integrate.required_checks)
        if v == forge_mod.base.RED:
            if pr.head_sha in rec.swept:
                return
            bad = [
                c
                for c in checks
                if c.status == "completed" and c.conclusion not in ("success", "skipped", "neutral", "cancelled")
            ]
            detail = "\n".join(f"- {c.name}: {c.conclusion} {c.url}" for c in bad[:10])
            return self._hand_back(
                rec,
                pr,
                "CI is red",
                f"CI is red on your {self.pr_long(pr.number)} "
                f"(head {pr.head_sha[:8]}):\n{detail}\n\nRead the failure, check whether it "
                f"also fails on {self.cfg.project.base_branch}, fix what is yours, run the "
                "tests you touched, push, and stop once pushed.",
            )
        # A conflicted PR may never get CI at all (the forge does not run it), so
        # mergeability is checked when checks are green or have not started.
        if v == forge_mod.base.GREEN or not checks:
            if pr.mergeable is None:
                try:
                    pr = self.forge.pull(pr.number)
                except ForgeError:
                    return
            if pr.mergeable is False:
                if pr.head_sha in rec.swept:
                    return
                if not self._conflict_left_to_batch(rec, pr, v):
                    from .integrate.refresh import before_hand_back

                    if before_hand_back(self, rec, pr):
                        return  # bookkeeping only: nohalt merged the base itself (INT-12)
                    return self._hand_back(
                        rec,
                        pr,
                        "conflicts with the base branch",
                        f"Your {self.pr_long(pr.number)} conflicts with "
                        f"`{self.cfg.project.base_branch}`. Fetch and merge "
                        f"`origin/{self.cfg.project.base_branch}` into your branch, keeping both "
                        "sides' behaviour, re-run the tests you touched, push, and stop.",
                    )
        if v == forge_mod.base.GREEN and pr.draft:
            # The prompt asks agents to open a draft early. The agent has finished
            # and the checks are green: the draft has done its job, and forges
            # refuse to merge drafts, so nohalt marks it ready.
            try:
                self.forge.mark_ready(pr.number)
            except ForgeError as e:
                self.state.log(f"#{rec.issue}: could not mark {self.pr(pr.number)} ready yet ({e})")
                return
            self.say(f"#{rec.issue}: {self.pr(pr.number)} was a draft; marked ready (green, agent finished)")
        if v == forge_mod.base.GREEN:
            self.reviews.gate(rec, pr)  # ready, or in review when the issue needs one

    def _conflict_left_to_batch(self, rec: IssueRecord, pr: PullRequest, v: str) -> bool:
        """Train: a green PR whose conflicts the drivers settle goes on to the batch unrefreshed (#69).

        Not green (a conflicted PR may get no CI at all): the refresh is what lets CI run.
        """
        from .integrate.refresh import left_to_batch

        return v == forge_mod.base.GREEN and left_to_batch(self, rec, pr)

    def _hand_back(self, rec: IssueRecord, pr: PullRequest, why: str, note: str) -> None:
        rec.swept.append(pr.head_sha)
        del rec.swept[:-20]
        rec.ci_rounds += 1
        if rec.ci_rounds > self.cfg.agents.ci_rounds:
            return self._block(rec, f"{self.pr(pr.number)}: {why}, after {rec.ci_rounds - 1} rounds handed back")
        try:
            self.forge.comment(rec.issue, f"**nohalt:** {note}")
        except ForgeError:
            pass
        self._requeue(
            rec, f"{self.pr(pr.number)}: {why}; handed back ({rec.ci_rounds}/{self.cfg.agents.ci_rounds})", resume=True, note=note
        )

    def hand_back(self, issue: int, why: str, note: str) -> None:
        """Called by the integrator: send an issue back to an agent with a note."""
        rec = self.state.get(issue)
        if rec.status == RUNNING:
            try:
                self.forge.comment(issue, f"**nohalt:** {note}")
            except ForgeError:
                pass
            return
        rec.ci_rounds += 1
        if rec.ci_rounds > self.cfg.agents.ci_rounds:
            return self._block(rec, f"{why}, after {rec.ci_rounds - 1} rounds handed back")
        try:
            self.forge.comment(issue, f"**nohalt:** {note}")
        except ForgeError:
            pass
        self._requeue(rec, why, resume=True, note=note, fresh=True)

    # -- record transitions ---------------------------------------------------------------------------------
    def _requeue(
        self, rec: IssueRecord, why: str, *, resume: bool, delay: float = 0, note: str = "", fresh: bool = False
    ) -> None:
        rec.status = QUEUED
        rec.pid = 0
        rec.resume = resume and not fresh
        rec.fresh = fresh
        rec.not_before = now() + delay
        rec.note = note or why
        rec.last_exit = ""
        rec.event(why)
        self.state.put(rec)
        self.say(f"#{rec.issue}: {why}" + (f"; again in {int(delay // 60)} min" if delay >= 60 else ""))

    def _block(self, rec: IssueRecord, reason: str) -> None:
        label = self.cfg.queue.blocked_label
        rec.status = BLOCKED
        rec.pid = 0
        rec.blocked_reason = reason
        rec.event(f"blocked: {reason}")
        self.state.put(rec)
        self.say(f"#{rec.issue} -> {label}: {reason}")
        if self.dry_run:
            return
        try:
            self.forge.add_label(rec.issue, label)
            self.forge.comment(
                rec.issue,
                f"**nohalt:** skipped -- {reason}.\n\nThe agent's log is on the runner host at "
                f"`{rec.log}`. Read the agent's own comments above for where it stopped. Work that does not "
                f"depend on this issue continues. **Remove the `{label}` label to have nohalt try again.**",
            )
            rec.labelled = True
            self.state.put(rec)
            self._issues.pop(rec.issue, None)
        except ForgeError as e:
            self.say(f"#{rec.issue}: could not label it blocked yet ({e}); will retry")

    def _done(self, rec: IssueRecord, report: TickReport, how: str) -> None:
        if self.dry_run:  # a dry run reports; it never changes anything
            report.finished.append(rec.issue)
            return
        if rec.status == RUNNING and self._alive(rec):
            self._kill(rec, "done", "the issue is closed")
        rec.status = DONE
        rec.pid = 0
        rec.done_at = now()
        rec.event(how)
        self.state.put(rec)
        report.finished.append(rec.issue)
        self.say(f"#{rec.issue}: {how} -- done")
        try:
            rescued = self.trees.remove(rec.issue)
            if rescued:
                self.say(f"#{rec.issue}: uncommitted work kept on branch {rescued}")
        except GitError as e:
            self.say(f"#{rec.issue}: worktree not removed: {e}")
        shutil.rmtree(self.state.agent_data / str(rec.issue), ignore_errors=True)
        self.router.unpin(rec.issue)

    # -- 4. launch ---------------------------------------------------------------------------------------
    def _pool_of(self, model: str) -> str:
        for prefix in sorted(self.cfg.agents.pools, key=len, reverse=True):
            if model.startswith(prefix):
                return prefix
        return ""

    def _cap(self, pool: str) -> int:
        override = self.state.load("caps.json", {})
        if pool in override:
            return int(override[pool])
        return self.cfg.agents.pools[pool] if pool else self.cfg.agents.max_parallel

    def _has_slot(self, model: str, in_use: dict[str, int], reserve: bool = False) -> bool:
        """A free slot in the model's pool; overflow (``reserve``) may not take a pool's last reserved slots."""
        pool = self._pool_of(model)
        held = self.cfg.models.overflow_reserve.get(pool, 0) if reserve else 0
        return in_use.get(pool, 0) < self._cap(pool) - held

    def _stall_reason(self, model: str, in_use: dict[str, int], warned_ok: bool = False) -> str:
        unavailable = self.capacity.unavailable(model, warned_ok)
        if unavailable:
            return f"{unavailable} and no fallback or overflow model can start"
        pool = self._pool_of(model)
        if not self._has_slot(model, in_use):
            return f"{pool or 'shared'} pool full ({in_use.get(pool, 0)}/{self._cap(pool)})"
        return f"{model} not answering and no fallback has a free slot"

    def _stall(self, rec: IssueRecord) -> None:
        """Start the overflow clock: the issue could start but its model has no slot."""
        if self.cfg.models.overflow_after and not rec.stalled_since and not self.dry_run:
            rec.stalled_since = now()
            self.state.put(rec)

    def _overflow_due(self, rec: IssueRecord, pinned: bool) -> bool:
        """MOD-13: waited ``overflow_after`` for a slot. Pinned issues wait for their model (MOD-05)."""
        after = self.cfg.models.overflow_after
        return bool(after and not pinned and rec.stalled_since and now() - rec.stalled_since >= after)

    def _launch(self, report: TickReport) -> None:
        try:
            entries = queue.load(self.cfg.queue_file)
        except queue.QueueError as e:
            self.say(f"queue: {e}")
            return
        stopping = self.state.has("STOP") or self.state.has("PAUSE")
        records = {r.issue: r for r in self.state.all()}
        in_use: dict[str, int] = {}
        for r in records.values():
            if r.status == RUNNING:
                p = self._pool_of(r.model)
                in_use[p] = in_use.get(p, 0) + 1
        for p, n in self.reviews.in_use().items():
            in_use[p] = in_use.get(p, 0) + n

        reopened = self._reopened([records[e.issue] for e in entries if e.issue in records and records[e.issue].status == DONE])
        open_count = 0
        for entry in entries:
            rec = records.get(entry.issue) or IssueRecord(issue=entry.issue)
            if rec.status == DONE and not (entry.issue in reopened and self._reopen(rec)):
                continue
            issue = self.issue(entry.issue)
            if issue.state == MISSING:
                if rec.status != DONE and not self.dry_run:
                    rec.status = DONE
                    rec.done_at = now()
                    rec.event("does not exist on the forge; dropped")
                    self.state.put(rec)
                    self.say(f"#{entry.issue} does not exist -- dropped from the queue")
                continue
            if issue.state == CLOSED:
                if rec.status not in ACTIVE:
                    self._done(rec, report, "closed")
                continue
            open_count += 1
            if issue.state == UNKNOWN:
                continue
            labelled = issue.has_label(self.cfg.queue.blocked_label)
            if rec.status == BLOCKED:
                if labelled:
                    report.blocked.append(entry.issue)
                    continue
                if not rec.labelled and not self.dry_run:
                    # Labelling failed earlier: try again rather than read its absence as an unblock.
                    self._block(rec, rec.blocked_reason)
                    report.blocked.append(entry.issue)
                    continue
                rec.attempts = rec.provider_retries = rec.ci_rounds = rec.timeouts = rec.revivals = rec.launch_failures = 0
                rec.review_rounds = rec.review_failures = rec.review_retries = 0
                rec.review_verdict = ""
                rec.hold_model, rec.down_waiting = "", 0.0
                rec.status, rec.resume, rec.not_before, rec.labelled = QUEUED, True, 0, False
                rec.note = "A human removed the blocked label: try again."
                rec.event("unblocked by a human")
                self.state.put(rec)
                self.say(f"#{entry.issue}: unblocked by a human -- back in the queue")
            elif labelled:
                rec.status = BLOCKED
                rec.event("labelled blocked by a human")
                rec.labelled = True
                self.state.put(rec)
                report.blocked.append(entry.issue)
                continue
            if rec.status in ACTIVE or rec.status in (EXITED, AWAITING_GREEN, RELEASED):
                continue  # RELEASED: merged, waiting to be live (integrate/live.py)
            if issue.has_label(*self.cfg.queue.held_labels):
                report.waiting.append(f"#{entry.issue}(held)")
                continue
            pending = [d for d in entry.deps if self.issue(d).state != CLOSED]
            if pending and not self._may_start_early(rec, pending, entries, report):
                continue
            if not rec.started_at and not rec.pr:
                # Work that already has an open PR (started by hand, or by a runner
                # nohalt replaced) is watched, not redone: an agent is launched only
                # if the PR needs one (red, conflicting, closed unmerged).
                known, existing = self.pr_for(rec)
                if not known:
                    continue
                if existing and self.dry_run:
                    report.adopted.append(f"#{entry.issue}<-{self.pr(existing.number)}")
                    continue
                if existing:
                    rec.pr, rec.pr_sha, rec.branch = existing.number, existing.head_sha, existing.head_ref
                    rec.status = WAITING_CI
                    rec.event(f"adopted existing {self.pr(existing.number)} ({existing.head_ref})")
                    self.state.put(rec)
                    self.say(f"#{entry.issue}: already has {self.pr(existing.number)} -- adopted, watching it through CI")
                    continue
            if rec.not_before > now() or stopping:
                if rec.stalled_since:  # the overflow clock runs only while the issue could start
                    rec.stalled_since = 0
                    self.state.put(rec)
                continue
            model, pinned = self._wanted(rec, issue)
            pool = self._pool_of(model)
            overflow_due = self._overflow_due(rec, pinned)
            warned_ok = pinned and self.router.pin_through_warning(rec.issue)
            unavailable = self.capacity.unavailable(model, warned_ok)
            if not self._has_slot(model, in_use) and not overflow_due and not unavailable:
                self._stall(rec)
                continue
            if self.dry_run:
                if self._has_slot(model, in_use):
                    report.launched.append(entry.issue)
                    in_use[pool] = in_use.get(pool, 0) + 1
                continue
            free = mem_available_mb()
            if free is not None and free < self.cfg.agents.min_free_mb:
                self.state.log(f"#{entry.issue} waiting: {free} MB available, need {self.cfg.agents.min_free_mb}")
                break
            overflow = ""
            if unavailable:  # down or cooling down: elsewhere at once, or wait if pinned
                chosen, overflow = self._divert(issue, model, pinned, unavailable, in_use)
            else:
                chosen = self.router.choose(issue, lambda m: self._has_slot(m, in_use), want=(model, pinned), warned_ok=warned_ok)
            if not chosen and overflow_due:
                chosen = self.router.overflow(issue, lambda m: self._has_slot(m, in_use, reserve=True))
                if chosen:
                    overflow = (
                        f"overflow: {self._stall_reason(model, in_use)} for {int((now() - rec.stalled_since) // 60)}m → {chosen}"
                    )
            if not chosen:
                if self._down_block(rec, model, pinned):
                    continue
                self._stall(rec)
                self.state.log(f"#{entry.issue} waiting: {self._stall_reason(model, in_use, warned_ok)}")
                continue
            try:
                self.launch(rec, issue, chosen, overflow=overflow, cooling=self._pin_cooling(chosen, warned_ok))
            except early_mod.NoEarlyStart as e:
                self.early.refused(rec, pending, str(e))
                report.waiting.append(f"#{entry.issue}<-{','.join('#' + str(d) for d in pending)}")
                continue
            except (GitError, prompt_mod.PromptError, OSError, ValueError) as e:
                rec.not_before = now() + 600
                rec.event(f"launch failed: {e}")
                self.state.put(rec)
                self.say(f"#{entry.issue}: launch failed: {e}")
                continue
            report.launched.append(entry.issue)
            p2 = self._pool_of(chosen)
            in_use[p2] = in_use.get(p2, 0) + 1
        report.open_ = open_count
        report.complete = open_count == 0 and not any(r.status in ACTIVE for r in self.state.all())

    def _may_start_early(self, rec: IssueRecord, pending: list[int], entries: list[queue.Entry], report: TickReport) -> bool:
        """Dependencies still open: start early on a synthetic base (SCH-10), or wait and say what for."""
        if self.early.can_start(rec, pending, entries):
            return True
        report.waiting.append(f"#{rec.issue}<-{','.join('#' + str(d) for d in pending)}")
        report.deps[rec.issue] = self.early.waiting_for(pending, rec)
        return False

    # -- reopened issues (AGT-20) ----------------------------------------------------------------------------
    def _reopened(self, done: list[IssueRecord]) -> set[int]:
        """The queued issues recorded done that the forge lists as open again.

        One listing per tick, of open issues updated since the last listing
        (API-05): a reopen updates the issue. The first listing reaches back to
        the oldest done record (or lists every open issue, for records written
        before ``done_at`` existed). A failed listing is no answer (API-02).
        Candidates stay pending until ``_reopen`` settles them: the next listing
        starts after their reopen, so dropping one would lose it.
        """
        if not done or self.dry_run:
            return set()
        saved = self.state.load("reopen.json", {})
        ids = {r.issue for r in done}
        pending = set(saved.get("pending", [])) & ids
        since = saved.get("since")
        if since is None:
            since = min(r.done_at for r in done)
        started = now()
        try:
            listed = self.forge.open_issues(since)
        except ForgeError as e:
            self.state.log(f"cannot list open issues: {e}")
            return pending
        pending |= listed & ids
        # Overlap the next listing a little: the forge's clock is not ours.
        self.state.save("reopen.json", {"since": max(0.0, started - 300), "pending": sorted(pending)})
        return pending

    def _settled(self, issue: int) -> None:
        saved = self.state.load("reopen.json", {})
        if issue in saved.get("pending", []):
            saved["pending"] = [n for n in saved["pending"] if n != issue]
            self.state.save("reopen.json", saved)

    def _reopen(self, rec: IssueRecord) -> bool:
        """A done issue is open on the forge again: it is new work. Queue it again.

        The attempt counters start over; the history stays. The branch is kept
        unless it was merged -- then a fresh one is cut from the base, beside it.
        Anything the forge cannot answer leaves the record done until a later tick.
        """
        issue = self.issue(rec.issue, fresh=True)
        if issue.state != OPEN:
            if issue.state != UNKNOWN:
                self._settled(rec.issue)  # closed again (or gone) before nohalt got to it
            return False
        merged = False
        if rec.pr:
            try:
                merged = self.forge.pull(rec.pr).merged
            except ForgeError as e:
                self.state.log(f"#{rec.issue}: reopened, but {self.pr(rec.pr)} cannot be read yet ({e})")
                return False
        try:
            if not merged and rec.branch:
                merged = self.trees.merged(rec.branch)
            comments = self.forge.comments_since(rec.issue, rec.done_at)
        except (ForgeError, GitError) as e:
            self.state.log(f"#{rec.issue}: reopened, but not queued yet ({e})")
            return False
        old = rec.branch
        if merged and old:
            if (self.trees.path(rec.issue) / ".git").exists():
                rescued = self.trees.remove(rec.issue)  # a tree left on the merged branch
                if rescued:
                    self.say(f"#{rec.issue}: uncommitted work kept on branch {rescued}")
            rec.branch, rec.worktree = self.trees.next_branch(old), ""
        rec.attempts = rec.provider_retries = rec.launch_failures = rec.timeouts = rec.ci_rounds = rec.revivals = 0
        rec.review_rounds = rec.review_failures = rec.review_retries = 0
        rec.review_verdict = rec.review_text = rec.review_approved = rec.review_sha = rec.review_exit = ""
        rec.review_body = rec.review_body_hint = ""
        rec.review_not_before = 0
        # Not the old PR: the agent is launched, and finds (and continues) a PR still open on its branch.
        rec.pr, rec.pr_sha, rec.pid = 0, "", 0
        rec.blocked_reason, rec.labelled, rec.stalled_since, rec.overflow = "", False, 0, ""
        rec.status, rec.not_before, rec.last_exit = QUEUED, 0, ""
        rec.resume, rec.fresh = False, True
        rec.note = prompt_mod.reopen_note(
            comments, since_known=bool(rec.done_at), branch=rec.branch, old_branch=old, base=self.cfg.project.base_branch
        )
        rec.event(
            "reopened on the forge -- queued again"
            + (f"; {old} was merged, new branch {rec.branch}" if rec.branch != old else "")
        )
        self.state.put(rec)
        self._settled(rec.issue)
        self.say(f"#{rec.issue}: reopened on the forge -- queued again")
        if rec.branch != old:
            self.say(f"#{rec.issue}: {old} was merged; a fresh branch {rec.branch} is cut from {self.cfg.project.base_branch}")
        return True

    def _agent(self, model: str) -> agents.Agent | None:
        if not model:
            return None
        name, _ = agents.split_model(model)
        return agents.get(name, self.cfg.agents.options.get(name))

    def settle_lane(self, rec: IssueRecord, issue: Issue) -> str:
        """Choose (once) or re-choose (the lanes changed) the issue's prompt lane; log a change."""
        change = prompt_mod.settle_lane(self.cfg, rec, issue)
        if change:
            self.say(f"#{issue.number}: {change}")
        return change

    def compose_prompt(self, rec: IssueRecord, issue: Issue, branch: str, tree: Path) -> str:
        """The rendered author prompt in the issue's lane, without the issue text or the supervisor's note."""
        lane = prompt_mod.find_lane(self.cfg, rec.lane)
        text = prompt_mod.template(self.cfg, self.stack.prompt_addendum, lane)
        values = prompt_mod.values(self.cfg, issue=issue.number, title=issue.title, branch=branch, worktree=tree, lane=lane)
        return prompt_mod.render(text, values)

    def render_prompt(self, rec: IssueRecord, issue: Issue, branch: str, tree: Path) -> str:
        body = self.compose_prompt(rec, issue, branch, tree)
        if self.cfg.prompt.inline_issue:
            try:
                body += f"\n## The issue, as it read when you were launched\n\n{self.forge.issue_text(issue.number)}\n"
            except ForgeError as e:
                self.state.log(f"#{issue.number}: could not inline the issue text ({e})")
        if rec.note:
            body += f"\n## Note from the supervisor\n\n{rec.note}\n"
        return body

    def launch(self, rec: IssueRecord, issue: Issue, model: str, overflow: str = "", cooling: str = "") -> None:
        name, mid = agents.split_model(model)
        agent = agents.get(name, self.cfg.agents.options.get(name))
        branch = rec.branch or self.trees.branch_for(issue.number, issue.title, issue.labels)
        tree = self.trees.prepare(issue.number, branch)
        early_on = self.early.build(issue.number, tree)  # [] unless it starts early; NoEarlyStart on a conflict
        data_dir = self.state.agent_data / str(issue.number)
        data_dir.mkdir(parents=True, exist_ok=True)
        # Credentials the agent needs in its private data dir (opencode keeps
        # them under <data>/opencode/).
        for f in self.cfg.agents.copy_files:
            src = Path(f).expanduser()
            dst = data_dir / ("opencode" if name == "opencode" else "") / src.name
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        lane_change = self.settle_lane(rec, issue)
        resume = rec.resume and not rec.fresh and model == rec.model and rec.started_at > 0
        if resume:
            text = agents.RESUME_PROMPT.format(why=rec.note or "restarted")
            if rec.note:
                text += f"\n\nNote from the supervisor: {rec.note}"
            if lane_change:
                text += self._lane_change_note(rec, issue, branch, tree, lane_change)
        else:
            text = self.render_prompt(rec, issue, branch, tree)
        text += self.early.brief(rec, early_on)
        title = f"{self.cfg.agents.title_prefix} #{issue.number}"
        spec = agent.launch(mid, text, title=title, resume=resume, data_dir=data_dir)
        env = self.agent_env(issue.number, spec)

        log = self.state.issue_log(issue.number)
        self._rotate(log)
        with open(log, "ab") as out:
            out.write(f"\n--- nohalt launch {time.strftime('%F %T')} model={model} resume={resume} ---\n".encode())
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
        self._procs[issue.number] = proc
        pids = self.state.load("pids.json", {})
        pids[str(issue.number)] = _pid_start(proc.pid)
        self.state.save("pids.json", pids)

        rec.status, rec.pid, rec.model, rec.started_at = RUNNING, proc.pid, model, now()
        rec.branch, rec.worktree, rec.log = branch, str(tree), str(log)
        rec.resume = rec.fresh = False
        rec.note = ""
        rec.last_exit = ""
        rec.stalled_since = 0
        rec.hold_model, rec.down_waiting = "", 0.0
        rec.overflow = overflow  # not a pin: the next launch is routed as usual (fail-back)
        rec.pin_cooling = cooling
        warned = f" -- pinned; {cooling}, a warning: pinned issues still start there" if cooling else ""
        if early_on:
            rec.early_on, rec.early_refused = early_on, []
            rec.event("started early on " + ", ".join(f"#{u['issue']} ({u['sha'][:8]})" for u in early_on))
        rec.event(f"launched on {model}" + (" (resumed)" if resume else "") + (f" -- {overflow}" if overflow else "") + warned)
        self.state.put(rec)
        self.say(
            f"#{issue.number}: agent launched on {model} in {tree} ({branch})"
            + (" -- resumed" if resume else "")
            + (f" -- {overflow}" if overflow else "")
            + warned
        )

    def _lane_change_note(self, rec: IssueRecord, issue: Issue, branch: str, tree: Path, change: str) -> str:
        """A resumed session was briefed in its old lane: give it the new instructions in full."""
        return (
            f"\n\n## Your instructions changed\n\nThe {change.replace('prompt lane', 'lane of this issue', 1)}. "
            "The instructions below replace the ones you were given when you started; where they differ, they win.\n\n"
            + self.compose_prompt(rec, issue, branch, tree)
        )

    def agent_env(self, issue: int, spec: agents.Launch) -> dict[str, str]:
        """The environment of an agent (or reviewer) on an issue: guard shims first on PATH."""
        env = dict(os.environ)
        for k in spec.unset_env:
            env.pop(k, None)
        # The real binaries are resolved BEFORE the shim directory goes first
        # on PATH, or a shim can find itself and loop forever.
        env.update(
            {
                "NOHALT_REAL_GH": shims.real("gh", env.get("PATH")),
                "NOHALT_REAL_GIT": shims.real("git", env.get("PATH")),
                "NOHALT_REAL_GLAB": shims.real("glab", env.get("PATH")),
                "NOHALT_GUARD_LOG": str(self.state.logs_dir / "guard.log"),
                "NOHALT_SHARED_TARGETS": shims.shared_targets(self.cfg.repo_path, self.cfg.project.shared_paths),
                "NOHALT_BASE_BRANCH": self.cfg.project.base_branch,
                "NOHALT_ISSUE": str(issue),
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        if self.cfg.project.forge == "gitlab" and self.cfg.project.forge_url:
            env.setdefault("GITLAB_HOST", self.cfg.project.forge_url)
        env.update(spec.env)
        env["PATH"] = f"{shims.install(self.state.bin_dir)}:{env.get('PATH', '')}"
        return env

    def _rotate(self, log: Path) -> None:
        try:
            if log.stat().st_size > self.cfg.agents.log_max_mb * 1024 * 1024:
                os.replace(log, log.with_suffix(".log.1"))
        except OSError:
            pass

    # -- capacity: endpoint-down state, usage cool-downs, pause expiry (capacity.py) ---------------------------
    def _pause_expiry(self) -> None:
        """``nohalt pause --for``: an expired PAUSE is cleared, and says so."""
        until = pause_until(self.state)
        if until and now() >= until and self.state.has("PAUSE"):
            set_at = self.state.read_lines("PAUSE")[:1]
            self.state.clear_flag("PAUSE")
            self.say(f"=== PAUSE expired (set {set_at[0] if set_at else '?'}, until {stamp(until)}); launching resumes")

    def _capacity_tick(self) -> None:
        """Read running agents' usage lines, end expired cool-downs, probe down models that are due."""
        if self.dry_run:
            return
        for rec in self.state.all():
            agent = self._agent(rec.model) if rec.status == RUNNING and rec.log else None
            if agent is None:
                continue
            sig = agent.usage_signal(tail(Path(rec.log), 6000).rsplit("\n--- nohalt launch ", 1)[-1])
            if sig:
                self.capacity.usage(rec.model, sig, f"#{rec.issue}", launch=rec.started_at)
        self.router.prober.reprobe_down()

    def _wanted(self, rec: IssueRecord, issue: Issue) -> tuple[str, bool]:
        """(model, held): routing, except that an issue stopped by a usage limit waits for its model (MOD-07)."""
        if rec.hold_model and not self.router.pinned(rec.issue):
            return rec.hold_model, True
        return self.router.wanted(issue)

    def _usage_limited(self, rec: IssueRecord, agent: agents.Agent, log_tail: str) -> None:
        """The agent stopped on its plan's usage limit: cool the pool down; this issue waits and resumes there."""
        self.router.prober.mark_limited(rec.model)
        sig = agent.usage_signal(log_tail) or agents.UsageSignal("limit")
        self.capacity.usage(rec.model, sig, f"#{rec.issue}", launch=rec.started_at)
        until = self.capacity.cooling_until(rec.model)
        rec.hold_model = rec.model
        window = f" ({sig.window})" if sig.window else ""
        self._requeue(
            rec,
            f"the plan's usage limit was reached{window}; resumes on {rec.model} once it resets",
            resume=True,
            delay=max(60.0, until - now()) if until else 900,
        )

    def _endpoint_down(self, rec: IssueRecord) -> bool:
        """An endpoint error on a model that is down (or just went down): not counted, for anyone in the storm."""
        refund = self.capacity.endpoint_error(rec.model, rec.issue)
        if refund is None:
            return False
        for n in refund:
            r = rec if n == rec.issue else self.state.get(n)
            r.provider_retries = max(0, r.provider_retries - 1)
            r.down_waiting = r.down_waiting or now()
            if r is not rec:
                r.event(f"endpoint error not counted after all: {rec.model} went down")
                self.state.put(r)
        rec.down_waiting = rec.down_waiting or now()
        self._requeue(
            rec,
            f"model endpoint error; {self.capacity.unavailable(rec.model)} -- not counted as a retry",
            resume=True,
            delay=self.cfg.agents.provider_backoff,
        )
        return True

    def _pin_cooling(self, model: str, warned_ok: bool) -> str:
        """The warning cool-down a pinned launch on ``model`` goes through, or "" (#81)."""
        return self.capacity.unavailable(model) if warned_ok else ""

    def _divert(self, issue: Issue, model: str, pinned: bool, why: str, in_use: dict[str, int]) -> tuple[str | None, str]:
        """The routed model is down or cooling down: a fallback or overflow target, now (not a pin)."""
        chosen = self.router.divert(issue, lambda m: self._has_slot(m, in_use, reserve=True), want=(model, pinned))
        return chosen, (f"capacity: {why} → {chosen}" if chosen else "")

    def _down_block(self, rec: IssueRecord, model: str, pinned: bool) -> bool:
        """Endpoint failures block an issue only once its model and every fallback were down ``down_block_after``."""
        after = self.cfg.models.down_block_after
        chain = [model] if pinned else [model, *self.cfg.models.fallbacks]
        if not rec.down_waiting or not self.capacity.all_down_for(chain, after):
            return False
        from .capacity import ago

        self._block(
            rec,
            f"every model it may run on ({', '.join(chain)}) has been down for over {ago(after)}; "
            "this is the model endpoints, not the issue -- `nohalt status` shows what is down",
        )
        return True

    # -- 5. budget -------------------------------------------------------------------------------------------
    def _budget(self) -> None:
        if self.state.flag_age("budget-checked") is not None and (self.state.flag_age("budget-checked") or 0) < 180:
            return
        self.state.set_flag("budget-checked")
        on_api = [r.issue for r in self.state.all() if r.status == RUNNING and budget.is_paid(self.cfg, r.model)]
        if not on_api:
            return
        _, _total, hand = budget.assess(self.cfg, self.state, on_api)
        fallback = self.cfg.budget.fallback_model
        for issue, why in hand:
            rec = self.state.get(issue)
            if fallback:
                self.router.pin(issue, fallback)
            self.say(f"#{issue}: budget -- {why}; " + (f"moving to {fallback}" if fallback else "stopping (no fallback model)"))
            if fallback:
                self._kill(rec, "handover", why)
            else:
                self._kill(rec, "stopped", why)
                self._block(self.state.get(issue), f"budget: {why}")

    # -- status ------------------------------------------------------------------------------------------------
    def _snapshot(self, report: TickReport) -> None:
        self.state.save(
            "status.json",
            {
                "at": now(),
                "running": report.running,
                "launched": report.launched,
                "finished": report.finished,
                "waiting": report.waiting,
                "blocked": report.blocked,
                "deps": {str(k): v for k, v in report.deps.items()},
                "open": report.open_,
                "complete": report.complete,
                "forge_calls": getattr(self.forge, "calls", 0),
            },
        )

    # -- control --------------------------------------------------------------------------------------------------
    def stop_now(self) -> list[int]:
        stopped = []
        for rec in self.state.all():
            if rec.status == RUNNING and self._alive(rec):
                self._kill(rec, "stopped", "stop requested")
                stopped.append(rec.issue)
        self.reviews.stop_all()  # reviewers resume (from scratch) on the next run
        return stopped
