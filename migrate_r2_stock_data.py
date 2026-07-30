"""One-time migration from per-symbol JSON files into the R2 layout.

Run this before removing the old candle checkout, or point ``--source-root`` at
an archived copy containing ``india/daily`` and ``us/daily``.
"""

from __future__ import annotations

import argparse
import io
import json
import tomllib
from pathlib import Path

import pandas as pd

from r2_stock_data import (
    configure_r2,
    dataframe_json_bytes,
    get_r2_store,
    manifest_entry,
    normalize_candles,
)


def _aggregate_period(files, *, year, month=None):
    frames = []
    for path in files:
        try:
            rows = pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if rows.empty or "Date" not in rows.columns:
            continue
        rows["Date"] = pd.to_datetime(rows["Date"], errors="coerce")
        mask = rows["Date"].dt.year.eq(year)
        if month is not None:
            mask &= rows["Date"].dt.month.eq(month)
        selected = rows.loc[mask].copy()
        if selected.empty:
            continue
        selected["Symbol"] = path.stem.upper()
        frames.append(selected)
    return normalize_candles(
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["Symbol", "Date", "Close"])
    )


def migrate(source_root, *, completed_through, current_year):
    store = get_r2_store()
    if not store.settings.configured:
        raise RuntimeError("Cloudflare R2 is not configured.")
    manifest = {"schema_version": 1, "markets": {}}
    for market in ("india", "us"):
        files = sorted((Path(source_root) / market / "daily").glob("*.json"))
        market_manifest = manifest["markets"].setdefault(
            market,
            {"yearly": {}, "current": {}},
        )
        all_symbols = {path.stem.upper() for path in files}
        for year in range(2017, int(completed_through) + 1):
            frame = _aggregate_period(files, year=year)
            if frame.empty:
                continue
            buffer = io.BytesIO()
            frame.to_parquet(
                buffer,
                index=False,
                engine="pyarrow",
                compression="zstd",
                row_group_size=10_000,
            )
            payload = buffer.getvalue()
            key = store.key(f"{market}/yearly/{year}.parquet")
            store.client.put_object(
                Bucket=store.settings.bucket,
                Key=key,
                Body=payload,
                ContentType="application/octet-stream",
            )
            market_manifest["yearly"][str(year)] = manifest_entry(
                key,
                payload,
                rows=len(frame),
            )
            print(
                f"Uploaded {market}/yearly/{year}.parquet "
                f"({len(frame):,} rows)",
                flush=True,
            )
        latest_date = None
        for month in range(1, 13):
            frame = _aggregate_period(
                files,
                year=int(current_year),
                month=month,
            )
            if frame.empty:
                continue
            payload = dataframe_json_bytes(frame)
            period = f"{int(current_year):04d}-{month:02d}"
            key = store.key(f"{market}/current/{period}.json")
            store.client.put_object(
                Bucket=store.settings.bucket,
                Key=key,
                Body=payload,
                ContentType="application/json",
            )
            market_manifest["current"][period] = manifest_entry(
                key,
                payload,
                rows=len(frame),
            )
            print(
                f"Uploaded {market}/current/{period}.json "
                f"({len(frame):,} rows)",
                flush=True,
            )
            latest_date = frame["Date"].max()
        market_manifest["symbols"] = sorted(all_symbols)
        market_manifest["symbol_count"] = len(all_symbols)
        if latest_date is not None:
            market_manifest["latest_date"] = latest_date.strftime("%Y-%m-%d")

    from r2_update import upload_manifest

    return upload_manifest(store, manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default="data")
    parser.add_argument("--completed-through", type=int, default=2025)
    parser.add_argument("--current-year", type=int, default=2026)
    parser.add_argument(
        "--secrets-file",
        help="Optional TOML file containing an [r2] section.",
    )
    args = parser.parse_args()
    if args.secrets_file:
        secrets = tomllib.loads(
            Path(args.secrets_file).read_text(encoding="utf-8")
        )
        configure_r2(secrets.get("r2", {}), force=True)
    manifest = migrate(
        args.source_root,
        completed_through=args.completed_through,
        current_year=args.current_year,
    )
    print(f"Uploaded R2 manifest version {manifest['version']}.")


if __name__ == "__main__":
    main()
