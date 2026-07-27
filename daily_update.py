"""Headless daily update used by GitHub Actions."""

import os

from cloud_storage import SupabaseCloudStorage
from config import DAILY_DIR, US_DAILY_DIR
from downloader import (
    MARKET_INDIA,
    MARKET_US,
    download_nifty_index,
    download_top_stocks,
    load_top_symbols,
)
from market_snapshots import collect_monthly_valuations, refresh_latest_stock_values
from price_alerts import configure_cloud_alerts
from app_paths import symbols_file_for_market


def main():
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    configure_cloud_alerts(
        SupabaseCloudStorage(supabase_url, service_key)
        if supabase_url and service_key else None,
        require_auth=True,
    )
    symbols_by_market = {}
    failures = []
    for market in (MARKET_INDIA, MARKET_US):
        symbols_file = symbols_file_for_market(market)
        symbols = load_top_symbols(symbols_file, limit=1_000_000, market=market)
        symbols_by_market[market] = symbols
        rows = download_top_stocks(
            symbols_file, "DAY", limit=len(symbols), incremental=True, market=market
        )
        failures.extend(row for row in rows if not row.get("Downloaded"))
        if market == MARKET_INDIA:
            nifty = download_nifty_index("DAY", incremental=True, market=market)
            if not nifty.get("Downloaded"):
                failures.append(nifty)
    refresh_latest_stock_values({MARKET_INDIA: DAILY_DIR, MARKET_US: US_DAILY_DIR})
    _, valuation_failures = collect_monthly_valuations(symbols_by_market)
    print(
        f"Daily update complete. Candle failures: {len(failures)}; "
        f"valuation failures: {len(valuation_failures)}"
    )


if __name__ == "__main__":
    main()
