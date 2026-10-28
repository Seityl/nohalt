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

`nohalt stop` is not a restart: it is a **drain**. Nothing new launches and the supervisor exits once no agent is running, which can take hours, and `keepalive` starts nothing while `STOP` exists. `nohalt resume` clears `STOP` and `PAUSE`.

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
| `fix_forward` | `true` | a red pipeline opens an issue at the front of the queue and stops merging until it closes |
| `cancel_superseded` | `false` | cancel queued or running CI for commits no open PR or the base points at |
| `network_errors` | DNS/reset patterns | failed-log substrings that mark a red as the network's, not the code's |
| `mergers` | `{}` | glob → driver, see [STACKS.md](STACKS.md#merge-drivers-any-stack). Paths in `[integrate.train] version_files` that no glob covers get one implicitly: `package_json` for JSON, `version_lines` for anything else |
| `refresh_conflicts` | `true` | `direct` and `train`: before handing a PR back for conflicting with the base, nohalt merges the base into the PR branch itself, in a scratch tree, when the merge drivers settle every conflicted file. It then runs `after_version` (if set), commits the merge (`merge: <base> into <branch> (nohalt refresh)`) and any regenerated files, and pushes, fast-forward only. Any file left in conflict, or a failed regenerate or push, means a hand-back as before. A branch whose agent is running is never touched. Refreshes don't count as `ci_rounds` |

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
```

`<state>/priority` and `<state>/hold` (one issue per line) put issues first in, or keep them out of, the next batch.

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

The reviewer's prompt is the built-in review prompt, your `prompt` files, then the rules your authors were given (the stack's addendum and `[prompt] extra`), so "every new string is translated" or "every endpoint checks roles" are checked as well as stated. The issue's text and comments are inlined, and on a re-review so is the previous review.

How it works:

- **Verdict.** The reviewer writes its review to a file nohalt names (or prints it); its last lines are `NOHALT-REVIEW: approve`, or `NOHALT-REVIEW: changes` and a numbered list. nohalt posts the review on the PR -- as a formal GitHub review when the token may (GitHub refuses one on a PR the same account opened), else as a comment.
- **approve** → `ready`. The approval covers that head SHA only: a later push (by anyone) sends the PR back through CI and then through a new review of the **whole** PR.
- **changes** → handed back to the author agent, resuming its session with the review as its note (as a red CI is). If the author stops without pushing, the same review stands and another round is counted without running a reviewer.
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
