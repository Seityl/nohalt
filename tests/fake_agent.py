"""A scripted stand-in for a coding agent.

    fake_agent.py <forge.json> <script-dir> <prompt>

Each launch pops the first line of ``<script-dir>/<issue>.txt`` and acts it out:

    close            close the issue (as a merged PR would)
    pr-green         commit, push the branch, open a PR whose checks are green
    pr-red           same, with red checks
    pr-pending       same, with checks still running
    pr-conflict      same, with no checks at all and a merge conflict
    busy <seconds>   run a quiet shell command that long (a test run, not a hang)
    provider-error   die the way an agent dies when the model server is down
    usage-limit      stop on a subscription usage limit
    nothing          exit 0 having done nothing
    sleep <seconds>  run that long (to be timed out or found hung)
    dirty            leave an uncommitted file in the worktree

The prompt it was given is appended to ``<script-dir>/<issue>.prompts``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nohalt.forge.local import LocalForge


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def open_pr(forge: LocalForge, issue: int, checks: str, base: str = "main") -> None:
    Path(f"work-{issue}.txt").write_text(f"work for {issue} at {time.time()}\n")
    git("add", "-A")
    git("-c", "user.name=agent", "-c", "user.email=agent@example.com", "commit", "-q", "-m", f"work for #{issue}")
    branch = git("branch", "--show-current")
    git("push", "-q", "origin", branch)
    sha = git("rev-parse", "HEAD")
    n = forge.data()["next"]
    forge.add_pull(n, branch, sha, body=f"Closes #{issue}", base=base, mergeable=checks != "conflict")
    with forge._db() as d:
        d["branches"][branch] = sha
    if checks == "green":
        forge.set_checks(sha, [("ci", "completed", "success")])
    elif checks == "red":
        forge.set_checks(sha, [("ci", "completed", "failure")])
    elif checks == "conflict":
        pass
    else:
        forge.set_checks(sha, [("ci", "in_progress", "")])


def main() -> int:
    forge = LocalForge(sys.argv[1])
    script_dir = Path(sys.argv[2])
    prompt = sys.argv[3] if len(sys.argv) > 3 else ""
    issue = int(os.environ["NOHALT_ISSUE"])
    with open(script_dir / f"{issue}.prompts", "a") as f:
        f.write(prompt + "\n=====\n")
    script = script_dir / f"{issue}.txt"
    lines = script.read_text().splitlines() if script.exists() else []
    action = lines[0].strip() if lines else "nothing"
    script.write_text("\n".join(lines[1:]) + ("\n" if lines[1:] else ""))
    print(f"fake agent on #{issue}: {action}", flush=True)
    if action == "close":
        forge.set_state(issue, "CLOSED")
    elif action.startswith("pr-"):
        open_pr(forge, issue, action[3:], base=os.environ.get("NOHALT_BASE_BRANCH", "main"))
    elif action.startswith("busy"):
        secs = action.split()[1]
        subprocess.run(["bash", "-c", f"{sys.executable} -c 'import time; time.sleep({secs})'"], check=False)
    elif action == "provider-error":
        print("Error: Bad Gateway", flush=True)
        return 1
    elif action == "usage-limit":
        print("[agent] rate_limit five_hour rejected", flush=True)
        return 1
    elif action.startswith("sleep"):
        time.sleep(float(action.split()[1]))
    elif action == "dirty":
        Path("uncommitted.txt").write_text("precious\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
