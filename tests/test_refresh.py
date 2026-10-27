"""A conflicting PR whose conflicts are all bookkeeping is refreshed by nohalt, not handed back (INT-12).

Every test runs real merges in real repositories: a bare origin, a PR branch
and a base that moved under it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import git
from nohalt.integrate.mergers import effective
from nohalt.integrate.refresh import CONFLICT, DEFERRED, before_hand_back, record_conflicts, recurring_conflicts, refresh
from nohalt.state import READY, RUNNING, WAITING_CI

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
