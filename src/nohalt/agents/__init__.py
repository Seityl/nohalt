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


# An agent that could not start at all: its CLI is not logged in, the model
# router refuses it, or it was given an option it does not accept. Retrying
# the issue cannot fix any of these; the agent's setup must be fixed.
SETUP_ERROR = re.compile(
    r"(Failed to authenticate|authentication_failed|invalid (x-)?api[ -]key|not logged in|"
    r"please run \S+ login|API Error: 401|HTTP 401|401 Unauthorized|"
    r"error: (unknown|unrecognized) option|error: option '[^']+' argument '[^']*' is invalid|"
    r"credit balance is too low)",
    re.I,
)


# An agent's structured "nothing to change" result: a line of its own output,
#     NOHALT-RESULT: no-change -- <reason>
# (the claude-code stream also folds it into its one-line result event). The
# reason must be real text: a quoted "<reason>" from the prompt is not one.
NO_CHANGE = re.compile(r"NOHALT-RESULT:\s*no-change\b[ \t]*(?:--|—|–|-|:)?[ \t]*([^<\s\-—–:][^\n]*)", re.I)


def no_change(log_tail: str) -> str | None:
    """The reason an agent gave for changing nothing, or None if it gave none."""
    found = NO_CHANGE.findall(log_tail)
    if not found:
        return None
    return found[-1].strip().strip("`*").strip()[:500] or None


# After a review asked for changes to the PR description, an author that edited it (no push) may say
#     NOHALT-RESULT: updated-description -- <what>
# A hint only: the review gate compares the description itself with the one the reviewer read (AGT-22).
UPDATED_DESCRIPTION = re.compile(r"NOHALT-RESULT:\s*updated-description\b[ \t]*(?:--|—|–|-|:)?[ \t]*([^<\s\-—–:][^\n]*)", re.I)


def updated_description(log_tail: str) -> str | None:
    """What an agent said it changed in the PR description, or None if it said nothing."""
    found = UPDATED_DESCRIPTION.findall(log_tail)
    if not found:
        return None
    return found[-1].strip().strip("`*").strip()[:500] or None


@dataclass
class UsageSignal:
    """A usage line an agent printed: ``limit`` (it stopped on it) or ``warning`` (it is close to it)."""

    kind: str
    window: str = ""  # five_hour, seven_day, ... when the line names one
    resets_at: float = 0.0  # epoch seconds, when the line reports it
    line: str = ""


def usage_signal(log_tail: str, limit: list[str], warning: list[str]) -> UsageSignal | None:
    """The last usage line in ``log_tail`` matching a ``limit`` or ``warning`` pattern.

    Patterns are regular expressions matched per line. A named group ``window``
    names the usage window and ``resets`` the reset time (epoch seconds). A limit
    anywhere in the tail wins over a later warning: the agent stopped on it.
    """
    found: dict[str, list[tuple[int, UsageSignal]]] = {"limit": [], "warning": []}
    for kind, pats in (("limit", limit), ("warning", warning)):
        for pat in pats:
            for m in re.finditer(pat, log_tail, re.I | re.M):
                start = log_tail.rfind("\n", 0, m.start()) + 1
                end = log_tail.find("\n", m.end())
                resets = m.groupdict().get("resets") or ""
                line = log_tail[start : end if end >= 0 else None]
                window = m.groupdict().get("window") or ""
                found[kind].append((m.start(), UsageSignal(kind, window, float(resets) if resets.isdigit() else 0.0, line)))
    for kind in ("limit", "warning"):
        hits = [sig for _, sig in sorted(found[kind], key=lambda h: h[0])]
        if hits:
            # The last line decides; an earlier line of the same kind may carry the window and reset.
            last = hits[-1]
            last.window = last.window or next((h.window for h in reversed(hits) if h.window), "")
            last.resets_at = last.resets_at or next((h.resets_at for h in reversed(hits) if h.resets_at), 0.0)
            return last
    return None


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
    def setup_failed(self, log_tail: str) -> bool: ...
    def usage_signal(self, log_tail: str) -> UsageSignal | None: ...


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
