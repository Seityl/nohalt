"""After the merge: the post-deploy check (DEP-14) and "shipped means live" (DEP-15)."""

from __future__ import annotations

import subprocess
import sys

from conftest import git
from nohalt import cli
from nohalt.state import DONE, READY, RELEASED

T0 = 1_800_000_000.0


def direct(world, tmp_path, check="", **integrate):  # type: ignore[no-untyped-def]
    """Direct mode, no base pipeline to wait for, a deploy that succeeds."""
    ic = {"mode": "direct", "base_pipeline_grace": 0, "deploy": "true", "post_deploy_retries": 0, **integrate}
    if check:
        ic["post_deploy_check"] = check
    return world(integrate=ic)


def counter_check(tmp_path, passes_on: int) -> str:
    """A check that fails until its ``passes_on``-th run (a warming-up app)."""
    c = tmp_path / "runs"
    return f'n=$(( $(cat {c} 2>/dev/null || echo 0) + 1 )); echo $n > {c}; echo "try $n: home page 502"; [ $n -ge {passes_on} ]'


def runs(tmp_path) -> int:
    p = tmp_path / "runs"
    return int(p.read_text()) if p.exists() else 0


# -- post_deploy_check (DEP-14) ------------------------------------------------------------------


def test_a_failing_post_deploy_check_blocks_deploys_and_opens_one_issue(world, tmp_path):
    """DEP-14: a deploy that exits 0 is not a working app. A failed check stops the line like a failed deploy."""
    w = direct(world, tmp_path, check="echo checking /; echo 'GET /app -> 502 Bad Gateway'; exit 3")
    w.issue(1, "pr-green")
    w.issue(2, "pr-green")
    w.drive(6)
    assert len(w.forge.data()["merged"]) == 1, w.messages  # nothing after the failed check
    assert w.sup.state.has("DEPLOY_BLOCKED")
    flag = (w.sup.state.root / "DEPLOY_BLOCKED").read_text()
    assert "exited 3" in flag and "502 Bad Gateway" in flag
    opened = {int(k): i for k, i in w.forge.data()["issues"].items() if i["title"].startswith("Post-deploy check failed")}
    assert len(opened) == 1
    ((n, issue),) = opened.items()
    assert "GET /app -> 502 Bad Gateway" in issue["title"] and "502 Bad Gateway" in issue["body"]
    assert w.queue.read_text().splitlines()[0].startswith(f"{n}:")
    assert any("post-deploy check FAILED" in m for m in w.messages)


def test_the_check_gets_the_release_in_its_environment(world, tmp_path):
    out = tmp_path / "env.txt"
    w = direct(world, tmp_path, check=f"env | grep ^NOHALT_ | sort > {out}")
    gate = w.sup.integrator.gate
    gate.start_check({"what": "v1.4.2", "sha": "a" * 40, "tag": "v1.4.2", "issues": [7, 9]})
    env = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert env == {"NOHALT_ISSUES": "7 9", "NOHALT_SHA": "a" * 40, "NOHALT_TAG": "v1.4.2", "NOHALT_VERSION": "1.4.2"}
    gate.start_check({"what": "#3", "sha": "b" * 40, "issues": [3]})  # direct: no tag, the short SHA is the version
    assert "NOHALT_VERSION=" + "b" * 12 in out.read_text()


def test_the_check_is_retried_for_warm_up_without_blocking_a_tick(world, tmp_path):
    """DEP-14: workers take a while to come back. Retries wait between ticks, and merging waits for them."""
    w = direct(world, tmp_path, check=counter_check(tmp_path, passes_on=3), post_deploy_retries=2, post_deploy_delay=30)
    gate = w.sup.integrator.gate
    gate.clock = lambda: T0
    gate.start_check({"what": "#1", "sha": "c" * 40, "issues": [1]})
    assert runs(tmp_path) == 1
    assert gate.open() is False  # retrying: nothing merges meanwhile
    assert runs(tmp_path) == 1  # not due yet: not run again
    gate.clock = lambda: T0 + 31
    assert gate.open() is False and runs(tmp_path) == 2
    gate.clock = lambda: T0 + 62
    assert gate.open() is True and runs(tmp_path) == 3
    assert not w.sup.state.has("DEPLOY_BLOCKED")
    assert any("post-deploy check passed on cccccc" in m and "try 3" in m for m in w.messages), w.messages


def test_retries_used_up_is_a_failure(world, tmp_path):
    w = direct(world, tmp_path, check=counter_check(tmp_path, passes_on=9), post_deploy_retries=1, post_deploy_delay=10)
    gate = w.sup.integrator.gate
    gate.clock = lambda: T0
    gate.start_check({"what": "#1", "sha": "d" * 40, "issues": [1]})
    gate.clock = lambda: T0 + 11
    assert gate.open() is False
    assert runs(tmp_path) == 2
    assert w.sup.state.has("DEPLOY_BLOCKED") and "try 2: home page 502" in (w.sup.state.root / "DEPLOY_BLOCKED").read_text()


def test_only_the_fix_for_a_failed_check_merges_and_a_passing_check_lifts_the_block(world, tmp_path):
    """DEP-14: the failed check's issue goes through as a fix-forward; nothing else does until the check passes."""
    ok = tmp_path / "ok"
    w = direct(world, tmp_path, check=f"test -f {ok} || {{ echo 'login page 500'; exit 1; }}")
    gate = w.sup.integrator.gate
    gate.start_check({"what": "#1", "sha": "e" * 40, "issues": [1]})
    fix = gate.fix_issue()
    assert fix and w.sup.state.has("DEPLOY_BLOCKED")
    assert gate.open() is True and gate.only == fix  # its fix may merge, deploy and be checked
    ok.write_text("")
    gate.start_check({"what": f"#{fix}", "sha": "f" * 40, "issues": [fix]})
    assert not w.sup.state.has("DEPLOY_BLOCKED")
    assert gate.open() is True and gate.only is None
    assert any("DEPLOY_BLOCKED cleared" in m for m in w.messages)


def test_end_to_end_the_fix_ships_and_the_line_moves_again(world, tmp_path):
    """DEP-14: the check's issue is worked, its fix merges and deploys, the check passes, and the rest follows."""
    ok = tmp_path / "ok"
    w = direct(world, tmp_path, check=f"test -f {ok} || {{ echo 'dashboard 500'; exit 1; }}")
    w.issue(1, "pr-green")
    w.issue(2, "pr-green", deps=[1])
    for n in range(3, 12):  # whatever number the check's issue gets, its agent opens a green PR
        (w.scripts / f"{n}.txt").write_text("pr-green\n")
    w.drive(3)
    assert w.sup.state.has("DEPLOY_BLOCKED"), w.messages
    fix = w.sup.integrator.gate.fix_issue()
    assert fix
    ok.write_text("")  # what the fix repairs
    w.drive(8)
    merged = w.forge.data()["merged"]
    assert w.rec(fix).pr in merged and w.rec(2).pr in merged, w.messages
    assert merged.index(w.rec(fix).pr) < merged.index(w.rec(2).pr)
    assert not w.sup.state.has("DEPLOY_BLOCKED")


def test_a_second_failure_comments_on_the_open_issue(world, tmp_path):
    w = direct(world, tmp_path, check="echo 'still 502'; exit 1")
    gate = w.sup.integrator.gate
    gate.start_check({"what": "#1", "sha": "1" * 40, "issues": [1]})
    fix = gate.fix_issue()
    gate.start_check({"what": "#2", "sha": "2" * 40, "issues": [2]})
    assert gate.fix_issue() == fix
    titles = [i["title"] for i in w.forge.data()["issues"].values()]
    assert sum(t.startswith("Post-deploy check failed") for t in titles) == 1
    assert any(c[0] == fix and "failed again" in c[1] for c in w.forge.data()["comments"])


def test_a_failed_deploy_is_still_a_humans(world, tmp_path):
    """A deploy that fails outright never ran the check; DEPLOY_BLOCKED holds everything, as before."""
    w = direct(world, tmp_path, check="true")
    w.sup.state.set_flag("DEPLOY_BLOCKED", "deploy of x exited 1")
    assert w.sup.integrator.gate.open() is False
    assert w.sup.integrator.gate.fix_issue() is None


def test_a_failed_check_is_a_hotfix_outside_the_release_window(world, repo, tmp_path):
    """DEP-13 + DEP-14: the train releases the failed check's fix alone, outside any window."""
    from test_windows import WIN, at, carried, ready, releases, tick

    w = world(integrate={"mode": "train", "post_deploy_check": "exit 1", "post_deploy_retries": 0, "train": WIN})
    ready(w, repo, 1)
    ready(w, repo, 9)
    w.sup.integrator.gate.start_check({"what": "v0.1.0", "sha": "9" * 40, "issues": [5]})
    fix = w.sup.integrator.gate.fix_issue()
    assert fix
    w.forge.close_issue(fix)  # stands in for the agent's work: issue 9 is that fix
    w.sup.state.save("integrate.json", {**w.sup.state.load("integrate.json"), "check_block": {"issue": 9}})
    tick(w, at(14))
    (rel,) = releases(w)
    assert carried(rel) == {9}


def test_a_green_cd_workflow_is_the_deploy(world, tmp_path):
    """With cd_workflow, CD deploys: the check runs when it goes green, not after the deploy command."""
    from nohalt.forge import Run

    w = direct(world, tmp_path, check=counter_check(tmp_path, passes_on=1), cd_workflow="CD", deploy="", base_pipeline_grace="1h")
    w.issue(1, "pr-green")
    w.drive(4)
    sha = w.sup.state.load("integrate.json")["watch"]["sha"]
    assert runs(tmp_path) == 0
    w.forge.add_run(Run(5, "CD", sha, "completed", "success", "main", ""))
    w.drive(1)
    assert runs(tmp_path) == 1


# -- live_version (DEP-15) ------------------------------------------------------------------------


def commit(repo, name: str) -> str:
    (repo / name).write_text(name + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    git(repo, "push", "-q", "origin", "HEAD")
    return git(repo, "rev-parse", "HEAD")


def live(world, tmp_path, **integrate):  # type: ignore[no-untyped-def]
    return world(integrate={"mode": "direct", "live_version": f"cat {tmp_path}/live.txt", **integrate})


def test_an_issue_closes_only_once_the_live_version_contains_it(world, repo, tmp_path):
    """DEP-15: merged is not shipped. The issue stays open until production runs a commit containing its merge."""
    old = commit(repo, "a")
    merge = commit(repo, "b")
    w = live(world, tmp_path)
    w.forge.add_issue(1, "Issue 1")
    rec = w.sup.state.get(1)
    rec.status = READY
    w.sup.state.put(rec)
    w.sup.integrator.live.released([1], merge, "", "PR !1")
    assert w.rec(1).status == RELEASED
    assert any("not yet live" in c[1] for c in w.forge.data()["comments"] if c[0] == 1)
    (tmp_path / "live.txt").write_text(old + "\n")
    w.sup.integrator.live.tick()
    assert w.forge.issue(1).state == "OPEN"
    later = commit(repo, "c")  # production may run anything that contains the merge
    (tmp_path / "live.txt").write_text(f"deploying...\n{later}\n")
    w.sup.integrator.live.tick()
    assert w.forge.issue(1).state == "CLOSED"
    assert any(f"live in {later} (verified)" in c[1] for c in w.forge.data()["comments"] if c[0] == 1)
    assert w.sup.state.load("live.json")["pending"] == []


def test_a_live_tag_is_read_as_a_version(world, repo, tmp_path):
    merge = commit(repo, "b")
    git(repo, "tag", "v0.4.0")
    git(repo, "push", "-q", "origin", "v0.4.0")
    w = live(world, tmp_path)
    w.forge.add_issue(1, "Issue 1")
    w.forge.close_issue(1)  # the forge closed it on the merge (a closing keyword on the default branch)
    w.sup.integrator.live.released([1], merge, "v0.4.0", "release !9")
    (tmp_path / "live.txt").write_text("0.4.0\n")
    w.sup.integrator.live.tick()
    assert any("live in 0.4.0 (verified)" in c[1] for c in w.forge.data()["comments"] if c[0] == 1)


def test_released_issues_are_not_closed_relaunched_or_requeued_after_the_merge(world, repo, tmp_path):
    """DEP-15 end to end: merged into a non-default base, the issue is held open as released, then closed once live."""
    git(repo, "push", "-q", "origin", "main:develop")
    w = world(
        project={"base_branch": "develop"},
        integrate={"mode": "direct", "live_version": f"cat {tmp_path}/live.txt", "base_pipeline_grace": 0},
    )
    (tmp_path / "live.txt").write_text("nothing yet\n")
    w.issue(1, "pr-green")
    w.drive(6)
    assert w.forge.data()["merged"]
    assert w.forge.issue(1).state == "OPEN" and w.rec(1).status == RELEASED, w.messages
    assert len(w.prompts(1)) == 1  # not relaunched
    st = w.sup.state.load("live.json")
    st["pending"][0]["sha"] = w.rec(1).pr_sha  # the local forge's merge commit is not a real one; its head is
    w.sup.state.save("live.json", st)
    (tmp_path / "live.txt").write_text(w.rec(1).pr_sha + "\n")
    w.drive(2)
    assert w.forge.issue(1).state == "CLOSED" and w.rec(1).status == DONE


def test_not_live_in_time_is_a_warning_not_a_close(world, repo, tmp_path):
    merge = commit(repo, "b")
    w = live(world, tmp_path, live_timeout="20m")
    lw = w.sup.integrator.live
    lw.clock = lambda: T0
    w.forge.add_issue(1, "Issue 1")
    lw.released([1], merge, "v0.5.0", "release !3")
    (tmp_path / "live.txt").write_text("v0.4.9\n")
    lw.clock = lambda: T0 + 19 * 60
    lw.tick()
    assert not any("WARNING" in m for m in w.messages)
    lw.clock = lambda: T0 + 21 * 60
    lw.tick()
    lw.tick()
    warnings = [m for m in w.messages if "WARNING" in m]
    assert len(warnings) == 1 and "v0.5.0 (#1) merged 21m ago and is not live" in warnings[0]
    assert w.forge.issue(1).state == "OPEN"


def test_the_timeout_defaults_to_cd_timeout(world, tmp_path):
    w = live(world, tmp_path, cd_timeout="45m")
    assert w.sup.integrator.live.timeout() == 45 * 60


def test_status_shows_released_awaiting_live(world, repo, tmp_path, monkeypatch):
    merge = commit(repo, "b")
    w = live(world, tmp_path, live_timeout="10m")
    lw = w.sup.integrator.live
    lw.clock = lambda: T0
    w.forge.add_issue(1, "Issue 1")
    lw.released([1], merge, "v0.5.0", "release !3")
    monkeypatch.setattr(cli, "now", lambda: T0 + 300)
    assert "released, awaiting live: #1 in v0.5.0 (for 5m)" in cli.render_status(w.cfg)
    monkeypatch.setattr(cli, "now", lambda: T0 + 900)
    assert "OVERDUE" in cli.render_status(w.cfg)


# -- doctor ---------------------------------------------------------------------------------------------------


def doctor(tmp_path, repo, integrate: str, train: str = "") -> str:
    (tmp_path / "q.txt").write_text("")
    (tmp_path / "nohalt.toml").write_text(
        f'[project]\nrepo = "local:{tmp_path}/f.json"\npath = "{repo}"\nstate_dir = "{tmp_path}/s"\n'
        f'[queue]\nfile = "{tmp_path}/q.txt"\n[models]\ndefault = "command:x"\nprobe = false\n'
        f'[agents.options.command]\nargv = ["true"]\n[integrate]\n{integrate}\n'
        + (f"[integrate.train]\n{train}\n" if train else "")
    )
    cp = subprocess.run(
        [sys.executable, "-m", "nohalt", "--config", str(tmp_path / "nohalt.toml"), "doctor"], capture_output=True, text=True
    )
    return cp.stdout


def test_doctor_warns_about_a_deploy_without_a_post_deploy_check(tmp_path, repo):
    out = doctor(tmp_path, repo, 'mode = "direct"\ndeploy = "./deploy.sh"')
    assert "warn [integrate] deploy is set but post_deploy_check is not" in out, out
    out = doctor(tmp_path, repo, 'mode = "direct"\ndeploy = "./deploy.sh"\npost_deploy_check = "./smoke.sh"')
    assert "ok   post_deploy_check runs after each deploy" in out, out


def test_doctor_checks_the_release_windows(tmp_path, repo):
    out = doctor(tmp_path, repo, 'mode = "train"', 'windows = ["05:00-06:00", "05:30-07:00"]\ndays = ["mon", "tue"]')
    assert "release windows 05:00-06:00, 05:30-07:00" in out and "mon, tue" in out, out
    assert "warn [integrate.train] timezone is not set" in out
    assert "warn [integrate.train] windows overlap" in out
    out = doctor(tmp_path, repo, 'mode = "direct"', 'windows = ["05:00-06:00"]\ntimezone = "America/Curacao"')
    assert "windows is set but mode is 'direct'" in out, out
    out = doctor(tmp_path, repo, 'mode = "train"', 'timezone = "America/Curacao"')
    assert "timezone or days is set but windows is empty" in out, out
    out = doctor(tmp_path, repo, 'mode = "train"', 'windows = ["5-6"]')
    assert "FAIL config" in out and "HH:MM-HH:MM" in out, out
