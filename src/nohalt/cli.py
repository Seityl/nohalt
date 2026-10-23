"""``nohalt`` -- the command line."""

from __future__ import annotations

import argparse
import os
import re
import shutil
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
from .state import RUNNING, FileLock, StateStore
from .util import now, pid_alive, stamp, tail

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
    print(
        "the supervisor stops launching and exits once no agent is running"
        + ("" if args.now else "; running agents finish their issue first (use --now to stop them)")
    )
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    state = StateStore(_cfg(args).state_dir)
    if args.off:
        state.clear_flag("PAUSE")
        print("launching resumed")
    else:
        state.set_flag("PAUSE", stamp())
        print("no new agents will launch (running ones continue); `nohalt pause --off` to resume")
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


def render_status(cfg: Config) -> str:
    state = StateStore(cfg.state_dir)
    lines = []
    sup_lock = FileLock(state.root / "supervisor.lock")
    holder = sup_lock.held_by()
    up = holder and pid_alive(holder)
    flags = [f for f in ("STOP", "PAUSE", "DEPLOY_BLOCKED", "BUDGET_EXHAUSTED") if state.has(f)]
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
    spend = state.load("spend.json")
    if spend:
        lines.append(f"API spend ~${spend.get('total', 0):.2f} of ${spend.get('cap', 0):.2f}")
    lines.append("")
    lines.append(f"{'ISSUE':<8}{'STATUS':<12}{'MODEL':<34}{'IDLE':>7}  {'TRIES':<6}LAST")
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
        lines.append(f"#{r.issue:<7}{r.status:<12}{r.model[:33]:<34}{idle:>7}  {tries:<6}{last}")
    snap = state.load("status.json", {})
    if snap:
        lines.append("")
        lines.append(f"waiting: {' '.join(snap.get('waiting', [])[:15]) or 'none'}")
        lines.append(f"blocked (need a human): {' '.join('#' + str(i) for i in snap.get('blocked', [])) or 'none'}")
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
    from .state import IssueRecord

    cfg = _cfg(args)
    sup = _sup(cfg, quiet=True)
    issue = sup.issue(args.issue, fresh=True)
    branch = sup.trees.branch_for(issue.number, issue.title, issue.labels)
    print(sup.render_prompt(IssueRecord(issue=args.issue), issue, branch, sup.trees.path(args.issue)))
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
    argv = [sys.executable, "-m", "nohalt"]
    if cfg.source:
        argv += ["--config", str(cfg.source)]
    argv.append("run")
    with open(state.logs_dir / "supervisor.out", "ab") as log:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=cfg.root,
        )
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
    if args.probe:
        from .models import Prober

        p = Prober(cfg, StateStore(cfg.state_dir))
        for m in sorted(models):
            check(p._probe(m), f"{m} answers", f"{m} does not answer a probe", fatal=False)
    if cfg.integrate.required_checks:
        _doctor_check_drift(cfg, check)
    if cfg.integrate.mode != "none" and not cfg.integrate.required_checks and not cfg.integrate.ignore_checks:
        check(False, "", "[integrate] required_checks is empty: every check run must pass (fine if intended)", fatal=False)
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

    s = sub.add_parser("stop", help="stop between issues (or now)")
    s.add_argument("--now", action="store_true", help="also stop running agents (they resume next run)")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("pause", help="launch no new agents until `pause --off`")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_pause)

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
