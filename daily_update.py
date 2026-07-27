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
from market_snapshots import refresh_latest_stock_values
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
    market_summaries = []
    for market in (MARKET_INDIA, MARKET_US):
        try:
            symbols_file = symbols_file_for_market(market)
            symbols = load_top_symbols(
                symbols_file,
                limit=1_000_000,
                market=market,
            )
            if not symbols:
                raise RuntimeError(f"No {market} symbols were found.")
            symbols_by_market[market] = symbols
            rows = download_top_stocks(
                symbols_file,
                "DAY",
                limit=len(symbols),
                incremental=True,
                market=market,
            )
            market_failures = [
                row for row in rows if not row.get("Downloaded")
            ]
            failures.extend(
                {"Market": market, **row} for row in market_failures
            )
            market_summaries.append({
                "Market": market,
                "Symbols": len(symbols),
                "Updated": sum(
                    row.get("Status") in {"Updated", "Full download"}
                    for row in rows
                ),
                "Current": sum(
                    row.get("Status") == "Already current" for row in rows
                ),
                "Failed": len(market_failures),
            })
            if market == MARKET_INDIA:
                nifty = download_nifty_index(
                    "DAY",
                    incremental=True,
                    market=market,
                )
                if not nifty.get("Downloaded"):
                    failures.append({"Market": market, **nifty})
        except Exception as exc:
            # A source-level failure in one market must not prevent the other
            # market from reconciling and committing its successful updates.
            failures.append({
                "Market": market,
                "Symbol": "*",
                "Downloaded": False,
                "Status": "Market update failed",
                "Error": str(exc),
            })
    refresh_latest_stock_values({MARKET_INDIA: DAILY_DIR, MARKET_US: US_DAILY_DIR})
    print(f"Daily update complete. Candle failures: {len(failures)}")
    for summary in market_summaries:
        print(
            "{Market}: {Symbols} symbols; {Updated} updated; "
            "{Current} already current; {Failed} failed".format(**summary)
        )
    for failure in failures:
        print(
            "FAILED {Market}:{Symbol} - {Error}".format(
                Market=failure.get("Market", ""),
                Symbol=failure.get("Symbol", ""),
                Error=failure.get("Error") or failure.get("Status", "Unknown error"),
            )
        )
    return {
        "markets": market_summaries,
        "candle_failures": failures,
    }


if __name__ == "__main__":
    main()
