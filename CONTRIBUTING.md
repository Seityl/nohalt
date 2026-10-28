# Contributing to nohalt

Thanks for helping. nohalt is small on purpose: every mechanism in it exists because something went wrong without it. The best contributions start from an incident -- "this happened while agents ran unattended" -- and end with a mechanism and a test.

## Ground rules

- **Every change has an issue.** Open one first (or comment on an existing one) so the approach can be agreed before you write code.
- **Branches:** `feat/<issue>-<slug>`, `fix/<issue>-<slug>`, `docs/<issue>-<slug>`, `chore/<issue>-<slug>`. Never commit to `main`; everything goes through a pull request.
- **PR titles are conventional commits** (`feat: ...`, `fix(scope): ...`, `docs: ...`). PRs are squash-merged, so the title becomes the commit message that release-please reads to version and write the changelog. A CI check enforces it.
- **A rule needs a mechanism.** If a behaviour matters, put it in code (or a guard) and pin it with a test; a prompt line alone is a preference.
- **Lesson-backed behaviour cites its lesson.** If a test pins a rule from [`docs/LESSONS.md`](docs/LESSONS.md), start its docstring with the lesson ID (`"""LIV-03: ..."""`). New lessons are welcome -- add them to the right table.
- **No private data.** Never commit tokens, hostnames, IPs, private repository names, customer data or real agent logs -- not in code, tests, fixtures or docs. CI runs gitleaks over the whole history; redact anything you paste into issues.

## Development

```bash
git clone https://github.com/Seityl/nohalt && cd nohalt
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # or: uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest -q            # the whole suite runs in well under a minute
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy
shellcheck -S warning src/nohalt/shims/gh src/nohalt/shims/glab src/nohalt/shims/git src/nohalt/shims/pkill src/nohalt/shims/pkgguard
```

Optional: `pre-commit install` runs ruff, gitleaks and shellcheck before each commit.

The tests run real `git` against temporary repositories and real (scripted) agent processes against a file-backed forge (`nohalt.forge.local`), so they exercise the supervisor end to end without touching GitHub or any model. `tests/fake_agent.py` documents the scripted behaviours; add one if your change needs a new kind of agent misbehaviour.

Linux is the supported platform (the supervisor reads `/proc`).

## Releasing (maintainers)

Releases are automated with [release-please](https://github.com/googleapis/release-please):

**Versions before 1.0:** a `feat:` or `fix:` is a **patch** (0.2.0 → 0.2.1). Only a breaking change (`feat!:` or a `BREAKING CHANGE:` footer) moves the **minor**, because below 1.0 the minor is the only compatibility signal left. From 1.0 on, normal semver applies.

**Cadence:** releases are cut **weekly**, batched, by merging the open `chore: release` PR. Unreleased work on `main` waits for the next one.

1. Conventional commits land on `main`; release-please keeps a `chore: release x.y.z` PR open with the version bump and changelog.
2. Pull requests opened by GitHub Actions do not trigger other workflows, so run CI on the release PR's branch once: `gh workflow run CI --ref release-please--branches--main`. Its checks then satisfy branch protection.
3. Merge the release PR. release-please tags `vX.Y.Z`, creates the GitHub release and dispatches `publish.yml`, which builds the distributions, attaches them to the release and publishes to PyPI via trusted publishing (publisher: workflow `publish.yml`, environment `pypi`). To re-publish an existing tag: `gh workflow run publish.yml -f tag=vX.Y.Z`.
