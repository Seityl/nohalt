"""The forge interface: the only things nohalt ever asks of GitHub (or a fake).

Completion is the forge's to decide. An issue is done when the forge says it
is CLOSED -- never because an agent said so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

OPEN, CLOSED, MISSING, UNKNOWN = "OPEN", "CLOSED", "MISSING", "UNKNOWN"


@dataclass
class Issue:
    number: int
    state: str  # OPEN | CLOSED | MISSING | UNKNOWN
    title: str = ""
    labels: list[str] = field(default_factory=list)

    def has_label(self, *names: str) -> bool:
        low = {lab.lower() for lab in self.labels}
        return any(n.lower() in low for n in names)


@dataclass
class PullRequest:
    number: int
    head_ref: str
    head_sha: str
    base_ref: str = ""
    body: str = ""
    title: str = ""
    mergeable: bool | None = None  # None: the forge has not computed it yet
    draft: bool = False
    merged: bool = False

    def closes(self) -> int | None:
        """The first issue the body closes ("Closes #N"), or None."""
        m = re.search(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", self.body or "", re.I)
        return int(m.group(1)) if m else None


@dataclass
class Check:
    name: str
    status: str  # queued | in_progress | completed
    conclusion: str = ""  # success | failure | timed_out | cancelled | skipped | neutral | ...
    url: str = ""
    suite: int = 0  # the check suite it belongs to (GitHub), 0 when unknown


@dataclass
class Run:
    id: int
    name: str
    head_sha: str
    status: str
    conclusion: str = ""
    branch: str = ""
    url: str = ""
    event: str = ""  # what started it (GitHub: push, pull_request, dynamic, ...)
    suite: int = 0  # its check suite (GitHub): which check runs are its jobs. 0 when unknown


class ForgeError(RuntimeError):
    """The forge could not be asked. Never read this as an answer."""


class Forge(Protocol):
    def issue(self, number: int) -> Issue: ...
    def add_label(self, number: int, label: str) -> None: ...
    def remove_label(self, number: int, label: str) -> None: ...
    def comment(self, number: int, body: str) -> None: ...
    def close_issue(self, number: int) -> None: ...
    def issue_text(self, number: int) -> str: ...
    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int: ...
    def open_pulls(self) -> list[PullRequest]: ...
    def pull(self, number: int) -> PullRequest: ...
    def create_pull(self, title: str, head: str, base: str, body: str) -> int: ...
    def merge_pull(self, number: int, method: str) -> bool: ...
    def close_pull(self, number: int, comment: str = "") -> None: ...
    def comment_pull(self, number: int, body: str) -> None: ...
    def mark_ready(self, number: int) -> None: ...
    def checks(self, sha: str) -> list[Check]: ...
    def managed_suites(self, sha: str) -> set[int]: ...
    def runs(self, *, head_sha: str = "", workflow: str = "", status: str = "", branch: str = "") -> list[Run]: ...
    def rerun_failed(self, run_id: int) -> None: ...
    def cancel_run(self, run_id: int) -> None: ...
    def failed_log(self, run_id: int) -> str: ...
    def commit_time(self, sha: str) -> float | None: ...
    def branch_sha(self, branch: str) -> str: ...


# -- check verdicts ----------------------------------------------------------------
GREEN, RED, PENDING = "green", "red", "pending"
_OK = {"success", "skipped", "neutral"}
_BAD = {"failure", "timed_out", "action_required", "startup_failure", "stale"}


def verdict(checks: list[Check], required: list[str], ignore: list[str] | tuple[str, ...] = ()) -> str:
    """GREEN, RED or PENDING for a commit's check runs.

    With ``required`` empty, every check counts and at least one must exist.
    A cancelled run is neither: superseded runs get cancelled all the time.
    Checks named in ``ignore`` never count, either way (a suite cancelled on
    pull requests by policy, say).
    """
    if ignore:
        checks = [c for c in checks if c.name not in ignore]
    if required:
        by: dict[str, Check] = {}
        for c in checks:
            by.setdefault(c.name, c)
        relevant = [by[n] for n in required if n in by]
        if any(c.status == "completed" and c.conclusion in _BAD for c in relevant):
            return RED
        if len(relevant) < len(required):
            return PENDING
    else:
        relevant = checks
        if any(c.status == "completed" and c.conclusion in _BAD for c in relevant):
            return RED
        if not relevant:
            return PENDING
    if all(c.status == "completed" and c.conclusion in _OK for c in relevant):
        return GREEN
    return PENDING


def check_drift(seen: set[str], required: list[str], ignore: list[str]) -> tuple[list[str], list[str]]:
    """(required names never seen, seen names that count for nothing).

    With ``required`` set only the named checks count: a misspelt name waits
    forever, and a real check left off the list can go red unnoticed.
    """
    if not required or not seen:
        return [], []
    missing = sorted(set(required) - seen)
    uncounted = sorted(seen - set(required) - set(ignore))
    return missing, uncounted
