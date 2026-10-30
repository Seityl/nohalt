# Architecture

nohalt is one supervisor loop and a set of small, replaceable parts:

```
src/nohalt/
  supervisor.py    the loop: reap → watch → integrate → launch → budget
  queue.py         queue.txt: "<issue>: <deps>"
  state.py         issue records and control files (JSON, written atomically)
  worktree.py      one git worktree per issue; rescue before removal
  models.py        routing, pins, premium gate, probes, fallbacks, overflow
  capacity.py      models that are down (endpoint storms, failed probes), pools cooling down after a usage limit
  budget.py        paid-API spend estimate and caps
  prompt.py        core prompt + stack addendum + project files + the issue's lane; strict placeholders
  verify.py        the verification lock (slot pool) and refused commands
  upgrade.py       a new nohalt installed under the running supervisor: modules preloaded, re-exec once it settles
  forge/           the forge interface; GitHub (gh REST), GitLab (REST v4); a file-backed forge
  agents/          opencode, claude-code, any command
  stacks/          generic (shell hooks), frappe (site pool on a shared bench)
  early.py         early start: a dependant on a synthetic base of its dependencies' unmerged PRs
  review.py        the optional review gate: a reviewer agent, chosen by the author's model, checks green PRs against their issue
  integrate/       none | direct | train, the bookkeeping merge drivers, the async workflow watch, release windows, the post-deploy check and the live-version watch
  shims/           gh, glab, git, pkill guards put first on every agent's PATH
  cli.py           the `nohalt` command
```

## The loop

Every `tick` (default 60 s):

1. **Reap.** For every issue whose agent is running: past its time limit → stop it (`timeout`). No output for `hung_after` and no command of its own running → stop it (`hung`). Exited → *judge* it.
2. **Watch.** For every issue whose agent finished with a pull request: read the PR's checks on its **current head**. Red → hand back to an agent with the failing checks. Green (or no checks yet) but conflicting with the base → if the merge drivers settle every conflicted file, nohalt merges the base into the PR branch itself and pushes (`refresh_conflicts`, INT-12). Otherwise it hands the PR back to rebase. Checks pending, or a head handed back already that came back unmoved, don't make a conflicting PR wait: a red check hands it back at once, and after `conflict_wait` it is refreshed or handed back with its conflicted paths (INT-19). A PR in review is not refreshed until its verdict, and in `train` mode a green PR whose conflicts the drivers settle is left to the batch, unrefreshed (INT-16); that is logged once per PR head and base, and the trial merge's answer is kept in `trial_merges.json`, so a later tick or a restarted supervisor does not merge again (INT-18). Green and mergeable → `ready` -- or, for an issue `[review]` selects whose head has no approval yet, `in_review`.

   **Review.** For each `in_review` issue: no reviewer yet → launch one (the reviewer `[review] models` maps the author's model to, else `model`, else `fallbacks`; never the author's own model with `distinct`; a slot in its model's pool, a model that answers -- with `during_cooldown = "warning"`, also on a pool only a usage warning cools down) in a detached checkout of the PR head, read-only. Running → time limit and hang checks, as for agents. Exited → read its verdict: `approve` (and the PR head is still the commit reviewed) → `ready`; `changes` → handed back to the author with the review; back green with the same head, it is reviewed again if the PR description differs from the one the reviewer read (a hash on the record), and otherwise counts another round (AGT-22); no verdict → retried, never approved. A head that moves during a review stops the reviewer and voids its verdict.
3. **Integrate.** `direct` or `train` merges `ready` PRs (see below). `none` does nothing: a human merges. Either way, an issue still open after its PR merged is closed by nohalt -- forges act on closing keywords only for the default branch.
   **Async workflows** (`async_workflows`) are read here too, in any mode: the newest completed run of each on the base branch. They never hold a merge. Red opens one issue per workflow (later reds comment on it), green closes it.
   **Capacity.** Running agents' logs are read for usage lines: a usage limit or a warning cools down the model's pool until its reset. Expired cool-downs end, and down models whose turn has come are probed again. An expired `pause --for` was cleared at the start of the tick.
4. **Launch.** Read the queue again (it can be edited live). Walk it in order. An issue seen for the first time that already has an open PR (opened by hand, or by a runner nohalt replaced) is **adopted**: it goes straight to `waiting_ci`, and gets an agent only if that PR needs one. For each other open issue that is not blocked, not held, has its dependencies closed (or, with `early_start`, closed or with a PR green -- or approved -- on its current head; see below), and has passed its backoff time, launch an agent if a slot in its model's pool is free, enough memory is available, and the model answers a probe (or a fallback with a free slot does). An unpinned issue that has waited `overflow_after` for a slot overflows to the first `overflow_to` model with a free slot (outside `overflow_reserve`) that answers; the next launch is routed as usual. An unpinned issue whose model is down or whose pool is cooling down is diverted at once to the first fallback or overflow target that can start (`capacity: … → …`). A pinned issue, or one stopped by its model's usage limit, waits -- except that with `[models] pinned_during_cooldown = "warning"` a pinned issue starts on a pool only a usage warning cools down.
5. **Budget.** Every few minutes, estimate paid-API spend and move issues over their cap to a fallback model.

Before each tick the supervisor checks `nohalt.toml`, the `RESTART` flag and the nohalt package on disk. A valid change to the file, a restart request, or a newly installed nohalt that has settled and imports cleanly (LIV-14; every module was imported at start, so nothing mixes versions meanwhile), makes it release its lock and `exec` itself; the new process adopts the running agents like any restarted supervisor. An invalid file keeps the running configuration and raises `CONFIG_INVALID` (see [CONFIGURATION.md](CONFIGURATION.md#changing-it-while-nohalt-runs)).

The supervisor holds an exclusive lock, so only one runs per state directory. A tick never blocks on long waits: watching a pipeline after a merge, or waiting for a usage limit to reset, is a state that later ticks re-examine.

**Early start** (`[agents] early_start`, SCH-10). An issue whose open dependencies all have PRs in the `early_start_on` state is launched on a synthetic base: `early.py` merges each dependency's PR head into the fresh worktree (dependency order, merge drivers) before the agent starts, records the heads on the record (`early_on`), and the prompt says what was merged and that it is not merged yet. A conflict in code aborts that launch (`NoEarlyStart`) and the issue waits as usual. Every tick, before the integrator, each early dependant is followed: a dependency head that moved leaves a note for its next launch (`early_note`); once all its dependencies have closed, its branch is refreshed onto the real base (`integrate/refresh.py`, clean merges too) and `early_on` is cleared. Until then `Early.held` keeps it out of `direct` merges and out of any train batch its dependencies are not in, and the train orders a batch so dependencies merge first.

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

With `[integrate] live_version`, `ready ──merged──► released ──live version contains the merge──► closed ──► done` (DEP-15): a released issue is not closed by nohalt, never relaunched, and not watched for its PR; `integrate/live.py` closes it, or comments on it if the forge already closed it, once production runs it.

`done` is not the end if a human reopens the issue (AGT-20). Each tick, one listing of the forge's open issues updated since the last one finds done records whose issue is open again. Such a record goes back to `queued` with fresh counters and its history kept, and a fresh launch is told the issue was reopened, with the comments made since nohalt saw it closed. The branch is kept unless it was merged (its PR merged, or the branch is in the base); then a new `<branch>-reopened` is cut from the base.

With `[review]`, a selected issue goes `waiting_ci ──green──► in_review ──approve──► ready`. A `changes` verdict hands it back to `queued` (resumed with the review) like a red CI, up to `max_rounds`, then `blocked`. A push after an approval sends it back to `waiting_ci`, and then to a new review -- unless it is a nohalt refresh that changed only bookkeeping, which carries the approval (`review_carried`) and needs only green CI again (INT-16).

How an exit is judged, in order:

| Evidence | Verdict | Costs an attempt? |
|---|---|---|
| Forge says CLOSED (or the issue no longer exists) | done: worktree rescued and removed, agent data deleted | -- |
| Forge unreachable | `exited`, judged again next tick | no |
| Stopped by the budget guard or by `stop --now` | requeued, resumes | no |
| The agent's output ends on a usage-limit event | the pool cools down until the reported reset (else `usage_cooldown`). The issue waits for that model and resumes its session there | no |
| The agent's output ends on a model-endpoint error, and the model is down, or this error makes it down (a storm) | requeued, not counted; the storm's earlier failures are refunded. Blocked only once its model and every fallback were down `down_block_after` | no |
| The agent's output ends on a model-endpoint error | requeued with backoff × n. Blocked after `provider_retries`, with a diagnosis that says it was not the issue's fault | no |
| Time limit, issue still open | continues in the same worktree, up to `timeout_continues` times | no |
| Hung | restarted, up to `revive_limit` times | no |
| A pull request exists | `waiting_ci` | no |
| The agent ended with `NOHALT-RESULT: no-change -- <reason>` | an async workflow's issue: `awaiting_green` (relaunched only by a red run with failures it has not seen). Any other: blocked with the reason | no |
| None of the above | requeued after `revive_grace` (it may have stopped to ask). Blocked after `max_attempts` | **yes** |

**Prompt lanes.** At an issue's first launch, the first `[[prompt.lane]]` whose selector matches (labels, title, description, or "Part of #N" for a tracker) becomes its lane, and is written on its record with a digest of the lanes as configured. The prompt is the core prompt, the stack addendum, `[prompt] extra` minus the lane's `replace`, then the lane's `extra`, with `{{LANE}}` from its `verify_scope`. Later launches and the reviewer read the lane from the record. Only a different digest (the lanes were edited) chooses again; a different answer is logged, and a resumed session gets the new prompt in full.

A *resumed* launch continues the agent's previous session in the same worktree with a short "you were stopped, check git status and your PR first" prompt. A *fresh* launch (used after a rebase request, when the world has moved on) starts a new session from the full prompt.

## Integration

**direct.** Takes the first `ready` issue -- approved PRs first, in approval order, with `approved_first` (INT-17) -- in queue order, checks again that its PR is green on its current head and mergeable, merges it, then watches the pipeline on the base branch's new head (`cd_workflow`, else `ci_workflow`, else the required checks). Every check of the pipeline the merge triggered counts, except `base_ignore_checks` (forge-managed jobs that post on the base head afterwards, like Dependabot updates on GitHub, are not that pipeline) -- including checks `ignore_checks` skips on PRs, whose only gate is this one. Nothing else merges until that pipeline is green. A cancelled run is no verdict: it is re-run, and after `base_cancelled_limit` in a row merging is held (`BASE_NO_VERDICT`) for a human. If it is red, or times out, nohalt opens a fix-forward issue at the front of the queue, and merging resumes when that issue closes. A `deploy` command, if set, runs after a green pipeline (unless a CD workflow already deploys).

**After the deploy** (DEP-14, DEP-15). `post_deploy_check` runs once the `deploy` command succeeds, or once `cd_workflow` is green. The gate keeps its retries in `integrate.json` (`post_check`) and holds merging while they are due. A final failure writes `DEPLOY_BLOCKED` and opens one issue (`check_block`), which is treated like a fix-forward issue. A later passing check clears the flag. With `live_version`, the integrator merges and then hands the carried issues to `LiveWatch` instead of closing them. `LiveWatch` runs `live_version` every tick, checks each released merge commit with `git merge-base --is-ancestor`, and closes or comments on the issues when it is live, or warns after `live_timeout`.

**Async workflows.** A workflow listed in `async_workflows` is never part of that gate (on GitHub its jobs are excluded from the base verdict by check suite). `integrate/async_watch.py` reads its newest completed run on the base every tick, in every mode, and keeps at most one open issue per workflow: opened on red at the front of the queue, commented on each further red (new and fixed failures, read from the failed log), closed with a comment on the first green. An agent on that issue that finds the base already fixed ends with `NOHALT-RESULT: no-change -- <reason>`; the issue waits in `awaiting_green` rather than being blocked.

**train.** With no release PR open, every `ready` issue's green PR (priority list first, then -- with `[review]` and `approved_first` -- PRs approved for their current head in approval order, then queue order, minus held issues and parked SHAs) is merged into `release/auto-<time>` off the base. Conflicts go to the merge drivers by glob (`[integrate.mergers]`). If any file stays in conflict, that PR is left out. It is refreshed against the base if the drivers settle that (see `refresh_conflicts`), otherwise handed back to rebase. The batch gets the next free version (from tags, or the version files), an optional regeneration command, and a PR closing every carried issue. On a later tick: green and mergeable → merge, tag, deploy, then the same pipeline gate as `direct` watches the base branch. Red → re-run the failed jobs once (more if the failure was the network). Still red → close the release, park the carried heads until their agents push, and hand each issue back. While a release's CI has not started (no check beyond `queued`), an approved PR that merges cleanly with it joins it: merged into the release branch, re-stamped, pushed and carried (INT-17); one that does not goes first in the next batch. A candidate whose mergeability the forge has not computed yet is read again once, `MERGEABLE_WAIT` seconds later (the read starts the computation). Still unknown, it is left out with `#n: left out of this batch -- forge has not computed mergeability yet`, and goes first in the next batch, after the priority list (`mergeable_first` in `train.json`, INT-18).

**Release windows** (`[integrate.train] windows`, DEP-13) decide when a batch may *open*. Inside a window the train batches as above, until `per_window` normal releases have merged in that window instance (a red or closed batch does not count). Outside a window, or once the quota is used, a batch opens only if a ready issue is a hotfix (a `hotfix_labels` label, `<state>/priority`, or the fix-forward issue of a red base), and it carries only the hotfixes. An open release always runs to the end (CI, merge, tag, deploy, base watch), whatever the time. `nohalt release-now` writes `RELEASE_NOW`: the next batch is a normal one regardless of windows, used once. The train records each release's kind (`normal`, `hotfix`, `manual`) and window in `train.json`, the merged count per window (`windows_used`), and what waits on the schedule (`waiting`, shown by `nohalt status`).

## State on disk

```
<state_dir>/
  supervisor.lock          one supervisor at a time
  issues/<n>.json          IssueRecord: status, counters, model, pid, branch, worktree, PR, lane (and the lanes' digest), history, review_* (reviewer pid, verdict, rounds, the head and description hash reviewed), early_* (the dependency heads an early start was built on)
  pids.json                process start times (a recycled pid is not our agent or reviewer)
  reviews/<n>.md           the last review a reviewer wrote
  pins.json approved.json  model pins and premium approvals
  probes.json limited.json model probe cache and usage-limit marks
  capacity.json            down models (since, next probe), recent endpoint errors, pools cooling down (until, window, source)
  spend.json               budget high-water marks
  integrate.json train.json integrator state that spans ticks (the base watch, fix-forward issue, post-deploy check and its block)
  refresh.json conflicts.json  conflicting PRs refreshed by nohalt (counted per issue); files left in conflict at hand-backs (for doctor)
  async.json               per async workflow: its open issue, the last run read, its failures, what the agent saw
  live.json                released merges not yet live (issues, merge commit, tag, since) and what live_version last printed
  status.json              last tick's summary (for `nohalt status`)
  supervisor.json          the running supervisor: pid, start time, hash of the config it loaded
  STOP PAUSE WAKE RESTART BATCH_PAUSE RELEASE_NOW DEPLOY_BLOCKED BUDGET_EXHAUSTED CONFIG_INVALID   control flags
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

- **A new agent CLI:** implement `launch`, `probe_launch`, `provider_failed`, `usage_limited` and `usage_signal` (`agents.usage_signal` with `usage_limit`/`usage_warning` patterns does it; see `agents/opencode.py`), and register it in `agents.get`. Before writing an adapter, try `command:` with `[agents.options.command] argv = [...]`.
- **A new stack:** subclass `stacks.Stack`. Implement `apply_defaults` (slots, refused commands, merge drivers) and `prepare` (bring the test environment to the worktree's commit inside the lock), and optionally a prompt addendum.
- **A new forge:** implement the `forge.base.Forge` protocol. Keep to its rule: a failed call raises `ForgeError`. It never returns something that looks like an answer.
- **A new merge driver:** a function `(repo, path) -> bool` in `integrate/mergers.py`, added to `DRIVERS`, with a test that runs it inside a real conflicted merge.
