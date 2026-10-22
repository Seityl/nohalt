"""Frappe / ERPNext apps on a shared bench.

    [stack]
    name  = "frappe"
    bench = "/home/me/frappe-bench"
    app   = "myapp"
    sites = ["agents1.test", "agents2.test", "agents3.test"]

The bench may also serve sites people use, so the bench's checkout, built
assets and running processes are never touched:

* the agent's worktree goes first on ``PYTHONPATH``, so ``import <app>`` is the
  worktree's code;
* each verification takes one of the agent sites (a pool: with one site,
  eight agents queued a minute and a half each), migrated to the branch's
  schema before anything is measured -- a site wears the schema of whichever
  branch migrated it last;
* stale bytecode from another branch is removed first;
* no ``bench build`` and no ``bench restart`` -- frontend work is checked with
  the app's own JS test runner in the worktree, and CI builds the product.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import ConfigError, VerifySlot
from ..util import run
from . import Prepared, Stack

REFUSE = [
    "bench build",
    "bench restart",
    "bench update",
    "--site all",
    "drop-site",
    "reinstall",
    "bench setup",
    "bench migrate-env",
]

MERGERS = {
    "**/patches.txt": "keep_both_lines",
    "**/doctype/*/*.json": "doctype_json",
    "**/fixtures/custom_field.json": "fixture_json",
    "**/fixtures/property_setter.json": "fixture_json",
    "**/package.json": "package_json",
    "package.json": "package_json",
}


class Frappe(Stack):
    name = "frappe"
    prompt_addendum = "builtin:frappe"

    def apply_defaults(self) -> None:
        o = self.cfg.stack.options
        unknown = set(o) - {"bench", "app", "sites", "full_suite_guard", "mergers"}
        if unknown:
            raise ConfigError(f"[stack] unknown frappe option(s): {', '.join(sorted(unknown))}")
        for key in ("bench", "app", "sites"):
            if not o.get(key):
                raise ConfigError(f"[stack] frappe needs `{key}`")
        self.bench = Path(o["bench"]).expanduser()
        self.app = o["app"]
        v = self.cfg.verify
        if [s.name for s in v.slots] == ["default"]:
            v.slots = [VerifySlot(site, {"SITE": site}) for site in o["sites"]]
        if not v.placeholder:
            v.placeholder = o["sites"][0]
        v.refuse = list(dict.fromkeys([*REFUSE, *v.refuse]))
        v.pythonpath_worktree = True
        if o.get("mergers", True):
            for glob, driver in MERGERS.items():
                self.cfg.integrate.mergers.setdefault(glob, driver)

    def check_command(self, command: str) -> str | None:
        reason = super().check_command(command)
        if reason:
            return reason
        if (
            self.cfg.stack.options.get("full_suite_guard", True)
            and "run-tests" in command
            and not any(f in command for f in ("--module", "--doctype", "--test", "--case"))
        ):
            return "the whole app's suite is CI's job; run your own modules here (--module ...)"
        return None

    def prepare(self, worktree: Path, slot: VerifySlot) -> Prepared:
        if not (worktree / self.app).is_dir():
            raise RuntimeError(f"{worktree} is not a worktree of {self.app}")
        site = slot.env.get("SITE", slot.name)
        p = Prepared(cwd=self.bench, env={**slot.env, "SITE": site})
        for cache in (worktree / self.app).rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        env_path = {"PYTHONPATH": str(worktree)}
        cp = run(["bench", "--site", site, "migrate"], cwd=self.bench, timeout=3600, env=_merged(env_path))
        if cp.returncode == 0:
            p.notes.append(f"{site} migrated to this branch's schema")
        else:
            p.notes.append(f"WARNING: migrate failed on {site}: {(cp.stderr or cp.stdout).strip()[-400:]}")
        run(["bench", "--site", site, "clear-cache"], cwd=self.bench, timeout=600, env=_merged(env_path))
        return p


def _merged(extra: dict[str, str]) -> dict[str, str]:
    import os

    env = dict(os.environ)
    for k, v in extra.items():
        env[k] = f"{v}:{env[k]}" if k == "PYTHONPATH" and env.get(k) else v
    return env
