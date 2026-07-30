from pathlib import Path


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STOCK_CACHE_DIR = DATA_DIR / ".stock-cache"

EXCEL_DIR = DATA_DIR / "excel"
INDIA_DATA_DIR = DATA_DIR / "india"
DAILY_DIR = INDIA_DATA_DIR / "daily"
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
    STOCK_CACHE_DIR,
]:
    d.mkdir(parents=True, exist_ok=True)
