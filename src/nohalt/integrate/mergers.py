"""Mechanical resolution of bookkeeping conflicts.

When the integrator batches many green PRs, most conflicts are not in code
but in files every branch touches: dependency manifests, migration lists,
schema JSON, version files. Those are resolved here, by rule. A conflict in
real code is never resolved mechanically -- that PR is left out and handed
back to its agent.

Every driver runs inside a conflicted merge in the repository root, reads the
three sides from git (base = merge-base, ours = HEAD, theirs = MERGE_HEAD), and
returns True if it resolved the file. ``nohalt merge-driver <name> <path>``
runs one by hand.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

MISSING: Any = object()


def _show(repo: Path, rev: str, path: str) -> str | None:
    cp = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, text=True)
    return cp.stdout if cp.returncode == 0 else None


def _base(repo: Path) -> str:
    cp = subprocess.run(["git", "-C", str(repo), "merge-base", "HEAD", "MERGE_HEAD"], capture_output=True, text=True)
    return cp.stdout.strip()


def _sides(repo: Path, path: str) -> tuple[str | None, str | None, str | None]:
    return _show(repo, _base(repo), path), _show(repo, "HEAD", path), _show(repo, "MERGE_HEAD", path)


def _indent(text: str | None) -> int | str:
    if text and "\n\t" in text:
        return "\t"
    m = re.search(r"\n( +)\S", text or "")
    return len(m.group(1)) if m else 2


def _write_json(repo: Path, path: str, data: Any, like: str | None) -> None:
    (repo / path).write_text(json.dumps(data, indent=_indent(like), ensure_ascii=False) + "\n")


# -- package.json ---------------------------------------------------------------------
def three_way(base: Any, ours: Any, theirs: Any, path: str, clashes: list[str], take_ours: set[str]) -> Any:
    """Each key decided against the merge base.

    Changed on one side only -> that side. Changed the same on both -> either.
    Changed differently on both -> a clash (the caller treats the file as a
    code conflict). Taking "ours" for every key both sides had once dropped an
    incoming dependency bump while the lockfile kept it, and the release build
    went red.
    """
    if isinstance(ours, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in dict.fromkeys([*ours.keys(), *theirs.keys()]):
            v = three_way(b.get(k, MISSING), ours.get(k, MISSING), theirs.get(k, MISSING), f"{path}.{k}", clashes, take_ours)
            if v is not MISSING:
                out[k] = v
        return out
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if path in take_ours:
        return ours
    clashes.append(f"{path}: ours={ours!r} theirs={theirs!r}")
    return ours


def package_json(repo: Path, path: str) -> bool:
    b, o, t = _sides(repo, path)
    if o is None or t is None:
        return False
    clashes: list[str] = []
    merged = three_way(json.loads(b) if b else {}, json.loads(o), json.loads(t), "", clashes, {".version"})
    if clashes:
        return False
    _write_json(repo, path, merged, o)
    return True


# -- line lists (patches.txt, translations, changelog fragments) -------------------------------
def keep_both_lines(repo: Path, path: str) -> bool:
    """Keep both sides' lines, ours first, without duplicates."""
    p = repo / path
    out: list[str] = []
    seen: set[str] = set()
    for line in p.read_text().split("\n"):
        if line.startswith(("<<<<<<<", "=======", ">>>>>>>", "|||||||")):
            continue
        if line and line in seen:
            continue
        seen.add(line)
        out.append(line)
    p.write_text("\n".join(out))
    return True


def keep_both_blocks(repo: Path, path: str) -> bool:
    """Prose: keep both sides' conflicting blocks, ours first."""
    p = repo / path
    s = re.sub(
        r"<<<<<<< [^\n]*\n(.*?)(?:\|\|\|\|\|\|\| [^\n]*\n.*?)?=======\n(.*?)>>>>>>> [^\n]*\n",
        lambda m: m.group(1) + m.group(2),
        p.read_text(),
        flags=re.S,
    )
    p.write_text(s)
    return "<<<<<<<" not in s


def take_ours(repo: Path, path: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--ours", "--", path]).returncode == 0


def take_theirs(repo: Path, path: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--theirs", "--", path]).returncode == 0


# -- records keyed by a field (Frappe fixtures and DocType JSON) ----------------------------------
def _merge_records(base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    both: list[str] = []
    for k in dict.fromkeys([*ours, *theirs, *base]):
        b, o, t = base.get(k), ours.get(k), theirs.get(k)
        if o == t:
            r = o
        elif o == b:
            r = t
        elif t == b:
            r = o
        else:
            r = o if o is not None else t
            both.append(k)
        if r is not None:
            out[k] = r
    return out, both


def fixture_json(repo: Path, path: str) -> bool:
    """A list of records keyed by ``name``; changed on both sides -> ours, reported."""
    b, o, t = _sides(repo, path)
    if o is None or t is None:
        return False

    def keyed(text: str | None) -> dict[str, Any]:
        return {r["name"]: r for r in json.loads(text)} if text else {}

    ko, kt = keyed(o), keyed(t)
    merged, _both = _merge_records(keyed(b), ko, kt)
    order = [n for n in ko if n in merged] + [n for n in kt if n in merged and n not in ko]
    _write_json(repo, path, [merged[n] for n in order], o)
    return True


def doctype_json(repo: Path, path: str) -> bool:
    """A Frappe DocType: fields merged by fieldname, ``field_order`` unioned.

    A Select field both sides extended gets the union of its options. Any
    other field both sides changed differently is a real conflict.
    """
    b, o, t = _sides(repo, path)
    if o is None or t is None:
        return False
    db, do, dt = json.loads(b) if b else {}, json.loads(o), json.loads(t)

    def byname(d: dict[str, Any]) -> dict[str, Any]:
        return {f["fieldname"]: f for f in d.get("fields", [])}

    fb, fo, ft = byname(db), byname(do), byname(dt)
    fields, both = _merge_records(fb, fo, ft)
    for name in both:
        oo, tt = fo.get(name), ft.get(name)
        if (
            oo
            and tt
            and oo.get("fieldtype") == tt.get("fieldtype") == "Select"
            and {k: v for k, v in oo.items() if k != "options"} == {k: v for k, v in tt.items() if k != "options"}
        ):
            ol = (oo.get("options") or "").split("\n")
            fields[name] = {**oo, "options": "\n".join(ol + [x for x in (tt.get("options") or "").split("\n") if x not in ol])}
        else:
            return False
    order = list(do.get("field_order", []))
    tord = dt.get("field_order", [])
    for i, n in enumerate(tord):
        if n in order or n not in fields:
            continue
        prev = next((tord[j] for j in range(i - 1, -1, -1) if tord[j] in order), None)
        order.insert(order.index(prev) + 1 if prev else len(order), n)
    order = [n for n in order if n in fields]
    out = dict(do)
    for k in set(dt) - set(do):
        out[k] = dt[k]
    out["field_order"] = order
    out["fields"] = [fields[n] for n in order] + [fields[n] for n in fields if n not in order]
    out["modified"] = max(do.get("modified", ""), dt.get("modified", ""))
    _write_json(repo, path, out, o)
    return True


def min_counts(repo: Path, path: str) -> bool:
    """An allow-list of counts that only ever shrinks: per key, the lower count wins."""
    _, o, t = _sides(repo, path)
    if o is None or t is None:
        return False

    def merge(a: Any, b: Any) -> Any:
        if isinstance(a, dict) and isinstance(b, dict):
            return {k: merge(a[k], b[k]) for k in a if k in b}
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return min(a, b)
        return a

    _write_json(repo, path, merge(json.loads(o), json.loads(t)), o)
    return True


DRIVERS: dict[str, Callable[[Path, str], bool]] = {
    "package_json": package_json,
    "keep_both_lines": keep_both_lines,
    "keep_both_blocks": keep_both_blocks,
    "take_ours": take_ours,
    "take_theirs": take_theirs,
    "fixture_json": fixture_json,
    "doctype_json": doctype_json,
    "min_counts": min_counts,
    "fail": lambda repo, path: False,
}


def driver_for(mergers: dict[str, str], path: str) -> str | None:
    """The driver for ``path``: the most specific matching glob wins."""
    from fnmatch import fnmatch

    best: tuple[int, str] | None = None
    for glob, name in mergers.items():
        if fnmatch(path, glob) or (glob.startswith("**/") and fnmatch(path, glob[3:])):
            score = len(glob.replace("*", ""))
            if best is None or score > best[0]:
                best = (score, name)
    return best[1] if best else None


def resolve(repo: Path, conflicted: list[str], mergers: dict[str, str]) -> list[str]:
    """Resolve what can be resolved and stage it; return the files left in conflict."""
    left = []
    for path in conflicted:
        name = driver_for(mergers, path)
        ok = False
        if name:
            try:
                ok = DRIVERS[name](repo, path)
            except (ValueError, KeyError, TypeError, OSError):
                ok = False
        if ok and (repo / path).exists():
            ok = "<<<<<<<" not in (repo / path).read_text(errors="replace")
        if ok:
            subprocess.run(["git", "-C", str(repo), "add", "--", path], check=False)
        else:
            left.append(path)
    return left
