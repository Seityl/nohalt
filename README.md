# nohalt

**Autonomous coding agents that keep going until the forge says done.**

[![CI](https://github.com/Seityl/nohalt/actions/workflows/ci.yml/badge.svg)](https://github.com/Seityl/nohalt/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)

nohalt supervises a fleet of AI coding agents ([opencode](https://opencode.ai), [Claude Code](https://www.claude.com/product/claude-code), or any CLI) working through a queue of GitHub or GitLab issues: one agent per issue, each in its own git worktree, until every issue is **closed** -- not until the agents *say* they are done.

An endpoint that raised `NameError` on every call once shipped with green CI and a confident agent summary, and stayed broken for two months. Agents report success they have not earned. So nohalt believes exactly one thing: the issue's state on the forge, and an issue closes only when a reviewed, green pull request merges.

Everything else in nohalt exists because something went wrong without it, while agents ran unattended through the night on three production projects. Those lessons -- around a hundred of them -- are written up in [**docs/LESSONS.md**](docs/LESSONS.md), and the ones nohalt enforces are pinned by tests.

## What it does

```
queue.txt ──► scheduler ──► agent in its own worktree ──► PR ──► CI ──► integrator ──► merged ──► issue CLOSED
 (deps)      slots, memory,     (opencode / claude-code)          │       (direct or         │
             a model that                │                         │        release train)    └─► pipeline watched;
             answers                     ▼                         ▼                              red → fix-forward
                               verify lock: one test env    red / conflicting → handed back      issue, merging stops
                               per slot, at this commit     to the agent with the failure
```

- **Dependency-ordered queue.** `14: 13` means #14 waits until #13 is closed -- or, with `early_start`, starts once #13's PR is green, on a synthetic base with #13 merged in, and still integrates only after it. Order is preference; dependencies are law. Edit it while nohalt runs.
- **GitHub or GitLab.** gitlab.com or self-hosted: merge requests, pipeline jobs as checks, `glab` for agents. Subgroups are fine.
- **Parallel agents, isolated.** One worktree and branch per issue; guard shims on each agent's `PATH` refuse merging, closing issues, pushing to the base branch, `git stash` (worktrees share one stash stack), and machine-wide `pkill`.
- **A verification lock.** Agents edit in parallel but verify one at a time per test slot (`nohalt verify <worktree> -- '<cmd>'`), with the environment reset to the commit under test.
- **Failures classified, not counted blindly.** A model-endpoint outage is not an attempt on the issue. A time limit continues in the same worktree. A hung agent (no output, no command running) is restarted. A red PR goes back to an agent with the failing checks. Only real failures spend attempts; then the issue is labelled for a human and the rest of the queue carries on.
- **A review gate, optional.** Green is not acceptance. Issues you select (`[review]` by label, title or all) go to `in_review` when green: a second agent, on a model chosen from the author's (a local model reviewing Claude's work, and the other way round; never the model that wrote the PR), checks the PR against every acceptance item in the issue, read-only, and either approves it or hands it back to its author with a numbered list. Unresolved after `max_rounds` → a human.
- **One merger.** Agents never merge. `direct` mode merges green PRs one at a time and stops everything if the pipeline on the base branch goes red; `train` mode batches green PRs into a versioned release branch, resolving bookkeeping conflicts (package.json, migration lists, schema JSON) mechanically and handing real code conflicts back. A PR that conflicts with the base only in bookkeeping is refreshed by nohalt rather than handed back. A suite too slow to gate on can be watched without gating (`async_workflows`): one issue while it is red, closed when it is green.
- **Release windows.** Production changes when you say: the train opens a normal batch only inside daily windows (`05:00-06:00` in your time zone, chosen weekdays, one release per window), while hotfixes, priority issues and the fix for a red base go out alone at any time. `nohalt release-now` ships once, off schedule.
- **Survives itself.** Agents run in their own sessions. Kill or restart the supervisor and it adopts the running agents from their records. `nohalt keepalive` in cron brings it back. A changed `nohalt.toml` applies without a restart by hand.
- **Prompt lanes.** Issues that need different rules (a tracker's children delivered in full, docs issues verified lightly) get their own prompt files by label, title, description or tracker, chosen at the first launch and kept for every relaunch.
- **Model routing and a spend guard.** Route by label or title, pin per issue, gate premium models behind approval, fall over to a model that answers, and cap paid-API spend per issue and in total. A model server that takes all its agents down at once is marked down, and its failures cost no retries. A subscription near or at its usage window cools its pool down until the reset, and new work goes elsewhere meanwhile.
- **Stacks.** A `generic` stack (your own `up`/`reset`/`env` hooks for the test environment) and a `frappe` stack (a pool of agent test sites on a shared bench, worktree-first `PYTHONPATH`, migrate before test, DocType/fixture/patch merge drivers).

## Quick start

```bash
pip install nohalt          # Python 3.11+, Linux; needs git, plus gh (GitHub) or glab (GitLab)
cd your-repo
nohalt init                 # writes nohalt.toml and queue.txt (--stack frappe for Frappe apps)
$EDITOR nohalt.toml queue.txt
nohalt doctor --probe       # checks config, forge auth, branch protection, agent binaries, models
nohalt run --dry-run        # what would launch now, what waits on what
nohalt run                  # supervise; Ctrl-C leaves agents running, `nohalt run` adopts them again
nohalt status --watch       # one screen: agents, idle time, attempts, last line of each log
```

Keep it running without a terminal:

```cron
*/3 * * * * cd /path/to/your-repo && nohalt keepalive
```

Control while it runs: `nohalt restart` (now, with the config on disk; agents keep running), `nohalt stop` (a drain: launch nothing more, exit once no agent runs), `nohalt stop --now`, `nohalt pause [--for 2h]`, `nohalt resume` (undo `stop`/`pause`), `nohalt release-now` (train: one release outside the windows), `nohalt route <issue> <model>`, `nohalt unblock <issue>`, `nohalt queue --add 42 --after 41`, `nohalt prompt <issue>` (see exactly what an agent will be told).

Edits to `nohalt.toml` apply on their own: the supervisor notices the change between ticks, re-executes itself and adopts its running agents. A file that does not parse or validate is reported in `nohalt status` (`CONFIG_INVALID`) and the last good configuration keeps running. Upgrading works the same way: `pip install -U nohalt`, and the running supervisor re-executes on the new version within a tick (or run `nohalt restart`).

## Before you run it

nohalt gives AI agents write access to your repository and, if you enable an integrator, lets nohalt merge and deploy. **Read [docs/SAFETY.md](docs/SAFETY.md).** In short: protect the base branch on the forge (the shims are speed bumps, protection is the wall), use a token scoped to the one repository, run agents as a user that cannot reach production, start with `integrate.mode = "none"`, and watch the first few issues end to end.

## Documentation

- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) -- every key in `nohalt.toml`
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) -- the supervisor loop, issue lifecycle, state on disk
- [docs/STACKS.md](docs/STACKS.md) -- generic and Frappe test environments, writing your own
- [docs/SAFETY.md](docs/SAFETY.md) -- the threat model and the guardrails
- [docs/LESSONS.md](docs/LESSONS.md) -- what running agent fleets unattended taught us

## Status

Alpha. The mechanisms come from scripts that ran production agent fleets for weeks; this package is their consolidation into one tested, configurable tool, and it is younger than they are. Before the first release it was run end to end on both forges -- real agents (Claude Code on GitHub, opencode on a self-hosted GitLab), real CI, both `none` and `direct` integration, a supervisor `kill -9` mid-run -- and the bugs those runs found are fixed. Expect rough edges. Issues and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © Jeriel Francis trading as Seityl, and the nohalt contributors.
