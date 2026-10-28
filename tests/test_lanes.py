"""Per-issue prompt lanes (prompt.py) and cross-model review (review.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nohalt import config as config_mod
from nohalt import prompt as prompt_mod
from nohalt.forge import Issue
from nohalt.review import reviewer_models
from nohalt.state import BLOCKED, READY, IssueRecord

FAST = "## Fast lane\n\nTwo screenshots are enough; run only the tests you touched.\n"
FULL = "## Full delivery\n\nNo fast lane: every acceptance item, every screenshot.\n"
RULES = "## House rules\n\nTranslate every new string.\n"


def _files(root: Path) -> None:
    (root / "fast.md").write_text(FAST)
    (root / "full.md").write_text(FULL)
    (root / "rules.md").write_text(RULES)


def _lanes(**lane: object) -> list[dict[str, object]]:
    return [
        {
            "name": "full",
            "tracker": 4194,
            "extra": ["full.md"],
            "replace": ["fast.md"],
            "verify_scope": "Run the module's whole test file.",
            **lane,
        }
    ]


def _cfg(tmp_path: Path, lanes: list[dict[str, object]] | None = None, **review: object) -> config_mod.Config:
    _files(tmp_path)
    data: dict[str, object] = {"prompt": {"extra": ["fast.md", "rules.md"], "lane": _lanes() if lanes is None else lanes}}
    if review:
        data["review"] = review
    return config_mod.from_dict(data, root=tmp_path)


def _issue(n: int = 7, title: str = "Export button", labels: list[str] | None = None, body: str = "") -> Issue:
    return Issue(n, "OPEN", title, labels or [], body=body)


# -- configuration ----------------------------------------------------------------------------------
def test_lanes_parse_and_normalise(tmp_path):
    cfg = _cfg(tmp_path, [{"name": "a", "labels": "epic", "tracker": 12, "extra": "full.md", "replace": "fast.md"}])
    lane = cfg.prompt.lane[0]
    assert (lane.labels, lane.tracker, lane.extra, lane.replace) == (["epic"], [12], ["full.md"], ["fast.md"])
    assert _cfg(tmp_path, []).prompt.lane == []


@pytest.mark.parametrize(
    "lane, error",
    [
        ({"labels": ["x"]}, "needs a name"),
        ({"name": "a"}, "selector"),
        ({"name": "a", "title": "("}, "title: not a regular expression"),
        ({"name": "a", "body": "["}, "body: not a regular expression"),
        ({"name": "a", "tracker": "4194"}, "tracker"),
        ({"name": "a", "labels": ["x"], "replace": ["nope.md"]}, "not in \\[prompt\\] extra"),
        ({"name": "a", "labels": ["x"], "colour": "red"}, "unknown key"),
    ],
)
def test_bad_lanes_are_refused(tmp_path, lane, error):
    with pytest.raises(config_mod.ConfigError, match=error):
        _cfg(tmp_path, [lane])


def test_two_lanes_with_one_name_are_refused(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="two lanes"):
        _cfg(tmp_path, [{"name": "a", "labels": ["x"]}, {"name": "a", "labels": ["y"]}])


# -- selection --------------------------------------------------------------------------------------
def test_selectors(tmp_path):
    cfg = _cfg(
        tmp_path,
        [
            {"name": "by-label", "labels": ["Careful"]},
            {"name": "by-title", "title": "^epic:"},
            {"name": "by-body", "body": r"^Scope: full$"},
            {"name": "by-tracker", "tracker": [4194, 5000]},
        ],
    )

    def name(issue: Issue) -> str:
        lane = prompt_mod.select_lane(cfg, issue)
        return lane.name if lane else ""

    assert name(_issue(labels=["careful"])) == "by-label"  # labels are case-insensitive
    assert name(_issue(title="Epic: reports")) == "by-title"
    assert name(_issue(body="Intro\nScope: full\n")) == "by-body"
    assert name(_issue(body="Part of #4194.")) == "by-tracker"
    assert name(_issue(body="part of  #5000")) == "by-tracker"
    assert name(_issue(body="Part of #41940")) == ""  # another issue's children
    assert name(_issue(body="See #4194")) == ""
    assert name(_issue()) == ""  # the default is no lane
    assert name(_issue(labels=["careful"], body="Part of #4194")) == "by-label"  # the first match wins


# -- composition ------------------------------------------------------------------------------------
def test_a_lane_drops_and_appends_files_and_fills_LANE(tmp_path):
    cfg = _cfg(tmp_path)
    lane = cfg.prompt.lane[0]
    vals = prompt_mod.values(cfg, issue=7, title="T", branch="b", worktree=tmp_path / "7", lane=lane)
    body = prompt_mod.render(prompt_mod.template(cfg, lane=lane), vals)
    assert "Fast lane" not in body and "Full delivery" in body
    assert body.index("House rules") < body.index("Full delivery")  # after the project's own files
    assert "## This issue's lane: full\n\nRun the module's whole test file." in body

    plain = prompt_mod.render(
        prompt_mod.template(cfg), prompt_mod.values(cfg, issue=7, title="T", branch="b", worktree=tmp_path / "7")
    )
    assert "Fast lane" in plain and "Full delivery" not in plain and "This issue's lane" not in plain


def test_a_file_that_places_LANE_itself_is_not_given_a_second_copy(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "full.md").write_text("Verify like this: {{LANE}} (lane {{LANE_NAME}})\n")
    lane = cfg.prompt.lane[0]
    body = prompt_mod.render(
        prompt_mod.template(cfg, lane=lane),
        prompt_mod.values(cfg, issue=7, title="T", branch="b", worktree=tmp_path, lane=lane),
    )
    assert body.count("Run the module's whole test file.") == 1
    assert "Verify like this: Run the module's whole test file. (lane full)" in body
    # Without a lane the placeholders exist and are empty: a project file may use them unconditionally.
    vals = prompt_mod.values(cfg, issue=7, title="T", branch="b", worktree=tmp_path)
    assert vals["LANE"] == "" and vals["LANE_NAME"] == ""


def test_the_reviewer_checks_against_the_lane(tmp_path):
    cfg = _cfg(tmp_path)
    text = prompt_mod.review_template(cfg, lane=cfg.prompt.lane[0])
    assert "Full delivery" in text and "Fast lane" not in text and "**full** lane" in text and "{{LANE}}" in text
    assert "Fast lane" in prompt_mod.review_template(cfg)


# -- persistence --------------------------------------------------------------------------------------
def test_the_lane_is_chosen_once_and_kept_until_the_lanes_change(tmp_path):
    cfg = _cfg(tmp_path)
    rec = IssueRecord(issue=7)
    assert prompt_mod.settle_lane(cfg, rec, _issue(body="Part of #4194")) == ""
    assert rec.lane == "full" and rec.lane_digest
    assert any("prompt lane: full" in h for h in rec.history)

    # The issue's text changes; the lane does not (relaunches get the rules the first launch got).
    assert prompt_mod.settle_lane(cfg, rec, _issue(body="unrelated now")) == ""
    assert rec.lane == "full"

    # The lanes change: chosen again, and a different answer is reported.
    cfg.prompt.lane[0].tracker = [9999]
    change = prompt_mod.settle_lane(cfg, rec, _issue(body="Part of #4194"))
    assert rec.lane == "" and "from full to (none)" in change and change in rec.history[-1]

    # Changed again, same answer: nothing to report, the new configuration is recorded.
    cfg.prompt.lane[0].verify_scope = "something else"
    digest = rec.lane_digest
    assert prompt_mod.settle_lane(cfg, rec, _issue(body="Part of #4194")) == ""
    assert rec.lane_digest != digest


# -- end to end -------------------------------------------------------------------------------------------
def _world_lanes(world, tmp_path):  # type: ignore[no-untyped-def]
    _files(tmp_path)
    return world(prompt={"extra": [str(tmp_path / "fast.md")], "lane": _lanes(replace=[str(tmp_path / "fast.md")])})


def test_the_lane_is_persisted_and_relaunches_use_it(world, tmp_path):
    w = _world_lanes(world, tmp_path)
    w.issue(1, "nothing", "pr-green")
    w.forge.add_issue(1, "Issue 1", body="Part of #4194")
    w.issue(2, "pr-green")
    w.drive(1)
    first = w.prompts(1)[0]
    assert "Full delivery" in first and "Fast lane" not in first and "Run the module's whole test file." in first
    assert "Fast lane" in w.prompts(2)[0] and "Full delivery" not in w.prompts(2)[0]
    assert w.rec(1).lane == "full" and w.rec(2).lane == "" and w.rec(2).lane_digest

    # Someone edits the issue; the relaunch keeps the lane the first launch got.
    w.forge.add_issue(1, "Issue 1", body="no tracker any more")
    w.sup._issues.clear()
    w.drive(4)
    rec = w.rec(1)
    assert len(w.prompts(1)) == 2 and rec.lane == "full" and rec.pr
    issue = w.sup.issue(1, fresh=True)
    assert "Full delivery" in w.sup.render_prompt(rec, issue, rec.branch, Path(rec.worktree))

    from nohalt.cli import render_status

    assert "lanes: #1 full" in render_status(w.cfg)


def test_a_lane_changed_by_the_config_reaches_a_resumed_session(world, tmp_path):
    w = _world_lanes(world, tmp_path)
    w.issue(1, "pr-red", "push-green")
    w.drive(1)  # launched in no lane; its PR goes red, so the next tick hands it back to resume
    assert w.rec(1).lane == "" and w.rec(1).lane_digest
    w.forge.add_issue(1, "Issue 1", labels=["epic"])
    w.sup._issues.clear()
    w.cfg.prompt.lane[0].labels = ["epic"]  # as a reloaded nohalt.toml would
    w.drive(3)
    resumed = w.prompts(1)[1]
    assert "Your instructions changed" in resumed and "from (none) to full" in resumed
    assert "Full delivery" in resumed
    assert w.rec(1).lane == "full"
    assert any("prompt lane changed from (none) to full" in m for m in w.messages)


def test_nohalt_prompt_shows_the_lane(world, tmp_path, capsys, monkeypatch):
    from nohalt import cli

    w = _world_lanes(world, tmp_path)
    monkeypatch.setattr(cli, "_cfg", lambda _args: w.cfg)
    monkeypatch.setattr(cli, "_sup", lambda _cfg, quiet=False: w.sup)
    w.issue(1, "pr-green")
    w.forge.add_issue(1, "Issue 1", body="Part of #4194")
    assert cli.main(["prompt", "1"]) == 0
    out = capsys.readouterr()
    assert "# lane: full -- would be chosen at the next launch" in out.err
    assert "Full delivery" in out.out and "Fast lane" not in out.out
    assert not w.sup.state.exists(1)  # showing a prompt chooses nothing

    w.drive(2)
    w.forge.add_issue(1, "Issue 1", body="edited")  # the record's lane is what the next launch uses
    w.sup._issues.clear()
    assert cli.main(["prompt", "1"]) == 0
    out = capsys.readouterr()
    assert "# lane: full -- kept on the issue record" in out.err and "Full delivery" in out.out


# -- cross-model review ----------------------------------------------------------------------------------
def test_the_reviewer_is_chosen_from_the_authors_model(tmp_path):
    cfg = _cfg(
        tmp_path,
        model="claude-code:opus",
        fallbacks=["opencode:cloud/big"],
        models={
            "opencode:": "claude-code:sonnet",
            "opencode:local/": "claude-code:haiku",
            "claude-code:": "opencode:local/coder",
        },
    )
    assert reviewer_models(cfg, "opencode:local/coder")[0] == "claude-code:haiku"  # the most specific prefix wins
    assert reviewer_models(cfg, "opencode:cloud/big")[0] == "claude-code:sonnet"
    assert reviewer_models(cfg, "claude-code:sonnet") == ["opencode:local/coder", "claude-code:opus", "opencode:cloud/big"]
    # No mapping: model, then fallbacks -- minus the author's own model.
    assert reviewer_models(cfg, "command:x") == ["claude-code:opus", "opencode:cloud/big"]
    assert reviewer_models(cfg) == ["claude-code:opus", "opencode:cloud/big"]


def test_distinct_never_reviews_with_the_authors_model(tmp_path):
    cfg = _cfg(tmp_path, model="opencode:local/coder", fallbacks=["opencode:local/coder", "claude-code:sonnet"])
    assert reviewer_models(cfg, "opencode:local/coder") == ["claude-code:sonnet"]
    cfg.review.distinct = False
    assert reviewer_models(cfg, "opencode:local/coder") == ["opencode:local/coder", "claude-code:sonnet"]
    cfg = _cfg(tmp_path, models={"opencode:": "opencode:local/coder"}, model="claude-code:sonnet")
    assert reviewer_models(cfg, "opencode:local/coder") == ["claude-code:sonnet"]  # a mapping to itself is skipped too


def test_review_models_are_checked(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="must name its agent"):
        _cfg(tmp_path, models={"opencode:": "sonnet"})


REVIEW = {"enabled": True, "all": True}


def test_an_author_on_one_model_is_reviewed_on_another(world):
    w = world(review={**REVIEW, "models": {"command:f": "command:other-family", "command:": "command:generic"}})
    w.issue(1, "pr-green")
    w.review(1, "approve")
    w.drive(4)
    r = w.rec(1)
    assert r.model == "command:fake" and r.review_model == "command:other-family" and r.status == READY
    comments = [c[1] for c in w.forge.data().get("pull_comments", [])]
    assert any("reviewer `command:other-family`" in c and "author `command:fake`" in c for c in comments)


def test_the_authors_model_is_skipped_for_the_next_fallback(world):
    w = world(review={**REVIEW, "model": "command:fake", "fallbacks": ["command:second"]})
    w.issue(1, "pr-green")
    w.review(1, "approve")
    w.drive(4)
    assert w.rec(1).review_model == "command:second" and w.rec(1).status == READY


def test_no_reviewer_but_the_author_goes_to_a_human(world):
    w = world(review=REVIEW)  # the reviewer defaults to [models] default: the author's model
    w.issue(1, "pr-green")
    w.review(1, "approve")
    w.drive(4)
    r = w.rec(1)
    assert r.status == BLOCKED and "no reviewer model differs from the author's (command:fake)" in r.blocked_reason
    assert w.review_prompts(1) == []


def test_the_reviewer_is_given_the_authors_lane(world, tmp_path):
    _files(tmp_path)
    w = world(
        prompt={"extra": [str(tmp_path / "fast.md")], "lane": _lanes(replace=[str(tmp_path / "fast.md")])},
        review={**REVIEW, "model": "command:reviewer"},
    )
    w.issue(1, "pr-green")
    w.forge.add_issue(1, "Issue 1", body="Part of #4194")
    w.review(1, "approve")
    w.drive(4)
    text = w.review_prompts(1)[0]
    assert "**full** lane" in text and "Full delivery" in text and "Fast lane" not in text
    assert "Run the module's whole test file." in text
