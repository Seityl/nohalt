"""Approved PRs go first: in the next batch, into an open batch, and in direct mode (INT-17)."""

from __future__ import annotations

from conftest import git
from nohalt.forge import CLOSED
from nohalt.state import READY
from test_windows import WIN, at, carried, releases, tick

REVIEW = {"enabled": True, "all": True}
PR = 500  # agent PRs: clear of the numbers the forge gives release PRs (900 on)


def make(world, train=None, review=True, mode="train"):  # type: ignore[no-untyped-def]
    integrate = {"mode": mode, "base_pipeline_grace": 0}
    if mode == "train":
        integrate["train"] = train or {}
    return world(integrate=integrate, review=REVIEW if review else {"enabled": False})


def ready(w, repo, n: int, path: str = "", labels: list[str] | None = None, text: str = "") -> str:  # type: ignore[no-untyped-def]
    """Issue n with a green PR writing ``path`` (default: a file of its own), READY. Returns its head."""
    w.forge.add_issue(n, f"Issue {n}", labels=labels)
    b = f"feature/{n}-x"
    git(repo, "checkout", "-q", "-b", b, "main")
    (repo / (path or f"f{n}.txt")).write_text(text or f"{n}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"#{n}")
    git(repo, "push", "-q", "origin", b)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    w.forge.add_pull(PR + n, b, sha, body=f"Closes #{n}")
    with w.forge._db() as d:
        d["next"] = max(d["next"], 900)  # release PRs are numbered from 900
    w.forge.set_checks(sha, [("ci", "completed", "success")])
    r = w.sup.state.get(n)
    r.status, r.pr, r.pr_sha, r.branch = READY, PR + n, sha, b
    w.sup.state.put(r)
    with open(w.queue, "a") as f:
        f.write(f"{n}:\n")
    return sha


def approve(w, n: int, when: float) -> None:  # type: ignore[no-untyped-def]
    r = w.sup.state.get(n)
    r.review_verdict, r.review_approved, r.review_approved_at = "approve", r.pr_sha, when
    w.sup.state.put(r)


def batch(w):  # type: ignore[no-untyped-def]
    """The issues the open release carries, in the order they were merged into it."""
    (rel,) = w.sup.state.load("train.json")["releases"].values()
    return [int(c[0]) for c in rel["carried"]]


def on_release(w, repo, path: str) -> str:  # type: ignore[no-untyped-def]
    (rel,) = releases(w)
    git(repo, "fetch", "-q", "origin")
    return git(repo, "show", f"origin/{rel.head_ref}:{path}")


# -- the next batch ------------------------------------------------------------------------------


def test_an_approved_pr_is_batched_first(world, repo):
    w = make(world)
    for n in (1, 2, 3):
        ready(w, repo, n)
    approve(w, 3, 100)
    tick(w, at(14))
    assert batch(w) == [3, 1, 2]


def test_the_order_is_priority_then_approved_in_approval_order_then_the_rest(world, repo):
    w = make(world)
    for n in (1, 2, 3, 4, 5):
        ready(w, repo, n)
    (w.sup.state.root / "priority").write_text("5\n")
    approve(w, 3, 200)
    approve(w, 2, 100)  # approved before #3
    approve(w, 5, 50)  # on the priority list: it stays there, first
    tick(w, at(14))
    assert batch(w) == [5, 2, 3, 1, 4]


def test_an_approval_for_an_older_head_does_not_count(world, repo):
    w = make(world)
    for n in (1, 2):
        ready(w, repo, n)
    r = w.sup.state.get(2)
    r.review_approved, r.review_approved_at = "0" * 40, 100  # approved, then pushed again
    w.sup.state.put(r)
    tick(w, at(14))
    assert batch(w) == [1, 2]


def test_turning_it_off_restores_queue_order(world, repo):
    w = make(world, train={"approved_first": False})
    for n in (1, 2, 3):
        ready(w, repo, n)
    approve(w, 3, 100)
    tick(w, at(14))
    assert batch(w) == [1, 2, 3]


def test_without_review_approvals_change_nothing(world, repo):
    w = make(world, review=False)
    for n in (1, 2, 3):
        ready(w, repo, n)
    approve(w, 3, 100)
    tick(w, at(14))
    assert batch(w) == [1, 2, 3]


# -- the open batch ------------------------------------------------------------------------------


def test_a_pr_approved_during_assembly_joins_the_open_batch(world, repo):
    w = make(world)
    ready(w, repo, 1)
    tick(w, at(14))
    (rel,) = releases(w)
    assert batch(w) == [1]
    ready(w, repo, 2)
    approve(w, 2, 100)
    w.forge.set_checks(rel.head_sha, [("ci", "queued", "")])  # waiting for a runner: CI has not started
    tick(w, at(14, 1))
    assert batch(w) == [1, 2]
    assert on_release(w, repo, "f2.txt") == "2"
    assert any("#2 joined this release" in body for _n, body in w.forge.data()["pull_comments"])
    assert any("joined the open release" in m for m in w.messages)
    # It merges with the batch and closes both, as if it had been there from the start.
    w.forge.set_checks(rel.head_sha, [("ci", "completed", "success")])
    tick(w, at(14, 2))
    assert rel.number in w.forge.data()["merged"]
    assert {w.forge.issue(n).state for n in (1, 2)} == {CLOSED}
    assert w.forge.data()["pulls"]["502"]["state"] == "closed"


def test_an_unapproved_pr_does_not_join(world, repo):
    w = make(world)
    ready(w, repo, 1)
    tick(w, at(14))
    ready(w, repo, 2)
    calls = w.forge.calls
    tick(w, at(14, 1))
    assert batch(w) == [1]
    assert w.forge.calls - calls <= 3  # nothing approved waits: no candidate scan while a release is open


def test_once_the_batch_ci_started_an_approved_pr_waits_and_goes_first_next(world, repo):
    w = make(world)
    ready(w, repo, 1)
    tick(w, at(14))
    (rel,) = releases(w)
    w.forge.set_checks(rel.head_sha, [("ci", "in_progress", "")])
    ready(w, repo, 3)
    ready(w, repo, 2)
    approve(w, 2, 100)
    tick(w, at(14, 1))
    assert batch(w) == [1]
    w.forge.set_checks(rel.head_sha, [("ci", "completed", "success")])
    tick(w, at(14, 2))  # merged
    assert releases(w) == []
    tick(w, at(14, 3))
    assert batch(w) == [2, 3]


def test_an_approved_pr_that_conflicts_with_the_batch_waits_for_the_next(world, repo):
    w = make(world)
    ready(w, repo, 1, path="shared.txt")
    tick(w, at(14))
    sha = ready(w, repo, 2, path="shared.txt")
    approve(w, 2, 100)
    tick(w, at(14, 1))
    assert batch(w) == [1]
    assert w.sup.state.get(2).status == READY  # not handed back: the batch it conflicts with is not the base
    assert any("first in the next batch" in m for m in w.messages)
    (rel,) = w.sup.state.load("train.json")["releases"].values()
    assert rel["join_tried"] == [sha]
    said = len(w.messages)
    tick(w, at(14, 2))
    assert len(w.messages) == said  # tried once per head


def test_a_hotfix_release_takes_only_hotfixes(world, repo):
    w = make(world, train=WIN)
    ready(w, repo, 1, labels=["hotfix"])
    tick(w, at(14))  # outside the window: a hotfix release
    (rel,) = releases(w)
    assert carried(rel) == {1}
    ready(w, repo, 2)
    ready(w, repo, 3, labels=["hotfix"])
    approve(w, 2, 100)
    approve(w, 3, 200)
    tick(w, at(14, 1))
    assert batch(w) == [1, 3]


def test_off_nothing_joins(world, repo):
    w = make(world, train={"approved_first": False})
    ready(w, repo, 1)
    tick(w, at(14))
    ready(w, repo, 2)
    approve(w, 2, 100)
    tick(w, at(14, 1))
    assert batch(w) == [1]


def test_an_approval_records_when_it_was_given(world, repo, clock):
    w = make(world)
    ready(w, repo, 1)
    r = w.sup.state.get(1)
    r.review_sha, r.review_model = r.pr_sha, "command:fake"
    w.sup.reviews._approve(r, "fine\nNOHALT-REVIEW: approve")
    r = w.sup.state.get(1)
    assert r.review_approved == r.pr_sha and abs(r.review_approved_at - clock()) < 5


# -- direct --------------------------------------------------------------------------------------


def test_direct_merges_an_approved_pr_ahead_of_the_queue(world, repo):
    w = make(world, mode="direct")
    for n in (1, 2, 3):
        ready(w, repo, n)
    approve(w, 3, 200)
    approve(w, 2, 100)
    w.sup.integrator.tick()
    assert w.forge.data()["merged"] == [502]


def test_direct_off_merges_in_queue_order(world, repo):
    w = make(world, mode="direct", review=False)
    for n in (1, 2):
        ready(w, repo, n)
    approve(w, 2, 100)
    w.sup.integrator.tick()
    assert w.forge.data()["merged"] == [501]


def test_a_joining_pr_keeps_the_batch_version(world, repo):
    (repo / "VERSION").write_text("0.4.0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "version")
    git(repo, "push", "-q", "origin", "main")
    w = make(world, train={"version_files": [{"path": "VERSION", "pattern": r"^(\S+)$"}]})
    ready(w, repo, 1)
    tick(w, at(14))
    assert on_release(w, repo, "VERSION") == "0.4.1"
    ready(w, repo, 2, path="VERSION", text="0.9.0\n")  # an agent that bumped the version itself
    approve(w, 2, 100)
    tick(w, at(14, 1))
    assert batch(w) == [1, 2]
    assert on_release(w, repo, "VERSION") == "0.4.1"
