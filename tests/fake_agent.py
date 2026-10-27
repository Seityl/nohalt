"""A scripted stand-in for a coding agent.

    fake_agent.py <forge.json> <script-dir> <prompt>

Each launch pops the first line of ``<script-dir>/<issue>.txt`` and acts it out:

    close            close the issue (as a merged PR would)
    pr-green         commit, push the branch, open a PR whose checks are green
    pr-red           same, with red checks
    pr-pending       same, with checks still running
    pr-conflict      same, with no checks at all and a merge conflict
    pr-draft         same as pr-green, opened as a draft
    push-green       commit and push to the branch; its open PR moves to the new head, green
    setup-fail       die the way an agent dies when its CLI is not logged in
    busy <seconds>   run a quiet shell command that long (a test run, not a hang)
    provider-error   die the way an agent dies when the model server is down
    usage-limit      stop on a subscription usage limit
    nothing          exit 0 having done nothing
    no-change <why>  exit 0 having changed nothing, saying why (NOHALT-RESULT: no-change -- <why>)
    sleep <seconds>  run that long (to be timed out or found hung)
    dirty            leave an uncommitted file in the worktree

The prompt it was given is appended to ``<script-dir>/<issue>.prompts``.

Launched as a reviewer (``NOHALT_ROLE=review``) it reads ``<issue>.review.txt``
and appends its prompts to ``<issue>.review-prompts``:

    approve            write an approving review to $NOHALT_REVIEW_FILE
    changes <text>     write a review asking for <text>
    stdout-approve     print an approving review, write no file
    noverdict          end without a verdict
    push               try to push a commit (exit code to <issue>.review-push-rc), then approve
    sleep-approve <s>  run that long, then approve
    provider-error     die the way an agent dies when the model server is down
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
    draft = checks == "draft"
    checks = "green" if draft else checks
    Path(f"work-{issue}.txt").write_text(f"work for {issue} at {time.time()}\n")
    git("add", "-A")
    git("-c", "user.name=agent", "-c", "user.email=agent@example.com", "commit", "-q", "-m", f"work for #{issue}")
    branch = git("branch", "--show-current")
    git("push", "-q", "origin", branch)
    sha = git("rev-parse", "HEAD")
    with forge._db() as d:  # taken under the forge's lock: two agents finishing together must not share a number
        n = d["next"]
        d["next"] = n + 1
    forge.add_pull(n, branch, sha, body=f"Closes #{issue}", base=base, mergeable=checks != "conflict", draft=draft)
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


def push_green(forge: LocalForge, issue: int) -> None:
    Path(f"work-{issue}.txt").write_text(f"more work for {issue} at {time.time()}\n")
    git("add", "-A")
    git("-c", "user.name=agent", "-c", "user.email=agent@example.com", "commit", "-q", "-m", f"more work for #{issue}")
    branch = git("branch", "--show-current")
    git("push", "-q", "origin", branch)
    sha = git("rev-parse", "HEAD")
    with forge._db() as d:
        d["branches"][branch] = sha
        for p in d["pulls"].values():
            if p["head_ref"] == branch and p["state"] == "open":
                p["head_sha"] = sha
    forge.set_checks(sha, [("ci", "completed", "success")])


def review(script_dir: Path, prompt: str, issue: int) -> int:
    with open(script_dir / f"{issue}.review-prompts", "a") as f:
        f.write(prompt + "\n=====\n")
    script = script_dir / f"{issue}.review.txt"
    lines = script.read_text().splitlines() if script.exists() else []
    action = lines[0].strip() if lines else "noverdict"
    script.write_text("\n".join(lines[1:]) + ("\n" if lines[1:] else ""))
    print(f"fake reviewer on #{issue}: {action}", flush=True)
    out = Path(os.environ["NOHALT_REVIEW_FILE"])
    approve = f"1. Criterion met: work file added (work-{issue}.txt).\n\nNOHALT-REVIEW: approve\n"
    if action == "push":
        Path("reviewer.txt").write_text("not mine to push\n")
        subprocess.run(["git", "add", "-A"], check=False)
        subprocess.run(["git", "-c", "user.name=r", "-c", "user.email=r@example.com", "commit", "-q", "-m", "x"], check=False)
        rc = subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/reviewer-was-here"], check=False).returncode
        (script_dir / f"{issue}.review-push-rc").write_text(str(rc))
        action = "approve"
    if action.startswith("sleep-approve"):
        time.sleep(float(action.split()[1]))
        action = "approve"
    if action == "approve":
        out.write_text(approve)
    elif action.startswith("changes"):
        out.write_text(f"Missing an acceptance item.\n\nNOHALT-REVIEW: changes\n1. {action[8:]}\n")
    elif action == "stdout-approve":
        print(approve, flush=True)
    elif action == "provider-error":
        print("Error: Bad Gateway", flush=True)
        return 1
    else:
        print("I looked around and have no opinion.", flush=True)
    return 0


def main() -> int:
    forge = LocalForge(sys.argv[1])
    script_dir = Path(sys.argv[2])
    prompt = sys.argv[3] if len(sys.argv) > 3 else ""
    issue = int(os.environ["NOHALT_ISSUE"])
    if os.environ.get("NOHALT_ROLE") == "review":
        return review(script_dir, prompt, issue)
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
    elif action == "push-green":
        push_green(forge, issue)
    elif action.startswith("busy"):
        secs = action.split()[1]
        subprocess.run(["bash", "-c", f"{sys.executable} -c 'import time; time.sleep({secs})'"], check=False)
    elif action == "provider-error":
        print("Error: Bad Gateway", flush=True)
        return 1
    elif action == "setup-fail":
        print("Failed to authenticate. API Error: 401 -- try 'claude login'", flush=True)
        return 1
    elif action == "usage-limit":
        print("[agent] rate_limit five_hour rejected", flush=True)
        return 1
    elif action.startswith("sleep"):
        time.sleep(float(action.split()[1]))
    elif action.startswith("no-change"):
        print("I looked at the newest base.", flush=True)
        print(f"NOHALT-RESULT: no-change -- {action.partition(' ')[2]}", flush=True)
    elif action == "dirty":
        Path("uncommitted.txt").write_text("precious\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
