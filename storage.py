
import json
import hashlib
import threading
from contextvars import ContextVar
from config import META_DIR

SETTINGS_FILE = META_DIR / "session_settings.json"
LEGACY_SETTINGS_FILE = META_DIR / "app_settings.json"
FAVOURITE_FILTERS_FILE = META_DIR / "favourite_filters.json"
PE_RATIOS_FILE = META_DIR / "pe_ratios.json"
FUNDAMENTALS_FILE = META_DIR / "screener_fundamentals.json"
RESULTS_FILE = META_DIR / "last_results.json"
_SETTINGS_LOCK = threading.RLock()
_USER_STORAGE_BACKEND = None
_CURRENT_USER_ID = ContextVar("settings_user_id", default=None)


def configure_user_storage(backend, user_id=None):
    """Configure optional per-user settings without changing shared JSON storage."""
    global _USER_STORAGE_BACKEND
    _USER_STORAGE_BACKEND = backend
    _CURRENT_USER_ID.set(str(user_id).strip() if user_id else None)


def _load_shared_settings():
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    if LEGACY_SETTINGS_FILE.exists():
        settings = json.loads(LEGACY_SETTINGS_FILE.read_text())
        save_settings(settings)
        return settings
    return {}

def load_settings():
    with _SETTINGS_LOCK:
        settings = _load_shared_settings()
        user_id = _CURRENT_USER_ID.get()
        if _USER_STORAGE_BACKEND is not None and user_id:
            settings.update(_USER_STORAGE_BACKEND.load_settings(user_id))
        return settings

def save_settings(data):
    with _SETTINGS_LOCK:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))

def update_settings(data):
    with _SETTINGS_LOCK:
        user_id = _CURRENT_USER_ID.get()
        if _USER_STORAGE_BACKEND is not None and user_id:
            return _USER_STORAGE_BACKEND.update_settings(user_id, data)
        settings = load_settings()
        settings.update(data)
        save_settings(settings)
        return settings

def load_favourite_filter_sets():
    if FAVOURITE_FILTERS_FILE.exists():
        return json.loads(FAVOURITE_FILTERS_FILE.read_text())
    return {}

def save_favourite_filter_sets(data):
    FAVOURITE_FILTERS_FILE.write_text(json.dumps(data, indent=2))

def load_pe_ratios():
    if PE_RATIOS_FILE.exists():
        return json.loads(PE_RATIOS_FILE.read_text())
    return {}

def save_pe_ratios(data):
    PE_RATIOS_FILE.write_text(json.dumps(data, indent=2))

def load_fundamentals():
    if FUNDAMENTALS_FILE.exists():
        return json.loads(FUNDAMENTALS_FILE.read_text())
    return {}

def save_fundamentals(data):
    FUNDAMENTALS_FILE.write_text(json.dumps(data, indent=2))

def save_results(rows, metadata=None):
    """Persist results only inside the current authenticated user's namespace."""
    user_id = _CURRENT_USER_ID.get()
    if _USER_STORAGE_BACKEND is not None:
        if not user_id:
            return []
        _USER_STORAGE_BACKEND.save_results(user_id, rows, metadata or {})
        return rows
    if not user_id:
        return rows
    # Local development fallback remains isolated by a non-identifying hash.
    local_file = _local_results_file(user_id)
    local_file.write_text(json.dumps({"rows": rows, "metadata": metadata or {}}, indent=2, default=str))
    return rows

def load_results(include_metadata=False):
    """Load only the current user's persisted screener results."""
    user_id = _CURRENT_USER_ID.get()
    if _USER_STORAGE_BACKEND is not None:
        if not user_id:
            return ([], {}) if include_metadata else []
        payload = _USER_STORAGE_BACKEND.load_results(user_id)
        rows = payload.get("rows", [])
        metadata = payload.get("metadata", {})
        return (rows, metadata) if include_metadata else rows
    if not user_id:
        return ([], {}) if include_metadata else []
    local_file = _local_results_file(user_id)
    if local_file.exists():
        payload = json.loads(local_file.read_text())
        if isinstance(payload, list):  # legacy local format
            return (payload, {}) if include_metadata else payload
        rows = payload.get("rows", [])
        metadata = payload.get("metadata", {})
        return (rows, metadata) if include_metadata else rows
    return ([], {}) if include_metadata else []


def _local_results_file(user_id):
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:24]
    return META_DIR / f"last_results_{digest}.json"
