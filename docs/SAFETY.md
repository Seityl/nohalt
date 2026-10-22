# Safety

nohalt runs AI coding agents unattended, with a token that can write to your repository, on a machine you care about. This page says what can go wrong, what nohalt does about it, and -- just as important -- what it does **not** do.

## The threat model in one paragraph

Assume an agent will eventually do anything its tools allow, in good faith, when pursuing "make the checks green" or "finish the issue". It will merge its own work if the prompt is ambiguous about who merges. It will reach for the REST API if the CLI refuses. It will kill processes by name across the machine to clean up its test runner. It will mark its own issue done. None of this needs malice; each of these has happened. nohalt's answer is layered: **prompt rules** (cheap, easily outvoted), **guard shims** (speed bumps that log), **supervisor invariants** (what nohalt itself will and will not do), and **forge-side protection** (the only hard wall).

## What nohalt guarantees

- **It decides "done" only from the forge.** An issue is finished when the forge says CLOSED. Agent exit codes and summaries are never evidence.
- **It reads an unreachable forge as "unknown".** It never treats a failed call as "closed", "missing" or "no pull request". It waits.
- **It never discards work.** Before it removes a worktree, it commits any uncommitted changes to a `nohalt/rescued-<issue>-<time>` branch in your clone. Those branches are local and never pushed.
- **One merger.** With `integrate.mode = "none"`, nohalt never merges. With `direct` or `train`, nohalt alone merges, and only PRs whose checks are green on their current head. Agents never merge, whatever the mode.
- **It stops merging on a red pipeline.** In `direct` mode a red or timed-out pipeline after a merge stops all further merges and files a fix-forward issue at the front of the queue.
- **It stops between issues.** `nohalt stop` never kills an agent mid-work. `stop --now` does, and the agent resumes on the next run.

## Guard shims (speed bumps, with a log)

`gh`, `glab`, `git`, `pkill` and `killall` shims go first on every agent's `PATH`. Each refusal is written to `<state>/logs/guard.log`.

| Shim | Refuses | Why |
|---|---|---|
| `gh` | `pr merge`, `pr close`, `issue close/delete/transfer`, `release *`, `api …/merge`, `api … state=closed`, tag and release writes via the API | Green CI means work is *ready*, not *accepted*. A closed issue means done, so an agent must not be able to declare it. |
| `gh` | (logs) `auth token` | Taking the token is the first step of going around the shim. |
| `glab` | `mr merge/approve/close/delete`, `issue close/delete/move`, `release *`, `repo delete/archive/transfer`, `api …/merge`, `api …/approve`, `api … state_event=close`, tag and release writes via the API; logs `config get token` and `auth status --show-token` | The GitLab twin of the `gh` shim. |
| `git` | `stash` (except `list`/`show`) | Worktrees share one stash stack. One agent's pop takes another agent's work. |
| `git` | pushes to the base branch, tag pushes, `--mirror`, a bare `push` while on the base | Agents push their own branch and nothing else. |
| `pkill`, `killall` | signalling test, build or interpreter processes outside the caller's worktree | CI runners and other agents run as the same user. One broad `pkill` killed builds on four runners. |

**These are not a sandbox.** An agent with a write token can call the API with `curl`, or run the real binary by its absolute path. The shims block the obvious paths and log the rest. The hard controls live outside nohalt.

## What you must do

1. **Protect the base branch on the forge.** Require pull requests and passing checks, and disallow force pushes. On GitLab, make the base a protected branch with "Allowed to push: No one" and "Allowed to merge" limited to the account nohalt merges with, and turn on "Pipelines must succeed". This is the only real wall against an agent merging or pushing to the base. `nohalt doctor` warns when protection is missing. (On GitHub, private repositories need a paid plan for branch protection. Without it, treat the shims as your only, porous, defence.)
2. **Scope the token.** On GitHub, use a fine-grained token limited to the one repository, with the permissions the agents need (contents and pull requests: write; issues: write for comments). On GitLab, use a project access token with the Developer role (so protected-branch rules apply to it) rather than a personal token. Don't give agents an organisation-wide, group-owner or admin token, and don't let them act under a human's personal token: their work is then indistinguishable from the human's, and a mistake has the human's reach. `nohalt doctor` warns about broad GitLab token scopes. If nohalt merges, give its own token the merge rights and keep agents on the narrower one.
3. **Isolate the machine.** Run agents as a user that cannot reach production hosts, secret stores or other projects' data. Agents run shell commands. Assume they will run any command that seems helpful.
4. **Keep secrets out of prompts.** Never put passwords or tokens in `prompt.vars` or prompt files. Prompts are written to agent logs and session stores. Point agents at a credentials file the test environment reads instead.
5. **Keep the verification lock honest.** List in `[verify] refuse` the commands that reach past the agent's own test environment (for example, shared-server restarts, dropping databases, anything naming a production host). The `frappe` stack ships a default list.
6. **Know whether a merge is a deployment.** If a pipeline deploys production on every push to the base branch, an integrator merging into it is doing unattended production deploys, migrations included. Point `base_branch` at an integration branch (`develop`, `release/…`) and let humans promote, or keep `integrate.mode = "none"` for that branch. `nohalt doctor` warns when an integrator would merge into the forge's default branch.
7. **Use TLS to reach the forge.** A plain `http://` `forge_url` sends the token in cleartext on every call. `nohalt doctor` warns about it.
8. **Start without an integrator.** Run with `integrate.mode = "none"` until you have watched a few issues end to end, then move to `direct`. Hold sensitive changes (money, permissions, customer documents) for human review by keeping them out of the queue or labelling them with a `held_labels` label.
9. **Cap spend.** If agents run on a pay-per-token API, enable `[budget]` and set a fallback model.
10. **Watch the first night.** `nohalt status --watch`, `<state>/logs/nohalt.log` and `<state>/logs/guard.log`.

## Data nohalt stores

Everything lives under `state_dir` (default `.nohalt/`, which `nohalt init` adds to `.gitignore`):

- `issues/<n>.json`: each issue's record (status, counters, model, branch, history);
- `logs/issue-<n>.log`: each agent's full output, which may include code, test output and anything the agent printed;
- `agent-data/<n>/`: each agent's private session data and copied credentials, deleted when the issue is done;
- `logs/nohalt.log`, `logs/guard.log`, `logs/refused.log`, `logs/deploy.log`.

Treat the state directory as sensitive. Don't attach it to bug reports without redacting it.
