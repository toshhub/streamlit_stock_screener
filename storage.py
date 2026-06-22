
import json
from config import META_DIR

SETTINGS_FILE = META_DIR / "session_settings.json"
LEGACY_SETTINGS_FILE = META_DIR / "app_settings.json"
FAVOURITE_FILTERS_FILE = META_DIR / "favourite_filters.json"
PE_RATIOS_FILE = META_DIR / "pe_ratios.json"
RESULTS_FILE = META_DIR / "last_results.json"

def load_settings():
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    if LEGACY_SETTINGS_FILE.exists():
        settings = json.loads(LEGACY_SETTINGS_FILE.read_text())
        save_settings(settings)
        return settings
    return {}

def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))

def update_settings(data):
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

def save_results(rows):
    """Persist screener results to disk so they survive app restarts."""
    RESULTS_FILE.write_text(json.dumps(rows, indent=2, default=str))

def load_results():
    """Load persisted screener results. Returns empty list if not found."""
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return []
