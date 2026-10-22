# Architecture

nohalt is one supervisor loop and a set of small, replaceable parts:

```
src/nohalt/
  supervisor.py    the loop: reap → watch → integrate → launch → budget
  queue.py         queue.txt: "<issue>: <deps>"
  state.py         issue records and control files (JSON, written atomically)
  worktree.py      one git worktree per issue; rescue before removal
  models.py        routing, pins, premium gate, probes, fallbacks
  budget.py        paid-API spend estimate and caps
  prompt.py        core prompt + stack addendum + project files; strict placeholders
  verify.py        the verification lock (slot pool) and refused commands
  forge/           the forge interface; GitHub (gh REST), GitLab (REST v4); a file-backed forge
  agents/          opencode, claude-code, any command
  stacks/          generic (shell hooks), frappe (site pool on a shared bench)
  integrate/       none | direct | train, plus the bookkeeping merge drivers
  shims/           gh, glab, git, pkill guards put first on every agent's PATH
  cli.py           the `nohalt` command
```

## The loop

Every `tick` (default 60 s):

1. **Reap.** For every issue whose agent is running: past its time limit → stop it (`timeout`). No output for `hung_after` and no command of its own running → stop it (`hung`). Exited → *judge* it.
2. **Watch.** For every issue whose agent finished with a pull request: read the PR's checks on its **current head**. Red → hand back to an agent with the failing checks. Green but unmergeable → hand back to rebase. Green and mergeable → `ready`.
3. **Integrate.** `direct` or `train` merges `ready` PRs (see below). `none` does nothing: a human merges. Either way, an issue still open after its PR merged is closed by nohalt -- forges act on closing keywords only for the default branch.
4. **Launch.** Read the queue again (it can be edited live). Walk it in order. An issue seen for the first time that already has an open PR (opened by hand, or by a runner nohalt replaced) is **adopted**: it goes straight to `waiting_ci`, and gets an agent only if that PR needs one. For each other open issue that is not blocked, not held, has its dependencies closed, and has passed its backoff time, launch an agent if a slot in its model's pool is free, enough memory is available, and the model answers a probe (or a fallback does).
5. **Budget.** Every few minutes, estimate paid-API spend and move issues over their cap to a fallback model.

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
```

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
| None of the above | requeued after `revive_grace` (it may have stopped to ask). Blocked after `max_attempts` | **yes** |

A *resumed* launch continues the agent's previous session in the same worktree with a short "you were stopped, check git status and your PR first" prompt. A *fresh* launch (used after a rebase request, when the world has moved on) starts a new session from the full prompt.

## Integration

**direct.** Takes the first `ready` issue in queue order, checks again that its PR is green on its current head and mergeable, merges it, then watches the pipeline on the base branch's new head (`cd_workflow`, else `ci_workflow`, else the required checks). Nothing else merges until that pipeline is green. If it is red, or times out, nohalt opens a fix-forward issue at the front of the queue, and merging resumes when that issue closes. A `deploy` command, if set, runs after a green pipeline (unless a CD workflow already deploys).

**train.** With no release PR open, every `ready` issue's green PR (priority list first, then queue order, minus held issues and parked SHAs) is merged into `release/auto-<time>` off the base. Conflicts go to the merge drivers by glob (`[integrate.mergers]`). If any file stays in conflict, that PR is left out and handed back to rebase. The batch gets the next free version (from tags, or the version files), an optional regeneration command, and a PR closing every carried issue. On a later tick: green and mergeable → merge, tag, deploy, then the same pipeline gate as `direct` watches the base branch. Red → re-run the failed jobs once (more if the failure was the network). Still red → close the release, park the carried heads until their agents push, and hand each issue back.

## State on disk

```
<state_dir>/
  supervisor.lock          one supervisor at a time
  issues/<n>.json          IssueRecord: status, counters, model, pid, branch, worktree, PR, history
  pids.json                process start times (a recycled pid is not our agent)
  pins.json approved.json  model pins and premium approvals
  probes.json limited.json model probe cache and usage-limit marks
  spend.json               budget high-water marks
  integrate.json train.json integrator state that spans ticks
  status.json              last tick's summary (for `nohalt status`)
  STOP PAUSE WAKE BATCH_PAUSE DEPLOY_BLOCKED BUDGET_EXHAUSTED   control flags
  priority hold            one issue per line (train ordering / exclusion)
  bin/                     guard shims
  locks/verify-<slot>.lock verification slots
  agent-data/<n>/          per-agent session data (opencode XDG_DATA_HOME)
  logs/                    nohalt.log, events.jsonl, issue-<n>.log, guard.log, refused.log, deploy.log
  work/<n>/                worktrees (unless [project] worktrees is set)
  work/_release/           the train's integration tree
```

## Extending

- **A new agent CLI:** implement `launch`, `probe_launch`, `provider_failed` and `usage_limited` (see `agents/opencode.py`), and register it in `agents.get`. Before writing an adapter, try `command:` with `[agents.options.command] argv = [...]`.
- **A new stack:** subclass `stacks.Stack`. Implement `apply_defaults` (slots, refused commands, merge drivers) and `prepare` (bring the test environment to the worktree's commit inside the lock), and optionally a prompt addendum.
- **A new forge:** implement the `forge.base.Forge` protocol. Keep to its rule: a failed call raises `ForgeError`. It never returns something that looks like an answer.
- **A new merge driver:** a function `(repo, path) -> bool` in `integrate/mergers.py`, added to `DRIVERS`, with a test that runs it inside a real conflicted merge.
