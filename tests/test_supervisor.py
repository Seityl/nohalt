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


def test_usage_limit_waits_and_resumes_without_an_attempt(world, clock):
    """MOD-07: wait for the reset and continue the same session on the same model."""
    w = world(agents={"max_attempts": 1})
    w.issue(1, "usage-limit", "close")
    w.drive(4)
    assert w.rec(1).status == QUEUED and w.rec(1).hold_model == "command:fake"
    clock.advance(w.cfg.models.usage_cooldown + 60)
    w.drive(3)
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


def _existing_pr(w, n: int, checks: list[tuple[str, str, str]]) -> None:
    w.forge.add_issue(n, f"Issue {n}")
    (w.scripts / f"{n}.txt").write_text("close\n")
    with open(w.queue, "a") as f:
        f.write(f"{n}:\n")
    w.forge.add_pull(500 + n, f"feature/{n}-earlier-work", f"sha-{n}", body=f"Closes #{n}")
    w.forge.set_checks(f"sha-{n}", checks)


def test_an_issue_with_an_open_pr_is_adopted_not_relaunched(world):
    """Work that already has a PR (a runner nohalt replaced, a human) is watched, not redone."""
    w = world()
    _existing_pr(w, 1, [("ci", "completed", "success")])
    w.drive(2)
    r = w.rec(1)
    assert r.status == READY and r.pr == 501 and r.branch == "feature/1-earlier-work"
    assert w.prompts(1) == []  # no agent was launched
    assert any("adopted" in m for m in w.messages)


def test_an_adopted_red_pr_gets_an_agent(world):
    w = world()
    _existing_pr(w, 1, [("ci", "completed", "failure")])
    w.drive(3)
    assert len(w.prompts(1)) == 1 and "CI is red" in w.prompts(1)[0]
    assert w.rec(1).status == DONE


def test_dry_run_reports_adoption_without_changing_anything(world):
    w = world()
    _existing_pr(w, 1, [("ci", "completed", "success")])
    w.sup.dry_run = True
    report = w.sup.tick()
    assert report.adopted == ["#1<-PR #501"] and not report.launched
    assert not w.sup.state.exists(1) or w.rec(1).status == QUEUED


def test_ignored_checks_never_count(world):
    """A suite cancelled on PRs by policy must not hold a green PR forever."""
    checks = [
        ("build the product", "completed", "success"),
        ("Frappe tests", "completed", "cancelled"),
        ("Upgrade rehearsal", "completed", "success"),
    ]
    w = world(integrate={"ignore_checks": ["Frappe tests"]})
    _existing_pr(w, 1, checks)
    w.drive(2)
    assert w.rec(1).status == READY


def test_without_ignore_a_cancelled_check_keeps_waiting(world):
    w = world()
    _existing_pr(w, 1, [("build the product", "completed", "success"), ("Frappe tests", "completed", "cancelled")])
    w.drive(2)
    assert w.rec(1).status == WAITING_CI


def test_a_check_that_must_pass_when_present_still_gates(world):
    w = world(integrate={"ignore_checks": ["Frappe tests"]})
    _existing_pr(
        w,
        1,
        [
            ("build the product", "completed", "success"),
            ("Frappe tests", "completed", "cancelled"),
            ("Upgrade rehearsal", "completed", "failure"),
        ],
    )
    w.drive(2)
    assert len(w.prompts(1)) == 1 and "Upgrade rehearsal" in w.prompts(1)[0]


def test_dry_run_never_writes_state(world):
    w = world()
    w.issue(1, "close")
    w.forge.set_state(1, "CLOSED")
    w.queue.write_text("1:\n999:\n")
    w.sup.dry_run = True
    w.sup.tick()
    assert not w.sup.state.exists(1) and not w.sup.state.exists(999)


# -- overflow (MOD-13) -------------------------------------------------------------------------------
def overflow_world(world, agents=None, **models):  # type: ignore[no-untyped-def]
    return world(
        agents={"pools": {"command:local": 1, "command:cloud": 2}, **(agents or {})},
        models={"default": "command:local", "fallbacks": ["command:cloud"], "overflow_after": "10m", **models},
    )


def stalled_long_ago(w, n: int) -> None:  # type: ignore[no-untyped-def]
    rec = w.rec(n)
    assert rec.stalled_since, "the overflow clock should be running"
    rec.stalled_since -= 12 * 60
    w.sup.state.put(rec)


def test_a_full_pool_overflows_to_an_idle_one_after_the_delay(world):
    w = overflow_world(world)
    w.issue(1, "sleep 3")
    w.issue(2, "close")
    w.sup.tick()
    assert w.rec(1).model == "command:local" and not w.rec(2).started_at
    stalled_long_ago(w, 2)
    w.sup.tick()
    rec = w.rec(2)
    assert rec.status == RUNNING and rec.model == "command:cloud"
    assert rec.overflow == "overflow: command:local pool full (1/1) for 12m → command:cloud"
    assert not w.sup.router.pinned(2)
    assert any("overflow: command:local pool full" in m for m in w.messages)
    from nohalt.cli import render_status

    assert "command:cloud ^" in render_status(w.cfg) and "#2: overflow: command:local" in render_status(w.cfg)
    w.wait_agents()


def test_before_the_delay_a_full_pool_waits(world):
    w = overflow_world(world)
    w.issue(1, "sleep 3")
    w.issue(2, "close")
    w.sup.tick()
    w.sup.tick()
    assert not w.rec(2).started_at and w.rec(2).stalled_since
    w.wait_agents()


def test_without_overflow_after_a_full_pool_waits_forever(world):
    w = overflow_world(world, overflow_after="")
    w.issue(1, "sleep 3")
    w.issue(2, "close")
    w.sup.tick()
    w.sup.tick()
    assert not w.rec(2).started_at and not w.rec(2).stalled_since
    w.wait_agents()


def test_overflow_never_takes_a_premium_model_unless_approved(world):
    w = overflow_world(world, premium=["command:cloud"], overflow_to=["command:cloud"])
    w.issue(1, "sleep 3")
    w.issue(2, "close")
    w.sup.tick()
    stalled_long_ago(w, 2)
    w.sup.tick()
    assert not w.rec(2).started_at
    w.sup.state.save("approved.json", [2])
    w.sup.tick()
    assert w.rec(2).model == "command:cloud" and w.rec(2).overflow
    w.wait_agents()


def test_a_pinned_issue_never_overflows(world):
    """MOD-05 stands: a pinned issue waits for its own model."""
    w = overflow_world(world)
    w.issue(1, "sleep 3")
    w.issue(2, "close")
    w.sup.router.pin(2, "command:local")
    w.sup.tick()
    rec = w.rec(2)  # the clock may run; it must not matter
    rec.stalled_since = time.time() - 3600
    w.sup.state.put(rec)
    w.sup.tick()
    assert not w.rec(2).started_at
    w.wait_agents()


def test_an_overflowed_issue_is_routed_as_usual_on_relaunch(world):
    """MOD-04: overflow is not a pin; the next launch goes back to the routed model."""
    w = overflow_world(world, agents={"max_attempts": 1})
    w.issue(1, "sleep 2")  # then blocked: frees the local slot
    w.issue(2, "provider-error", "close")
    w.sup.tick()
    stalled_long_ago(w, 2)
    w.sup.tick()
    assert w.rec(2).model == "command:cloud"
    w.wait_agents()
    w.sup.tick()
    rec = w.rec(2)
    assert w.rec(1).status == BLOCKED
    assert rec.model == "command:local" and rec.overflow == "" and rec.status == RUNNING
    w.drive(3)
    assert w.rec(2).status == DONE


def test_a_dead_primary_with_full_fallbacks_overflows(world):
    w = overflow_world(
        world,
        agents={"pools": {"command:local": 2, "command:cloud": 1}},
        overflow_to=["command:cloud", "command:spare"],
    )
    w.sup.router.prober.answers = lambda m: m != "command:local"  # type: ignore[method-assign]
    w.issue(1, "sleep 3")
    w.issue(2, "close")
    w.sup.tick()
    assert w.rec(1).model == "command:cloud"  # failed over (MOD-03)
    assert not w.rec(2).started_at  # the fallback's pool is full: not taken
    stalled_long_ago(w, 2)
    w.sup.tick()
    rec = w.rec(2)
    assert rec.model == "command:spare"
    assert rec.overflow.startswith("overflow: command:local not answering and no fallback has a free slot for 12m")
    w.wait_agents()


def test_overflow_leaves_a_pools_reserved_slots_alone(world):
    w = overflow_world(world, overflow_reserve={"command:cloud": 1}, route=[{"labels": ["big"], "model": "command:cloud"}])
    w.issue(1, "sleep 3")
    w.issue(3, "sleep 3", labels=["big"])
    w.issue(2, "close")
    w.sup.tick()
    assert w.rec(3).model == "command:cloud"
    stalled_long_ago(w, 2)
    w.sup.tick()
    assert not w.rec(2).started_at  # cloud has 1 of 2 free, and 1 is reserved
    w.wait_agents()
