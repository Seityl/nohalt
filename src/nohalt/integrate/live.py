"""After the merge: is it working, and is it live? (DEP-14, DEP-15)

A deploy's exit code says the script finished, not that the app works, and a
merged release PR says nothing about what production runs. Two commands
close the gap:

* ``post_deploy_check`` runs after a successful ``deploy`` (or a green CD
  workflow), with ``NOHALT_VERSION``, ``NOHALT_SHA``, ``NOHALT_TAG`` and
  ``NOHALT_ISSUES`` set. ``PipelineGate`` runs it, retries it for warm-up
  without blocking a tick, and treats a failure like a failed deploy.
* ``live_version`` prints what production runs. With it set, a merged issue is
  ``released`` -- nohalt does not close it -- until that version's commit
  contains the issue's merge commit (``git merge-base --is-ancestor``). Then
  it is closed, or commented on if the forge already closed it, "live in vX
  (verified)". Too long without that is a warning, not a close.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

from ..forge import OPEN, UNKNOWN, ForgeError
from ..state import ACTIVE, RELEASED
from ..util import now
from .windows import duration

if TYPE_CHECKING:
    from ..supervisor import Supervisor

CHECK_TIMEOUT = 600  # one run of post_deploy_check
LIVE_TIMEOUT = 120  # one run of live_version
OUTPUT_KEPT = 2000  # characters of a failed check's output kept in DEPLOY_BLOCKED and the issue


def check_env(ctx: dict[str, Any], tag_prefix: str = "") -> dict[str, str]:
    """The environment a post_deploy_check runs with: what was just deployed."""
    sha, tag = ctx.get("sha", ""), ctx.get("tag", "")
    version = tag.removeprefix(tag_prefix) if tag else sha[:12]
    return {
        "NOHALT_VERSION": version,
        "NOHALT_SHA": sha,
        "NOHALT_TAG": tag,
        "NOHALT_ISSUES": " ".join(str(i) for i in ctx.get("issues", [])),
    }


def label(ctx: dict[str, Any]) -> str:
    """How a deployment is named in messages: its tag, else its short SHA, else what merged."""
    return ctx.get("tag") or (ctx.get("sha") or "")[:7] or ctx.get("what", "the deploy")


def run_post_check(sup: Supervisor, ctx: dict[str, Any]) -> tuple[int, str]:
    """Run ``post_deploy_check`` once: (exit code, its output). Output also goes to deploy.log."""
    env = {**os.environ, **check_env(ctx, sup.cfg.integrate.train.tag_prefix)}
    try:
        cp = subprocess.run(
            ["bash", "-c", sup.cfg.integrate.post_deploy_check],
            cwd=sup.cfg.repo_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
        )
        rc, out = cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except subprocess.TimeoutExpired as e:
        partial = e.stdout if isinstance(e.stdout, str) else ""
        rc, out = 124, f"{partial}\n(post_deploy_check timed out after {CHECK_TIMEOUT} s)"
    except OSError as e:
        rc, out = 127, f"post_deploy_check could not run: {e}"
    with open(sup.state.logs_dir / "deploy.log", "a") as f:
        f.write(f"\n--- post-deploy check of {label(ctx)} (exit {rc}) ---\n{out}")
    return rc, out


def summary(out: str) -> str:
    """The last line of a check's output that says something: the issue title's tail."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1][:120] if lines else "no output"


class LiveWatch:
    """Released issues wait here until production runs them. State: ``live.json``."""

    def __init__(self, sup: Supervisor):
        self.sup = sup
        self.cfg = sup.cfg
        self.clock = now  # tests move time by replacing it

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.integrate.live_version)

    def _st(self) -> dict[str, Any]:
        return self.sup.state.load("live.json", {"pending": []})

    def _save(self, st: dict[str, Any]) -> None:
        self.sup.state.save("live.json", st)

    def timeout(self) -> int:
        return self.cfg.integrate.live_timeout or self.cfg.integrate.cd_timeout

    def released(self, issues: list[int], sha: str, tag: str, what: str) -> None:
        """A merge carried these issues: they are released, not yet live. Nothing is closed here."""
        for n in issues:
            rec = self.sup.state.get(n)
            if rec.status in ACTIVE:
                rec.status = RELEASED
                rec.event(f"released in {tag or what} ({sha[:12]}), not yet live")
                self.sup.state.put(rec)
            try:
                self.sup.forge.comment(
                    n,
                    f"**nohalt:** released in {tag or what} (merged as `{sha[:12]}`), not yet live. This issue is closed "
                    "once `live_version` reports a version that contains it.",
                )
            except ForgeError as e:
                self.sup.state.log(f"#{n}: could not comment that it is released: {e}")
        st = self._st()
        st["pending"].append({"issues": list(issues), "sha": sha, "tag": tag, "what": what, "since": self.clock()})
        self._save(st)
        listed = " ".join(f"#{n}" for n in issues)
        self.sup.say(f"{listed}: released in {tag or what}, not yet live -- closed once live_version shows it")

    def tick(self) -> None:
        st = self._st()
        if not st["pending"]:
            return
        value, commit = self._live(st)
        left = []
        for entry in st["pending"]:
            if commit and self._contains(commit, entry["sha"]):
                entry["issues"] = self._verified(entry, value)
                if not entry["issues"]:
                    continue  # every issue closed or told; the rest are tried again next tick
            age = self.clock() - entry["since"]
            if age >= self.timeout() and not entry.get("warned"):
                entry["warned"] = True
                listed = " ".join(f"#{n}" for n in entry["issues"])
                self.sup.say(
                    f"WARNING: {entry['tag'] or entry['what']} ({listed}) merged {duration(age)} ago and is not live: "
                    f"live_version reports {value or 'nothing usable'}. The issues stay open (released, not yet live); "
                    "check the deploy, or close them by hand if the release is live another way"
                )
            left.append(entry)
        st["pending"] = left
        self._save(st)

    # -- what production runs ---------------------------------------------------------------------
    def _live(self, st: dict[str, Any]) -> tuple[str, str]:
        """(what live_version printed, the commit it names) -- either may be empty."""
        try:
            cp = subprocess.run(
                ["bash", "-c", self.cfg.integrate.live_version],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                timeout=LIVE_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            self.sup.state.log(f"live_version did not answer: {e}")
            return "", ""
        lines = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
        if cp.returncode != 0 or not lines:
            self.sup.state.log(f"live_version exited {cp.returncode} with {'output' if lines else 'no output'}")
            return "", ""
        value = lines[-1]
        seen = st.get("live") or {}
        if seen.get("value") == value and seen.get("commit"):
            return value, seen["commit"]
        commit = self._resolve(value)
        if not commit:
            self._fetch()
            commit = self._resolve(value)
        if not commit and seen.get("value") != value:
            self.sup.say(f"live_version reports {value!r}, which names no commit or tag in the clone (yet)")
        st["live"] = {"value": value, "commit": commit, "at": self.clock()}
        return value, commit

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.cfg.repo_path), *args], capture_output=True, text=True)

    def _fetch(self) -> None:
        self._git("fetch", "-q", "--tags", self.cfg.project.remote)

    def _resolve(self, value: str) -> str:
        prefix = self.cfg.integrate.train.tag_prefix
        for ref in dict.fromkeys([value, f"{prefix}{value}", f"refs/tags/{value}"]):
            cp = self._git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
            if cp.returncode == 0 and cp.stdout.strip():
                return cp.stdout.strip()
        return ""

    def _contains(self, live: str, sha: str) -> bool:
        """Does the live commit contain the merge commit? Unknown commits are fetched once, then 'no'."""
        for attempt in range(2):
            cp = self._git("merge-base", "--is-ancestor", sha, live)
            if cp.returncode in (0, 1):
                return cp.returncode == 0
            if attempt == 0:
                self._fetch()
        return False

    def _verified(self, entry: dict[str, Any], value: str) -> list[int]:
        """Close (or comment on) each issue as live. Returns those the forge could not be told about yet."""
        what = entry["tag"] or entry["what"]
        left, done = [], []
        for n in entry["issues"]:
            issue = self.sup.issue(n, fresh=True)
            if issue.state == UNKNOWN:
                left.append(n)
                continue
            text = (
                f"**nohalt:** live in {value} (verified): production runs a version that contains {what} (`{entry['sha'][:12]}`)."
            )
            try:
                if issue.state == OPEN:  # closed first: a retry after a failed comment finds it closed, not twice told
                    self.sup.forge.close_issue(n)
                    self.sup._issues.pop(n, None)  # the launch pass must see it closed and finish the record
                self.sup.forge.comment(n, text)
            except ForgeError as e:
                self.sup.say(f"#{n}: live in {value}, but the forge refused the close or comment: {e}")
                left.append(n)
                continue
            done.append(n)
            rec = self.sup.state.get(n)
            if rec.status == RELEASED:
                rec.event(f"live in {value} (verified)")
                self.sup.state.put(rec)
        if done:
            listed = " ".join(f"#{n}" for n in done)
            self.sup.say(f"{listed}: live in {value} (verified) -- {what} is in production")
        return left


def awaiting(st: dict[str, Any], at: float, timeout: int) -> list[str]:
    """Status lines for released issues not yet live (``nohalt status``)."""
    lines = []
    seen = (st.get("live") or {}).get("value")
    for e in st.get("pending", []):
        age = at - e.get("since", at)
        late = f", OVERDUE (live_timeout {duration(timeout)})" if age >= timeout else ""
        listed = " ".join(f"#{n}" for n in e.get("issues", []))
        lines.append(f"released, awaiting live: {listed} in {e.get('tag') or e.get('what')} (for {duration(age)}{late})")
    if lines and seen:
        lines.append(f"  live_version last reported: {seen}")
    return lines
