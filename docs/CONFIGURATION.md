# Configuration

One `nohalt.toml` per project. `nohalt init [--stack frappe]` writes a starter file, and `nohalt doctor` checks it. nohalt looks for `nohalt.toml` in the current directory and its parents, or takes `--config PATH`. Relative paths are relative to the file.

**Unknown keys are errors.** A typo that silently falls back to a default is how a guard quietly stops guarding.

## Changing it while nohalt runs

Edit the file; there is nothing to restart. Between ticks (and within a second while it waits) the supervisor compares the file with the one it loaded:

- **Valid and different:** it re-executes itself (`python -m nohalt --config <file> run`, same process id) and adopts its running agents from their records, as after any restart (LIV-01). The `=== nohalt supervising …` line in `logs/nohalt.log` shows the new settings.
- **Invalid** (TOML error, unknown key, failed validation): it logs the error once, keeps running on the last good configuration, and writes `<state>/CONFIG_INVALID`, which `nohalt status` shows with the error until the file is fixed or reverted.
- **Comments or formatting only:** nothing happens.

Changes apply to what the supervisor does next, never to an agent already running: a new `models.default`, route or pool cap applies to the next launch, and an agent over a lowered cap finishes its run. `[project] state_dir` cannot change under a running supervisor (its agents are recorded in the old one): that edit is reported as invalid. `queue.txt` and `<state>/caps.json` are re-read every tick anyway.

`nohalt restart` does the same on demand: it validates the file first (a broken config never replaces a running supervisor), asks the supervisor to re-execute after its current tick, and starts one, detached, if none is running -- no cron needed. Agents are never signalled. If the supervisor does not respond within `--timeout` (default 120 s), it is sent SIGTERM and a new one is started. `restart` clears `STOP`.

**Upgrading nohalt:** install the new version (`pip install -U nohalt`) and leave the supervisor running; it picks the new code up within a tick. Every tick, and every second while it waits, it compares the installed package (its version and its files) with the one it loaded. Once the new one has stayed unchanged for a few seconds (an install still writing files is not switched to) and a fresh interpreter imports all of it, the supervisor logs `nohalt upgraded on disk: re-executing` and re-executes on it, adopting its agents. A new version that does not import is reported once in `logs/nohalt.log` and the loaded one keeps running. `nohalt restart` switches at once. The supervisor imports all of its own modules when it starts, so code it already runs never loads a file from a newer release (LIV-14).

`nohalt stop` is not a restart: it is a **drain**. Nothing new launches and the supervisor exits once no agent is running, which can take hours, and `keepalive` starts nothing while `STOP` exists. `nohalt resume` clears `STOP` and `PAUSE`. `nohalt pause` launches nothing new but keeps the supervisor running; `nohalt pause --for 2h` expires on its own (the supervisor clears `PAUSE` once the time passes and logs `PAUSE expired`). Without `--for` it lasts until `nohalt resume` or `pause --off`, and `nohalt status` shows how long it has been paused.

Durations accept `90`, `"90s"`, `"30m"`, `"4h"` or `"2d"`. Models are always written `<agent>:<model>`: `opencode:anthropic/claude-sonnet-4-5`, `claude-code:sonnet`, `command:anything`.

## [project]

| Key | Default | Meaning |
|---|---|---|
| `repo` | -- | `owner/name` on GitHub, `group[/subgroup]/name` on GitLab, or `local:/path/forge.json` for a file-backed forge (rehearsal, tests) |
| `forge` | `"github"` | `github` or `gitlab` |
| `forge_url` | `""` | GitLab only: the instance, e.g. `https://gitlab.example.com` (default `$GITLAB_HOST`, else `https://gitlab.com`). Also exported to agents as `GITLAB_HOST` |
| `token_env` | `"GITLAB_TOKEN"` | GitLab only: the variable holding a token with the `api` scope. If unset, nohalt uses the token `glab` is logged in with |
| `path` | `"."` | the main clone. Worktrees are cut from it |
| `base_branch` | `"main"` | branches start here. PRs target it. Agents may never push to it |
| `remote` | `"origin"` | |
| `state_dir` | `".nohalt"` | records, logs, locks, shims, agent data |
| `worktrees` | `<state_dir>/work` | where per-issue worktrees go (keep it out of a Frappe bench's `apps/`) |
| `shared_paths` | `[]` | untracked directories symlinked from the main clone into each worktree (e.g. `node_modules`, a venv). Installs that would write through the link are refused by the package-manager guard (SAFETY.md) |
| `branch_prefixes` | `{ bug = "fix" }` | label → branch prefix |
| `default_prefix` | `"feature"` | branches are `<prefix>/<issue>-<slug>`. An existing branch matching `*/<issue>-*` is reused |

## [queue]

| Key | Default | Meaning |
|---|---|---|
| `file` | `"queue.txt"` | `<issue>: <deps…>` per line, `#` comments. Re-read every tick |
| `blocked_label` | `"nohalt-blocked"` | put on issues that need a human. Remove it to retry, with counters reset |
| `held_labels` | `["needs-human"]` | issues carrying one of these are never worked |
| `tick` | `60` | seconds between passes |
| `poll_when_idle` | `600` | seconds between passes when nothing can move |

## [agents]

| Key | Default | Meaning |
|---|---|---|
| `max_parallel` | `2` | agent slots shared by every model not in a `pools` entry. Measure: on a small machine, more is slower |
| `pools` | `{}` | model prefix → its own cap, e.g. `{ "claude-code:" = 2, "opencode:anthropic/" = 4 }`. Override live with `<state>/caps.json` |
| `min_free_mb` | `2000` | do not launch below this much available memory |
| `phase_timeout` | `"4h"` | one agent run |
| `long_timeout` | `"8h"` | for issues carrying one of `long_labels` |
| `long_labels` | `[]` | |
| `max_attempts` | `2` | real attempts (exits with no PR and no close) before blocking |
| `provider_retries` | `5` | model-endpoint failures per issue before blocking. Never counted as attempts |
| `provider_backoff` | `"2m"` | multiplied by the retry number |
| `timeout_continues` | `3` | time limits that continue in the same worktree before counting as an attempt |
| `hung_after` | `"30m"` | no output and no command running: stop and restart |
| `revive_limit` | `2` | hung restarts before counting as an attempt |
| `revive_grace` | `"15m"` | wait before relaunching an agent that stopped without a PR (it may have asked a question) |
| `ci_rounds` | `3` | red or conflicting PRs handed back before blocking |
| `log_max_mb` | `20` | per-issue agent logs rotate past this |
| `title_prefix` | `"nohalt"` | agent session titles: `<prefix> #<issue>` |
| `copy_files` | `[]` | files copied into each agent's private data dir (e.g. an opencode `auth.json`) |
| `options` | `{}` | per-adapter options, below |
| `early_start` | `false` | start a dependant before its dependencies close, on a synthetic base built from their unmerged PRs. See [Early start](#early-start) |
| `early_start_on` | `"green"` | the state every open dependency's PR must be in: `green` (its checks are green on its current head) or `approved` (green, and past the `[review]` gate if that issue needs a review) |

### Early start

`14: 13` makes #14 wait until #13 is **closed**: a full release cycle per dependency hop, however long #13's PR has been green. With `early_start = true`, a dependant whose every dependency is closed **or** has a PR in the `early_start_on` state starts at once (SCH-10):

- **A synthetic base.** Its worktree is cut from the base branch, and each unmerged dependency's PR head is merged in, in dependency order (a dependency that needs another comes after it), through the `[integrate.mergers]` drivers. A conflict no driver settles means **no early start**: the issue waits as before, the log says which dependency conflicted in which files, and nohalt does not try those heads again until one of them moves. Only an issue's first launch is built this way.
- **The prompt** names the dependency branches included, with their PRs and heads, and says they are not merged yet: build on them, do not rewrite them, and expect the PR's diff to include their commits until they merge.
- **A dependency that moves** (a new push) is noted: the dependant's next launch is told to merge `origin/<dependency branch>` again. An agent that is running, or a PR already waiting, gets the note on its next launch; the refresh below brings in the final version either way.
- **Never before its dependencies.** A dependant's PR does not integrate until every dependency has merged. `train` batches it only when each open dependency goes into the same batch, ahead of it; `direct` merges it only after its dependencies have merged. A dependency whose PR is closed unmerged, or handed back red, leaves the dependant working but unable to integrate; the log says which.
- **Onto the real base.** Once every dependency has merged, nohalt merges the base into the dependant's branch (the `refresh_conflicts` path, also when the merge is clean), so its CI runs on the real base; real code conflicts are handed back. From then on it is an ordinary PR. With `integrate.mode = "none"` the agent is told to merge the base instead.

`nohalt status` lists `#14 early on #13` for each early dependant, and each waiting issue with what it waits for: `#14 waiting for #13 (no PR), #12 (PR red)` (also `PR pending`, `PR green` while it waits for approval, `PR closed unmerged`).

```toml
[agents.options.opencode]
binary = "opencode"
agent = "build"            # opencode --agent
extra_args = []

[agents.options.claude-code]
binary = "claude"
disallowed_tools = ["Task", "CronCreate", "..."]   # default: sub-agents, schedulers, remote triggers, worktree tools
mcp = false                # true keeps your MCP servers available to agents

[agents.options.command]   # any CLI: placeholders {model} {prompt} {title} {data_dir}
argv = ["my-agent", "--model", "{model}", "{prompt}"]
resume_args = ["--continue"]
probe_argv = ["my-agent", "--model", "{model}", "--ping"]
```

## [models]

| Key | Default | Meaning |
|---|---|---|
| `default` | `opencode:anthropic/claude-sonnet-4-5` | |
| `fallbacks` | `[]` | tried in order when an unpinned issue's model does not answer a probe. A fallback whose pool is full is skipped |
| `premium` | `[]` | models that run only on issues approved with `nohalt route <n> <model> --approve` |
| `premium_fallback` | `""` | what a non-approved request for a premium model gets (default: `default`) |
| `probe` | `true` | ask the model for "OK" before launching. A dead endpoint otherwise kills every agent in its first second |
| `probe_ttl` | `"5m"` | probe results are cached this long |
| `[[models.route]]` | `[]` | `{ labels = [...], model = "..." }` or `{ title = "regex", model = "..." }`. The first match wins |
| `overflow_after` | `""` (off) | an unpinned issue that could start but has waited this long for a slot -- its model's pool (or `max_parallel`) is full, or its model does not answer and no fallback has a free slot -- starts on the first `overflow_to` model with a free slot that answers a probe, e.g. `"10m"` |
| `overflow_to` | `fallbacks` minus `premium` | models allowed to take overflow, in order. A premium model is used only for issues approved for it |
| `overflow_reserve` | `{}` | `[agents] pools` prefix → slots overflow may not take, e.g. `{ "claude-code:" = 2 }` keeps the last two Claude slots for work routed there |
| `down_after` | `3` | a model is **down** when agents on this many different issues hit endpoint errors on it within `down_window`, or when this many probes in a row fail. `0`: never |
| `down_window` | `"10m"` | |
| `down_probe` | `"2m"` | a down model is probed again after this, then after twice as long each time it still fails ... |
| `down_probe_max` | `"30m"` | ... up to this. With `probe = false` a down model is simply let back after `down_probe`, and goes down again on the next storm |
| `down_block_after` | `"6h"` | an issue whose endpoint failures went uncounted is blocked only once its model **and every fallback** (only its model, if pinned) have been down this long |
| `usage_cooldown` | `"1h"` | how long a pool cools down after a usage signal that reports no reset time |

### Down models and usage cool-downs

Some trouble belongs to a model, not to an issue (MOD-14, MOD-15):

- **Down.** A storm (agents on `down_after` different issues hitting endpoint errors on one model within `down_window`; a restarted model server kills all of its agents at once) or `down_after` failed probes in a row mark the model down. The storm's own failures are refunded, and later failures on a down model are never counted toward an issue's `provider_retries`. New launches routed to it go at once to the first fallback, then overflow target, that has a free slot and answers. With none, they wait. It is probed again at `down_probe` (doubling up to `down_probe_max`). When it answers, it is up again (`model … is UP again … after 2h 10m down` in the log). An issue is blocked for the endpoint only after `down_block_after`, as above. One issue failing on its own is still that issue's `provider_retries` (MOD-01).
- **Cooling down.** An agent's usage line (claude-code: `rate_limit five_hour rejected`, or `allowed_warning` when it is close) cools down the model's **pool** until the reset time the line reports, else for `usage_cooldown`. The pool is its `[agents] pools` prefix, or else the model itself, so give a subscription's models one pool (`"claude-code:" = 4`) to cool them together. Running agents' logs are read every tick, so a warning steers new work away before the limit hits. While a pool cools down, new launches routed to it go to a fallback or overflow target at once, never premium unless approved, and never using `overflow_reserve` slots. Pinned issues wait. Running agents are left alone. An agent that **stopped** on the limit waits for its own model's reset and resumes its session there (MOD-07).
- **Reviews during a warning** (`[review] during_cooldown = "warning"`, MOD-16). A warning can cool a pool down for days (a 7-day window nearly used). A review takes minutes and is the quality gate, and a smaller fallback often cannot finish one, so with this setting a reviewer may still start on a pool that only a warning cools down. A limit is remembered apart from the warning: once an agent or reviewer on the pool hits the limit, reviews divert too until that limit's reset, even if the warning's cool-down lasts longer. `nohalt status` lists each review let through (`reviewed on a pool cooling down for a usage warning`), and the issue's history says so.
- `nohalt status` shows `model DOWN: … since …; next probe in …` and `pool cooling down: claude-code: five_hour usage limit (#12) until 05:30 (in 2h 10m; reset)`. Diverted launches are marked `^` with the reason (`capacity: claude-code: cooling down (five_hour) until … → opencode:local/coder`), like overflow. State is `<state>/capacity.json`.

The usage patterns are per agent, in `[agents.options.<agent>]`: `usage_limit` and `usage_warning`, lists of regular expressions matched per line of the agent's output. A named group `window` names the usage window, and `resets` gives the reset time in epoch seconds. claude-code's defaults read the `[agent] rate_limit <window> <status> resets=<epoch> used=<fraction>` lines nohalt prints from Claude's stream. To react to a high `used=` on plain `allowed` events too:

```toml
[agents.options.claude-code]
usage_warning = ['^\[agent\] rate_limit (?P<window>\S+) allowed\S* .*?resets=(?P<resets>\d+) used=0\.(8|9)']
```

opencode has no usage events of its own, so it has no defaults. Name your provider's limit text there if it prints one.

Pins (`nohalt route <issue> <model>`) beat rules and are never failed over or overflowed: a pinned issue waits for its model. Overflow is not a pin: the issue's next launch (a resume, a retry, a hand-back) is routed as usual, and starts a fresh session if that lands on a different model. Running agents are never moved. Overflowed launches are marked `^` in `nohalt status`, with the reason (`overflow: opencode:local/ pool full (8/8) for 12m → claude-code:sonnet`), which is also in the issue's history and the log.

## [budget]

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `per_issue` | `15.0` | USD. Over it, the issue moves to `fallback_model` |
| `total` | `100.0` | USD across all paid issues. Once reached, every paid issue moves |
| `providers` | `["anthropic"]` | model id prefixes that are billed per token |
| `fallback_model` | `""` | where over-budget issues go. Empty: stop and block them |
| `rates` | Sonnet list prices | USD per million tokens: `input`, `output`, `cache_read`, `cache_write`. **Calibrate against your console**: estimates are routinely off by a factor until measured |

Spend is estimated from each opencode agent's own session database, keeping the high-water mark per issue (pruning session data never lowers the total).

## [verify]

| Key | Default | Meaning |
|---|---|---|
| `slots` | `["default"]` | names, or `{ name, env = {...} }`. One verification per slot at a time |
| `placeholder` | `""` | a token in agents' commands replaced by the slot's `SITE` (the frappe stack sets it) |
| `refuse` | `[]` | substrings a verify command may not contain. Refusals are logged |
| `up` / `reset` / `env` | `""` | shell hooks (generic stack), see [STACKS.md](STACKS.md) |
| `pythonpath` | `[]` | worktree subdirectories put first on `PYTHONPATH`, e.g. `["src/api"]` for a `src/` layout |
| `pythonpath_worktree` | `false` | put the worktree first on `PYTHONPATH` (the frappe stack sets it) |
| `clean_pycache` | `true` | delete stale bytecode in the worktree before running |

## [integrate]

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"none"` | `none` (a human merges), `direct`, `train`. Agents never merge in any mode |
| `required_checks` | `[]` | check-run names (GitHub) or job names in the newest pipeline (GitLab) that must pass. Empty: every check must pass, and at least one must exist. On GitLab, a job with `allow_failure` never makes a commit red, and a manual job counts as skipped. `nohalt doctor` compares this list with the checks that ran on recent commits and warns about names never seen and checks that count for nothing. |
| `ignore_checks` | `[]` | **pull requests only**: check names that never count towards a PR's green or red, e.g. a slow suite your pipeline cancels on PRs. On the base branch after a merge they count again: that is the suite's only gate |
| `base_ignore_checks` | `[]` | checks that never count on the base branch after a merge. After a merge, nohalt counts **every** check of the pipeline the merge triggered except these (GitHub-managed jobs such as Dependabot updates never count) (`required_checks` and `ignore_checks` are PR settings) |
| `base_cancelled_limit` | `3` | a cancelled base run is no verdict, neither green nor red: nohalt re-runs it, up to this many in a row. A job that hits its own time limit is reported as cancelled too; set `1` to stop re-running jobs that will only time out again |
| `base_no_verdict` | `"continue"` | what happens when the base pipeline still has no verdict after `base_cancelled_limit`: `continue` files one issue at the front of the queue, deploys if the merge asked for a deploy, and keeps merging (no verdict is not a red); `hold` writes `BASE_NO_VERDICT` and stops merging until a human deletes it |
| `red_base` | `"fix_only"` | while a fix-forward issue is open: `fix_only` merges only the fix; `with_fix` (train mode) waits until the fix's PR is green, then lets that batch carry the fix **and every other green PR**. Trades a strictly green base between a red and its fix for throughput, worth it when the base suite is long and reds are usually test isolation, not product. Direct mode always behaves as `fix_only` |
| `merge_method` | `"squash"` | for `direct` (release PRs always merge with a merge commit). On GitLab, `squash` squashes; otherwise the project's merge method applies |
| `ci_workflow` / `cd_workflow` | `""` | runs watched on the base branch after a merge: GitHub workflow names, or on GitLab a pipeline's `workflow:name` (or `"pipeline"` for any pipeline on the commit) |
| `async_workflows` | `[]` | workflows (named as for `ci_workflow`) that are **watched on the base branch but never gate**: a slow full suite, say. nohalt reads the newest completed run of each every tick and never waits for one. Red with no open issue for that workflow: one issue at the front of the queue, with the run, its failing jobs and tests. Red again: a comment on that issue (what newly fails, what no longer does), never a second issue. Green: a comment, and the issue is closed (an unmerged PR of its own is left open, and the comment says so). Their runs' jobs never count in the base gate on GitHub; on GitLab, set `ci_workflow` so the gate watches only the pipeline that should gate |
| `async_label` | `"async-red"` | label on the issue an async workflow's red run opens |
| `cd_timeout` | `"90m"` | a pipeline that does not finish in time counts as red |
| `base_pipeline_grace` | `"10m"` | if **no** pipeline appears on the base branch's new head within this time, there is nothing to watch, and merging continues. Many integration branches run no CI after a merge |
| `deploy` | `""` | shell command after a green pipeline (`direct`, when no CD workflow deploys) or a merged release (`train`). A failure writes `DEPLOY_BLOCKED` |
| `post_deploy_check` | `""` | shell command run after a successful `deploy`, or when `cd_workflow` goes green (the CD job is the deploy then). It checks the live app, e.g. that the main pages answer. See [Post-deploy check](#post-deploy-check) |
| `post_deploy_retries` | `2` | further tries of a failing `post_deploy_check` before it counts as failed (warm-up: workers take a while to come back) |
| `post_deploy_delay` | `"30s"` | time between those tries. A retry waits between ticks; nothing merges meanwhile |
| `live_version` | `""` | shell command that prints what production runs: a version, a tag or a commit SHA (its last non-empty line is read). When set, issues are closed only once that is live. See [Shipped means live](#shipped-means-live) |
| `live_timeout` | `""` | how long a merged issue may wait to be live before nohalt warns. Empty: `cd_timeout` |
| `fix_forward` | `true` | a red pipeline opens an issue at the front of the queue and stops merging until it closes |
| `cancel_superseded` | `false` | cancel queued or running CI for commits no open PR or the base points at |
| `network_errors` | DNS/reset patterns | failed-log substrings that mark a red as the network's, not the code's |
| `mergers` | `{}` | glob → driver, see [STACKS.md](STACKS.md#merge-drivers-any-stack). Paths in `[integrate.train] version_files` that no glob covers get one implicitly: `package_json` for JSON, `version_lines` for anything else |
| `refresh_conflicts` | `true` | `direct` and `train`: before handing a PR back for conflicting with the base, nohalt merges the base into the PR branch itself, in a scratch tree, when the merge drivers settle every conflicted file. It then runs `after_version` (if set), commits the merge (`merge: <base> into <branch> (nohalt refresh)`) and any regenerated files, and pushes, fast-forward only. Any file left in conflict, or a failed regenerate or push, means a hand-back as before. A branch whose agent is running, or whose PR is `in_review`, is never touched. In `train` mode a green PR whose conflicts the drivers settle is not refreshed at all: the release batch settles them the same way. An approval carries over a refresh that changed, beyond what it took from the base verbatim, only paths with a merge driver or `after_version` outputs (INT-16); CI must be green on the new head again. Refreshes don't count as `ci_rounds` |

### Post-deploy check

A deploy that exits 0 has finished, but the app may not work. `post_deploy_check` runs after every successful deploy (DEP-14), in the clone, with:

| Variable | Value |
|---|---|
| `NOHALT_VERSION` | the release's version without `tag_prefix` (`1.4.2`); in `direct` mode, the first 12 characters of the merge commit |
| `NOHALT_SHA` | the base branch's head after the merge |
| `NOHALT_TAG` | the release tag (`v1.4.2`); empty in `direct` mode |
| `NOHALT_ISSUES` | the issues the merge carried, space-separated |

Exit 0 is a pass. Anything else is tried again `post_deploy_retries` times, `post_deploy_delay` apart. If it still fails:

- `DEPLOY_BLOCKED` is written, with the check's exit code and last output (`nohalt status` shows it). Merging and deploys stop, as for a failed `deploy`.
- One issue opens at the front of the queue, "Post-deploy check failed on v1.4.2: <the output's last line>", with the output in its body. A further failure while that issue is still worked on is a comment on it, not a second issue. `fix_forward = false` opens nothing.
- That issue is a fix-forward. Its own PR may merge (with `red_base = "with_fix"` in train mode, it carries every green PR), and outside the release windows it releases as a hotfix.
- Once a later deploy passes the check, `DEPLOY_BLOCKED` is cleared by itself. A human may also delete it. A `deploy` command that fails outright still needs a human, as before.

```toml
[integrate]
deploy = "./scripts/deploy.sh"
post_deploy_check = "./scripts/smoke.sh"   # curl the login page, the dashboard, an API call; exit non-zero on failure
post_deploy_retries = 3
post_deploy_delay = "20s"
```

`nohalt doctor` warns when `deploy` (or `cd_workflow`) is set without a `post_deploy_check`.

### Shipped means live

By default, an issue is done when its PR (or its release) merges. With `live_version` set, merged is not done (DEP-15):

- After the merge, nohalt does **not** close the issue. Its record becomes `released`, and the issue gets a comment: "released in v1.4.2, not yet live". A released issue is never relaunched. Dependants wait for it, since they wait for closed issues.
- Every tick (`direct` and `train` modes), nohalt runs `live_version` and resolves what it prints in the clone: a commit, a tag, or a version that is a tag with `tag_prefix` (`1.4.2` → `v1.4.2`). Tags are fetched when the value is new to the clone.
- When the merge commit is an ancestor of the live commit (`git merge-base --is-ancestor`), each issue is closed with a comment "live in 1.4.2 (verified)". If the forge already closed it (a closing keyword on the default branch), it gets the comment only.
- After `live_timeout` (default `cd_timeout`) without that, nohalt logs a warning once and keeps waiting. The issues stay open; close them by hand if the release went live some other way.
- `nohalt status` lists them: `released, awaiting live: #12 #14 in v1.4.2 (for 25m)`, with `OVERDUE` past the timeout, and the value `live_version` last printed.

```toml
[integrate]
live_version = "curl -fsS https://app.example.com/api/version | jq -r .version"
live_timeout = "2h"
```

Not covered: restoring a neutralised copy of production after a release and sweeping it with real-backend checks (see LESSONS, Open problems).

An agent may finish with **nothing to change**: the line `NOHALT-RESULT: no-change -- <reason>` in its output (the built-in prompt tells it how). On an async workflow's issue that moves the issue to `awaiting_green`: no attempt is spent and no agent is launched, the next green run closes it, and a red run with failures the agent has not seen sends an agent back with that run. On any other issue it is blocked for a human with the reason "agent says no change needed: <reason>".

Slow suite example: releases wait only for the build, the full suite is watched without holding anything.

```toml
[integrate]
mode = "train"
ci_workflow = "build"          # gates every release
async_workflows = ["Tests"]    # ~2 h: watched, one issue while it is red, closed when it is green
```

```toml
[integrate.train]
branch_prefix = "release/"
version_files = [{ path = "pkg/__init__.py", pattern = '__version__ = "([^"]+)"' }]   # group 1 is replaced
tag_prefix = "v"
after_version = "python scripts/regenerate_reference.py"   # run in the release tree after stamping, and after a refresh
red_retries = 1
batch_pause_file = "BATCH_PAUSE"   # touch <state>/BATCH_PAUSE to hold new batches (expires after 1 h)

# Release windows: one release a day before staff start; hotfixes any time.
windows = ["05:00-06:00"]
timezone = "America/Curacao"
days = ["mon", "tue", "wed", "thu", "fri"]
per_window = 1
hotfix_labels = ["hotfix"]
hotfix_priority = true

approved_first = true              # with [review]: approved PRs go first, and may join an open batch
```

`<state>/priority` and `<state>/hold` (one issue per line) put issues first in, or keep them out of, the next batch.

### Approved PRs first

A PR approved just after a batch opened used to wait for the next one, and the siblings in that batch -- often editing the same files -- could turn its clean merge into a code conflict: handed back, CI again, a new review, and the next batch missed too (INT-17). With `[review]` enabled, `approved_first` (default `true`) changes that:

- **The next batch** takes, in order: `<state>/priority`, then issues whose PR is approved **for its current head**, in approval order, then every other ready issue in queue order. An approval of an older head does not count; an approval carried over a nohalt refresh (INT-16) does, and keeps its time.
- **The open batch.** While a release this train opened has no check beyond `queued` on its head (its CI has not started: no runner has picked it up), an approved PR that merges cleanly with the batch, using the same merge drivers, is merged into the release branch and pushed (no force; the version files are stamped with the batch's version again and `after_version` re-runs). It is carried, closed and parked like the batch's other PRs, and the release PR gets a comment naming it. One that does not merge cleanly is not handed back: it stays `ready` and goes first in the next batch (tried once per head). Once any check has started, nothing joins.
- **Windows** are unchanged: a hotfix-only release takes approved hotfixes only, and an approval never makes an issue a hotfix.
- **`direct` mode** merges approved PRs (approval order) before the other ready PRs (queue order). The key lives in `[integrate.train]` but applies there too.
- `approved_first = false`, or `[review]` off, gives the old order and nothing joins an open batch.

### Release windows

By default the train opens a batch whenever no release is open, so with a `deploy` command every batch deploys, all day. Release windows restrict when a **normal** batch may open (DEP-13):

| Key | Default | Meaning |
|---|---|---|
| `windows` | `[]` | daily `"HH:MM-HH:MM"` ranges (24-hour clock, end exclusive) in which a normal batch may **open**. A range whose end is at or before its start crosses midnight (`"23:00-01:00"`). Empty: no schedule, a batch opens whenever one can (the old behaviour) |
| `timezone` | `""` | IANA name the windows are read in, e.g. `"America/Curacao"`. Empty: the system's local time |
| `days` | `[]` | weekdays the windows apply on: `mon` `tue` `wed` `thu` `fri` `sat` `sun`. A window crossing midnight belongs to the day it starts. Empty: every day |
| `per_window` | `1` | normal releases **merged** per window instance. After that, no further normal batch opens in the same window. A batch that fails (red, closed for conflicts, its PRs parked) does not count, so it is retried in the same window. `0`: unlimited inside the window |
| `hotfix_labels` | `["hotfix"]` | issues carrying any of these labels (case-insensitive) are hotfixes |
| `hotfix_priority` | `true` | issues listed in `<state>/priority` are hotfixes too |

- **Outside a window**, or once the window's quota is used, no normal batch opens. If any ready issue is a hotfix, a release opens carrying **only the hotfixes**; the other green PRs wait for the next window. The log says why nothing opened (`no normal batch: outside release window (next 05:00-06:00 America/Curacao, in 8h 12m); 9 ready issue(s) wait; 0 hotfix(es)`), once per change, and `nohalt status` shows the next window and the hotfixes waiting.
- **The fix-forward issue for a red base is always a hotfix**: a red production base is fixable at once. With `red_base = "with_fix"`, outside a window the fix goes with the other hotfixes only, not with every green PR; inside one it carries them as usual.
- **A release already open always proceeds**, window or not: CI, re-runs, merge, tag, `deploy` and the base watch. Windows only decide when a batch is *opened*, so a batch opened at 05:55 may deploy at 06:20.
- A batch of hotfixes only (inside a window too) does not use the window's quota.
- **`nohalt release-now`** asks for one unscheduled release: once no release is open and the base gate is open, the next tick opens a normal batch of everything ready, ignoring windows and quota. The request (`<state>/RELEASE_NOW`) is used once and does not count against a window. With nothing ready it is dropped; unused after 12 h it expires; `nohalt release-now --off` withdraws it. `BATCH_PAUSE`, `hold`, a red base and `DEPLOY_BLOCKED` still apply.

## [stack]

`name = "generic"` or `"frappe"`. Other keys are the stack's options (frappe: `bench`, `app`, `sites`, `full_suite_guard`, `mergers`). See [STACKS.md](STACKS.md).

## [prompt]

| Key | Default | Meaning |
|---|---|---|
| `inline_issue` | `false` | put the issue's text and comments in the prompt, so agents don't need a working forge CLI to read it |
| `template` | `"builtin:core"` | the core prompt: `builtin:<name>` or a file |
| `extra` | `[]` | your project's own prompt files, appended in order |
| `vars` | `{}` | extra `{{PLACEHOLDERS}}`. **Never put secrets here**: prompts end up in logs |

Placeholders available: `{{ISSUE}} {{TITLE}} {{REPO}} {{BRANCH}} {{WORKTREE}} {{BASE}} {{VERIFY}}`, `{{LANE}} {{LANE_NAME}}` (empty without a lane), `{{SITE}}` when `verify.placeholder` is set, plus `vars`. An undefined placeholder is an error, not an empty string. Run `nohalt prompt <issue>` to see exactly what an agent will be told.

### Prompt lanes

One composed prompt per project is one set of rules for every issue. A **lane** gives some issues different rules -- say, the children of a large feature tracker get "full delivery" while everything else keeps a fast lane -- without one shared prompt full of "if your issue says…, ignore the section above" (PRM-10).

| Key | Default | Meaning |
|---|---|---|
| `name` | -- | required, unique. Kept on the issue record and shown by `nohalt status` |
| `labels` | `[]` | selects issues carrying any of these labels (case-insensitive) |
| `title` | `""` | selects issues whose title matches this regex (case-insensitive) |
| `body` | `""` | selects issues whose description matches this regex (case-insensitive, `^`/`$` per line), e.g. `"Part of #4194"` |
| `tracker` | `[]` | an issue number, or a list: selects that tracker's children, the issues whose description says `Part of #N` |
| `extra` | `[]` | prompt files appended after `[prompt] extra` |
| `replace` | `[]` | `[prompt] extra` entries this lane leaves out, written exactly as in `extra` |
| `verify_scope` | `""` | free text for `{{LANE}}`: how this lane verifies. If no prompt file uses `{{LANE}}`, it is appended as a section of its own |

A lane needs at least one selector; any one matching selects it. Lanes are tried in order: **the first match wins**, and an issue no lane matches has no lane (the project's prompt as it is).

```toml
[prompt]
extra = ["prompts/house-rules.md", "prompts/fast-lane.md"]

[[prompt.lane]]
name = "full-delivery"
tracker = 4194                       # every "Part of #4194" issue
labels = ["epic"]
extra = ["prompts/full-delivery.md"]
replace = ["prompts/fast-lane.md"]   # no fast lane for these
verify_scope = "Run the whole test module you touched, and attach a screenshot for every acceptance item."

[[prompt.lane]]
name = "docs"
title = "^docs"
verify_scope = "Build the docs; no test run is needed."
```

- **Chosen once, kept.** The lane is chosen at an issue's first launch and kept on its record (`issues/<n>.json`: `lane`, plus a digest of the lanes it was chosen under). Every relaunch -- a resume, a hand-back, a retry, a fresh session -- uses the same lane, even if the issue's labels or text change.
- **A changed configuration chooses again.** When the `[[prompt.lane]]` tables change, each issue's lane is chosen again at its next launch; a different answer is logged (`#12: prompt lane changed from (none) to full-delivery`) and recorded in the issue's history. A resumed session, which was briefed in its old lane, is given the new instructions in full. A running agent is never touched.
- **The reviewer** (`[review]`) is given the same lane: its files, and the issue's `verify_scope`, are among the rules it checks the PR against.
- `nohalt status` lists each issue's lane; `nohalt prompt <issue>` prints the prompt in the lane the next launch would use, and says which (on stderr).

## [review]

Green CI says the tests pass, not that the issue is done. With `[review]` enabled, a selected issue whose PR is green goes to **`in_review`** instead of `ready`: a reviewer agent checks the PR against the issue in a detached checkout of the PR head, and only its approval makes the issue `ready` for the integrator.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `all` | `false` | every issue needs review |
| `labels` | `[]` | issues carrying any of these labels need review |
| `title` | `""` | a regex on the title (case-insensitive): matching issues need review |
| `model` | `[models] default` | the reviewer, `<agent>:<model>`, for authors `models` does not map. It may differ from the author's: a local model reviewing Claude's work, or Claude reviewing a local model's. A reviewer takes a slot in its model's pool (`[agents] pools`, else `max_parallel`) like any agent |
| `models` | `{}` | author model prefix → reviewer, e.g. `{ "opencode:" = "claude-code:sonnet", "claude-code:" = "opencode:local/coder" }`: the reviewer is chosen from the model that wrote the PR (the issue's last launch), and the longest matching prefix wins. Then `model`, then `fallbacks` |
| `distinct` | `true` | never review a PR with the exact model that wrote it: that model is skipped and the next one that differs is used. If none differs, the issue is blocked for a human with a message that says so (`nohalt doctor` warns beforehand). `false` allows it |
| `fallbacks` | `[]` | reviewer models tried in order when the ones before have no free slot or do not answer a probe |
| `max_rounds` | `2` | "changes" verdicts handed back to the author. One more, and the issue is blocked: "review: changes still requested after N round(s)" |
| `attempts` | `3` | reviews that ended without a verdict (time limit, hang, no verdict line) before the issue goes to a human. Never an approval |
| `timeout` | `"1h"` | a reviewer running longer is stopped (and that review counts against `attempts`) |
| `prompt` | `[]` | your own reviewer prompt files, appended to the built-in one (`src/nohalt/prompts/review.md`) |
| `during_cooldown` | `"none"` | `"warning"`: a reviewer may start on a pool that is cooling down because of a usage **warning** (MOD-16). Authoring launches still divert. A pool cooling down for a hard limit (a `usage_limit` match, such as claude-code's `rejected` line) diverts reviews too, until that limit's reset. `"none"`: reviews divert like authoring |

```toml
[review]
enabled = true
labels = ["feature", "security"]
title = "^(feat|epic)"
model = "opencode:local/coder"      # reviews work authored on claude-code:sonnet
fallbacks = ["claude-code:sonnet"]
max_rounds = 2
prompt = ["prompts/review-rules.md"]
```

Two model families checking each other (AGT-21): Claude reviews what the self-hosted model wrote, and the other way round. A PR whose author overflowed or failed over is reviewed by the mapping for the model it ran on last.

```toml
[review]
enabled = true
all = true
models = { "opencode:" = "claude-code:sonnet", "claude-code:" = "opencode:local/coder" }
model = "claude-code:sonnet"          # authors no prefix matches
fallbacks = ["opencode:local/coder"]  # distinct = true skips whichever of these wrote the PR
```

The reviewer's prompt is the built-in review prompt, your `prompt` files, then the rules your authors were given (the stack's addendum and `[prompt] extra`), so "every new string is translated" or "every endpoint checks roles" are checked as well as stated. The issue's text and comments are inlined, so is the PR description as it read at launch, and on a re-review so is the previous review.

How it works:

- **Verdict.** The reviewer writes its review to a file nohalt names (or prints it); its last lines are `NOHALT-REVIEW: approve`, or `NOHALT-REVIEW: changes` and a numbered list. nohalt posts the review on the PR -- as a formal GitHub review when the token may (GitHub refuses one on a PR the same account opened), else as a comment.
- **approve** → `ready`. The approval covers that head SHA only: a later push (by anyone) sends the PR back through CI and then through a new review of the **whole** PR. The one exception is a nohalt refresh (`refresh_conflicts`) that changed only bookkeeping: the approval is carried to the refreshed head, said so on the PR and in `nohalt status`, and the new head still needs green CI (INT-16).
- **changes** → handed back to the author agent, resuming its session with the review as its note (as a red CI is). nohalt records the head **and a hash of the PR description** each review read. If the author pushes nothing but the description now differs (a review often asks for an Acceptance section, evidence, screenshot paths), the reviewer runs again on the same head and no round is counted. The author may end with `NOHALT-RESULT: updated-description -- <what>`; that is a hint for the log and the hand-back, and the hash decides. If neither the head nor the description changed, the same review stands and another round is counted without running a reviewer. A description edit after an approval does not void it: the approval covers the code, which has not changed (AGT-22).
- **Read-only.** The reviewer runs with `NOHALT_READONLY=1`: the `git` shim refuses every push and the `gh`/`glab` shims allow reads only. As with the merge refusal, that is a speed bump; the binding rule is that a verdict counts only if the PR head is still the commit reviewed.
- **Failures.** A reviewer that hits an endpoint error is retried with backoff (`[agents] provider_retries`, not counted); a usage limit waits; a time limit, a hang or a missing verdict is retried up to `attempts`. Never an approval.
- **Restarts.** A reviewer runs in its own session; a restarted supervisor adopts it from the issue record. `nohalt stop --now` stops reviewers too; their reviews start again from scratch.
- `nohalt status` shows `in_review` with the reviewer's model and round, and each issue's last verdict. The review posted on the PR names the reviewer's model and the author's.

## Example: self-hosted GitLab

Work lands on an integration branch, and humans promote it to the default branch, which deploys. CI on merge requests is a short validation job, and the instance itself doesn't require pipelines to pass.

```toml
[project]
repo = "team/app"                       # group[/subgroup]/name
forge = "gitlab"
forge_url = "https://gitlab.example.com"
token_env = "NOHALT_GITLAB_TOKEN"        # a project access token (Developer, api), not a person's token
base_branch = "develop"                  # NOT the default branch that deploys to production

[queue]
blocked_label = "blocked"                # reuse the label humans already use
held_labels = ["in-progress", "Draft"]   # matched case-insensitively

[integrate]
mode = "direct"
required_checks = ["validate"]           # nohalt enforces this itself, even if GitLab doesn't
merge_method = "merge"                   # match the project's merge method
base_pipeline_grace = "5m"               # develop runs no pipeline after a merge: don't wait for one

[prompt]
inline_issue = true                      # agents read the issue even if glab can't reach the host
```

If GitLab is configured to close issues only for merges into the default branch, nohalt closes them itself after a merge into `develop`.
