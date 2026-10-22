"""The queue file: order is preference, dependencies are law.

    # comment
    12:
    14: 13              # 14 waits until 13 is CLOSED
    15: 12 14

The file is re-read on every tick, so editing it needs no restart. Issue
numbers are never invented: an empty queue is an error, not a default range,
because a number that does not exist reads like work to anything that asks
about it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_LINE = re.compile(r"^\s*#?(\d+)\s*:\s*([\d\s#,]*)$")


class QueueError(ValueError):
    pass


@dataclass
class Entry:
    issue: int
    deps: list[int] = field(default_factory=list)


def parse(text: str, source: str = "queue") -> list[Entry]:
    entries: list[Entry] = []
    seen: set[int] = set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or (line.startswith("#") and not _is_issue_ref(line)):
            continue
        # strip a trailing comment: "14: 13   # waits on the schema"
        body = re.sub(r"\s+#(?!\d).*$", "", line)
        m = _LINE.match(body)
        if not m:
            raise QueueError(f"{source}:{n}: expected '<issue>: <deps...>', got {raw!r}")
        issue = int(m.group(1))
        if issue in seen:
            raise QueueError(f"{source}:{n}: #{issue} is queued twice")
        seen.add(issue)
        deps = [int(d) for d in re.findall(r"\d+", m.group(2))]
        if issue in deps:
            raise QueueError(f"{source}:{n}: #{issue} depends on itself")
        entries.append(Entry(issue, deps))
    return entries


def _is_issue_ref(line: str) -> bool:
    """'#12:' is an issue written with its hash, not a comment."""
    return re.match(r"^\s*#\d+\s*:", line) is not None


def load(path: Path) -> list[Entry]:
    try:
        text = path.read_text()
    except OSError as e:
        raise QueueError(f"cannot read queue {path}: {e}") from None
    entries = parse(text, str(path))
    if not entries:
        raise QueueError(f"{path} lists no issues")
    return entries


def append(path: Path, issue: int, deps: list[int] | None = None, front: bool = False) -> None:
    line = f"{issue}: {' '.join(map(str, deps or []))}".rstrip() + "\n"
    text = path.read_text() if path.exists() else ""
    if any(e.issue == issue for e in parse(text, str(path))):
        return
    if front:
        path.write_text(line + text)
    else:
        path.write_text(text + ("" if text.endswith("\n") or not text else "\n") + line)
