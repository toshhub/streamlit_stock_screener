# Stock data and personal-data migration

## Canonical candle layout

Daily candles are stored as yearly Parquet partitions:

```text
data/daily/<INDIA_SYMBOL>/<YEAR>.parquet
data/us/daily/<US_SYMBOL>/<YEAR>.parquet
```

`migrate_stock_data.py` converts legacy symbol JSON files atomically. The
application can still read a legacy JSON file during rollout, but all writes
use Parquet. A normal update re-downloads a 10-day overlap and rewrites only a
year whose contents changed.

## Supabase migration

Run the complete `supabase_schema.sql` in Supabase Dashboard → SQL Editor
before deploying this version. It adds:

- `user_results`
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
2. writes changed yearly Parquet partitions;
3. refreshes `latest_stock_values.parquet`;
4. checks alerts without allowing alert failures to fail candle downloads;
5. fills the current month's consolidated valuation observations once;
6. commits and pushes changed data files.

Configure these repository secrets for cloud alert processing:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
