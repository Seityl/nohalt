"""The review gate (review.py): green is not enough for issues that need a review."""

from __future__ import annotations

import subprocess
import time
from types import SimpleNamespace

import pytest

from nohalt import config as config_mod
from nohalt.forge import Issue
from nohalt.review import Reviews, body_hash, parse_verdict
from nohalt.state import BLOCKED, DONE, IN_REVIEW, QUEUED, READY, WAITING_CI
from nohalt.supervisor import Supervisor

REVIEW = {"enabled": True, "labels": ["careful"], "model": "command:reviewer"}


def pull_comments(w) -> list[str]:  # type: ignore[no-untyped-def]
    return [c[1] for c in w.forge.data().get("pull_comments", [])]


def test_green_and_selected_goes_to_review_then_approve_makes_it_ready(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "approve")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()  # judged: PR -> waiting_ci; watched: green -> in review; reviewer launched
    r = w.rec(1)
    assert r.status == IN_REVIEW and r.review_pid and r.review_model == "command:reviewer"
    w.drive(2)
    r = w.rec(1)
    assert r.status == READY
    assert r.review_verdict == "approve" and r.review_approved == r.pr_sha
    assert any("nohalt review: approve" in c for c in pull_comments(w))
    prompt = w.review_prompts(1)[0]
    assert "NOHALT-REVIEW" in prompt and "issue #1" in prompt and r.pr_sha in prompt
    assert not w.sup.trees.review_path(1).exists()  # review trees are thrown away


def test_changes_hand_the_issue_back_to_its_author_with_the_review(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green", "push-green", labels=["careful"])
    w.review(1, "changes add the missing export button", "approve")
    w.drive(8)
    r = w.rec(1)
    assert r.status == READY and r.review_rounds == 1
    second = w.prompts(1)[1]
    assert "asked for changes" in second and "add the missing export button" in second
    assert len(w.review_prompts(1)) == 2
    assert "previous review" in w.review_prompts(1)[1]  # the re-review sees what was asked
    assert any("nohalt review: changes" in c for c in pull_comments(w))


def test_changes_still_requested_after_max_rounds_go_to_a_human(world):
    w = world(review={**REVIEW, "max_rounds": 1})
    w.issue(1, "pr-green", "push-green", "push-green", labels=["careful"])
    w.review(1, "changes one", "changes two", "approve")
    w.drive(10)
    r = w.rec(1)
    assert r.status == BLOCKED
    assert r.blocked_reason == "review: changes still requested after 1 round(s)"
    assert "nohalt-blocked" in w.forge.issue(1).labels


def test_nothing_pushed_after_changes_counts_another_round_without_a_new_review(world):
    w = world(review={**REVIEW, "max_rounds": 2})
    w.issue(1, "pr-green", "nothing", "push-green", labels=["careful"])
    w.review(1, "changes do the thing", "approve")
    w.drive(10)
    r = w.rec(1)
    assert r.status == READY and r.review_rounds == 2
    assert len(w.review_prompts(1)) == 2
    assert "pushing anything or editing the PR description" in w.prompts(1)[2]


def test_a_description_edit_alone_is_reviewed_again_without_a_round(world):
    """#74: the review asked for the description; the author edited it and pushed nothing."""
    w = world(review={**REVIEW, "max_rounds": 1})
    w.issue(1, "pr-green", "describe Acceptance: real-backend run PASS", labels=["careful"])
    w.review(1, "changes add an Acceptance section", "approve")
    w.drive(10)
    r = w.rec(1)
    assert r.status == READY and r.review_rounds == 1  # the one "changes" verdict; the edit cost nothing
    assert r.review_approved == r.pr_sha == r.review_sha  # the same head, reviewed twice
    assert "updated-description" in w.prompts(1)[1]  # the hand-back says how to report a description fix
    first, second = w.review_prompts(1)
    assert "Acceptance: real-backend run PASS" not in first
    assert "Acceptance: real-backend run PASS" in second  # the reviewer judges the description it now has
    assert "the author edited the description instead" in second
    assert any("the author says it updated the PR description" in h for h in r.history)
    assert any("its description changed (the author: Acceptance: real-backend run PASS)" in h for h in r.history)


def test_the_description_hash_decides_not_the_marker(world):
    w = world(review={**REVIEW, "max_rounds": 3})
    script = ["pr-green", "describe-quiet Acceptance table", "claim-describe screenshots listed", "push-green"]
    w.issue(1, *script, labels=["careful"])
    w.review(1, "changes list the screenshots", "changes the screenshots are still missing", "approve")
    w.drive(16)
    r = w.rec(1)
    # An edit nobody announced is a change (reviewed again); an announced edit that never reached the PR is not.
    assert r.status == READY and r.review_rounds == 3
    assert len(w.review_prompts(1)) == 3
    assert "Acceptance table" in w.review_prompts(1)[1]
    assert "the edit did not reach the PR" in w.prompts(1)[3]


def test_a_description_edit_after_approval_keeps_the_approval(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "approve", "changes should never run")
    w.drive(4)
    first = w.rec(1)
    assert first.status == READY and first.review_approved == first.pr_sha
    with w.forge._db() as d:
        d["pulls"][str(first.pr)]["body"] += "\n\nA late note on the description."
    w.drive(2)
    r = w.rec(1)
    assert r.status == READY and r.review_approved == first.pr_sha
    # Even through the gate again (back from a green re-check): the code it approved has not changed.
    r.status = WAITING_CI
    w.sup.state.put(r)
    w.sup.reviews.gate(r, w.forge.pull(r.pr))
    assert w.rec(1).status == READY and w.rec(1).review_approved == first.pr_sha
    assert len(w.review_prompts(1)) == 1


def test_a_review_from_before_descriptions_were_recorded_trusts_the_marker_once(tmp_path):
    from nohalt.forge import PullRequest
    from nohalt.state import IssueRecord

    pr = PullRequest(7, "b", "a" * 40, body="Closes #1")
    old = IssueRecord(issue=1)  # review_body "": written before #74
    assert Reviews._description_changed(old, pr, "added the Acceptance table")
    assert not Reviews._description_changed(old, pr, "")
    new = IssueRecord(issue=1, review_body=body_hash("Closes #1"))
    assert not Reviews._description_changed(new, pr, "added the Acceptance table")  # the hash decides
    assert Reviews._description_changed(new, PullRequest(7, "b", "a" * 40, body="Closes #1\n\nAcceptance"), "")


def test_body_hash_ignores_what_the_forge_rewrites():
    assert body_hash("a\r\nb  \n\n") == body_hash("a\nb")
    assert body_hash("a\nb") != body_hash("a\nb\nc")
    assert body_hash("") == body_hash(None)  # type: ignore[arg-type]


def test_updated_description_reads_the_marker():
    from nohalt.agents import updated_description

    assert updated_description("done\nNOHALT-RESULT: updated-description -- Acceptance table\n") == "Acceptance table"
    assert updated_description("end with `NOHALT-RESULT: updated-description -- <what you changed>`") is None
    assert updated_description("NOHALT-RESULT: no-change -- fixed on main") is None


def test_the_reviewer_cannot_push(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "push")
    w.drive(4)
    assert (w.scripts / "1.review-push-rc").read_text() != "0"
    assert "REFUSED" in (w.sup.state.logs_dir / "guard.log").read_text()
    origin = w.repo.parent / "origin.git"
    branches = subprocess.run(["git", "-C", str(origin), "branch", "--list"], capture_output=True, text=True).stdout
    assert "reviewer-was-here" not in branches
    assert w.rec(1).status == READY


def test_a_push_after_approval_needs_a_new_review_of_the_whole_pr(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "approve", "approve")
    w.drive(4)
    first = w.rec(1)
    assert first.status == READY
    # Someone pushes to the PR after it was approved.
    tree = w.cfg.worktree_root / "1"
    (tree / "late.txt").write_text("a late change\n")
    git = ["git", "-C", str(tree), "-c", "user.name=h", "-c", "user.email=h@example.com"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "late"], check=True)
    subprocess.run([*git, "push", "-q", "origin", first.branch], check=True)
    new = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    with w.forge._db() as d:
        d["pulls"][str(first.pr)]["head_sha"] = new
    w.forge.set_checks(new, [("ci", "completed", "success")])
    w.sup.tick()
    assert w.rec(1).status == IN_REVIEW  # the approval was for the old head
    assert w.rec(1).review_approved == first.pr_sha
    w.drive(3)
    r = w.rec(1)
    assert r.status == READY and r.review_approved == new
    assert len(w.review_prompts(1)) == 2 and "Review the **whole** PR again" in w.review_prompts(1)[1]


def test_a_head_moved_during_review_discards_the_verdict(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "sleep-approve 1", "approve")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    assert w.rec(1).status == IN_REVIEW and w.rec(1).review_pid
    pr = w.rec(1).pr
    new = "e" * 40
    with w.forge._db() as d:
        d["pulls"][str(pr)]["head_sha"] = new
    w.forge.set_checks(new, [("ci", "pending", "")])
    w.sup.tick()  # head moved: back to CI, the running review is stopped
    r = w.rec(1)
    assert r.status == WAITING_CI and not r.review_pid and r.review_verdict == ""


def test_issues_not_selected_skip_review(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green")
    w.review(1, "changes should never run")
    w.drive(3)
    assert w.rec(1).status == READY
    assert w.review_prompts(1) == []


def _reviews(tmp_path, **review):  # type: ignore[no-untyped-def]
    cfg = config_mod.from_dict({"review": review}, root=tmp_path)
    return Reviews(SimpleNamespace(cfg=cfg))  # type: ignore[arg-type]


def test_title_and_all_select_issues(tmp_path):
    r = _reviews(tmp_path, enabled=True, title="^Issue 2$", model="command:reviewer", fallbacks=["command:other"])
    assert not r.needed(Issue(1, "OPEN", "Issue 1"))
    assert r.needed(Issue(2, "OPEN", "Issue 2"))
    assert r.models() == ["command:reviewer", "command:other"]
    r = _reviews(tmp_path, enabled=True, all=True)
    assert r.needed(Issue(1, "OPEN", "anything"))
    assert r.models() == [r.cfg.models.default]
    assert not _reviews(tmp_path, enabled=False, all=True).needed(Issue(1, "OPEN", "anything"))
    assert not _reviews(tmp_path, enabled=True, labels=["careful"]).needed(Issue(1, "OPEN", "x", ["other"]))


def test_review_config_is_checked(tmp_path):
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_dict({"review": {"title": "("}}, root=tmp_path)
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_dict({"review": {"model": "no-agent"}}, root=tmp_path)
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_dict({"review": {"colour": "red"}}, root=tmp_path)
    assert config_mod.from_dict({"review": {"timeout": "30m"}}, root=tmp_path).review.timeout == 1800


def test_a_verdict_printed_to_the_log_counts(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "stdout-approve")
    w.drive(4)
    assert w.rec(1).status == READY


def test_no_verdict_is_never_an_approval(world):
    w = world(review={**REVIEW, "attempts": 2})
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "noverdict", "noverdict", "approve")
    w.drive(6)
    r = w.rec(1)
    assert r.status == BLOCKED and "no verdict, so no approval" in r.blocked_reason
    assert r.review_failures == 2


def test_reviewer_endpoint_errors_are_retried_and_cost_nothing(world):
    w = world(review={**REVIEW, "attempts": 1})
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "provider-error", "provider-error", "approve")
    w.drive(8)
    r = w.rec(1)
    assert r.status == READY and r.review_failures == 0
    assert len(w.review_prompts(1)) == 3


def test_a_timed_out_reviewer_is_retried(world):
    w = world(review={**REVIEW, "timeout": 1})
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "sleep-approve 30", "approve")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    assert w.rec(1).review_pid
    time.sleep(1.5)
    w.sup.tick()  # stopped on time, judged: no verdict -> another review
    assert w.rec(1).review_failures == 1
    w.drive(3)
    assert w.rec(1).status == READY


def test_restart_mid_review_adopts_the_reviewer(world):
    w = world(review=REVIEW)
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "sleep-approve 1")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    pid = w.rec(1).review_pid
    assert pid
    w.sup = Supervisor(w.cfg, forge=w.forge, say=w.messages.append)
    w.sup.tick()
    assert w.rec(1).review_pid == pid and w.rec(1).status == IN_REVIEW
    w.drive(3)
    assert w.rec(1).status == READY
    assert len(w.review_prompts(1)) == 1  # adopted, never relaunched


def test_reviewers_take_pool_slots(world):
    w = world(review=REVIEW, agents={"max_parallel": 1})
    w.issue(1, "pr-green", labels=["careful"])
    w.issue(2, "close")
    w.review(1, "sleep-approve 1")
    w.sup.tick()  # #1 launched; #2 waits for the only slot
    w.wait_agents()
    w.sup.tick()  # #1 in review: the reviewer takes the slot
    assert w.rec(1).review_pid
    assert w.rec(2).status == QUEUED and not w.rec(2).started_at
    w.drive(4)
    assert w.rec(1).status == READY and w.rec(2).status == DONE


def test_unblocking_after_review_rounds_resets_them(world):
    w = world(review={**REVIEW, "max_rounds": 0})
    w.issue(1, "pr-green", "push-green", labels=["careful"])
    w.review(1, "changes x", "approve")
    w.drive(4)
    assert w.rec(1).status == BLOCKED
    w.forge.remove_label(1, "nohalt-blocked")
    w.sup._issues.clear()
    w.drive(6)
    r = w.rec(1)
    assert r.status == READY and r.review_rounds == 0


def test_parse_verdict_needs_the_line_alone():
    assert parse_verdict("ok\n\nNOHALT-REVIEW: approve\n")[0] == "approve"
    assert parse_verdict("**NOHALT-REVIEW: changes**\n1. x")[0] == "changes"
    assert parse_verdict("end with `NOHALT-REVIEW: approve` or `NOHALT-REVIEW: changes` and a list")[0] == ""
    assert parse_verdict("NOHALT-REVIEW: approve\nlater\nNOHALT-REVIEW: changes\n1. y")[0] == "changes"
    assert parse_verdict("") == ("", "")


def test_status_shows_reviews(world):
    from nohalt.cli import render_status

    w = world(review=REVIEW)
    w.issue(1, "pr-green", labels=["careful"])
    w.review(1, "sleep-approve 1")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    out = render_status(w.cfg)
    assert "in_review" in out and "command:reviewer" in out
    w.drive(3)
    assert "approve" in render_status(w.cfg)


def test_the_integrator_merges_only_after_approval(world):
    w = world(review=REVIEW, integrate={"mode": "direct"})
    w.issue(1, "pr-green", "push-green", labels=["careful"])
    w.review(1, "changes not yet", "approve")
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()
    w.wait_agents()
    w.sup.tick()  # changes: handed back, nothing merged
    assert not w.forge.data()["merged"]
    w.drive(6)
    assert w.forge.data()["merged"] and w.forge.issue(1).state == "CLOSED"
    assert w.rec(1).status == DONE
