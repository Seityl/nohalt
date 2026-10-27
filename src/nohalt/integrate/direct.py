"""Direct integration: merge green PRs one at a time, then watch the pipeline.

After each merge the run on the base branch (``cd_workflow``, else
``ci_workflow``, else the required checks on the new base commit) is
followed. Until it is green nothing else merges: nothing should build on a
pipeline that cannot deploy. A red or timed-out pipeline opens a fix-forward
issue at the front of the queue, and merging resumes when it closes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import queue
from ..forge import ForgeError, verdict
from ..forge.base import GREEN
from ..state import READY
from .common import PipelineGate, cancel_superseded, close_merged
from .refresh import before_hand_back

if TYPE_CHECKING:
    from ..supervisor import Supervisor


class Direct:
    def __init__(self, sup: Supervisor):
        self.sup = sup
        self.cfg = sup.cfg
        self.gate = PipelineGate(sup)

    def tick(self) -> None:
        if self.cfg.integrate.cancel_superseded:
            cancel_superseded(self.sup)
        if self.gate.open():
            self._merge_next()

    def _merge_next(self) -> None:
        try:
            order = [e.issue for e in queue.load(self.cfg.queue_file)]
        except queue.QueueError:
            order = []
        ready = [r for r in self.sup.state.all() if r.status == READY and r.pr]
        if self.gate.only is not None:
            ready = [r for r in ready if r.issue == self.gate.only]
        if not ready:
            return
        ready.sort(key=lambda r: order.index(r.issue) if r.issue in order else len(order))
        rec = ready[0]
        f = self.sup.forge
        base = self.cfg.project.base_branch
        try:
            pr = f.pull(rec.pr)
            v = verdict(f.checks(pr.head_sha), self.cfg.integrate.required_checks, self.cfg.integrate.ignore_checks)
        except ForgeError:
            return
        if pr.head_sha != rec.pr_sha or v != GREEN:
            return  # the watcher catches up on the next tick
        if pr.mergeable is None or pr.draft:
            return  # the forge is still computing it (GitLab: unchecked, checking, ...): ask again next tick
        if pr.mergeable is False:
            if before_hand_back(self.sup, rec, pr):
                return  # bookkeeping only: refreshed, it goes through CI again (INT-12)
            self.sup.hand_back(
                rec.issue,
                f"{self.sup.pr(pr.number)} cannot merge",
                f"Your {self.sup.pr_long(pr.number)} conflicts with `{base}`. Merge `origin/{base}` into your branch, keeping "
                "both sides' behaviour, re-run your tests, push, and stop.",
            )
            return
        if not f.merge_pull(pr.number, self.cfg.integrate.merge_method):
            self.sup.say(f"merge of {self.sup.pr(pr.number)} (#{rec.issue}) refused by the forge; will retry")
            return
        self.sup.say(f"merged {self.sup.pr(pr.number)} (#{rec.issue})")
        rec.event(f"merged {self.sup.pr(pr.number)}")
        self.sup.state.put(rec)
        close_merged(self.sup, [rec.issue], f"{self.sup.pr(pr.number)}")
        self.gate.merged(f"#{rec.issue}", deploy_after=True)
