"""GitLab: the forge against a fake GitLab API, the glab guard, prompts and init."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import pytest

from nohalt import config as config_mod
from nohalt import prompt as prompt_mod
from nohalt import shims
from nohalt.forge import ForgeError, verdict
from nohalt.forge.gitlab import GitLab

PROJECT = "/api/v4/projects/grp%2Fsub%2Fapp"


class FakeGitLab(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str, dict]]] = []

    def log_message(self, *a: object) -> None:  # quiet
        pass

    def _send(self, code: int, body: object, headers: dict[str, str] | None = None) -> None:
        data = body if isinstance(body, str) else json.dumps(body)
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data.encode())

    def _route(self, method: str) -> None:
        if self.path.startswith("/api/v4/personal_access_tokens/self"):
            return self._send(200, {"scopes": ["api", "sudo"]})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        u = urlparse(self.path)
        q = parse_qs(u.query)
        FakeGitLab.calls.append((method, u.path, body))
        assert self.headers.get("PRIVATE-TOKEN") == "tok"
        if not u.path.startswith(PROJECT):
            return self._send(404, {"message": "404 Project Not Found"})
        tail = u.path[len(PROJECT) :]
        if tail == "/issues/404":
            return self._send(404, {"message": "404 Not found"})
        if tail == "/issues/500":
            return self._send(502, "bad gateway")
        if tail.startswith("/issues/") and method == "GET":
            return self._send(200, {"iid": 1, "state": "opened", "title": "Fix it", "labels": ["bug", "P1"]})
        if tail.startswith("/issues/") and method in ("PUT", "POST"):
            return self._send(200, {"iid": 1})
        if tail == "/merge_requests" and method == "GET":
            page = q.get("page", ["1"])[0]
            mr = {
                "iid": 3 if page == "1" else 4,
                "source_branch": f"feature/{page}-x",
                "sha": f"sha{page}",
                "target_branch": "main",
                "description": f"Closes #{page}",
                "title": "t",
                "detailed_merge_status": "conflict" if page == "2" else "mergeable",
                "state": "opened",
            }
            if page == "2":
                mr["sha"] = None  # some instances leave the head SHA out of the list
            return self._send(200, [mr], {"X-Next-Page": "2" if page == "1" else ""})
        if tail == "/merge_requests/4" and method == "GET":
            return self._send(
                200,
                {
                    "iid": 4,
                    "source_branch": "feature/2-x",
                    "sha": "sha2-detail",
                    "target_branch": "main",
                    "description": "Closes #2",
                    "detailed_merge_status": "conflict",
                    "state": "opened",
                },
            )
        if tail == "/merge_requests/5" and method == "GET":
            return self._send(
                200, {"iid": 5, "source_branch": "f", "sha": "s", "detailed_merge_status": "commits_status", "state": "opened"}
            )
        if tail == "/merge_requests/6" and method == "GET":
            return self._send(200, {"iid": 6, "title": "Draft: add divide", "sha": "s6", "state": "opened", "draft": True})
        if tail == "/merge_requests/6" and method == "PUT":
            return self._send(200, {"iid": 6, "title": body.get("title"), "draft": False})
        if tail == "" and method == "GET":
            return self._send(200, {"id": 1, "default_branch": "main"})
        if tail == "/merge_requests/3/merge":
            return self._send(200, {"iid": 3, "state": "merged"})
        if tail == "/merge_requests/3/notes" and method == "POST":
            return self._send(201, {"id": 1})
        if tail == "/merge_requests/9/merge":
            return self._send(405, {"message": "405 Method Not Allowed"})
        if tail == "/pipelines":
            if q.get("sha") == ["none"]:
                return self._send(200, [])
            return self._send(200, [{"id": 7, "sha": "abc", "status": "failed", "ref": "main", "name": "CI"}])
        if tail == "/pipelines/7/jobs":
            return self._send(
                200,
                [
                    {"name": "test", "status": "success", "allow_failure": False},
                    {"name": "lint", "status": "failed", "allow_failure": True},
                    {"name": "deploy", "status": "manual"},
                ],
            )
        if tail == "/repository/branches/main":
            return self._send(200, {"commit": {"id": "headsha"}})
        return self._send(404, {"message": "404 Not found"})

    def do_GET(self) -> None:
        self._route("GET")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_POST(self) -> None:
        self._route("POST")


@pytest.fixture
def gitlab(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    FakeGitLab.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitLab)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield GitLab("grp/sub/app", url=f"http://127.0.0.1:{srv.server_address[1]}", token="tok", retries=2, timeout=5)
    srv.shutdown()


def test_issue_states_and_labels(gitlab):
    i = gitlab.issue(1)
    assert i.state == "OPEN" and i.title == "Fix it" and i.has_label("p1")
    assert gitlab.issue(404).state == "MISSING"


def test_unreachable_is_an_error_not_an_answer(gitlab):
    """API-02 on GitLab: a failed call raises; it is never read as missing or closed."""
    with pytest.raises(ForgeError):
        gitlab.issue(500)
    assert sum(1 for c in FakeGitLab.calls if c[1].endswith("/issues/500")) == 2  # retried
    assert sum(1 for c in FakeGitLab.calls if c[1].endswith("/issues/404")) == 0


def test_merge_requests_paginate_and_map(gitlab):
    mrs = gitlab.open_pulls()
    assert [m.number for m in mrs] == [3, 4]
    assert mrs[0].mergeable is True and mrs[1].mergeable is False
    assert mrs[1].head_sha == "sha2-detail"  # fetched from the detail endpoint
    assert mrs[0].closes() == 1 and mrs[0].head_ref == "feature/1-x"


def test_jobs_are_checks(gitlab):
    checks = gitlab.checks("abc")
    by = {c.name: c for c in checks}
    assert by["test"].conclusion == "success"
    assert by["lint"].conclusion == "neutral"  # allowed to fail: never red
    assert by["deploy"].conclusion == "skipped"  # manual: has not run
    assert verdict(checks, []) == "green"
    assert gitlab.checks("none") == []


def test_pipelines_are_runs(gitlab):
    runs = gitlab.runs(head_sha="abc", workflow="pipeline")
    assert runs and runs[0].conclusion == "failure" and runs[0].name == "CI"
    assert gitlab.runs(head_sha="abc", workflow="CD") == []


def test_merge_and_labels(gitlab):
    assert gitlab.merge_pull(3, "squash") is True
    assert gitlab.merge_pull(9, "merge") is False
    merge = next(c for c in FakeGitLab.calls if c[1].endswith("/merge_requests/3/merge"))
    assert merge[2]["squash"] is True
    gitlab.add_label(1, "nohalt-blocked")
    gitlab.close_issue(1)
    puts = [c[2] for c in FakeGitLab.calls if c[0] == "PUT" and c[1].endswith("/issues/1")]
    assert {"add_labels": "nohalt-blocked"} in puts and {"state_event": "close"} in puts
    assert gitlab.branch_sha("main") == "headsha"


def test_review_comments_go_on_the_merge_request(gitlab):
    gitlab.comment_pull(3, "**nohalt review: approve**")
    method, path, body = FakeGitLab.calls[-1][:3]
    assert method == "POST" and path.endswith("/merge_requests/3/notes") and "approve" in body["body"]


def test_config_accepts_subgroups_and_validates_forge(tmp_path):
    cfg = config_mod.from_dict({"project": {"repo": "grp/sub/app", "forge": "gitlab"}}, root=tmp_path)
    assert cfg.project.forge == "gitlab"
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_dict({"project": {"repo": "grp/sub/app"}}, root=tmp_path)  # github needs owner/name
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_dict({"project": {"repo": "a/b", "forge": "bitbucket"}}, root=tmp_path)


def test_gitlab_prompt_speaks_glab_and_merge_requests(tmp_path):
    cfg = config_mod.from_dict({"project": {"repo": "grp/app", "forge": "gitlab"}}, root=tmp_path)
    body = prompt_mod.render(prompt_mod.template(cfg), prompt_mod.values(cfg, issue=5, title="T", branch="b", worktree=tmp_path))
    assert "glab issue view 5 --repo grp/app" in body
    assert "merge request" in body and "pull request" not in body and "`glab`" in body


# -- the glab guard ------------------------------------------------------------------------------
@pytest.fixture
def glab_bin(tmp_path: Path) -> Path:
    d = shims.install(tmp_path / "bin")
    real = tmp_path / "real"
    real.mkdir()
    (real / "glab").write_text('#!/bin/sh\necho REAL-GLAB "$@"\n')
    (real / "glab").chmod(0o755)
    return d


def glab(bin_dir: Path, tmp: Path, *argv: str, real: str = "", readonly: bool = False):
    env = {**os.environ, "PATH": f"{bin_dir}:{tmp / 'real'}:{os.environ['PATH']}", "NOHALT_GUARD_LOG": str(tmp / "guard.log")}
    if real:
        env["NOHALT_REAL_GLAB"] = real
    if readonly:
        env["NOHALT_READONLY"] = "1"
    return subprocess.run(["glab", *argv], env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize(
    "argv",
    [
        ["mr", "merge", "3"],
        ["mr", "approve", "3"],
        ["mr", "close", "3"],
        ["issue", "close", "1"],
        ["release", "create", "v1"],
        ["repo", "delete"],
        ["api", "projects/1/merge_requests/3/merge", "-X", "PUT"],
        ["api", "projects/1/issues/1", "-X", "PUT", "-f", "state_event=close"],
    ],
)
def test_glab_refuses_accepting_or_declaring_work(glab_bin, tmp_path, argv):
    cp = glab(glab_bin, tmp_path, *argv)
    assert cp.returncode == 1 and "refused" in cp.stderr
    assert "REFUSED" in (tmp_path / "guard.log").read_text()


@pytest.mark.parametrize(
    "argv", [["mr", "create", "--fill"], ["issue", "view", "1", "--comments"], ["api", "projects/1/issues/1"]]
)
def test_glab_passes_the_agents_own_work_through(glab_bin, tmp_path, argv):
    cp = glab(glab_bin, tmp_path, *argv)
    assert cp.returncode == 0 and cp.stdout.startswith("REAL-GLAB")


@pytest.mark.parametrize(
    "argv",
    [
        ["mr", "note", "3", "-m", "x"],
        ["mr", "create", "--fill"],
        ["issue", "note", "1", "-m", "x"],
        ["api", "projects/1/issues/1/notes", "-f", "body=x"],
    ],
)
def test_a_reviewer_glab_only_reads(glab_bin, tmp_path, argv):
    cp = glab(glab_bin, tmp_path, *argv, readonly=True)
    assert cp.returncode == 1 and "reviewing" in cp.stderr


@pytest.mark.parametrize("argv", [["mr", "view", "3", "--comments"], ["mr", "diff", "3"], ["api", "projects/1/issues/1"]])
def test_a_reviewer_glab_reads_pass_through(glab_bin, tmp_path, argv):
    cp = glab(glab_bin, tmp_path, *argv, readonly=True)
    assert cp.returncode == 0 and cp.stdout.startswith("REAL-GLAB")


def test_glab_shim_never_execs_itself(glab_bin, tmp_path):
    cp = glab(glab_bin, tmp_path, "issue", "view", "1", real=str(glab_bin / "glab"))
    assert cp.returncode == 0 and cp.stdout.startswith("REAL-GLAB")


def test_init_detects_a_self_hosted_gitlab_remote(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "remote", "add", "origin", "git@gitlab.example.com:team/sub/app.git"], check=True)
    cp = subprocess.run([sys.executable, "-m", "nohalt", "init", "--dir", str(d)], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    cfg = config_mod.load(d / "nohalt.toml")
    assert cfg.project.repo == "team/sub/app"
    assert cfg.project.forge == "gitlab" and cfg.project.forge_url == "https://gitlab.example.com"


def test_unknown_merge_status_is_not_mergeable_yet(gitlab):
    """The detailed_merge_status set grows with GitLab versions; anything unknown means 'not known yet'."""
    assert gitlab.pull(5).mergeable is None


def test_doctor_helpers(gitlab):
    assert gitlab.project() == {"id": 1, "default_branch": "main"}
    assert "sudo" in gitlab.token_scopes()


def test_gitlab_prompt_offers_push_options_when_glab_is_broken(tmp_path):
    cfg = config_mod.from_dict({"project": {"repo": "grp/app", "forge": "gitlab", "base_branch": "develop"}}, root=tmp_path)
    body = prompt_mod.render(prompt_mod.template(cfg), prompt_mod.values(cfg, issue=5, title="T", branch="b", worktree=tmp_path))
    assert "-o merge_request.create -o merge_request.target=develop" in body and 'description="Closes #5"' in body


def test_mark_ready_strips_the_draft_prefix(gitlab):
    assert gitlab.pull(6).draft is True
    gitlab.mark_ready(6)
    put = next(c for c in FakeGitLab.calls if c[0] == "PUT" and c[1].endswith("/merge_requests/6"))
    assert put[2] == {"title": "add divide"}
