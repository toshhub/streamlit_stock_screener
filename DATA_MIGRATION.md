# Stock data and personal-data migration

## Canonical candle layout

Daily candles are stored as one JSON file per stock:

```text
data/india/daily/<INDIA_SYMBOL>.json
data/us/daily/<US_SYMBOL>.json
```

`migrate_stock_data.py` converts the former yearly Parquet directories to the
canonical JSON files. It reloads and verifies each JSON file before removing
that stock's Parquet partitions. A normal update re-downloads the recent
reconciliation window, merges by candle date, and atomically replaces only the
JSON files whose candles changed.

`backfill_stock_history.py` performs a resumable true 10-year Yahoo backfill
for existing datasets that were originally downloaded with a shorter period:

```text
python backfill_stock_history.py --market ALL --batch-size 100
```

It checkpoints successful symbols between batches, excludes intraday candles,
preserves existing data when a ticker fails, and removes Yahoo's null
pre-listing padding so newer stocks begin in their real listing year.

## Supabase migration

Run the complete `supabase_schema.sql` in Supabase Dashboard → SQL Editor
before deploying this version. It adds:

- `user_watchlists`
- `user_watchlist_items`
- the alert acknowledgement columns (safe if already present)

All personal tables have row-level security enabled and browser roles revoked.
The trusted server always filters user-facing operations by the verified OIDC
subject.

## Automated updates

`.github/workflows/streamlit-cron.yml` runs `daily_update.py` after the India
and US sessions. The job:

1. re-fetches recent candles and excludes the current intraday candle;
2. atomically writes changed per-stock JSON files;
3. refreshes `latest_stock_values.parquet`;
4. checks alerts without allowing alert failures to fail candle downloads;
5. fills the current month's consolidated valuation observations once;
6. commits and pushes changed data files.

Configure these repository secrets for cloud alert processing:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
