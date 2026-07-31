"""Headless Cloudflare R2 and price-alert update used by GitHub Actions."""

from cloud_storage import cloud_storage_from_environment
from price_alerts import configure_cloud_alerts
from r2_update import run_update


def main():
    alert_backend = cloud_storage_from_environment()
    if alert_backend is None:
        raise RuntimeError(
            "Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY so the "
            "stock-data cron can evaluate price alerts."
        )
    configure_cloud_alerts(alert_backend, require_auth=True)
    result = run_update()
    for summary in result["markets"]:
        print(
            "{Market}: {Rows} rows across {Symbols} symbols in {Month}; "
            "{failed} Yahoo failures; {alerts} alerts triggered".format(
                failed=len(summary["Failures"]),
                alerts=int(summary.get("Alerts Triggered", 0)),
                **summary,
            )
        )
    for rollover in result["rollovers"]:
        print("Finalized {Market} {Year}: {Rows} rows".format(**rollover))
    return result


if __name__ == "__main__":
    main()
