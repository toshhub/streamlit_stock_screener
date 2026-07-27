"""Monthly Screener.in valuation-history sync used by GitHub Actions."""

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
        raise RuntimeError("No Indian symbols were found for valuation sync.")
    rows, failures = collect_monthly_valuations({MARKET_INDIA: symbols})
    successful_symbols = len({
        row["Symbol"] for row in rows if row.get("Symbol")
    })
    print(
        f"Monthly valuation sync complete: {successful_symbols}/{len(symbols)} "
        f"symbols refreshed; {len(rows)} monthly rows; {len(failures)} failures."
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
        "successful_symbols": successful_symbols,
        "rows": len(rows),
        "failures": failures,
    }


if __name__ == "__main__":
    main()
