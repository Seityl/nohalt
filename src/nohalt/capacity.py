"""Whether a model can take new work: endpoint-down state and usage cool-downs.

Two kinds of trouble are about a model, not about any one issue:

* **Down.** Agents on ``[models] down_after`` different issues hitting endpoint
  errors on one model within ``down_window`` (a storm: every agent on a
  restarted server dies at once), or
  that many failed probes in a row, mark the model down. While it is down, new
  launches go elsewhere or wait, and endpoint failures on it are not counted
  against any issue (the storm's own errors are refunded). It is probed again at
  a growing interval (``down_probe`` doubling up to ``down_probe_max``) and
  marked up when it answers.
* **Cooling down.** An agent's usage-limit or usage-warning line (claude-code's
  ``rate_limit five_hour rejected`` and the like) cools down the model's pool --
  the ``[agents] pools`` prefix it belongs to, or the model itself -- until the
  reset time the line reports, else for ``usage_cooldown``. New launches avoid a
  cooling pool. Running agents are not touched. A limit is remembered apart
  (``hard_until``): with ``[review] during_cooldown = "warning"`` a reviewer may
  still start on a pool that only a warning cools down, never on one at its limit.

State is ``<state>/capacity.json``, read by ``nohalt status``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .agents import UsageSignal
from .config import Config
from .state import StateStore
from .util import now, stamp

FILE = "capacity.json"


def ago(seconds: float) -> str:
    from .integrate.windows import duration

    return duration(seconds)


class Capacity:
    def __init__(self, cfg: Config, state: StateStore, say: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.state = state
        self.say: Callable[[str], None] = say or state.log  # the supervisor's own (log and console)

    # -- storage ---------------------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        d = self.state.load(FILE, {}) or {}
        for k in ("down", "errors", "probe_fails", "cooling"):
            d.setdefault(k, {})
        return d

    def _save(self, d: dict[str, Any]) -> None:
        self.state.save(FILE, d)

    def pool_key(self, model: str) -> str:
        """What a cool-down covers: the model's ``[agents] pools`` prefix, else the model."""
        for prefix in sorted(self.cfg.agents.pools, key=len, reverse=True):
            if model.startswith(prefix):
                return prefix
        return model

    # -- queries -----------------------------------------------------------------------------------
    def down_since(self, model: str) -> float:
        return float((self._load()["down"].get(model) or {}).get("since", 0))

    def cooling_until(self, model: str) -> float:
        c = self._load()["cooling"].get(self.pool_key(model))
        return float(c["until"]) if c and c["until"] > now() else 0.0

    def unavailable(self, model: str, warned_ok: bool = False) -> str:
        """Why no new agent should start on ``model`` now, or "" if nothing here says so.

        ``warned_ok``: a pool cooling down only for a warning is no reason (a reviewer, ``[review] during_cooldown``).
        """
        d = self._load()
        down = d["down"].get(model)
        if down:
            return f"{model} down since {stamp(down['since'])}"
        key = self.pool_key(model)
        c = d["cooling"].get(key)
        if c and c["until"] > now() and not (warned_ok and hard_until(c) <= now()):
            window = f" ({c['window']})" if c.get("window") else ""
            return f"{key} cooling down{window} until {stamp(c['until'])}"
        return ""

    def all_down_for(self, models: list[str], seconds: float) -> bool:
        """Every one of ``models`` has been down for at least ``seconds``."""
        down = self._load()["down"]
        t = now()
        return bool(models) and all(m in down and t - down[m]["since"] >= seconds for m in models)

    # -- endpoint errors and probes --------------------------------------------------------------------
    def endpoint_error(self, model: str, issue: int) -> list[int] | None:
        """An agent on ``model`` died on an endpoint error.

        None: the model is up, count the failure as usual. A list: the model is
        down (already, or this error made it so) and this failure is not counted;
        the list holds the issues whose earlier errors in the storm were counted
        and should be refunded (one entry per counted error).
        """
        m = self.cfg.models
        d = self._load()
        if model in d["down"]:
            return []
        if m.down_after <= 0:
            return None
        t = now()
        recent = [e for e in d["errors"].get(model, []) if t - e[0] <= m.down_window]
        agents = {int(e[1]) for e in recent} | {issue}
        if len(agents) < m.down_after:
            recent.append([t, issue])
            d["errors"][model] = recent
            self._save(d)
            return None
        d["errors"].pop(model, None)
        self._mark_down(d, model, f"agents on {len(agents)} issues hit endpoint errors within {ago(m.down_window)}")
        self._save(d)
        return [int(e[1]) for e in recent]

    def probe_result(self, model: str, ok: bool) -> None:
        """A fresh probe answered or not. ``down_after`` failures in a row mark the model down."""
        d = self._load()
        if ok:
            changed = d["probe_fails"].pop(model, None) is not None
            if model in d["down"]:
                self._mark_up(d, model, "it answered a probe")
                changed = True
            if changed:
                self._save(d)
            return
        if model in d["down"]:
            return
        n = int(d["probe_fails"].get(model, 0)) + 1
        d["probe_fails"][model] = n
        if self.cfg.models.down_after > 0 and n >= self.cfg.models.down_after:
            d["probe_fails"].pop(model, None)
            self._mark_down(d, model, f"{n} probes in a row failed")
        self._save(d)

    def _mark_down(self, d: dict[str, Any], model: str, why: str) -> None:
        t = now()
        d["down"][model] = {"since": t, "why": why, "probes": 0, "next_probe": t + self.cfg.models.down_probe}
        self.say(f"model {model} is DOWN: {why}; new work goes to fallbacks or waits, its failures are not counted")

    def _mark_up(self, d: dict[str, Any], model: str, how: str) -> None:
        since = d["down"].pop(model)["since"]
        self.say(f"model {model} is UP again ({how}) after {ago(now() - since)} down")

    def due_probes(self) -> list[str]:
        t = now()
        return [m for m, v in self._load()["down"].items() if v.get("next_probe", 0) <= t]

    def reprobe(self, model: str, ok: bool) -> None:
        """The scheduled probe of a down model: up if it answered, else probe later (doubling)."""
        d = self._load()
        v = d["down"].get(model)
        if v is None:
            return
        if ok:
            self._mark_up(d, model, "it answered a probe")
        else:
            v["probes"] = int(v.get("probes", 0)) + 1
            m = self.cfg.models
            v["next_probe"] = now() + min(m.down_probe * 2 ** v["probes"], max(m.down_probe, m.down_probe_max))
        self._save(d)

    # -- usage ---------------------------------------------------------------------------------------------
    def usage(self, model: str, sig: UsageSignal, source: str, launch: float = 0) -> bool:
        """An agent on ``model`` reported a usage limit or warning. True if this started, moved or hardened a cool-down.

        The same line from the same source is read only once (a running agent's
        log is read every tick).
        """
        key = self.pool_key(model)
        d = self._load()
        c = d["cooling"].get(key) or {}
        seen = f"{source}@{int(launch)}|{sig.line.strip()}"
        if seen in c.get("seen", []):
            return False
        t = now()
        until = sig.resets_at if sig.resets_at > t else t + self.cfg.models.usage_cooldown
        active = c.get("until", 0) > t
        c["seen"] = [*c.get("seen", []), seen][-20:]
        moved = not active or until > c["until"]
        # A limit is hard while its own reset is ahead, whatever a warning later says (#76).
        hard = hard_until(c) if active else 0.0
        hardened = sig.kind == "limit" and until > hard
        if hardened:
            c["hard_until"] = until
        elif not active:
            c.pop("hard_until", None)
        if moved:
            c.update(
                until=until,
                since=c["since"] if active else t,
                window=sig.window or c.get("window", ""),
                kind=sig.kind,
                source=source,
                reported=sig.resets_at > t,
            )
        d["cooling"][key] = c
        self._save(d)
        if hardened and not moved:
            self.say(f"pool {key}: usage limit reached ({source}); hard until {stamp(until)}, reviews divert too")
        if moved:
            what = "usage limit reached" if sig.kind == "limit" else "close to its usage limit"
            reset = "reported reset" if sig.resets_at > t else f"no reset reported, {ago(until - t)} by default"
            window = f" ({sig.window})" if sig.window else ""
            self.say(
                f"pool {key} cooling down until {stamp(until)}{window}: {what} ({source}; {reset}); "
                "new work goes to fallbacks, pinned issues wait"
            )
        return moved or hardened

    # -- each tick ---------------------------------------------------------------------------------------------
    def tick(self, probe: Callable[[str], bool] | None) -> None:
        """End expired cool-downs; probe down models that are due (or, with probing off, let them try again)."""
        d = self._load()
        t = now()
        over = [k for k, c in d["cooling"].items() if c.get("until", 0) <= t]
        for k in over:
            c = d["cooling"].pop(k)
            self.say(f"pool {k}: cool-down over (since {stamp(c.get('since'))}); new work may go there again")
        if over:
            self._save(d)
        for model in self.due_probes():
            if probe is None:
                d = self._load()
                if model in d["down"]:
                    self._mark_up(d, model, "probing is off, so it gets a launch to prove it")
                    self._save(d)
                continue
            self.reprobe(model, probe(model))

    def snapshot(self) -> dict[str, Any]:
        return self._load()


def hard_until(c: dict[str, Any]) -> float:
    """Until when a cool-down is for a hard limit, not only a warning (a record from before #76: its kind says)."""
    return float(c.get("hard_until", c.get("until", 0) if c.get("kind") == "limit" else 0))
