"""``nohalt.toml``: one file per project, every tunable in one place.

Every value that was once hard-coded in a script -- repository, branch, paths,
models, caps, timeouts, labels -- lives here. Unknown keys are an error: a typo
that silently falls back to a default is how a guard quietly stops guarding.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .util import parse_duration

CONFIG_NAME = "nohalt.toml"


class ConfigError(ValueError):
    pass


@dataclass
class ProjectCfg:
    repo: str = ""  # owner/name (GitHub), group[/subgroup]/name (GitLab), or local:/path.json
    forge: str = "github"  # github | gitlab
    forge_url: str = ""  # GitLab: https://gitlab.example.com (default https://gitlab.com or $GITLAB_HOST)
    token_env: str = "GITLAB_TOKEN"  # GitLab: the variable holding an `api`-scoped token
    path: Path = Path(".")  # the main clone; worktrees are cut from it
    base_branch: str = "main"
    remote: str = "origin"
    state_dir: Path = Path(".nohalt")
    worktrees: Path = Path("")  # default: <state_dir>/work
    shared_paths: list[str] = field(default_factory=list)  # symlinked into worktrees, e.g. node_modules
    branch_prefixes: dict[str, str] = field(default_factory=lambda: {"bug": "fix"})
    default_prefix: str = "feature"


@dataclass
class QueueCfg:
    file: Path = Path("queue.txt")
    blocked_label: str = "nohalt-blocked"
    held_labels: list[str] = field(default_factory=lambda: ["needs-human"])
    tick: int = 60
    poll_when_idle: int = 600


@dataclass
class AgentsCfg:
    max_parallel: int = 2
    min_free_mb: int = 2000
    phase_timeout: int = 4 * 3600
    long_timeout: int = 8 * 3600
    long_labels: list[str] = field(default_factory=list)
    max_attempts: int = 2
    provider_retries: int = 5
    provider_backoff: int = 120
    timeout_continues: int = 3
    hung_after: int = 30 * 60
    revive_limit: int = 2
    revive_grace: int = 15 * 60
    ci_rounds: int = 3
    log_max_mb: int = 20
    title_prefix: str = "nohalt"
    pools: dict[str, int] = field(default_factory=dict)  # model prefix -> own slot cap
    options: dict[str, dict[str, Any]] = field(default_factory=dict)  # per agent adapter, e.g. [agents.options.opencode]
    copy_files: list[str] = field(default_factory=list)  # copied into each agent's data dir, e.g. opencode auth.json
    early_start: bool = False  # start a dependant on its dependencies' unmerged PRs (a synthetic base)
    early_start_on: str = "green"  # green | approved: the state every open dependency's PR must be in


@dataclass
class RouteRule:
    model: str
    labels: list[str] = field(default_factory=list)  # any label matches (case-insensitive)
    title: str = ""  # regex on the title


@dataclass
class ModelsCfg:
    default: str = "opencode:anthropic/claude-sonnet-4-5"
    fallbacks: list[str] = field(default_factory=list)
    premium: list[str] = field(default_factory=list)  # need `nohalt route <n> <model> --approve`
    premium_fallback: str = ""
    probe: bool = True
    probe_ttl: int = 300
    route: list[RouteRule] = field(default_factory=list)
    overflow_after: int = 0  # waited this long for a slot: start on an overflow model. 0: never
    overflow_to: list[str] = field(default_factory=list)  # default: fallbacks minus premium
    overflow_reserve: dict[str, int] = field(default_factory=dict)  # pool prefix -> slots overflow may not take
    down_after: int = 3  # issues with endpoint errors within down_window, or probes failed in a row: model down. 0: never
    down_window: int = 10 * 60
    down_probe: int = 2 * 60  # a down model is probed again after this, doubling ...
    down_probe_max: int = 30 * 60  # ... up to this
    down_block_after: int = 6 * 3600  # block for endpoint reasons only when the model and every fallback were down this long
    usage_cooldown: int = 3600  # a pool's cool-down after a usage signal that reports no reset time
    pinned_during_cooldown: str = "none"  # "warning": a pinned issue may start on a pool cooling down for a usage warning (#81)


@dataclass
class BudgetCfg:
    enabled: bool = False
    per_issue: float = 15.0
    total: float = 100.0
    providers: list[str] = field(default_factory=lambda: ["anthropic"])
    fallback_model: str = ""
    # USD per million tokens
    rates: dict[str, float] = field(
        default_factory=lambda: {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}
    )


@dataclass
class VerifySlot:
    name: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class VerifyCfg:
    slots: list[VerifySlot] = field(default_factory=lambda: [VerifySlot("default")])
    placeholder: str = ""  # a token in agents' commands replaced by the slot name, e.g. "test-site.local"
    refuse: list[str] = field(default_factory=list)  # substrings a verify command may not contain
    up: str = ""  # bring the test environment up (idempotent)
    reset: str = ""  # put it at the schema of the commit under test
    env: str = ""  # prints KEY=VALUE lines to export
    pythonpath_worktree: bool = False
    pythonpath: list[str] = field(default_factory=list)  # worktree subdirectories put first on PYTHONPATH (src layouts)
    clean_pycache: bool = True


@dataclass
class TrainCfg:
    branch_prefix: str = "release/"
    version_files: list[dict[str, str]] = field(default_factory=list)  # {path, pattern}
    tag_prefix: str = "v"
    after_version: str = ""  # a command run in the release tree after stamping (regenerate docs, ...)
    red_retries: int = 1
    batch_pause_file: str = "BATCH_PAUSE"
    windows: list[str] = field(default_factory=list)  # daily "HH:MM-HH:MM" ranges a normal batch may open in; empty: always
    timezone: str = ""  # IANA name for windows; default: the system's local time
    days: list[str] = field(default_factory=list)  # weekdays windows apply on ("mon".."sun"); empty: every day
    per_window: int = 1  # normal releases merged per window instance; 0: unlimited
    hotfix_labels: list[str] = field(default_factory=lambda: ["hotfix"])  # these may release outside a window
    hotfix_priority: bool = True  # issues in <state>/priority count as hotfixes too
    approved_first: bool = True  # with [review]: approved PRs batch (or merge, direct) first and may join an open batch


@dataclass
class IntegrateCfg:
    mode: str = "none"  # none | direct | train
    required_checks: list[str] = field(default_factory=list)  # empty: every check run must pass
    ignore_checks: list[str] = field(default_factory=list)  # PRs only: never count, e.g. a suite cancelled on PRs by policy
    base_ignore_checks: list[str] = field(default_factory=list)  # base branch after a merge: checks that never count there
    base_cancelled_limit: int = 3  # cancelled base runs in a row before the base is given up on as "no verdict"
    base_no_verdict: str = "continue"  # then: "continue" (file an issue, keep merging) or "hold" (stop for a human)
    red_base: str = (
        "fix_only"  # on a red base: "fix_only" merges only the fix; "with_fix" (train) carries the fix and every green PR
    )
    merge_method: str = "squash"
    cd_workflow: str = ""
    ci_workflow: str = ""
    async_workflows: list[str] = field(default_factory=list)  # watched on the base, never gating: one self-closing issue each
    async_label: str = "async-red"  # put on the issue an async workflow's red run opens
    cd_timeout: int = 90 * 60
    base_pipeline_grace: int = 10 * 60  # no pipeline at all on the base after a merge for this long: nothing to watch
    deploy: str = ""  # run after a merge (direct) or a tagged release (train)
    post_deploy_check: str = ""  # run after a deploy (or a green CD workflow); non-zero: DEPLOY_BLOCKED and an issue
    post_deploy_retries: int = 2  # further tries of a failing post_deploy_check (warm-up), post_deploy_delay apart
    post_deploy_delay: int = 30
    live_version: str = ""  # prints the live version or SHA; merged issues close only once it contains them
    live_timeout: int = 0  # released and still not live after this long: a warning; 0: cd_timeout
    fix_forward: bool = True
    cancel_superseded: bool = False
    network_errors: list[str] = field(
        default_factory=lambda: [
            "Could not resolve host",
            "no route to host",
            "ECONNRESET",
            "ETIMEDOUT",
            "EAI_AGAIN",
            "Connection timed out",
        ]
    )
    mergers: dict[str, str] = field(default_factory=dict)  # glob -> driver
    refresh_conflicts: bool = True  # merge the base into a conflicting PR when drivers settle every file (INT-12)
    train: TrainCfg = field(default_factory=TrainCfg)


@dataclass
class StackCfg:
    name: str = "generic"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptLane:
    """A per-issue variant of the author prompt, chosen once per issue and kept on its record."""

    name: str = ""
    labels: list[str] = field(default_factory=list)  # any label matches (case-insensitive)
    title: str = ""  # regex on the title
    body: str = ""  # regex on the issue's description, e.g. "Part of #4194"
    tracker: list[int] = field(default_factory=list)  # children of these issues ("Part of #N" in their body)
    extra: list[str] = field(default_factory=list)  # prompt files appended after [prompt] extra
    replace: list[str] = field(default_factory=list)  # [prompt] extra entries this lane leaves out
    verify_scope: str = ""  # the {{LANE}} placeholder


@dataclass
class PromptCfg:
    inline_issue: bool = False  # put the issue's text and comments in the prompt (agents need no forge CLI to read it)
    template: str = "builtin:core"
    extra: list[str] = field(default_factory=list)
    vars: dict[str, str] = field(default_factory=dict)
    lane: list[PromptLane] = field(default_factory=list)  # [[prompt.lane]]: the first match wins; none: no lane


@dataclass
class ReviewCfg:
    enabled: bool = False
    all: bool = False  # every issue needs review
    labels: list[str] = field(default_factory=list)  # issues carrying any of these need review
    title: str = ""  # regex on the title: matching issues need review
    model: str = ""  # the reviewer, <agent>:<model>; default: [models] default
    models: dict[str, str] = field(default_factory=dict)  # author model prefix -> reviewer; the longest prefix wins
    distinct: bool = True  # never review a PR with the exact model that wrote it
    fallbacks: list[str] = field(default_factory=list)  # tried in order when the reviewer model does not answer
    max_rounds: int = 2  # "changes" verdicts handed back before the issue goes to a human
    attempts: int = 3  # reviews that ended without a verdict (timeout, hang, no verdict) before a human is asked
    timeout: int = 3600
    prompt: list[str] = field(default_factory=list)  # extra prompt files for the reviewer
    during_cooldown: str = "none"  # "warning": a reviewer may start on a pool cooling down for a usage warning (#76)


@dataclass
class Config:
    root: Path
    project: ProjectCfg = field(default_factory=ProjectCfg)
    queue: QueueCfg = field(default_factory=QueueCfg)
    agents: AgentsCfg = field(default_factory=AgentsCfg)
    models: ModelsCfg = field(default_factory=ModelsCfg)
    budget: BudgetCfg = field(default_factory=BudgetCfg)
    verify: VerifyCfg = field(default_factory=VerifyCfg)
    integrate: IntegrateCfg = field(default_factory=IntegrateCfg)
    stack: StackCfg = field(default_factory=StackCfg)
    prompt: PromptCfg = field(default_factory=PromptCfg)
    review: ReviewCfg = field(default_factory=ReviewCfg)
    source: Path | None = None
    source_hash: str = ""  # of the file as loaded: the supervisor reloads when it changes

    # -- resolved paths --------------------------------------------------------
    @property
    def repo_path(self) -> Path:
        return self._abs(self.project.path)

    @property
    def state_dir(self) -> Path:
        return self._abs(self.project.state_dir)

    @property
    def worktree_root(self) -> Path:
        wt = self.project.worktrees
        return self._abs(wt) if str(wt) not in ("", ".") else self.state_dir / "work"

    @property
    def queue_file(self) -> Path:
        return self._abs(self.queue.file)

    def _abs(self, p: Path) -> Path:
        p = Path(p).expanduser()
        return p if p.is_absolute() else (self.root / p).resolve()


_DURATIONS = {
    "tick",
    "poll_when_idle",
    "phase_timeout",
    "long_timeout",
    "provider_backoff",
    "hung_after",
    "revive_grace",
    "probe_ttl",
    "cd_timeout",
    "base_pipeline_grace",
    "post_deploy_delay",
    "live_timeout",
    "overflow_after",
    "timeout",
    "down_window",
    "down_probe",
    "down_probe_max",
    "down_block_after",
    "usage_cooldown",
}
_OFF_WHEN_EMPTY = {"overflow_after", "live_timeout"}


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"[{where}] unknown key {key!r} (known: {', '.join(sorted(known))})")
        f = known[key]
        if key in _OFF_WHEN_EMPTY and value == "":
            value = 0
        elif key in _DURATIONS:
            try:
                value = parse_duration(value)
            except ValueError as e:
                raise ConfigError(f"[{where}] {key}: {e}") from None
        elif f.type in ("Path", "Path | None"):
            value = Path(value)
        kwargs[key] = value
    return cls(**kwargs)


def from_dict(data: dict[str, Any], root: Path, source: Path | None = None) -> Config:
    data = dict(data)
    sections = {
        "project": ProjectCfg,
        "queue": QueueCfg,
        "agents": AgentsCfg,
        "budget": BudgetCfg,
        "review": ReviewCfg,
    }
    cfg = Config(root=root, source=source)
    for name, cls in sections.items():
        if name in data:
            setattr(cfg, name, _build(cls, data.pop(name), name))

    if "prompt" in data:
        p = dict(data.pop("prompt"))
        lanes = p.pop("lane", [])
        if not isinstance(lanes, list):
            raise ConfigError("[prompt] lane: write each lane as a [[prompt.lane]] table")
        cfg.prompt = _build(PromptCfg, p, "prompt")
        cfg.prompt.lane = [_build(PromptLane, ln, "prompt.lane") for ln in lanes]
    if "models" in data:
        m = dict(data.pop("models"))
        routes = [_build(RouteRule, r, "models.route") for r in m.pop("route", [])]
        cfg.models = _build(ModelsCfg, m, "models")
        cfg.models.route = routes
    if "verify" in data:
        v = dict(data.pop("verify"))
        slots = v.pop("slots", None)
        cfg.verify = _build(VerifyCfg, v, "verify")
        if slots is not None:
            cfg.verify.slots = [VerifySlot(s) if isinstance(s, str) else _build(VerifySlot, s, "verify.slots") for s in slots]
            if not cfg.verify.slots:
                raise ConfigError("[verify] slots: at least one slot is needed")
    if "integrate" in data:
        i = dict(data.pop("integrate"))
        train = i.pop("train", {})
        cfg.integrate = _build(IntegrateCfg, i, "integrate")
        cfg.integrate.train = _build(TrainCfg, train, "integrate.train")
    if "stack" in data:
        s = dict(data.pop("stack"))
        name = s.pop("name", "generic")
        cfg.stack = StackCfg(name=name, options=s)
    if data:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(data))}")
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    repo = cfg.project.repo
    if cfg.project.forge not in ("github", "gitlab"):
        raise ConfigError(f"[project] forge must be github or gitlab, got {cfg.project.forge!r}")
    if repo and not repo.startswith("local:"):
        if cfg.project.forge == "github" and repo.count("/") != 1:
            raise ConfigError(f"[project] repo must be owner/name, got {repo!r}")
        if cfg.project.forge == "gitlab" and (repo.count("/") < 1 or repo.startswith("/") or repo.endswith("/")):
            raise ConfigError(f"[project] repo must be group[/subgroup]/name, got {repo!r}")
    if cfg.integrate.mode not in ("none", "direct", "train"):
        raise ConfigError(f"[integrate] mode must be none, direct or train, got {cfg.integrate.mode!r}")
    if cfg.integrate.red_base not in ("fix_only", "with_fix"):
        raise ConfigError(f"[integrate] red_base must be fix_only or with_fix, got {cfg.integrate.red_base!r}")
    if cfg.integrate.base_no_verdict not in ("continue", "hold"):
        raise ConfigError(f"[integrate] base_no_verdict must be continue or hold, got {cfg.integrate.base_no_verdict!r}")
    gating = {w for w in (cfg.integrate.ci_workflow, cfg.integrate.cd_workflow) if w}
    both = sorted(gating & set(cfg.integrate.async_workflows))
    if both:
        raise ConfigError(
            f"[integrate] async_workflows: {', '.join(both)} is also ci_workflow/cd_workflow; a workflow gates or it does not"
        )
    if cfg.integrate.async_workflows and not cfg.integrate.async_label:
        raise ConfigError("[integrate] async_label must not be empty while async_workflows is set")
    n = cfg.integrate.post_deploy_retries
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ConfigError(f"[integrate] post_deploy_retries must be a whole number, 0 for none, got {n!r}")
    if cfg.integrate.merge_method not in ("merge", "squash", "rebase"):
        raise ConfigError("[integrate] merge_method must be merge, squash or rebase")
    if cfg.agents.max_parallel < 1:
        raise ConfigError("[agents] max_parallel must be at least 1")
    if cfg.agents.early_start_on not in ("green", "approved"):
        raise ConfigError(f"[agents] early_start_on must be green or approved, got {cfg.agents.early_start_on!r}")
    if cfg.agents.max_attempts < 1:
        raise ConfigError("[agents] max_attempts must be at least 1")
    if cfg.review.max_rounds < 0 or cfg.review.attempts < 1:
        raise ConfigError("[review] max_rounds must be at least 0 and attempts at least 1")
    if cfg.models.pinned_during_cooldown not in ("none", "warning"):
        raise ConfigError(f"[models] pinned_during_cooldown must be none or warning, got {cfg.models.pinned_during_cooldown!r}")
    if cfg.review.during_cooldown not in ("none", "warning"):
        raise ConfigError(f"[review] during_cooldown must be none or warning, got {cfg.review.during_cooldown!r}")
    if isinstance(cfg.review.prompt, str):
        cfg.review.prompt = [cfg.review.prompt]
    if cfg.review.title:
        try:
            re.compile(cfg.review.title)
        except re.error as e:
            raise ConfigError(f"[review] title: not a regular expression: {e}") from None
    _validate_lanes(cfg)
    reviewers = [cfg.review.model, *cfg.review.fallbacks] if cfg.review.model else list(cfg.review.fallbacks)
    reviewers += list(cfg.review.models.values())
    models = cfg.models
    for model in [models.default, *models.fallbacks, *models.overflow_to, *(r.model for r in models.route), *reviewers]:
        if ":" not in model:
            raise ConfigError(f"model {model!r} must name its agent, e.g. 'opencode:provider/model'")
    for pool, n in models.overflow_reserve.items():
        if pool not in cfg.agents.pools:
            raise ConfigError(f"[models] overflow_reserve: {pool!r} is not one of [agents] pools")
        if not isinstance(n, int) or n < 0:
            raise ConfigError(f"[models] overflow_reserve: {pool!r} must be a whole number of slots, got {n!r}")
    if not isinstance(models.down_after, int) or isinstance(models.down_after, bool) or models.down_after < 0:
        raise ConfigError(f"[models] down_after must be a whole number, 0 to turn it off, got {models.down_after!r}")
    for name, opts in cfg.agents.options.items():
        for key in ("usage_limit", "usage_warning"):
            pats = opts.get(key) if isinstance(opts, dict) else None
            if pats is None:
                continue
            if isinstance(pats, str):
                opts[key] = pats = [pats]
            for pat in pats:
                try:
                    re.compile(pat)
                except (re.error, TypeError) as e:
                    raise ConfigError(f"[agents.options.{name}] {key}: {pat!r} is not a regular expression: {e}") from None
    from .integrate.mergers import DRIVERS
    from .integrate.windows import Schedule, WindowError

    t = cfg.integrate.train
    for key in ("windows", "days", "hotfix_labels"):
        if isinstance(getattr(t, key), str):
            setattr(t, key, [getattr(t, key)])
    try:
        Schedule(t.windows, t.timezone, t.days)
    except WindowError as e:
        raise ConfigError(f"[integrate.train] {e}") from None
    if not isinstance(t.per_window, int) or isinstance(t.per_window, bool) or t.per_window < 0:
        raise ConfigError(f"[integrate.train] per_window must be a whole number, 0 for unlimited, got {t.per_window!r}")

    for glob, driver in cfg.integrate.mergers.items():
        if driver not in DRIVERS:
            raise ConfigError(f"[integrate.mergers] {glob!r}: unknown driver {driver!r} (known: {', '.join(DRIVERS)})")


def _validate_lanes(cfg: Config) -> None:
    seen: set[str] = set()
    for i, lane in enumerate(cfg.prompt.lane, 1):
        where = f"[[prompt.lane]] {lane.name or '#' + str(i)}"
        for key in ("labels", "extra", "replace"):
            if isinstance(getattr(lane, key), str):
                setattr(lane, key, [getattr(lane, key)])
        if isinstance(lane.tracker, int) and not isinstance(lane.tracker, bool):
            lane.tracker = [lane.tracker]
        if not lane.name:
            raise ConfigError(f"{where}: every lane needs a name (it is kept on the issue record)")
        if lane.name in seen:
            raise ConfigError(f"{where}: two lanes are called {lane.name!r}")
        seen.add(lane.name)
        if not (lane.labels or lane.title or lane.body or lane.tracker):
            raise ConfigError(f"{where}: give it a selector (labels, title, body or tracker)")
        for key in ("title", "body"):
            try:
                re.compile(getattr(lane, key))
            except re.error as e:
                raise ConfigError(f"{where}: {key}: not a regular expression: {e}") from None
        if any(isinstance(t, bool) or not isinstance(t, int) or t < 1 for t in lane.tracker):
            raise ConfigError(f"{where}: tracker must be an issue number, or a list of them")
        unknown = [r for r in lane.replace if r not in cfg.prompt.extra]
        if unknown:
            raise ConfigError(f"{where}: replace {', '.join(map(repr, unknown))}: not in [prompt] extra")


def load(path: Path | str | None = None) -> Config:
    """Load ``nohalt.toml`` from ``path``, or search upwards from the cwd."""
    if path is None:
        here = Path.cwd()
        for d in [here, *here.parents]:
            if (d / CONFIG_NAME).is_file():
                path = d / CONFIG_NAME
                break
        else:
            raise ConfigError(f"no {CONFIG_NAME} here or in any parent directory (run `nohalt init`)")
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode())
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    cfg = from_dict(data, root=path.parent, source=path)
    cfg.source_hash = digest(raw)
    return cfg


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fingerprint(path: Path) -> str:
    """The hash of the file as it is now ("" if it cannot be read)."""
    try:
        return digest(path.read_bytes())
    except OSError:
        return ""
