"""Release windows: a scheduled train with a hotfix lane (DEP-13)."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import git
from nohalt import cli
from nohalt import config as config_mod
from nohalt.integrate.train import RELEASE_NOW
from nohalt.integrate.windows import Schedule
from nohalt.state import READY
from test_reload import _toml

CUR = ZoneInfo("America/Curacao")  # UTC-4 all year
WIN = {"windows": ["05:00-06:00"], "timezone": "America/Curacao"}


def at(h: int, m: int = 0, day: int = 28) -> float:
    """A moment in October 2026, Curacao time. The 28th is a Wednesday."""
    return datetime(2026, 10, day, h, m, tzinfo=CUR).timestamp()


def make(world, train=None, **integrate):  # type: ignore[no-untyped-def]
    return world(integrate={"mode": "train", "base_pipeline_grace": 0, **integrate, "train": {**WIN, **(train or {})}})


def ready(w, repo, n: int, labels: list[str] | None = None) -> None:  # type: ignore[no-untyped-def]
    """Issue n with a green PR of its own (no conflicts), READY."""
    w.forge.add_issue(n, f"Issue {n}", labels=labels)
    b = f"feature/{n}-x"
    git(repo, "checkout", "-q", "-b", b, "main")
    (repo / f"f{n}.txt").write_text(f"{n}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"#{n}")
    git(repo, "push", "-q", "origin", b)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    w.forge.add_pull(100 + n, b, sha, body=f"Closes #{n}")
    w.forge.set_checks(sha, [("ci", "completed", "success")])
    r = w.sup.state.get(n)
    r.status, r.pr, r.pr_sha, r.branch = READY, 100 + n, sha, b
    w.sup.state.put(r)
    with open(w.queue, "a") as f:
        f.write(f"{n}:\n")


def tick(w, ts: float) -> None:  # type: ignore[no-untyped-def]
    w.sup._pulls, w.sup._pulls_ok = None, False
    w.sup.integrator.clock = lambda: ts
    w.sup.integrator.tick()


def releases(w):  # type: ignore[no-untyped-def]
    return [p for p in w.forge.open_pulls() if p.head_ref.startswith("release/")]


def carried(p) -> set[int]:  # type: ignore[no-untyped-def]
    return {int(x) for x in re.findall(r"Closes #(\d+)", p.body)}


def merge(w, ts: float) -> None:  # type: ignore[no-untyped-def]
    """The open release goes green and merges; the base pipeline has nothing to watch."""
    (rel,) = releases(w)
    w.forge.set_checks(rel.head_sha, [("ci", "completed", "success")])
    tick(w, ts)
    assert rel.number in w.forge.data()["merged"], w.messages


# -- the schedule ------------------------------------------------------------------------------


def test_a_window_is_open_only_inside_its_range():
    s = Schedule(["05:00-06:00"], "America/Curacao")
    assert s.current(at(4, 59)) is None
    assert s.current(at(5, 0)) is not None
    assert s.current(at(5, 59)) is not None
    assert s.current(at(6, 0)) is None  # the end is exclusive
    nxt = s.next(at(14))
    assert nxt and nxt.start.timestamp() == at(5, day=29)


def test_a_window_may_cross_midnight_and_days_name_the_day_it_starts():
    s = Schedule(["23:00-01:00"], "America/Curacao")
    assert s.current(at(23, 30)) is not None
    late = s.current(at(0, 30))
    assert late and late.start.timestamp() == at(23, day=27)  # the instance that began on Tuesday
    assert s.current(at(1, 30)) is None
    tue = Schedule(["23:00-01:00"], "America/Curacao", ["tue"])
    assert tue.current(at(0, 30)) is not None  # Wednesday 00:30 belongs to Tuesday's window
    assert tue.current(at(23, 30)) is None  # Wednesday's own window does not apply
    nxt = tue.next(at(1, 30))
    assert nxt and nxt.start.timestamp() == datetime(2026, 11, 3, 23, tzinfo=CUR).timestamp()


def test_the_time_zone_is_respected():
    utc_0930 = datetime(2026, 10, 28, 9, 30, tzinfo=ZoneInfo("UTC")).timestamp()  # 05:30 in Curacao
    assert Schedule(["05:00-06:00"], "America/Curacao").current(utc_0930) is not None
    assert Schedule(["05:00-06:00"], "UTC").current(utc_0930) is None
    assert Schedule(["09:00-10:00"], "UTC").current(utc_0930) is not None


@pytest.mark.parametrize(
    "train, err",
    [
        ({"windows": ["5-6"]}, "HH:MM-HH:MM"),
        ({"windows": ["05:00-05:00"]}, "empty"),
        ({"windows": ["24:00-01:00"]}, "HH:MM-HH:MM"),
        ({"timezone": "Mars/Olympus"}, "unknown time zone"),
        ({"days": ["mon", "funday"]}, "unknown day"),
        ({"per_window": -1}, "per_window"),
    ],
)
def test_bad_window_settings_are_config_errors(tmp_path, train, err):
    with pytest.raises(config_mod.ConfigError, match=err):
        config_mod.from_dict({"integrate": {"mode": "train", "train": train}}, root=tmp_path)


# -- the train ---------------------------------------------------------------------------------


def test_without_windows_the_train_releases_any_time(world, repo):
    w = world(integrate={"mode": "train"})
    ready(w, repo, 1)
    tick(w, at(14))
    (rel,) = releases(w)
    assert carried(rel) == {1}


def test_outside_the_window_no_batch_opens_and_the_log_says_why(world, repo):
    w = make(world)
    ready(w, repo, 1)
    ready(w, repo, 2)
    tick(w, at(14))
    assert releases(w) == []
    said = [m for m in w.messages if "no normal batch" in m]
    assert len(said) == 1 and "outside release window" in said[0], w.messages
    assert "2 ready issue(s) wait; 0 hotfix(es)" in said[0] and "in 15h 00m" in said[0]
    tick(w, at(14, 5))
    assert len([m for m in w.messages if "no normal batch" in m]) == 1  # said once, not every tick
    assert w.sup.state.load("train.json")["waiting"]["ready"] == [1, 2]


def test_a_hotfix_label_releases_outside_the_window_alone(world, repo):
    w = make(world)
    ready(w, repo, 1)
    ready(w, repo, 2, labels=["Hotfix"])
    tick(w, at(14))
    (rel,) = releases(w)
    assert carried(rel) == {2}  # #1 is green too, but does not ride along
    assert w.sup.state.load("train.json")["releases"][rel.head_ref]["kind"] == "hotfix"


def test_a_priority_issue_counts_as_a_hotfix(world, repo):
    w = make(world)
    ready(w, repo, 1)
    ready(w, repo, 2)
    w.sup.state.append_line("priority", "2")
    tick(w, at(14))
    (rel,) = releases(w)
    assert carried(rel) == {2}


def test_priority_need_not_mean_hotfix(world, repo):
    w = make(world, {"hotfix_priority": False})
    ready(w, repo, 1)
    w.sup.state.append_line("priority", "1")
    tick(w, at(14))
    assert releases(w) == []


def test_inside_the_window_one_batch_carries_everything(world, repo):
    w = make(world)
    ready(w, repo, 1)
    ready(w, repo, 2)
    tick(w, at(5, 10))
    (rel,) = releases(w)
    assert carried(rel) == {1, 2}
    rec = w.sup.state.load("train.json")["releases"][rel.head_ref]
    assert rec["kind"] == "normal" and rec["window"] == "2026-10-28 05:00-06:00"


def test_once_the_window_has_released_no_second_batch_opens_in_it(world, repo):
    w = make(world)
    ready(w, repo, 1)
    tick(w, at(5, 10))
    merge(w, at(5, 20))
    tick(w, at(5, 21))  # the base pipeline gate clears
    ready(w, repo, 2)
    tick(w, at(5, 30))
    assert releases(w) == [], w.messages
    assert any("has had its release (1/1)" in m for m in w.messages)
    tick(w, at(5, 10, day=29))  # the next day's window
    (rel,) = releases(w)
    assert carried(rel) == {2}


def test_per_window_zero_is_unlimited(world, repo):
    w = make(world, {"per_window": 0})
    ready(w, repo, 1)
    tick(w, at(5, 10))
    merge(w, at(5, 20))
    tick(w, at(5, 21))
    ready(w, repo, 2)
    tick(w, at(5, 30))
    assert len(releases(w)) == 1


def test_a_failed_batch_does_not_use_the_window(world, repo):
    w = make(world, {"red_retries": 0})
    ready(w, repo, 1)
    tick(w, at(5, 10))
    (rel,) = releases(w)
    w.forge.set_checks(rel.head_sha, [("ci", "completed", "failure")])
    tick(w, at(5, 20))  # red with no re-runs left: closed, #1 parked
    assert releases(w) == []
    ready(w, repo, 2)
    tick(w, at(5, 30))
    (again,) = releases(w)
    assert carried(again) == {2}  # the same window, a second try
    assert not w.sup.state.load("train.json").get("windows_used")


def test_days_limit_the_windows(world, repo):
    w = make(world, {"days": ["mon", "Tuesday"]})
    ready(w, repo, 1)
    tick(w, at(5, 10))  # a Wednesday
    assert releases(w) == []
    tick(w, at(5, 10, day=27))  # a Tuesday
    assert len(releases(w)) == 1


def test_an_open_release_proceeds_outside_the_window(world, repo):
    w = make(world)
    ready(w, repo, 1)
    tick(w, at(5, 50))
    merge(w, at(9))  # green after the window closed: merged, tagged, deployed as usual


def test_a_fix_forward_release_goes_outside_the_window(world, repo):
    """A red production base is fixed at once; with with_fix, only the fix goes outside a window."""
    w = make(world, red_base="with_fix")
    ready(w, repo, 1)
    ready(w, repo, 9)
    w.sup.state.save("integrate.json", {"fix_issue": 9})
    tick(w, at(14))
    (rel,) = releases(w)
    assert carried(rel) == {9}


def test_the_fix_only_release_goes_outside_the_window(world, repo):
    w = make(world)
    ready(w, repo, 9)
    w.sup.state.save("integrate.json", {"fix_issue": 9})
    tick(w, at(14))
    assert carried(releases(w)[0]) == {9}


def test_release_now_opens_one_normal_batch_once(world, repo, tmp_path, capsys):
    w = make(world)
    toml = tmp_path / "nohalt.toml"
    toml.write_text(_toml(w.data))
    ready(w, repo, 1)
    ready(w, repo, 2)
    assert cli.main(["--config", str(toml), "release-now"]) == 0
    assert "RELEASE_NOW set" in capsys.readouterr().out
    assert "RELEASE_NOW: a release-now request" in cli.render_status(w.cfg)
    tick(w, at(14))
    (rel,) = releases(w)
    assert carried(rel) == {1, 2}
    assert w.sup.state.load("train.json")["releases"][rel.head_ref]["kind"] == "manual"
    assert not w.sup.state.has(RELEASE_NOW)  # used once
    merge(w, at(14, 10))
    tick(w, at(14, 11))
    ready(w, repo, 3)
    tick(w, at(14, 20))
    assert releases(w) == []  # back on the schedule


def test_release_now_with_nothing_ready_is_dropped(world, repo):
    w = make(world)
    w.sup.state.set_flag(RELEASE_NOW)
    tick(w, at(14))
    assert not w.sup.state.has(RELEASE_NOW)
    assert any("nothing is ready" in m for m in w.messages)


def test_status_shows_the_next_window_and_waiting_hotfixes(world, repo, monkeypatch):
    w = make(world)
    ready(w, repo, 1)
    ready(w, repo, 2, labels=["hotfix"])
    w.sup.state.append_line("hold", "2")  # held: the hotfix waits, the train says so
    tick(w, at(14))
    monkeypatch.setattr(cli, "now", lambda: at(20, 48))
    out = cli.render_status(w.cfg)
    assert "next release window: 05:00-06:00 America/Curacao, in 8h 12m" in out
    assert "outside release window; 1 ready, hotfixes waiting: none" in out
    w.sup.state.save(
        "train.json", {**w.sup.state.load("train.json"), "waiting": {"at": at(14), "why": "x", "ready": [1, 2], "hotfixes": [2]}}
    )
    assert "hotfixes waiting: #2" in cli.render_status(w.cfg)
    monkeypatch.setattr(cli, "now", lambda: at(5, 15))
    assert "release window open: 05:00-06:00 America/Curacao, closes in 45m (0/1 released)" in cli.render_status(w.cfg)
