import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from r2_stock_data import R2DataError, manifest_entry
from r2_update import update_market_month


class _FakeClient:
    def __init__(self, objects, *, corrupt_upload=False):
        self.objects = dict(objects)
        self.corrupt_upload = corrupt_upload
        self.put_keys = []

    def put_object(self, Bucket, Key, Body, ContentType):
        del Bucket, ContentType
        self.put_keys.append(Key)
        self.objects[Key] = b"corrupt" if self.corrupt_upload else bytes(Body)


class _FakeStore:
    def __init__(self, objects, *, corrupt_upload=False):
        self.settings = SimpleNamespace(bucket="test")
        self.client = _FakeClient(objects, corrupt_upload=corrupt_upload)

    @staticmethod
    def key(relative):
        return f"stock-data/{relative}"

    @staticmethod
    def _entry_key(entry):
        return entry.get("key", "") if isinstance(entry, dict) else str(entry)

    def _object_bytes(self, key):
        try:
            return self.client.objects[key]
        except KeyError as exc:
            error = RuntimeError(key)
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error from exc


def _candle(date, close):
    return {
        "Symbol": "TEST",
        "Date": date,
        "Open": close - 1,
        "High": close + 1,
        "Low": close - 2,
        "Close": close,
        "Adj Close": close,
        "Volume": 100,
    }


class R2UpdatePublicationTests(unittest.TestCase):
    def _state(self, *, corrupt_upload=False):
        legacy_key = "stock-data/india/current/2026-08.json"
        legacy_payload = json.dumps([_candle("2026-08-03", 100)]).encode()
        store = _FakeStore(
            {legacy_key: legacy_payload},
            corrupt_upload=corrupt_upload,
        )
        manifest = {
            "markets": {
                "india": {
                    "yearly": {},
                    "current": {
                        "2026-08": manifest_entry(
                            legacy_key,
                            legacy_payload,
                            rows=1,
                        )
                    },
                    "symbols": ["TEST"],
                }
            }
        }
        downloaded = pd.DataFrame([_candle("2026-08-04", 101)])
        return store, manifest, legacy_key, legacy_payload, downloaded

    def test_monthly_snapshot_uses_verified_content_addressed_key(self):
        store, manifest, legacy_key, legacy_payload, downloaded = self._state()

        with patch("r2_update._download_symbol", return_value=downloaded):
            summary = update_market_month(
                store,
                manifest,
                "INDIA",
                now="2026-08-04T12:00:00Z",
                symbols=["TEST"],
            )

        published = manifest["markets"]["india"]["current"]["2026-08"]
        self.assertRegex(
            published["key"],
            re.compile(r"^stock-data/india/current/2026-08-[0-9a-f]{16}\.json$"),
        )
        self.assertEqual(store.client.put_keys, [published["key"]])
        self.assertEqual(
            store.client.objects[published["key"]],
            store._object_bytes(published["key"]),
        )
        self.assertEqual(store.client.objects[legacy_key], legacy_payload)
        self.assertEqual(summary["Rows"], 2)

    def test_failed_upload_verification_does_not_replace_manifest_entry(self):
        store, manifest, legacy_key, _payload, downloaded = self._state(
            corrupt_upload=True
        )

        with (
            patch("r2_update._download_symbol", return_value=downloaded),
            self.assertRaisesRegex(R2DataError, "checksum verification"),
        ):
            update_market_month(
                store,
                manifest,
                "INDIA",
                now="2026-08-04T12:00:00Z",
                symbols=["TEST"],
            )

        current = manifest["markets"]["india"]["current"]["2026-08"]
        self.assertEqual(current["key"], legacy_key)


if __name__ == "__main__":
    unittest.main()
