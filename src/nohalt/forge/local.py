"""A forge kept in one JSON file.

Used by the test-suite and for rehearsing a configuration without touching
GitHub (``[project] repo = "local:/path/forge.json"``). Separate processes --
a fake agent closing its issue, say -- can change it safely: every access
holds an flock.

    python -m nohalt.forge.local <file> close 12
    python -m nohalt.forge.local <file> pr 12 feature/12-x <sha> [--green|--red]
"""

from __future__ import annotations

import fcntl
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .base import CLOSED, MISSING, OPEN, Check, ForgeError, Issue, PullRequest, Run


class LocalForge:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.exists():
            self._write(self._empty())
        self.calls = 0
        self.fail = False  # tests: make every call raise, like a network outage

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"issues": {}, "pulls": {}, "checks": {}, "runs": [], "comments": [], "merged": [], "branches": {}, "next": 1}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, indent=1, sort_keys=True))

    @contextmanager
    def _db(self) -> Iterator[dict[str, Any]]:
        if self.fail:
            raise ForgeError("local forge: simulated outage")
        self.calls += 1
        with open(self.path.with_suffix(".lock"), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text() or "{}") or self._empty()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                self._write(data)

    # -- seeding (tests) ----------------------------------------------------------------
    def add_issue(self, number: int, title: str = "", labels: list[str] | None = None, state: str = OPEN) -> None:
        with self._db() as d:
            d["issues"][str(number)] = {"title": title or f"Issue {number}", "labels": labels or [], "state": state}
            d["next"] = max(d["next"], number + 1)

    def set_state(self, number: int, state: str) -> None:
        with self._db() as d:
            d["issues"][str(number)]["state"] = state

    def add_pull(
        self,
        number: int,
        head_ref: str,
        head_sha: str,
        body: str = "",
        base: str = "main",
        mergeable: bool | None = True,
        draft: bool = False,
    ) -> None:
        with self._db() as d:
            d["pulls"][str(number)] = {
                "head_ref": head_ref,
                "head_sha": head_sha,
                "body": body,
                "base": base,
                "mergeable": mergeable,
                "draft": draft,
                "state": "open",
                "title": f"PR {number}",
            }
            d["next"] = max(d["next"], number + 1)

    def set_checks(self, sha: str, checks: list[tuple[str, str, str]]) -> None:
        with self._db() as d:
            d["checks"][sha] = [list(c) for c in checks]

    def add_run(self, run: Run) -> None:
        with self._db() as d:
            d["runs"].append(run.__dict__)

    def data(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())

    # -- Forge protocol -----------------------------------------------------------------------
    def issue(self, number: int) -> Issue:
        with self._db() as d:
            i = d["issues"].get(str(number))
        if not i:
            return Issue(number, MISSING)
        return Issue(number, i["state"], i["title"], list(i["labels"]))

    def add_label(self, number: int, label: str) -> None:
        with self._db() as d:
            labels = d["issues"][str(number)]["labels"]
            if label not in labels:
                labels.append(label)

    def remove_label(self, number: int, label: str) -> None:
        with self._db() as d:
            labels = d["issues"][str(number)]["labels"]
            if label in labels:
                labels.remove(label)

    def comment(self, number: int, body: str) -> None:
        with self._db() as d:
            d["comments"].append([number, body])

    def issue_text(self, number: int) -> str:
        with self._db() as d:
            i = d["issues"].get(str(number)) or {}
            comments = [c[1] for c in d["comments"] if c[0] == number]
        return "\n\n".join([f"# #{number}: {i.get('title', '')}", i.get("body", "(no description)"), *comments])

    def close_issue(self, number: int) -> None:
        self.set_state(number, CLOSED)

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
        with self._db() as d:
            n = d["next"]
            d["next"] = n + 1
            d["issues"][str(n)] = {"title": title, "labels": labels or [], "state": OPEN, "body": body}
        return n

    def _pr(self, n: str, p: dict[str, Any]) -> PullRequest:
        return PullRequest(
            int(n),
            p["head_ref"],
            p["head_sha"],
            p.get("base", ""),
            p.get("body", ""),
            p.get("title", ""),
            p.get("mergeable"),
            merged=p["state"] == "merged",
            draft=bool(p.get("draft")),
        )

    def open_pulls(self) -> list[PullRequest]:
        with self._db() as d:
            return [self._pr(n, p) for n, p in d["pulls"].items() if p["state"] == "open"]

    def pull(self, number: int) -> PullRequest:
        with self._db() as d:
            p = d["pulls"].get(str(number))
        if not p:
            raise ForgeError(f"no PR {number}")
        return self._pr(str(number), p)

    def create_pull(self, title: str, head: str, base: str, body: str) -> int:
        with self._db() as d:
            n = d["next"]
            d["next"] = n + 1
            sha = d["branches"].get(head, f"sha-{head}")
            d["pulls"][str(n)] = {
                "head_ref": head,
                "head_sha": sha,
                "body": body,
                "base": base,
                "mergeable": True,
                "state": "open",
                "title": title,
            }
        return n

    def merge_pull(self, number: int, method: str) -> bool:
        with self._db() as d:
            p = d["pulls"].get(str(number))
            if not p or p["state"] != "open" or p.get("mergeable") is False:
                return False
            p["state"] = "merged"
            d["merged"].append(number)
            d["branches"][p.get("base") or "main"] = f"merge-{number}"
            body = p.get("body", "")
            # Like GitHub: closing keywords only act on merges into the default branch.
            if (p.get("base") or "main") != d.get("default_branch", "main"):
                body = ""
        for m in re.finditer(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", body, re.I):
            with self._db() as d:
                if m.group(1) in d["issues"]:
                    d["issues"][m.group(1)]["state"] = CLOSED
        return True

    def mark_ready(self, number: int) -> None:
        with self._db() as d:
            d["pulls"][str(number)]["draft"] = False

    def close_pull(self, number: int, comment: str = "") -> None:
        with self._db() as d:
            d["pulls"][str(number)]["state"] = "closed"
            if comment:
                d["comments"].append([number, comment])

    def checks(self, sha: str) -> list[Check]:
        with self._db() as d:
            return [Check(*c) for c in d["checks"].get(sha, [])]

    def runs(self, *, head_sha: str = "", workflow: str = "", status: str = "") -> list[Run]:
        with self._db() as d:
            rows = [Run(**r) for r in d["runs"]]
        return [
            r
            for r in rows
            if (not head_sha or r.head_sha == head_sha)
            and (not workflow or r.name == workflow)
            and (not status or r.status == status)
        ]

    def rerun_failed(self, run_id: int) -> None:
        with self._db() as d:
            d.setdefault("reruns", []).append(run_id)

    def cancel_run(self, run_id: int) -> None:
        with self._db() as d:
            d.setdefault("cancelled", []).append(run_id)

    def failed_log(self, run_id: int) -> str:
        with self._db() as d:
            return d.get("logs", {}).get(str(run_id), "")

    def commit_time(self, sha: str) -> float | None:
        with self._db() as d:
            return d.get("commit_times", {}).get(sha)

    def branch_sha(self, branch: str) -> str:
        with self._db() as d:
            return d["branches"].get(branch, "")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    f = LocalForge(argv[0])
    cmd, rest = argv[1], argv[2:]
    if cmd == "close":
        f.set_state(int(rest[0]), CLOSED)
    elif cmd == "pr":
        issue, ref, sha = int(rest[0]), rest[1], rest[2]
        with f._db() as d:
            n = d["next"]
        f.add_pull(n, ref, sha, body=f"Closes #{issue}")
        if "--green" in rest:
            f.set_checks(sha, [("ci", "completed", "success")])
        elif "--red" in rest:
            f.set_checks(sha, [("ci", "completed", "failure")])
        print(n)
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
