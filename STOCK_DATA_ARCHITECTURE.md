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

## Streamlit server cache

R2 is used only by the explicit server-cache synchronization workflow. Normal
screening, chart, backtest, and alert reads neither query R2 nor check the R2
manifest; they use the active local generation exclusively.

For each market, `r2_stock_data.py` materializes an atomic cache generation
under `data/.stock-cache/r2/materialized/` containing:

```text
india/
  active.json
  generations/<revision>/
    metadata.json
    market.parquet
    symbols/<SYMBOL>.parquet
```

- `market.parquet` contains the rolling ten-year market dataset and supports
  one local bulk read for screening.
- `symbols/*.parquet` contain rolling ten-year per-symbol data for charts,
  alerts, patterns, and backtests.
- The generation revision is derived from the R2 entry checksums and the local
  cache schema version.
- If no active generation exists, local data requests report that the server
  cache is not ready instead of falling back to R2.
- When the manifest changes, the previous generation remains active while a
  replacement is built in a background thread.
- `active.json` is replaced atomically only after the consolidated and symbol
  files have been written and validated.
- Downloaded R2 objects retain their checksum-indexed cache, so unchanged cloud
  objects are not downloaded again while constructing a new generation.
- The two newest materialized generations are retained for safe rollover.

On a regular app open, the server may start one background manifest check per
market per server process. Interactive-chart requests are excluded from this
startup check, and subsequent Streamlit reruns do not repeat it. If the active
revision matches, no candle objects are downloaded and no cache is rebuilt. The
Data Management page also reports readiness, and its **Refresh Server Cache
Now** button starts a forced check and refresh. While a refresh runs, a polling
Streamlit fragment shows download or symbol-build progress, percentage complete,
processed bytes or symbols, and an estimated time remaining without blocking
the page.

Runtime-generated files are local to the deployed server. A host with
persistent disk normally pays the initial build once. An ephemeral host may
need to rebuild after a container replacement or redeployment.

## Twice-daily update

`.github/workflows/streamlit-cron.yml` runs after the India and US sessions. It
downloads the current monthly object, fetches overlapping Yahoo candles, keeps
only completed sessions, validates and deduplicates rows, uploads the month,
and publishes the manifest. After publication, the same cron loads active
Supabase price alerts once per market, evaluates them against the finalized
candles, and persists triggered or last-checked changes in a batch. Local server
cache synchronization never evaluates alerts.

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
