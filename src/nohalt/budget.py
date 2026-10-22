"""A spending guard for agents on a pay-per-token API.

Spend is estimated from each agent's own session data, priced with
``[budget] rates`` (USD per million tokens). Calibrate the rates against your
provider's console: an estimate that runs high or low by a factor is normal
until it is measured.

Per issue the high-water mark is kept, so pruning an issue's session data
never makes the total shrink and lets the cap be overrun. An issue over
``per_issue``, or every API issue once ``total`` is reached, is handed to
``fallback_model``: the agent is stopped and relaunched there, in the same
worktree, continuing its session.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import Config
from .state import StateStore


def opencode_spend(data_dir: Path, providers: list[str], rates: dict[str, float]) -> float:
    db = data_dir / "opencode" / "opencode.db"
    if not db.exists():
        return 0.0
    total = 0.0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            for (raw,) in con.execute("select data from message"):
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if m.get("role") != "assistant" or not m.get("tokens"):
                    continue
                if not any(p in (m.get("providerID") or "") for p in providers):
                    continue
                t = m["tokens"]
                cache = t.get("cache") or {}
                total += (
                    t.get("input", 0) * rates.get("input", 0)
                    + t.get("output", 0) * rates.get("output", 0)
                    + cache.get("read", 0) * rates.get("cache_read", 0)
                    + cache.get("write", 0) * rates.get("cache_write", 0)
                ) / 1e6
        finally:
            con.close()
    except sqlite3.Error:
        return 0.0
    return total


def assess(cfg: Config, state: StateStore, on_api: list[int]) -> tuple[dict[str, float], float, list[tuple[int, str]]]:
    """(spend per issue, total, [(issue, reason) to hand over])."""
    b = cfg.budget
    prev = state.load("spend.json", {}).get("per_issue", {})
    spent: dict[str, float] = dict(prev)
    for issue in on_api:
        now_ = opencode_spend(state.agent_data / str(issue), b.providers, b.rates)
        spent[str(issue)] = max(now_, float(prev.get(str(issue), 0)))
    total = round(sum(spent.values()), 4)
    exhausted = total >= b.total
    if exhausted:
        state.set_flag("BUDGET_EXHAUSTED", f"{total:.2f}\n")
    state.save("spend.json", {"total": total, "cap": b.total, "per_issue": {k: round(v, 4) for k, v in spent.items()}})
    hand: list[tuple[int, str]] = []
    for issue in on_api:
        v = spent.get(str(issue), 0.0)
        if v >= b.per_issue:
            hand.append((issue, f"${v:.2f} spent, per-issue cap ${b.per_issue:.2f}"))
        elif exhausted:
            hand.append((issue, f"total ${total:.2f} reached the ${b.total:.2f} cap"))
    return spent, total, hand


def is_paid(cfg: Config, model: str) -> bool:
    _, _, mid = model.partition(":")
    return any(mid.startswith(p + "/") or mid.startswith(p) for p in cfg.budget.providers)
