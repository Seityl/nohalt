"""A batch candidate whose mergeability the forge has not computed yet is asked again, then said and kept first (INT-18)."""

from __future__ import annotations

from nohalt.integrate.train import MERGEABLE_WAIT
from test_approved_first import PR, batch, make, ready
from test_windows import at, releases, tick


def unknown(w, n: int) -> None:  # type: ignore[no-untyped-def]
    with w.forge._db() as d:
        d["pulls"][str(PR + n)]["mergeable"] = None


def computed(w, n: int) -> None:  # type: ignore[no-untyped-def]
    with w.forge._db() as d:
        d["pulls"][str(PR + n)]["mergeable"] = True


def test_an_unknown_mergeability_is_asked_again_once_and_then_batched(world, repo):
    """#78: GitHub said UNKNOWN for an approved, green PR, and MERGEABLE a minute later when asked again."""
    w = make(world, review=False)
    for n in (1, 2):
        ready(w, repo, n)
    unknown(w, 1)
    waits: list[float] = []

    def wait(s: float) -> None:
        waits.append(s)
        computed(w, 1)  # asking started the forge's computation

    w.sup.integrator.sleep = wait
    tick(w, at(14))
    assert batch(w) == [1, 2]
    assert waits == [MERGEABLE_WAIT]
    assert not any("left out of this batch" in m for m in w.messages)


def test_still_unknown_it_is_left_out_said_and_first_in_the_next_batch(world, repo):
    w = make(world, review=False)
    for n in (2, 1):
        ready(w, repo, n)
    unknown(w, 1)
    waits: list[float] = []
    w.sup.integrator.sleep = waits.append
    tick(w, at(14))
    assert batch(w) == [2]
    assert waits == [MERGEABLE_WAIT]  # one wait for the whole batch, not one per PR
    assert "#1: left out of this batch -- forge has not computed mergeability yet" in w.messages
    assert w.sup.state.load("train.json")["mergeable_first"] == [1]
    # That release is closed; the next batch takes #1 first, ahead of the queue order.
    (rel,) = releases(w)
    w.forge.close_pull(rel.number, "test")
    computed(w, 1)
    tick(w, at(14, 5))
    assert batch(w) == [1, 2]
    assert w.sup.state.load("train.json")["mergeable_first"] == []


def test_known_mergeability_costs_no_wait(world, repo):
    w = make(world, review=False)
    ready(w, repo, 1)
    waits: list[float] = []
    w.sup.integrator.sleep = waits.append
    tick(w, at(14))
    assert batch(w) == [1] and waits == []


def test_a_failed_read_is_unknown_too(world, repo):
    """API-02: a forge call that failed says nothing about the PR."""
    from nohalt.forge import ForgeError

    w = make(world, review=False)
    for n in (1, 2):
        ready(w, repo, n)
    real = w.forge.pull

    def pull(number: int):  # type: ignore[no-untyped-def]
        if number == PR + 1:
            raise ForgeError("timeout")
        return real(number)

    w.forge.pull = pull  # type: ignore[method-assign]
    w.sup.integrator.sleep = lambda _s: None
    tick(w, at(14))
    assert batch(w) == [2]
    assert "#1: left out of this batch -- forge has not computed mergeability yet" in w.messages
