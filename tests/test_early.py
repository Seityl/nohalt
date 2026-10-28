"""SCH-10: a dependant starts early on its dependencies' green PRs, and never integrates before them.

Real git throughout: a bare origin, dependency branches with PRs on the local
forge, and the dependant's worktree built on them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import git
from nohalt import config as config_mod
from nohalt.cli import render_status
from nohalt.state import QUEUED, READY, WAITING_CI


def branch(repo: Path, name: str, files: dict[str, str], start: str = "main") -> str:
    """Push ``name`` (cut from ``start``) with one commit writing ``files``; return its head."""
    git(repo, "checkout", "-q", "-b", name, start)
    for path, text in files.items():
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"work on {name}")
    git(repo, "push", "-q", "origin", name)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def dep_pr(w, n: int, pr: int, name: str, sha: str, checks: str = "success") -> None:  # type: ignore[no-untyped-def]
    w.forge.add_pull(pr, name, sha, body=f"Closes #{n}")
    w.forge.set_checks(sha, [("ci", "completed", checks)])


def ready(w, n: int, pr: int, name: str, sha: str, early_on: list[dict] | None = None) -> None:  # type: ignore[no-untyped-def]
    dep_pr(w, n, pr, name, sha)
    r = w.rec(n)
    r.status, r.pr, r.pr_sha, r.branch, r.started_at = READY, pr, sha, name, 1.0
    r.early_on = early_on or []
    w.sup.state.put(r)


def remote_sha(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo.parent / "origin.git"), "rev-parse", ref], capture_output=True, text=True
    ).stdout.strip()


def test_an_early_start_builds_the_synthetic_base_with_the_dependency_commits(world, repo):
    sha = branch(repo, "feature/1-schema", {"schema.py": "TABLE = 'x'\n"})
    w = world(agents={"early_start": True})
    w.forge.add_issue(1)
    dep_pr(w, 1, 101, "feature/1-schema", sha)
    w.queue.write_text("1:\n")
    w.issue(2, "pr-green", deps=[1])
    w.sup.tick()
    w.wait_agents()

    rec = w.rec(2)
    assert rec.started_at and rec.early_on == [{"issue": 1, "pr": 101, "branch": "feature/1-schema", "sha": sha}], w.messages
    tree = Path(rec.worktree)
    assert (tree / "schema.py").read_text() == "TABLE = 'x'\n"
    log = git(tree, "log", "--format=%s", "-n5")
    assert "merge: feature/1-schema (#1, PR #101) into" in log and "(nohalt early start)" in log
    prompt = w.prompts(2)[0]
    assert "not merged into `main` yet" in prompt and "`feature/1-schema` (PR #101" in prompt
    assert any("started early on #1" in h for h in rec.history)

    w.sup.tick()  # judged: its PR goes through CI
    assert w.rec(2).status in (WAITING_CI, READY)
    assert "#2 early on #1" in render_status(w.cfg)


def test_dependencies_merge_in_dependency_order(world, repo):
    """#3 needs #1 too: #1's head is merged first, whatever order the dependant lists them in."""
    a = branch(repo, "feature/1-a", {"a.py": "a\n"})
    b = branch(repo, "feature/3-b", {"b.py": "b\n"}, start="feature/1-a")
    w = world(agents={"early_start": True})
    for n in (1, 3):
        w.forge.add_issue(n)
    dep_pr(w, 1, 101, "feature/1-a", a)
    dep_pr(w, 3, 103, "feature/3-b", b)
    w.queue.write_text("1:\n3: 1\n")
    w.issue(2, "sleep 0", deps=[3, 1])
    w.sup.tick()
    w.wait_agents()
    assert [u["issue"] for u in w.rec(2).early_on] == [1, 3], w.messages


def test_a_code_conflict_means_no_early_start(world, repo):
    a = branch(repo, "feature/1-a", {"app.py": "x = 1\n"})
    b = branch(repo, "feature/3-b", {"app.py": "x = 3\n"})
    w = world(agents={"early_start": True})
    for n in (1, 3):
        w.forge.add_issue(n)
    dep_pr(w, 1, 101, "feature/1-a", a)
    dep_pr(w, 3, 103, "feature/3-b", b)
    w.queue.write_text("1:\n3:\n")
    w.issue(2, "pr-green", deps=[1, 3])
    report = w.sup.tick()

    rec = w.rec(2)
    assert rec.status == QUEUED and not rec.started_at and not rec.early_on
    assert 2 not in report.launched and rec.early_refused and "app.py" in rec.early_refused[1]
    assert any("#2: no early start" in m and "conflicts in code (app.py)" in m for m in w.messages)
    tree = w.sup.trees.path(2)
    assert git(tree, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")  # nothing half-merged is left
    assert not git(tree, "status", "--porcelain")

    report = w.sup.tick()  # the same heads: not tried again, and the waiting line says why
    assert 2 not in report.launched
    assert "no early start" in report.deps[2] and "#1 (PR green)" in report.deps[2]


def test_the_train_will_not_batch_a_dependant_ahead_of_its_dependencies(world, repo):
    a = branch(repo, "feature/1-a", {"a.py": "a\n"})
    b = branch(repo, "feature/2-b", {"b.py": "b\n"}, start="feature/1-a")
    w = world(integrate={"mode": "train"})
    w.forge.add_issue(1)
    w.forge.add_issue(2)
    w.queue.write_text("1:\n2: 1\n")
    ready(w, 1, 101, "feature/1-a", a)
    ready(w, 2, 102, "feature/2-b", b, early_on=[{"issue": 1, "pr": 101, "branch": "feature/1-a", "sha": a}])
    w.sup.state.append_line("hold", "1")  # #1 does not go this time
    w.sup.tick()
    assert not [p for p in w.forge.open_pulls() if p.head_ref.startswith("release/")], w.messages
    assert any("#2: started early, cannot integrate yet" in m and "#1 (PR green)" in m for m in w.messages)


def test_the_train_batches_a_dependant_together_with_its_dependencies(world, repo):
    a = branch(repo, "feature/1-a", {"a.py": "a\n"})
    b = branch(repo, "feature/2-b", {"b.py": "b\n"}, start="feature/1-a")
    w = world(integrate={"mode": "train"})
    w.forge.add_issue(1)
    w.forge.add_issue(2)
    w.queue.write_text("2: 1\n1:\n")  # the dependant first in the queue: the batch still takes #1 first
    ready(w, 1, 101, "feature/1-a", a)
    ready(w, 2, 102, "feature/2-b", b, early_on=[{"issue": 1, "pr": 101, "branch": "feature/1-a", "sha": a}])
    w.sup.tick()
    rel = [p for p in w.forge.open_pulls() if p.head_ref.startswith("release/")]
    assert len(rel) == 1 and "Closes #1" in rel[0].body and "Closes #2" in rel[0].body, w.messages
    added = [m.strip() for m in w.messages if m.strip().startswith("+ #")]
    assert added == ["+ #1 (PR #101)", "+ #2 (PR #102)"]


def test_a_moved_dependency_head_leaves_a_note(world, repo):
    a = branch(repo, "feature/1-a", {"a.py": "a\n"})
    w = world(agents={"early_start": True})
    w.forge.add_issue(1)
    w.forge.add_issue(2)
    w.queue.write_text("1:\n2: 1\n")
    r = w.rec(2)
    r.status, r.started_at = QUEUED, 1.0
    r.not_before = 2**40  # not relaunched in this test
    r.early_on = [{"issue": 1, "pr": 101, "branch": "feature/1-a", "sha": a}]
    w.sup.state.put(r)
    moved = branch(repo, "feature/1-a2", {"a.py": "a2\n"}, start="feature/1-a")
    dep_pr(w, 1, 101, "feature/1-a", moved)  # the PR moved to a new head

    w.sup.tick()
    rec = w.rec(2)
    assert "Merge `origin/feature/1-a` into your branch" in rec.early_note and moved[:8] in rec.early_note
    assert rec.early_on[0]["sha"] == moved
    assert any("dependency #1 moved" in m for m in w.messages)
    w.sup.tick()  # noted once, not every tick
    assert w.rec(2).early_note.count("Dependency #1 moved") == 1

    brief = w.sup.early.brief(rec, [])  # what its next launch is told
    assert "## Your dependencies changed" in brief and not rec.early_note


def test_a_dependency_closed_unmerged_blocks_integration(world, repo):
    a = branch(repo, "feature/1-a", {"a.py": "a\n"})
    b = branch(repo, "feature/2-b", {"b.py": "b\n"}, start="feature/1-a")
    w = world(integrate={"mode": "direct"})
    w.forge.add_issue(1)
    w.forge.add_issue(2)
    w.queue.write_text("2: 1\n")
    dep_pr(w, 1, 101, "feature/1-a", a)
    w.forge.close_pull(101)
    r1 = w.rec(1)
    r1.pr, r1.branch = 101, "feature/1-a"
    w.sup.state.put(r1)
    ready(w, 2, 102, "feature/2-b", b, early_on=[{"issue": 1, "pr": 101, "branch": "feature/1-a", "sha": a}])
    w.sup.tick()
    assert 102 not in w.forge.data()["merged"]
    assert any("#2: started early, cannot integrate yet" in m and "#1 (PR closed unmerged)" in m for m in w.messages)
    assert w.rec(2).status == READY  # it keeps its place; it is not handed back


def test_direct_merges_a_dependant_only_after_its_dependencies_then_refreshes_it_onto_the_base(world, repo):
    a = branch(repo, "feature/1-a", {"a.py": "a\n"})
    b = branch(repo, "feature/2-b", {"b.py": "b\n"}, start="feature/1-a")
    w = world(integrate={"mode": "direct", "base_pipeline_grace": 0})
    w.forge.add_issue(1)
    w.forge.add_issue(2)
    w.queue.write_text("2: 1\n1:\n")  # queue order would merge #2 first
    ready(w, 1, 101, "feature/1-a", a)
    ready(w, 2, 102, "feature/2-b", b, early_on=[{"issue": 1, "pr": 101, "branch": "feature/1-a", "sha": a}])
    w.sup.tick()
    assert w.forge.data()["merged"] == [101], w.messages  # #1 merged, #2 held back

    # The real base moved on with #1 (squashed, say) and something else.
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--squash", "feature/1-a")
    git(repo, "commit", "-q", "-m", "#1 squashed")
    (repo / "other.py").write_text("o\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "other")
    git(repo, "push", "-q", "origin", "main")
    w.sup.tick()

    rec = w.rec(2)
    new = remote_sha(repo, "feature/2-b")
    assert new != b and not rec.early_on, w.messages
    assert rec.status == WAITING_CI and rec.pr_sha == new  # through CI again, on the real base
    git(repo, "fetch", "-q", "origin")
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", "origin/main", new]).returncode == 0
    assert any("its dependencies #1 have merged" in m and "refreshed onto main" in m for m in w.messages)


def test_off_by_default_and_the_waiting_line_says_what_for(world, repo):
    a = branch(repo, "feature/1-a", {"a.py": "a\n"})
    red = branch(repo, "feature/3-b", {"b.py": "b\n"})
    w = world()
    assert w.cfg.agents.early_start is False and w.cfg.agents.early_start_on == "green"
    for n in (1, 3, 4):
        w.forge.add_issue(n)
    dep_pr(w, 1, 101, "feature/1-a", a)
    dep_pr(w, 3, 103, "feature/3-b", red, checks="failure")
    w.queue.write_text("1:\n3:\n4:\n")
    for n in (1, 3, 4):
        r = w.rec(n)
        r.status, r.started_at, r.not_before = QUEUED, 1.0, 2**40  # their agents are not this test's business
        w.sup.state.put(r)
    w.issue(2, "pr-green", deps=[1, 3, 4])
    report = w.sup.tick()

    assert 2 not in report.launched and not w.rec(2).started_at
    assert report.deps[2] == "waiting for #1 (PR green), #3 (PR red), #4 (no PR)"
    assert "#2 waiting for #1 (PR green), #3 (PR red), #4 (no PR)" in render_status(w.cfg)


def test_approved_waits_for_the_review_gate(world, repo):
    a = branch(repo, "feature/1-a", {"a.py": "a\n"})
    w = world(agents={"early_start": True, "early_start_on": "approved"}, review={"enabled": True, "all": True})
    w.forge.add_issue(1)
    dep_pr(w, 1, 101, "feature/1-a", a)
    r = w.rec(1)
    r.status, r.pr, r.pr_sha, r.branch, r.started_at, r.not_before = QUEUED, 101, a, "feature/1-a", 1.0, 2**40
    w.sup.state.put(r)
    w.queue.write_text("1:\n")
    w.issue(2, "sleep 0", deps=[1])
    report = w.sup.tick()
    assert 2 not in report.launched and report.deps[2] == "waiting for #1 (PR green)"

    r = w.rec(1)
    r.review_approved = a
    w.sup.state.put(r)
    report = w.sup.tick()
    assert 2 in report.launched, w.messages
    w.wait_agents()
    assert [u["issue"] for u in w.rec(2).early_on] == [1]


def test_early_start_on_is_validated(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="early_start_on"):
        config_mod.from_dict({"agents": {"early_start_on": "merged"}}, root=tmp_path)
