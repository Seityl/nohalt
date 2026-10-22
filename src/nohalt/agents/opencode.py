"""opencode (``opencode run``)."""

from __future__ import annotations

from pathlib import Path

from . import PROVIDER_ERROR, Launch


class OpenCode:
    name = "opencode"

    def __init__(self, binary: str = "opencode", agent: str = "build", extra_args: list[str] | None = None):
        self.binary = binary
        self.agent = agent
        self.extra_args = extra_args or []

    def _env(self, data_dir: Path) -> dict[str, str]:
        # One data directory per issue. A shared session database grew to tens
        # of gigabytes (~0.5 GB per agent run) and could not be vacuumed while
        # any agent was running; a per-issue directory is simply deleted once
        # the issue is done. Credentials are copied in by the supervisor.
        return {"XDG_DATA_HOME": str(data_dir)}

    def launch(self, model: str, prompt: str, *, title: str, resume: bool, data_dir: Path) -> Launch:
        argv = [self.binary, "run", "-m", model, "--title", title]
        if self.agent:
            argv += ["--agent", self.agent]
        if resume:
            argv.append("--continue")
        argv += self.extra_args
        argv.append(prompt)
        return Launch(argv, self._env(data_dir))

    def probe_launch(self, model: str, data_dir: Path) -> Launch:
        return Launch([self.binary, "run", "-m", model, "reply with the single word OK"], self._env(data_dir))

    def provider_failed(self, log_tail: str) -> bool:
        lines = [ln for ln in log_tail.splitlines() if ln.strip()][-8:]
        return PROVIDER_ERROR.search("\n".join(lines)) is not None

    def usage_limited(self, log_tail: str) -> bool:
        return False
