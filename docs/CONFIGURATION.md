# Configuration

One `nohalt.toml` per project. `nohalt init [--stack frappe]` writes a starter file, and `nohalt doctor` checks it. nohalt looks for `nohalt.toml` in the current directory and its parents, or takes `--config PATH`. Relative paths are relative to the file.

**Unknown keys are errors.** A typo that silently falls back to a default is how a guard quietly stops guarding.

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
| `shared_paths` | `[]` | untracked directories symlinked from the main clone into each worktree (e.g. `node_modules`) |
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
| `fallbacks` | `[]` | tried in order when an unpinned issue's model does not answer a probe |
| `premium` | `[]` | models that run only on issues approved with `nohalt route <n> <model> --approve` |
| `premium_fallback` | `""` | what a non-approved request for a premium model gets (default: `default`) |
| `probe` | `true` | ask the model for "OK" before launching. A dead endpoint otherwise kills every agent in its first second |
| `probe_ttl` | `"5m"` | probe results are cached this long |
| `[[models.route]]` | `[]` | `{ labels = [...], model = "..." }` or `{ title = "regex", model = "..." }`. The first match wins |

Pins (`nohalt route <issue> <model>`) beat rules and are never failed over: a pinned issue waits for its model.

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
| `pythonpath_worktree` | `false` | put the worktree first on `PYTHONPATH` (the frappe stack sets it) |
| `clean_pycache` | `true` | delete stale bytecode in the worktree before running |

## [integrate]

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"none"` | `none` (a human merges), `direct`, `train`. Agents never merge in any mode |
| `required_checks` | `[]` | check-run names (GitHub) or job names in the newest pipeline (GitLab) that must pass. Empty: every check must pass, and at least one must exist. On GitLab, a job with `allow_failure` never makes a commit red, and a manual job counts as skipped |
| `merge_method` | `"squash"` | for `direct` (release PRs always merge with a merge commit). On GitLab, `squash` squashes; otherwise the project's merge method applies |
| `ci_workflow` / `cd_workflow` | `""` | runs watched on the base branch after a merge: GitHub workflow names, or on GitLab a pipeline's `workflow:name` (or `"pipeline"` for any pipeline on the commit) |
| `cd_timeout` | `"90m"` | a pipeline that does not finish in time counts as red |
| `base_pipeline_grace` | `"10m"` | if **no** pipeline appears on the base branch's new head within this time, there is nothing to watch, and merging continues. Many integration branches run no CI after a merge |
| `deploy` | `""` | shell command after a green pipeline (`direct`, when no CD workflow deploys) or a merged release (`train`). A failure writes `DEPLOY_BLOCKED` |
| `fix_forward` | `true` | a red pipeline opens an issue at the front of the queue and stops merging until it closes |
| `cancel_superseded` | `false` | cancel queued or running CI for commits no open PR or the base points at |
| `network_errors` | DNS/reset patterns | failed-log substrings that mark a red as the network's, not the code's |
| `mergers` | `{}` | glob → driver, see [STACKS.md](STACKS.md#merge-drivers-any-stack) |

```toml
[integrate.train]
branch_prefix = "release/"
version_files = [{ path = "pkg/__init__.py", pattern = '__version__ = "([^"]+)"' }]   # group 1 is replaced
tag_prefix = "v"
after_version = "python scripts/regenerate_reference.py"   # run in the release tree after stamping
red_retries = 1
batch_pause_file = "BATCH_PAUSE"   # touch <state>/BATCH_PAUSE to hold new batches (expires after 1 h)
```

`<state>/priority` and `<state>/hold` (one issue per line) put issues first in, or keep them out of, the next batch.

## [stack]

`name = "generic"` or `"frappe"`. Other keys are the stack's options (frappe: `bench`, `app`, `sites`, `full_suite_guard`, `mergers`). See [STACKS.md](STACKS.md).

## [prompt]

| Key | Default | Meaning |
|---|---|---|
| `inline_issue` | `false` | put the issue's text and comments in the prompt, so agents don't need a working forge CLI to read it |
| `template` | `"builtin:core"` | the core prompt: `builtin:<name>` or a file |
| `extra` | `[]` | your project's own prompt files, appended in order |
| `vars` | `{}` | extra `{{PLACEHOLDERS}}`. **Never put secrets here**: prompts end up in logs |

Placeholders available: `{{ISSUE}} {{TITLE}} {{REPO}} {{BRANCH}} {{WORKTREE}} {{BASE}} {{VERIFY}}`, `{{SITE}}` when `verify.placeholder` is set, plus `vars`. An undefined placeholder is an error, not an empty string. Run `nohalt prompt <issue>` to see exactly what an agent will be told.

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
