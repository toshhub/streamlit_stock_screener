# Repository instructions for Codex

These instructions apply to the entire repository.

## Required reading before changes

Before implementing or diagnosing any feature, bug fix, refactor, deployment
change, data migration, or performance change:

1. Read `PROJECT_ARCHITECTURE.md` completely.
2. Read the source files and tests for the affected subsystem.
3. For candle storage, download jobs, cache behavior, or R2, also read
   `STOCK_DATA_ARCHITECTURE.md`.
4. For authentication, Supabase, personal data, alerts, or deployment secrets,
   also read `CLOUD_SETUP.md` and `supabase_schema.sql`.
5. For migrations, also read `DATA_MIGRATION.md`.

Treat `PROJECT_ARCHITECTURE.md` as canonical when older Markdown files conflict
with it. Update the canonical document in the same change whenever architecture,
data ownership, important UI behavior, background jobs, or performance
invariants change.

## Non-negotiable implementation rules

- Do not add Streamlit reruns or polling to tab selection, chart search, chart
  navigation, or other interactions that can remain inside an existing browser
  component.
- The Results, Alerts, and Chart workspaces must use the shared interactive chart
  route and Results component stack described in `PROJECT_ARCHITECTURE.md`.
- Keep R2 as the canonical candle-data source and local materialized Parquet files
  as the runtime chart/screening cache.
- Keep authenticated personal data scoped by `user_id` in Supabase. Never expose
  service-role credentials to browser code, logs, documentation, tests, or Git.
- Preserve user changes in a dirty worktree. Stage only files intentionally in
  scope unless the user explicitly requests the entire worktree.
- Run the most relevant focused tests and the complete `test_*.py` suite for
  application changes.
- Temporary Streamlit servers used for verification must be stopped before
  handing work back.
