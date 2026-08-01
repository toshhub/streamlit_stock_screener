"""Bootstrap Screener.in fundamentals before the monthly combined cron."""

from app_paths import symbols_file_for_market
from downloader import MARKET_INDIA, load_top_symbols
from market_snapshots import collect_monthly_valuations


def main():
    symbols = load_top_symbols(
        symbols_file_for_market(MARKET_INDIA),
        limit=1_000_000,
        market=MARKET_INDIA,
    )
    if not symbols:
        raise RuntimeError("No Indian symbols were found for fundamentals bootstrap.")
    rows, failures = collect_monthly_valuations(
        {MARKET_INDIA: symbols},
        fundamentals_first_cut=True,
    )
    print(
        f"Fundamentals first cut complete for {len(symbols)} symbols; "
        f"{len(rows)} new valuation rows; {len(failures)} failures."
    )
    for failure in failures:
        print(
            "FAILED {Market}:{Symbol} - {Error}".format(
                Market=failure.get("Market", ""),
                Symbol=failure.get("Symbol", ""),
                Error=failure.get("Error", "Unknown error"),
            )
        )
    return {
        "symbols": len(symbols),
        "valuation_rows": len(rows),
        "failures": failures,
    }


if __name__ == "__main__":
    main()
