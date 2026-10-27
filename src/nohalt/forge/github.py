"""GitHub through the ``gh`` CLI's REST passthrough.

REST, not GraphQL: GraphQL-backed ``gh`` subcommands have broken on GitHub-side
deprecations while the REST endpoints kept working. Every call has a timeout --
one hung ``gh`` call once stalled every release for 25 minutes -- and a failed
call raises ForgeError rather than returning something that looks like an
answer ("no PR", "not found").
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

from ..util import run
from .base import CLOSED, MISSING, OPEN, Check, ForgeError, Issue, PullRequest, Run


class GitHub:
    def __init__(self, repo: str, gh: str = "", timeout: int = 60, retries: int = 3):
        self.repo = repo
        self.gh = gh or os.environ.get("NOHALT_REAL_GH") or "gh"
        self.timeout = timeout
        self.retries = retries
        self.calls = 0

    # -- transport ------------------------------------------------------------------
    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        fields: dict[str, Any] | None = None,
        paginate: bool = False,
        items: str = "",
        ok_404: bool = False,
    ) -> Any:
        argv = [self.gh, "api", path, "-H", "Accept: application/vnd.github+json"]
        if method != "GET":
            argv += ["-X", method]
        for k, v in (fields or {}).items():
            if isinstance(v, list):
                for item in v:
                    argv += ["-f", f"{k}[]={item}"]
            elif isinstance(v, bool):
                argv += ["-F", f"{k}={'true' if v else 'false'}"]
            else:
                argv += ["-f", f"{k}={v}"]
        if paginate:
            argv.append("--paginate")
        if items:
            # Older gh has no --slurp, and --paginate concatenates pages: emit
            # one JSON object per line instead and parse them one by one.
            argv += ["--jq", f"{items} | tojson"]
        last = ""
        for attempt in range(self.retries):
            self.calls += 1
            cp = run(argv, timeout=self.timeout)
            if cp.returncode == 0:
                out = cp.stdout.strip()
                if items:
                    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]
                return json.loads(out) if out else {}
            err = (cp.stderr + cp.stdout).strip()
            if "HTTP 404" in err or "Not Found" in err:
                if ok_404:
                    return None
                raise ForgeError(f"{path}: not found")
            if "HTTP 422" in err or "HTTP 405" in err or "HTTP 409" in err:
                raise ForgeError(f"{path}: {err[:300]}")
            last = err
            time.sleep(min(30, 5 * (attempt + 1)))
        raise ForgeError(f"gh api {path} failed after {self.retries} tries: {last[:300]}")

    def _r(self, tail: str) -> str:
        return f"repos/{self.repo}/{tail}"

    # -- issues -------------------------------------------------------------------------
    def issue(self, number: int) -> Issue:
        # A number that does not exist is permanent: answer MISSING at once.
        # A network failure is not an answer: it raises, and the caller treats
        # the issue as UNKNOWN (never as closed, never as missing).
        data = self._api(self._r(f"issues/{number}"), ok_404=True)
        if data is None or "pull_request" in data:
            return Issue(number, MISSING)
        return Issue(
            number,
            OPEN if data.get("state") == "open" else CLOSED,
            title=data.get("title") or "",
            labels=[lab["name"] for lab in data.get("labels") or []],
        )

    def add_label(self, number: int, label: str) -> None:
        self._api(self._r(f"issues/{number}/labels"), method="POST", fields={"labels": [label]})

    def remove_label(self, number: int, label: str) -> None:
        self._api(self._r(f"issues/{number}/labels/{label}"), method="DELETE", ok_404=True)

    def comment(self, number: int, body: str) -> None:
        self._api(self._r(f"issues/{number}/comments"), method="POST", fields={"body": body})

    def close_issue(self, number: int) -> None:
        self._api(self._r(f"issues/{number}"), method="PATCH", fields={"state": "closed"})

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
        f: dict[str, Any] = {"title": title, "body": body}
        if labels:
            f["labels"] = labels
        return int(self._api(self._r("issues"), method="POST", fields=f)["number"])

    def issue_text(self, number: int) -> str:
        d = self._api(self._r(f"issues/{number}"))
        comments = self._api(self._r(f"issues/{number}/comments?per_page=100"), paginate=True, items=".[]")
        parts = [f"# #{number}: {d.get('title', '')}", "", d.get("body") or "(no description)"]
        for c in comments:
            parts += ["", f"--- comment by {(c.get('user') or {}).get('login', '?')}", c.get("body") or ""]
        return "\n".join(parts)

    # -- pull requests --------------------------------------------------------------------
    @staticmethod
    def _pr(d: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=d["number"],
            head_ref=d["head"]["ref"],
            head_sha=d["head"]["sha"],
            base_ref=d["base"]["ref"],
            body=d.get("body") or "",
            title=d.get("title") or "",
            mergeable=d.get("mergeable"),
            draft=bool(d.get("draft")),
            merged=bool(d.get("merged_at")),
        )

    def open_pulls(self) -> list[PullRequest]:
        # One paginated listing per pass. Per-issue PR lookups across a few
        # hundred watched issues once cost ~5,000 calls an hour and exhausted
        # the rate limit for every agent sharing the token.
        rows = self._api(self._r("pulls?state=open&per_page=100"), paginate=True, items=".[]")
        return [self._pr(d) for d in rows]

    def pull(self, number: int) -> PullRequest:
        return self._pr(self._api(self._r(f"pulls/{number}")))

    def create_pull(self, title: str, head: str, base: str, body: str) -> int:
        d = self._api(self._r("pulls"), method="POST", fields={"title": title, "head": head, "base": base, "body": body})
        return int(d["number"])

    def merge_pull(self, number: int, method: str) -> bool:
        try:
            d = self._api(self._r(f"pulls/{number}/merge"), method="PUT", fields={"merge_method": method})
        except ForgeError:
            return False
        return bool(d.get("merged"))

    def mark_ready(self, number: int) -> None:
        """Mark a draft ready for review (GraphQL: REST has no endpoint for it)."""
        node = self._api(self._r(f"pulls/{number}"))["node_id"]
        cp = run(
            [
                self.gh,
                "api",
                "graphql",
                "-f",
                "query=mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { clientMutationId } }",
                "-f",
                f"id={node}",
            ],
            timeout=self.timeout,
        )
        if cp.returncode != 0:
            raise ForgeError(f"mark ready #{number}: {(cp.stderr or cp.stdout).strip()[:300]}")

    def comment_pull(self, number: int, body: str) -> None:
        self.comment(number, body)  # a pull request is an issue to the comments API

    def review_pull(self, number: int, body: str, approve: bool) -> None:
        """A formal review. GitHub refuses one on a PR the token's own account opened: callers fall back to a comment."""
        event = "APPROVE" if approve else "REQUEST_CHANGES"
        self._api(self._r(f"pulls/{number}/reviews"), method="POST", fields={"body": body, "event": event})

    def close_pull(self, number: int, comment: str = "") -> None:
        if comment:
            self.comment(number, comment)
        self._api(self._r(f"pulls/{number}"), method="PATCH", fields={"state": "closed"})

    # -- checks and runs ----------------------------------------------------------------------
    def checks(self, sha: str) -> list[Check]:
        rows = self._api(self._r(f"commits/{sha}/check-runs?per_page=100"), paginate=True, items=".check_runs[]")
        return [
            Check(
                r["name"],
                r["status"],
                r.get("conclusion") or "",
                r.get("details_url") or "",
                int((r.get("check_suite") or {}).get("id") or 0),
            )
            for r in rows
        ]

    def managed_suites(self, sha: str) -> set[int]:
        """Check suites of GitHub-managed jobs on a commit (event "dynamic": Dependabot
        updates, the dependency graph). They post check runs on the base branch
        after a merge, but they are not the pipeline the merge triggered."""
        data = self._api(self._r(f"actions/runs?head_sha={sha}&per_page=100"))
        return {
            int(r["check_suite_id"])
            for r in data.get("workflow_runs", [])
            if r.get("event") == "dynamic" and r.get("check_suite_id")
        }

    def runs(self, *, head_sha: str = "", workflow: str = "", status: str = "") -> list[Run]:
        q = ["per_page=100"]
        if head_sha:
            q.append(f"head_sha={head_sha}")
        if status:
            q.append(f"status={status}")
        data = self._api(self._r("actions/runs?" + "&".join(q)))
        out = []
        for r in data.get("workflow_runs", []):
            if workflow and r.get("name") != workflow:
                continue
            out.append(
                Run(
                    r["id"],
                    r.get("name") or "",
                    r.get("head_sha") or "",
                    r.get("status") or "",
                    r.get("conclusion") or "",
                    r.get("head_branch") or "",
                    r.get("html_url") or "",
                    r.get("event") or "",
                )
            )
        return out

    def rerun_failed(self, run_id: int) -> None:
        self._api(self._r(f"actions/runs/{run_id}/rerun-failed-jobs"), method="POST")

    def cancel_run(self, run_id: int) -> None:
        self._api(self._r(f"actions/runs/{run_id}/cancel"), method="POST", ok_404=True)

    def failed_log(self, run_id: int) -> str:
        cp = run([self.gh, "run", "view", str(run_id), "-R", self.repo, "--log-failed"], timeout=self.timeout)
        return cp.stdout if cp.returncode == 0 else ""

    def commit_time(self, sha: str) -> float | None:
        d = self._api(self._r(f"commits/{sha}"), ok_404=True)
        if not d:
            return None
        iso = d["commit"]["committer"]["date"]
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    def branch_sha(self, branch: str) -> str:
        d = self._api(self._r(f"branches/{branch}"), ok_404=True)
        return (d or {}).get("commit", {}).get("sha", "")
