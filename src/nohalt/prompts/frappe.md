## This is a Frappe app

- Your worktree is not installed on the bench. `{{VERIFY}}` puts your worktree first on `PYTHONPATH`, takes one of the agent test sites, migrates it to your branch's schema and runs your command against it. Name the site as `{{SITE}}` in your commands; the lock substitutes whichever site is free.
- Run your own modules (`bench --site {{SITE}} run-tests --module <app>.<path>.test_x`). The whole app's suite is CI's job, and the lock refuses it.
- Never run `bench build`, `bench restart`, `bench update`, `--site all`, `drop-site` or `reinstall`: the bench is shared, and these reach every site on it, including ones people are using. The lock refuses them.
- Schema changes go in the DocType JSON and, where data must move, a patch listed in `patches.txt`. Do not hand-edit generated files.
- Core DocTypes are customised with Custom Fields and Property Setters, never by editing another app's JSON.
