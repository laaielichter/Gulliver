# dbt-style SQL lint and PR review

This example shows how to wire Gulliver into a repository that keeps dbt models under
`models/`. The models use made-up data and dbt's `source()`, `ref()` and `config()`
macros. Gulliver can lint the SQL without a dbt installation or a warehouse.

- `models/staging/stg_events.sql` stages events from a source. It deliberately uses
  a wildcard and applies a function in the date filter.
- `models/marts/daily_revenue.sql` aggregates the staging model and deliberately
  sorts by a random value without a limit.
- `gulliver.toml` configures SQL lint and pull request review.
- `.github/workflows/gulliver.yml` creates two jobs for each pull request. Lint
  emits GitHub annotations; review compares committed models with the base branch.
- `.pre-commit-config.yaml` runs the same lint on changed SQL before a commit.

## Try the lint locally

Install Gulliver with `python -m pip install gulliver-mcp==0.4.0`, then run these
commands from this directory:

```bash
gulliver lint models/
gulliver lint models/ --fail-on warn
gulliver lint models/ --format github
```

Expect five warnings, `GV001`, `GV002`, `GV005`, `GV006` and `GV016`, and no
errors. With the example policy, the first command exits 0. The second also fails on warnings and exits 2. The third emits GitHub annotations.

When a rule is intentionally violated, leave a line-specific reason with the
ignore. For example, put `-- gulliver: ignore GV007 exact count needed for billing`
on the line above a statement that needs an exact distinct count.

## Use it in a dbt repository

Copy `gulliver.toml`, `.pre-commit-config.yaml`, and the example workflow into
your repository root, preserving the workflow's path under `.github/workflows/`.
Set the SQL dialect and adjust the directory names to match your project.
Run `pre-commit install` if you want the local hook.

Once the base and proposed model changes are committed and available locally, run:

```bash
gulliver review --models models --base origin/main
```

With changes, the report lists changed models, new findings and any downstream
impact. Cost estimates are optional. If you enable them, use a read-only warehouse
connection with credentials supplied through the job environment. For fork pull
requests, GitHub may provide a read-only token. Set the action's `comment` input to
`false` if your workflow should report only in its job summary.
