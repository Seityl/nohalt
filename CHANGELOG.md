# Changelog

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
