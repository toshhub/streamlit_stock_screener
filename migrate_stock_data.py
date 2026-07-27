"""One-time migration of tracked stock JSON files to yearly Parquet."""

from config import DAILY_DIR, US_DAILY_DIR
from stock_data import migrate_legacy_json


def main():
    migrated = 0
    failed = []
    for directory in (DAILY_DIR, US_DAILY_DIR):
        json_files = sorted(directory.glob("*.json"))
        for index, json_file in enumerate(json_files, start=1):
            try:
                migrate_legacy_json(directory / json_file.stem)
                migrated += 1
            except Exception as exc:
                failed.append((str(json_file), str(exc)))
            if index % 250 == 0:
                print(f"{directory}: processed {index}/{len(json_files)}")
    print(f"Migrated {migrated} symbols; failures: {len(failed)}")
    for path, error in failed:
        print(f"FAILED {path}: {error}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
