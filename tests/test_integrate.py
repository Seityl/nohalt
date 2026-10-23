"""Integrators: the only thing that merges (INT-*, DEP-*)."""

from __future__ import annotations

import json
import subprocess

from conftest import git
from nohalt.forge import Run
from nohalt.state import DONE, READY


def test_direct_merges_green_then_watches_the_pipeline(world):
    w = world(integrate={"mode": "direct", "ci_workflow": "CI"})
    w.issue(1, "pr-green")
    w.drive(3)
    assert w.rec(1).status in (READY, DONE)
    w.drive(2)
    d = w.forge.data()
    assert d["merged"], "the integrator merged the green PR"
    assert w.forge.issue(1).state == "CLOSED"
    assert w.rec(1).status == DONE
    st = w.sup.state.load("integrate.json", {})
    assert "watch" in st  # waiting for CI on the base branch


def test_direct_red_pipeline_stops_merging_and_opens_fix_forward(world):
    """DEP-01: nothing builds on a pipeline that cannot deploy."""
    w = world(integrate={"mode": "direct", "ci_workflow": "CI"})
    w.issue(1, "pr-green")
    w.issue(2, "pr-green")
    w.drive(4)
    st = w.sup.state.load("integrate.json", {})
    sha = st["watch"]["sha"]
    merged_before = list(w.forge.data()["merged"])
    w.forge.add_run(Run(1, "CI", sha, "completed", "failure", "main", ""))
    w.drive(2)
    st = w.sup.state.load("integrate.json", {})
    fix = st.get("fix_issue")
    assert fix, w.messages
    assert w.queue.read_text().splitlines()[0].startswith(f"{fix}:")
    assert w.forge.data()["merged"] == merged_before  # nothing else merged on top of red


def test_direct_never_merges_red(world):
    w = world(integrate={"mode": "direct"}, agents={"ci_rounds": 0})
    w.issue(1, "pr-red")
    w.drive(4)
    assert not w.forge.data()["merged"]


def test_train_batches_resolves_bookkeeping_and_versions(world, repo):
    """INT-01: batch green PRs; bookkeeping conflicts by rule; version the batch."""
    (repo / "pkg").mkdir()
    (repo / "pkg/__init__.py").write_text('__version__ = "0.3.0"\n')
    (repo / "patches.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "version")
    git(repo, "tag", "v0.3.0")
    git(repo, "push", "-q", "origin", "main", "--tags")
    w = world(
        integrate={
            "mode": "train",
            "mergers": {"patches.txt": "keep_both_lines"},
            "train": {"version_files": [{"path": "pkg/__init__.py", "pattern": '__version__ = "([^"]+)"'}]},
        }
    )
    # Two branches that both append to patches.txt (a bookkeeping conflict).
    for n in (1, 2):
        w.forge.add_issue(n, f"Issue {n}")
        b = f"feature/{n}-x"
        git(repo, "checkout", "-q", "-b", b, "main")
        (repo / "patches.txt").write_text(f"base\npatch-{n}\n")
        (repo / f"f{n}.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"#{n}")
        git(repo, "push", "-q", "origin", b)
        sha = git(repo, "rev-parse", "HEAD")
        w.forge.add_pull(100 + n, b, sha, body=f"Closes #{n}")
        w.forge.set_checks(sha, [("ci", "completed", "success")])
        r = w.sup.state.get(n)
        r.status, r.pr, r.pr_sha, r.branch = READY, 100 + n, sha, b
        w.sup.state.put(r)
    git(repo, "checkout", "-q", "main")
    w.queue.write_text("1:\n2:\n")
    w.sup.tick()
    rel = [p for p in w.forge.open_pulls() if p.head_ref.startswith("release/")]
    assert len(rel) == 1, w.messages
    assert "Closes #1" in rel[0].body and "Closes #2" in rel[0].body
    origin = repo.parent / "origin.git"
    blob = subprocess.run(
        ["git", "-C", str(origin), "show", f"{rel[0].head_ref}:patches.txt"], capture_output=True, text=True
    ).stdout
    assert blob.split() == ["base", "patch-1", "patch-2"]
    ver = subprocess.run(
        ["git", "-C", str(origin), "show", f"{rel[0].head_ref}:pkg/__init__.py"], capture_output=True, text=True
    ).stdout
    assert '"0.3.1"' in ver
    st = w.sup.state.load("train.json")
    assert st["releases"][rel[0].head_ref]["version"] == "v0.3.1"


def test_train_code_conflict_is_left_out_and_handed_back(world, repo):
    (repo / "app.py").write_text("x = 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "app")
    git(repo, "push", "-q", "origin", "main")
    w = world(integrate={"mode": "train"})
    for n in (1, 2):
        w.forge.add_issue(n)
        b = f"feature/{n}-x"
        git(repo, "checkout", "-q", "-b", b, "main")
        (repo / "app.py").write_text(f"x = {n}\n")
        git(repo, "commit", "-q", "-am", f"#{n}")
        git(repo, "push", "-q", "origin", b)
        sha = git(repo, "rev-parse", "HEAD")
        w.forge.add_pull(100 + n, b, sha, body=f"Closes #{n}")
        w.forge.set_checks(sha, [("ci", "completed", "success")])
        r = w.sup.state.get(n)
        r.status, r.pr, r.pr_sha, r.branch = READY, 100 + n, sha, b
        w.sup.state.put(r)
    git(repo, "checkout", "-q", "main")
    w.queue.write_text("1:\n2:\n")
    w.sup.tick()
    w.wait_agents()
    rel = [p for p in w.forge.open_pulls() if p.head_ref.startswith("release/")]
    assert len(rel) == 1 and "Closes #1" in rel[0].body and "Closes #2" not in rel[0].body
    comments = [c for c in w.forge.data()["comments"] if c[0] == 2]
    assert comments and "conflicts with `main` in code" in comments[-1][1]


def test_train_red_release_reruns_once_then_parks(world, repo):
    w = world(integrate={"mode": "train"})
    w.forge.add_issue(1)
    sha = "abc123"
    w.forge.add_pull(50, "release/auto-1", sha, body="Closes #1")
    w.forge.set_checks(sha, [("ci", "completed", "failure")])
    w.forge.add_run(Run(9, "CI", sha, "completed", "failure", "release/auto-1", "u"))
    w.sup.state.save(
        "train.json", {"releases": {"release/auto-1": {"version": "v1", "carried": [[1, 7, "s1"]]}}, "parked": [], "reruns": {}}
    )
    w.sup.integrator.tick()
    assert json.loads(w.forge_path.read_text())["reruns"] == [9]
    w.sup._pulls, w.sup._pulls_ok = None, False
    w.sup.integrator.tick()
    st = w.sup.state.load("train.json")
    assert "s1" in st["parked"]
    assert w.forge.data()["pulls"]["50"]["state"] == "closed"


def test_train_network_red_is_not_charged_to_the_code(world):
    w = world(integrate={"mode": "train", "train": {"red_retries": 0}})
    sha = "def456"
    w.forge.add_pull(50, "release/auto-2", sha)
    w.forge.set_checks(sha, [("ci", "completed", "failure")])
    w.forge.add_run(Run(9, "CI", sha, "completed", "failure", "release/auto-2", ""))
    with w.forge._db() as d:
        d["logs"] = {"9": "fatal: Could not resolve host: github.com"}
    w.sup.integrator.tick()
    assert w.forge.data()["pulls"]["50"]["state"] == "open"
    assert w.forge.data()["reruns"] == [9]


def test_merge_into_a_non_default_base_closes_the_issue(world, repo):
    """Closing keywords act only on the default branch; the merge still finishes the issue."""
    git(repo, "push", "-q", "origin", "main:develop")
    w = world(project={"base_branch": "develop"}, integrate={"mode": "direct"})
    w.issue(1, "pr-green")
    w.drive(5)
    assert w.forge.data()["merged"]
    assert w.forge.issue(1).state == "CLOSED"
    assert w.rec(1).status == DONE


def test_human_merge_into_a_non_default_base_finishes_the_issue(world, repo):
    git(repo, "push", "-q", "origin", "main:develop")
    w = world(project={"base_branch": "develop"})
    w.issue(1, "pr-green")
    w.drive(3)
    assert w.rec(1).status == READY
    w.forge.merge_pull(w.rec(1).pr, "merge")  # a human merges; the forge leaves the issue open
    assert w.forge.issue(1).state == "OPEN"
    w.drive(2)
    assert w.forge.issue(1).state == "CLOSED" and w.rec(1).status == DONE


def test_failed_deploy_stops_merging(world):
    w = world(integrate={"mode": "direct"})
    w.sup.state.set_flag("DEPLOY_BLOCKED", "deploy of x exited 1")
    w.issue(1, "pr-green")
    w.drive(4)
    assert not w.forge.data()["merged"]
    assert any("merging stopped" in m for m in w.messages)


def test_direct_waits_while_mergeability_is_unknown(world):
    w = world(integrate={"mode": "direct"})
    w.issue(1, "pr-green")
    w.sup.tick()
    w.wait_agents()
    with w.forge._db() as d:
        for p in d["pulls"].values():
            p["mergeable"] = None
    w.drive(3)
    assert not w.forge.data()["merged"]


def test_no_pipeline_on_the_base_is_not_a_red_pipeline(world):
    """Many integration branches run no pipeline after a merge: nothing to watch is not a failure."""
    w = world(integrate={"mode": "direct", "base_pipeline_grace": 0})
    w.issue(1, "pr-green")
    w.issue(2, "pr-green")
    w.drive(8)
    assert len(w.forge.data()["merged"]) == 2
    assert not w.sup.state.load("integrate.json", {}).get("fix_issue")
    assert any("no pipeline ran" in m for m in w.messages)
    assert not any("pipeline green" in m for m in w.messages)  # one message, not a contradictory pair


def test_inline_issue_puts_the_issue_text_in_the_prompt(world):
    w = world(prompt={"inline_issue": True})
    w.issue(1, "close")
    with w.forge._db() as d:
        d["issues"]["1"]["body"] = "The export button does nothing."
    w.drive(2)
    assert "The export button does nothing." in w.prompts(1)[0]


def test_train_closes_the_prs_it_carried_once_the_release_merges(world, repo):
    """The release merges each PR's branch, not the PR: left alone, every carried PR stays open forever."""
    w = world(integrate={"mode": "train"})
    for n in (1, 2):
        w.forge.add_issue(n, f"Issue {n}")
        b = f"feature/{n}-x"
        git(repo, "checkout", "-q", "-b", b, "main")
        (repo / f"f{n}.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"#{n}")
        git(repo, "push", "-q", "origin", b)
        sha = git(repo, "rev-parse", "HEAD")
        w.forge.add_pull(100 + n, b, sha, body=f"Closes #{n}")
        w.forge.set_checks(sha, [("ci", "completed", "success")])
        r = w.sup.state.get(n)
        r.status, r.pr, r.pr_sha, r.branch = READY, 100 + n, sha, b
        w.sup.state.put(r)
    git(repo, "checkout", "-q", "main")
    w.queue.write_text("1:\n2:\n")
    w.sup.tick()  # builds the release
    rel = next(p for p in w.forge.open_pulls() if p.head_ref.startswith("release/"))
    w.forge.set_checks(rel.head_sha, [("ci", "completed", "success")])
    w.sup._pulls, w.sup._pulls_ok = None, False
    w.sup.integrator.tick()  # merges it
    pulls = w.forge.data()["pulls"]
    assert pulls[str(rel.number)]["state"] == "merged"
    assert pulls["101"]["state"] == "closed" and pulls["102"]["state"] == "closed"
    notes = [c[1] for c in w.forge.data()["comments"] if c[0] in (101, 102)]
    assert len(notes) == 2 and all("merged into" in t for t in notes)


def _merged_and_watching(w):
    """Run one green PR through direct integration; return the base sha the gate now watches."""
    w.issue(1, "pr-green")
    w.issue(2, "pr-green")
    w.drive(4)
    st = w.sup.state.load("integrate.json", {})
    assert "watch" in st, w.messages
    return st["watch"]["sha"]


def test_a_suite_ignored_on_prs_still_gates_the_base(world):
    """DEP-01: a suite skipped on PRs by policy counts on the base after a merge -- red opens fix-forward."""
    w = world(integrate={"mode": "direct", "ignore_checks": ["full suite"]})
    sha = _merged_and_watching(w)
    merged = list(w.forge.data()["merged"])
    w.forge.set_checks(sha, [("ci", "completed", "success"), ("full suite", "completed", "failure")])
    w.drive(2)
    st = w.sup.state.load("integrate.json", {})
    assert st.get("fix_issue"), w.messages
    assert not any("pipeline green" in m for m in w.messages)
    assert w.forge.data()["merged"] == merged  # merging held


def test_base_ignore_checks_opts_a_check_out_on_the_base(world):
    w = world(integrate={"mode": "direct", "ignore_checks": ["lint"], "base_ignore_checks": ["lint"]})
    sha = _merged_and_watching(w)
    w.forge.set_checks(sha, [("ci", "completed", "success"), ("lint", "completed", "failure")])
    w.drive(2)
    assert any("pipeline green" in m for m in w.messages)


def test_a_suite_ignored_on_prs_is_still_ignored_on_a_pr(world):
    w = world(integrate={"mode": "direct", "ignore_checks": ["full suite"]})
    w.forge.add_issue(1, "Issue 1")
    (w.scripts / "1.txt").write_text("close\n")
    w.queue.write_text("1:\n")
    w.forge.add_pull(501, "feature/1-x", "sha-1", body="Closes #1")
    w.forge.set_checks("sha-1", [("ci", "completed", "success"), ("full suite", "completed", "failure")])
    w.drive(2)
    assert 501 in w.forge.data()["merged"]


def test_cancelled_base_runs_are_no_verdict_rerun_then_flagged(world):
    """A cancelled base run is neither green nor red: re-run it, and after three in a row hold for a human."""
    w = world(integrate={"mode": "direct"})
    sha = _merged_and_watching(w)
    merged = list(w.forge.data()["merged"])
    w.forge.add_run(Run(77, "Tests", sha, "completed", "cancelled", "main", ""))
    for attempt in (1, 2, 3):
        w.forge.set_checks(sha, [("ci", "completed", "success"), ("full suite", "completed", "cancelled")])
        w.drive(1)
        if attempt < 3:
            assert w.forge.data()["reruns"].count(77) == attempt
            w.forge.set_checks(sha, [("ci", "completed", "success"), ("full suite", "in_progress", "")])
            w.drive(1)  # the re-run started
    assert w.sup.state.has("BASE_NO_VERDICT")
    st = w.sup.state.load("integrate.json", {})
    assert not st.get("fix_issue")  # no verdict is not a red
    assert not any("pipeline green" in m for m in w.messages)
    assert w.forge.data()["merged"] == merged  # held
    w.sup.state.clear_flag("BASE_NO_VERDICT")  # a human looked
    w.drive(4)
    assert len(w.forge.data()["merged"]) > len(merged)


def test_a_cancelled_ci_workflow_run_is_not_a_failure(world):
    w = world(integrate={"mode": "direct", "ci_workflow": "CI"})
    sha = _merged_and_watching(w)
    w.forge.add_run(Run(5, "CI", sha, "completed", "cancelled", "main", ""))
    w.drive(1)
    assert not w.sup.state.load("integrate.json", {}).get("fix_issue")
    assert 5 in w.forge.data()["reruns"]


def test_forge_managed_jobs_on_the_base_do_not_count(world):
    """Dependabot update jobs post their own (often red) check runs on the base head: not the merge's pipeline."""
    w = world(integrate={"mode": "direct"})
    sha = _merged_and_watching(w)
    w.forge.set_checks(sha, [("ci", "completed", "success", "", 10), ("Dependabot", "completed", "failure", "", 99)])
    with w.forge._db() as d:
        d["managed_suites"] = {sha: [99]}
    w.drive(2)
    assert any("pipeline green" in m for m in w.messages)
    assert not w.sup.state.load("integrate.json", {}).get("fix_issue")


def test_only_managed_jobs_on_the_base_means_no_pipeline(world):
    w = world(integrate={"mode": "direct"})
    sha = _merged_and_watching(w)
    w.cfg.integrate.base_pipeline_grace = 0
    w.forge.set_checks(sha, [("Dependabot", "completed", "failure", "", 99)])
    with w.forge._db() as d:
        d["managed_suites"] = {sha: [99]}
    w.drive(2)
    assert any("no pipeline ran" in m for m in w.messages)
    assert not w.sup.state.load("integrate.json", {}).get("fix_issue")


def test_base_is_not_green_while_a_workflow_run_is_unfinished(world):
    """A job that waits on another (or a late workflow) has no check run yet: green over a partial set is not green."""
    w = world(integrate={"mode": "direct"})
    sha = _merged_and_watching(w)
    w.forge.set_checks(sha, [("build", "completed", "success")])
    w.forge.add_run(Run(41, "Tests", sha, "in_progress", "", "main", "", "push"))
    w.drive(2)
    assert not any("pipeline green" in m for m in w.messages)
    with w.forge._db() as d:
        for r in d["runs"]:
            if r["id"] == 41:
                r["status"], r["conclusion"] = "completed", "success"
    w.forge.set_checks(sha, [("build", "completed", "success"), ("tests", "completed", "success")])
    w.drive(1)
    assert any("pipeline green" in m and "build ✓" in m and "tests ✓" in m for m in w.messages)


def test_a_managed_run_in_progress_does_not_hold_the_verdict(world):
    w = world(integrate={"mode": "direct"})
    sha = _merged_and_watching(w)
    w.forge.set_checks(sha, [("build", "completed", "success")])
    w.forge.add_run(Run(42, "Dependabot", sha, "in_progress", "", "main", "", "dynamic"))
    w.drive(1)
    assert any("pipeline green" in m for m in w.messages)


def test_a_started_run_without_checks_yet_is_not_no_pipeline(world):
    w = world(integrate={"mode": "direct"})
    sha = _merged_and_watching(w)
    w.cfg.integrate.base_pipeline_grace = 0
    w.forge.add_run(Run(43, "Tests", sha, "queued", "", "main", "", "push"))
    w.drive(2)
    assert not any("no pipeline ran" in m for m in w.messages)
