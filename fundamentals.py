import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape

from downloader import MARKET_INDIA, normalize_market
from storage import load_fundamentals, save_fundamentals


GROWTH_SECTION_PERIODS = {
    "Compounded Sales Growth": ("10 Years", "5 Years", "3 Years", "TTM"),
    "Compounded Profit Growth": ("10 Years", "5 Years", "3 Years", "TTM"),
    "Stock Price CAGR": ("10 Years", "5 Years", "3 Years", "1 Year"),
    "Return on Equity": ("10 Years", "5 Years", "3 Years", "Last Year"),
}

GROWTH_SUMMARY_COLUMNS = (
    "Sales CAGR 3Y",
    "Profit CAGR 3Y",
    "Price CAGR 3Y",
    "ROE 3Y",
)

VALUATION_PERIOD_DAYS = {
    "3 Years": 1095,
    "5 Years": 1825,
    "10 Years": 3652,
}

_CACHE_LOCK = threading.RLock()
_FETCH_LIMIT = threading.BoundedSemaphore(2)
_SUCCESS_TTL = timedelta(days=7)
_EMPTY_TTL = timedelta(days=1)
_RETRIABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def _plain_text(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(unescape(text).replace("\xa0", " ").split())


def _percentage_value(value):
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", value)
    return float(match.group(1)) if match else None


def parse_screener_growth_html(page_html):
    metrics = {}
    range_tables = re.findall(
        r"<table[^>]*class=[\"'][^\"']*ranges-table[^\"']*[\"'][^>]*>(.*?)</table>",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for section_title, allowed_periods in GROWTH_SECTION_PERIODS.items():
        section_html = ""
        for table_html in range_tables:
            heading_match = re.search(
                r"<th[^>]*>(.*?)</th>",
                table_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if heading_match and _plain_text(heading_match.group(1)).lower() == section_title.lower():
                section_html = table_html
                break
        if not section_html:
            section_match = re.search(
                rf"<h3[^>]*>\s*{re.escape(section_title)}\s*</h3>(.*?</table>)",
                page_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if section_match:
                section_html = section_match.group(1)
        if not section_html:
            continue

        values = {}
        for row_html in re.findall(
            r"<tr[^>]*>(.*?)</tr>",
            section_html,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            cells = re.findall(
                r"<td[^>]*>(.*?)</td>",
                row_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if len(cells) < 2:
                continue
            period = _plain_text(cells[0]).rstrip(":").strip()
            if period not in allowed_periods:
                continue
            values[period] = _percentage_value(_plain_text(cells[1]))

        if values:
            metrics[section_title] = {
                period: values.get(period)
                for period in allowed_periods
            }
    return metrics


def _cache_key(symbol, market):
    return f"{normalize_market(market)}:{str(symbol).upper()}"


def _cached_field(
    symbol,
    market,
    field,
    fetched_at_field,
    allow_stale=False,
):
    with _CACHE_LOCK:
        entry = load_fundamentals().get(_cache_key(symbol, market))
    if not isinstance(entry, dict):
        return None

    value = entry.get(field)
    if not isinstance(value, dict):
        return None
    if allow_stale:
        return value

    try:
        fetched_at = datetime.fromisoformat(str(entry.get(fetched_at_field, "")))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

    ttl = _SUCCESS_TTL if value else _EMPTY_TTL
    return value if datetime.now(timezone.utc) - fetched_at <= ttl else None


def get_cached_company_growth_metrics(symbol, market=MARKET_INDIA):
    return _cached_field(symbol, market, "metrics", "fetched_at", allow_stale=True) or {}


def get_cached_company_valuation_medians(symbol, market=MARKET_INDIA):
    return (
        _cached_field(
            symbol,
            market,
            "valuation_medians",
            "valuation_fetched_at",
            allow_stale=True,
        )
        or {}
    )


def _read_url_with_retries(request, timeout=15, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in _RETRIABLE_HTTP_CODES or attempt + 1 >= attempts:
                raise
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise
        time.sleep(0.45 * (attempt + 1))
    if last_error:
        raise last_error
    return b""


def _fetch_screener_page(symbol):
    encoded_symbol = urllib.parse.quote(str(symbol).upper(), safe="")
    urls = (
        f"https://www.screener.in/company/{encoded_symbol}/consolidated/",
        f"https://www.screener.in/company/{encoded_symbol}/",
    )
    last_error = None
    for url in urls:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NSEStockScreener/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            return _read_url_with_retries(request).decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {404, 410}:
                break
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            break
    if last_error:
        raise last_error
    return ""


def parse_screener_company_chart_context(page_html):
    info_match = re.search(
        r"<div[^>]*\bid=[\"']company-info[\"'][^>]*>|"
        r"<div[^>]*\bdata-company-id=[\"'][^\"']+[\"'][^>]*\bid=[\"']company-info[\"'][^>]*>",
        page_html,
        flags=re.IGNORECASE,
    )
    if not info_match:
        return None
    tag = info_match.group(0)
    company_match = re.search(r"data-company-id=[\"'](\d+)[\"']", tag, flags=re.IGNORECASE)
    if not company_match:
        return None
    return {
        "company_id": company_match.group(1),
        "consolidated": bool(
            re.search(r"data-consolidated=[\"']true[\"']", tag, flags=re.IGNORECASE)
        ),
    }


def parse_screener_valuation_chart_payload(payload):
    medians = {}
    datasets = payload.get("datasets", []) if isinstance(payload, dict) else []
    metric_map = {
        "Median PE": "Median PE",
        "Median Market Cap to Sales": "Median Market Cap to Sales",
    }
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        output_name = metric_map.get(dataset.get("metric"))
        if not output_name:
            continue
        numeric_value = None
        for point in dataset.get("values", []):
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                numeric_value = float(point[1])
                break
            except (TypeError, ValueError):
                continue
        if numeric_value is not None:
            medians[output_name] = numeric_value
    return medians


def _fetch_valuation_medians(page_html, symbol):
    context = parse_screener_company_chart_context(page_html)
    if not context:
        return {}

    values = {
        "Median PE": {},
        "Median Market Cap to Sales": {},
    }
    query = (
        "Price to Earning-Median PE-"
        "Market Cap to Sales-Median Market Cap to Sales"
    )
    for period, days in VALUATION_PERIOD_DAYS.items():
        params = {
            "q": query,
            "days": days,
        }
        if context["consolidated"]:
            params["consolidated"] = "true"
        url = (
            f"https://www.screener.in/api/company/{context['company_id']}/chart/?"
            f"{urllib.parse.urlencode(params)}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NSEStockScreener/1.0)",
                "Accept": "application/json",
                "Referer": (
                    f"https://www.screener.in/company/"
                    f"{urllib.parse.quote(str(symbol).upper(), safe='')}/"
                ),
            },
        )
        try:
            payload = json.loads(
                _read_url_with_retries(request).decode("utf-8", errors="ignore")
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"Screener.in valuation period unavailable for {symbol} ({period}): {exc}")
            continue
        period_values = parse_screener_valuation_chart_payload(payload)
        for metric_name, metric_value in period_values.items():
            values[metric_name][period] = metric_value

    return {
        metric_name: period_values
        for metric_name, period_values in values.items()
        if period_values
    }


def _merge_valuation_medians(existing, refreshed):
    merged = {}
    for source in (existing, refreshed):
        if not isinstance(source, dict):
            continue
        for metric_name, period_values in source.items():
            if not isinstance(period_values, dict):
                continue
            merged.setdefault(metric_name, {}).update(period_values)
    return merged


def _numeric_value_count(values):
    if not isinstance(values, dict):
        return 0
    count = 0
    for value in values.values():
        try:
            if value is not None and math.isfinite(float(value)):
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def has_complete_company_fundamentals(metrics, valuation_medians):
    growth_available = all(
        _numeric_value_count(metrics.get(section_name, {})) > 0
        for section_name in GROWTH_SECTION_PERIODS
    ) if isinstance(metrics, dict) else False
    if not isinstance(valuation_medians, dict):
        return False
    pe_periods = _numeric_value_count(valuation_medians.get("Median PE", {}))
    sales_periods = _numeric_value_count(
        valuation_medians.get("Median Market Cap to Sales", {})
    )
    return growth_available and pe_periods >= 3 and sales_periods >= 3


def get_company_fundamentals(symbol, market=MARKET_INDIA):
    market = normalize_market(market)
    if market != MARKET_INDIA:
        return {}, {}

    cached_growth = _cached_field(symbol, market, "metrics", "fetched_at")
    cached_valuations = _cached_field(
        symbol,
        market,
        "valuation_medians",
        "valuation_fetched_at",
    )
    if cached_growth is not None and cached_valuations is not None:
        return cached_growth, cached_valuations

    metrics = cached_growth if cached_growth is not None else {}
    valuation_medians = cached_valuations if cached_valuations is not None else {}
    try:
        with _FETCH_LIMIT:
            page_html = _fetch_screener_page(symbol)
            if cached_growth is None:
                metrics = parse_screener_growth_html(page_html)
            if cached_valuations is None:
                valuation_medians = _fetch_valuation_medians(page_html, symbol)
    except Exception as exc:
        print(f"Screener.in fundamentals unavailable for {symbol}: {exc}")

    with _CACHE_LOCK:
        cache = load_fundamentals()
        entry = cache.get(_cache_key(symbol, market), {})
        if not isinstance(entry, dict):
            entry = {}
        now = datetime.now(timezone.utc).isoformat()
        if cached_growth is None:
            entry["fetched_at"] = now
            entry["metrics"] = metrics
        if cached_valuations is None:
            entry["valuation_fetched_at"] = now
            entry["valuation_medians"] = valuation_medians
        cache[_cache_key(symbol, market)] = entry
        save_fundamentals(cache)
    return metrics, valuation_medians


def refresh_company_fundamentals(
    symbol,
    market=MARKET_INDIA,
    include_status=False,
):
    """Fetch Screener.in growth and valuation data without using the TTL cache."""
    market = normalize_market(market)
    if market != MARKET_INDIA:
        return ({}, {}, False) if include_status else ({}, {})

    with _CACHE_LOCK:
        existing_entry = load_fundamentals().get(_cache_key(symbol, market), {})
    if not isinstance(existing_entry, dict):
        existing_entry = {}
    existing_metrics = existing_entry.get("metrics", {})
    existing_valuations = existing_entry.get("valuation_medians", {})
    metrics = existing_metrics if isinstance(existing_metrics, dict) else {}
    valuation_medians = (
        existing_valuations if isinstance(existing_valuations, dict) else {}
    )

    fetched = False
    try:
        with _FETCH_LIMIT:
            page_html = _fetch_screener_page(symbol)
            refreshed_metrics = parse_screener_growth_html(page_html)
            refreshed_valuations = _fetch_valuation_medians(page_html, symbol)
        metrics = refreshed_metrics or metrics
        valuation_medians = _merge_valuation_medians(
            valuation_medians,
            refreshed_valuations,
        )
        fetched = True
    except Exception as exc:
        print(f"Screener.in fundamentals refresh unavailable for {symbol}: {exc}")

    if fetched:
        with _CACHE_LOCK:
            cache = load_fundamentals()
            entry = cache.get(_cache_key(symbol, market), {})
            if not isinstance(entry, dict):
                entry = {}
            now = datetime.now(timezone.utc).isoformat()
            entry.update(
                {
                    "fetched_at": now,
                    "metrics": metrics,
                    "valuation_fetched_at": now,
                    "valuation_medians": valuation_medians,
                }
            )
            cache[_cache_key(symbol, market)] = entry
            save_fundamentals(cache)
    if include_status:
        return metrics, valuation_medians, fetched
    return metrics, valuation_medians


def get_company_growth_metrics(symbol, market=MARKET_INDIA):
    metrics, _ = get_company_fundamentals(symbol, market)
    return metrics


def get_company_valuation_medians(symbol, market=MARKET_INDIA):
    _, valuation_medians = get_company_fundamentals(symbol, market)
    return valuation_medians


def growth_summary_fields(metrics):
    def value(section, period):
        section_values = metrics.get(section, {})
        return section_values.get(period) if isinstance(section_values, dict) else None

    return {
        "Sales CAGR 3Y": value("Compounded Sales Growth", "3 Years"),
        "Profit CAGR 3Y": value("Compounded Profit Growth", "3 Years"),
        "Price CAGR 3Y": value("Stock Price CAGR", "3 Years"),
        "ROE 3Y": value("Return on Equity", "3 Years"),
    }


def enrich_result_with_growth_metrics(result, symbol, market=MARKET_INDIA):
    metrics, valuation_medians = get_company_fundamentals(symbol, market)
    result["GrowthMetrics"] = metrics
    result["ValuationMedians"] = valuation_medians
    result.update(growth_summary_fields(metrics))
    return result


def refresh_result_with_growth_metrics(result, symbol, market=MARKET_INDIA):
    metrics, valuation_medians, refreshed = refresh_company_fundamentals(
        symbol,
        market,
        include_status=True,
    )
    result["GrowthMetrics"] = metrics
    result["ValuationMedians"] = valuation_medians
    result.update(growth_summary_fields(metrics))
    return refreshed
