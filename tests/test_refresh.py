"""A conflicting PR whose conflicts are all bookkeeping is refreshed by nohalt, not handed back (INT-12).

Every test runs real merges in real repositories: a bare origin, a PR branch
and a base that moved under it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from conftest import git
from nohalt.cli import render_status
from nohalt.integrate import refresh as refresh_mod
from nohalt.integrate.mergers import effective
from nohalt.integrate.refresh import (
    CONFLICT,
    DEFERRED,
    before_hand_back,
    left_to_batch,
    record_conflicts,
    recurring_conflicts,
    refresh,
)
from nohalt.state import IN_REVIEW, READY, RUNNING, WAITING_CI


def pull_comments(w) -> list[str]:  # type: ignore[no-untyped-def]
    return [c[1] for c in w.forge.data().get("pull_comments", [])]


VERSION = {"path": "pkg/__init__.py", "pattern": '__version__ = "([^"]+)"'}
PKG_VERSION = {"path": "package.json", "pattern": '"version": "([^"]+)"'}


def j(d: object) -> str:
    return json.dumps(d, indent=2) + "\n"


def write(repo: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def commit(repo: Path, files: dict[str, str], msg: str) -> str:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def show(repo: Path, ref: str, path: str) -> str:
    origin = repo.parent / "origin.git"
    return subprocess.run(["git", "-C", str(origin), "show", f"{ref}:{path}"], capture_output=True, text=True).stdout


def remote_sha(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo.parent / "origin.git"), "rev-parse", ref], capture_output=True, text=True
    ).stdout.strip()


BASE_FILES = {
    "pkg/__init__.py": '__version__ = "1.0.0"\n\n\ndef hello():\n    return "hi"\n',
    "package.json": j({"name": "x", "version": "1.0.0", "dependencies": {"a": "1"}}),
    "translations.csv": "hello,hola\n",
    "docs/_generated/ref.md": "hello,hola\n",
    "app.py": "x = 0\n",
}


def scenario(repo: Path, pr: dict[str, str], base: dict[str, str], branch: str = "feature/1-x") -> tuple[str, str]:
    """main with BASE_FILES; ``branch`` changes ``pr``; then main moves on with ``base``. -> (pr head, base head)."""
    commit(repo, BASE_FILES, "base files")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-b", branch)
    head = commit(repo, pr, "pr work")
    git(repo, "push", "-q", "origin", branch)
    git(repo, "checkout", "-q", "main")
    base_sha = commit(repo, base, "base moves on")
    git(repo, "push", "-q", "origin", "main")
    return head, base_sha


def conflicting_pr(w, head: str, branch: str = "feature/1-x", status: str = WAITING_CI) -> None:  # type: ignore[no-untyped-def]
    w.forge.add_issue(1)
    w.forge.add_pull(101, branch, head, body="Closes #1", mergeable=False)
    r = w.sup.state.get(1)
    r.status, r.pr, r.pr_sha, r.branch = status, 101, head, branch
    w.sup.state.put(r)
    w.queue.write_text("1:\n")


def test_version_only_conflict_is_refreshed_not_handed_back(world, repo):
    """The incident behind INT-12: version stamps bumped on both sides; no driver configured for them."""
    pr = {
        # An agent-side hook bumped the version; the agent also changed real code in the same file.
        "pkg/__init__.py": '__version__ = "1.0.7"\n\n\ndef hello():\n    return "hello"\n',
        "package.json": j({"name": "x", "version": "1.0.7", "dependencies": {"a": "1", "b": "2"}}),
        "feature.py": "y = 1\n",
    }
    base = {
        "pkg/__init__.py": '__version__ = "1.1.0"\n\n\ndef hello():\n    return "hi"\n',
        "package.json": j({"name": "x", "version": "1.1.0", "dependencies": {"a": "1"}}),
    }
    head, base_sha = scenario(repo, pr, base)
    w = world(integrate={"mode": "direct", "train": {"version_files": [VERSION, PKG_VERSION]}})
    conflicting_pr(w, head)
    agent_tree = w.sup.trees.prepare(1, "feature/1-x")
    w.sup.tick()

    new = remote_sha(repo, "feature/1-x")
    assert new != head, w.messages
    git(repo, "fetch", "-q", "origin")
    # A merge of the base INTO the branch (first parent: the PR head), so the push is a fast-forward.
    assert git(repo, "rev-list", "--parents", "-n1", new).split()[1:] == [head, base_sha]
    assert "merge: main into feature/1-x (nohalt refresh)" in git(repo, "log", "-1", "--format=%B", new)
    init = show(repo, "feature/1-x", "pkg/__init__.py")
    assert '__version__ = "1.1.0"' in init  # the base's version wins ...
    assert 'return "hello"' in init  # ... and the PR's other edit to the file survives
    pkg = json.loads(show(repo, "feature/1-x", "package.json"))
    assert pkg["version"] == "1.1.0" and pkg["dependencies"] == {"a": "1", "b": "2"}
    assert show(repo, "feature/1-x", "feature.py") == "y = 1\n"

    rec = w.rec(1)
    assert rec.ci_rounds == 0 and rec.status == WAITING_CI and rec.pr_sha == new
    assert not [c for c in w.forge.data()["comments"] if c[0] == 1]  # nobody was handed anything
    assert w.sup.state.load("refresh.json")["1"]["count"] == 1
    assert any("refreshed by nohalt" in m and "not handed back" in m for m in w.messages)
    assert git(agent_tree, "rev-parse", "HEAD") == new  # the agent's tree follows, its next push is not refused


def test_translations_keep_both_and_the_regenerate_command_runs(world, repo):
    pr = {"translations.csv": "hello,hola\nbye,adios\n", "docs/_generated/ref.md": "hello,hola\nbye,adios\n"}
    base = {"translations.csv": "hello,hola\nyes,si\n", "docs/_generated/ref.md": "hello,hola\nyes,si\n"}
    head, base_sha = scenario(repo, pr, base)
    w = world(
        integrate={
            "mode": "direct",
            "mergers": {"translations.csv": "keep_both_lines", "docs/_generated/**": "take_theirs"},
            "train": {"after_version": "sort translations.csv > docs/_generated/ref.md && touch docs/_generated/new.md"},
        }
    )
    conflicting_pr(w, head)
    w.sup.tick()

    new = remote_sha(repo, "feature/1-x")
    assert new != head, w.messages
    assert show(repo, "feature/1-x", "translations.csv").split() == ["hello,hola", "yes,si", "bye,adios"]
    assert show(repo, "feature/1-x", "docs/_generated/ref.md").split() == ["bye,adios", "hello,hola", "yes,si"]
    git(repo, "fetch", "-q", "origin")
    # The regenerated outputs are their own commit on top of the merge, and nothing else is in it.
    assert git(repo, "log", "-1", "--format=%s", new) == "chore: regenerate after merging main (nohalt refresh)"
    assert set(git(repo, "diff", "--name-only", f"{new}^", new).split()) == {"docs/_generated/ref.md", "docs/_generated/new.md"}
    assert git(repo, "rev-list", "--parents", "-n1", f"{new}^").split()[1:] == [head, base_sha]
    assert w.rec(1).ci_rounds == 0


def test_a_real_code_conflict_is_still_handed_back(world, repo):
    head, _ = scenario(repo, {"app.py": "x = 1\n", "pkg/__init__.py": '__version__ = "1.0.1"\n'}, {"app.py": "x = 2\n"})
    w = world(integrate={"mode": "direct", "train": {"version_files": [VERSION]}})
    conflicting_pr(w, head)
    w.sup.tick()
    w.wait_agents()
    assert remote_sha(repo, "feature/1-x") == head  # nothing pushed
    rec = w.rec(1)
    assert rec.ci_rounds == 1
    comments = [c[1] for c in w.forge.data()["comments"] if c[0] == 1]
    assert comments and "conflicts with" in comments[-1]
    events = w.sup.state.load("conflicts.json")["events"]
    assert events[-1]["files"] == ["app.py"]
    assert "refresh.json" not in [p.name for p in w.sup.state.root.iterdir()]


def test_running_agent_is_never_refreshed_but_deferred(world, repo):
    head, _ = scenario(repo, {"translations.csv": "hello,hola\nbye,adios\n"}, {"translations.csv": "hello,hola\nyes,si\n"})
    w = world(integrate={"mode": "train", "mergers": {"translations.csv": "keep_both_lines"}})
    conflicting_pr(w, head, status=RUNNING)
    rec = w.rec(1)
    pr = w.forge.pull(101)
    out = refresh(w.sup, rec, pr)
    assert out.status == DEFERRED and "running" in out.why
    assert before_hand_back(w.sup, rec, pr)  # deferred: no hand-back this tick either
    assert remote_sha(repo, "feature/1-x") == head
    assert w.rec(1).ci_rounds == 0
    rec.status = WAITING_CI  # the agent finished: the next tick refreshes
    assert refresh(w.sup, rec, pr).status == "refreshed"


def test_refresh_conflicts_off_hands_back_as_before(world, repo):
    head, _ = scenario(repo, {"translations.csv": "hello,hola\nbye,adios\n"}, {"translations.csv": "hello,hola\nyes,si\n"})
    w = world(integrate={"mode": "direct", "refresh_conflicts": False, "mergers": {"translations.csv": "keep_both_lines"}})
    conflicting_pr(w, head)
    w.sup.tick()
    w.wait_agents()
    assert remote_sha(repo, "feature/1-x") == head
    assert w.rec(1).ci_rounds == 1


def test_user_config_beats_the_implicit_version_driver(world, repo):
    head, _ = scenario(repo, {"pkg/__init__.py": '__version__ = "1.0.1"\n'}, {"pkg/__init__.py": '__version__ = "1.1.0"\n'})
    w = world(integrate={"mode": "direct", "mergers": {"pkg/*.py": "fail"}, "train": {"version_files": [VERSION]}})
    conflicting_pr(w, head)
    rec, pr = w.rec(1), w.forge.pull(101)
    out = refresh(w.sup, rec, pr)
    assert out.status == CONFLICT and out.files == ["pkg/__init__.py"]
    assert remote_sha(repo, "feature/1-x") == head


def test_implicit_drivers_for_version_files():
    vfs = [VERSION, PKG_VERSION, {"path": "web/package.json", "pattern": "x"}]
    assert effective({}, vfs) == {
        "pkg/__init__.py": "version_lines",
        "package.json": "package_json",
        "web/package.json": "package_json",
    }
    # Anything the user (or the stack) mapped is left alone, even by a broader glob.
    got = effective({"**/package.json": "fail", "pkg/*": "take_theirs"}, vfs)
    assert got == {"**/package.json": "fail", "pkg/*": "take_theirs"}


def test_train_merges_a_pr_through_a_version_file_with_no_configured_driver(world, repo):
    """In a release the base side is "ours" too: the PR's other edits to a version file survive."""
    git(repo, "tag", "v1.0.0")
    head, _ = scenario(
        repo,
        {"pkg/__init__.py": '__version__ = "1.0.5"\n\n\ndef hello():\n    return "hello"\n'},
        {"pkg/__init__.py": '__version__ = "1.0.0"  # stamped\n\n\ndef hello():\n    return "hi"\n'},
    )
    git(repo, "push", "-q", "origin", "--tags")
    w = world(integrate={"mode": "train", "train": {"version_files": [VERSION]}})
    w.forge.add_issue(1)
    w.forge.add_pull(101, "feature/1-x", head, body="Closes #1")
    w.forge.set_checks(head, [("ci", "completed", "success")])
    r = w.rec(1)
    r.status, r.pr, r.pr_sha, r.branch = READY, 101, head, "feature/1-x"
    w.sup.state.put(r)
    w.queue.write_text("1:\n")
    w.sup.tick()
    rel = [p for p in w.forge.open_pulls() if p.head_ref.startswith("release/")]
    assert len(rel) == 1 and "Closes #1" in rel[0].body, w.messages
    init = show(repo, rel[0].head_ref, "pkg/__init__.py")
    assert '__version__ = "1.0.1"  # stamped' in init  # the base's line kept, then stamped
    assert 'return "hello"' in init
    assert w.rec(1).ci_rounds == 0


def test_doctor_names_files_that_keep_conflicting_without_a_driver(world):
    w = world(integrate={"mode": "direct", "mergers": {"locale/*.csv": "keep_both_lines"}})
    st = w.sup.state
    record_conflicts(st, 1, ["src/hot.py", "CHANGES.md"])
    record_conflicts(st, 2, ["src/hot.py", "CHANGES.md", "locale/fr.csv"])
    record_conflicts(st, 3, ["locale/fr.csv", "once.txt"])
    got = recurring_conflicts(w.cfg, st)
    assert [(p, n) for p, n, _ in got] == [("CHANGES.md", 2), ("src/hot.py", 2)]  # fr.csv has a driver, once.txt is once
    assert "keep_both_blocks" in dict((p, h) for p, _, h in got)["CHANGES.md"]


def test_doctor_warns_about_a_recurring_conflict(tmp_path, repo):
    import sys

    from nohalt.state import StateStore

    (tmp_path / "q.txt").write_text("1:\n")
    (tmp_path / "nohalt.toml").write_text(
        f'[project]\nrepo = "local:{tmp_path}/f.json"\npath = "{repo}"\nstate_dir = "{tmp_path}/s"\n'
        f'[queue]\nfile = "{tmp_path}/q.txt"\n[models]\ndefault = "command:x"\nprobe = false\n'
        '[agents.options.command]\nargv = ["true"]\n'
    )
    st = StateStore(tmp_path / "s")
    record_conflicts(st, 1, ["locale/fr.csv"])
    record_conflicts(st, 2, ["locale/fr.csv"])
    cp = subprocess.run(
        [sys.executable, "-m", "nohalt", "--config", str(tmp_path / "nohalt.toml"), "doctor"], capture_output=True, text=True
    )
    assert "warn locale/fr.csv was left in conflict at 2 hand-backs" in cp.stdout, cp.stdout
    assert '"keep_both_lines"' in cp.stdout


# -- the refresh and the review gate (#69) ----------------------------------------------------------------
REVIEW = {"enabled": True, "labels": ["careful"], "model": "command:reviewer"}
TRANSLATIONS = {"mergers": {"translations.csv": "keep_both_lines"}}


def approved_pr(w, head: str, status: str = READY) -> None:  # type: ignore[no-untyped-def]
    """PR 101 for issue #1 (needs review), green and approved on ``head``, conflicting with the base."""
    conflicting_pr(w, head, status=status)
    with w.forge._db() as d:
        d["issues"]["1"]["labels"] = ["careful"]
    w.forge.set_checks(head, [("ci", "completed", "success")])
    r = w.rec(1)
    r.review_verdict, r.review_sha, r.review_approved, r.review_model = "approve", head, head, "command:reviewer"
    w.sup.state.put(r)


def move_pull(w, sha: str) -> None:  # type: ignore[no-untyped-def]
    with w.forge._db() as d:
        d["pulls"]["101"]["head_sha"] = sha
        d["pulls"]["101"]["mergeable"] = True


def test_a_pr_in_review_is_not_refreshed_until_its_verdict(world, repo):
    """INT-16 (#69): a refresh during a review moved the head, which stopped the review and started it again."""
    head, _ = scenario(repo, {"translations.csv": "hello,hola\nbye,adios\n"}, {"translations.csv": "hello,hola\nyes,si\n"})
    w = world(integrate={"mode": "direct", **TRANSLATIONS}, review=REVIEW)
    conflicting_pr(w, head, status=IN_REVIEW)
    rec, pr = w.rec(1), w.forge.pull(101)
    out = refresh(w.sup, rec, pr)
    assert out.status == DEFERRED and "in review" in out.why
    assert before_hand_back(w.sup, rec, pr)  # no hand-back either: it waits, as for a running agent
    assert remote_sha(repo, "feature/1-x") == head
    assert w.rec(1).ci_rounds == 0 and w.rec(1).status == IN_REVIEW


def test_train_leaves_a_conflict_its_batch_settles_unrefreshed(world, repo):
    """INT-16 (#69): in train mode the batch settles bookkeeping with the same drivers, so the head is left alone."""
    head, _ = scenario(repo, {"translations.csv": "hello,hola\nbye,adios\n"}, {"translations.csv": "hello,hola\nyes,si\n"})
    w = world(integrate={"mode": "train", **TRANSLATIONS}, review=REVIEW)
    approved_pr(w, head, status=WAITING_CI)
    pr = w.forge.pull(101)
    w.sup._watch(w.rec(1), SimpleNamespace())
    assert remote_sha(repo, "feature/1-x") == head  # not refreshed
    assert w.rec(1).status == READY and w.rec(1).ci_rounds == 0  # the approval of this head stands
    assert "refresh.json" not in [p.name for p in w.sup.state.root.iterdir()]
    assert left_to_batch(w.sup, w.rec(1), pr)  # asked again: answered from the cache
    # Not green yet (a conflicted PR may get no CI): then the refresh is what unblocks it.
    w.forge.set_checks(head, [])
    assert not w.sup._conflict_left_to_batch(w.rec(1), pr, "pending")


def test_left_to_the_batch_is_logged_once_per_head_and_base(world, repo):
    """INT-18 (#78): it was logged on every tick, and the trial merge repeated whenever the in-memory cache missed."""
    head, _ = scenario(repo, {"translations.csv": "hello,hola\nbye,adios\n"}, {"translations.csv": "hello,hola\nyes,si\n"})
    w = world(integrate={"mode": "train", **TRANSLATIONS})
    conflicting_pr(w, head)
    w.forge.set_checks(head, [("ci", "completed", "success")])
    log = w.sup.state.logs_dir / "nohalt.log"

    def said() -> int:
        return log.read_text().count("left to the release batch") if log.exists() else 0

    for _ in range(3):
        w.sup._watch(w.rec(1), SimpleNamespace())
    assert said() == 1
    base = remote_sha(repo, "main")
    assert w.rec(1).left_to_batch == f"{head}:{base}"
    # The answer is kept with the state, not in the process: a restarted supervisor does not merge again.
    assert w.sup.state.load("trial_merges.json") == {f"{head}:{base}": True}
    assert refresh(w.sup, w.rec(1), w.forge.pull(101), dry=True).why == "cached"
    # The base moves: a new trial merge, said once more.
    git(repo, "checkout", "-q", "main")
    commit(repo, {"other.txt": "x\n"}, "base moves")
    git(repo, "push", "-q", "origin", "main")
    refresh_mod._fetched.clear()
    w.sup._watch(w.rec(1), SimpleNamespace())
    w.sup._watch(w.rec(1), SimpleNamespace())
    assert said() == 2 and len(w.sup.state.load("trial_merges.json")) == 2


def test_train_still_hands_back_a_code_conflict(world, repo):
    head, _ = scenario(repo, {"app.py": "x = 1\n"}, {"app.py": "x = 2\n"})
    w = world(integrate={"mode": "train"})
    conflicting_pr(w, head)
    w.forge.set_checks(head, [("ci", "completed", "success")])
    assert not left_to_batch(w.sup, w.rec(1), w.forge.pull(101))
    w.sup._watch(w.rec(1), SimpleNamespace())
    assert w.rec(1).ci_rounds == 1  # handed back as before


def test_an_approval_survives_a_refresh_that_only_settles_bookkeeping(world, repo):
    """INT-16 (#69): the refresh changed a driver path, regenerated an output and brought in the base's own change."""
    head, _ = scenario(
        repo,
        {"translations.csv": "hello,hola\nbye,adios\n"},
        {"translations.csv": "hello,hola\nyes,si\n", "app.py": "x = 2  # another PR, already released\n"},
    )
    w = world(
        integrate={
            "mode": "direct",
            **TRANSLATIONS,
            "train": {"after_version": "sort translations.csv > docs/_generated/ref.md"},
        },
        review=REVIEW,
    )
    approved_pr(w, head)
    out = refresh(w.sup, w.rec(1), w.forge.pull(101))
    assert out.status == "refreshed", out.why
    r = w.rec(1)
    assert r.review_approved == out.sha and r.review_carried == head and r.status == WAITING_CI
    assert any("carries to" in c for c in pull_comments(w))
    assert any("approval carried" in m for m in w.messages)

    # CI is not carried: no checks on the new head yet, so it is neither ready nor merged.
    move_pull(w, out.sha)
    w.sup.tick()
    assert w.rec(1).status == WAITING_CI and not w.forge.data()["merged"]
    w.forge.set_checks(out.sha, [("ci", "completed", "success")])
    w.sup.tick()
    assert w.forge.data()["merged"] == [101], w.messages  # green, and still approved: no new review
    assert not w.review_prompts(1)
    assert any("carried over nohalt refreshes" in e for e in w.rec(1).history)


def test_an_agent_push_after_a_carried_approval_needs_a_new_review(world, repo):
    head, _ = scenario(repo, {"translations.csv": "hello,hola\nbye,adios\n"}, {"translations.csv": "hello,hola\nyes,si\n"})
    w = world(integrate={"mode": "direct", **TRANSLATIONS}, review=REVIEW)
    approved_pr(w, head)
    out = refresh(w.sup, w.rec(1), w.forge.pull(101))
    assert w.rec(1).review_approved == out.sha
    # The agent (or a human) pushes on top of the refresh.
    git(repo, "fetch", "-q", "origin")
    git(repo, "checkout", "-q", "-B", "feature/1-x", "origin/feature/1-x")
    pushed = commit(repo, {"feature.py": "late = True\n"}, "agent work")
    git(repo, "push", "-q", "origin", "feature/1-x")
    move_pull(w, pushed)
    w.forge.set_checks(pushed, [("ci", "completed", "success")])
    w.sup._watch(w.rec(1), SimpleNamespace())
    r = w.rec(1)
    assert r.status == IN_REVIEW and r.review_approved == out.sha


def test_a_refresh_that_changes_code_does_not_carry_the_approval(world, repo):
    """Both sides changed lib.py and git merged it: that is new code next to the PR's, so it is reviewed again."""
    base_lib = "a = 1\n\n\n\n\nb = 1\n"
    commit(repo, {"lib.py": base_lib}, "app")
    head, _ = scenario(
        repo,
        {"translations.csv": "hello,hola\nbye,adios\n", "lib.py": base_lib.replace("a = 1", "a = 2")},
        {"translations.csv": "hello,hola\nyes,si\n", "lib.py": base_lib.replace("b = 1", "b = 2")},
    )
    w = world(integrate={"mode": "direct", **TRANSLATIONS}, review=REVIEW)
    approved_pr(w, head)
    out = refresh(w.sup, w.rec(1), w.forge.pull(101))
    assert out.status == "refreshed", out.why
    r = w.rec(1)
    assert r.review_approved == head and not r.review_carried  # still the old head's approval only
    assert any("not carried" in e and "lib.py" in e for e in r.history)
    move_pull(w, out.sha)
    w.forge.set_checks(out.sha, [("ci", "completed", "success")])
    w.sup._watch(w.rec(1), SimpleNamespace())
    assert w.rec(1).status == IN_REVIEW


# -- a conflicting PR in waiting_ci never sits there (#82) -----------------------------------------

CODE_CONFLICT = ({"app.py": "x = 1\n"}, {"app.py": "x = 2\n"})
BOOKKEEPING = ({"translations.csv": "hello,hola\nbye,adios\n"}, {"translations.csv": "hello,hola\nyes,si\n"})


def issue_comments(w) -> list[str]:  # type: ignore[no-untyped-def]
    return [c[1] for c in w.forge.data()["comments"] if c[0] == 1]


def test_a_conflicting_pr_whose_checks_never_come_is_handed_back_after_conflict_wait(world, repo, clock):
    """INT-19: GitHub runs no PR CI on a conflicting PR, so its required check stays missing: PENDING forever."""
    head, _ = scenario(repo, *CODE_CONFLICT)
    w = world(integrate={"mode": "direct", "required_checks": ["tests"]}, agents={"conflict_wait": "15m"})
    conflicting_pr(w, head)
    w.forge.set_checks(head, [("lint", "completed", "success")])  # a push-triggered check; `tests` never comes
    w.sup.tick()
    rec = w.rec(1)
    assert rec.status == WAITING_CI and rec.conflict_since and rec.ci_rounds == 0, w.messages
    clock.advance(14 * 60)
    w.sup.tick()
    assert w.rec(1).status == WAITING_CI and not issue_comments(w)
    clock.advance(2 * 60)
    w.sup.tick()
    w.wait_agents()
    assert remote_sha(repo, "feature/1-x") == head  # a code conflict: nothing pushed
    assert w.rec(1).ci_rounds == 1
    note = issue_comments(w)[-1]
    assert "has conflicted with `main` for 16m" in note and "Left in conflict:\n- `app.py`" in note


def test_a_conflicting_pr_without_checks_handed_back_before_is_handed_back_again(world, repo, clock):
    """The agent came back without moving the head: once per head used to mean never again."""
    head, _ = scenario(repo, *CODE_CONFLICT)
    w = world(integrate={"mode": "direct"}, agents={"conflict_wait": "10m", "ci_rounds": 2})
    conflicting_pr(w, head)
    w.drive(2)  # no checks at all: handed back at once, as before; the scripted agent pushes nothing
    assert w.rec(1).ci_rounds == 1 and w.rec(1).status == WAITING_CI, w.rec(1).history
    w.drive(1)
    assert w.rec(1).ci_rounds == 1  # waiting again, on a clock restarted when it came back
    clock.advance(11 * 60)
    w.drive(2)
    assert w.rec(1).ci_rounds == 2 and w.rec(1).status == WAITING_CI
    assert "Left in conflict:\n- `app.py`" in issue_comments(w)[-1]
    clock.advance(11 * 60)
    w.drive(1)
    assert w.rec(1).status == "blocked"  # past ci_rounds: a human, not an endless wait


def test_a_red_check_on_a_conflicting_pr_is_handed_back_at_once(world, repo):
    head, _ = scenario(repo, *CODE_CONFLICT)
    w = world(integrate={"mode": "direct", "required_checks": ["tests"]})
    conflicting_pr(w, head)
    w.forge.set_checks(head, [("lint", "completed", "failure", "https://ci/lint")])  # red, but not `tests`: PENDING
    w.sup.tick()
    w.wait_agents()
    assert w.rec(1).ci_rounds == 1
    note = issue_comments(w)[-1]
    assert "CI is red" in note and "lint: failure https://ci/lint" in note and "conflicts with `main`" in note


def test_a_conflicting_pr_with_pending_checks_is_refreshed_after_conflict_wait(world, repo, clock):
    head, _ = scenario(repo, *BOOKKEEPING)
    w = world(
        integrate={"mode": "direct", "mergers": {"translations.csv": "keep_both_lines"}},
        agents={"conflict_wait": "15m"},
    )
    conflicting_pr(w, head)
    w.forge.set_checks(head, [("tests", "in_progress", "")])
    w.sup.tick()
    assert remote_sha(repo, "feature/1-x") == head
    clock.advance(16 * 60)
    w.sup.tick()
    new = remote_sha(repo, "feature/1-x")
    assert new != head, w.messages
    rec = w.rec(1)
    assert rec.status == WAITING_CI and rec.pr_sha == new and rec.ci_rounds == 0
    assert not issue_comments(w)


def test_a_pr_no_longer_conflicting_just_waits_for_its_checks(world, repo, clock):
    head, _ = scenario(repo, *CODE_CONFLICT)
    w = world(integrate={"mode": "direct"}, agents={"conflict_wait": "5m"})
    conflicting_pr(w, head)
    w.forge.set_checks(head, [("tests", "in_progress", "")])
    w.sup.tick()
    assert w.rec(1).conflict_since
    w.forge.add_pull(101, "feature/1-x", head, body="Closes #1", mergeable=None)  # not known: nothing decided
    w.sup.tick()
    assert w.rec(1).conflict_since
    w.forge.add_pull(101, "feature/1-x", head, body="Closes #1", mergeable=True)
    w.sup.tick()
    assert not w.rec(1).conflict_since  # the clock stops
    clock.advance(10 * 60)
    w.sup.tick()
    rec = w.rec(1)
    assert rec.status == WAITING_CI and rec.ci_rounds == 0 and not issue_comments(w)


def test_status_flags_a_head_waiting_in_waiting_ci_too_long(world, repo, clock):
    head, _ = scenario(repo, *CODE_CONFLICT)
    w = world(integrate={"mode": "direct", "required_checks": ["tests"]}, agents={"conflict_wait": "6h"})
    conflicting_pr(w, head)
    w.forge.set_checks(head, [("lint", "completed", "success")])
    w.sup.tick()
    assert "WAITING_CI for longer" not in render_status(w.cfg)
    clock.advance(2 * 3600 + 60)
    out = render_status(w.cfg)
    assert "WAITING_CI for longer than 2h 00m" in out
    assert f"#1: PR #101 at {head[:8]} for 2h 01m, conflicting with the base for 2h 01m" in out
