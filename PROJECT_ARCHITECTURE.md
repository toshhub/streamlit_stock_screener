# Stock Screener: Canonical Architecture and Engineering Guide

This is the canonical development document for the application. Read it before
changing or diagnosing the app. `AGENTS.md` defines the required reading order
for future Codex work.

Last architecture review: 2026-08-01.

## Product scope

The project is a Streamlit stock-screening and analysis application supporting
Indian and US markets. Its primary feature set is:

1. Data management and server-cache visibility.
2. Configurable technical, price-action, pattern, and valuation screening.
3. Historical backtesting of saved filter sets.
4. Results tables with sortable metrics, static chart previews, and fast
   interactive charts.
5. A dedicated searchable interactive Chart workspace.
6. Authenticated personal watchlists.
7. Authenticated price alerts with automatic and manual cached-candle checks.
8. Historical fundamentals and valuation comparisons.

The seven main tabs are Data, Screener, Backtest, Results, Chart, Watchlists,
and Alerts. The labels and indexes are defined by `MAIN_TAB_LABELS` in `app.py`.

## Runtime architecture

### Streamlit application

`app.py` is the UI controller and request router. It initializes cloud storage,
authentication, R2 cache synchronization, application state, and the seven tab
workspaces.

Streamlit evaluates normal tab bodies during an app run. Consequently, hidden
tabs must not perform avoidable network calls, chart generation, or other heavy
work. Main tabs deliberately use `on_change="ignore"` so merely selecting a tab
does not rerun the entire application.

Use Streamlit reruns for genuine server-state transitions only. Do not use them
for tab selection, chart-range changes, autocomplete, chart navigation, or
other interactions that can remain within a browser component.

### Canonical candle storage

Cloudflare R2 is the deployed source of truth for candles. The remote layout is:

```text
stock-data/
  manifest.json
  india/yearly/YYYY.parquet
  india/current/YYYY-MM.json
  us/yearly/YYYY.parquet
  us/current/YYYY-MM.json
```

`r2_stock_data.py` owns manifest validation, downloads, revisions, atomic local
cache activation, and local per-symbol materialization.

The Streamlit server uses two forms of local materialized cache:

- Market Parquet data for bulk screening and cache-aware processing.
- Ten-year per-symbol Parquet files for charts, backtests, and alert checks.

Existing per-symbol JSON support is retained for local/downloader compatibility,
but new deployed features must treat R2 plus the active local materialization as
canonical. Read `STOCK_DATA_ARCHITECTURE.md` before changing this subsystem.

### Stock universes

- India source universe: `data/excel/MCAP_JUGAAD.xlsx`.
- US source universe: `data/excel/nasdaq_screener_1784114565446.csv`.
- The cached R2 manifest also contains the currently available symbol list for
  each market.

Market identifiers are `INDIA` and `US`. Normalize them through existing helper
functions instead of duplicating normalization logic.

## Interactive chart architecture

Interactive charts have one canonical server route and one shared component
stack.

### Canonical route

- `chart_context.interactive_chart_query()` builds chart query strings.
- `app.run_interactive_chart_view()` handles `interactive_chart` requests early
  and stops the normal app execution path.
- The isolated route loads only the selected symbol, cached fundamentals,
  valuation data, watchlist context, and alert markers needed by that chart.
- `charting.render_interactive_stock_chart()` renders the candle chart and its
  controls.

Never render the full interactive chart inline inside the normal seven-tab app
run. Doing so causes chart events to rebuild every tab and was the root cause of
historically slow, repeatedly refreshing Chart-tab behavior.

### Results and Alerts

`charting.sortable_results_table()` uses the declared Results/Alerts component
and `results_hover_table_html()`.

Interactive chart icons create an eager iframe pointing at the canonical
embedded chart route. Opening, navigating, zooming, and changing ranges occurs
inside browser-side JavaScript and must not rerun Streamlit.

Static PNG chart paths are passed through `ChartPath`. Results and Alerts share
the same table renderer and therefore share chart previews and PE-based row
coloring.

### Dedicated Chart tab

`charting.results_style_chart_workspace()` is the standalone Chart-tab entry to
the same component stack used by Results. `app.py` supplies any initial chart
context, cached market symbol lists, and optional navigation context.

The Chart tab's Market selector, Stock name input, autocomplete, chart opening,
range changes, close behavior, and in-chart symbol navigation are browser-side.
Typing or selecting a symbol directly changes the embedded iframe URL. It must
not call `st.rerun()`, poll the server, or rebuild the main app.

Autocomplete lists come from the local cached R2 manifest. Filtering happens
locally on each input event, prefix matches rank first, and only the first 12
matches are rendered. This keeps suggestions instantaneous without polling.

Performance invariants for every chart change:

- Results, Alerts, and Chart must use `interactive_chart_query()` and the
  isolated embedded route.
- Do not introduce a second interactive renderer.
- Do not fetch candle history from the browser when the server cache already
  contains it.
- Do not add Streamlit widgets whose change callbacks are required merely to
  choose or navigate a chart.
- Keep component keys stable so unchanged iframes survive unrelated app reruns.
- Hidden Chart content must not initiate unnecessary chart requests.

## Screening and backtesting

`screener.py` defines filter labels, defaults, normalization, required moving
averages, data loading, and filter evaluation. Expensive valuation work is
deferred until cheaper filters pass where possible.

`pattern.py` evaluates custom expressions through a restricted AST. Never expose
arbitrary Python execution. Update validation, runtime context, and UI help
together when adding expression features.

`backtest.py` owns historical entry/exit and portfolio calculations. Backtest
charts and result navigation should reuse existing chart helpers rather than
creating another candle-loading path.

The live screener uses background workers and event queues. Matching rows can be
published before optional static chart generation completes. Preserve this
progressive-results behavior.

## Fundamentals and valuation

`monthly_valuation_update.py` collects monthly Screener.in valuation history and
fundamentals. The monthly GitHub workflow persists:

- `data/metadata/monthly_valuations.parquet`
- `data/metadata/screener_fundamentals.json`

`market_snapshots.py` hydrates result rows with current PE and historical
3-year, 5-year, and 10-year PE medians from local data. `fundamentals.py`
provides cached company growth and valuation payloads.

`charting.historical_pe_valuation_state()` drives valuation presentation:

- Favorable/green when current PE is below the relevant historical reference.
- Unfavorable/red when current PE is above it.
- Neutral when the comparison is unavailable.

Results and Alerts must pass both `PE Ratio` and `ValuationMedians` to the shared
table renderer for consistent styling.

## Price-alert architecture

### Ownership and persistence

Authenticated alerts are stored in Supabase `public.user_alerts` and scoped by
`user_id`. `cloud_storage.py` provides user-scoped UI operations plus server-side
cron queries. Guest alert writes are disabled when authenticated cloud storage
is required.

An alert records its symbol, market, target and reference prices, direction,
status, creation/check dates, trigger candle/date, and acknowledgement state.

### Trigger semantics

`price_alerts._evaluate_alerts()` is the shared evaluator. Only candles after
the alert's trigger boundary are eligible. An above alert triggers when a future
candle reaches the target through its High; a below alert uses its Low. Changed
alerts update `last_checked_date`; triggered alerts record the triggering candle
date and become `Triggered`.

Do not create a second trigger algorithm. Cron, symbol checks, and manual refresh
must all use the shared evaluator.

### Automatic cron triggering

`.github/workflows/streamlit-cron.yml` runs `daily_update.py` twice daily:

- 11:30 UTC (17:00 IST), after the Indian close.
- 23:30 UTC (05:00 IST the following day), after the completed US session.

The job updates candles and R2 snapshots, loads active Supabase alerts by market,
evaluates them against downloaded candles, and persists changed rows. It needs
R2 credentials plus `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` as GitHub
Actions secrets. Never place these values in source or documentation.

### Manual refresh and Alerts UI

The Alerts tab's **Refresh Alerts** button calls
`price_alerts.refresh_price_alerts_from_cache()`. It loads only the current
authenticated user's active alerts, reads already materialized daily candles,
uses the shared evaluator, and persists only that user's changed alerts.

The Alerts tables use the Results table renderer. They include PE valuation
coloring and reusable static chart previews. Static chart generation should
occur only when needed and should reuse valid cached PNGs.

Alert UI groups are:

- Active Alerts: monitoring.
- New Alerts: triggered and not acknowledged.
- Old Alerts: triggered and acknowledged.

## Authentication and personal cloud data

`user_auth.py` manages Google login. `cloud_storage.py` is the Supabase boundary.
The schema is in `supabase_schema.sql`.

Authenticated personal data includes settings, favorite filters, watchlists,
alerts, and the latest screener result. Shared candle data and shared filter
definitions are not user-owned.

All personal reads and writes must remain scoped by `user_id`. The Supabase
service-role key is server-only. Read `CLOUD_SETUP.md` and the schema before
changing authentication or personal persistence.

## Scheduled workflows

### Daily candles and alerts

`.github/workflows/streamlit-cron.yml` executes `daily_update.py` twice daily and
does not commit candle data to Git. R2 is the output store.

### Monthly valuation history

`.github/workflows/monthly-valuations.yml` runs on the second day of each month,
updates the consolidated valuation/fundamental files, and commits those data
artifacts when changed.

Workflow changes must preserve secret names, avoid logging credentials, and keep
alert evaluation after candle normalization.

## Important module map

| File | Responsibility |
|---|---|
| `app.py` | Streamlit UI, request routing, tab orchestration, cache status |
| `charting.py` | Static charts, interactive chart renderer, shared table/chart components |
| `chart_context.py` | Canonical chart URLs, overlays, alert-marker normalization |
| `r2_stock_data.py` | R2 manifests, revisions, local materialization, candle reads |
| `stock_data.py` | Unified local symbol/path/data access |
| `downloader.py` | Universe loading and Yahoo Finance updates |
| `daily_update.py` | Scheduled multi-market candle, snapshot, and alert update |
| `screener.py` | Filter definitions and screening engine |
| `pattern.py` | Safe expression and pattern evaluation |
| `backtest.py` | Historical strategy evaluation |
| `market_snapshots.py` | Monthly PE/fundamental snapshot hydration |
| `fundamentals.py` | Cached company metrics and valuation payloads |
| `price_alerts.py` | Alert creation, shared evaluation, refresh, acknowledgement |
| `cloud_storage.py` | Supabase persistence and user scoping |
| `storage.py` | Shared/local settings and fallback persistence |
| `user_auth.py` | Google authentication and account controls |
| `supabase_schema.sql` | Personal cloud-data schema and access restrictions |

## Testing and verification

Use the repository virtual environment when available:

```powershell
.\.venv\Scripts\python.exe -m py_compile app.py charting.py
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
git diff --check
```

For UI behavior, start Streamlit on a temporary port, verify the affected path,
then stop the exact verified Streamlit process. Do not leave localhost servers
running.

Relevant test groups include chart rendering/navigation, alert evaluation and
cloud persistence, R2 cache behavior, market snapshots, screener pipelines,
authentication, and UI source invariants.

## Change checklist

Before implementation:

1. Read this file and the required supplemental documents from `AGENTS.md`.
2. Inspect both the producer and consumer of changed data.
3. Search for existing shared helpers before adding a new path.
4. Identify whether an interaction can remain browser-side.
5. Check the worktree and preserve unrelated user changes.

Before handoff:

1. Run focused tests.
2. Run the complete test suite for application changes.
3. Run `git diff --check`.
4. Verify relevant UI behavior when the change is visual or lifecycle-sensitive.
5. Stop temporary servers.
6. Update this document if an architectural invariant or feature set changed.

## Documentation hierarchy

- `AGENTS.md`: mandatory instructions and reading order.
- `PROJECT_ARCHITECTURE.md`: canonical current architecture and feature set.
- `STOCK_DATA_ARCHITECTURE.md`: detailed R2/cache design.
- `CLOUD_SETUP.md`: Google OAuth, Supabase, R2, and secret configuration.
- `DATA_MIGRATION.md`: migration procedures.
- `CONTEXT.md` and `ARCHITECTURE.md`: historical background; they may describe
  older layouts and must not override this document.
