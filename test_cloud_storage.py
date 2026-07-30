import unittest

import pandas as pd

from cloud_storage import SupabaseCloudStorage


class _Response:
    def __init__(self, data):
        self.data = data


class _ResultQuery:
    def __init__(self, rows):
        self.rows = rows
        self.operation = ""
        self.payload = None
        self.user_id = ""

    def select(self, *_args):
        self.operation = "select"
        return self

    def eq(self, column, value):
        if column == "user_id":
            self.user_id = str(value)
        return self

    def limit(self, _value):
        return self

    def upsert(self, payload, on_conflict=None):
        self.operation = "upsert"
        self.payload = dict(payload)
        self.user_id = str(payload["user_id"])
        self.on_conflict = on_conflict
        return self

    def execute(self):
        if self.operation == "upsert":
            self.rows[self.user_id] = dict(self.payload)
            return _Response([dict(self.payload)])
        row = self.rows.get(self.user_id)
        return _Response([dict(row)] if row else [])


class _FakeClient:
    def __init__(self):
        self.rows = {}
        self.tables = []

    def table(self, name):
        self.tables.append(name)
        return _ResultQuery(self.rows)


class CloudScreenerResultTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseCloudStorage("https://example.invalid", "secret")
        self.client = _FakeClient()
        self.store._client = self.client

    def test_latest_result_upsert_replaces_previous_run(self):
        self.store.save_last_screener_result(
            "user-1",
            [{"Symbol": "OLD"}],
            {"market": "INDIA"},
        )
        self.store.save_last_screener_result(
            "user-1",
            [{"Symbol": "NEW"}],
            {"market": "US"},
        )

        restored = self.store.load_last_screener_result("user-1")

        self.assertEqual(restored["rows"], [{"Symbol": "NEW", "ChartPath": ""}])
        self.assertEqual(restored["metadata"]["market"], "US")
        self.assertEqual(len(self.client.rows), 1)

    def test_result_payload_removes_server_chart_paths_and_is_json_safe(self):
        saved = self.store.save_last_screener_result(
            "user-2",
            [{
                "Symbol": "TEST",
                "ChartPath": "C:/temporary/chart.png",
                "ChartSrc": "data:image/png;base64,abc",
                "Latest Date": pd.Timestamp("2026-07-30"),
                "PE Ratio": float("nan"),
            }],
            {"run_at": pd.Timestamp("2026-07-30 17:00:00")},
        )

        row = saved["rows"][0]
        self.assertEqual(row["ChartPath"], "")
        self.assertNotIn("ChartSrc", row)
        self.assertEqual(row["Latest Date"], "2026-07-30T00:00:00")
        self.assertIsNone(row["PE Ratio"])
        self.assertEqual(
            saved["metadata"]["run_at"],
            "2026-07-30T17:00:00",
        )


if __name__ == "__main__":
    unittest.main()
