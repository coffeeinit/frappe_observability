"""Permission-checked APIs backing the single in-Desk observability dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .api import _GAUGES


_ALLOWED_WINDOWS = (15, 60, 360, 1440, 10080)
_SERIES_METRICS = (
    "requests", "request_errors", "request_duration_ms", "request_bytes_in", "request_bytes_out",
    "request_status_1xx", "request_status_2xx", "request_status_3xx", "request_status_4xx",
    "request_status_5xx", "request_status_other", "request_duration_le_5_ms", "request_duration_le_10_ms",
    "request_duration_le_25_ms", "request_duration_le_50_ms", "request_duration_le_100_ms",
    "request_duration_le_250_ms", "request_duration_le_500_ms", "request_duration_le_1000_ms",
    "request_duration_le_2500_ms", "request_duration_le_5000_ms", "request_duration_gt_5000_ms",
    "jobs", "job_failures", "job_duration_ms", "jobs_by_queue", "jobs_by_method",
    "job_failures_by_method", "job_duration_ms_by_method", "collector_errors",
    "host_cpu_percent", "host_memory_percent", "host_memory_used_bytes", "host_memory_available_bytes",
    "host_swap_percent", "host_swap_used_bytes", "host_root_disk_percent", "host_root_disk_used_bytes",
    "host_root_disk_free_bytes", "host_disk_percent", "host_disk_used_bytes", "host_disk_free_bytes",
    "host_load_1m", "host_load_5m", "host_load_15m", "host_disk_read_bytes_total", "host_disk_write_bytes_total",
    "host_network_in_bytes_total", "host_network_out_bytes_total", "database_connections",
    "database_connections_percent", "database_max_connections", "database_threads_running",
    "database_long_queries", "database_slow_queries_total", "database_buffer_pool_hit_percent",
    "database_size_bytes", "database_table_size_bytes", "redis_memory_used_bytes", "redis_memory_max_bytes",
    "redis_memory_percent", "redis_clients_connected", "redis_cache_hit_percent", "redis_evicted_keys_total",
    "redis_queue_memory_used_bytes", "redis_queue_memory_max_bytes", "redis_queue_memory_percent",
    "redis_queue_clients_connected", "redis_queue_cache_hit_percent", "redis_queue_evicted_keys_total",
    "queue_depth_total", "queue_failed_total", "queue_short_depth", "queue_default_depth", "queue_long_depth",
    "queue_short_failed", "queue_default_failed", "queue_long_failed", "application_error_logs_per_minute",
    "email_queue_pending", "email_queue_errors", "request_error_rate_percent", "backup_age_hours",
    "backup_latest_size_bytes", "backup_count", "storage_public_bytes", "storage_public_files",
    "storage_private_bytes", "storage_private_files", "log_directory_bytes", "log_directory_files",
    "flush_age_seconds",
)
_GAUGE_METRICS = frozenset(field for field, _help in _GAUGES.values()) | {"database_table_size_bytes"}


def _require_view_access() -> tuple[str, set[str]]:
    import frappe

    user = str(getattr(frappe.session, "user", "Guest") or "Guest")
    if user == "Guest":
        frappe.throw("Sign in to view observability data.", frappe.PermissionError)
    roles = set(frappe.get_roles(user) or [])
    if user != "Administrator" and not roles.intersection({"System Manager", "Observability Admin", "Observability Viewer"}):
        frappe.throw("You do not have permission to view observability data.", frappe.PermissionError)
    return user, roles


def _clamp_window(value: Any) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return 60
    return min(_ALLOWED_WINDOWS, key=lambda candidate: abs(candidate - requested))


def _is_manager(user: str, roles: set[str]) -> bool:
    return user == "Administrator" or bool(roles.intersection({"System Manager", "Observability Admin"}))


def _decode_hash(raw: dict) -> dict[str, str]:
    return {
        (key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)):
        (value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value))
        for key, value in (raw or {}).items()
    }


def _number(value: Any) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed and abs(parsed) != float("inf") else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _age_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _live_data(frappe) -> tuple[dict[str, str], dict[str, str], str | None, str | None]:
    from .collectors import _prefix, _redis

    redis = _redis(frappe)
    prefix = _prefix(frappe)
    sample = _decode_hash(redis.hgetall(f"{prefix}:current_sample"))
    now = int(datetime.now(timezone.utc).timestamp())
    current_minute = now // 60
    counters = _decode_hash(redis.hgetall(f"{prefix}:minute:{current_minute}"))
    last_flush = redis.get(f"{prefix}:last_flush")
    last_sample = redis.get(f"{prefix}:last_sample")
    if isinstance(last_flush, bytes):
        last_flush = last_flush.decode("utf-8", "replace")
    if isinstance(last_sample, bytes):
        last_sample = last_sample.decode("utf-8", "replace")
    return sample, counters, str(last_sample) if last_sample else None, str(last_flush) if last_flush else None


def _active_count(frappe, window_minutes: int, dimension: str) -> int | None:
    from .collectors import _prefix, _redis

    try:
        redis = _redis(frappe)
        prefix = _prefix(frappe)
        now = int(datetime.now(timezone.utc).timestamp())
        keys = []
        if window_minutes <= 60:
            current = now // 60
            first = max(0, current - window_minutes + 1)
            keys = [f"{prefix}:active:{dimension}:m:{minute}" for minute in range(first, current + 1)]
        else:
            current = now // 3600
            hours = min(168, max(1, (window_minutes + 59) // 60))
            first = max(0, current - hours + 1)
            keys = [f"{prefix}:active:{dimension}:h:{hour}" for hour in range(first, current + 1)]
        if not keys:
            return 0
        return int(redis.pfcount(*keys) or 0)
    except Exception:
        return None


def _query_history(frappe, since: datetime, window_minutes: int) -> list[dict[str, Any]]:
    """Downsample fixed metric names with a parameterized MariaDB read.

    The endpoint performs an explicit observability-role check first. The table
    is site-local (Frappe database isolation), contains no cross-site rows, and
    has no row-level tenancy field; aggregation avoids loading unbounded sample
    rows into Python while preserving one-minute/5-minute/hourly chart points.
    """
    if window_minutes <= 360:
        step = 60
    elif window_minutes <= 1440:
        step = 300
    else:
        step = 3600
    metric_placeholders = ",".join(["%s"] * len(_SERIES_METRICS))
    bucket = f"FLOOR(UNIX_TIMESTAMP(`ts`) / {step}) * {step}"
    query = (
        "SELECT FROM_UNIXTIME(" + bucket + ") AS bucket, `metric`, `label`, SUM(`sum`) AS value, SUM(`count`) AS sample_count, MAX(`max`) AS max_value "
        "FROM `tabMetric Bucket` WHERE `ts` >= %s AND `metric` IN (" + metric_placeholders + ") "
        "GROUP BY " + bucket + ", `metric`, `label` ORDER BY bucket ASC LIMIT 25000"
    )
    rows = frappe.db.sql(query, [since, *_SERIES_METRICS], as_dict=True) or []
    output = []
    for row in rows:
        item = dict(row)
        ts = item.get("bucket")
        item["ts"] = ts.isoformat(sep=" ") if hasattr(ts, "isoformat") else str(ts)
        item["metric"] = str(item.get("metric") or "")
        item["label"] = str(item.get("label") or "")
        item["value"] = _number(item.get("value"))
        item["sample_count"] = _number(item.get("sample_count"))
        output.append(item)
    return output


def _time_series(rows: list[dict[str, Any]], window_minutes: int, counters: dict[str, str]) -> dict[str, Any]:
    buckets: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        ts, metric, label = row["ts"], row["metric"], row["label"]
        value = _number(row["value"])
        sample_count = _number(row.get("sample_count"))
        if metric in _GAUGE_METRICS:
            if metric.endswith("_total") and row.get("max_value") is not None:
                value = _number(row["max_value"])
            elif sample_count > 0:
                value /= sample_count
        buckets.setdefault(ts, {}).setdefault(metric, {})[label] = value
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    current_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    if counters:
        current = buckets.setdefault(current_ts, {})
        for metric, field in (("requests", "requests"), ("request_errors", "errors"), ("request_duration_ms", "duration_ms_sum"),
                              ("request_bytes_in", "bytes_in"), ("request_bytes_out", "bytes_out")):
            if field in counters:
                current.setdefault(metric, {})["all"] = _number(counters[field])
        for bound in (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000):
            key = f"hist_le_{bound}"
            if key in counters:
                current.setdefault(f"request_duration_le_{bound}_ms", {})["all"] = _number(counters[key])
        if "hist_gt_5000" in counters:
            current.setdefault("request_duration_gt_5000_ms", {})["all"] = _number(counters["hist_gt_5000"])
        for status in ("1xx", "2xx", "3xx", "4xx", "5xx", "other"):
            key = f"status_{status}"
            if key in counters:
                current.setdefault(f"request_status_{status}", {})["all"] = _number(counters[key])
    timestamps = sorted(buckets)
    if window_minutes > 360:
        timestamps = timestamps[-min(1008, max(168, window_minutes // 5)):]

    def all_value(ts: str, metric: str) -> float:
        values = buckets.get(ts, {}).get(metric, {})
        if "all" in values:
            return values["all"]
        return sum(values.values())

    def series(metric: str, label: str = "all") -> list[dict[str, Any]]:
        result = []
        for ts in timestamps:
            entries = buckets.get(ts, {}).get(metric, {})
            if label == "all":
                value = entries.get("all", sum(entries.values()))
            else:
                value = entries.get(label, 0.0)
            result.append({"ts": ts, "value": _number(value)})
        return result

    # p95 is calculated from mutually exclusive histogram buckets, not average latency.
    p95 = []
    bounds = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
    for ts in timestamps:
        counts = [all_value(ts, f"request_duration_le_{bound}_ms") for bound in bounds]
        overflow = all_value(ts, "request_duration_gt_5000_ms")
        total = sum(counts) + overflow
        target = total * 0.95
        cumulative = 0.0
        percentile = 0.0
        for bound, count in zip(bounds, counts):
            cumulative += count
            if cumulative >= target:
                percentile = float(bound)
                break
        if total and cumulative < target:
            percentile = 5000.0
        p95.append({"ts": ts, "value": percentile})

    endpoint_requests = []
    latest = timestamps[-1] if timestamps else None
    if latest:
        for label, value in buckets.get(latest, {}).get("requests", {}).items():
            if label != "all":
                endpoint_requests.append({"label": label, "requests": value})
    endpoint_requests.sort(key=lambda item: item["requests"], reverse=True)

    def named_series(metric_prefix: str, metric_exact: str | None = None) -> list[dict[str, Any]]:
        result = []
        for ts in timestamps:
            metric_map = buckets.get(ts, {})
            if metric_exact:
                value = all_value(ts, metric_exact)
                result.append({"ts": ts, "value": value})
            else:
                matches = [value for name, labels in metric_map.items() if name.startswith(metric_prefix)
                           for label, value in labels.items() if not label]
                result.append({"ts": ts, "value": matches[-1] if matches else 0.0})
        return result

    requests = series("requests")
    errors = series("request_errors")
    error_rate = []
    for index, point in enumerate(requests):
        req = _number(point["value"])
        err = _number(errors[index]["value"]) if index < len(errors) else 0.0
        error_rate.append({"ts": point["ts"], "value": 100.0 * err / req if req else 0.0})

    return {
        "labels": timestamps,
        "requests": requests,
        "errors": errors,
        "error_rate_percent": error_rate,
        "p95_ms": p95,
        "bytes_in": series("request_bytes_in"),
        "bytes_out": series("request_bytes_out"),
        "cpu_percent": series("host_cpu_percent"),
        "memory_percent": series("host_memory_percent"),
        "root_disk_percent": series("host_root_disk_percent"),
        "disk_read_bytes_total": series("host_disk_read_bytes_total"),
        "disk_write_bytes_total": series("host_disk_write_bytes_total"),
        "network_in_bytes_total": series("host_network_in_bytes_total"),
        "network_out_bytes_total": series("host_network_out_bytes_total"),
        "db_connections_percent": series("database_connections_percent"),
        "redis_memory_percent": series("redis_memory_percent"),
        "redis_queue_memory_percent": series("redis_queue_memory_percent"),
        "queue_depth": series("queue_depth_total"),
        "jobs": series("jobs"),
        "job_failures": series("job_failures"),
        "database_size_bytes": series("database_size_bytes"),
        "public_storage_bytes": series("storage_public_bytes"),
        "private_storage_bytes": series("storage_private_bytes"),
        "log_directory_bytes": series("log_directory_bytes"),
        "endpoint_requests": endpoint_requests[:10],
        "http_status": {status: series(f"request_status_{status}") for status in ("1xx", "2xx", "3xx", "4xx", "5xx", "other")},
    }


def _top_usage(frappe, since: datetime) -> dict[str, list[dict[str, Any]]]:
    output = {"user": [], "ip": [], "endpoint": []}
    try:
        rows = frappe.get_list(
            "Usage Top",
            filters={"hour": (">=", since), "dimension": ["in", ["user", "ip", "endpoint"]]},
            fields=["dimension", "key", {"SUM": "requests", "as": "requests"},
                    {"SUM": "bytes", "as": "bytes"}, {"SUM": "errors", "as": "errors"}],
            group_by="dimension, key",
            order_by="requests desc",
            limit_page_length=150,
        )
        for row in rows:
            dimension = str(row.get("dimension") or "")
            if dimension in output and len(output[dimension]) < 50:
                output[dimension].append({"key": str(row.get("key") or ""),
                                          "requests": int(_number(row.get("requests"))),
                                          "bytes": int(_number(row.get("bytes"))),
                                          "errors": int(_number(row.get("errors")))})
    except Exception:
        pass
    return output


def _open_alert_count(frappe) -> int:
    try:
        return int(frappe.db.count("Alert Event", {"status": ["in", ["Open", "Acknowledged"]]}) or 0)
    except Exception:
        return 0


def get_dashboard_data(window_minutes: int | str = 60) -> dict[str, Any]:
    """Return a bounded overview payload for Frappe's in-app chart dashboard."""
    import frappe

    user, roles = _require_view_access()
    window = _clamp_window(window_minutes)
    manager = _is_manager(user, roles)
    try:
        settings = frappe.get_cached_doc("Observability Settings")
        mode = str(settings.get("mode") or "internal")
        schedule_interval = min(3600, max(60, int(settings.get("flush_interval_seconds") or 60)))
    except Exception:
        mode = "internal"
        schedule_interval = 60
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=window)
    try:
        rows = _query_history(frappe, since, window)
    except Exception:
        rows = []
    try:
        sample, counters, last_sample, last_flush = _live_data(frappe)
    except Exception:
        sample, counters, last_sample, last_flush = {}, {}, None, None
    series = _time_series(rows, window, counters)
    active_users = _active_count(frappe, window, "user")
    active_ips = _active_count(frappe, window, "ip")

    # Recent rate and request totals are based on the selected persisted window;
    # the current Redis minute is appended once for an up-to-date Desk view.
    requests = sum(_number(point["value"]) for point in series["requests"])
    errors = sum(_number(point["value"]) for point in series["errors"])
    bytes_in = sum(_number(point["value"]) for point in series["bytes_in"])
    bytes_out = sum(_number(point["value"]) for point in series["bytes_out"])
    duration_rows = [row for row in rows if row["metric"] == "request_duration_ms" and row["label"] == "all"]
    duration_sum = sum(_number(row["value"]) for row in duration_rows)
    duration_count = sum(_number(row.get("sample_count", 0)) for row in duration_rows) + _number(counters.get("requests"))
    duration_sum += _number(counters.get("duration_ms_sum"))
    average_latency = duration_sum / duration_count if duration_count else 0.0
    live = {key: _number(value) for key, value in sample.items() if key != "sampled_at"}
    sample_age = _age_seconds(last_sample)
    flush_age = _age_seconds(last_flush) if mode != "external" else None
    if sample_age is not None:
        live["scheduler_lag_seconds"] = sample_age
    if flush_age is not None:
        live["flush_age_seconds"] = flush_age
    try:
        live["collector_errors_total"] = _number(_decode_hash(_redis_hash(frappe)).get("collector_errors"))
    except Exception:
        live["collector_errors_total"] = 0.0
    live["current_queue_depth"] = _number(live.get("queue_depth_total"))
    live["current_error_rate_percent"] = _number(live.get("request_error_rate_percent"))

    # Site viewers cannot see host/DB/cache identifiers or top user/IP names.
    if not manager:
        for key in list(live):
            if key.startswith(("host_", "database_", "redis_", "queue_", "backup_", "storage_", "log_directory_", "flush_")):
                live.pop(key, None)
        for key in ("cpu_percent", "memory_percent", "root_disk_percent", "db_connections_percent", "redis_memory_percent", "queue_depth",
                    "database_size_bytes", "public_storage_bytes", "private_storage_bytes", "log_directory_bytes"):
            series.pop(key, None)

    top_usage = _top_usage(frappe, since) if manager else None
    return {
        "window_minutes": window,
        "mode": mode,
        "history_available": mode != "external" and bool(rows),
        "can_manage": manager,
        "can_view_host": manager,
        "summary": {
            "requests": requests,
            "request_errors": errors,
            "error_rate_percent": 100.0 * errors / requests if requests else 0.0,
            "average_latency_ms": average_latency,
            "active_users": active_users,
            "active_ips": active_ips,
            "bytes_in": bytes_in,
            "bytes_out": bytes_out,
            "open_alerts": _open_alert_count(frappe),
            "collector_errors": int(live.get("collector_errors_total", 0)),
            "last_sample": last_sample,
            "last_sample_age_seconds": sample_age,
            "scheduler_stale_after_seconds": max(180, schedule_interval * 2),
            "last_flush": last_flush,
            "last_flush_age_seconds": flush_age,
        },
        "live": live,
        "series": series,
        "top_usage": top_usage,
    }


def _redis_hash(frappe) -> dict:
    from .collectors import _prefix, _redis

    return _redis(frappe).hgetall(f"{_prefix(frappe)}:cumulative") or {}


def get_summary(window_minutes=60):
    """Compatibility alias retained for existing callers."""
    return get_dashboard_data(window_minutes).get("summary", {})


def get_recent_alerts(limit=10):
    """Return recent sanitized alert fields for the current site."""
    import frappe

    _require_view_access()
    try:
        limit = min(100, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = 10
    rows = frappe.get_list(
        "Alert Event", fields=["name", "fired_at", "severity", "status", "message"],
        order_by="fired_at desc", limit_page_length=limit,
    )
    return [{"name": row.name, "fired_at": str(row.fired_at), "severity": row.severity,
             "status": row.status, "message": row.message} for row in rows]


def acknowledge_alert(name):
    """Allow observability admins to acknowledge a currently open event."""
    import frappe

    user, roles = _require_view_access()
    if not _is_manager(user, roles):
        frappe.throw("Only observability administrators can acknowledge alerts.", frappe.PermissionError)
    doc = frappe.get_doc("Alert Event", name)
    if doc.status == "Resolved":
        frappe.throw("Resolved alerts cannot be acknowledged.")
    doc.status = "Acknowledged"
    doc.acknowledged = 1
    doc.save(ignore_permissions=True)
    return {"name": doc.name, "status": doc.status}


try:
    import frappe as _frappe

    get_dashboard_data = _frappe.whitelist()(get_dashboard_data)
    get_summary = _frappe.whitelist()(get_summary)
    get_recent_alerts = _frappe.whitelist()(get_recent_alerts)
    acknowledge_alert = _frappe.whitelist()(acknowledge_alert)
except ImportError:
    pass
