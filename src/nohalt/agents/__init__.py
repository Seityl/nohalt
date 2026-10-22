"""Coding-agent adapters.

A model is always written ``<agent>:<model>`` -- ``opencode:anthropic/claude-sonnet-4-5``,
``claude-code:sonnet`` -- so routing a model also routes the program that runs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# What a model server's transport failure looks like at the end of an agent's
# output. Only lines the agent CLI itself prints as errors count, so text the
# agent quoted mid-session cannot trigger a retry.
PROVIDER_ERROR = re.compile(
    r"^\s*(?:Error:|\[agent\] error:)\s*(?:.*?)(Bad Gateway|Service Unavailable|Gateway Time-?out|fetch failed|"
    r"socket hang up|ECONNRESET|ECONNREFUSED|ETIMEDOUT|overloaded|(?:HTTP |status )?50[0234]\b|529)",
    re.I | re.M,
)


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    unset_env: list[str] = field(default_factory=list)


class Agent(Protocol):
    name: str

    def launch(self, model: str, prompt: str, *, title: str, resume: bool, data_dir: Path) -> Launch: ...
    def provider_failed(self, log_tail: str) -> bool: ...
    def usage_limited(self, log_tail: str) -> bool: ...
    def probe_launch(self, model: str, data_dir: Path) -> Launch: ...


def split_model(spec: str) -> tuple[str, str]:
    agent, _, model = spec.partition(":")
    if not model:
        raise ValueError(f"model {spec!r} must be '<agent>:<model>'")
    return agent, model


def get(name: str, options: dict[str, Any] | None = None) -> Agent:
    opts = options or {}
    if name == "opencode":
        from .opencode import OpenCode

        return OpenCode(**opts)
    if name == "claude-code":
        from .claude_code import ClaudeCode

        return ClaudeCode(**opts)
    if name == "command":
        from .command import Command

        return Command(**opts)
    raise ValueError(f"unknown agent {name!r} (known: opencode, claude-code, command)")


RESUME_PROMPT = (
    "You were stopped ({why}). Continue the same task from where you left off. "
    "Check `git status`, your branch and your pull request first; every instruction "
    "from the start of this session still holds."
)
