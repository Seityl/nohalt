"""Integrators: the only thing that merges.

* ``none``   -- nohalt never merges; a human does. Issues close when they do.
* ``direct`` -- green PRs are merged one at a time; after each, the pipeline
  on the base branch is watched, and a red pipeline stops all merging and
  opens a fix-forward issue at the front of the queue.
* ``train``  -- green PRs are batched into a ``release/*`` branch with
  bookkeeping conflicts resolved mechanically, versioned, and merged once
  CI is green on the batch.

Agents never merge, in any mode: parallel merges from several sessions each
against a different picture of the base branch produced colliding migrations
and a deploy race. One merger, a known base.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..supervisor import Supervisor


class Integrator(Protocol):
    def tick(self) -> None: ...


class Nothing:
    def __init__(self, sup: Supervisor):
        self.sup = sup

    def tick(self) -> None:
        return None


def make(sup: Supervisor) -> Integrator:
    mode = sup.cfg.integrate.mode
    if mode == "direct":
        from .direct import Direct

        return Direct(sup)
    if mode == "train":
        from .train import Train

        return Train(sup)
    return Nothing(sup)
