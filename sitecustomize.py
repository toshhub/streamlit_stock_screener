"""Process-wide startup optimizations for the Streamlit application.

Python imports ``sitecustomize`` automatically during interpreter startup when
this repository is on ``sys.path``.  Keep the R2 symbol membership cache here
so every Streamlit rerun avoids rebuilding and re-reading the same symbol list
for each stock in the screener universe.
"""

from pathlib import Path
import threading

import stock_data


_ORIGINAL_STOCK_EXISTS = stock_data.stock_exists
_R2_SYMBOL_CACHE = {}
_R2_SYMBOL_CACHE_LOCK = threading.RLock()


def _cached_stock_exists(path):
    path = Path(path)

    # Preserve the fast local and migration compatibility checks.
    if path.is_file() and path.suffix.lower() == ".json":
        return True
    if path.suffix.lower() != ".json" and path.with_suffix(".json").is_file():
        return True
    if path.is_dir() and any(path.glob("*.parquet")):
        return True

    store, market = stock_data._r2_store_for_path(path)
    if store is None:
        return False

    cache_key = (id(store), market)
    with _R2_SYMBOL_CACHE_LOCK:
        symbols = _R2_SYMBOL_CACHE.get(cache_key)
        if symbols is None:
            try:
                symbols = frozenset(store.list_symbols(market))
            except Exception:
                return False
            _R2_SYMBOL_CACHE[cache_key] = symbols

    return path.stem.upper() in symbols


stock_data.stock_exists = _cached_stock_exists
