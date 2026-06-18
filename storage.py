
import json
from config import META_DIR

SETTINGS_FILE = META_DIR / "app_settings.json"

def load_settings():
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {}

def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))

def update_settings(data):
    settings = load_settings()
    settings.update(data)
    save_settings(settings)
    return settings
