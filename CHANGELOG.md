# Changelog

## [0.2.2](https://github.com/Seityl/nohalt/compare/v0.2.1...v0.2.2) (2026-10-30)


### Features

* **models:** pinned issues may start on a pool cooling down for a usage warning ([#83](https://github.com/Seityl/nohalt/issues/83)) ([1d46e3d](https://github.com/Seityl/nohalt/commit/1d46e3d296c140caa6b7123b2be2ef1cd11c9b18))
* **review:** reviews may run on a pool cooling down for a usage warning ([#77](https://github.com/Seityl/nohalt/issues/77)) ([7429c13](https://github.com/Seityl/nohalt/commit/7429c13f02c0ce33b27f84b24506d65d1f251588))
* **train:** approved PRs go first, and join a batch whose CI has not started ([#72](https://github.com/Seityl/nohalt/issues/72)) ([786b052](https://github.com/Seityl/nohalt/commit/786b052d78bd1b94ae0dd0e0a6eb752f7a1c542d))


### Bug Fixes

* **async:** read Frappe's test log and distrust a failure count below the summary's ([#84](https://github.com/Seityl/nohalt/issues/84)) ([909ab5e](https://github.com/Seityl/nohalt/commit/909ab5ec0e9c678390318f03cca7cf38f850f3db))
* **review:** a PR-description edit is a change the review gate re-reviews ([#75](https://github.com/Seityl/nohalt/issues/75)) ([7cc325a](https://github.com/Seityl/nohalt/commit/7cc325accf8ed25815a239f6b6e4bbb50f5cf6e2))
* **train:** unknown mergeability no longer drops a PR silently; left-to-batch logged once ([#85](https://github.com/Seityl/nohalt/issues/85)) ([b98aa8a](https://github.com/Seityl/nohalt/commit/b98aa8accc316e186fcb87cb7fdb624d0e56c232))
* **watch:** a conflicting PR in waiting_ci is refreshed or handed back, not left waiting ([#86](https://github.com/Seityl/nohalt/issues/86)) ([1e5ae74](https://github.com/Seityl/nohalt/commit/1e5ae74b9501d6fa4757d1df4da01e6028f1476d))

## [0.2.1](https://github.com/Seityl/nohalt/compare/v0.2.0...v0.2.1) (2026-10-28)


### Features

* **integrate:** a base pipeline with no verdict files an issue and keeps merging ([#35](https://github.com/Seityl/nohalt/issues/35)) ([00383e3](https://github.com/Seityl/nohalt/commit/00383e3371cd03ad10040d1ba66dc6bf38ce4815))
* **integrate:** a post-deploy check, and "shipped" means live ([#68](https://github.com/Seityl/nohalt/issues/68)) ([a48f2f3](https://github.com/Seityl/nohalt/commit/a48f2f312b80f7773e993c96b2b1b221aada5826))
* **integrate:** on a red base the train can carry the fix and every green PR ([#38](https://github.com/Seityl/nohalt/issues/38)) ([fa20405](https://github.com/Seityl/nohalt/commit/fa20405b5c50eae1f34c797a1ad016b16de7ac5c))
* **integrate:** refresh PRs whose conflicts with the base are all bookkeeping ([#41](https://github.com/Seityl/nohalt/issues/41)) ([5c97afa](https://github.com/Seityl/nohalt/commit/5c97afaa4136fa355db397de6a6c47986b186393))
* **integrate:** watch slow workflows without gating; one self-closing issue each; no-change is a result ([#47](https://github.com/Seityl/nohalt/issues/47)) ([39a9544](https://github.com/Seityl/nohalt/commit/39a95441d19a4ba2830a696b9883e6305c97ede5))
* **models:** endpoint-down state, usage-aware routing and pause expiry ([#63](https://github.com/Seityl/nohalt/issues/63)) ([d953a18](https://github.com/Seityl/nohalt/commit/d953a1803030a609b223237a8a652e932e96de84))
* **models:** overflow to an idle pool when the routed pool is full ([#39](https://github.com/Seityl/nohalt/issues/39)) ([#44](https://github.com/Seityl/nohalt/issues/44)) ([666b087](https://github.com/Seityl/nohalt/commit/666b087c814c35ac1e79c2c9d5e09fc69152484a))
* **prompt,review:** per-issue prompt lanes; reviewers chosen from the author's model ([#61](https://github.com/Seityl/nohalt/issues/61)) ([7cf79e5](https://github.com/Seityl/nohalt/commit/7cf79e516bb6e37fc29eb28f366ffcffd77e2a57))
* **review:** a review gate -- a second agent checks green PRs against their issue ([#48](https://github.com/Seityl/nohalt/issues/48)) ([e79b4c6](https://github.com/Seityl/nohalt/commit/e79b4c62c32fbf97b97cc7771c6fca01d525abaf))
* **supervisor:** start dependants early on a synthetic base of their dependencies' green PRs ([#66](https://github.com/Seityl/nohalt/issues/66)) ([d4fe72a](https://github.com/Seityl/nohalt/commit/d4fe72a2d3a81e9a419d38dfc6e0ecc81bdd8958))
* **train:** release windows -- ship on a schedule, with a hotfix lane ([#55](https://github.com/Seityl/nohalt/issues/55)) ([17f05c0](https://github.com/Seityl/nohalt/commit/17f05c0089904982e67c4cbb6d20c04deac8f3c3))


### Bug Fixes

* **review:** stop the conflict refresh and the review gate from livelocking ([#70](https://github.com/Seityl/nohalt/issues/70)) ([1b06247](https://github.com/Seityl/nohalt/commit/1b06247286bb809026b1c58048d39cf8bdfde28a))
* **supervisor:** queue a reopened issue again instead of leaving it done ([#59](https://github.com/Seityl/nohalt/issues/59)) ([0bcece2](https://github.com/Seityl/nohalt/commit/0bcece26aedf2dcb840f1a927d5cbbf8b8b0e4fc))
* **supervisor:** switch to a newly installed nohalt by re-executing; preload every module ([#67](https://github.com/Seityl/nohalt/issues/67)) ([bfddffe](https://github.com/Seityl/nohalt/commit/bfddffe7e2a73a88aae314d3f7f0d5e63d3b23a1))

## [0.2.0](https://github.com/Seityl/nohalt/compare/v0.1.0...v0.2.0) (2026-10-23)


### Features

* ignore_checks, and adopt existing PRs instead of relaunching ([cafb259](https://github.com/Seityl/nohalt/commit/cafb259f6a04e8648f0c763706678eb4712d5ab6))
* package-manager guard for shared paths, doctor check drift, verify.pythonpath ([ca554ea](https://github.com/Seityl/nohalt/commit/ca554ea525cb4c4ec2a86ad6f2bccec98b03909b))


### Bug Fixes

* **integrate:** a fix-forward issue's own PR may merge while the gate is closed ([7d7952c](https://github.com/Seityl/nohalt/commit/7d7952cdf2208cf1cb0334e5c7d6a05ecb56b920))
* **integrate:** base not green while a workflow run is unfinished; log the checks behind each verdict ([e41c6cd](https://github.com/Seityl/nohalt/commit/e41c6cd34bc5fc9d1e51e963e5c7070629b9ee74))
* **integrate:** ignore_checks applies to PRs only; cancelled base runs are no verdict ([c2ba15b](https://github.com/Seityl/nohalt/commit/c2ba15be9e49ca3bba3e5b32a664cde33cb13e6a))
* train closes the PRs it carried; dry run writes no state; doctor nits ([dca65e1](https://github.com/Seityl/nohalt/commit/dca65e169c85487b2c00c726fd741bb4aa8da535))

## 0.1.0 (2026-10-22)


### Features

* autonomous agent supervisor for GitHub and GitLab issues ([c401ee5](https://github.com/Seityl/nohalt/commit/c401ee527198bb896cdb92c604b245a6a781e36c))


### Bug Fixes

* GitLab dogfood findings -- probe in launch mode, setup failures cost no attempt, drafts marked ready ([b67644d](https://github.com/Seityl/nohalt/commit/b67644d06a61561486ef470de3b0f352f88cf730))
* **prompt:** read GitHub issues over REST, not gh issue view ([ee0f8b4](https://github.com/Seityl/nohalt/commit/ee0f8b4e0b2c9155701750b461ade1f9e28f8262))
* **prompt:** verify command works when nohalt is not on PATH ([d462893](https://github.com/Seityl/nohalt/commit/d462893767c03cf7d6ff59045d7e458a7b33e871))


### Documentation

* architecture, configuration, stacks, safety and lessons learnt ([51bdd1b](https://github.com/Seityl/nohalt/commit/51bdd1baaa92472b920e909d06b1c845b7641ae0))

## Changelog

All notable changes to this project are documented here. This file is maintained by
[release-please](https://github.com/googleapis/release-please) from conventional commits.
