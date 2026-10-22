"""Config, queue, prompts, routing, budget, agents, forge transport."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from nohalt import budget, queue
from nohalt import config as config_mod
from nohalt import prompt as prompt_mod
from nohalt.agents import claude_stream
from nohalt.agents import get as get_agent
from nohalt.config import ConfigError
from nohalt.forge import Check, Issue, verdict
from nohalt.forge.github import GitHub
from nohalt.models import Router
from nohalt.state import StateStore
from nohalt.util import parse_duration


# -- config ------------------------------------------------------------------------------
def test_durations():
    assert parse_duration(90) == 90
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 1800
    assert parse_duration("4h") == 14400
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_unknown_keys_are_errors(tmp_path):
    """A typo that silently falls back to a default is how a guard stops guarding."""
    with pytest.raises(ConfigError, match="unknown key 'max_paralel'"):
        config_mod.from_dict({"agents": {"max_paralel": 3}}, root=tmp_path)
    with pytest.raises(ConfigError, match="unknown section"):
        config_mod.from_dict({"agent": {}}, root=tmp_path)


def test_models_must_name_their_agent(tmp_path):
    with pytest.raises(ConfigError, match="must name its agent"):
        config_mod.from_dict({"models": {"default": "sonnet"}}, root=tmp_path)


def test_unknown_merge_driver(tmp_path):
    with pytest.raises(ConfigError, match="unknown driver"):
        config_mod.from_dict({"integrate": {"mergers": {"x": "magic"}}}, root=tmp_path)


def test_templates_load(tmp_path):
    from importlib import resources

    for name in ("generic", "frappe"):
        text = resources.files("nohalt").joinpath(f"templates/{name}.toml").read_text()
        p = tmp_path / f"{name}.toml"
        p.write_text(text.replace("OWNER/REPO", "o/r"))
        cfg = config_mod.load(p)
        assert cfg.project.repo == "o/r"


def test_frappe_stack_defaults(tmp_path):
    from nohalt import stacks

    cfg = config_mod.from_dict({"stack": {"name": "frappe", "bench": "/b", "app": "a", "sites": ["s1", "s2"]}}, root=tmp_path)
    st = stacks.get(cfg)
    assert [s.name for s in cfg.verify.slots] == ["s1", "s2"]
    assert cfg.verify.pythonpath_worktree
    assert "bench build" in cfg.verify.refuse
    assert cfg.integrate.mergers["**/patches.txt"] == "keep_both_lines"
    assert st.check_command("bench --site s1 run-tests --app a")  # whole suite refused
    assert st.check_command("bench --site s1 run-tests --module a.x") is None
    with pytest.raises(ConfigError):
        stacks.get(config_mod.from_dict({"stack": {"name": "frappe"}}, root=tmp_path))


# -- queue -----------------------------------------------------------------------------------
def test_queue_parse():
    q = queue.parse("# comment\n12:\n14: 13   # waits\n#15: 12, 14\n")
    assert [(e.issue, e.deps) for e in q] == [(12, []), (14, [13]), (15, [12, 14])]


@pytest.mark.parametrize("bad", ["148\n", "1: 1\n", "1:\n1:\n", "x: 2\n"])
def test_queue_errors(bad):
    with pytest.raises(queue.QueueError):
        queue.parse(bad)


def test_empty_queue_is_an_error_not_a_default_range(tmp_path):
    p = tmp_path / "q.txt"
    p.write_text("# nothing\n")
    with pytest.raises(queue.QueueError, match="lists no issues"):
        queue.load(p)


def test_queue_append_front(tmp_path):
    p = tmp_path / "q.txt"
    p.write_text("1:\n")
    queue.append(p, 9, front=True)
    queue.append(p, 1)
    assert p.read_text() == "9:\n1:\n"


# -- prompts -------------------------------------------------------------------------------------
def test_unknown_placeholder_is_an_error():
    """PRM-03: a prompt carried over by search-and-replace once told agents 'use npm, never npm'."""
    with pytest.raises(prompt_mod.PromptError, match="PASSWORD"):
        prompt_mod.render("site {{SITE}} pw {{PASSWORD}}", {"SITE": "x"})


def test_builtin_prompts_render(tmp_path):
    cfg = config_mod.from_dict({"project": {"repo": "o/r"}}, root=tmp_path)
    vals = prompt_mod.values(cfg, issue=5, title="T", branch="feature/5-t", worktree=tmp_path / "5")
    body = prompt_mod.render(prompt_mod.template(cfg), vals)
    assert "issue #5" in body and "Closes #5" in body and "You do not merge" in body
    cfg2 = config_mod.from_dict(
        {"project": {"repo": "o/r"}, "stack": {"name": "frappe", "bench": "/b", "app": "a", "sites": ["s"]}}, root=tmp_path
    )
    from nohalt import stacks

    st = stacks.get(cfg2)
    vals2 = prompt_mod.values(cfg2, issue=5, title="T", branch="b", worktree=tmp_path / "5")
    assert "bench build" in prompt_mod.render(prompt_mod.template(cfg2, st.prompt_addendum), vals2)


# -- routing ---------------------------------------------------------------------------------------
def router(tmp_path, **models):
    cfg = config_mod.from_dict({"models": {"probe": False, **models}}, root=tmp_path)
    return Router(cfg, StateStore(tmp_path / "s"))


def test_route_rules_and_premium_gate(tmp_path):
    r = router(
        tmp_path,
        default="opencode:a/b",
        premium=["claude-code:opus"],
        premium_fallback="claude-code:sonnet",
        route=[{"labels": ["polish"], "model": "opencode:local/small"}, {"title": "^\\[big\\]", "model": "claude-code:opus"}],
    )
    assert r.wanted(Issue(1, "OPEN", "x", ["Polish"]))[0] == "opencode:local/small"
    assert r.wanted(Issue(2, "OPEN", "[big] thing"))[0] == "claude-code:sonnet"  # not approved
    r.pin(2, "claude-code:opus", approve=True)
    assert r.wanted(Issue(2, "OPEN", "[big] thing")) == ("claude-code:opus", True)
    assert r.wanted(Issue(3, "OPEN", "plain"))[0] == "opencode:a/b"


def test_fallback_when_default_does_not_answer_but_pins_wait(tmp_path):
    r = router(tmp_path, default="opencode:dead/x", fallbacks=["opencode:alive/y"])
    r.prober.answers = lambda m: m == "opencode:alive/y"  # type: ignore[method-assign]
    assert r.choose(Issue(1, "OPEN")) == "opencode:alive/y"
    r.pin(1, "opencode:dead/x")
    assert r.choose(Issue(1, "OPEN")) is None  # a pinned issue waits for its model


# -- budget ---------------------------------------------------------------------------------------------
def test_budget_high_water_mark_and_caps(tmp_path):
    cfg = config_mod.from_dict(
        {
            "budget": {
                "enabled": True,
                "per_issue": 1.0,
                "total": 1.5,
                "rates": {"input": 0, "output": 1_000_000, "cache_read": 0, "cache_write": 0},
            }
        },
        root=tmp_path,
    )
    state = StateStore(tmp_path / "s")
    db_dir = state.agent_data / "4" / "opencode"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(db_dir / "opencode.db")
    con.execute("create table message (data text)")
    con.execute(
        "insert into message values (?)",
        (json.dumps({"role": "assistant", "providerID": "anthropic", "tokens": {"output": 1.2}}),),
    )
    con.commit()
    con.close()
    _, total, hand = budget.assess(cfg, state, [4])
    assert round(total, 2) == 1.2 and hand and "per-issue" in hand[0][1]
    (db_dir / "opencode.db").unlink()  # data pruned: the spend must not shrink
    _, total, _ = budget.assess(cfg, state, [4])
    assert round(total, 2) == 1.2
    assert budget.is_paid(cfg, "opencode:anthropic/claude") and not budget.is_paid(cfg, "opencode:local/x")


# -- agents ----------------------------------------------------------------------------------------------
def test_provider_error_detection_reads_only_the_tail():
    oc = get_agent("opencode")
    assert oc.provider_failed("working...\nError: Bad Gateway\n")
    assert oc.provider_failed("Error: fetch failed")
    assert not oc.provider_failed("the agent quoted 'Error: Bad Gateway' earlier\n" + "\n".join(f"line {i}" for i in range(20)))
    assert not oc.provider_failed("all done")


def test_claude_launch_is_wrapped_and_disallows_subagents(tmp_path):
    cc = get_agent("claude-code")
    launch = cc.launch("sonnet", "do it", title="t", resume=True, data_dir=tmp_path)
    assert launch.argv[:3] == [sys.executable, "-m", "nohalt.agents.claude_stream"]
    assert "--continue" in launch.argv and "Task" in launch.argv and "--strict-mcp-config" in launch.argv
    assert launch.argv[-1] == "do it" and "CLAUDECODE" in launch.unset_env
    assert cc.usage_limited("[agent] rate_limit five_hour rejected")
    assert cc.provider_failed("[agent] result: error_during_execution true API Error: 529 overloaded")
    assert not cc.provider_failed("[agent] result: success false done")


def test_claude_stream_render():
    lines = claude_stream.render(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
            },
        }
    )
    assert lines == ["hi", "$ ls"]
    assert claude_stream.render({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}) == [
        "[agent] result: success false ok"
    ]


def test_opencode_isolates_data_per_issue(tmp_path):
    launch = get_agent("opencode").launch("a/b", "p", title="t", resume=False, data_dir=tmp_path / "9")
    assert launch.env["XDG_DATA_HOME"] == str(tmp_path / "9")
    assert "--continue" not in launch.argv


# -- forge -------------------------------------------------------------------------------------------------
def test_verdict():
    ok = Check("ci", "completed", "success")
    bad = Check("ci", "completed", "failure")
    run = Check("ci", "in_progress")
    other = Check("lint", "completed", "success")
    assert verdict([ok, other], []) == "green"
    assert verdict([bad, other], []) == "red"
    assert verdict([run], []) == "pending"
    assert verdict([], []) == "pending"
    assert verdict([other], ["ci"]) == "pending"
    assert verdict([ok, Check("x", "completed", "failure")], ["ci"]) == "green"
    assert verdict([Check("ci", "completed", "cancelled")], []) == "pending"


@pytest.fixture
def fake_gh(tmp_path: Path):
    """A gh that answers from a script: argv path -> (rc, stdout, stderr)."""
    script = tmp_path / "gh"
    script.write_text(f"""#!{sys.executable}
import json, sys
path = sys.argv[2]
calls = open({str(tmp_path / "calls")!r}, 'a'); calls.write(' '.join(sys.argv[1:]) + '\\n'); calls.close()
if 'issues/404' in path:
    sys.stderr.write('gh: Not Found (HTTP 404)'); sys.exit(1)
if 'issues/500' in path:
    sys.stderr.write('connection reset'); sys.exit(1)
if 'issues/77' in path:
    print(json.dumps({{"number": 77, "pull_request": {{}}}})); sys.exit(0)
if 'issues/' in path:
    print(json.dumps({{"number": 1, "state": "closed", "title": "t", "labels": [{{"name": "bug"}}]}})); sys.exit(0)
if 'pulls?' in path:
    print(json.dumps({{"number": 3, "head": {{"ref": "feature/1-x", "sha": "s"}}, "base": {{"ref": "main"}}, "body": "Closes #1"}}))
    sys.exit(0)
sys.exit(1)
""")
    script.chmod(0o755)
    return script


def test_github_missing_vs_unreachable(fake_gh, tmp_path, monkeypatch):
    """API-01: missing is permanent and answered at once; unreachable is an error, never an answer."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    gh = GitHub("o/r", gh=str(fake_gh), retries=2)
    assert gh.issue(404).state == "MISSING"
    assert gh.issue(77).state == "MISSING"  # a PR number is not an issue
    i = gh.issue(1)
    assert i.state == "CLOSED" and i.labels == ["bug"]
    from nohalt.forge import ForgeError

    with pytest.raises(ForgeError):
        gh.issue(500)
    calls = (tmp_path / "calls").read_text().splitlines()
    assert sum("issues/404" in c for c in calls) == 1  # not retried
    assert sum("issues/500" in c for c in calls) == 2  # retried


def test_github_lists_prs_in_one_paginated_call(fake_gh, tmp_path):
    gh = GitHub("o/r", gh=str(fake_gh))
    prs = gh.open_pulls()
    assert prs[0].closes() == 1 and prs[0].head_ref == "feature/1-x"
    calls = (tmp_path / "calls").read_text()
    assert "--paginate" in calls and "tojson" in calls


def test_cli_init_and_dry_run(tmp_path, repo):
    import subprocess

    env = {**os.environ}
    cp = subprocess.run(
        [sys.executable, "-m", "nohalt", "init", "--dir", str(tmp_path), "--repo", "o/r"], capture_output=True, text=True, env=env
    )
    assert cp.returncode == 0, cp.stderr
    text = (
        (tmp_path / "nohalt.toml")
        .read_text()
        .replace('path = "."', f'path = "{repo}"')
        .replace('repo = "o/r"', f'repo = "local:{tmp_path}/f.json"')
    )
    text = text.replace("probe = true", "probe = false")
    (tmp_path / "nohalt.toml").write_text(text + "")
    from nohalt.forge.local import LocalForge

    LocalForge(tmp_path / "f.json").add_issue(4)
    (tmp_path / "queue.txt").write_text("4:\n5: 4\n")
    cp = subprocess.run(
        [sys.executable, "-m", "nohalt", "--config", str(tmp_path / "nohalt.toml"), "run", "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert cp.returncode == 0, cp.stderr
    assert "would launch now: #4" in cp.stdout
    cp = subprocess.run(
        [sys.executable, "-m", "nohalt", "--config", str(tmp_path / "nohalt.toml"), "status"], capture_output=True, text=True
    )
    assert cp.returncode == 0 and "supervisor: DOWN" in cp.stdout


def test_init_creates_the_directory(tmp_path):
    import subprocess

    d = tmp_path / "new" / "project"
    cp = subprocess.run(
        [sys.executable, "-m", "nohalt", "init", "--dir", str(d), "--repo", "o/r"], capture_output=True, text=True
    )
    assert cp.returncode == 0, cp.stderr
    assert (d / "nohalt.toml").exists() and (d / "queue.txt").exists()
