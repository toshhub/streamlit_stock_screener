
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

EXCEL_DIR = DATA_DIR / "excel"
DAILY_DIR = DATA_DIR / "daily"
WEEKLY_DIR = DATA_DIR / "weekly"
MONTHLY_DIR = DATA_DIR / "monthly"
CHARTS_DIR = DATA_DIR / "charts"
META_DIR = DATA_DIR / "metadata"

for d in [EXCEL_DIR, DAILY_DIR, WEEKLY_DIR, MONTHLY_DIR, CHARTS_DIR, META_DIR]:
    d.mkdir(parents=True, exist_ok=True)
