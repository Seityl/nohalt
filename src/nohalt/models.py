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

Overflow (``[models] overflow_after``): an unpinned issue that has waited that
long for a slot -- its model's pool is full, or its model does not answer and
no fallback has a free slot -- may start on the first ``overflow_to`` model
with a free slot that answers. It is not a pin: the next launch is routed as
usual again.

Capacity (``capacity.py``): a model that is **down** (an endpoint storm, or
probes failing in a row) or whose pool is **cooling down** after a usage-limit
signal does not answer, so it is not chosen; an unpinned issue routed to it is
diverted at once to a fallback or an overflow target (``divert``), with the same
premium rule overflow keeps. A pinned issue waits -- unless ``[models]
pinned_during_cooldown = "warning"`` and only a usage warning cools the pool
down (``pin_through_warning``): a pin is the owner's choice, so it starts there.
A hard limit still makes it wait.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import agents
from .capacity import Capacity
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

    def pin_through_warning(self, issue: int) -> bool:
        """The issue is pinned and ``[models] pinned_during_cooldown = "warning"``: a warning cool-down is no reason to wait (#81)."""
        return self.cfg.models.pinned_during_cooldown == "warning" and bool(self.pinned(issue))

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

    def choose(
        self,
        issue: Issue,
        has_slot: Callable[[str], bool] | None = None,
        want: tuple[str, bool] | None = None,
        warned_ok: bool = False,
    ) -> str | None:
        """The model to launch with now, or None if nothing suitable answers.

        ``has_slot`` says whether a model's pool has a free slot. A full pool
        for the wanted model means wait (overflow decides when waiting is
        enough); a fallback is taken only if its own pool has room. ``want``
        overrides routing: ``(model, True)`` holds an issue to one model.
        ``warned_ok``: a pinned model whose pool only a usage warning cools down
        may still be chosen (``pin_through_warning``).
        """
        free = has_slot or (lambda _m: True)
        model, pinned = want or self.wanted(issue)
        if not free(model):
            return None
        if self.prober.answers(model, True) if warned_ok and pinned else self.prober.answers(model):
            return model
        if pinned:
            return None
        for alt in self.cfg.models.fallbacks:
            if alt != model and free(alt) and self.prober.answers(alt):
                return alt
        return None

    def overflow_targets(self, issue: int) -> list[str]:
        """Models an issue may overflow to, in order. Premium only if approved."""
        m = self.cfg.models
        targets = m.overflow_to or [f for f in m.fallbacks if f not in m.premium]
        approved = self.approved(issue)
        return [t for t in targets if approved or t not in m.premium]

    def divert(self, issue: Issue, has_slot: Callable[[str], bool], want: tuple[str, bool] | None = None) -> str | None:
        """The routed model is down or cooling down: the first fallback or overflow target that can start now.

        Like overflow, but without waiting ``overflow_after``: the routed model
        cannot take the work at all. Premium only if approved; never a pinned issue.
        """
        model, pinned = want or self.wanted(issue)
        if pinned:
            return None
        m = self.cfg.models
        approved = self.approved(issue.number)
        seen = {model}
        for alt in [*m.fallbacks, *self.overflow_targets(issue.number)]:
            if alt in seen or (alt in m.premium and not approved):
                continue
            seen.add(alt)
            if has_slot(alt) and self.prober.answers(alt):
                return alt
        return None

    def overflow(self, issue: Issue, has_slot: Callable[[str], bool]) -> str | None:
        """The first overflow model with a free slot that answers. Pinned issues never overflow."""
        model, pinned = self.wanted(issue)
        if pinned:
            return None
        for alt in self.overflow_targets(issue.number):
            if alt != model and has_slot(alt) and self.prober.answers(alt):
                return alt
        return None


class Prober:
    """Asks a model for "OK", caching the answer for ``probe_ttl`` seconds."""

    def __init__(self, cfg: Config, state: StateStore):
        self.cfg = cfg
        self.state = state
        self.capacity = Capacity(cfg, state)

    def answers(self, model: str, warned_ok: bool = False) -> bool:
        if self.capacity.unavailable(model, warned_ok):
            # Down (probed again on its own schedule) or cooling down: probing a plan at its
            # limit can itself be billed (MOD-08).
            return False
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
        self.capacity.probe_result(model, ok)
        return ok

    def reprobe_down(self) -> None:
        """End expired cool-downs, and probe each down model whose turn has come (up again if it answers)."""

        def probe(model: str) -> bool:
            ok = self._probe(model)
            cache = self.state.load("probes.json", {})
            cache[model] = {"at": now(), "ok": ok}
            self.state.save("probes.json", cache)
            return ok

        self.capacity.tick(probe if self.cfg.models.probe else None)

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
        out = cp.stdout + cp.stderr
        if agent.setup_failed(out) or agent.provider_failed(out):
            return False
        return cp.returncode == 0 and ("OK" in cp.stdout or name == "command")
