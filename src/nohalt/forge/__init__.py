"""Forges: where issues, pull requests and checks live."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import CLOSED, MISSING, OPEN, UNKNOWN, Check, Forge, ForgeError, Issue, PullRequest, Run, verdict

if TYPE_CHECKING:
    from ..config import ProjectCfg


def make(project: ProjectCfg) -> Forge:
    """The forge for a ``[project]``: GitHub, GitLab, or ``local:/path/forge.json`` (file-backed)."""
    repo = project.repo
    if repo.startswith("local:"):
        from .local import LocalForge

        return LocalForge(repo[len("local:") :])
    if project.forge == "gitlab":
        from .gitlab import GitLab

        return GitLab(repo, url=project.forge_url, token_env=project.token_env)
    from .github import GitHub

    return GitHub(repo)


def cli(project: ProjectCfg) -> str:
    """The forge's command-line tool agents use."""
    return "glab" if project.forge == "gitlab" else "gh"


__all__ = [
    "CLOSED",
    "MISSING",
    "OPEN",
    "UNKNOWN",
    "Check",
    "Forge",
    "ForgeError",
    "Issue",
    "PullRequest",
    "Run",
    "cli",
    "make",
    "verdict",
]
