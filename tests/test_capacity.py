"""Capacity: a model that is down, a pool cooling down after a usage limit, and a pause that expires (MOD-14 to MOD-16, LIV-13)."""

from __future__ import annotations

import time

import pytest

from nohalt import cli
from nohalt import config as config_mod
from nohalt.agents import UsageSignal
from nohalt.agents.claude_code import ClaudeCode
from nohalt.state import BLOCKED, DONE, QUEUED, READY, RUNNING


# -- the endpoint is down (MOD-14) ------------------------------------------------------------------------------
def storm_world(world, **models):  # type: ignore[no-untyped-def]
    return world(
        agents={"max_parallel": 4, "provider_retries": 1},
        models={"default": "command:local", "down_after": 3, **models},
    )


def test_a_storm_marks_the_model_down_and_refunds_its_errors(world):
    """MOD-14: every agent on one endpoint dying at once is the endpoint, not three issues' retries."""
    w = storm_world(world)
    for n in (1, 2, 3):
        w.issue(n, "provider-error", "close")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    assert w.sup.capacity.down_since("command:local")
    for n in (1, 2, 3):
        r = w.rec(n)
        assert r.status == QUEUED and r.provider_retries == 0 and r.attempts == 0
    assert any("model command:local is DOWN" in m for m in w.messages)
    assert "model DOWN: command:local" in cli.render_status(w.cfg)


def test_no_retries_are_spent_while_the_model_is_down(world):
    """MOD-14: failures on a down model are not counted; nothing blocks on provider_retries meanwhile."""
    w = storm_world(world)
    for n in (1, 2, 3):
        w.issue(n, "provider-error", "close")
    w.issue(4, "provider-error-after 1", "close")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    assert w.sup.capacity.down_since("command:local")
    r = w.rec(4)
    assert r.status == QUEUED and r.provider_retries == 0 and r.down_waiting
    # Nothing launches onto the down model (no fallback here): the issues wait, unblocked.
    w.drive(3)
    assert all(w.rec(n).status == QUEUED for n in (1, 2, 3, 4))
    assert not any(w.rec(n).status == BLOCKED for n in (1, 2, 3, 4))


def test_new_work_goes_to_a_fallback_while_the_model_is_down(world):
    w = storm_world(world, fallbacks=["command:cloud"])
    for n in (1, 2, 3):
        w.issue(n, "provider-error", "close")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()  # judged: down; relaunched at once on the fallback
    for n in (1, 2, 3):
        r = w.rec(n)
        assert r.model == "command:cloud" and r.overflow.startswith("capacity: command:local down since")
    w.drive(3)
    assert all(w.rec(n).status == DONE for n in (1, 2, 3))


def test_a_down_model_that_answers_is_up_again(world, clock):
    """MOD-14: probed at a backoff while down; up and logged when it answers."""
    w = storm_world(world, probe=True, probe_ttl=0, down_probe="2m")
    answers = {"command:local": False}
    w.sup.router.prober._probe = lambda m: answers[m]  # type: ignore[method-assign]
    w.issue(1, "close")
    for _ in range(3):  # three failed probes in a row
        w.sup.tick()
    cap = w.sup.capacity
    assert cap.down_since("command:local") and not w.rec(1).started_at
    w.sup.tick()  # not due yet: no probe, still down
    assert cap.down_since("command:local")
    answers["command:local"] = True
    clock.advance(3 * 60)
    w.sup.tick()
    assert not cap.down_since("command:local")
    assert any("model command:local is UP again" in m for m in w.messages)
    assert w.rec(1).status == RUNNING and w.rec(1).model == "command:local"
    w.wait_agents()


def test_a_failed_reprobe_waits_longer(world, clock):
    w = storm_world(world, probe=True, probe_ttl=0, down_probe="2m", down_probe_max="5m")
    w.sup.router.prober._probe = lambda m: False  # type: ignore[method-assign]
    for _ in range(3):
        w.sup.capacity.probe_result("command:local", False)
    clock.advance(121)
    w.sup.tick()
    v = w.sup.capacity.snapshot()["down"]["command:local"]
    assert v["probes"] == 1 and v["next_probe"] - clock() == pytest.approx(240, abs=5)
    clock.advance(241)
    w.sup.tick()
    v = w.sup.capacity.snapshot()["down"]["command:local"]
    assert v["next_probe"] - clock() == pytest.approx(300, abs=5)  # capped by down_probe_max


def test_blocked_for_the_endpoint_only_after_down_block_after(world, clock):
    """MOD-14: an issue is blocked only once its model and every fallback were down for down_block_after."""
    w = storm_world(world, fallbacks=["command:cloud"], down_block_after="6h", probe=True)
    alive = {"command:local"}
    w.sup.router.prober._probe = lambda m: m in alive  # type: ignore[method-assign]
    for _ in range(3):
        w.sup.capacity.probe_result("command:cloud", False)
    for n in (1, 2, 3):
        w.issue(n, "provider-error", "close")
    w.sup.tick()
    w.wait_agents()
    alive.clear()  # and it stays down
    w.sup.tick()
    assert all(w.rec(n).status == QUEUED and w.rec(n).down_waiting for n in (1, 2, 3))
    clock.advance(5 * 3600)
    w.sup.tick()
    assert all(w.rec(n).status == QUEUED for n in (1, 2, 3))
    clock.advance(3600 + 60)
    w.sup.tick()
    r = w.rec(1)
    assert r.status == BLOCKED
    assert "has been down for over 6h" in r.blocked_reason and "not the issue" in r.blocked_reason


def test_one_issue_failing_alone_is_not_a_storm(world):
    """An endpoint that fails one issue repeatedly is that issue's retries (MOD-01), not a down model."""
    w = storm_world(world)
    w.issue(1, *["provider-error"] * 5)
    w.drive(5)
    assert not w.sup.capacity.down_since("command:local")
    assert w.rec(1).status == BLOCKED and "model endpoint failed" in w.rec(1).blocked_reason


# -- usage-aware routing (MOD-15) ------------------------------------------------------------------------------
def usage_world(world, **models):  # type: ignore[no-untyped-def]
    return world(
        agents={"max_parallel": 1, "pools": {"command:claude": 2, "command:local": 2}},
        models={"default": "command:claude", "fallbacks": ["command:local"], **models},
    )


def test_a_usage_limit_cools_the_pool_and_new_work_goes_to_the_fallback(world, clock):
    """MOD-15: a usage limit cools the pool down until its reset; new work starts elsewhere at once."""
    w = usage_world(world)
    reset = int(time.time() + 3 * 3600)
    w.issue(1, f"usage-limit {reset}", "close")
    w.sup.tick()
    w.wait_agents()
    w.issue(2, "close")
    w.sup.tick()
    cap = w.sup.capacity
    assert cap.cooling_until("command:claude") == reset
    r1 = w.rec(1)  # MOD-07: the stopped agent waits for its own model's reset and resumes there
    assert r1.status == QUEUED and r1.hold_model == "command:claude" and r1.attempts == 0
    assert r1.not_before == pytest.approx(reset, abs=5)
    r2 = w.rec(2)
    assert r2.model == "command:local" and r2.overflow.startswith("capacity: command:claude cooling down (five_hour)")
    status = cli.render_status(w.cfg)
    assert "pool cooling down: command:claude five_hour usage limit (#1)" in status and "; reset)" in status
    w.wait_agents()


def test_a_warning_from_a_running_agent_cools_the_pool_and_leaves_the_agent_alone(world):
    w = usage_world(world)
    reset = int(time.time() + 3600)
    w.issue(1, f"usage-warning {reset} 2", "close")
    w.sup.tick()
    time.sleep(0.5)
    w.issue(2, "close")
    w.sup.tick()
    w.sup.tick()
    assert w.sup.capacity.cooling_until("command:claude") == reset
    assert w.rec(1).status == RUNNING and w.rec(1).model == "command:claude"
    assert w.rec(2).model == "command:local"
    assert "near its usage limit" in cli.render_status(w.cfg)
    assert sum("cooling down until" in m for m in w.messages) == 1  # read every tick, said once
    w.wait_agents()


def test_without_a_reported_reset_the_default_cool_down_applies(world):
    w = usage_world(world, usage_cooldown="45m")
    w.issue(1, "usage-limit", "close")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    assert w.sup.capacity.cooling_until("command:claude") - time.time() == pytest.approx(45 * 60, abs=10)
    assert "default cool-down, no reset reported" in cli.render_status(w.cfg)


def test_a_pinned_issue_waits_for_a_cooling_pool(world):
    """MOD-05 stands: a pinned issue is never diverted."""
    w = usage_world(world)
    w.issue(1, f"usage-limit {int(time.time() + 3600)}", "close")
    w.sup.tick()
    w.wait_agents()
    w.issue(2, "close")
    w.sup.router.pin(2, "command:claude")
    w.sup.tick()
    w.sup.tick()
    assert not w.rec(2).started_at


def test_divert_respects_premium(world):
    w = usage_world(world, fallbacks=["command:local"], premium=["command:local"])
    w.issue(1, f"usage-limit {int(time.time() + 3600)}", "close")
    w.sup.tick()
    w.wait_agents()
    w.issue(2, "close")
    w.sup.tick()
    assert not w.rec(2).started_at
    w.sup.state.save("approved.json", [2])
    w.sup.tick()
    assert w.rec(2).model == "command:local"
    w.wait_agents()


def test_the_cool_down_ends_and_the_waiting_issue_resumes_on_its_model(world, clock):
    w = usage_world(world)
    reset = int(time.time() + 3600)
    w.issue(1, f"usage-limit {reset}", "close")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    assert w.rec(1).status == QUEUED and not w.rec(1).overflow
    clock.advance(3600 + 120)
    w.sup.tick()
    assert not w.sup.capacity.cooling_until("command:claude")
    assert any("cool-down over" in m for m in w.messages)
    r = w.rec(1)
    assert r.status == RUNNING and r.model == "command:claude" and r.hold_model == ""
    w.drive(2)
    assert w.rec(1).status == DONE
    log = (w.sup.state.issue_log(1)).read_text()
    assert "model=command:claude resume=True" in log


# -- reviews during a warning cool-down (MOD-16) -----------------------------------------------------------------
def review_world(world, during: str = "warning"):  # type: ignore[no-untyped-def]
    review = {"enabled": True, "labels": ["careful"], "model": "command:claude", "fallbacks": ["command:local"]}
    return world(
        agents={"max_parallel": 1, "pools": {"command:claude": 2, "command:local": 2}},
        models={"default": "command:claude", "fallbacks": ["command:local"]},
        review={**review, "distinct": False, "during_cooldown": during},
    )


def cool(w, kind: str, hours: float, source: str = "#9") -> None:  # type: ignore[no-untyped-def]
    reset = time.time() + hours * 3600
    w.sup.capacity.usage("command:claude", UsageSignal(kind, "seven_day", reset, f"{kind} {reset}"), source)


def green_in_review(w):  # type: ignore[no-untyped-def]
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "approve")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()  # green -> in review; the reviewer launched
    return w.rec(1)


def test_a_reviewer_runs_on_a_warned_pool_while_authoring_diverts(world):
    """MOD-16: a warning diverts the author; with during_cooldown = "warning" the short review still runs there."""
    w = review_world(world)
    cool(w, "warning", 5 * 24)
    r = green_in_review(w)
    assert r.model == "command:local" and r.overflow.startswith("capacity: command:claude cooling down")
    assert r.review_pid and r.review_model == "command:claude"
    assert "command:claude cooling down (seven_day)" in r.review_cooling
    assert any("a warning: reviews still run there" in m for m in w.messages)
    status = cli.render_status(w.cfg)
    assert "reviewed on a pool cooling down for a usage warning" in status and "#1: command:claude;" in status
    w.drive(2)
    assert w.rec(1).status == READY


def test_a_hard_limited_pool_diverts_the_reviewer(world):
    w = review_world(world)
    cool(w, "limit", 5)
    r = green_in_review(w)
    assert r.review_pid and r.review_model == "command:local" and not r.review_cooling
    assert "reviewed on a pool cooling down" not in cli.render_status(w.cfg)
    w.wait_agents()


def test_by_default_reviews_divert_with_authoring(world):
    w = review_world(world, during="none")
    cool(w, "warning", 5 * 24)
    r = green_in_review(w)
    assert r.model == "command:local" and r.review_model == "command:local" and not r.review_cooling
    w.wait_agents()


def test_a_limit_during_a_warning_diverts_reviews_until_its_own_reset(world, clock):
    """A reviewer (or author) hitting the limit on a warned pool hardens it; a later warning does not soften it."""
    w = review_world(world)
    cap = w.sup.capacity
    cool(w, "warning", 5 * 24)
    assert cap.unavailable("command:claude") and not cap.unavailable("command:claude", warned_ok=True)
    cool(w, "limit", 5, source="review #1")  # sooner than the warning's reset: the cool-down does not move
    assert cap.cooling_until("command:claude") == pytest.approx(time.time() + 5 * 24 * 3600, abs=5)
    assert cap.unavailable("command:claude", warned_ok=True)
    assert any("usage limit reached (review #1); hard until" in m for m in w.messages)
    cool(w, "warning", 6 * 24, source="#8")  # moves the cool-down, keeps the limit
    assert cap.unavailable("command:claude", warned_ok=True)
    assert "usage limit (#8)" in cli.render_status(w.cfg)
    clock.advance(5 * 3600 + 60)  # the limit's window reset; the warning's has not
    assert cap.unavailable("command:claude") and not cap.unavailable("command:claude", warned_ok=True)
    assert "near its usage limit (#8)" in cli.render_status(w.cfg)


def test_during_cooldown_is_checked(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="during_cooldown"):
        config_mod.from_dict({"project": {"repo": "o/r"}, "review": {"during_cooldown": "always"}}, root=tmp_path)


# -- pinned issues during a warning cool-down (MOD-17) ------------------------------------------------------------
def pinned_in_cooldown(w, kind: str, hours: float):  # type: ignore[no-untyped-def]
    cool(w, kind, hours)
    w.issue(1, "close")
    w.issue(2, "close")
    w.sup.router.pin(1, "command:claude")
    w.sup.tick()
    return w.rec(1), w.rec(2)


def test_a_pinned_issue_starts_through_a_warning_cool_down(world):
    """MOD-17: with pinned_during_cooldown = "warning", the owner's pin starts on a pool only a warning cools down."""
    w = usage_world(world, pinned_during_cooldown="warning")
    r1, r2 = pinned_in_cooldown(w, "warning", 32)
    assert r1.status == RUNNING and r1.model == "command:claude"
    assert "command:claude cooling down (seven_day)" in r1.pin_cooling and not r1.overflow
    assert any("a warning: pinned issues still start there" in e for e in r1.history)
    assert r2.model == "command:local" and r2.overflow.startswith("capacity:")  # unpinned work still diverts
    assert any("#1: agent launched on command:claude" in m and "pinned issues still start" in m for m in w.messages)
    status = cli.render_status(w.cfg)
    assert "pinned, launched on a pool cooling down for a usage warning" in status
    assert "#1: command:claude; command:claude cooling down" in status
    w.wait_agents()


def test_a_pinned_issue_waits_through_a_hard_limit_even_with_the_setting(world):
    w = usage_world(world, pinned_during_cooldown="warning")
    r1, _ = pinned_in_cooldown(w, "limit", 5)
    assert not r1.started_at and not r1.pin_cooling
    assert "pinned, launched on a pool" not in cli.render_status(w.cfg)
    w.wait_agents()


def test_a_limit_during_the_warning_makes_pinned_issues_wait_again(world):
    w = usage_world(world, pinned_during_cooldown="warning")
    cool(w, "warning", 32)
    cool(w, "limit", 5, source="#7")  # sooner than the warning's reset: hardens it without moving it
    w.issue(1, "close")
    w.sup.router.pin(1, "command:claude")
    w.sup.tick()
    assert not w.rec(1).started_at


def test_by_default_a_pinned_issue_waits_through_a_warning(world):
    w = usage_world(world)
    r1, r2 = pinned_in_cooldown(w, "warning", 32)
    assert not r1.started_at and not r1.pin_cooling
    assert r2.model == "command:local"
    w.wait_agents()


def test_pinned_during_cooldown_is_checked(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="pinned_during_cooldown"):
        config_mod.from_dict({"project": {"repo": "o/r"}, "models": {"pinned_during_cooldown": "yes"}}, root=tmp_path)


def test_claude_stream_lines_carry_the_window_and_reset():
    from nohalt.agents.claude_stream import render

    line = render(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"rateLimitType": "five_hour", "status": "rejected", "resetsAt": 1900000000},
        }
    )[0]
    assert line == "[agent] rate_limit five_hour rejected resets=1900000000"
    sig = ClaudeCode().usage_signal(f"working\n{line}\n[agent] result: error true You've hit your usage limit\n")
    assert sig and sig.kind == "limit" and sig.window == "five_hour" and sig.resets_at == 1900000000
    assert ClaudeCode().usage_signal("[agent] rate_limit five_hour allowed\n") is None


def test_usage_patterns_are_configurable_per_agent(world):
    from nohalt import config as config_mod

    c = ClaudeCode(usage_warning=[r"^\[agent\] rate_limit (?P<window>\S+) allowed .*used=0\.(8|9)"])
    sig = c.usage_signal("[agent] rate_limit seven_day allowed resets=1 used=0.85\n")
    assert sig and sig.kind == "warning" and sig.window == "seven_day"
    with pytest.raises(config_mod.ConfigError, match="usage_limit"):
        world(agents={"options": {"claude-code": {"usage_limit": ["(unclosed"]}}})


# -- pause expiry (LIV-13) ---------------------------------------------------------------------------------------
def test_pause_for_expires_and_says_so(world, tmp_path, clock):
    from test_reload import _toml

    w = world()
    toml = tmp_path / "nohalt.toml"
    toml.write_text(_toml(w.data))
    w.issue(1, "close")
    assert cli.main(["--config", str(toml), "pause", "--for", "2h"]) == 0
    w.sup.tick()
    assert not w.rec(1).started_at
    assert "expires" in cli.render_status(w.cfg)
    clock.advance(2 * 3600 + 1)
    w.sup.tick()
    assert not w.sup.state.has("PAUSE")
    assert any("PAUSE expired" in m for m in w.messages)
    assert w.rec(1).started_at
    w.wait_agents()


def test_pause_without_for_never_expires_but_shows_its_age(world, tmp_path, clock):
    from test_reload import _toml

    w = world()
    toml = tmp_path / "nohalt.toml"
    toml.write_text(_toml(w.data))
    w.issue(1, "close")
    assert cli.main(["--config", str(toml), "pause"]) == 0
    clock.advance(30 * 86400)
    w.sup.tick()
    assert w.sup.state.has("PAUSE") and not w.rec(1).started_at
    assert "paused for" in cli.render_status(w.cfg) and "no expiry" in cli.render_status(w.cfg)
