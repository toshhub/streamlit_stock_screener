"""Small dependency-free path helpers shared by the UI and cron entrypoint."""

from config import EXCEL_DIR
from downloader import MARKET_US, normalize_market


def symbols_file_for_market(market):
    if normalize_market(market) == MARKET_US:
        return EXCEL_DIR / "nasdaq_screener_1784114565446.csv"
    return EXCEL_DIR / "MCAP_JUGAAD.xlsx"
