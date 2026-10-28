"""AGT-20: an issue reopened on the forge is new work, not a finished record."""

from __future__ import annotations

from conftest import git
from nohalt.forge import Comment, ForgeError
from nohalt.forge.github import GitHub
from nohalt.state import DONE, QUEUED, RUNNING
from nohalt.worktree import Worktrees


def reopen(w, n: int, *comments: str) -> None:  # type: ignore[no-untyped-def]
    w.forge.set_state(n, "OPEN")
    for c in comments:
        w.forge.comment(n, c, author="human")


def test_reopened_after_a_merge_is_queued_again_on_a_fresh_branch(world, repo):
    w = world(integrate={"mode": "direct"})
    w.issue(1, "pr-green", "close")
    w.drive(4)
    r = w.rec(1)
    assert r.status == DONE and w.forge.data()["merged"] and r.done_at
    old, old_head = r.branch, git(repo, "rev-parse", f"origin/{r.branch}")
    w.forge.comment(1, "said before it was closed", author="human")
    with w.forge._db() as d:  # comments written before the close are not news
        d["comments"][-1][2] = r.done_at - 60
    reopen(w, 1, "The export still ignores the date filter.")

    w.sup.tick()
    r = w.rec(1)
    assert "#1: reopened on the forge -- queued again" in w.messages
    assert r.status == RUNNING and r.branch == f"{old}-reopened"
    w.wait_agents()
    prompt = w.prompts(1)[-1]
    assert "reopened on the forge" in prompt
    assert "The export still ignores the date filter." in prompt
    assert "said before it was closed" not in prompt
    assert f"`{old}` was merged" in prompt
    # Cut from the base, not from the merged branch, which is left alone.
    git(repo, "fetch", "-q", "origin")
    assert git(repo, "rev-parse", f"{old}-reopened") == git(repo, "rev-parse", "origin/main")
    assert git(repo, "rev-parse", f"origin/{old}") == old_head

    w.drive(2)
    assert w.rec(1).status == DONE


def test_reopened_unmerged_keeps_its_branch_resets_counters_keeps_history(world):
    w = world(agents={"max_attempts": 3})
    w.issue(1, "nothing", "pr-pending", "close")
    w.drive(3)
    r = w.rec(1)
    assert r.status not in (DONE, QUEUED) and r.attempts == 1 and r.pr
    w.forge.set_state(1, "CLOSED")  # a human closes it; the PR never merged
    w.sup._issues.clear()
    w.drive(1)
    r = w.rec(1)
    assert r.status == DONE
    branch, history = r.branch, list(r.history)

    reopen(w, 1, "Not done: the CSV header is missing.")
    w.sup.tick()
    r = w.rec(1)
    assert r.status == RUNNING and r.branch == branch
    assert r.attempts == 0 and r.ci_rounds == 0 and r.provider_retries == 0
    assert r.history[: len(history)] == history
    assert any("reopened on the forge -- queued again" in h for h in r.history)
    w.wait_agents()
    prompt = w.prompts(1)[-1]
    assert f"earlier branch `{branch}`" in prompt and "the CSV header is missing" in prompt


def test_still_closed_is_left_alone(world):
    w = world()
    w.issue(1, "close")
    w.drive(2)
    assert w.rec(1).status == DONE
    w.drive(3)
    assert w.rec(1).status == DONE
    assert len(w.prompts(1)) == 1
    assert not [m for m in w.messages if "reopened" in m]


def test_reopen_needs_an_answer_from_the_forge(world):
    """API-02: a listing that failed is no answer: the record stays done until one comes."""
    w = world()
    w.issue(1, "close", "close")
    w.drive(2)
    reopen(w, 1)
    w.forge.fail = True
    w.sup.tick()
    assert w.rec(1).status == DONE
    w.forge.fail = False
    w.sup.tick()
    assert w.rec(1).status == RUNNING


def test_a_record_from_before_done_at_is_found_and_given_every_comment(world):
    w = world()
    w.issue(1, "close", "close")
    w.drive(2)
    r = w.rec(1)
    r.done_at = 0  # written by an older nohalt
    w.sup.state.put(r)
    (w.sup.state.root / "reopen.json").unlink()  # and before any listing
    w.forge.comment(1, "an old note", author="human")
    with w.forge._db() as d:
        d["issues"]["1"]["state"] = "OPEN"
        d["issues"]["1"]["updated"] = 1.0  # long ago
    w.sup.tick()
    assert w.rec(1).status == RUNNING
    w.wait_agents()
    prompt = w.prompts(1)[-1]
    assert "when it was closed was not recorded" in prompt and "an old note" in prompt


def test_one_listing_per_tick_not_one_call_per_done_issue(world):
    """API-05: done issues cost one listing a tick between them."""
    w = world()
    for n in range(1, 6):
        w.issue(n, "close")
    w.drive(6)
    assert all(w.rec(n).status == DONE for n in range(1, 6))
    seen = []
    real = w.forge.issue
    w.forge.issue = lambda n: seen.append(n) or real(n)  # type: ignore[method-assign]
    w.sup._issues.clear()
    w.sup.tick()
    assert seen == []


def test_merged_and_next_branch(world, repo):
    w = world()
    trees = Worktrees(w.cfg)
    git(repo, "checkout", "-q", "-b", "fix/7-x")
    (repo / "x.txt").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    git(repo, "push", "-q", "origin", "fix/7-x")
    git(repo, "checkout", "-q", "main")
    assert not trees.merged("fix/7-x")
    git(repo, "push", "-q", "origin", "fix/7-x:main")
    assert trees.merged("fix/7-x")
    assert not trees.merged("fix/7-gone")
    assert trees.next_branch("fix/7-x") == "fix/7-x-reopened"
    git(repo, "branch", "fix/7-x-reopened")
    assert trees.next_branch("fix/7-x") == "fix/7-x-reopened-2"
    assert trees.next_branch("fix/7-x-reopened") == "fix/7-x-reopened-2"


def test_github_lists_open_issues_since_and_new_comments(monkeypatch):
    gh = GitHub("o/r")
    seen: list[str] = []

    def api(path, **kw):
        seen.append(path)
        if "/comments" in path:
            return [
                {"user": {"login": "a"}, "body": "old, edited", "created_at": "2026-10-01T00:00:00Z"},
                {"user": {"login": "b"}, "body": "new", "created_at": "2026-10-28T10:00:00Z"},
            ]
        return [{"number": 3, "pr": False}, {"number": 4, "pr": True}]

    monkeypatch.setattr(gh, "_api", api)
    since = 1793000000.0  # 2026-10-26
    assert gh.open_issues(since) == {3}
    assert "state=open" in seen[0] and "since=2026-10-2" in seen[0]
    assert gh.comments_since(3, since) == [Comment("b", "new", 1793181600.0)]


def test_gitlab_lists_open_issues_since_and_new_comments(monkeypatch):
    from nohalt.forge.gitlab import GitLab

    gl = GitLab("grp/app", url="http://127.0.0.1:9", token="tok")
    seen: list[str] = []

    def all_(path):
        seen.append(path)
        if "/notes" in path:
            return [
                {"author": {"username": "a"}, "body": "old", "created_at": "2026-10-01T00:00:00.000Z"},
                {"author": {"username": "bot"}, "body": "reopened", "system": True, "created_at": "2026-10-28T10:00:00.000Z"},
                {"author": {"username": "b"}, "body": "new", "created_at": "2026-10-28T10:00:00.000Z"},
            ]
        return [{"iid": 3}]

    monkeypatch.setattr(gl, "_all", all_)
    since = 1793000000.0
    assert gl.open_issues(since) == {3}
    assert "state=opened" in seen[0] and "updated_after=2026-10-26T07:33:20Z" in seen[0]
    assert gl.comments_since(3, since) == [Comment("b", "new", 1793181600.0)]


def test_a_reopen_that_could_not_be_settled_is_not_lost(world):
    """The next listing starts after the reopen: a candidate the forge could not answer for stays pending."""
    w = world(integrate={"mode": "direct"})
    w.issue(1, "pr-green", "close")
    w.drive(4)
    assert w.rec(1).status == DONE
    reopen(w, 1)
    real = w.forge.pull

    def down(n):  # type: ignore[no-untyped-def]
        raise ForgeError("timeout")

    w.forge.pull = down  # type: ignore[method-assign]
    w.sup.tick()
    assert w.rec(1).status == DONE
    assert w.sup.state.load("reopen.json")["pending"] == [1]
    w.sup.state.save("reopen.json", {**w.sup.state.load("reopen.json"), "since": 4e9})  # listed nothing new since
    w.forge.pull = real  # type: ignore[method-assign]
    w.sup.tick()
    assert w.rec(1).status == RUNNING
    assert w.sup.state.load("reopen.json")["pending"] == []
