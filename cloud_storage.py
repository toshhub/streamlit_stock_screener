"""Server-side Supabase persistence for Google-authenticated app users.

The service-role key is intentionally used only by this Streamlit server. Every
user-facing method requires and filters by the verified Google OIDC subject.
"""

import os
import threading


class CloudStorageError(RuntimeError):
    pass


def _secret_value(st, section, key, environment_key):
    try:
        value = st.secrets.get(section, {}).get(key, "")
    except Exception:
        value = ""
    return str(value or os.environ.get(environment_key, "")).strip()


def cloud_storage_from_config(st):
    """Build the cloud store, or return None when Supabase is not configured."""
    url = _secret_value(st, "supabase", "url", "SUPABASE_URL")
    service_key = _secret_value(
        st,
        "supabase",
        "service_role_key",
        "SUPABASE_SERVICE_ROLE_KEY",
    )
    if not url or not service_key:
        return None
    return SupabaseCloudStorage(url, service_key)


class SupabaseCloudStorage:
    def __init__(self, url, service_role_key):
        self.url = str(url).rstrip("/")
        self.service_role_key = str(service_role_key)
        self._client = None
        self._lock = threading.RLock()
        self._settings_cache = {}

    @property
    def client(self):
        with self._lock:
            if self._client is None:
                try:
                    from supabase import create_client
                except ImportError as exc:
                    raise CloudStorageError(
                        "The 'supabase' package is not installed. Install the project requirements."
                    ) from exc
                self._client = create_client(self.url, self.service_role_key)
            return self._client

    @staticmethod
    def _require_user(user_id):
        clean = str(user_id or "").strip()
        if not clean:
            raise PermissionError("Sign in with Google to use personal cloud data.")
        return clean

    def load_filter_sets(self, user_id):
        user_id = self._require_user(user_id)
        try:
            response = (
                self.client.table("user_filter_sets")
                .select("name,filter_data")
                .eq("user_id", user_id)
                .order("name")
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not load personal favorite filters: {exc}") from exc
        return {
            str(row["name"]): row.get("filter_data", {})
            for row in (response.data or [])
            if row.get("name")
        }

    def load_settings(self, user_id):
        user_id = self._require_user(user_id)
        if user_id in self._settings_cache:
            return dict(self._settings_cache[user_id])
        try:
            response = (
                self.client.table("user_settings")
                .select("settings")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not load personal settings: {exc}") from exc
        if not response.data:
            settings = {}
        else:
            settings = response.data[0].get("settings", {})
            settings = dict(settings) if isinstance(settings, dict) else {}
        self._settings_cache[user_id] = settings
        return dict(settings)

    def update_settings(self, user_id, updates):
        user_id = self._require_user(user_id)
        current = self.load_settings(user_id)
        updates = dict(updates)
        if all(current.get(key) == value for key, value in updates.items()):
            return current
        current.update(updates)
        try:
            (
                self.client.table("user_settings")
                .upsert(
                    {"user_id": user_id, "settings": current},
                    on_conflict="user_id",
                )
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not save personal settings: {exc}") from exc
        self._settings_cache[user_id] = current
        return current

    def save_filter_set(self, user_id, name, filter_data):
        user_id = self._require_user(user_id)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Favorite filter name is required.")
        row = {
            "user_id": user_id,
            "name": clean_name,
            "filter_data": filter_data,
        }
        try:
            (
                self.client.table("user_filter_sets")
                .upsert(row, on_conflict="user_id,name")
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not save the personal favorite filter: {exc}") from exc

    def delete_filter_set(self, user_id, name):
        user_id = self._require_user(user_id)
        try:
            response = (
                self.client.table("user_filter_sets")
                .delete()
                .eq("user_id", user_id)
                .eq("name", str(name))
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not remove the personal favorite filter: {exc}") from exc
        return len(response.data or [])

    def load_watchlists(self, user_id):
        user_id = self._require_user(user_id)
        try:
            response = (
                self.client.table("user_watchlists")
                .select("id,name,position,user_watchlist_items(symbol,market,note,position)")
                .eq("user_id", user_id)
                .order("position")
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not load personal watchlists: {exc}") from exc
        watchlists = []
        for row in response.data or []:
            items = sorted(
                row.get("user_watchlist_items") or [],
                key=lambda item: int(item.get("position", 0)),
            )
            watchlists.append({**row, "items": items})
            watchlists[-1].pop("user_watchlist_items", None)
        return watchlists

    def save_watchlist(self, user_id, watchlist_id, name, position=0):
        user_id = self._require_user(user_id)
        row = {
            "user_id": user_id,
            "id": str(watchlist_id),
            "name": str(name).strip(),
            "position": int(position),
        }
        if not row["name"]:
            raise ValueError("Watchlist name is required.")
        try:
            self.client.table("user_watchlists").upsert(
                row, on_conflict="user_id,id"
            ).execute()
        except Exception as exc:
            raise CloudStorageError(f"Could not save the personal watchlist: {exc}") from exc
        return row

    def delete_watchlist(self, user_id, watchlist_id):
        user_id = self._require_user(user_id)
        try:
            response = (
                self.client.table("user_watchlists").delete()
                .eq("user_id", user_id).eq("id", str(watchlist_id)).execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not delete the personal watchlist: {exc}") from exc
        return len(response.data or [])

    def save_watchlist_item(self, user_id, watchlist_id, symbol, market, note="", position=0):
        user_id = self._require_user(user_id)
        row = {
            "user_id": user_id,
            "watchlist_id": str(watchlist_id),
            "symbol": str(symbol).strip().upper(),
            "market": str(market).strip().upper(),
            "note": str(note or ""),
            "position": int(position),
        }
        try:
            self.client.table("user_watchlist_items").upsert(
                row, on_conflict="user_id,watchlist_id,symbol,market"
            ).execute()
        except Exception as exc:
            raise CloudStorageError(f"Could not save the watchlist stock: {exc}") from exc
        return row

    def delete_watchlist_items(self, user_id, watchlist_id, symbols):
        user_id = self._require_user(user_id)
        clean_symbols = [str(symbol).upper() for symbol in symbols if symbol]
        if not clean_symbols:
            return 0
        try:
            response = (
                self.client.table("user_watchlist_items").delete()
                .eq("user_id", user_id).eq("watchlist_id", str(watchlist_id))
                .in_("symbol", clean_symbols).execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not remove watchlist stocks: {exc}") from exc
        return len(response.data or [])

    def load_alerts(self, user_id):
        user_id = self._require_user(user_id)
        try:
            response = (
                self.client.table("user_alerts")
                .select("*")
                .eq("user_id", user_id)
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not load personal price alerts: {exc}") from exc
        return [self._without_user_id(row) for row in (response.data or [])]

    def create_alert(self, user_id, alert):
        user_id = self._require_user(user_id)
        alert_id = str(alert.get("id") or "")
        try:
            existing = (
                self.client.table("user_alerts")
                .select("*")
                .eq("user_id", user_id)
                .eq("id", alert_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                return self._without_user_id(existing.data[0]), False
            row = {"user_id": user_id, **alert}
            response = self.client.table("user_alerts").insert(row).execute()
        except Exception as exc:
            raise CloudStorageError(f"Could not create the personal price alert: {exc}") from exc
        stored = response.data[0] if response.data else row
        return self._without_user_id(stored), True

    def remove_alerts(self, user_id, alert_ids):
        user_id = self._require_user(user_id)
        clean_ids = [str(item) for item in alert_ids if item]
        if not clean_ids:
            return 0
        try:
            response = (
                self.client.table("user_alerts")
                .delete()
                .eq("user_id", user_id)
                .in_("id", clean_ids)
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not remove personal price alerts: {exc}") from exc
        return len(response.data or [])

    def acknowledge_alerts(self, user_id, alert_ids, acknowledged_at):
        user_id = self._require_user(user_id)
        clean_ids = [str(item) for item in alert_ids if item]
        if not clean_ids:
            return 0
        try:
            response = (
                self.client.table("user_alerts")
                .update({
                    "acknowledged": True,
                    "acknowledged_at": str(acknowledged_at),
                })
                .eq("user_id", user_id)
                .eq("status", "Triggered")
                .eq("acknowledged", False)
                .in_("id", clean_ids)
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not acknowledge personal price alerts: {exc}") from exc
        return len(response.data or [])

    def load_active_alerts(self, symbol, market):
        """Server-only query used by the central stock download worker."""
        try:
            response = (
                self.client.table("user_alerts")
                .select("*")
                .eq("status", "Active")
                .eq("symbol", str(symbol))
                .eq("market", str(market))
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not load active cloud alerts: {exc}") from exc
        return [dict(row) for row in (response.data or [])]

    def update_alerts(self, alerts):
        """Persist alert state changes while retaining each row's user_id."""
        rows = [dict(alert) for alert in alerts if alert.get("user_id") and alert.get("id")]
        if not rows:
            return
        try:
            (
                self.client.table("user_alerts")
                .upsert(rows, on_conflict="user_id,id")
                .execute()
            )
        except Exception as exc:
            raise CloudStorageError(f"Could not update triggered cloud alerts: {exc}") from exc

    @staticmethod
    def _without_user_id(row):
        clean = dict(row)
        clean.pop("user_id", None)
        return clean
