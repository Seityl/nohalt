"""Any program as an agent: ``command:<name>`` runs a configured argv.

    [agents.options.command]
    argv = ["my-agent", "--model", "{model}", "--prompt", "{prompt}"]
    resume_args = ["--continue"]

Used by the test-suite (a fake agent) and for agents nohalt has no adapter for.
"""

from __future__ import annotations

from pathlib import Path

from . import PROVIDER_ERROR, SETUP_ERROR, Launch


class Command:
    name = "command"

    def __init__(self, argv: list[str] | None = None, resume_args: list[str] | None = None, probe_argv: list[str] | None = None):
        if not argv:
            raise ValueError("[agents.options.command] argv is required")
        self.argv = argv
        self.resume_args = resume_args or []
        self.probe_argv = probe_argv or ["true"]

    def _fmt(self, items: list[str], **kw: str) -> list[str]:
        return [a.format(**kw) for a in items]

    def launch(self, model: str, prompt: str, *, title: str, resume: bool, data_dir: Path) -> Launch:
        argv = self._fmt(self.argv, model=model, prompt=prompt, title=title, data_dir=str(data_dir))
        if resume:
            argv += self.resume_args
        return Launch(argv, {"NOHALT_AGENT_DATA": str(data_dir)})

    def probe_launch(self, model: str, data_dir: Path) -> Launch:
        return Launch(self._fmt(self.probe_argv, model=model, prompt="", title="", data_dir=str(data_dir)))

    def setup_failed(self, log_tail: str) -> bool:
        return SETUP_ERROR.search(log_tail) is not None

    def provider_failed(self, log_tail: str) -> bool:
        lines = [ln for ln in log_tail.splitlines() if ln.strip()][-8:]
        return PROVIDER_ERROR.search("\n".join(lines)) is not None

    def usage_limited(self, log_tail: str) -> bool:
        return "[agent] rate_limit" in log_tail and "rejected" in log_tail
