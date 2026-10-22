# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through GitHub's security advisories:

**https://github.com/Seityl/nohalt/security/advisories/new**

Include what you found, how to reproduce it, and the impact you expect. You should get an acknowledgement within a few days. Fixes are released as patch versions, and credited in the advisory unless you prefer otherwise.

## Supported versions

nohalt is pre-1.0: only the latest release receives fixes.

## Scope

nohalt runs AI coding agents with write access to your repository and, if you configure it, the ability to merge and deploy. In scope:

- a way for an agent to get around the guard shims without it being logged;
- nohalt itself merging, deploying, or declaring an issue done when it should not;
- credentials leaking into prompts, logs, issue comments or the state directory;
- the verification lock letting a command reach past its test environment when a `refuse` rule says it should not.

Out of scope: what an agent with a write token can do by itself through the API. The shims are speed bumps with a log, not a sandbox. Branch protection on the forge is the hard control. Read [docs/SAFETY.md](docs/SAFETY.md) before running nohalt.
