# Stock Data Architecture

Cloudflare R2 is the permanent source of truth for India and US daily candles.
The Git repository contains application code, workflows, and symbol-universe
inputs, but no candle files.

## R2 layout

```text
stock-data/
  india/
    yearly/YYYY.parquet
    current/YYYY-MM.json
  us/
    yearly/YYYY.parquet
    current/YYYY-MM.json
  manifest.json
```

Completed years are immutable Parquet files. The current year is split into
monthly JSON arrays containing `Symbol`, `Date`, OHLC, adjusted close, and
volume. `Symbol + Date` is the unique candle key.

The manifest records each object key, checksum, byte size, row count, update
time, market symbols, latest date, and an overall version. Writers upload and
validate candle objects before publishing the new manifest, so readers never
observe a manifest pointing at an incomplete upload.

## Streamlit reads

`r2_stock_data.py` keeps downloaded objects in a temporary local cache. It:

- refreshes `manifest.json`;
- selects only files intersecting the requested date window;
- reads only requested Parquet columns and applies a symbol predicate;
- merges current JSON with historical Parquet;
- keeps the newest duplicate `Symbol + Date` row;
- redownloads an object when its manifest checksum changes.

Existing screener and chart paths are virtual symbol references. Their requested
lookback determines which aggregate files are downloaded. The Data Management
page's **Sync Stock Data** button refreshes the manifest and current monthly
file; all other missing files download automatically.

## Twice-daily update

`.github/workflows/streamlit-cron.yml` runs after the India and US sessions. It
downloads the current monthly object, fetches overlapping Yahoo candles, keeps
only completed sessions, validates and deduplicates rows, uploads the month,
and publishes the manifest last.

At the first run of a new year, the updater merges the prior year's monthly
objects into one compressed Parquet object and removes those months from the
published manifest. Old monthly objects may be lifecycle-deleted later.

## One-time migration

With R2 environment variables configured, run:

```powershell
python migrate_r2_stock_data.py --source-root data --completed-through 2025 --current-year 2026
```

Verify the uploaded manifest and both markets before deploying the code that no
longer carries candle files.
