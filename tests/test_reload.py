"""Config changes apply without a human restart; `restart`, `stop` and `resume` say what they do (LIV-12)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nohalt import cli
from nohalt import config as config_mod
from nohalt.state import RUNNING, FileLock
from nohalt.supervisor import Supervisor
from nohalt.util import pid_alive

SRC = Path(__file__).resolve().parents[1] / "src"


def _toml(data: dict[str, Any], prefix: str = "") -> str:
    """Just enough TOML for the test configuration: tables, strings, numbers, booleans, lists."""
    out, tables = [], []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            out.append(f"{json.dumps(k)} = {json.dumps(v)}")
    text = (f"[{prefix}]\n" if prefix and out else "") + "".join(ln + "\n" for ln in out)
    for k, v in tables:
        text += "\n" + _toml(v, f"{prefix}.{json.dumps(k)}" if prefix else json.dumps(k))
    return text


class Exec(Exception):
    pass


@pytest.fixture
def on_disk(world, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """A World whose configuration is a real nohalt.toml, and an execv that records instead of replacing pytest."""
    monkeypatch.chdir(tmp_path)  # _reexec changes directory; put it back afterwards
    execs: list[list[str]] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        execs.append(argv)
        raise Exec(argv)

    monkeypatch.setattr("nohalt.supervisor.os.execv", fake_execv)

    def make(**overrides: Any):  # type: ignore[no-untyped-def]
        w = world(**overrides)
        w.toml = w.tmp / "nohalt.toml"
        w.toml.write_text(_toml(w.data))
        w.cfg = config_mod.load(w.toml)
        w.sup = Supervisor(w.cfg, forge=w.forge, say=w.messages.append)
        w.execs = execs

        def edit(section: str, **values: Any) -> None:
            w.data[section].update(values)
            w.toml.write_text(_toml(w.data))

        w.edit = edit
        return w

    return make


def test_a_changed_config_is_applied_on_the_next_tick_and_agents_are_adopted(on_disk):
    """LIV-12: the operator edits nohalt.toml; the supervisor re-executes and adopts its agents."""
    w = on_disk(agents={"max_parallel": 1})
    w.issue(1, "sleep 3", "close")
    w.issue(2, "close")
    w.sup.tick()
    pid = w.rec(1).pid
    assert w.rec(1).status == RUNNING and not w.rec(2).started_at

    w.edit("agents", max_parallel=2)
    with pytest.raises(Exec):
        w.sup.run()
    argv = w.execs[-1]
    assert argv[:3] == [sys.executable, "-m", "nohalt"] and argv[-1] == "run"
    assert argv[argv.index("--config") + 1] == str(w.toml)
    assert any("changed: re-executing" in m for m in w.messages)
    lock = FileLock(w.sup.state.root / "supervisor.lock")
    assert lock.acquire(blocking=False), "the lock is released before exec"
    lock.release()

    # What the re-executed process does: load the file, adopt, launch with the new caps.
    new = Supervisor(config_mod.load(w.toml), forge=w.forge, say=w.messages.append)
    assert new.cfg.agents.max_parallel == 2
    new.tick()
    assert w.rec(1).status == RUNNING and w.rec(1).pid == pid  # adopted, not relaunched
    assert w.rec(2).started_at  # the second slot is used at once


def test_an_invalid_config_keeps_the_old_one_and_raises_a_flag(on_disk):
    w = on_disk()
    good = w.toml.read_text()
    w.toml.write_text(good + "\n[agents\n")
    assert w.sup.check_config() is False
    assert w.sup.state.has("CONFIG_INVALID")
    assert "nohalt.toml" in w.sup.state.flag("CONFIG_INVALID").read_text()
    assert w.sup.cfg.agents.max_parallel == 2  # still the old configuration
    assert w.sup.check_config() is False
    assert sum("is invalid" in m for m in w.messages) == 1  # reported once, not every tick
    status = cli.render_status(w.sup.cfg)
    assert "CONFIG_INVALID: nohalt.toml has an error" in status

    w.edit("agents", max_paralel=3)  # a typo is an error too, not a silent default
    assert w.sup.check_config() is False
    assert "max_paralel" in w.sup.state.flag("CONFIG_INVALID").read_text()

    w.toml.write_text(good)  # reverted
    assert w.sup.check_config() is False
    assert not w.sup.state.has("CONFIG_INVALID")

    w.toml.write_text(good + "\n[agents\n")
    w.sup.check_config()
    del w.data["agents"]["max_paralel"]
    w.edit("agents", max_parallel=3)  # fixed with a real change
    assert w.sup.check_config() is True
    assert not w.sup.state.has("CONFIG_INVALID")


def test_a_comment_only_edit_does_not_restart(on_disk):
    w = on_disk()
    w.toml.write_text("# tuned on Tuesday\n" + w.toml.read_text())
    assert w.sup.check_config() is False
    assert w.sup.cfg.source_hash == config_mod.fingerprint(w.toml)
    assert not w.sup._restart_pending()


def test_moving_the_state_dir_is_refused_while_running(on_disk):
    w = on_disk()
    w.edit("project", state_dir=str(w.tmp / "elsewhere"))
    assert w.sup.check_config() is False
    assert "state_dir" in w.sup.state.flag("CONFIG_INVALID").read_text()


def test_a_restart_flag_re_executes_without_touching_agents(on_disk):
    w = on_disk()
    w.issue(1, "sleep 3", "close")
    w.sup.tick()
    pid = w.rec(1).pid
    w.sup.state.set_flag("RESTART")
    with pytest.raises(Exec):
        w.sup.run()
    assert not w.sup.state.has("RESTART")
    assert pid_alive(pid) and w.rec(1).status == RUNNING


def test_stop_drains_loudly_and_resume_clears_it(world, capsys, tmp_path):
    """LIV-05 kept, and said out loud: stop is a drain, resume undoes it."""
    w = world()
    toml = tmp_path / "nohalt.toml"
    toml.write_text(_toml(w.data))
    assert cli.main(["--config", str(toml), "stop"]) == 0
    out = capsys.readouterr().out
    assert "DRAINING" in out and "nohalt resume" in out and "nohalt restart" in out
    assert w.sup.state.has("STOP")
    assert "STOP: draining" in cli.render_status(w.cfg)

    assert cli.main(["--config", str(toml), "pause"]) == 0
    assert cli.main(["--config", str(toml), "resume"]) == 0
    out = capsys.readouterr().out
    assert "cleared STOP PAUSE" in out and "not running" in out
    assert not w.sup.state.has("STOP") and not w.sup.state.has("PAUSE")
    w.issue(1, "close")
    w.drive(2)
    assert w.rec(1).started_at  # launching resumed


def _wait(cond, timeout: float = 30) -> None:  # type: ignore[no-untyped-def]
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_restart_command_end_to_end(world, monkeypatch, tmp_path, capsys):
    """LIV-12: `nohalt restart` with no cron: starts a supervisor, restarts a running one, agents adopted."""
    monkeypatch.setenv("PYTHONPATH", f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    w = world()
    toml = tmp_path / "nohalt.toml"
    toml.write_text(_toml(w.data))
    w.issue(1, "sleep 8", "close")
    state = w.sup.state
    conf = ["--config", str(toml)]
    sup_pid = 0
    try:
        assert cli.main([*conf, "stop"]) == 0
        assert cli.main([*conf, "restart", "--timeout", "30"]) == 0  # nothing running: starts one, clears STOP
        out = capsys.readouterr().out
        assert "cleared STOP" in out and "started one" in out
        _wait(lambda: state.get(1).status == RUNNING and state.get(1).pid > 0)
        agent = state.get(1).pid
        sup_pid = state.load("supervisor.json")["pid"]
        first = state.load("supervisor.json")["started"]

        assert cli.main([*conf, "restart", "--timeout", "30"]) == 0
        out = capsys.readouterr().out
        assert "supervisor restarted" in out and "1 running agent(s)" in out
        assert state.load("supervisor.json")["started"] > first
        assert pid_alive(agent) and state.get(1).pid == agent and state.get(1).status == RUNNING

        # A config edit is picked up by the running supervisor on its own.
        w.data["agents"]["max_parallel"] = 3
        toml.write_text(_toml(w.data))
        _wait(lambda: "up to 3 agents" in (state.logs_dir / "nohalt.log").read_text())
        assert state.get(1).pid == agent

        # The adopted agent's exit is noticed (and reaped) by the re-executed supervisor.
        _wait(lambda: state.get(1).status == "done", timeout=60)
        assert len(w.prompts(1)) == 2
    finally:
        held = FileLock(state.root / "supervisor.lock").held_by() or sup_pid
        if held and pid_alive(held):
            os.kill(held, signal.SIGTERM)
            _wait(lambda: not pid_alive(held))
        rec = state.get(1)
        if rec.pid and pid_alive(rec.pid):
            subprocess.run(["kill", str(rec.pid)], check=False)
