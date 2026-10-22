"""Run ``claude -p --output-format stream-json`` and print readable lines.

    python -m nohalt.agents.claude_stream -- claude -p ... -- "<prompt>"

Assistant text is printed as is, tool calls as ``$ <command or path>``, and
rate-limit and result events as ``[agent] ...`` lines that the supervisor
reads to tell a usage limit or a server error from the agent's own exit.
A TERM to this process takes claude down with it, never orphans it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from typing import Any


def render(event: dict[str, Any]) -> list[str]:
    t = event.get("type")
    out: list[str] = []
    if t == "assistant":
        for c in (event.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and c.get("text"):
                out.append(c["text"])
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                what = inp.get("command") or inp.get("file_path") or inp.get("pattern") or c.get("name")
                out.append(f"$ {what}")
    elif t == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        out.append(f"[agent] rate_limit {info.get('rateLimitType', '?')} {info.get('status', '?')}")
    elif t == "result":
        result = str(event.get("result") or "")[:400].replace("\n", " ")
        out.append(f"[agent] result: {event.get('subtype', '')} {str(bool(event.get('is_error'))).lower()} {result}")
    return out


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("usage: python -m nohalt.agents.claude_stream -- claude -p ...", file=sys.stderr)
        return 2
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    def stop(signum: int, _frame: object) -> None:
        try:
            proc.send_signal(signum)
        finally:
            sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        try:
            event = json.loads(line)
        except ValueError:
            if line:
                print(line, flush=True)
            continue
        for text in render(event):
            print(text, flush=True)
    rc = proc.wait()
    sys.stdout.flush()
    os._exit(rc)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
