"""``nohalt`` -- the command line."""

from __future__ import annotations

import argparse
import itertools
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from . import config as config_mod
from . import queue as queue_mod
from .config import Config, ConfigError
from .state import AWAITING_GREEN, BLOCKED, IN_REVIEW, QUEUED, RUNNING, FileLock, StateStore
from .util import now, parse_duration, pid_alive, stamp, tail

if TYPE_CHECKING:
    from .supervisor import Supervisor


def _cfg(args: argparse.Namespace) -> Config:
    return config_mod.load(args.config)


def _sup(cfg: Config, quiet: bool = False) -> Supervisor:
    from .supervisor import Supervisor

    return Supervisor(cfg, say=None if quiet else (lambda s: print(f"{stamp()}  {s}", flush=True)))


# -- commands ----------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir).resolve() / config_mod.CONFIG_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    text = resources.files("nohalt").joinpath(f"templates/{args.stack}.toml").read_text()
    repo, forge, url = _guess_repo(Path(args.dir))
    repo = args.repo or repo
    forge = args.forge or forge
    text = text.replace("OWNER/REPO", repo or "OWNER/REPO")
    if forge == "gitlab":
        text = text.replace('# forge = "github"', 'forge = "gitlab"')
        if url:
            text = text.replace('# forge_url = ""', f'forge_url = "{url}"')
    target.write_text(text)
    q = target.parent / "queue.txt"
    if not q.exists():
        q.write_text(
            "# nohalt queue: <issue>: <issues that must be CLOSED first>\n"
            "# Order is preference; dependencies are law. Edit freely while nohalt runs.\n"
        )
    gi = target.parent / ".gitignore"
    if gi.exists() and ".nohalt/" not in gi.read_text():
        with open(gi, "a") as f:
            f.write("\n.nohalt/\n")
    print(f"wrote {target} and {q}")
    print("next: edit both, then `nohalt doctor`, then `nohalt run --once --dry-run`")
    return 0


def _guess_repo(d: Path) -> tuple[str, str, str]:
    """(repo, forge, forge_url) from the origin remote."""
    cp = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"], capture_output=True, text=True)
    url = cp.stdout.strip()
    m = re.match(r"^(?:https?://|ssh://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", url)
    if not m:
        return "", "github", ""
    host, path = m.group(1), m.group(2)
    if host == "github.com":
        return path, "github", ""
    if "gitlab" in host:
        return path, "gitlab", "" if host == "gitlab.com" else f"https://{host}"
    return path, "github", ""


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    sup = _sup(cfg)
    if args.dry_run:
        sup.dry_run = True
        report = sup.tick()
        print(f"[dry run] open: {report.open_}")
        print(f"[dry run] would launch now: {' '.join('#' + str(i) for i in report.launched) or 'none'}")
        print(f"[dry run] would adopt (already have a PR): {' '.join(report.adopted) or 'none'}")
        print(f"[dry run] waiting: {' '.join(report.waiting) or 'none'}")
        print(f"[dry run] blocked: {' '.join('#' + str(i) for i in report.blocked) or 'none'}")
        return 0
    return sup.run(once=args.once, exit_when_done=args.exit_when_done)


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    state = StateStore(cfg.state_dir)
    state.set_flag("STOP", stamp())
    state.set_flag("WAKE")
    if args.now:
        stopped = _sup(cfg, quiet=True).stop_now()
        print(f"stopped agents: {' '.join('#' + str(i) for i in stopped) or 'none'} (they resume on the next run)")
    print("STOP set -- nohalt is DRAINING, not restarting:")
    print("  - no new agents launch, from now on")
    print(
        "  - the supervisor exits once no agent is running"
        + ("" if args.now else "; running agents finish their issue first (--now stops them)")
    )
    print("  - `nohalt keepalive` starts no supervisor while STOP is set")
    print("undo: `nohalt resume`.  To apply a config change or restart instead: `nohalt restart` (agents keep running)")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    state = StateStore(_cfg(args).state_dir)
    if args.off:
        state.clear_flag("PAUSE")
        state.set_flag("WAKE")
        print("launching resumed")
        return 0
    if args.for_:
        try:
            secs = parse_duration(args.for_)
        except ValueError as e:
            print(f"nohalt: --for: {e}", file=sys.stderr)
            return 2
        if secs <= 0:
            print("nohalt: --for must be longer than 0", file=sys.stderr)
            return 2
        until = now() + secs
        state.set_flag("PAUSE", f"{stamp()}\nuntil={int(until)}\n")
        print(f"no new agents will launch until {stamp(until)} (running ones continue); `nohalt resume` undoes it sooner")
    else:
        state.set_flag("PAUSE", f"{stamp()}\n")
        print("no new agents will launch (running ones continue), with no expiry; `nohalt resume` to undo")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    state = StateStore(cfg.state_dir)
    cleared = [f for f in ("STOP", "PAUSE") if state.has(f)]
    for f in cleared:
        state.clear_flag(f)
    state.set_flag("WAKE")
    print(f"cleared {' '.join(cleared)}: launching resumes" if cleared else "neither STOP nor PAUSE was set")
    if not _supervisor_up(state):
        print("the supervisor is not running: `nohalt restart` (or `nohalt keepalive`) starts one")
    return 0


def cmd_release_now(args: argparse.Namespace) -> int:
    from .integrate.train import RELEASE_NOW

    cfg = _cfg(args)
    state = StateStore(cfg.state_dir)
    if args.off:
        print("release-now request withdrawn" if state.has(RELEASE_NOW) else "no release-now request was pending")
        state.clear_flag(RELEASE_NOW)
        return 0
    if cfg.integrate.mode != "train":
        print(f"nohalt: release-now is for the release train; [integrate] mode is {cfg.integrate.mode!r}", file=sys.stderr)
        return 2
    state.set_flag(RELEASE_NOW, stamp())
    state.set_flag("WAKE")
    print("RELEASE_NOW set: once no release is open and the base is green, the next tick opens one normal batch")
    print("  - release windows and their quota are ignored for that one batch; BATCH_PAUSE and a red base are not")
    print("  - the request is used once; with nothing ready it is dropped (`release-now --off` withdraws it)")
    if not cfg.integrate.train.windows:
        print("  - no [integrate.train] windows are set, so the train releases on its own anyway")
    if not _supervisor_up(state):
        print("the supervisor is not running: `nohalt restart` (or `nohalt keepalive`) starts one")
    return 0


def _windows_status(cfg: Config, state: StateStore) -> list[str]:
    """The release schedule (DEP-13): the window now or next, and what waits on it."""
    from .integrate.windows import Schedule, duration

    t = cfg.integrate.train
    if cfg.integrate.mode != "train" or not t.windows:
        return []
    sched = Schedule(t.windows, t.timezone, t.days)
    ts = now()
    zone = sched.label(ts)
    lines = []
    cur = sched.current(ts)
    st = state.load("train.json", {}) or {}
    if cur:
        used = (st.get("windows_used") or {}).get(cur.key, 0)
        quota = "unlimited" if t.per_window == 0 else f"{used}/{t.per_window} released"
        lines.append(f"release window open: {cur.window.label} {zone}, closes in {duration(cur.end.timestamp() - ts)} ({quota})")
    nxt = sched.next(ts)
    if nxt:
        lines.append(f"next release window: {nxt.window.label} {zone}, in {duration(nxt.start.timestamp() - ts)}")
    w = st.get("waiting")
    if w:
        hot = " ".join(f"#{i}" for i in w.get("hotfixes", []))
        lines.append(
            f"  as of {stamp(w.get('at'))}: {w.get('why', '')}; {len(w.get('ready', []))} ready, "
            f"hotfixes waiting: {hot or 'none'}"
        )
    return lines


def _pause_detail(state: StateStore) -> str:
    from .capacity import ago
    from .state import pause_until

    until = pause_until(state)
    if until:
        return f" -- expires {stamp(until)}, in {ago(until - now())}"
    age = state.flag_age("PAUSE")
    return f" -- paused for {ago(age)}, no expiry" if age is not None else ""


def _capacity_status(state: StateStore) -> list[str]:
    """Down models and cooling pools (capacity.py): where new work is not going, and until when."""
    from .capacity import FILE, ago

    snap = state.load(FILE, {}) or {}
    lines = []
    t = now()
    for model, v in sorted((snap.get("down") or {}).items()):
        nxt = v.get("next_probe", 0)
        lines.append(
            f"model DOWN: {model} since {stamp(v.get('since'))} ({ago(t - v.get('since', t))}; {v.get('why', '')}); "
            f"next probe {'due' if nxt <= t else 'in ' + ago(nxt - t)}"
        )
    for pool, c in sorted((snap.get("cooling") or {}).items()):
        if c.get("until", 0) <= t:
            continue
        window = f" {c['window']}" if c.get("window") else ""
        what = "usage limit" if c.get("kind") == "limit" else "near its usage limit"
        reset = "reset" if c.get("reported") else "default cool-down, no reset reported"
        lines.append(
            f"pool cooling down: {pool}{window} {what} ({c.get('source', '?')}) until {stamp(c['until'])} "
            f"(in {ago(c['until'] - t)}; {reset}); new work goes to fallbacks, pinned issues wait"
        )
    return lines


def _live_status(cfg: Config, state: StateStore) -> list[str]:
    """A post-deploy check still retrying, and released issues not yet live (DEP-14, DEP-15)."""
    from .integrate.live import awaiting, label

    lines = []
    pc = (state.load("integrate.json", {}) or {}).get("post_check")
    if pc:
        lines.append(f"post-deploy check on {label(pc.get('ctx') or {})}: retrying (failed {pc.get('tries', 0)} time(s))")
    if cfg.integrate.live_version:
        timeout = cfg.integrate.live_timeout or cfg.integrate.cd_timeout
        lines.extend(awaiting(state.load("live.json", {}) or {}, now(), timeout))
    return lines


def _supervisor_up(state: StateStore) -> bool:
    lock = FileLock(state.root / "supervisor.lock")
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def _start_detached(cfg: Config, state: StateStore) -> int:
    """Start ``nohalt run`` in its own session, owned by no terminal (LIV-02). Returns its pid."""
    argv = [sys.executable, "-m", "nohalt"]
    if cfg.source:
        argv += ["--config", str(cfg.source)]
    argv.append("run")
    with open(state.logs_dir / "supervisor.out", "ab") as log:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=cfg.root,
        )
    return proc.pid


def _wait(cond: Callable[[], bool], timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


def cmd_restart(args: argparse.Namespace) -> int:
    """Restart the supervisor now, with the configuration as it is on disk. Agents keep running."""
    cfg = _cfg(args)  # an invalid config fails here, before the running supervisor is touched
    state = StateStore(cfg.state_dir)
    if state.has("STOP"):
        state.clear_flag("STOP")
        print("cleared STOP (it drains; a restart is meant to keep going)")
    running = sum(1 for r in state.all() if r.status == RUNNING and pid_alive(r.pid))
    agents = f"{running} running agent(s) were not touched and are adopted"
    lock = FileLock(state.root / "supervisor.lock")
    if not _supervisor_up(state):
        pid = _start_detached(cfg, state)
        state.log("restart: no supervisor was running; started one")
        print(f"no supervisor was running; started one (pid {pid}); {agents}")
        return 0
    holder = lock.held_by()
    asked = now()
    state.set_flag("RESTART", stamp())
    state.set_flag("WAKE")
    print(f"asked the supervisor (pid {holder}) to restart; it does so after its current tick ...", flush=True)

    def restarted() -> bool:
        return (state.load("supervisor.json", {}) or {}).get("started", 0) >= asked and not state.has("RESTART")

    if _wait(restarted, args.timeout):
        pid = (state.load("supervisor.json", {}) or {}).get("pid", "?")
        print(f"supervisor restarted (pid {pid}) on {cfg.source}; {agents}")
        return 0
    print(f"no restart after {args.timeout} s; sending SIGTERM to pid {holder} (agents are not signalled)", flush=True)
    state.clear_flag("RESTART")
    try:
        os.kill(holder, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        print(f"nohalt: cannot signal pid {holder}: {e}", file=sys.stderr)
        return 1
    if not _wait(lambda: not _supervisor_up(state), args.timeout):
        print(f"nohalt: pid {holder} still holds the supervisor lock; look at it (`nohalt status`)", file=sys.stderr)
        return 1
    pid = _start_detached(cfg, state)
    state.log("restart: the old supervisor exited on SIGTERM; started a new one")
    print(f"old supervisor exited; started a new one (pid {pid}); {agents}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    while True:
        out = render_status(cfg)
        if args.watch:
            print("\x1b[2J\x1b[H" + out, flush=True)
            time.sleep(args.watch)
        else:
            print(out)
            return 0


# Control flags `nohalt status` shows, and what each one means.
FLAGS = {
    "STOP": "draining -- no new launches; the supervisor exits when no agent runs (`nohalt resume` undoes)",
    "PAUSE": "no new launches; running agents continue (`nohalt resume` undoes; `pause --for` expires)",
    "RESTART": "a restart was asked for; the supervisor re-executes after its current tick",
    "CONFIG_INVALID": "nohalt.toml has an error; the supervisor runs on the last good configuration",
    "DEPLOY_BLOCKED": "a deploy or its post-deploy check failed; nothing merges (but the fix for a failed check) until "
    "a human deletes the flag or a deploy passes the check",
    "BASE_NO_VERDICT": "the base pipeline gave no verdict; merging is held until a human deletes the flag",
    "BUDGET_EXHAUSTED": "the paid-API budget is spent; paid issues go to the fallback model, or stop",
    "RELEASE_NOW": "a release-now request: the train's next batch ignores the release windows (`release-now --off` withdraws)",
}


def render_status(cfg: Config) -> str:
    state = StateStore(cfg.state_dir)
    lines = []
    sup_lock = FileLock(state.root / "supervisor.lock")
    holder = sup_lock.held_by()
    up = holder and pid_alive(holder)
    flags = [f for f in FLAGS if state.has(f)]
    mem = ""
    try:
        from .util import mem_available_mb

        m = mem_available_mb()
        mem = f"   memory {m} MB free" if m is not None else ""
    except OSError:
        pass
    lines.append(
        f"nohalt {__version__}  {cfg.project.repo}  supervisor: {'up (pid ' + str(holder) + ')' if up else 'DOWN'}"
        f"{mem}{'   flags: ' + ' '.join(flags) if flags else ''}"
    )
    for f in flags:
        detail = ""
        if f in ("CONFIG_INVALID", "DEPLOY_BLOCKED"):
            detail = " -- " + " ".join(state.read_lines(f))[:300]
        if f == "PAUSE":
            detail = _pause_detail(state)
        lines.append(f"  {f}: {FLAGS[f]}{detail}")
    lines.extend(_windows_status(cfg, state))
    lines.extend(_capacity_status(state))
    lines.extend(_live_status(cfg, state))
    spend = state.load("spend.json")
    if spend:
        lines.append(f"API spend ~${spend.get('total', 0):.2f} of ${spend.get('cap', 0):.2f}")
    lines.append("")
    lines.append(f"{'ISSUE':<8}{'STATUS':<15}{'MODEL':<34}{'IDLE':>7}  {'TRIES':<6}LAST")
    recs = [r for r in state.all() if r.status != "done"]
    for r in recs:
        idle = ""
        if r.status == RUNNING and r.log and Path(r.log).exists():
            idle = f"{int(now() - Path(r.log).stat().st_mtime)}s"
        last = ""
        if r.status == RUNNING and r.log:
            txt = [ln for ln in tail(Path(r.log), 3000).splitlines() if ln.strip()]
            last = txt[-1][:70] if txt else ""
        else:
            last = (r.history[-1].split("  ", 1)[-1] if r.history else "")[:70]
        tries = f"{r.attempts}/{cfg.agents.max_attempts}"
        overflowed = r.overflow and r.status not in (QUEUED, BLOCKED)  # the mark is for the launch that overflowed
        model = (r.model[:31] + " ^") if overflowed else r.model[:33]
        if r.status == IN_REVIEW:
            model = (r.review_model or "(reviewer)")[:33]
            rlog = state.logs_dir / f"review-{r.issue}.log"
            if r.review_pid and rlog.exists():
                idle = f"{int(now() - rlog.stat().st_mtime)}s"
                last = f"reviewing {r.review_sha[:8]}"
            else:
                last = "review waiting for a reviewer slot or model"
            tries = f"{r.review_rounds}/{cfg.review.max_rounds}"
        lines.append(f"#{r.issue:<7}{r.status:<15}{model:<34}{idle:>7}  {tries:<6}{last}")
    overflows = [r for r in recs if r.overflow and r.status not in (QUEUED, BLOCKED)]
    if overflows:
        lines.append("")
        lines.append("^ overflowed (not pinned; the next launch is routed as usual):")
        lines.extend(f"  #{r.issue}: {r.overflow}" for r in overflows)
    early = [r for r in recs if r.early_on]
    if early:
        lines.append("")
        lines.append(
            "started early (cannot integrate before these merge): "
            + "  ".join(f"#{r.issue} early on " + ",".join(f"#{u['issue']}" for u in r.early_on) for r in early[:30])
        )
    lanes = [r for r in recs if r.lane]
    if lanes:
        lines.append("")
        lines.append("lanes: " + "  ".join(f"#{r.issue} {r.lane}" for r in lanes[:30]))
    reviewed = [r for r in recs if r.review_verdict]
    if reviewed:
        lines.append("")
        lines.append(
            "reviews: "
            + "  ".join(
                f"#{r.issue} {r.review_verdict} on {r.review_sha[:8]} (round {r.review_rounds}/{cfg.review.max_rounds})"
                for r in reviewed[:15]
            )
        )
    snap = state.load("status.json", {})
    if snap:
        lines.append("")
        lines.append(f"waiting: {' '.join(snap.get('waiting', [])[:15]) or 'none'}")
        lines.extend(f"  #{n} {why}" for n, why in list(snap.get("deps", {}).items())[:15])
        lines.append(f"blocked (need a human): {' '.join('#' + str(i) for i in snap.get('blocked', [])) or 'none'}")
        awaiting = [r.issue for r in recs if r.status == AWAITING_GREEN]
        if awaiting or cfg.integrate.async_workflows:
            owner = {w.get("issue"): wf for wf, w in state.load("async.json", {}).items()}
            lines.append(
                "awaiting a green run: " + (" ".join(f"#{i}({owner[i]})" if i in owner else f"#{i}" for i in awaiting) or "none")
            )
        lines.append(
            f"last tick {stamp(snap.get('at'))}; open in queue: {snap.get('open', '?')}"
            + ("  -- COMPLETE" if snap.get("complete") else "")
        )
    return "\n".join(lines)


def cmd_verify(args: argparse.Namespace) -> int:
    from .verify import run_verify

    command = " ".join(args.command).strip()
    if not command:
        print("usage: nohalt verify <worktree> -- '<command>'", file=sys.stderr)
        return 2
    return run_verify(_cfg(args), Path(args.worktree), command)


def cmd_route(args: argparse.Namespace) -> int:
    from .models import Router

    cfg = _cfg(args)
    router = Router(cfg, StateStore(cfg.state_dir))
    if args.model == "-":
        router.unpin(args.issue)
        print(f"#{args.issue}: pin removed; routing rules apply")
    else:
        router.pin(args.issue, args.model, approve=args.approve)
        print(f"#{args.issue} -> {args.model}" + (" (approved for premium)" if args.approve else ""))
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    from . import forge as forge_mod

    cfg = _cfg(args)
    f = forge_mod.make(cfg.project)
    f.remove_label(args.issue, cfg.queue.blocked_label)
    StateStore(cfg.state_dir).set_flag("WAKE")
    print(f"#{args.issue}: removed {cfg.queue.blocked_label}; it is retried on the next tick")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.add:
        queue_mod.append(cfg.queue_file, args.add, args.after or [], front=args.front)
        print(f"queued #{args.add}" + (f" after {' '.join('#' + str(d) for d in args.after)}" if args.after else ""))
        return 0
    for e in queue_mod.load(cfg.queue_file):
        print(f"#{e.issue}" + (f"  <- {' '.join('#' + str(d) for d in e.deps)}" if e.deps else ""))
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    from . import prompt as prompt_mod
    from .state import IssueRecord

    cfg = _cfg(args)
    sup = _sup(cfg, quiet=True)
    issue = sup.issue(args.issue, fresh=True)
    branch = sup.trees.branch_for(issue.number, issue.title, issue.labels)
    # The lane the next launch would use: the record's, unless the lanes changed since it was chosen.
    # Nothing is saved: this only shows.
    rec = sup.state.get(args.issue) if sup.state.exists(args.issue) else IssueRecord(issue=args.issue)
    kept = rec.lane_digest == prompt_mod.lanes_digest(cfg)
    if not kept:
        lane = prompt_mod.select_lane(cfg, issue)
        rec.lane = lane.name if lane else ""
    how = "kept on the issue record" if kept else "would be chosen at the next launch"
    print(f"# lane: {rec.lane or '(none)'} -- {how}", file=sys.stderr)
    print(sup.render_prompt(IssueRecord(issue=args.issue, lane=rec.lane), issue, branch, sup.trees.path(args.issue)))
    return 0


def cmd_merge_driver(args: argparse.Namespace) -> int:
    from .integrate.mergers import DRIVERS

    if args.name not in DRIVERS:
        print(f"unknown driver {args.name!r} (known: {', '.join(DRIVERS)})", file=sys.stderr)
        return 2
    return 0 if DRIVERS[args.name](Path.cwd(), args.path) else 1


def cmd_keepalive(args: argparse.Namespace) -> int:
    """Start the supervisor if it is not running. Safe to call from cron every few minutes.

    Daemons owned by an interactive session die with it; owned by cron, they
    (and the agents they start) outlive any terminal.
    """
    cfg = _cfg(args)
    state = StateStore(cfg.state_dir)
    lock = FileLock(state.root / "supervisor.lock")
    if not lock.acquire(blocking=False):
        return 0
    lock.release()
    if state.has("STOP"):
        return 0
    _start_detached(cfg, state)
    state.log("keepalive started the supervisor")
    print("supervisor started")
    return 0


def _doctor_check_drift(cfg: Config, check: Callable[..., None]) -> None:
    """Compare required_checks with the checks CI really ran on recent commits."""
    from . import forge as forge_mod
    from .forge.base import check_drift

    try:
        f = forge_mod.make(cfg.project)
        shas = [f.branch_sha(cfg.project.base_branch)] + [p.head_sha for p in f.open_pulls()[:5]]
        seen = {c.name for sha in shas if sha for c in f.checks(sha)}
    except forge_mod.ForgeError as e:
        check(False, "", f"could not read recent check runs to compare with required_checks: {e}", fatal=False)
        return
    missing, uncounted = check_drift(seen, cfg.integrate.required_checks, cfg.integrate.ignore_checks)
    check(
        not missing,
        "every required check has run recently",
        f"required check(s) never seen on recent commits (misspelt? a PR would wait forever): {', '.join(missing)}",
        fatal=False,
    )
    check(
        not uncounted,
        "every check CI runs is required or ignored",
        f"check(s) CI runs that count for nothing -- red would go unnoticed (add to required_checks or ignore_checks): "
        f"{', '.join(uncounted)}",
        fatal=False,
    )


def _doctor_review_authors(cfg: Config, check: Callable[..., None]) -> None:
    """With ``[review] distinct``, an author model no reviewer differs from is never reviewed (its issues block)."""
    from .review import reviewer_models

    m = cfg.models
    authors = sorted({m.default, *m.fallbacks, *m.overflow_to, *(r.model for r in m.route)})
    alone = [a for a in authors if not reviewer_models(cfg, a)]
    check(
        not alone,
        "every author model has a reviewer on another model",
        f"[review] distinct: nothing may review work written on {', '.join(alone)} (the only reviewer is the same "
        "model); add [review] models or fallbacks, or set distinct = false",
        fatal=False,
    )


def _doctor_deploy(cfg: Config, check: Callable[..., None], probe: bool) -> None:
    """A deploy judged by its exit code alone, and live_version where nothing would read it (DEP-14, DEP-15)."""
    ic = cfg.integrate
    if ic.deploy or ic.cd_workflow:
        check(
            bool(ic.post_deploy_check),
            "post_deploy_check runs after each deploy",
            ("[integrate] deploy" if ic.deploy else "[integrate] cd_workflow")
            + " is set but post_deploy_check is not: a deploy is judged only by its exit code, so a release that "
            "breaks the live app looks done (add a command that checks the main pages)",
            fatal=False,
        )
    elif ic.post_deploy_check:
        check(
            False,
            "",
            "[integrate] post_deploy_check is set but nothing deploys (no deploy, no cd_workflow): it never runs",
            fatal=False,
        )
    if ic.live_version and ic.mode == "none":
        check(
            False, "", "[integrate] live_version needs mode direct or train: with a human merging, nothing reads it", fatal=False
        )
    if ic.live_version and probe:
        cp = subprocess.run(["bash", "-c", ic.live_version], cwd=cfg.repo_path, capture_output=True, text=True, timeout=120)
        out = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
        check(
            cp.returncode == 0 and bool(out),
            f"live_version reports {out[-1] if out else ''!r}",
            f"live_version exited {cp.returncode}" + (f" printing {out[-1]!r}" if out else " with no output"),
            fatal=False,
        )


def _doctor_windows(cfg: Config, check: Callable[..., None]) -> None:
    """``[integrate.train] windows``, ``timezone`` and ``days`` parse at load; here: do they do what they seem to (DEP-13)."""
    from .integrate.windows import DAYS, Schedule, duration

    t = cfg.integrate.train
    if not (t.windows or t.timezone or t.days):
        return
    if not t.windows:
        check(
            False,
            "",
            "[integrate.train] timezone or days is set but windows is empty: the train releases at any time",
            fatal=False,
        )
        return
    if cfg.integrate.mode != "train":
        check(
            False,
            "",
            f"[integrate.train] windows is set but mode is {cfg.integrate.mode!r}: only the train uses them",
            fatal=False,
        )
        return
    sched = Schedule(t.windows, t.timezone, t.days)
    ts = now()
    days = ", ".join(DAYS[d] for d in sorted(sched.days)) if t.days else "every day"
    nxt = sched.next(ts)
    when = f"; next {nxt.window.label} in {duration(nxt.start.timestamp() - ts)}" if nxt else ""
    check(
        nxt is not None or sched.current(ts) is not None,
        f"release windows {', '.join(w.label for w in sched.windows)} {sched.label(ts)}, {days}{when}",
        "[integrate.train] windows never open (no day in days has one): normal releases never happen",
        fatal=False,
    )
    check(
        bool(t.timezone),
        "release windows have a time zone",
        f"[integrate.train] timezone is not set: windows follow this host's clock ({sched.label(ts)}); set an IANA zone "
        "so a host in another zone, or in UTC, does not move them",
        fatal=False,
    )
    starts = sorted(w.start for w in sched.windows)
    ends = sorted((w.start, w.start + ((w.end - w.start) % 1440)) for w in sched.windows)
    overlap = any(b[0] < a[1] for a, b in itertools.pairwise(ends)) or (len(ends) > 1 and ends[-1][1] > 1440 + ends[0][0])
    check(
        not overlap and len(set(starts)) == len(starts),
        "release windows do not overlap",
        "[integrate.train] windows overlap: each counts its own per_window quota, so more releases go out than it looks",
        fatal=False,
    )
    check(
        bool(t.hotfix_labels) or t.hotfix_priority,
        "a hotfix lane exists outside the windows",
        "[integrate.train] no hotfix_labels and hotfix_priority = false: only a red base's fix may release outside a window",
        fatal=False,
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True

    def check(cond: bool, good: str, bad: str, fatal: bool = True) -> None:
        nonlocal ok
        print(("  ok   " if cond else ("  FAIL " if fatal else "  warn ")) + (good if cond else bad))
        if not cond and fatal:
            ok = False

    try:
        cfg = _cfg(args)
    except ConfigError as e:
        print(f"  FAIL config: {e}")
        return 1
    print(f"config {cfg.source}")
    from . import agents, stacks

    try:
        stacks.get(cfg)
        check(True, f"stack {cfg.stack.name}", "")
    except ConfigError as e:
        check(False, "", f"stack: {e}")
    check(
        bool(cfg.project.repo) and "OWNER/REPO" not in cfg.project.repo, f"repo {cfg.project.repo}", "[project] repo is not set"
    )
    check((cfg.repo_path / ".git").exists(), f"clone at {cfg.repo_path}", f"no git clone at {cfg.repo_path}")
    try:
        entries = queue_mod.load(cfg.queue_file)
        check(True, f"queue {cfg.queue_file} ({len(entries)} issues)", "")
    except queue_mod.QueueError as e:
        check(False, "", f"queue: {e}", fatal=False)
    for tool in ("git", "bash"):
        check(shutil.which(tool) is not None, f"{tool} found", f"{tool} not on PATH")
    if cfg.project.repo.startswith("local:"):
        pass
    elif cfg.project.forge == "gitlab":
        from .forge.gitlab import GitLab

        gl = GitLab(cfg.project.repo, url=cfg.project.forge_url, token_env=cfg.project.token_env)
        check(
            bool(gl.token),
            f"GitLab token found for {gl.url}",
            f"no GitLab token: set ${cfg.project.token_env} (scope: api) or `glab auth login`",
        )
        check(
            not gl.url.startswith("http://"),
            f"{gl.url} uses TLS",
            f"{gl.url} is plain HTTP: the token travels unencrypted on every call",
            fatal=False,
        )
        proj = gl.project()
        check(
            proj is not None,
            f"project {cfg.project.repo} reachable on {gl.url}",
            f"cannot read project {cfg.project.repo} on {gl.url}",
        )
        broad = sorted(set(gl.token_scopes()) & {"sudo", "admin_mode", "manage_runner", "create_runner", "k8s_proxy"})
        check(
            not broad,
            "token scopes are narrow",
            f"the token has broad scopes ({', '.join(broad)}): give agents a project access token (Developer role, api scope)",
            fatal=False,
        )
        check(
            gl.protected(cfg.project.base_branch),
            f"{cfg.project.base_branch} is a protected branch",
            f"{cfg.project.base_branch} is not protected: the guard shims are speed bumps; protection is the wall "
            "(docs/SAFETY.md)",
            fatal=False,
        )
        if proj and cfg.integrate.mode != "none" and proj.get("default_branch") == cfg.project.base_branch:
            check(
                False,
                "",
                f"nohalt will merge into the default branch ({cfg.project.base_branch}): if a pipeline deploys from it, "
                "every merge is an unattended deployment (docs/SAFETY.md)",
                fatal=False,
            )
        check(
            shutil.which("glab") is not None,
            "glab found (agents use it to read issues and open merge requests)",
            "glab (GitLab CLI) not on PATH: set [prompt] inline_issue = true; agents can open MRs with git push options",
            fatal=False,
        )
    else:
        check(shutil.which("gh") is not None, "gh found", "gh (GitHub CLI) not on PATH")
        cp = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        check(cp.returncode == 0, "gh is authenticated", "gh is not authenticated (`gh auth login`)")
        prot = subprocess.run(
            ["gh", "api", f"repos/{cfg.project.repo}/branches/{cfg.project.base_branch}/protection"],
            capture_output=True,
            text=True,
        )
        check(
            prot.returncode == 0,
            f"branch protection on {cfg.project.base_branch}",
            f"no branch protection on {cfg.project.base_branch}: the guard shims are speed bumps; protection is the "
            "wall (docs/SAFETY.md)",
            fatal=False,
        )
    models = {cfg.models.default, *cfg.models.fallbacks, *(r.model for r in cfg.models.route)}
    for m in sorted(models):
        name, _ = agents.split_model(m)
        try:
            a = agents.get(name, cfg.agents.options.get(name))
            binary = getattr(a, "binary", "") or a.probe_launch("x", cfg.state_dir).argv[0]
            check(shutil.which(binary) is not None, f"{m}: {binary} found", f"{m}: {binary} not on PATH")
        except ValueError as e:
            check(False, "", f"{m}: {e}")
    if cfg.review.enabled and cfg.review.distinct:
        _doctor_review_authors(cfg, check)
    if args.probe:
        from .models import Prober

        p = Prober(cfg, StateStore(cfg.state_dir))
        for m in sorted(models):
            check(p._probe(m), f"{m} answers", f"{m} does not answer a probe", fatal=False)
    if cfg.integrate.required_checks:
        _doctor_check_drift(cfg, check)
    _doctor_deploy(cfg, check, args.probe)
    _doctor_windows(cfg, check)
    if cfg.integrate.mode != "none" and not cfg.integrate.required_checks and not cfg.integrate.ignore_checks:
        check(False, "", "[integrate] required_checks is empty: every check run must pass (fine if intended)", fatal=False)
    if cfg.state_dir.is_dir():
        from .integrate.refresh import HISTORY_DAYS, recurring_conflicts

        for path, n, hint in recurring_conflicts(cfg, StateStore(cfg.state_dir)):
            check(
                False,
                "",
                f"{path} was left in conflict at {n} hand-backs in the last {HISTORY_DAYS} days and has no merge driver: "
                f"if it is bookkeeping, map it in [integrate.mergers] -- suggested: {hint}",
                fatal=False,
            )
    print("ready" if ok else "not ready")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nohalt", description="Autonomous coding agents that keep going until the forge says done.")
    p.add_argument("--config", "-c", help="path to nohalt.toml (default: search upwards from the cwd)")
    p.add_argument("--version", action="version", version=f"nohalt {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a starter nohalt.toml and queue.txt")
    s.add_argument("--stack", choices=["generic", "frappe"], default="generic")
    s.add_argument("--repo", help="owner/name or group/name (default: from the origin remote)")
    s.add_argument("--forge", choices=["github", "gitlab"], help="default: from the origin remote")
    s.add_argument("--dir", default=".")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("run", help="supervise until stopped (or until done with --exit-when-done)")
    s.add_argument("--once", action="store_true", help="one tick, then exit")
    s.add_argument("--dry-run", action="store_true", help="show what would launch; change nothing")
    s.add_argument("--exit-when-done", action="store_true", help="exit when every queued issue is closed")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("stop", help="drain: launch nothing more, exit once no agent runs (`resume` undoes)")
    s.add_argument("--now", action="store_true", help="also stop running agents (they resume next run)")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("resume", help="clear STOP and PAUSE: launching resumes")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("restart", help="restart the supervisor now with the config on disk; agents keep running")
    s.add_argument("--timeout", type=float, default=120, metavar="SECONDS", help="wait this long for each step")
    s.set_defaults(fn=cmd_restart)

    s = sub.add_parser("pause", help="launch no new agents until `pause --off`, or for a while (--for 2h)")
    s.add_argument("--off", action="store_true")
    s.add_argument("--for", dest="for_", metavar="DURATION", help="expire after this long, e.g. 30m or 2h")
    s.set_defaults(fn=cmd_pause)

    s = sub.add_parser("release-now", help="train: open one normal batch now, ignoring the release windows")
    s.add_argument("--off", action="store_true", help="withdraw a pending request")
    s.set_defaults(fn=cmd_release_now)

    s = sub.add_parser("status", help="one screen of what is happening (local, no forge calls)")
    s.add_argument("--watch", "-w", type=int, nargs="?", const=10, default=0, metavar="SECONDS")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("verify", help="run a command inside the verification lock (agents call this)")
    s.add_argument("worktree")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("route", help="pin an issue to a model ('-' to unpin)")
    s.add_argument("issue", type=int)
    s.add_argument("model")
    s.add_argument("--approve", action="store_true", help="allow a premium model for this issue")
    s.set_defaults(fn=cmd_route)

    s = sub.add_parser("unblock", help="remove the blocked label so the issue is retried")
    s.add_argument("issue", type=int)
    s.set_defaults(fn=cmd_unblock)

    s = sub.add_parser("queue", help="show the queue, or add to it")
    s.add_argument("--add", type=int, metavar="ISSUE")
    s.add_argument("--after", type=int, nargs="*", metavar="DEP")
    s.add_argument("--front", action="store_true")
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("prompt", help="print the prompt an issue's agent would get")
    s.add_argument("issue", type=int)
    s.set_defaults(fn=cmd_prompt)

    s = sub.add_parser("doctor", help="check the configuration and tools")
    s.add_argument("--probe", action="store_true", help="also ask every configured model for a reply")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("keepalive", help="start the supervisor if it is down (for cron)")
    s.set_defaults(fn=cmd_keepalive)

    s = sub.add_parser("merge-driver", help="run a bookkeeping merge driver on a conflicted file")
    s.add_argument("name")
    s.add_argument("path")
    s.set_defaults(fn=cmd_merge_driver)

    args = p.parse_args(argv)
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        return int(args.fn(args) or 0)
    except ConfigError as e:
        print(f"nohalt: {e}", file=sys.stderr)
        return 2
    except queue_mod.QueueError as e:
        print(f"nohalt: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
