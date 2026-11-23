"""Fail-back (#88, MOD-18): a running agent on a fallback moves to the wanted model once it has been back a while."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import git
from nohalt import cli
from nohalt import config as config_mod
from nohalt.agents import UsageSignal
from nohalt.state import DONE, IN_REVIEW, RUNNING, WAITING_CI

LONG = {"phase_timeout": "10h", "hung_after": "10h"}  # nohalt's clock is moved by hours; the agents keep real time


def fb_world(world, **models):  # type: ignore[no-untyped-def]
    return world(
        agents={"max_parallel": 1, **LONG, "pools": {"command:claude": 2, "command:local": 2}},
        models={"default": "command:claude", "fallbacks": ["command:local"], "fail_back": "relaunch", **models},
    )


def limit(w, hours: float = 1) -> None:  # type: ignore[no-untyped-def]
    reset = time.time() + hours * 3600
    w.sup.capacity.usage("command:claude", UsageSignal("limit", "five_hour", reset, f"limit {reset}"), "#9")


def on_fallback(w, *script: str):  # type: ignore[no-untyped-def]
    """Issue 1 starts on the fallback while the claude pool is at its limit."""
    limit(w)
    w.issue(1, *script)
    w.sup.tick()
    started(w)
    r = w.rec(1)
    assert r.status == RUNNING and r.model == "command:local"
    return r


def started(w, n: int = 1) -> None:  # type: ignore[no-untyped-def]
    """The agent has read its script line: a stop now is a stop of that action."""
    end = time.time() + 15
    while time.time() < end and f"fake agent on #{n}:" not in w.sup.state.issue_log(n).read_text():
        time.sleep(0.05)


def limit_over_for(w, clock, minutes: float) -> None:  # type: ignore[no-untyped-def]
    clock.advance(3600 + 60)
    w.sup.tick()  # the cool-down ends; the fail-back clock starts
    clock.advance(minutes * 60)
    w.sup.tick()


def stop(w) -> None:  # type: ignore[no-untyped-def]
    w.sup.stop_now()
    w.wait_agents()


def test_after_the_cool_down_ends_and_fail_back_after_passes_the_agent_moves_with_its_work(world, repo, clock):
    w = fb_world(world)
    r = on_fallback(w, "sleep 120", "close")
    pid = r.pid
    (Path(r.worktree) / "half.txt").write_text("half done\n")
    limit_over_for(w, clock, 14)
    r = w.rec(1)
    assert r.pid == pid and r.model == "command:local" and r.fail_back_since  # not yet: 14 of 15 minutes
    clock.advance(2 * 60)
    w.sup.tick()
    r = w.rec(1)
    assert r.status == RUNNING and r.model == "command:claude" and r.pid != pid
    assert r.failed_back.startswith("from command:local to command:claude after 1h")
    assert any(m.startswith("#1: failed back from command:local to command:claude after 1h") for m in w.messages)
    assert "#1: failed back from command:local to command:claude after" in cli.render_status(w.cfg)
    git(repo, "fetch", "-q", "origin")
    assert git(repo, "log", "-1", "--format=%s", f"origin/{r.branch}").startswith("wip: #1")
    assert "half.txt" in git(repo, "show", "--name-only", f"origin/{r.branch}")
    w.wait_agents()
    prompt = w.prompts(1)[-1]
    assert "continuing work that another agent, on command:local" in prompt and "pushed to" in prompt
    assert "model=command:claude resume=False" in w.sup.state.issue_log(1).read_text()  # a new session, same worktree
    w.drive(2)
    assert w.rec(1).status == DONE


def test_the_stop_is_not_an_attempt_and_not_a_finished_agent(world, clock):
    """Today a stopped agent with a PR moves on to CI as if finished; a fail-back stop must not."""
    w = fb_world(world)
    r = on_fallback(w, "sleep 120", "sleep 120")
    sha = git(Path(r.worktree), "rev-parse", "HEAD")
    w.forge.add_pull(50, r.branch, sha, body="Closes #1")
    limit_over_for(w, clock, 16)
    r = w.rec(1)
    assert r.status == RUNNING and r.model == "command:claude"
    assert r.attempts == 0 and r.ci_rounds == 0 and r.timeouts == 0 and r.revivals == 0
    assert r.status != WAITING_CI and not any("agent finished" in h for h in r.history)
    stop(w)


def test_a_pinned_issue_on_its_pin_is_not_touched(world, clock):
    w = fb_world(world)
    w.issue(1, "sleep 120")
    w.sup.router.pin(1, "command:local")
    w.sup.tick()
    started(w)
    pid = w.rec(1).pid
    for _ in range(3):
        clock.advance(20 * 60)
        w.sup.tick()
    r = w.rec(1)
    assert r.pid == pid and r.model == "command:local" and not r.fail_back_since and not r.failed_back
    stop(w)


def test_a_reviewer_is_not_touched(world, clock):
    review = {"enabled": True, "labels": ["careful"], "model": "command:claude", "fallbacks": ["command:local"]}
    w = world(
        agents={"max_parallel": 1, **LONG, "pools": {"command:claude": 2, "command:local": 2}},
        models={"default": "command:local", "fallbacks": ["command:claude"], "fail_back": "relaunch"},
        review={**review, "distinct": False, "timeout": "10h"},
    )
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "sleep-approve 120")
    w.sup.tick()
    w.wait_agents()
    limit(w)
    w.sup.tick()  # green -> in review; the reviewer goes to the fallback
    r = w.rec(1)
    assert r.status == IN_REVIEW and r.review_pid and r.review_model == "command:local"
    pid = r.review_pid
    limit_over_for(w, clock, 20)
    r = w.rec(1)
    assert r.status == IN_REVIEW and r.review_pid == pid and r.review_model == "command:local" and not r.failed_back
    stop(w)


def test_a_running_test_command_defers_the_fail_back(world, clock):
    w = fb_world(world)
    on_fallback(w, "test 3 120", "close")
    pid = w.rec(1).pid
    end = time.time() + 10
    while not w.sup._verifying(pid) and time.time() < end:
        time.sleep(0.05)
    assert w.sup._verifying(pid)
    limit_over_for(w, clock, 16)
    r = w.rec(1)
    assert r.pid == pid and r.model == "command:local"
    assert (
        "fail-back to command:claude deferred: a verify or test command is running"
        in (w.sup.state.logs_dir / "nohalt.log").read_text()
    )
    end = time.time() + 15
    while w.sup._verifying(pid) and time.time() < end:
        time.sleep(0.2)
    w.sup.tick()
    assert w.rec(1).model == "command:claude"
    stop(w)


def test_off_by_default(world, clock):
    w = fb_world(world, fail_back="off")
    r = on_fallback(w, "sleep 120")
    limit_over_for(w, clock, 60)
    r2 = w.rec(1)
    assert r2.pid == r.pid and r2.model == "command:local" and not r2.failed_back
    assert config_mod.ModelsCfg().fail_back == "off"
    stop(w)


@pytest.mark.parametrize(
    ("models", "match"), [({"fail_back": "always"}, "fail_back must"), ({"fail_back_after": "10m"}, "at least 15m")]
)
def test_fail_back_is_checked(tmp_path, models, match):  # type: ignore[no-untyped-def]
    with pytest.raises(config_mod.ConfigError, match=match):
        config_mod.from_dict({"project": {"repo": "o/r"}, "models": models}, root=tmp_path)
