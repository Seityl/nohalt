"""Which model an issue's agent runs on, and whether that model is answering.

Order of precedence:
  1. a pin set with ``nohalt route <issue> <model>`` -- never failed over:
     an issue pinned to a model waits for it rather than leaving it;
  2. the first ``[[models.route]]`` rule whose labels or title match;
  3. ``[models] default``.
A model listed in ``premium`` runs only on issues approved for it; anything
else asking for it gets ``premium_fallback``. An unpinned model that does not
answer a probe falls over to the first fallback that does, instead of waiting
on a dead server -- and no agent is started on a model that is not answering,
because a dead endpoint kills each agent in its first second and a loop would
burn through the whole queue in minutes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import agents
from .config import Config
from .forge import Issue
from .state import StateStore
from .util import now


class Router:
    def __init__(self, cfg: Config, state: StateStore, prober: Prober | None = None):
        self.cfg = cfg
        self.state = state
        self.prober = prober or Prober(cfg, state)

    def pinned(self, issue: int) -> str:
        pins = self.state.load("pins.json", {})
        return pins.get(str(issue), "")

    def pin(self, issue: int, model: str, approve: bool = False) -> None:
        agents.split_model(model)
        pins = self.state.load("pins.json", {})
        pins[str(issue)] = model
        self.state.save("pins.json", pins)
        if approve:
            approved = set(self.state.load("approved.json", []))
            approved.add(issue)
            self.state.save("approved.json", sorted(approved))

    def unpin(self, issue: int) -> None:
        pins = self.state.load("pins.json", {})
        pins.pop(str(issue), None)
        self.state.save("pins.json", pins)

    def approved(self, issue: int) -> bool:
        return issue in set(self.state.load("approved.json", []))

    def wanted(self, issue: Issue) -> tuple[str, bool]:
        """(model, pinned) before any probing."""
        pin = self.pinned(issue.number)
        if pin:
            return self._gate(issue.number, pin), True
        for rule in self.cfg.models.route:
            if rule.labels and issue.has_label(*rule.labels):
                return self._gate(issue.number, rule.model), False
            if rule.title and re.search(rule.title, issue.title or "", re.I):
                return self._gate(issue.number, rule.model), False
        return self._gate(issue.number, self.cfg.models.default), False

    def _gate(self, issue: int, model: str) -> str:
        if model in self.cfg.models.premium and not self.approved(issue):
            return self.cfg.models.premium_fallback or self.cfg.models.default
        return model

    def choose(self, issue: Issue) -> str | None:
        """The model to launch with now, or None if nothing suitable answers."""
        model, pinned = self.wanted(issue)
        if self.prober.answers(model):
            return model
        if pinned:
            return None
        for alt in self.cfg.models.fallbacks:
            if alt != model and self.prober.answers(alt):
                return alt
        return None


class Prober:
    """Asks a model for "OK", caching the answer for ``probe_ttl`` seconds."""

    def __init__(self, cfg: Config, state: StateStore):
        self.cfg = cfg
        self.state = state

    def answers(self, model: str) -> bool:
        if not self.cfg.models.probe:
            return True
        cache = self.state.load("probes.json", {})
        hit = cache.get(model)
        if hit and now() - hit["at"] < self.cfg.models.probe_ttl:
            return bool(hit["ok"])
        if self.recently_limited(model):
            return False
        ok = self._probe(model)
        cache[model] = {"at": now(), "ok": ok}
        self.state.save("probes.json", cache)
        return ok

    def mark_limited(self, model: str) -> None:
        """A usage limit was hit: say "not yet" without asking for a while.

        Probing a plan that is at its limit can itself be billed, or show up
        to the owner as a failed session; one reading every 20 minutes is enough.
        """
        lim = self.state.load("limited.json", {})
        lim[model] = now()
        self.state.save("limited.json", lim)
        cache = self.state.load("probes.json", {})
        cache[model] = {"at": now(), "ok": False}
        self.state.save("probes.json", cache)

    def recently_limited(self, model: str) -> bool:
        at = self.state.load("limited.json", {}).get(model)
        return bool(at and now() - at < 1200)

    def _probe(self, model: str) -> bool:
        name, mid = agents.split_model(model)
        agent = agents.get(name, self.cfg.agents.options.get(name))
        data_dir = self.state.agent_data / "_probe"
        data_dir.mkdir(parents=True, exist_ok=True)
        launch = agent.probe_launch(mid, data_dir)
        if not shutil.which(launch.argv[0]) and not Path(launch.argv[0]).exists():
            return False
        env = {**os.environ, **launch.env}
        for k in launch.unset_env:
            env.pop(k, None)
        try:
            cp = subprocess.run(launch.argv, cwd="/tmp", env=env, capture_output=True, text=True, timeout=150)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return cp.returncode == 0 and ("OK" in cp.stdout or name == "command")
