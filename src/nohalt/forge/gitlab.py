"""GitLab (gitlab.com or self-hosted) through the REST API v4.

Mapping onto nohalt's forge model:

* issues are issues (``iid``); ``opened`` is OPEN;
* merge requests are pull requests (``iid``, ``source_branch``, ``sha``);
* the jobs of the newest pipeline for a commit are its check runs -- a
  required check is a job name; a job allowed to fail never makes a commit red;
* pipelines are runs; ``ci_workflow`` / ``cd_workflow`` match a pipeline's
  ``workflow:name``, or ``"pipeline"`` for any pipeline on the commit.

Authentication: a token in ``$GITLAB_TOKEN`` (or the variable named by
``[project] token_env``), with the ``api`` scope; if unset, the token ``glab``
is logged in with for that host. As with GitHub, every call has a timeout and
a failed call raises ForgeError -- it never returns something that looks like
an answer.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from ..util import run
from .base import CLOSED, MISSING, OPEN, Check, ForgeError, Issue, PullRequest, Run

DEFAULT_URL = "https://gitlab.com"

# GitLab marks a merge request as a draft by its title.
_DRAFT_PREFIX = re.compile(r"^\s*(\[draft\]|\(draft\)|draft:|draft\s+-|\[wip\]|wip:)\s*", re.I)

# job status -> (check status, conclusion)
_JOB = {
    "created": ("queued", ""),
    "pending": ("queued", ""),
    "waiting_for_resource": ("queued", ""),
    "preparing": ("queued", ""),
    "scheduled": ("queued", ""),
    "running": ("in_progress", ""),
    "success": ("completed", "success"),
    "failed": ("completed", "failure"),
    "canceled": ("completed", "cancelled"),
    "canceling": ("in_progress", ""),
    "skipped": ("completed", "skipped"),
    "manual": ("completed", "skipped"),  # a manual job has not run; it cannot gate
}

# detailed_merge_status -> mergeable. The set is open-ended and grows with GitLab
# versions (`unchecked`, `checking`, `commits_status`, `ci_must_pass`,
# `not_approved`, ... are all seen in the wild): anything not listed here is
# "not known yet" (None), never "mergeable".
_MERGEABLE: dict[str, bool | None] = {
    "mergeable": True,
    "conflict": False,
    "need_rebase": False,
    "broken_status": False,
}


class GitLab:
    def __init__(
        self, repo: str, url: str = "", token: str = "", token_env: str = "GITLAB_TOKEN", timeout: int = 60, retries: int = 3
    ):
        self.repo = repo
        self.url = (url or os.environ.get("GITLAB_HOST") or DEFAULT_URL).rstrip("/")
        if "://" not in self.url:
            self.url = "https://" + self.url
        self.token = token or os.environ.get(token_env) or os.environ.get("GITLAB_TOKEN") or self._glab_token()
        self.timeout = timeout
        self.retries = retries
        self.calls = 0
        self.pid = urllib.parse.quote(repo, safe="")

    def _glab_token(self) -> str:
        host = urllib.parse.urlparse(self.url if "://" in self.url else "https://" + self.url).hostname or "gitlab.com"
        cp = run(["glab", "config", "get", "token", "--host", host], timeout=15)
        return cp.stdout.strip() if cp.returncode == 0 else ""

    # -- transport ----------------------------------------------------------------------
    def _request(
        self, method: str, path: str, data: dict[str, Any] | None = None, *, ok_404: bool = False, raw: bool = False
    ) -> tuple[Any, dict[str, str]]:
        url = f"{self.url}/api/v4/{path}"
        body = None
        headers = {"Accept": "application/json", "User-Agent": "nohalt"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        last = ""
        for attempt in range(self.retries):
            self.calls += 1
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8", "replace")
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    if raw:
                        return text, hdrs
                    return (json.loads(text) if text.strip() else {}), hdrs
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code == 404:
                    if ok_404:
                        return None, {}
                    raise ForgeError(f"{path}: not found") from None
                if e.code in (400, 401, 403, 405, 406, 409, 422):
                    raise ForgeError(f"{method} {path}: HTTP {e.code} {detail}") from None
                last = f"HTTP {e.code} {detail}"
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = str(e)
            time.sleep(min(30, 5 * (attempt + 1)))
        raise ForgeError(f"{method} {path} failed after {self.retries} tries: {last[:300]}")

    def _get(self, path: str, ok_404: bool = False) -> Any:
        return self._request("GET", path, ok_404=ok_404)[0]

    def _all(self, path: str) -> list[Any]:
        """Every page of a list endpoint (X-Next-Page)."""
        out: list[Any] = []
        sep = "&" if "?" in path else "?"
        page = "1"
        while page:
            data, hdrs = self._request("GET", f"{path}{sep}per_page=100&page={page}")
            out.extend(data or [])
            page = hdrs.get("x-next-page", "")
        return out

    def _p(self, tail: str) -> str:
        return f"projects/{self.pid}/{tail}"

    # -- issues -----------------------------------------------------------------------------
    def issue(self, number: int) -> Issue:
        d = self._get(self._p(f"issues/{number}"), ok_404=True)
        if d is None:
            return Issue(number, MISSING)
        return Issue(number, OPEN if d.get("state") == "opened" else CLOSED, d.get("title") or "", list(d.get("labels") or []))

    def add_label(self, number: int, label: str) -> None:
        self._request("PUT", self._p(f"issues/{number}"), {"add_labels": label})

    def remove_label(self, number: int, label: str) -> None:
        self._request("PUT", self._p(f"issues/{number}"), {"remove_labels": label}, ok_404=True)

    def comment(self, number: int, body: str) -> None:
        self._request("POST", self._p(f"issues/{number}/notes"), {"body": body})

    def close_issue(self, number: int) -> None:
        self._request("PUT", self._p(f"issues/{number}"), {"state_event": "close"})

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
        d: dict[str, Any] = {"title": title, "description": body}
        if labels:
            d["labels"] = ",".join(labels)
        return int(self._request("POST", self._p("issues"), d)[0]["iid"])

    # -- merge requests ------------------------------------------------------------------------
    @staticmethod
    def _mr(d: dict[str, Any]) -> PullRequest:
        status = d.get("detailed_merge_status") or d.get("merge_status") or ""
        mergeable = _MERGEABLE.get(status)
        if status == "can_be_merged":
            mergeable = True
        elif status == "cannot_be_merged" or d.get("has_conflicts"):
            mergeable = False
        return PullRequest(
            number=int(d["iid"]),
            head_ref=d.get("source_branch") or "",
            head_sha=d.get("sha") or "",
            base_ref=d.get("target_branch") or "",
            body=d.get("description") or "",
            title=d.get("title") or "",
            mergeable=mergeable,
            draft=bool(d.get("draft") or d.get("work_in_progress")),
            merged=d.get("state") == "merged",
        )

    def open_pulls(self) -> list[PullRequest]:
        out = []
        for d in self._all(self._p("merge_requests?state=opened")):
            if not d.get("sha"):
                # Some instances omit the head SHA from the list; the detail endpoint has it.
                d = self._get(self._p(f"merge_requests/{d['iid']}"))
            out.append(self._mr(d))
        return out

    def pull(self, number: int) -> PullRequest:
        return self._mr(self._get(self._p(f"merge_requests/{number}")))

    def create_pull(self, title: str, head: str, base: str, body: str) -> int:
        d = self._request(
            "POST", self._p("merge_requests"), {"source_branch": head, "target_branch": base, "title": title, "description": body}
        )[0]
        return int(d["iid"])

    def merge_pull(self, number: int, method: str) -> bool:
        data: dict[str, Any] = {"should_remove_source_branch": False}
        if method == "squash":
            data["squash"] = True
        try:
            d = self._request("PUT", self._p(f"merge_requests/{number}/merge"), data)[0]
        except ForgeError:
            return False
        return (d or {}).get("state") == "merged"

    def mark_ready(self, number: int) -> None:
        """Clear a merge request's draft status by removing the draft prefix from its title."""
        title = self._get(self._p(f"merge_requests/{number}")).get("title") or ""
        clean = _DRAFT_PREFIX.sub("", title, count=1).strip() or title
        self._request("PUT", self._p(f"merge_requests/{number}"), {"title": clean})

    def comment_pull(self, number: int, body: str) -> None:
        self._request("POST", self._p(f"merge_requests/{number}/notes"), {"body": body})

    def close_pull(self, number: int, comment: str = "") -> None:
        if comment:
            self._request("POST", self._p(f"merge_requests/{number}/notes"), {"body": comment})
        self._request("PUT", self._p(f"merge_requests/{number}"), {"state_event": "close"})

    # -- pipelines and jobs ----------------------------------------------------------------------
    def _pipelines(self, sha: str = "", status: str = "") -> list[dict[str, Any]]:
        q = ["order_by=id", "sort=desc"]
        if sha:
            q.append(f"sha={sha}")
        if status:
            q.append(f"status={status}")
        return list(self._get(self._p("pipelines?" + "&".join(q) + "&per_page=100")) or [])

    def checks(self, sha: str) -> list[Check]:
        pipes = self._pipelines(sha=sha)
        if not pipes:
            return []
        jobs = self._all(self._p(f"pipelines/{pipes[0]['id']}/jobs?include_retried=false"))
        out = []
        for j in jobs:
            status, conclusion = _JOB.get(j.get("status", ""), ("queued", ""))
            if conclusion == "failure" and j.get("allow_failure"):
                conclusion = "neutral"
            out.append(Check(j.get("name") or "", status, conclusion, j.get("web_url") or ""))
        return out

    def managed_suites(self, sha: str) -> set[int]:
        return set()  # GitLab has no forge-managed jobs posting on the base branch

    @staticmethod
    def _run(p: dict[str, Any]) -> Run:
        status, conclusion = _JOB.get(p.get("status", ""), ("queued", ""))
        return Run(
            int(p["id"]),
            p.get("name") or "pipeline",
            p.get("sha") or "",
            status,
            conclusion,
            p.get("ref") or "",
            p.get("web_url") or "",
        )

    def runs(self, *, head_sha: str = "", workflow: str = "", status: str = "") -> list[Run]:
        statuses = {"queued": ["pending", "created"], "in_progress": ["running"]}.get(status, [""])
        out: list[Run] = []
        for st in statuses:
            for p in self._pipelines(sha=head_sha, status=st):
                r = self._run(p)
                if workflow and workflow not in ("pipeline", r.name):
                    continue
                out.append(r)
        return out

    def rerun_failed(self, run_id: int) -> None:
        self._request("POST", self._p(f"pipelines/{run_id}/retry"))

    def cancel_run(self, run_id: int) -> None:
        self._request("POST", self._p(f"pipelines/{run_id}/cancel"), ok_404=True)

    def failed_log(self, run_id: int) -> str:
        logs = []
        for j in self._all(self._p(f"pipelines/{run_id}/jobs?scope[]=failed")):
            try:
                text, _ = self._request("GET", self._p(f"jobs/{j['id']}/trace"), raw=True)
            except ForgeError:
                continue
            logs.append(str(text)[-20000:])
        return "\n".join(logs)

    def commit_time(self, sha: str) -> float | None:
        d = self._get(self._p(f"repository/commits/{sha}"), ok_404=True)
        if not d:
            return None
        return datetime.fromisoformat(d["committed_date"].replace("Z", "+00:00")).timestamp()

    def branch_sha(self, branch: str) -> str:
        d = self._get(self._p(f"repository/branches/{urllib.parse.quote(branch, safe='')}"), ok_404=True)
        return (d or {}).get("commit", {}).get("id", "")

    def issue_text(self, number: int) -> str:
        d = self._get(self._p(f"issues/{number}"))
        notes = self._all(self._p(f"issues/{number}/notes?sort=asc&order_by=created_at"))
        parts = [f"# #{number}: {d.get('title', '')}", "", d.get("description") or "(no description)"]
        for n in notes:
            if n.get("system"):
                continue
            parts += ["", f"--- comment by {(n.get('author') or {}).get('username', '?')}", n.get("body") or ""]
        return "\n".join(parts)

    # -- for `nohalt doctor` ------------------------------------------------------------------------
    def token_scopes(self) -> list[str]:
        try:
            d = self._get("personal_access_tokens/self")
        except ForgeError:
            return []
        return list((d or {}).get("scopes") or [])

    def project(self) -> dict[str, Any] | None:
        try:
            return self._get(f"projects/{self.pid}", ok_404=True)
        except ForgeError:
            return None

    def reachable(self) -> bool:
        return self.project() is not None

    def protected(self, branch: str) -> bool:
        try:
            return self._get(self._p(f"protected_branches/{urllib.parse.quote(branch, safe='')}"), ok_404=True) is not None
        except ForgeError:
            return False
