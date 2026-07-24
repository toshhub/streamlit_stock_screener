# NSE Stock Screener - Project Architecture

This project is a Streamlit app for downloading NSE stock price data from Yahoo Finance, screening stocks using configurable moving-average filter sets, viewing filtered results, and emailing those results as a CSV attachment.

The main app entry point is `app.py`.

## High-Level Purpose

The app supports this workflow:

1. Select India or US in the Data Management tab. Default is India.
2. Upload or keep the default India market-cap Excel file, or use the US Nasdaq CSV file.
3. Download historical data for symbols from the selected market using `yfinance`.
4. Screen downloaded stock JSON files using user-selected filter sets.
5. Optionally generate price/MA charts for matched stocks.
6. View filtered results in the app.
7. Download results as CSV or email the CSV through Gmail SMTP.

## Main Files

### `app.py`

Streamlit UI and orchestration layer.

Responsibilities:

- Defines the page layout and four tabs:
  - `Data`
  - `MA Screener`
  - `Pattern Screener`
  - `Results`
- Loads persisted UI settings from `storage.py`.
- Saves latest UI values back to settings as users interact with the app.
- Handles Excel upload/replacement.
- Handles market selection in Data Management:
  - India uses `data/excel/MCAP_JUGAAD.xlsx`.
  - US uses `data/excel/nasdaq_screener_1784114565446.csv`.
- Starts top-1000 stock data download.
- Displays download progress bar and live count.
- Displays last stock download timestamp in the Data tab.
- Runs the MA screener over downloaded JSON files using the selected filter set.
- Displays screener progress bar and live count.
- Optionally creates chart PNGs for matched stocks during screening.
- Stores screener results in `st.session_state["results"]`.
- Persists the latest screener rows to `data/metadata/last_results.json`.
- Automatically switches to the Results tab after a completed Run Screener.
- Supports protected external cron calls:
  - `?ping=1&token=...`
  - `?scheduled_download=1&token=...`
- Shows results table, hover charts when available, and CSV download button.
- Provides Gmail email form and sends results CSV using `emailer.py`.

### `config.py`

Central path configuration.

Defines:

- `BASE_DIR`
- `DATA_DIR`
- `EXCEL_DIR`
- `DAILY_DIR`
- `WEEKLY_DIR`
- `MONTHLY_DIR`
- `CHARTS_DIR`
- `META_DIR`

It also creates these folders at import time with `mkdir(parents=True, exist_ok=True)`.

### `downloader.py`

Stock data download and Excel symbol extraction logic.

Key objects/functions:

- `TIMEFRAME_CONFIG`
  - India `DAY`: interval `1d`, period `5y`, output folder `data/daily`
  - India `WEEK`: interval `1wk`, period `10y`, output folder `data/weekly`
  - India `MONTH`: interval `1mo`, period `max`, output folder `data/monthly`
  - US `DAY`: interval `1d`, period `5y`, output folder `data/us/daily`
  - US `WEEK`: interval `1wk`, period `10y`, output folder `data/us/weekly`
  - US `MONTH`: interval `1mo`, period `max`, output folder `data/us/monthly`
- `MARKET_INDIA` and `MARKET_US`
  - Supported market IDs saved in settings.
- `flatten_columns(df)`
  - Handles yfinance MultiIndex columns.
- `yfinance_symbol(symbol, market)`
  - Appends `.NS` for India.
  - Uses plain symbols for US.
- `download_symbol(symbol, interval, period, out_file, market=...)`
  - Downloads one symbol for the selected market.
  - Incrementally loads the existing stock JSON when present.
  - Requests only missing candles after the latest saved date when possible.
  - Merges, de-duplicates by `Date`, and atomically saves records to JSON.
  - Returns a status dictionary with `Downloaded`, `Rows Added`, and `Status`.
- `timeframe_config(timeframe)`
  - Returns the config for `DAY`, `WEEK`, or `MONTH`.
- `clean_symbol(value)`
  - Cleans Excel ticker values for yfinance/NSE use.
- `find_column(columns, required_terms, optional_terms=None)`
  - Helps detect symbol and market-cap columns in the Excel file.
- `load_top_symbols(symbols_file, limit=1000, market=...)`
  - Reads Excel or CSV with pandas.
  - Finds a symbol column.
  - Sorts by market-cap-like column if found.
  - Returns unique symbols up to the requested limit.
- `download_top_stocks(symbols_file, timeframe, limit=1000, progress_callback=None, market=...)`
  - Downloads data for top symbols.
  - Writes JSON files into timeframe-specific folder.
  - Calls `progress_callback(index, total, downloaded_count, symbol)` after each stock.

Important storage note:

- Keep downloaded price data as one JSON file per stock per timeframe.
- Do not replace this with one large JSON keyed by stock name unless there is a strong measured reason.
- One-file-per-stock keeps updates independent, reduces corruption blast radius, and keeps screening/backtesting simple.
- If faster status checks are needed, prefer adding a small manifest/index file with each symbol's latest date and row count.

### `screener.py`

Moving-average screening logic.

Key functions:

- `DEFAULT_FILTER_SET`
  - Defines the default dynamic filter list.
- `FILTER_TYPE_LABELS`
  - Maps internal filter type IDs to UI labels.
- `FILTER_TYPE_DEFAULTS`
  - Defines the default params for each filter category.
- Custom expression filters are regular, repeatable filter rows (`custom_expression`).
  - Each row stores one expression in `params.expression` and is saved with the rest of the current filter set.
  - Separately stored legacy expressions are migrated into Custom Filter rows when settings or favorites are loaded.
  - Candle expressions support `Candle[0]`, negative historical offsets, inclusive ranges such as `Candle[0..-4]`, OHLC fields, and `IsGreen()`.

Backtest sell strategies provide optional Target and Stop Loss expressions. Percentage-only values are relative to the buy price; candle expressions are anchored to the buy date. SMA- and market-value-based Stop Loss expressions are recalculated on every future candle while their `Candle[...]` references remain anchored to the buy candle. Trades use the corresponding candle's High/Low touch by default or Close when Closing Basis is enabled, and realized returns are averaged with equal stock weights. An optional Backtest-wide Green Candle toggle limits every selected strategy to stocks whose Buy Date Close is greater than Open. Per-stock charts show ten available trading candles before the buy date through ten after the requested end date. Both static and interactive views mark the buy and booked exit; the interactive view also overlays the evaluated Buy, Target, and Stop Loss price lines and every MA period required by that favorite strategy, including MA references in Custom Filters.
- `long_ma_rising_from_two_bars_back(series)`
  - Long MA is considered rising if current Long MA is greater than the Long MA value from two rows/candles back.
  - Also returns the rising-rate percent between those two values.
- `pct_close_to_ma(price, moving_average)`
  - Calculates absolute percentage distance from price to MA.
- `crossed_up(short_ma, long_ma, lookback_days)`
  - Detects whether Short MA crossed above Long MA within the last N rows/candles.
- `long_ma_down_from_max(series, down_pct, lookback_units)`
  - Checks whether the current Long MA is down at least the requested percent from its max value over the last M rows/candles.
- `required_ma_periods(filter_set)`
  - Collects the MA periods needed by enabled filters.
- `normalize_filter_set(filter_set)`
  - Merges a user/favorite filter set with defaults.
- `screen_json_file(...)`
  - Reads one downloaded stock JSON file.
- Builds all SMA columns required by enabled filters.
- Emits a sortable `ROI{period}` value when a `price_near_long` filter is active; this matches the Custom Filter `ROI(period)` calculation and measures the SMA percentage change from the previous trading candle.
  - Applies every enabled filter in the selected filter set.
  - Returns a result dictionary only if the stock matches all enabled filters.
  - Returns `None` if it does not match or does not have enough data.

Current filter blocks:

- MA Rising:
  - Current MA is greater than the same MA value from two rows/candles back.
- Short MA Above Long MA:
  - Current Short MA is greater than current Long MA.
- Current Price Near And Above Long MA:
  - Current price is above or equal to Long MA.
  - Current price is within the UI-provided percent of Long MA.
- Golden Cross:
  - Short MA crossed above Long MA within the last N timeframe units.
- Long MA Down From Recent Max:
  - Current Long MA is down at least m percent from its max value over the last M timeframe units.

Filter-set behavior:

- Users can add any filter category any number of times.
- Each added filter becomes its own filter instance with its own field values.
- Stocks must satisfy all added filters.
- Favorite filter sets save the exact list of added filter instances and their field values.

Result fields include:

- `Symbol`
- `Price`
- `SMA{short_ma}`
- `SMA{long_ma}`
- `MatchedFilters`
- `MARising`
- `MARisingRatePct`
- `ShortMAAboveLongMA`
- `PercentCloseToLongMA`
- `PriceNearAndAboveLongMA`
- `GoldenCross`
- `LongMADownFromMaxPct`
- `LongMADownFromMax`

### `storage.py`

Small JSON settings persistence layer.

Settings file:

```text
data/metadata/app_settings.json
```

Functions:

- `load_settings()`
  - Reads the JSON file if present.
  - Returns `{}` if missing.
- `save_settings(data)`
  - Writes settings JSON.
- `update_settings(data)`
  - Loads existing settings.
  - Merges new values.
  - Saves the merged result.

Persisted values currently include:

- Selected market (`INDIA` or `US`).
- Download timeframe.
- Last download timestamp.
- Last download timeframe.
- Screener timeframe.
- Current dynamic screener filter list.
- Favorite screener filter sets.
- Selected favorite screener filter set.
- Gmail ID.
- Recipient email.
- Email subject.
- Email message body.

Important: Gmail App Password is intentionally not saved.

### `emailer.py`

Gmail SMTP email helper.

Function:

- `send_results_email(sender_email, app_password, recipient_email, subject, body, csv_data)`

Behavior:

- Creates an `EmailMessage`.
- Attaches screener results as `stock_screener_results.csv`.
- Sends via Gmail SMTP over SSL:

```text
smtp.gmail.com:465
```

Users should use a Gmail App Password, not their normal Gmail password.

### `charting.py`

Chart generation and hover-table rendering.

Key functions:

- `create_stock_chart(json_path, filter_set, output_dir=CHARTS_DIR, max_points=260)`
  - Reads the downloaded stock JSON file.
  - Computes all MA periods required by the selected filter set.
  - Creates a PNG chart with Close price plus each selected MA line.
  - Uses different colors for different MA lines.
  - Saves charts in `data/charts/`.
- `results_hover_table_html(df)`
  - Builds an HTML results table.
  - When a row has `ChartPath`, hovering the stock symbol displays the chart.

Streamlit note: `st.dataframe` does not support custom hover popups, so chart-enabled results use an HTML table rendered through `st.markdown(..., unsafe_allow_html=True)`.

### `requirements.txt`

Deployment dependencies:

```text
streamlit
yfinance
pandas
openpyxl
matplotlib
```

Earlier unused heavy packages were removed to make Streamlit Cloud deployment simpler.

### `.gitignore`

Ignores generated/runtime files:

- Python cache files.
- Virtual environments.
- Downloaded stock JSON data:
  - `data/daily/`
  - `data/weekly/`
  - `data/monthly/`
- Generated charts.
- Local metadata/settings.
- Local Streamlit secrets file.

## Data Folders

### `data/excel/`

Holds the market-cap Excel file.

Default expected filename:

```text
data/excel/MCAP_JUGAAD.xlsx
```

Behavior:

- If this file exists, the Data tab shows it as the default Excel.
- User can upload a new `.xlsx` file to replace it.
- The app keeps using the previous/default Excel until a new one is uploaded.

### `data/daily/`

Stores downloaded daily JSON files.

Used when timeframe is `DAY`.

### `data/weekly/`

Stores downloaded weekly JSON files.

Used when timeframe is `WEEK`.

### `data/monthly/`

Stores downloaded monthly JSON files.

Used when timeframe is `MONTH`.

### `data/metadata/`

Stores app settings in `app_settings.json`.

This is local persistence for user-entered UI values and last download timestamp.

## UI Tab Details

### Data Tab

Controls:

- Market selector:
  - `India`
  - `US`
- Last stock data download timestamp display.
- Download timeframe selectbox:
  - `DAY`
  - `WEEK`
  - `MONTH`
- Default Excel status.
- Replace Excel file uploader.
- Download Top 1000 Stocks button.

Download behavior:

- Reads `MCAP_JUGAAD.xlsx` for India or `nasdaq_screener_1784114565446.csv` for US.
- Finds/sorts symbols by market cap if a market-cap-like column exists.
- Downloads each symbol using yfinance.
- India symbols use the `.NS` suffix.
- US symbols use no suffix.
- India data is stored in `data/daily`, `data/weekly`, or `data/monthly`.
- US data is stored in `data/us/daily`, `data/us/weekly`, or `data/us/monthly`.
- Shows progress bar.
- Shows live text like:

```text
Downloaded 37 of 1000 stocks. Processing 42/1000: RELIANCE
```

- Saves last download timestamp after completion.

### MA Screener Tab

Controls:

- Favorite Filter Set selector.
- Timeframe selectbox:
  - `DAY`
  - `WEEK`
  - `MONTH`
- Add Filter dropdown.
- Dynamically added filter sections.
- Remove Filter button for each added filter.
- Favorite Filter Sets save/remove controls.
- Create charts for matched stocks checkbox.
- Run Screener button.

Market behavior:

- No market selector is shown here.
- Screener uses the market selected in Data Management.
- P/E lookup appends `.NS` and can use Screener.in fallback for India.
- P/E lookup uses plain Yahoo symbols and skips Screener.in fallback for US.

Validation:

- At least one filter must be added.
- Short MA must be less than Long MA in filters that compare or cross short/long MAs.

Run behavior:

- Scans JSON files from the timeframe-specific folder.
- Requires every added filter in the selected filter set to pass.
- If chart creation is enabled, matched stocks get a `ChartPath` pointing to a generated PNG.
- Shows progress bar.
- Shows live text like:

```text
Screened 120 of 1000 stocks. Matches found: 8. Processing: RELIANCE
```

- Saves matches into `st.session_state["results"]`.

### Pattern Screener Tab

Placeholder tab.

Current text:

```text
Add Cup&Handle, Double Bottom, Bull Flag scanners here.
```

No active pattern-scanning logic exists yet.

### Results Tab

Displays the latest screener results from session state or `data/metadata/last_results.json`.

The result rows represent whichever market was selected when Run Screener was last executed.

Controls:

- Dataframe of filtered stocks.
- Hover chart table when `ChartPath` is present.
- Download Results CSV button.
- Email Results form:
  - Gmail ID.
  - Gmail App Password.
  - Recipient Email.
  - Subject.
  - Message.
  - Send Results Email button.

Email behavior:

- Sends the current filtered results as CSV.
- Does not save Gmail App Password.
- Saves non-sensitive email fields for convenience.

### Alerts Tab

Price alerts are created by moving or tapping the interactive-chart crosshair
and clicking the `+` control at the matching right-side price level. No manual
price field is used. Alerts are persisted in
`data/metadata/price_alerts.json`. The target is classified automatically as a
cross-above or cross-below alert relative to the latest close. Only candles
after the alert's creation candle are evaluated: `High >= target` triggers an
above alert and `Low <= target` triggers a below alert.

Every successful manual or scheduled stock download checks alerts for that
symbol. A triggered alert changes state once, so repeated downloads do not
produce duplicate triggers. The Alerts tab lists Active and Triggered alerts
with the trigger candle date, shows a red triggered-count badge, and lets the
user remove selected rows.

## Runtime State vs Persistent State

### Runtime State

`st.session_state["results"]` stores current screener results for the active Streamlit session.

`st.session_state["switch_to_results_tab"]` is a one-shot flag used after Run Screener completes. On the next rerun, `switch_to_tab(3)` injects a tiny Streamlit component script that clicks the Results tab because native `st.tabs` does not expose a selected-tab API.

If the app reloads or restarts, the Results tab can reload the latest saved rows from `data/metadata/last_results.json`.

### Persistent State

`data/metadata/session_settings.json` stores UI preferences and last download timestamp locally. `data/metadata/app_settings.json` is treated as a legacy settings file.

`data/metadata/last_results.json` stores the latest screener output.

`data/metadata/price_alerts.json` stores persistent price-alert definitions and
their latest Active or Triggered state.

On Streamlit Community Cloud, this local file may not persist across app restarts because the deployment filesystem is ephemeral.

## Scheduled Downloads / Keep Awake

Streamlit Community Cloud does not provide a native cron scheduler for the app. Use an external scheduler such as GitHub Actions, cron-job.org, UptimeRobot, or EasyCron to call protected app URLs.

Token:

- Set `SCHEDULED_DOWNLOAD_TOKEN` in Streamlit secrets or as an environment variable.
- Do not commit the token.

URLs:

```text
https://your-app.streamlit.app/?ping=1&token=SECRET
https://your-app.streamlit.app/?scheduled_download=1&token=SECRET
https://your-app.streamlit.app/?scheduled_download=1&token=SECRET&market=ALL&timeframe=DAY
```

Recommended schedule:

- Lightweight ping every 6 hours to avoid the 12-hour inactivity sleep window.
- Actual scheduled downloads at 5 AM and 5 PM.
- Use `market=ALL` to refresh both India and US. Omit `market` to use the market saved in app settings.

## Deployment Notes

### Optional Google accounts and Supabase

`user_auth.py` uses Streamlit's Google OIDC login. Guest use remains available,
but only a verified Google user can save personal favorites or create alerts.
`cloud_storage.py` stores personal filter sets, UI settings, and alerts in
Supabase using the verified Google `sub` claim as the stable user ID.

The existing `data/metadata/favourite_filters.json` remains the shared,
centrally managed favorite library. Stock JSON, fundamentals, results, and
other market-data files also remain central. Shared favorites are read-only
from the normal user interface; logged-in users can add and remove only their
own cloud favorites.

The Supabase service-role key is server-only. Direct browser access is revoked
by `supabase_schema.sql`, and every application query for personal data is
filtered by the current verified user ID. See `CLOUD_SETUP.md` and
`.streamlit/secrets.example.toml` for deployment configuration.

The app can be deployed to Streamlit Community Cloud.

Recommended files to commit:

- `app.py`
- `config.py`
- `downloader.py`
- `screener.py`
- `storage.py`
- `emailer.py`
- `requirements.txt`
- `.gitignore`
- `PROJECT_ARCHITECTURE.md`
- Optionally `data/excel/MCAP_JUGAAD.xlsx`

Do not commit generated downloaded JSON folders unless there is a specific reason. They can become large quickly.

Streamlit Community Cloud is free for community apps, but app filesystem storage should not be treated as permanent.

## Local Run Commands

Known working Codex/runtime Python for checks:

```powershell
C:\Users\tusha\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
```

Use this interpreter when the Windows `py` launcher reports `No installed Python found!`.

Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

Run app:

```powershell
py -m streamlit run app.py
```

Compile check:

```powershell
py -m py_compile app.py downloader.py screener.py storage.py config.py emailer.py
```

Codex runtime compile check:

```powershell
& 'C:\Users\tusha\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -c "from pathlib import Path; [compile(Path(name).read_text(encoding='utf-8'), name, 'exec') for name in ['app.py','downloader.py','screener.py','storage.py','config.py','emailer.py','backtest.py','charting.py','pattern.py']]; print('syntax ok')"
```

## Known Constraints and Future Improvements

- Downloading top 1000 stocks through yfinance may be slow and may hit network/provider limits.
- US CSV downloads may include many more symbols than the India default, so yfinance limits can be more noticeable.
- Streamlit Cloud restarts may clear downloaded JSON files and metadata.
- Gmail send requires an App Password for most accounts.
- `app.py` currently mixes UI and orchestration; this is fine for the current size, but future larger features may benefit from moving tab logic into separate modules.
- Download performance could be improved later with NSE trading-date skip logic, chunked yfinance downloads, a manifest/index file, caching, or retry/backoff logic.
- Hover charts are implemented with an HTML table because native Streamlit dataframes do not support custom hover popups.
- If deployed publicly, consider using `st.secrets` or environment-backed settings for non-user-specific credentials. Current design correctly avoids storing the Gmail App Password.
