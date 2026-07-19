from pathlib import Path

from streamlit_filter_proxy import st


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

EXCEL_DIR = DATA_DIR / "excel"
DAILY_DIR = DATA_DIR / "daily"
US_DATA_DIR = DATA_DIR / "us"
US_DAILY_DIR = US_DATA_DIR / "daily"
CHARTS_DIR = DATA_DIR / "charts"
META_DIR = DATA_DIR / "metadata"

for d in [
    EXCEL_DIR,
    DAILY_DIR,
    US_DAILY_DIR,
    CHARTS_DIR,
    META_DIR,
]:
    d.mkdir(parents=True, exist_ok=True)
