# Architecture

nohalt is one supervisor loop and a set of small, replaceable parts:

```
src/nohalt/
  supervisor.py    the loop: reap → watch → integrate → launch → budget
  queue.py         queue.txt: "<issue>: <deps>"
  state.py         issue records and control files (JSON, written atomically)
  worktree.py      one git worktree per issue; rescue before removal
  models.py        routing, pins, premium gate, probes, fallbacks, overflow
  budget.py        paid-API spend estimate and caps
  prompt.py        core prompt + stack addendum + project files; strict placeholders
  verify.py        the verification lock (slot pool) and refused commands
  forge/           the forge interface; GitHub (gh REST), GitLab (REST v4); a file-backed forge
  agents/          opencode, claude-code, any command
  stacks/          generic (shell hooks), frappe (site pool on a shared bench)
  review.py        the optional review gate: a reviewer agent checks green PRs against their issue
  integrate/       none | direct | train, the bookkeeping merge drivers, the async workflow watch
  shims/           gh, glab, git, pkill guards put first on every agent's PATH
  cli.py           the `nohalt` command
```

## The loop

Every `tick` (default 60 s):

1. **Reap.** For every issue whose agent is running: past its time limit → stop it (`timeout`). No output for `hung_after` and no command of its own running → stop it (`hung`). Exited → *judge* it.
2. **Watch.** For every issue whose agent finished with a pull request: read the PR's checks on its **current head**. Red → hand back to an agent with the failing checks. Green (or no checks yet) but conflicting with the base → if the merge drivers settle every conflicted file, nohalt merges the base into the PR branch itself and pushes (`refresh_conflicts`, INT-12). Otherwise it hands the PR back to rebase. Green and mergeable → `ready` -- or, for an issue `[review]` selects whose head has no approval yet, `in_review`.

   **Review.** For each `in_review` issue: no reviewer yet → launch one (a slot in its model's pool, a model that answers) in a detached checkout of the PR head, read-only. Running → time limit and hang checks, as for agents. Exited → read its verdict: `approve` (and the PR head is still the commit reviewed) → `ready`; `changes` → handed back to the author with the review; no verdict → retried, never approved. A head that moves during a review stops the reviewer and voids its verdict.
3. **Integrate.** `direct` or `train` merges `ready` PRs (see below). `none` does nothing: a human merges. Either way, an issue still open after its PR merged is closed by nohalt -- forges act on closing keywords only for the default branch.
   **Async workflows** (`async_workflows`) are read here too, in any mode: the newest completed run of each on the base branch. They never hold a merge. Red opens one issue per workflow (later reds comment on it), green closes it.
4. **Launch.** Read the queue again (it can be edited live). Walk it in order. An issue seen for the first time that already has an open PR (opened by hand, or by a runner nohalt replaced) is **adopted**: it goes straight to `waiting_ci`, and gets an agent only if that PR needs one. For each other open issue that is not blocked, not held, has its dependencies closed, and has passed its backoff time, launch an agent if a slot in its model's pool is free, enough memory is available, and the model answers a probe (or a fallback with a free slot does). An unpinned issue that has waited `overflow_after` for a slot overflows to the first `overflow_to` model with a free slot (outside `overflow_reserve`) that answers; the next launch is routed as usual.
5. **Budget.** Every few minutes, estimate paid-API spend and move issues over their cap to a fallback model.

Before each tick the supervisor checks `nohalt.toml` and the `RESTART` flag. A valid change to the file, or a restart request, makes it release its lock and `exec` itself; the new process adopts the running agents like any restarted supervisor. An invalid file keeps the running configuration and raises `CONFIG_INVALID` (see [CONFIGURATION.md](CONFIGURATION.md#changing-it-while-nohalt-runs)).

The supervisor holds an exclusive lock, so only one runs per state directory. A tick never blocks on long waits: watching a pipeline after a merge, or waiting for a usage limit to reset, is a state that later ticks re-examine.

## Issue lifecycle

```
            ┌──────────────── handed back (red CI, conflict, PR closed) ─────────────┐
            ▼                                                                        │
 queued ──launch──► running ──exit──► judged ──PR open──► waiting_ci ──green──► ready ──merged──► done
   ▲                  │                 │                                            ▲
   │                  │ timeout / hung  │ provider error / usage limit (not an attempt)
   │                  └─────────────────┴─► queued (resume, with backoff)            │
   │                                    │                                            │
   │                                    └─ no PR, no close: attempt+1 ─► queued      │
   │                                                       attempts used ─► blocked  │
   │                                                                                 │
   └──── a human removes the blocked label (counters reset)        forge says CLOSED ┘ (at any point)

 judged ──"NOHALT-RESULT: no-change"──► async workflow's issue: awaiting_green ──green run──► closed ──► done
                                         │                       └─ red run, failures not seen ─► queued (fresh)
                                         └─ any other issue: blocked ("agent says no change needed: <reason>")
```

With `[review]`, a selected issue goes `waiting_ci ──green──► in_review ──approve──► ready`. A `changes` verdict hands it back to `queued` (resumed with the review) like a red CI, up to `max_rounds`, then `blocked`. A push after an approval sends it back to `waiting_ci`, and then to a new review.

How an exit is judged, in order:

| Evidence | Verdict | Costs an attempt? |
|---|---|---|
| Forge says CLOSED (or the issue no longer exists) | done: worktree rescued and removed, agent data deleted | -- |
| Forge unreachable | `exited`, judged again next tick | no |
| Stopped by the budget guard or by `stop --now` | requeued, resumes | no |
| The agent's output ends on a usage-limit event | requeued after 15 min, the model is not probed for 20 min | no |
| The agent's output ends on a model-endpoint error | requeued with backoff × n. Blocked after `provider_retries`, with a diagnosis that says it was not the issue's fault | no |
| Time limit, issue still open | continues in the same worktree, up to `timeout_continues` times | no |
| Hung | restarted, up to `revive_limit` times | no |
| A pull request exists | `waiting_ci` | no |
| The agent ended with `NOHALT-RESULT: no-change -- <reason>` | an async workflow's issue: `awaiting_green` (relaunched only by a red run with failures it has not seen). Any other: blocked with the reason | no |
| None of the above | requeued after `revive_grace` (it may have stopped to ask). Blocked after `max_attempts` | **yes** |

A *resumed* launch continues the agent's previous session in the same worktree with a short "you were stopped, check git status and your PR first" prompt. A *fresh* launch (used after a rebase request, when the world has moved on) starts a new session from the full prompt.

## Integration

**direct.** Takes the first `ready` issue in queue order, checks again that its PR is green on its current head and mergeable, merges it, then watches the pipeline on the base branch's new head (`cd_workflow`, else `ci_workflow`, else the required checks). Every check of the pipeline the merge triggered counts, except `base_ignore_checks` (forge-managed jobs that post on the base head afterwards, like Dependabot updates on GitHub, are not that pipeline) -- including checks `ignore_checks` skips on PRs, whose only gate is this one. Nothing else merges until that pipeline is green. A cancelled run is no verdict: it is re-run, and after `base_cancelled_limit` in a row merging is held (`BASE_NO_VERDICT`) for a human. If it is red, or times out, nohalt opens a fix-forward issue at the front of the queue, and merging resumes when that issue closes. A `deploy` command, if set, runs after a green pipeline (unless a CD workflow already deploys).

**Async workflows.** A workflow listed in `async_workflows` is never part of that gate (on GitHub its jobs are excluded from the base verdict by check suite). `integrate/async_watch.py` reads its newest completed run on the base every tick, in every mode, and keeps at most one open issue per workflow: opened on red at the front of the queue, commented on each further red (new and fixed failures, read from the failed log), closed with a comment on the first green. An agent on that issue that finds the base already fixed ends with `NOHALT-RESULT: no-change -- <reason>`; the issue waits in `awaiting_green` rather than being blocked.

**train.** With no release PR open, every `ready` issue's green PR (priority list first, then queue order, minus held issues and parked SHAs) is merged into `release/auto-<time>` off the base. Conflicts go to the merge drivers by glob (`[integrate.mergers]`). If any file stays in conflict, that PR is left out. It is refreshed against the base if the drivers settle that (see `refresh_conflicts`), otherwise handed back to rebase. The batch gets the next free version (from tags, or the version files), an optional regeneration command, and a PR closing every carried issue. On a later tick: green and mergeable → merge, tag, deploy, then the same pipeline gate as `direct` watches the base branch. Red → re-run the failed jobs once (more if the failure was the network). Still red → close the release, park the carried heads until their agents push, and hand each issue back.

## State on disk

```
<state_dir>/
  supervisor.lock          one supervisor at a time
  issues/<n>.json          IssueRecord: status, counters, model, pid, branch, worktree, PR, history, review_* (reviewer pid, verdict, rounds)
  pids.json                process start times (a recycled pid is not our agent or reviewer)
  reviews/<n>.md           the last review a reviewer wrote
  pins.json approved.json  model pins and premium approvals
  probes.json limited.json model probe cache and usage-limit marks
  spend.json               budget high-water marks
  integrate.json train.json integrator state that spans ticks
  refresh.json conflicts.json  conflicting PRs refreshed by nohalt (counted per issue); files left in conflict at hand-backs (for doctor)
  async.json               per async workflow: its open issue, the last run read, its failures, what the agent saw
  status.json              last tick's summary (for `nohalt status`)
  supervisor.json          the running supervisor: pid, start time, hash of the config it loaded
  STOP PAUSE WAKE RESTART BATCH_PAUSE DEPLOY_BLOCKED BUDGET_EXHAUSTED CONFIG_INVALID   control flags
  priority hold            one issue per line (train ordering / exclusion)
  bin/                     guard shims
  locks/verify-<slot>.lock verification slots
  agent-data/<n>/          per-agent session data (opencode XDG_DATA_HOME); review-<n>/ for reviewers
  logs/                    nohalt.log, events.jsonl, issue-<n>.log, review-<n>.log, guard.log, refused.log, deploy.log
  work/<n>/                worktrees (unless [project] worktrees is set)
  work/_release/           the train's integration tree
  work/_refresh/           scratch tree for merging the base into conflicting PR branches
  work/_review/<n>/        a reviewer's detached checkout of the PR head (cut fresh for every review)
```

## Extending

- **A new agent CLI:** implement `launch`, `probe_launch`, `provider_failed` and `usage_limited` (see `agents/opencode.py`), and register it in `agents.get`. Before writing an adapter, try `command:` with `[agents.options.command] argv = [...]`.
- **A new stack:** subclass `stacks.Stack`. Implement `apply_defaults` (slots, refused commands, merge drivers) and `prepare` (bring the test environment to the worktree's commit inside the lock), and optionally a prompt addendum.
- **A new forge:** implement the `forge.base.Forge` protocol. Keep to its rule: a failed call raises `ForgeError`. It never returns something that looks like an answer.
- **A new merge driver:** a function `(repo, path) -> bool` in `integrate/mergers.py`, added to `DRIVERS`, with a test that runs it inside a real conflicted merge.
