"""Headless Cloudflare R2 update used by GitHub Actions."""

from r2_update import run_update


def main():
    result = run_update()
    for summary in result["markets"]:
        print(
            "{Market}: {Rows} rows across {Symbols} symbols in {Month}; "
            "{failed} Yahoo failures".format(
                failed=len(summary["Failures"]),
                **summary,
            )
        )
    for rollover in result["rollovers"]:
        print("Finalized {Market} {Year}: {Rows} rows".format(**rollover))
    return result


if __name__ == "__main__":
    main()
