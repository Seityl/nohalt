"""Claude Code (``claude -p``), for subscription plans.

Subscription credentials work in Claude Code only, so agents on a plan run
here rather than in opencode. Claude's stream-json is turned into readable
lines by ``nohalt.agents.claude_stream`` so status screens can show the last
thing the agent did.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import SETUP_ERROR, Launch, UsageSignal, usage_signal

# Sub-agents, schedulers and remote triggers multiply spend on a shared plan
# and outlive the supervisor's view of the agent; the worktree tools fight
# nohalt's own worktree management.
DISALLOWED = [
    "Task",
    "CronCreate",
    "CronDelete",
    "ScheduleWakeup",
    "RemoteTrigger",
    "PushNotification",
    "EnterWorktree",
    "ExitWorktree",
    "SendMessage",
]

_PROVIDER = re.compile(r"^\[agent\] result: \S* true .*(overloaded|api_error|50[0234]|529|ECONNRESET|fetch failed)", re.I | re.M)
# Usage lines, as claude_stream prints them: the window (five_hour, seven_day, ...)
# and, when claude reports it, the reset time. ``[agents.options.claude-code]
# usage_limit`` / ``usage_warning`` replace these.
USAGE_LIMIT = [
    r"^\[agent\] rate_limit (?P<window>\S+) rejected\b(?:.*?\bresets=(?P<resets>\d+))?",
    r"^\[agent\] result: \S* true .*(usage limit|limit reached|rate limit)",
]
USAGE_WARNING = [r"^\[agent\] rate_limit (?P<window>\S+) allowed_warning\b(?:.*?\bresets=(?P<resets>\d+))?"]


class ClaudeCode:
    name = "claude-code"

    def __init__(
        self,
        binary: str = "claude",
        disallowed_tools: list[str] | None = None,
        extra_args: list[str] | None = None,
        mcp: bool = False,
        usage_limit: list[str] | None = None,
        usage_warning: list[str] | None = None,
    ):
        self.binary = binary
        self.disallowed = DISALLOWED if disallowed_tools is None else disallowed_tools
        self.extra_args = extra_args or []
        self.mcp = mcp
        self.usage_limit = USAGE_LIMIT if usage_limit is None else usage_limit
        self.usage_warning = USAGE_WARNING if usage_warning is None else usage_warning

    def _base(self, model: str) -> list[str]:
        argv = [self.binary, "-p", "--model", model, "--dangerously-skip-permissions"]
        if not self.mcp:
            # A coding agent has no business with the account's connectors
            # (mail, calendar, drive).
            argv.append("--strict-mcp-config")
        return argv

    def launch(self, model: str, prompt: str, *, title: str, resume: bool, data_dir: Path) -> Launch:
        argv = self._base(model)
        if resume:
            argv.append("--continue")
        if self.disallowed:
            argv += ["--disallowedTools", *self.disallowed]
        argv += ["--output-format", "stream-json", "--verbose", *self.extra_args, "--", prompt]
        wrapped = [sys.executable, "-m", "nohalt.agents.claude_stream", "--", *argv]
        env = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"} if not self.mcp else {}
        return Launch(wrapped, env, unset_env=["CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION"])

    def probe_launch(self, model: str, data_dir: Path) -> Launch:
        # Exactly the launch mode (stream-json through the wrapper): a plain-text
        # probe once passed on a machine whose model router rejected stream-json,
        # and every real launch then died on its first request.
        return self.launch(model, "reply with the single word OK", title="nohalt probe", resume=False, data_dir=data_dir)

    def setup_failed(self, log_tail: str) -> bool:
        return SETUP_ERROR.search(log_tail) is not None

    def provider_failed(self, log_tail: str) -> bool:
        return _PROVIDER.search(log_tail) is not None

    def usage_signal(self, log_tail: str) -> UsageSignal | None:
        return usage_signal(log_tail, self.usage_limit, self.usage_warning)

    def usage_limited(self, log_tail: str) -> bool:
        sig = self.usage_signal(log_tail)
        return sig is not None and sig.kind == "limit"
