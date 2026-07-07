# Repository Context

This file is intended to give future coding sessions enough context to make safe changes quickly. Read this first before editing code.

## Project Summary

`streamlit_stock_screener` is a Streamlit app for screening Indian NSE stocks. It downloads historical price data from Yahoo Finance, stores that data locally as JSON, applies configurable technical and valuation filters, generates charts, persists the latest results, and can email screener output as a CSV attachment.

The main app title is **NSE Stock Screener**. The UI is organized into three primary tabs:

1. **Data** - upload/replace the stock universe Excel file and download price data.
2. **Screener** - configure filters and run screening over downloaded JSON files.
3. **Results** - view, sort, chart, and optionally email matching stocks.

## Runtime / Dependency Overview

Dependencies are listed in `requirements.txt`:

- `streamlit` - web UI framework.
- `yfinance` - historical price data and some P/E lookups.
- `pandas` - tabular data processing, Excel reading, moving average calculations.
- `openpyxl` - Excel file support for `pandas.read_excel`.
- `matplotlib` - chart generation.

## Data Flow

1. The user provides or replaces `data/excel/MCAP_JUGAAD.xlsx`.
2. `downloader.py` reads symbols from the Excel file, optionally sorts by market cap, and downloads top N NSE symbols from Yahoo Finance using the `.NS` suffix.
3. Downloaded data is written as JSON files under one of:
   - `data/daily/`
   - `data/weekly/`
   - `data/monthly/`
4. `app.py` lets the user configure screener filters and choose a timeframe.
5. `screener.py` loads each JSON file, computes required SMAs, applies filters, and returns a row for each passing stock.
6. `pattern.py` optionally evaluates swing high / swing low expression filters.
7. `charting.py` generates chart PNG files under `data/charts/` and renders an interactive results table.
8. `storage.py` persists settings, favourite filters, P/E cache, and last results under `data/metadata/`.
9. `emailer.py` can email results as a CSV attachment through Gmail SMTP.

## File-by-File Map

### `app.py`

Main Streamlit entry point and UI controller.

Responsibilities:

- Sets Streamlit page config and title.
- Loads persisted settings and favourite filter sets.
- Injects custom CSS for buttons, tabs, badges, and data availability cards.
- Renders the three main app tabs: Data, Screener, Results.
- Handles Excel upload/replacement for the stock universe file.
- Lets the user download fresh stock data for daily, weekly, or monthly timeframes.
- Lets the user configure moving-average filters, P/E filters, ATH filters, green candle filters, and pattern-expression filters.
- Saves selected filter settings and favourite filter sets.
- Runs screening against downloaded JSON stock files.
- Generates charts for matching symbols.
- Saves and reloads the last result set.
- Provides email controls for sending screener output.

Key imports:

- `config` for data directories.
- `charting.create_stock_chart` and `charting.sortable_results_table` for result visuals.
- `downloader.clear_downloaded_json_files`, `download_top_stocks`, and `timeframe_config` for data download workflows.
- `emailer.send_results_email` for emailing CSV results.
- `pattern.evaluate_pattern_filters` and `validate_expression` for custom pattern filters.
- `screener` constants and `screen_json_file` for stock filtering.
- `storage` helpers for settings, favourites, and results persistence.

When changing this file:

- Be careful with `st.session_state` keys; many widgets rely on stable keys.
- Keep saved settings backward compatible where possible.
- Avoid putting heavy logic here if it can live in `screener.py`, `pattern.py`, `downloader.py`, or `charting.py`.

### `config.py`

Central path configuration.

Responsibilities:

- Defines `BASE_DIR` and `DATA_DIR`.
- Defines data subdirectories:
  - `EXCEL_DIR`
  - `DAILY_DIR`
  - `WEEKLY_DIR`
  - `MONTHLY_DIR`
  - `CHARTS_DIR`
  - `META_DIR`
- Creates those directories at import time.

When changing this file:

- Keep directory names aligned with `.gitignore` and code that reads/writes these paths.
- Be aware that importing `config.py` has side effects because it creates directories.

### `downloader.py`

Stock-universe loading and Yahoo Finance data download logic.

Responsibilities:

- Defines timeframe mapping:
  - `DAY` -> interval `1d`, period `5y`, output `data/daily/`
  - `WEEK` -> interval `1wk`, period `10y`, output `data/weekly/`
  - `MONTH` -> interval `1mo`, period `max`, output `data/monthly/`
- Downloads symbols from Yahoo Finance using `symbol + ".NS"`.
- Cleans and normalizes NSE symbols from Excel values.
- Detects symbol and market-cap columns in the uploaded Excel file.
- Sorts by market cap when a market-cap column exists.
- Deduplicates symbols and limits the number of symbols to download.
- Writes each downloaded stock as one JSON file named `<SYMBOL>.json`.
- Clears old downloaded JSON files for a selected timeframe before a fresh download.
- Supports a progress callback used by `app.py`.

When changing this file:

- Keep symbol normalization conservative; bad symbol cleanup can break downloads.
- `yf.download` can be slow or flaky, so preserve retry/error handling.
- If adding new timeframes, update UI options in `app.py` too.

### `screener.py`

Core screening and filter engine.

Responsibilities:

- Defines user-facing filter labels in `FILTER_TYPE_LABELS`.
- Defines default parameter values in `FILTER_TYPE_DEFAULTS`.
- Defines the default filter set in `DEFAULT_FILTER_SET`.
- Computes required SMA periods for the active filter set.
- Normalizes current and legacy filter configurations.
- Loads each stock JSON file and applies filters.
- Returns one result dictionary per stock that passes all active filters.
- Calculates:
  - price
  - SMA values
  - distance from SMA
  - moving-average rate of change
  - filter pass/fail detail fields
  - P/E ratio
- Supports these filter types:
  - `ma_rising`
  - `short_above_long`
  - `price_near_long`
  - `golden_cross`
  - `long_ma_down_from_max`
  - `long_ma_up_from_min`
  - `green_candle_today`
  - `pe_less_than`
  - `hitting_all_time_high`
  - `price_near_old_ath`
- Fetches P/E ratios from multiple fallbacks:
  - `yfinance.Ticker(...).info["trailingPE"]`
  - Yahoo quote API
  - Screener.in consolidated page scrape
- Caches P/E ratios through `storage.load_pe_ratios` and `storage.save_pe_ratios`.

When changing this file:

- Add new filters in all required places: labels, defaults, UI in `app.py`, required MA periods if needed, and the `screen_json_file` evaluation loop.
- Keep `screen_json_file` returning `None` for stocks that fail filters.
- Be mindful that `pe_less_than` is intentionally evaluated after other filters to avoid unnecessary network lookups.

### `pattern.py`

Swing high / swing low detection and safe custom expression evaluation.

Responsibilities:

- Loads price JSON data into a clean DataFrame.
- Detects swing highs and lows within a configurable lookback window and reversal percentage.
- Builds a context for pattern expressions using:
  - `P` = latest close price
  - `H1`, `H2`, ... = most recent swing highs
  - `L1`, `L2`, ... = most recent swing lows
  - `DH1`, `DH2`, ... = days since swing highs
  - `DL1`, `DL2`, ... = days since swing lows
- Validates expressions with Python AST before evaluation.
- Allows only simple arithmetic, comparisons, boolean operations, and safe functions:
  - `abs`
  - `min`
  - `max`
  - `round`
- Evaluates all configured pattern expressions and returns pass/fail state, swing annotations, and an error string.

When changing this file:

- Keep expression evaluation sandboxed; do not allow arbitrary builtins, attributes, imports, subscripts, or comprehensions unless intentionally secured.
- If adding new pattern variables, update `is_pattern_variable`, `build_swing_context`, and UI/help text in `app.py`.

### `charting.py`

Chart generation and result table rendering.

Responsibilities:

- Loads stock JSON price data for charting.
- Computes SMAs required by the active filter set.
- Generates PNG charts using Matplotlib.
- Saves chart images under `data/charts/`.
- Annotates latest close and SMA values on the chart.
- Optionally marks recent swing highs/lows.
- Converts chart images to base64 data URIs.
- Builds an HTML/JavaScript results table with a sticky chart preview panel.
- Supports sortable result display through `sortable_results_table`.

When changing this file:

- Generated chart paths are used by the results table, so keep `ChartPath` behavior compatible.
- Be careful when editing embedded HTML/JS; Streamlit component rendering can be sensitive to quoting and escaping.
- If adding new chart overlays, ensure they only require data already available or explicitly passed in.

### `storage.py`

Persistence helpers for app settings, saved filters, P/E cache, and last results.

Responsibilities:

- Stores metadata files under `data/metadata/`.
- Reads/writes:
  - `session_settings.json`
  - legacy `app_settings.json`
  - `favourite_filters.json`
  - `pe_ratios.json`
  - `last_results.json`
- Migrates legacy app settings to `session_settings.json` when present.
- Provides simple helper functions for load/save/update operations.

When changing this file:

- Avoid breaking existing JSON shapes unless migration code is added.
- Callers expect missing files to return empty dictionaries/lists rather than raising errors.

### `emailer.py`

SMTP email utility for sending screener results.

Responsibilities:

- Creates an `EmailMessage`.
- Sets sender, recipient, subject, and body.
- Attaches CSV data as `stock_screener_results.csv`.
- Sends through Gmail SMTP over SSL on port 465.

When changing this file:

- Do not hardcode credentials.
- Keep secrets in Streamlit secrets or user inputs, not in source code.
- Consider better error messages in `app.py` when SMTP login or sending fails.

### `requirements.txt`

Python dependency list for running the app.

Current packages:

- `streamlit`
- `yfinance`
- `pandas`
- `openpyxl`
- `matplotlib`

When changing this file:

- Add only runtime dependencies needed by the app.
- If a package is only needed for development/testing, consider a separate dev requirements file.

### `.gitignore`

Repository ignore rules.

Responsibilities:

- Ignores Python cache files.
- Ignores virtual environments.
- Ignores generated/downloaded data directories:
  - `data/daily/`
  - `data/weekly/`
  - `data/monthly/`
  - `data/charts/`
  - `data/metadata/`
- Ignores `.streamlit/secrets.toml`.

When changing this file:

- Keep generated data out of Git unless there is a deliberate reason to version sample data.
- Keep secrets excluded.

## Generated / Local Data Directories

These directories are created by `config.py` but ignored by Git:

- `data/excel/` - expected location for `MCAP_JUGAAD.xlsx`.
- `data/daily/` - downloaded daily JSON stock data.
- `data/weekly/` - downloaded weekly JSON stock data.
- `data/monthly/` - downloaded monthly JSON stock data.
- `data/charts/` - generated PNG chart files.
- `data/metadata/` - app settings, favourite filters, P/E cache, and last results.

Note: `.gitignore` currently ignores daily/weekly/monthly/charts/metadata but does not ignore `data/excel/`, so be careful not to commit a private Excel universe file unless intended.

## Common Change Recipes

### Add a new screener filter

1. Add a label in `screener.py` -> `FILTER_TYPE_LABELS`.
2. Add default params in `screener.py` -> `FILTER_TYPE_DEFAULTS`.
3. Update `required_ma_periods` if the filter needs moving averages.
4. Add evaluation logic in `screen_json_file`.
5. Add UI controls in `app.py`.
6. Include useful output columns in the result row.
7. If charts need new overlays, update `charting.py`.

### Add a new timeframe

1. Add it to `TIMEFRAME_CONFIG` in `downloader.py`.
2. Add it to the Streamlit select boxes in `app.py`.
3. Add a target data directory in `config.py` if needed.
4. Update `.gitignore` if a new generated data directory is created.

### Change persisted settings

1. Update save/load logic in `storage.py` if the JSON structure changes.
2. Update defaults and reads in `app.py`.
3. Preserve backward compatibility when possible.
4. Add migration handling for old keys if needed.

### Change result display

1. Result row fields are produced mostly by `screener.py`.
2. Chart paths are generated by `charting.create_stock_chart`.
3. Table display behavior is in `charting.sortable_results_table` and helper HTML functions.
4. `app.py` decides when results are generated, saved, loaded, and emailed.

## Important Implementation Notes

- NSE Yahoo symbols are constructed by appending `.NS` to cleaned symbols.
- P/E lookups can trigger network calls, so they are deferred and cached.
- `pe_less_than` is evaluated after other filters to reduce unnecessary P/E fetches.
- `config.py` creates directories at import time.
- Generated data and metadata are intentionally local and mostly ignored by Git.
- The app currently has no `README.md`; this `CONTEXT.md` is focused on development context, not end-user setup instructions.

## Suggested First Steps for Future Sessions

Before making code changes:

1. Read this file.
2. Inspect the specific module you plan to edit.
3. Check related files listed above so UI, filtering, storage, and charting stay consistent.
4. For filter changes, always update both `app.py` and `screener.py`.
5. For data path changes, always check `config.py` and `.gitignore`.
