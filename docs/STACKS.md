# Stacks

A stack describes a project's test environment: what agents verify against, how that environment is brought to the commit under test, and which bookkeeping files conflict on every merge. The core knows nothing about any framework.

## Why there is a lock at all

Agents edit in parallel, and each has its own worktree. They **verify** through `nohalt verify <worktree> -- '<command>'`, which hands out one test slot at a time per slot. The reason differs by project, so write yours down in the config. A lock nobody can explain gets deleted by the next person who wants things faster.

- **A singular test database**: two test runs against it truncate each other's fixtures, and the failures read as code faults.
- **A framework that builds assets for every site at once**: two agents verifying together each test the other's JavaScript.
- **A small machine**: two concurrent builds and two test runs on two cores go slower, not faster.

A lock can survive a port to a new project for a *different* reason. Document the new one.

## generic

The test environment is described by three shell hooks, each run in the agent's worktree with the slot's `env` and `NOHALT_SLOT` / `NOHALT_WORKTREE` set:

```toml
[verify]
# One Postgres and one Redis on non-standard loopback ports, mirroring the CI
# service definitions exactly. If CI's services change, change these in the same commit.
up    = "./scripts/testenv.sh up"      # idempotent; wait for readiness, not just "container started"
reset = "./scripts/testenv.sh reset"   # drop the schema and re-run migrations at this commit
env   = "./scripts/testenv.sh env"     # prints DATABASE_URL=..., REDIS_URL=...
refuse = ["--all-databases", "docker system prune"]
```

Lessons that apply here:

- **Mirror CI exactly.** A test environment that disagrees with CI gives confident wrong answers.
- **Readiness is not existence.** "Container up" is not "Postgres answering". Wait on a real readiness check (`pg_isready`), with a cap.
- **Bind to non-standard loopback ports.** On a shared host, 5432/6379 may belong to another project, and connecting to a stranger's database loses someone's data.
- **Migrate before measuring.** A shared database wears the schema of whichever branch migrated it last.

For more parallelism, give each slot its own database and list several slots:

```toml
[verify]
slots = [
  { name = "db1", env = { DATABASE_URL = "postgresql://localhost:55432/test1" } },
  { name = "db2", env = { DATABASE_URL = "postgresql://localhost:55432/test2" } },
]
```

## frappe

For Frappe / ERPNext apps on a bench that may also serve sites people use:

```toml
[stack]
name  = "frappe"
bench = "/home/me/frappe-bench"
app   = "myapp"
sites = ["agents1.test", "agents2.test", "agents3.test"]   # copies for agents only
```

What it does inside the lock, for the site it hands out:

- puts the **worktree first on `PYTHONPATH`**, so `import myapp` is the worktree's code. The bench's own checkout, built assets and running processes are never touched;
- deletes stale `__pycache__` from the worktree. Bytecode from another branch can keep a deleted module importable, as a namespace package;
- runs `bench --site <site> migrate` and `clear-cache` to bring the site to the branch's schema;
- replaces the placeholder site name in the agent's command (by default the first site) with the site it took. The prompt tells agents to write `{{SITE}}`.

It refuses `bench build`, `bench restart`, `bench update`, `--site all`, `drop-site`, `reinstall` and `bench setup`, plus a whole-app `run-tests` with no `--module`/`--doctype`/`--test` (the full suite is CI's job, and on a shared site it makes every other agent queue behind it).

It enables merge drivers for the files that conflict on every batch:

| File | Driver | Rule |
|---|---|---|
| `**/patches.txt` | `keep_both_lines` | keep both sides' lines, deduplicated |
| `**/doctype/*/*.json` | `doctype_json` | fields merged by fieldname, `field_order` unioned in place, Select options unioned; any other field changed differently on both sides is a real conflict |
| `**/fixtures/custom_field.json`, `property_setter.json` | `fixture_json` | records merged by `name`, one-sided changes win |
| `package.json` | `package_json` | three-way per key against the merge base; a key changed differently on both sides is a real conflict |

Frontend work is checked with the app's JS test runner inside the worktree (symlink `node_modules` with `[project] shared_paths`). CI builds the product.

## Merge drivers (any stack)

```toml
[integrate.mergers]
"CHANGELOG.d/*.md"         = "keep_both_blocks"
"docs/_generated/**"       = "take_theirs"      # then regenerate in [integrate.train] after_version
"lint-allowlist.json"      = "min_counts"       # an allow-list that only ever shrinks
"yarn.lock"                = "fail"             # never mechanically: the agent reinstalls
```

Drivers: `package_json`, `keep_both_lines`, `keep_both_blocks`, `take_ours`, `take_theirs`, `fixture_json`, `doctype_json`, `min_counts`, `fail`. The most specific matching glob wins. A file no glob matches is a code conflict. Run one by hand inside a conflicted merge with `nohalt merge-driver <name> <path>`.

## Writing a stack

Subclass `nohalt.stacks.Stack`:

```python
class Rails(Stack):
    name = "rails"
    prompt_addendum = "prompts/rails.md"      # a path, or builtin:<name>

    def apply_defaults(self):
        self.cfg.verify.refuse += ["db:drop", "RAILS_ENV=production"]
        self.cfg.integrate.mergers.setdefault("db/schema.rb", "fail")

    def prepare(self, worktree, slot):
        run(["bin/rails", "db:test:prepare"], cwd=worktree, env={**os.environ, **slot.env})
        return Prepared(cwd=worktree, env=dict(slot.env), notes=["test database prepared"])
```

Register it in `nohalt.stacks.get` and send a pull request.
