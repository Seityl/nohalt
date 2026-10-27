"""Async workflows: watched on the base, never gating; one self-closing issue each (DEP-11, DEP-12, AGT-19)."""

from __future__ import annotations

import pytest

from conftest import World
from nohalt import agents
from nohalt.cli import render_status
from nohalt.config import ConfigError
from nohalt.config import from_dict as config_from_dict
from nohalt.forge import Run
from nohalt.integrate.async_watch import failing_tests
from nohalt.state import AWAITING_GREEN, BLOCKED, DONE, QUEUED
from nohalt.supervisor import Supervisor

PYTEST_LOG = """Tests\tRun pytest\t2026-10-27T03:00:00Z FAILED tests/test_a.py::test_one - AssertionError
Tests\tRun pytest\t2026-10-27T03:00:00Z FAILED tests/test_b.py::test_two - KeyError
Tests\tRun pytest\t2026-10-27T03:00:01Z ===== 2 failed, 900 passed in 6600s =====
"""


def _async(world, **integrate):  # type: ignore[no-untyped-def]
    return world(integrate={"async_workflows": ["Tests"], **integrate})


def _red(w: World, run_id: int, sha: str, log: str = PYTEST_LOG) -> None:
    w.forge.add_run(Run(run_id, "Tests", sha, "completed", "failure", "main", f"https://ci/runs/{run_id}", "push", run_id))
    w.forge.set_checks(sha, [("build", "completed", "success", "", 1), ("pytest", "completed", "failure", "", run_id)])
    w.forge.set_log(run_id, log)


def _green(w: World, run_id: int, sha: str) -> None:
    w.forge.add_run(Run(run_id, "Tests", sha, "completed", "success", "main", f"https://ci/runs/{run_id}", "push", run_id))


def _issues(w: World) -> dict[str, dict]:  # type: ignore[type-arg]
    return {n: i for n, i in w.forge.data()["issues"].items() if "Tests red" in i["title"]}


def _comments(w: World, n: int) -> list[str]:
    return [c[1] for c in w.forge.data()["comments"] if c[0] == n]


def test_a_red_run_opens_one_issue_a_second_comments_and_green_closes_it(world):
    """DEP-12: one root cause once filed six issues, one per red SHA. One open issue per workflow; green closes it."""
    w = _async(world)
    _red(w, 10, "aaa1111")
    w.sup.async_watch.tick()
    issues = _issues(w)
    assert len(issues) == 1, w.messages
    n = int(next(iter(issues)))
    body = issues[str(n)]["body"]
    assert issues[str(n)]["title"] == "Tests red on main at aaa1111"
    assert "async-red" in issues[str(n)]["labels"]
    assert "https://ci/runs/10" in body and "tests/test_a.py::test_one" in body and "`pytest`" in body
    assert "build" not in body.split("Failing jobs:")[1].split("\n")[0]  # another workflow's job is not this run's
    assert "NOHALT-RESULT: no-change" in body
    assert w.queue.read_text().splitlines()[0] == f"{n}:"  # front of the queue

    w.sup.async_watch.tick()  # the same run again: nothing new
    assert _comments(w, n) == []

    _red(w, 11, "bbb2222", PYTEST_LOG.replace("test_two", "test_three"))
    w.sup.async_watch.tick()
    assert len(_issues(w)) == 1  # commented, not opened
    note = _comments(w, n)[-1]
    assert "red again" in note and "https://ci/runs/11" in note
    assert "tests/test_b.py::test_three" in note.split("No longer failing")[0]  # new
    assert "tests/test_b.py::test_two" in note.split("No longer failing")[1]  # gone

    _green(w, 12, "ccc3333")
    w.sup.async_watch.tick()
    assert w.forge.issue(n).state == "CLOSED"
    assert "green" in _comments(w, n)[-1] and "https://ci/runs/12" in _comments(w, n)[-1]
    assert "issue" not in w.sup.state.load("async.json")["Tests"]

    _red(w, 13, "ddd4444")  # red after the issue closed: a new issue, with the new run
    w.sup.async_watch.tick()
    assert len(_issues(w)) == 2


def test_an_unreadable_issue_is_never_a_reason_to_open_another(world):
    w = _async(world)
    _red(w, 10, "aaa1111")
    w.sup.async_watch.tick()
    _red(w, 11, "bbb2222")
    w.sup.issue = lambda n, fresh=False: type("I", (), {"state": "UNKNOWN"})()  # type: ignore[method-assign]
    w.sup.async_watch.tick()
    assert len(_issues(w)) == 1
    assert w.sup.state.load("async.json")["Tests"]["last"] == "10:failure"  # retried next tick


def test_cancelled_and_pull_request_runs_are_not_a_verdict(world):
    w = _async(world)
    w.forge.add_run(Run(10, "Tests", "a1", "completed", "cancelled", "main", "", "push"))
    w.forge.add_run(Run(11, "Tests", "a2", "completed", "failure", "main", "", "pull_request"))  # a fork's main
    w.forge.add_run(Run(12, "Tests", "a3", "completed", "failure", "feature/9-x", "", "push"))
    w.forge.add_run(Run(13, "Tests", "a4", "in_progress", "", "main", "", "push"))
    w.sup.async_watch.tick()
    assert _issues(w) == {}


def test_green_leaves_the_issues_open_pr_and_says_so(world):
    w = _async(world)
    _red(w, 10, "aaa1111")
    w.sup.async_watch.tick()
    n = int(next(iter(_issues(w))))
    w.forge.add_pull(77, f"fix/{n}-tests-red", "fixsha", body=f"Closes #{n}")
    _green(w, 11, "bbb2222")
    w.sup.async_watch.tick()
    assert w.forge.issue(n).state == "CLOSED"
    assert "PR #77 is still open and not merged" in _comments(w, n)[-1]
    assert w.forge.data()["pulls"]["77"]["state"] == "open"


def test_an_async_workflow_never_gates_the_base(world):
    """DEP-11: the slow suite is watched, not waited for. Its red run's jobs do not make the base gate red."""
    w = _async(world, mode="direct")
    w.issue(1, "pr-green")
    w.issue(2, "pr-green")
    w.drive(4)
    sha = w.sup.state.load("integrate.json")["watch"]["sha"]
    w.forge.add_run(Run(30, "Tests", sha, "in_progress", "", "main", "", "push", 30))
    w.forge.add_run(Run(31, "CI", sha, "completed", "success", "main", "", "push", 31))
    w.forge.set_checks(sha, [("build", "completed", "success", "", 31), ("pytest", "in_progress", "", "", 30)])
    w.drive(2)
    assert any("pipeline green" in m for m in w.messages), w.messages
    assert not w.sup.state.load("integrate.json", {}).get("fix_issue")


def test_a_workflow_cannot_both_gate_and_not(tmp_path):
    with pytest.raises(ConfigError, match="gates or it does not"):
        config_from_dict({"integrate": {"ci_workflow": "Tests", "async_workflows": ["Tests"]}}, root=tmp_path)


# -- an agent's "nothing to change" ---------------------------------------------------------------


def _async_issue_worked(w: World, *script: str) -> int:
    _red(w, 10, "aaa1111")
    w.sup.async_watch.tick()
    n = int(next(iter(_issues(w))))
    (w.scripts / f"{n}.txt").write_text("\n".join(script) + "\n")
    return n


def test_no_change_on_an_async_issue_awaits_green_and_survives_a_restart(world):
    """AGT-19: 'already fixed on the base' is not a block. No attempt, no human: the next green run closes it."""
    w = _async(world)
    n = _async_issue_worked(w, "no-change fixed on main by abc1234")
    w.drive(3)
    rec = w.rec(n)
    assert rec.status == AWAITING_GREEN, rec.history
    assert rec.attempts == 0
    assert "nohalt-blocked" not in w.forge.issue(n).labels
    assert any("fixed on main by abc1234" in c for c in _comments(w, n))
    assert len(w.prompts(n)) == 1  # not relaunched while it waits
    assert f"#{n}" in render_status(w.cfg).split("awaiting a green run:")[1]

    # A new supervisor (a restart) picks it up from disk.
    w.sup = Supervisor(w.cfg, forge=w.forge, say=w.messages.append)
    w.drive(2)
    assert w.rec(n).status == AWAITING_GREEN
    assert len(w.prompts(n)) == 1
    _green(w, 11, "bbb2222")
    w.drive(2)
    assert w.forge.issue(n).state == "CLOSED"
    assert w.rec(n).status == DONE


def test_a_red_run_with_failures_the_agent_has_not_seen_relaunches_it(world):
    w = _async(world)
    n = _async_issue_worked(w, "no-change already fixed", "no-change also fixed")
    w.drive(3)
    assert w.rec(n).status == AWAITING_GREEN
    _red(w, 11, "bbb2222", PYTEST_LOG)  # the same failures
    w.drive(2)
    assert w.rec(n).status == AWAITING_GREEN and len(w.prompts(n)) == 1
    assert "Still waiting for a green run" in _comments(w, n)[-1]

    _red(w, 12, "ccc3333", PYTEST_LOG + "FAILED tests/test_c.py::test_new - boom\n")
    w.sup.async_watch.tick()
    assert w.rec(n).status == QUEUED
    assert w.rec(n).fresh
    w.drive(3)
    prompts = w.prompts(n)
    assert len(prompts) == 2
    assert "tests/test_c.py::test_new" in prompts[1] and "https://ci/runs/12" in prompts[1]
    assert w.rec(n).status == AWAITING_GREEN and w.rec(n).attempts == 0


def test_no_change_on_any_other_issue_is_blocked_with_the_reason(world):
    """AGT-19: elsewhere a human decides, and reads why. Explicit, not 'no pull request after 2 attempts'."""
    w = world()
    w.issue(5, "no-change the endpoint already returns 404 by design")
    w.drive(3)
    rec = w.rec(5)
    assert rec.status == BLOCKED
    assert rec.attempts == 0
    assert rec.blocked_reason == "agent says no change needed: the endpoint already returns 404 by design"
    assert "nohalt-blocked" in w.forge.issue(5).labels
    assert any("the endpoint already returns 404 by design" in c for c in _comments(w, 5))


def test_the_no_change_marker():
    assert agents.no_change("work\nNOHALT-RESULT: no-change -- fixed by abc123\n") == "fixed by abc123"
    assert agents.no_change("NOHALT-RESULT: no-change — fixed upstream") == "fixed upstream"
    assert agents.no_change("[agent] result: success false Done. NOHALT-RESULT: no-change -- fixed by #12") == "fixed by #12"
    # Quoting the instruction is not a result, and neither is a marker without a reason.
    assert agents.no_change("end with `NOHALT-RESULT: no-change -- <reason>`") is None
    assert agents.no_change("NOHALT-RESULT: no-change\n") is None
    assert agents.no_change("all done, PR opened") is None
    first, last = "NOHALT-RESULT: no-change -- one", "NOHALT-RESULT: no-change -- two"
    assert agents.no_change(f"{first}\n{last}\n") == "two"


def test_failing_tests_are_read_from_pytest_and_unittest_logs():
    log = (
        PYTEST_LOG
        + "job\tstep\t2026Z ERROR tests/test_c.py::test_setup\n"
        + "FAIL: test_total (myapp.accounts.test_invoice.TestInvoice.test_total)\n"
        + "ERROR: test_old (myapp.stock.TestStock)\n"
    )
    assert failing_tests(log) == [
        "myapp.accounts.test_invoice.TestInvoice.test_total",
        "myapp.stock.TestStock.test_old",
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
        "tests/test_c.py::test_setup",
    ]
