# NSE Stock Screener — Architecture & Implementation

## Overview

A **Streamlit** web application that screens stocks listed on the **National Stock Exchange of India (NSE)** using a combination of **technical indicators** (moving averages, candle patterns) and **fundamental metrics** (PE ratio). Users can compose custom filter sets, detect swing patterns via user-written expressions, visualize results with charts, and export/email screener results.

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| UI Framework | Streamlit (`app.py`) |
| Data Download | `yfinance` (Yahoo Finance) |
| Data Processing | `pandas` |
| Charting | `matplotlib` |
| Email | `smtplib` (Gmail SMTP) |
| Excel I/O | `openpyxl` (via `pandas`) |
| Storage | JSON files on disk (no database) |

---

## Project Structure

```
streamlit_stock_screener/
├── app.py                 # Streamlit UI — entry point
├── config.py              # Directory paths & initialisation
├── charting.py            # Chart generation & HTML results table with hover previews
├── downloader.py          # Stock data downloading from Yahoo Finance
├── emailer.py             # Email results as CSV attachment
├── pattern.py             # Swing detection & expression-based pattern filtering
├── screener.py            # Core screening engine (MA filters, PE filter)
├── storage.py             # JSON-based persistence for settings, filters, PE cache
├── requirements.txt       # Python dependencies
├── .gitignore
├── ARCHITECTURE.md        # This document
└── data/
    ├── excel/             # MCAP_JUGAAD.xlsx — stock universe (symbols + market cap)
    ├── daily/             # Downloaded daily OHLC JSON files
    ├── weekly/            # Downloaded weekly OHLC JSON files
    ├── monthly/           # Downloaded monthly OHLC JSON files
    ├── charts/            # Generated chart PNG images
    └── metadata/          # Persistent JSON state files:
        ├── session_settings.json   # App settings (download TF, filter state, email config, etc.)
        ├── favourite_filters.json  # Saved filter sets (MA + pattern)
        └── pe_ratios.json          # PE ratio cache keyed by symbol
```

---

## Module Details

### 1. `config.py` — Directory Configuration

Defines all data directories and ensures they exist on startup.

```
BASE_DIR  →  data/
  ├── excel/    →  EXCEL_DIR
  ├── daily/    →  DAILY_DIR
  ├── weekly/   →  WEEKLY_DIR
  ├── monthly/  →  MONTHLY_DIR
  ├── charts/   →  CHARTS_DIR
  └── metadata/ →  META_DIR
```

All directories are created via `Path.mkdir(parents=True, exist_ok=True)` at import time.

---

### 2. `downloader.py` — Data Download

**Purpose**: Fetches historical OHLC data for top NSE stocks and writes each as a JSON file.

**Key constants** (`TIMEFRAME_CONFIG`):

| Timeframe | yfinance `interval` | yfinance `period` | Output Directory |
|-----------|---------------------|-------------------|------------------|
| DAY       | `1d`                | `5y`              | `data/daily/`    |
| WEEK      | `1wk`               | `10y`             | `data/weekly/`   |
| MONTH     | `1mo`               | `max`             | `data/monthly/`  |

**Key functions**:

- `load_top_symbols(excel_file, limit)` — Reads `MCAP_JUGAAD.xlsx`, auto-detects symbol and market-cap columns, sorts by market cap descending, returns top N symbols.
- `download_symbol(symbol, interval, period, out_file)` — Downloads data from Yahoo Finance via `yfinance.download()`, flattens multi-index columns, writes JSON.
- `download_top_stocks(...)` — Orchestrates bulk download with progress callback.
- `clear_downloaded_json_files(timeframe)` — Deletes all existing JSON files from a timeframe directory before a fresh download.
- `timeframe_config(timeframe)` — Returns the config dict for a given timeframe string.

**Data format**: Each downloaded file is a JSON array of OHLC records:
```json
[
  {"Date": "2021-01-01", "Open": 100.0, "High": 102.0, "Low": 99.0, "Close": 101.5, "Volume": 12345},
  ...
]
```
Values are auto-adjusted (`auto_adjust=True`).

---

### 3. `screener.py` — Core Screening Engine

**Purpose**: Evaluates a set of technical/fundamental filters against a single stock's price data.

**Filter types** (defined in `FILTER_TYPE_LABELS` and `FILTER_TYPE_DEFAULTS`):

| Filter Key | Label | Default Params | Description |
|-----------|-------|----------------|-------------|
| `ma_rising` | MA Rising | `ma=200` | SMA(ma) is higher than it was 2 bars ago |
| `short_above_long` | Short MA Above Long MA | `short_ma=50, long_ma=200` | Current SMA(short) > SMA(long) |
| `price_near_long` | Price Near & Above Long MA | `long_ma=200, threshold_pct=5.0` | Close ≥ SMA(long) and within threshold_pct% |
| `golden_cross` | Golden Cross | `short_ma=50, long_ma=200, lookback_units=20` | SMA(short) crossed above SMA(long) within N periods |
| `long_ma_down_from_max` | Long MA Down From Max | `long_ma=200, down_pct=5.0, lookback_units=50` | SMA(long) is down_pct% below its recent max |
| `green_candle_today` | Green Candle Today | `min_gain_pct=1.0` | Close > Open AND gain ≥ min_gain_pct% |
| `pe_less_than` | PE < N | `max_pe=30.0` | Trailing PE ratio < max_pe |

**Default filter set** (`DEFAULT_FILTER_SET`):
1. MA(200) Rising
2. Price near & above MA(200) within 5%

**Key functions**:

- `required_ma_periods(filter_set)` — Computes the union of all MA periods needed by the active filters.
- `normalize_filter_set(filter_set)` — Normalizes legacy dict-based and list-based filter formats into the canonical list format. Supports backward compatibility with old saved filter configs.
- `screen_json_file(path, filter_set)` — The main screening function:
  1. Loads the stock's JSON price data into a DataFrame.
  2. Computes all required SMAs.
  3. Evaluates each filter in sequence (PE filter is evaluated last to avoid unnecessary API calls).
  4. Returns a result dict with symbol, price, PE ratio, matched filter labels, and per-filter debug info if the stock passes all filters. Returns `None` if any filter fails.
  5. Each non-PE filter is short-circuit evaluated — the moment a filter fails, the function returns `None`.

- `get_pe_ratio(symbol)` — Tries three PE ratio sources in order:
  1. **yfinance** (`Ticker.info["trailingPE"]`)
  2. **Yahoo Finance Quote API** (`query1.finance.yahoo.com/v7/finance/quote`)
  3. **Screener.in** (HTML scraping for "Stock P/E")
  
  Results are cached via `@lru_cache(maxsize=2048)` in-memory AND persisted to `data/metadata/pe_ratios.json`.

**MA-based helper functions** (used during filter evaluation):
- `ma_rising_from_two_bars_back(df, ma_label)` — Compares current MA value to value 2 bars ago.
- `pct_close_to_ma(price, ma)` — Computes absolute percentage distance.
- `crossed_up(short_ma, long_ma, lookback)` — Detects cross-up events in a rolling window.
- `long_ma_down_from_max(series, down_pct, lookback)` — Measures drawdown from local max.
- `green_candle_today(df, min_gain_pct)` — Checks latest candle and gain from previous close.

---

### 4. `pattern.py` — Swing Pattern Detection & Expression Filtering

**Purpose**: Detects swing highs/lows in price data and evaluates user-defined logical expressions against swing variables.

**Swing Detection** (`detect_swings_from_df`):
- Uses a reversal-percentage algorithm: a swing high/low is confirmed when price reverses by ≥ `reversal_pct%` from the extreme.
- Scans backwards from the most recent bar within the `lookback_days` window.
- Each detected swing records: `type` ("H"/"L"), `index`, `date`, `price`, `days_since`.

**Swing Context** (`build_swing_context`):
- Builds a variable dictionary available in user expressions:
  - `P` — Current closing price
  - `H1`, `H2`, `H3`, ... — Swing high prices (most recent first)
  - `L1`, `L2`, `L3`, ... — Swing low prices (most recent first)
  - `DH1`, `DH2`, `DH3`, ... — Days since each swing high
  - `DL1`, `DL2`, `DL3`, ... — Days since each swing low

**Expression Validation** (`validate_expression`):
- Uses Python's `ast` module to parse and validate expressions safely.
- Only allowed node types: arithmetic, comparisons, boolean operators, function calls.
- Only allowed functions: `abs()`, `min()`, `max()`, `round()`.
- Variable names must match pattern variables (e.g., `H1`, `L2`, `DH3`) or be in the provided allowed set.
- This is a **sandboxed expression evaluator** — no arbitrary code execution.

**Expression Evaluation** (`evaluate_expression`):
- Compiles and evaluates the AST in a restricted environment with `__builtins__={}`.
- Returns a boolean result.

**Batch Evaluation** (`evaluate_pattern_filters`):
- Loads price data, detects swings, builds context, and evaluates all expressions.
- Returns `(passed: bool, swings: list, error: str)`.

---

### 5. `charting.py` — Chart Generation & Results Display

**Purpose**: Generates stock charts with SMA overlays and swing annotations, and renders an interactive HTML results table with hover-to-preview.

**Chart Generation** (`create_stock_chart`):
- Loads price data from JSON and computes required SMAs (same periods as active filters).
- Plots the last `max_points=260` bars using matplotlib.
- Overlays Close price and all SMA lines with distinct colors.
- If `swing_annotations` are provided, plots the last 3 swing highs (red ▼) and last 3 swing lows (green ▲) with labels.
- Saves chart as PNG to `data/charts/`.

**Results Table** (`sortable_results_table` / `results_hover_table_html`):
- Generates raw HTML with inline CSS and JavaScript for a sortable table.
- The "Symbol" column has hover tooltips: mousing over a symbol shows the stock's chart image.
- PE Ratio column is click-sortable (ascending/descending toggle) via embedded JavaScript.
- Chart images are embedded as base64 data URIs (`image_to_data_uri`).

---

### 6. `emailer.py` — Email Delivery

**Purpose**: Sends screener results as a CSV attachment via Gmail SMTP.

- Uses `smtplib.SMTP_SSL` on `smtp.gmail.com:465`.
- Authenticates with Gmail App Password (not the user's regular password).
- Attaches CSV data with `EmailMessage.add_attachment()`.
- No credentials are persisted — they are entered per-send in the UI.

---

### 7. `storage.py` — Persistent Storage Layer

**Purpose**: JSON file-based persistence for application state.

**Files managed**:

| File | Purpose | Functions |
|------|---------|-----------|
| `session_settings.json` | All UI state (download config, filter state, email fields, selected favorite, pattern config) | `load_settings()`, `save_settings()`, `update_settings()` |
| `favourite_filters.json` | Saved named filter sets (MA + pattern combined) | `load_favourite_filter_sets()`, `save_favourite_filter_sets()` |
| `pe_ratios.json` | PE ratio cache `{symbol: pe_value}` | `load_pe_ratios()`, `save_pe_ratios()` |

**Migration**: `load_settings()` checks for a legacy `app_settings.json` file and migrates it to `session_settings.json` on first load.

---

### 8. `app.py` — Streamlit UI (Entry Point)

**Purpose**: The main user interface with three tabs.

#### Tab 1: "Data" — Data Management
- Displays last download timestamp and timeframe.
- Allows uploading/replacing the `MCAP_JUGAAD.xlsx` stock universe file.
- Configuration: download timeframe (DAY/WEEK/MONTH) and stock count limit.
- "Download Stocks Data" button triggers bulk download with progress bar.
- Shows a table of failed downloads, if any.

#### Tab 2: "Screener" — Filter Configuration & Execution
Organized into sections:

1. **MA Based Filtering**:
   - Add/remove filters from the 7 filter types.
   - Each filter type renders its specific parameter inputs inside an expander.
   - Filter state is maintained in `st.session_state["current_filter_set"]`.

2. **Pattern Based Filtering**:
   - Sliders/inputs for Lookback Days and Swing Reversal % (synced via callbacks).
   - Expression-based swing pattern filters: users write expressions like `H1 > H2 and L1 < L2`.
   - Each expression is validated in real-time using `validate_expression()`.

3. **Favorite Filter Sets**:
   - Save current MA + pattern configuration with a name.
   - Load previously saved filter sets via dropdown (triggers `apply_filter_selection_to_state()`).

4. **Run Screener**:
   - Selects timeframe for screening.
   - Optionally generates charts.
   - Iterates over all JSON files in the target timeframe directory, running `screen_json_file()` and `evaluate_pattern_filters()`.
   - Displays progress bar and match count.

#### Tab 3: "Results" — View, Export & Email
- Displays screener results in a sortable, hoverable table.
- "Download Results CSV" button.
- Email form: Gmail ID, App Password, recipient, subject, body → sends results as CSV attachment.

**Session State Management**:
- Uses `st.session_state` for filter sets, pattern expressions, slider values, and results.
- Syncs sliders ↔ number inputs via callback functions.
- Persists most UI state to `session_settings.json` via `update_settings()` on every interaction.

---

## Data Flow

```
┌──────────────────────┐
│  MCAP_JUGAAD.xlsx    │  (User uploads or uses existing)
│  (symbols + mcap)    │
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│   downloader.py      │  Fetches OHLC data via yfinance
│   download_top_stocks│  → data/{daily,weekly,monthly}/*.json
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│   screener.py        │  Loads JSON → computes SMAs → evaluates MA filters
│   screen_json_file() │  Filters are short-circuit evaluated; returns result dict or None
└────────┬─────────────┘
         │ (if MA filters pass)
         ▼
┌──────────────────────┐
│   pattern.py         │  Detects swing highs/lows within lookback window
│   evaluate_pattern_  │  Evaluates user expressions (e.g., H1 > H2 and L1 > L2)
│   filters()          │  Returns (passed, swings, error)
└────────┬─────────────┘
         │ (if pattern passes)
         ▼
┌──────────────────────┐
│   charting.py        │  (Optional) Generates stock chart with SMAs + swing annotations
│   create_stock_chart │  → data/charts/*.png
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│   app.py (Tab 3)     │  Results table, CSV download, email
│   emailer.py         │  Sends CSV via Gmail SMTP
└──────────────────────┘
```

---

## Filter Evaluation Order

Within `screen_json_file()`, filters are evaluated in this order:

1. All **non-PE filters** in the order they appear in the filter set.
2. **PE filter** (`pe_less_than`) is evaluated **last** to avoid unnecessary API calls for stocks that already failed MA filters.

Each filter is **short-circuit evaluated** — if any filter fails, the function immediately returns `None` without evaluating remaining filters.

Pattern expressions are evaluated separately in `evaluate_pattern_filters()` after MA screening passes.

---

## Key Design Decisions

1. **JSON file-based persistence**: All stock data is stored as individual JSON files (one per symbol per timeframe). This avoids a database dependency and makes it easy to inspect/download individual stock data.

2. **No database**: Settings, favorites, and PE cache are stored as JSON files under `data/metadata/`. Simple, portable, and human-readable.

3. **PE ratio multi-source fallback**: Tries yfinance API → Yahoo quote API → Screener.in HTML scraping. Results are cached both in-memory (`@lru_cache`) and on disk (`pe_ratios.json`) to minimize API calls across app restarts.

4. **Sandboxed expression evaluator**: Pattern expressions use Python's `ast` module for safe parsing — no `exec()`, no `eval()` with full builtins. Only whitelisted AST node types and functions are permitted.

5. **Short-circuit filter evaluation**: Filters are evaluated sequentially and abort on first failure, minimizing unnecessary computation.

6. **Backward compatibility**: The `normalize_filter_set()` function in `screener.py` supports both legacy dict-format filter configs and the current list-based format, ensuring old saved favorites continue to work.

7. **Chart images as base64 data URIs**: Charts are embedded directly in the HTML table, enabling hover previews without requiring a separate image server.

8. **Settings auto-migration**: `storage.py` automatically migrates `app_settings.json` → `session_settings.json` on first load.

---

## Dependencies (`requirements.txt`)

| Package | Version | Purpose |
|---------|---------|---------|
| `streamlit` | * | Web UI framework |
| `yfinance` | * | Yahoo Finance data download |
| `pandas` | * | Data manipulation & Excel reading |
| `openpyxl` | * | Excel file I/O (engine for pandas) |
| `matplotlib` | * | Chart generation |

---

## Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Launch the app
streamlit run app.py
```

### Prerequisites
- Place `MCAP_JUGAAD.xlsx` in `data/excel/` with columns containing symbol codes and market cap.
- For email: a Gmail account with an [App Password](https://support.google.com/accounts/answer/185833) configured.