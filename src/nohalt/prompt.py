"""Agent prompts: a core prompt, the stack's addendum, the project's own files.

Prompts are composed per project, never copied between projects: a prompt
carried over by search-and-replace once told agents on a non-Frappe project
to run ``bench build`` and to "use npm, never npm". So rendering is strict --
a ``{{PLACEHOLDER}}`` that nothing defines is an error, not an empty string.

A *lane* (``[[prompt.lane]]``) is a per-issue variant of the project's files:
it drops some ``[prompt] extra`` files and appends its own. The lane is chosen
once, at an issue's first launch, and kept on the issue record, so every
relaunch (and the reviewer) gets the same rules. Only a change to the lanes in
the configuration chooses again, and a different answer is logged.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Config, PromptLane
from .forge import Comment
from .util import stamp

if TYPE_CHECKING:
    from .forge import Issue
    from .state import IssueRecord

_VAR = re.compile(r"\{\{\s*([A-Z][A-Z0-9_]*)\s*\}\}")


class PromptError(ValueError):
    pass


def builtin(name: str) -> str:
    return resources.files("nohalt.prompts").joinpath(f"{name}.md").read_text()


def _read(cfg: Config, ref: str) -> str:
    if ref.startswith("builtin:"):
        try:
            return builtin(ref.removeprefix("builtin:"))
        except FileNotFoundError:
            raise PromptError(f"no built-in prompt {ref!r}") from None
    p = Path(ref).expanduser()
    p = p if p.is_absolute() else cfg.root / p
    try:
        return p.read_text()
    except OSError as e:
        raise PromptError(f"cannot read prompt {p}: {e}") from None


# -- lanes ---------------------------------------------------------------------------------------------
def lane_matches(lane: PromptLane, issue: Issue) -> bool:
    """Any of the lane's selectors matching selects it."""
    if lane.labels and issue.has_label(*lane.labels):
        return True
    if lane.title and re.search(lane.title, issue.title or "", re.I):
        return True
    if lane.body and re.search(lane.body, issue.body or "", re.I | re.M):
        return True
    return any(re.search(rf"\bpart of\s+#{n}\b", issue.body or "", re.I) for n in lane.tracker)


def select_lane(cfg: Config, issue: Issue) -> PromptLane | None:
    """The first lane that matches the issue, or None (no lane)."""
    return next((lane for lane in cfg.prompt.lane if lane_matches(lane, issue)), None)


def find_lane(cfg: Config, name: str) -> PromptLane | None:
    return next((lane for lane in cfg.prompt.lane if name and lane.name == name), None)


def lanes_digest(cfg: Config) -> str:
    """Identifies the lanes as configured: a record chosen under another digest chooses again."""
    raw = json.dumps([asdict(lane) for lane in cfg.prompt.lane], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def settle_lane(cfg: Config, rec: IssueRecord, issue: Issue) -> str:
    """Choose the issue's lane if it has none yet, or if the lanes changed since it was chosen.

    Updates ``rec`` (the caller saves it). Returns a description of a change of lane for an issue
    that already had one chosen ("" when nothing changed, or on the first choice)."""
    digest = lanes_digest(cfg)
    if rec.lane_digest == digest:
        return ""
    lane = select_lane(cfg, issue)
    new = lane.name if lane else ""
    first = not rec.lane_digest
    old, rec.lane, rec.lane_digest = rec.lane, new, digest
    if first:
        if new:
            rec.event(f"prompt lane: {new}")
        return ""
    if new == old:
        return ""
    change = f"prompt lane changed from {old or '(none)'} to {new or '(none)'} (the lanes in the configuration changed)"
    rec.event(change)
    return change


def _extra(cfg: Config, lane: PromptLane | None) -> list[str]:
    refs = [r for r in cfg.prompt.extra if not lane or r not in lane.replace]
    return refs + (lane.extra if lane else [])


def template(cfg: Config, stack_addendum: str = "", lane: PromptLane | None = None) -> str:
    parts = [_read(cfg, cfg.prompt.template)]
    if stack_addendum:
        parts.append(_read(cfg, stack_addendum))
    parts += [_read(cfg, ref) for ref in _extra(cfg, lane)]
    if lane and lane.verify_scope and not any(_has_var(p, "LANE") for p in parts):
        parts.append(f"## This issue's lane: {lane.name}\n\n{{{{LANE}}}}")
    return "\n\n".join(p.strip() for p in parts) + "\n"


def _has_var(text: str, name: str) -> bool:
    return any(m.group(1) == name for m in _VAR.finditer(text))


def review_template(cfg: Config, stack_addendum: str = "", lane: PromptLane | None = None) -> str:
    """The reviewer's prompt: the built-in review prompt, ``[review] prompt`` files, then the
    rules the author was given (stack addendum, ``[prompt] extra`` and the issue's lane), to check the PR against."""
    parts = [_read(cfg, "builtin:review"), *(_read(cfg, ref) for ref in cfg.review.prompt)]
    rules = ([_read(cfg, stack_addendum)] if stack_addendum else []) + [_read(cfg, ref) for ref in _extra(cfg, lane)]
    if lane and lane.verify_scope:
        rules.append(f"How this issue must be verified ({lane.name} lane):\n\n{{{{LANE}}}}")
    if rules:
        which = f" This issue is in the **{lane.name}** lane." if lane else ""
        parts.append(
            "## The project's rules for authors\n\nThe author was given these rules." + which + " Check the change against "
            "them; they do not apply to you where they ask for commits, pushes or PRs.\n\n"
            + "\n\n".join(r.strip() for r in rules)
        )
    return "\n\n".join(p.strip() for p in parts) + "\n"


def render(text: str, values: dict[str, str]) -> str:
    missing = sorted({m.group(1) for m in _VAR.finditer(text)} - set(values))
    if missing:
        raise PromptError("prompt uses undefined placeholder(s): " + ", ".join("{{" + m + "}}" for m in missing))
    return _VAR.sub(lambda m: str(values[m.group(1)]), text)


def verify_command(cfg: Config, worktree: Path) -> str:
    """How an agent calls the verification lock from its worktree.

    The interpreter nohalt runs under, not a bare `nohalt`: installed in a
    virtualenv that is not on the agent's PATH, `nohalt` is not found.
    """
    argv = [sys.executable, "-m", "nohalt"]
    if cfg.source:
        argv += ["--config", str(cfg.source)]
    argv += ["verify", str(worktree), "--"]
    return " ".join(shlex.quote(a) for a in argv)


def forge_terms(cfg: Config, issue: int) -> dict[str, str]:
    """How agents talk to this project's forge."""
    repo = shlex.quote(cfg.project.repo)
    if cfg.project.forge == "gitlab":
        base = cfg.project.base_branch
        hint = (
            " If `glab` cannot reach GitLab, open it as you push, with push options: "
            f"`git push -o merge_request.create -o merge_request.target={base} -o merge_request.draft "
            f'-o merge_request.title="<title>" -o merge_request.description="Closes #{issue}" origin HEAD`.'
        )
        return {
            "CLI": "glab",
            "PR": "merge request",
            "ISSUE_VIEW": f"glab issue view {issue} --repo {repo} --comments",
            "OPEN_PR_HINT": hint,
        }
    # REST, not `gh issue view`: GraphQL-backed subcommands break on GitHub-side
    # deprecations (every agent's first command failed this way in a dogfood run).
    slug = cfg.project.repo
    view = (
        f"gh api repos/{slug}/issues/{issue} --jq '.title, .body' && "
        f"gh api repos/{slug}/issues/{issue}/comments --jq '.[] | \"--- \\(.user.login): \\(.body)\"'"
    )
    return {"CLI": "gh", "PR": "pull request", "ISSUE_VIEW": view, "OPEN_PR_HINT": ""}


def values(
    cfg: Config,
    *,
    issue: int,
    title: str,
    branch: str,
    worktree: Path,
    extra: dict[str, str] | None = None,
    lane: PromptLane | None = None,
) -> dict[str, str]:
    v = {
        "ISSUE": str(issue),
        "TITLE": title.replace("\n", " "),
        "REPO": cfg.project.repo,
        "BRANCH": branch,
        "WORKTREE": str(worktree),
        "BASE": cfg.project.base_branch,
        "VERIFY": verify_command(cfg, worktree),
        "LANE": lane.verify_scope if lane else "",
        "LANE_NAME": lane.name if lane else "",
        **forge_terms(cfg, issue),
    }
    if cfg.verify.placeholder:
        v["SITE"] = cfg.verify.placeholder
    v.update(cfg.prompt.vars)
    v.update(extra or {})
    return v


def reopen_note(comments: list[Comment], *, since_known: bool, branch: str, old_branch: str, base: str, limit: int = 20) -> str:
    """What a relaunched agent is told about an issue that was closed and reopened on the forge."""
    lines = [
        "This issue was closed, and has since been reopened on the forge: the work done for it is not complete. "
        "Read the issue and the comments below, work out what is still missing, and finish it. "
        "A merged PR did not finish it; a PR of yours still open for it is where to continue."
    ]
    if old_branch and old_branch != branch:
        lines.append(f"The earlier branch `{old_branch}` was merged; you are on a fresh branch `{branch}`, cut from `{base}`.")
    elif branch:
        lines.append(f"You are on the earlier branch `{branch}`: check what is on it, and merge `{base}` first.")
    head = "Comments since it was closed" if since_known else "Comments on the issue (when it was closed was not recorded)"
    shown = comments[-limit:]
    lines.append(f"\n### {head}")
    if len(comments) > len(shown):
        lines.append(f"({len(comments) - len(shown)} earlier comment(s) left out; read the issue for them.)")
    if not shown:
        lines.append("(none -- read the issue again for what is missing)")
    for c in shown:
        when = f" at {stamp(c.at)}" if c.at else ""
        body = c.body if len(c.body) <= 4000 else c.body[:4000] + "\n[... cut; read the issue for the rest]"
        lines.append(f"\n--- {c.author}{when}\n{body}")
    return "\n".join(lines)
