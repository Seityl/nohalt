"""What nohalt remembers between ticks and across restarts.

Everything is a small JSON file under the state directory, written atomically,
so the supervisor can be killed at any instant and pick up where it was. The
agents themselves do not depend on the supervisor being alive: they run in
their own sessions, and a restarted supervisor adopts them from their records.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from .util import now, read_json, stamp, write_json

# Issue lifecycle.
QUEUED = "queued"  # known, not yet launched (or waiting for a retry time)
RUNNING = "running"  # an agent process is alive
WAITING_CI = "waiting_ci"  # agent finished with a PR whose checks have not settled
IN_REVIEW = "in_review"  # PR green; a reviewer agent checks it against the issue before it is ready
READY = "ready"  # PR green (and approved, when reviewed), waiting for the integrator
DONE = "done"  # the forge says CLOSED (queued again if it is reopened)
BLOCKED = "blocked"  # needs a human; carries the blocked label
AWAITING_GREEN = "awaiting_green"  # async-red issue whose agent found nothing to change: the next green run closes it
RELEASED = "released"  # merged, not yet live: closed once [integrate] live_version reports a version containing it

ACTIVE = (RUNNING, WAITING_CI, IN_REVIEW, READY)


@dataclass
class IssueRecord:
    issue: int
    status: str = QUEUED
    attempts: int = 0  # real attempts spent on the issue
    provider_retries: int = 0  # model-endpoint failures; never attempts
    launch_failures: int = 0  # the agent could not start (login, config); never attempts
    timeouts: int = 0  # time limits reached and continued in the same worktree
    ci_rounds: int = 0  # times a red PR was handed back
    revivals: int = 0
    model: str = ""
    pid: int = 0
    started_at: float = 0.0
    not_before: float = 0.0  # backoff: do not relaunch before this time
    stalled_since: float = 0.0  # launchable but no slot for its model since then (overflow clock)
    overflow: str = ""  # why the current launch is on an overflow model; "" when routed as usual
    hold_model: str = ""  # stopped by its model's usage limit: waits for that model and resumes there (MOD-07)
    down_waiting: float = 0.0  # since when its endpoint failures went uncounted because its model was down
    branch: str = ""
    worktree: str = ""
    log: str = ""
    pr: int = 0
    pr_sha: str = ""
    note: str = ""  # handed to the next launch (why it was relaunched)
    resume: bool = False  # continue the previous agent session
    fresh: bool = False  # force a new session even if one exists
    last_exit: str = ""
    blocked_reason: str = ""
    done_at: float = 0.0  # when nohalt saw the issue closed; comments after it are handed over on a reopen
    labelled: bool = False  # the forge carries the blocked label we put there
    swept: list[str] = field(default_factory=list)  # PR head SHAs already handed back
    lane: str = ""  # the [[prompt.lane]] chosen at the first launch ("" = no lane); kept across relaunches
    lane_digest: str = ""  # the lanes' configuration it was chosen under ("" = not chosen yet)
    # Early start (early.py): the unmerged dependency heads its synthetic base was built on.
    early_on: list[dict[str, Any]] = field(default_factory=list)  # [{issue, pr, branch, sha}]; [] once they merged
    early_note: str = ""  # handed to the next launch: a dependency moved, or they all merged
    early_refused: list[str] = field(default_factory=list)  # [dependency heads, why] the synthetic base failed on
    # The review gate (review.py). A reviewer runs beside the record's own agent slot.
    review_pid: int = 0
    review_model: str = ""
    review_started: float = 0.0
    review_sha: str = ""  # the PR head the last (or running) review looked at
    review_body: str = ""  # a hash of the PR description that review read: an edit to it alone is a change (#74)
    review_body_hint: str = ""  # the author's "NOHALT-RESULT: updated-description" from its last run; a hint only
    review_verdict: str = ""  # approve | changes | "" (none yet)
    review_text: str = ""  # the last verdict's review, handed to the author on "changes"
    review_approved: str = ""  # the head SHA an approval covers; any other head needs a new review
    review_carried: str = ""  # the head actually approved, when nohalt refreshes carried it to review_approved (#69)
    review_approved_at: float = 0.0  # when the approval was given (a carry keeps it): approved PRs batch in this order
    review_rounds: int = 0  # "changes" verdicts handed back
    review_failures: int = 0  # reviews that ended without a verdict
    review_retries: int = 0  # reviewer endpoint failures; never failures
    review_not_before: float = 0.0
    review_exit: str = ""  # why the supervisor stopped the reviewer (timeout, hung)
    review_cooling: str = ""  # the warning cool-down the last reviewer was let through ([review] during_cooldown)
    history: list[str] = field(default_factory=list)

    def event(self, text: str) -> None:
        self.history.append(f"{stamp()}  {text}")
        del self.history[:-50]


def pause_until(state: StateStore) -> float:
    """When ``nohalt pause --for`` makes PAUSE expire (epoch seconds), or 0 if it does not."""
    for line in state.read_lines("PAUSE"):
        if line.startswith("until="):
            try:
                return float(line.split("=", 1)[1])
            except ValueError:
                return 0.0
    return 0.0


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        self.issues_dir = root / "issues"
        self.logs_dir = root / "logs"
        self.bin_dir = root / "bin"
        self.agent_data = root / "agent-data"
        for d in (self.root, self.issues_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- issue records -----------------------------------------------------------
    def _path(self, issue: int) -> Path:
        return self.issues_dir / f"{issue}.json"

    def get(self, issue: int) -> IssueRecord:
        data = read_json(self._path(issue))
        if not data:
            return IssueRecord(issue=issue)
        known = IssueRecord.__dataclass_fields__
        return IssueRecord(**{k: v for k, v in data.items() if k in known})

    def exists(self, issue: int) -> bool:
        return self._path(issue).exists()

    def put(self, rec: IssueRecord) -> None:
        write_json(self._path(rec.issue), asdict(rec))

    def all(self) -> list[IssueRecord]:
        out = []
        for p in sorted(self.issues_dir.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
            if p.stem.isdigit():
                out.append(self.get(int(p.stem)))
        return out

    # -- control files -----------------------------------------------------------
    def flag(self, name: str) -> Path:
        return self.root / name

    def has(self, name: str) -> bool:
        return self.flag(name).exists()

    def set_flag(self, name: str, text: str = "") -> None:
        self.flag(name).write_text(text)

    def clear_flag(self, name: str) -> None:
        self.flag(name).unlink(missing_ok=True)

    def flag_age(self, name: str) -> float | None:
        try:
            return now() - self.flag(name).stat().st_mtime
        except OSError:
            return None

    def read_lines(self, name: str) -> list[str]:
        try:
            return [ln.strip() for ln in self.flag(name).read_text().splitlines() if ln.strip()]
        except OSError:
            return []

    def append_line(self, name: str, line: str) -> None:
        with open(self.flag(name), "a") as f:
            f.write(line.rstrip("\n") + "\n")

    # -- json blobs --------------------------------------------------------------
    def load(self, name: str, default: Any = None) -> Any:
        return read_json(self.root / name, default)

    def save(self, name: str, data: Any) -> None:
        write_json(self.root / name, data)

    # -- event log -----------------------------------------------------------------
    def log(self, text: str, **fields_: Any) -> None:
        line = f"{stamp()}  {text}"
        with open(self.logs_dir / "nohalt.log", "a") as f:
            f.write(line + "\n")
        if fields_:
            with open(self.logs_dir / "events.jsonl", "a") as f:
                f.write(json.dumps({"ts": now(), "msg": text, **fields_}) + "\n")

    def issue_log(self, issue: int) -> Path:
        return self.logs_dir / f"issue-{issue}.log"


class FileLock:
    """An exclusive ``flock``. The descriptor is never inherited by children:
    a backgrounded server that inherits a lock holds it for as long as it lives."""

    def __init__(self, path: Path):
        self.path = path
        self._f: IO[str] | None = None

    def acquire(self, blocking: bool = True) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")  # noqa: SIM115 - held for the lock's lifetime
        os.set_inheritable(f.fileno(), False)
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self._f = f
        return True

    def release(self) -> None:
        if self._f:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
            self._f.close()
            self._f = None

    def held_by(self) -> int:
        try:
            return int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def iter_locked(paths: list[Path]) -> Iterator[tuple[int, FileLock]]:
    """Try each lock without blocking; yield the first one taken."""
    for i, p in enumerate(paths):
        lock = FileLock(p)
        if lock.acquire(blocking=False):
            yield i, lock
            return
