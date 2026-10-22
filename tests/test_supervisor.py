"""The supervisor's lifecycle rules, each tied to a lesson in docs/LESSONS.md."""

from __future__ import annotations

import time
from pathlib import Path

from nohalt.state import BLOCKED, DONE, QUEUED, READY, RUNNING, WAITING_CI


def test_done_means_the_forge_says_closed(world):
    """AGT-01: completion is a closed issue, never the agent's own report."""
    w = world()
    w.issue(1, "nothing", "nothing")  # exits 0 twice, closes nothing
    w.drive(4)
    r = w.rec(1)
    assert r.status == BLOCKED
    assert w.forge.issue(1).state == "OPEN"
    assert "nohalt-blocked" in w.forge.issue(1).labels


def test_closing_finishes_and_removes_the_worktree(world):
    w = world()
    w.issue(1, "close")
    w.drive(3)
    assert w.rec(1).status == DONE
    assert not (w.cfg.worktree_root / "1").exists()


def test_dependencies_are_law(world):
    """SCH-01: order is preference; an issue is worked only when its dependencies are closed."""
    w = world()
    w.issue(1, "sleep 0.5", "close")
    w.issue(2, "close", deps=[1])
    w.sup.tick()
    assert w.rec(1).status == RUNNING
    assert w.rec(2).status == QUEUED and not w.rec(2).started_at
    w.drive(6)
    assert w.rec(1).status == DONE and w.rec(2).status == DONE


def test_provider_errors_are_not_attempts(world):
    """MOD-01: a model-endpoint failure is not an attempt on the issue."""
    w = world(agents={"max_attempts": 1, "provider_retries": 5})
    w.issue(1, "provider-error", "provider-error", "close")
    w.drive(6)
    r = w.rec(1)
    assert r.status == DONE
    assert r.attempts == 0 and r.provider_retries == 2


def test_provider_error_storm_blocks_with_the_right_diagnosis(world):
    w = world(agents={"provider_retries": 2})
    w.issue(1, *["provider-error"] * 5)
    w.drive(6)
    r = w.rec(1)
    assert r.status == BLOCKED
    assert "model endpoint" in r.blocked_reason and "not the issue's fault" in r.blocked_reason


def test_blocked_issue_is_skipped_and_others_continue(world):
    """SCH-03: one stuck issue does not stop the run."""
    w = world(agents={"max_attempts": 1})
    w.issue(1, "nothing")
    w.issue(2, "close")
    w.drive(4)
    assert w.rec(1).status == BLOCKED
    assert w.rec(2).status == DONE


def test_removing_the_label_retries_with_fresh_counters(world):
    """SCH-04: removing the blocked label is how a human says 'try again'."""
    w = world(agents={"max_attempts": 1})
    w.issue(1, "nothing", "close")
    w.drive(3)
    assert w.rec(1).status == BLOCKED
    w.forge.remove_label(1, "nohalt-blocked")
    w.sup._issues.clear()
    w.drive(3)
    assert w.rec(1).status == DONE


def test_a_failed_label_is_not_read_as_an_unblock(world):
    """API-02: a forge call that failed is unknown, never an answer."""
    w = world(agents={"max_attempts": 1})
    w.issue(1, "nothing", "close")
    w.sup.tick()
    w.wait_agents()
    real_add = w.forge.add_label
    w.forge.add_label = lambda *a: (_ for _ in ()).throw(__import__("nohalt.forge", fromlist=["ForgeError"]).ForgeError("down"))  # type: ignore[method-assign]
    w.sup.tick()
    assert w.rec(1).status == BLOCKED and not w.rec(1).labelled
    w.forge.add_label = real_add  # type: ignore[method-assign]
    w.sup._issues.clear()
    w.sup.tick()
    w.wait_agents()
    # it re-labelled instead of relaunching
    assert w.rec(1).status == BLOCKED and w.rec(1).labelled
    assert "nohalt-blocked" in w.forge.issue(1).labels


def test_forge_outage_never_reads_as_closed_or_missing(world):
    w = world()
    w.issue(1, "nothing")
    w.forge.fail = True
    w.sup.tick()
    assert not w.rec(1).started_at
    assert w.rec(1).status == QUEUED


def test_missing_issue_is_dropped_at_once(world):
    """API-01: a number that does not exist is permanent; drop it, do not retry it."""
    w = world()
    w.queue.write_text("999:\n")
    w.issue(1, "close")
    w.drive(3)
    assert w.rec(999).status == DONE
    assert any("does not exist" in m for m in w.messages)
    assert w.rec(1).status == DONE


def test_timeout_continues_in_the_same_worktree(world):
    """AGT-07: a time limit with the issue still open continues the same work, not a new attempt."""
    w = world(agents={"phase_timeout": 1})
    w.issue(1, "sleep 30", "close")
    w.sup.tick()
    time.sleep(1.5)
    w.sup.tick()  # kills on timeout, judges, relaunches in the same tick
    r = w.rec(1)
    assert r.timeouts == 1 and r.attempts == 0
    w.drive(3)
    assert w.rec(1).status == DONE
    assert w.prompts(1)[1].startswith("You were stopped")


def test_hung_agent_is_restarted_only_when_idle(world):
    """LIV-03: no output and no command running means waiting on a reply that will not come."""
    w = world(agents={"hung_after": 1})
    w.issue(1, "sleep 30", "close")
    w.sup.tick()
    r = w.rec(1)
    time.sleep(1.5)
    Path(r.log).touch()  # fresh output: not hung
    w.sup.tick()
    assert w.rec(1).status == RUNNING
    time.sleep(1.5)
    w.sup.tick()
    assert w.rec(1).revivals == 1
    w.drive(3)
    assert w.rec(1).status == DONE


def test_supervisor_restart_adopts_running_agents(world):
    """LIV-01: a supervisor can die while its agent lives; adopt, never duplicate."""
    w = world()
    w.issue(1, "sleep 1", "close")
    w.sup.tick()
    pid = w.rec(1).pid
    from nohalt.supervisor import Supervisor

    w.sup = Supervisor(w.cfg, forge=w.forge, say=w.messages.append)  # a new supervisor
    w.sup.tick()
    assert w.rec(1).pid == pid and w.rec(1).status == RUNNING
    w.drive(4)
    assert w.rec(1).status == DONE
    assert len(w.prompts(1)) == 2  # "sleep" then "close": never a duplicate launch


def test_pr_goes_through_ci_then_waits_for_integration(world):
    w = world()
    w.issue(1, "pr-green")
    w.drive(3)
    r = w.rec(1)
    assert r.status == READY and r.pr


def test_red_pr_is_handed_back_with_the_failure(world):
    """CI-02: an agent that stops while CI runs must be called back when it goes red."""
    w = world()
    w.issue(1, "pr-red", "close")
    w.drive(4)
    assert w.rec(1).status == DONE
    second = w.prompts(1)[1]
    assert "CI is red" in second
    assert w.rec(1).ci_rounds == 1


def test_pending_pr_is_left_alone(world):
    w = world()
    w.issue(1, "pr-pending")
    w.drive(3)
    assert w.rec(1).status == WAITING_CI
    assert len(w.prompts(1)) == 1


def test_uncommitted_work_is_rescued_not_discarded(world, repo):
    """GIT-03: never discard an agent's work."""
    w = world()
    w.issue(1, "dirty", "close")
    w.drive(4)
    assert w.rec(1).status == DONE
    branches = (
        __import__("subprocess")
        .run(["git", "-C", str(repo), "branch", "--list", "nohalt/rescued-1-*"], capture_output=True, text=True)
        .stdout
    )
    assert "nohalt/rescued-1-" in branches


def test_slots_are_shared_and_capped(world):
    w = world(agents={"max_parallel": 1})
    w.issue(1, "sleep 0.5", "close")
    w.issue(2, "close")
    w.sup.tick()
    assert w.rec(1).status == RUNNING and not w.rec(2).started_at
    w.drive(6)
    assert w.rec(2).status == DONE


def test_stop_file_stops_launching(world):
    """LIV-05: stop between issues, never mid-agent."""
    w = world()
    w.issue(1, "close")
    w.sup.state.set_flag("STOP")
    w.sup.tick()
    assert not w.rec(1).started_at


def test_usage_limit_waits_and_resumes_without_an_attempt(world):
    w = world(agents={"max_attempts": 1})
    w.issue(1, "usage-limit", "close")
    w.drive(4)
    assert w.rec(1).status == DONE and w.rec(1).attempts == 0


def test_held_label_is_never_worked(world):
    w = world()
    w.issue(1, "close", labels=["needs-human"])
    w.drive(2)
    assert not w.rec(1).started_at


def test_agent_env_has_shims_first_and_real_binaries_resolved(world):
    w = world()
    w.issue(1, "sleep 1", "close")
    w.sup.tick()
    pid = w.rec(1).pid
    env = dict(e.split("=", 1) for e in Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0") if "=" in e)
    assert env["PATH"].split(":")[0] == str(w.sup.state.bin_dir)
    assert env["NOHALT_REAL_GIT"] and "nohalt" not in Path(env["NOHALT_REAL_GIT"]).parts[-2:-1]
    assert env["NOHALT_ISSUE"] == "1"
    w.drive(3)


def test_quiet_agent_running_a_python_test_is_not_hung(world):
    """LIV-03: a quiet agent running `python -m pytest` under a shell is busy, not hung."""
    w = world(agents={"hung_after": 1})
    w.issue(1, "busy 3", "close")
    w.sup.tick()
    time.sleep(2)
    w.sup.tick()
    assert w.rec(1).status == RUNNING and w.rec(1).revivals == 0
    w.drive(4)
    assert w.rec(1).status == DONE


def test_conflicted_pr_without_ci_is_handed_back(world):
    """A conflicted PR may never get CI at all; it must not wait forever."""
    w = world()
    w.issue(1, "pr-conflict", "close")
    w.drive(4)
    assert w.rec(1).status == DONE
    assert "conflicts with" in w.prompts(1)[1]


def test_an_agent_that_cannot_start_does_not_spend_attempts(world):
    """A deterministic setup failure (not logged in, refused by a router) is not the issue's fault."""
    w = world(agents={"max_attempts": 1})
    w.issue(1, "setup-fail", "close")
    w.drive(4)
    r = w.rec(1)
    assert r.status == DONE and r.attempts == 0 and r.launch_failures == 1


def test_repeated_setup_failures_block_with_a_setup_diagnosis(world):
    w = world()
    w.issue(1, "setup-fail", "setup-fail", "setup-fail", "close")
    w.drive(5)
    r = w.rec(1)
    assert r.status == BLOCKED and r.attempts == 0
    assert "agent's setup" in r.blocked_reason and "Failed to authenticate" in r.blocked_reason


def test_an_old_launchs_error_is_not_evidence(world):
    """Only this launch's output is judged: an error printed by an earlier launch must not count again."""
    w = world(agents={"max_attempts": 2})
    w.issue(1, "setup-fail", "nothing", "close")
    w.drive(3)
    r = w.rec(1)
    assert r.launch_failures == 1 and r.attempts == 1  # the "nothing" exit was judged on its own output


def test_green_draft_is_marked_ready(world):
    """Drafts cannot be merged; once green with the agent finished, nohalt marks them ready."""
    w = world(integrate={"mode": "direct"})
    w.issue(1, "pr-draft")
    w.drive(4)
    assert any("was a draft; marked ready" in m for m in w.messages)
    assert w.forge.data()["merged"] and w.rec(1).status == DONE


def test_guard_log_exists_from_the_start(world):
    w = world()
    w.issue(1, "close")
    w.sup.run(once=True)
    w.wait_agents()
    assert (w.sup.state.logs_dir / "guard.log").exists()
