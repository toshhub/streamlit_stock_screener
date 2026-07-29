"""One-time conversion from yearly candle Parquet files to stock JSON files."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import DAILY_DIR, DATA_DIR, US_DAILY_DIR
from stock_data import migrate_parquet_symbol


LEGACY_INDIA_DAILY_DIR = DATA_DIR / "daily"


def migrate_directory(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    data_root = DATA_DIR.resolve()
    if data_root not in source.parents or data_root not in destination.parents:
        raise ValueError("Migration paths must stay inside the project data directory.")
    destination.mkdir(parents=True, exist_ok=True)
    symbol_dirs = sorted(
        child
        for child in source.iterdir()
        if child.is_dir() and any(child.glob("*.parquet"))
    )
    migrated = 0
    failures = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(
                migrate_parquet_symbol,
                symbol_dir,
                destination,
                10,
            ): symbol_dir
            for symbol_dir in symbol_dirs
        }
        for index, future in enumerate(as_completed(futures), start=1):
            symbol_dir = futures[future]
            try:
                future.result()
                migrated += 1
            except Exception as exc:
                failures.append((str(symbol_dir), str(exc)))
            if index % 250 == 0 or index == len(symbol_dirs):
                print(
                    f"{source}: processed {index}/{len(symbol_dirs)} "
                    f"({migrated} converted, {len(failures)} failed)",
                    flush=True,
                )
    if source != destination:
        try:
            source.rmdir()
        except OSError:
            pass
    return migrated, failures


def main():
    migrated = 0
    failures = []
    for source, destination in (
        (LEGACY_INDIA_DAILY_DIR, DAILY_DIR),
        (US_DAILY_DIR, US_DAILY_DIR),
    ):
        if not source.exists():
            continue
        converted, conversion_failures = migrate_directory(source, destination)
        migrated += converted
        failures.extend(conversion_failures)
    print(f"Migrated {migrated} symbols; failures: {len(failures)}")
    for path, error in failures:
        print(f"FAILED {path}: {error}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
