"""Agent prompts: a core prompt, the stack's addendum, the project's own files.

Prompts are composed per project, never copied between projects: a prompt
carried over by search-and-replace once told agents on a non-Frappe project
to run ``bench build`` and to "use npm, never npm". So rendering is strict --
a ``{{PLACEHOLDER}}`` that nothing defines is an error, not an empty string.
"""

from __future__ import annotations

import re
import shlex
from importlib import resources
from pathlib import Path

from .config import Config

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


def template(cfg: Config, stack_addendum: str = "") -> str:
    parts = [_read(cfg, cfg.prompt.template)]
    if stack_addendum:
        parts.append(_read(cfg, stack_addendum))
    parts += [_read(cfg, ref) for ref in cfg.prompt.extra]
    return "\n\n".join(p.strip() for p in parts) + "\n"


def render(text: str, values: dict[str, str]) -> str:
    missing = sorted({m.group(1) for m in _VAR.finditer(text)} - set(values))
    if missing:
        raise PromptError("prompt uses undefined placeholder(s): " + ", ".join("{{" + m + "}}" for m in missing))
    return _VAR.sub(lambda m: str(values[m.group(1)]), text)


def verify_command(cfg: Config, worktree: Path) -> str:
    """How an agent calls the verification lock from its worktree."""
    argv = ["nohalt"]
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
    return {
        "CLI": "gh",
        "PR": "pull request",
        "ISSUE_VIEW": f"gh issue view {issue} --repo {repo} --comments",
        "OPEN_PR_HINT": "",
    }


def values(
    cfg: Config, *, issue: int, title: str, branch: str, worktree: Path, extra: dict[str, str] | None = None
) -> dict[str, str]:
    v = {
        "ISSUE": str(issue),
        "TITLE": title.replace("\n", " "),
        "REPO": cfg.project.repo,
        "BRANCH": branch,
        "WORKTREE": str(worktree),
        "BASE": cfg.project.base_branch,
        "VERIFY": verify_command(cfg, worktree),
        **forge_terms(cfg, issue),
    }
    if cfg.verify.placeholder:
        v["SITE"] = cfg.verify.placeholder
    v.update(cfg.prompt.vars)
    v.update(extra or {})
    return v
