"""Bookkeeping merge drivers (INT-*), each run inside a real conflicted git merge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import git
from nohalt.integrate.mergers import DRIVERS, driver_for, resolve


def conflict(repo: Path, path: str, base: str, ours: str, theirs: str) -> bool:
    """Create base/ours/theirs commits of ``path`` and start a conflicted merge. True if it conflicted."""
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(base)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "theirs")
    p.write_text(theirs)
    git(repo, "commit", "-q", "-am", "theirs")
    git(repo, "checkout", "-q", "main")
    p.write_text(ours)
    git(repo, "commit", "-q", "-am", "ours")
    cp = subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-edit", "theirs"], capture_output=True, text=True)
    return cp.returncode != 0


def j(d: object) -> str:
    return json.dumps(d, indent=2) + "\n"


def test_package_json_keeps_a_bump_from_either_side(repo):
    """A dependency bump on the incoming side was once silently dropped while the lockfile kept it."""
    base = {"name": "x", "version": "1.0.0", "dependencies": {"a": "1", "b": "1"}, "scripts": {"t": "x"}}
    ours = {**base, "version": "1.0.1", "dependencies": {"a": "2", "b": "1"}, "scripts": {"t": "x", "o": "y"}}
    theirs = {**base, "version": "1.0.2", "dependencies": {"a": "1", "b": "3", "c": "1"}, "scripts": {"t": "x", "th": "z"}}
    assert conflict(repo, "package.json", j(base), j(ours), j(theirs))
    assert DRIVERS["package_json"](repo, "package.json")
    out = json.loads((repo / "package.json").read_text())
    assert out["dependencies"] == {"a": "2", "b": "3", "c": "1"}
    assert out["scripts"] == {"t": "x", "o": "y", "th": "z"}
    assert out["version"] == "1.0.1"  # ours; the integrator stamps the release version afterwards


def test_package_json_real_clash_is_a_code_conflict(repo):
    base = {"dependencies": {"a": "1"}}
    assert conflict(repo, "package.json", j(base), j({"dependencies": {"a": "2"}}), j({"dependencies": {"a": "3"}}))
    assert not DRIVERS["package_json"](repo, "package.json")


def test_keep_both_lines(repo):
    assert conflict(repo, "app/patches.txt", "a\n", "a\nours\n", "a\ntheirs\n")
    assert DRIVERS["keep_both_lines"](repo, "app/patches.txt")
    assert (repo / "app/patches.txt").read_text().split() == ["a", "ours", "theirs"]


def test_doctype_fields_by_fieldname_and_select_options_union(repo):
    def dt(fields, order):
        return j({"name": "X", "fields": fields, "field_order": order, "modified": "2026-01-01"})

    f_a = {"fieldname": "a", "fieldtype": "Data"}
    sel = {"fieldname": "s", "fieldtype": "Select", "options": "One\nTwo"}
    base = dt([f_a, sel], ["a", "s"])
    ours = dt([f_a, {**sel, "options": "One\nTwo\nThree"}, {"fieldname": "o", "fieldtype": "Int"}], ["a", "s", "o"])
    theirs = dt([f_a, {**sel, "options": "One\nTwo\nFour"}, {"fieldname": "t", "fieldtype": "Check"}], ["a", "t", "s"])
    assert conflict(repo, "app/doctype/x/x.json", base, ours, theirs)
    assert DRIVERS["doctype_json"](repo, "app/doctype/x/x.json")
    out = json.loads((repo / "app/doctype/x/x.json").read_text())
    assert out["field_order"] == ["a", "t", "s", "o"]
    assert {f["fieldname"] for f in out["fields"]} == {"a", "s", "o", "t"}
    assert next(f for f in out["fields"] if f["fieldname"] == "s")["options"].split("\n") == ["One", "Two", "Three", "Four"]


def test_doctype_same_field_changed_differently_is_a_real_conflict(repo):
    base = j({"fields": [{"fieldname": "a", "fieldtype": "Data", "label": "A"}], "field_order": ["a"]})
    ours = j({"fields": [{"fieldname": "a", "fieldtype": "Data", "label": "Ours"}], "field_order": ["a"]})
    theirs = j({"fields": [{"fieldname": "a", "fieldtype": "Data", "label": "Theirs"}], "field_order": ["a"]})
    assert conflict(repo, "app/doctype/x/x.json", base, ours, theirs)
    assert not DRIVERS["doctype_json"](repo, "app/doctype/x/x.json")


def test_fixture_records_by_name(repo):
    base = json.dumps([{"name": "A-x", "v": 1}])
    ours = json.dumps([{"name": "A-x", "v": 1}, {"name": "A-o", "v": 1}])
    theirs = json.dumps([{"name": "A-x", "v": 2}, {"name": "A-t", "v": 1}])
    assert conflict(repo, "app/fixtures/custom_field.json", base, ours, theirs)
    assert DRIVERS["fixture_json"](repo, "app/fixtures/custom_field.json")
    out = json.loads((repo / "app/fixtures/custom_field.json").read_text())
    assert out == [{"name": "A-x", "v": 2}, {"name": "A-o", "v": 1}, {"name": "A-t", "v": 1}]


def test_min_counts_only_shrinks(repo):
    base = j({"rule": {"a.js": 5, "b.js": 3}})
    assert conflict(repo, "allow.json", base, j({"rule": {"a.js": 4, "b.js": 3}}), j({"rule": {"a.js": 5, "b.js": 1}}))
    assert DRIVERS["min_counts"](repo, "allow.json")
    assert json.loads((repo / "allow.json").read_text()) == {"rule": {"a.js": 4, "b.js": 1}}


def test_driver_for_most_specific_glob():
    m = {"**/*.json": "fail", "**/doctype/*/*.json": "doctype_json", "package.json": "package_json"}
    assert driver_for(m, "app/doctype/x/x.json") == "doctype_json"
    assert driver_for(m, "package.json") == "package_json"
    assert driver_for(m, "a/b.json") == "fail"
    assert driver_for(m, "src/main.py") is None


def test_resolve_leaves_code_conflicts(repo):
    assert conflict(repo, "src/app.py", "x = 1\n", "x = 2\n", "x = 3\n")
    left = resolve(repo, ["src/app.py"], {"**/patches.txt": "keep_both_lines"})
    assert left == ["src/app.py"]
