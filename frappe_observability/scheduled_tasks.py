"""Minute/hourly sampling, bounded Redis-to-DB flushing, and retention pruning.

Request and job hooks only write compact Redis counters. This module performs
optional system/DB probes out of band and persists completed buckets in
idempotent bulk inserts. Probe errors are counted instead of escaping workers.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .collectors import (
    APP_PREFIX,
    BUCKET_TTL_SECONDS,
    HISTOGRAM_BOUNDS_MS,
    TOP_N,
    _frappe,
    _redis,
    _site_key,
    _site_name,
    get_settings,
)

MAX_FLUSH_KEYS = 20_000
DB_INSERT_CHUNK_SIZE = 500
PRUNE_CHUNK_SIZE = 1000
_MAX_WALK_FILES = 200_000


def _prefix(frappe) -> str:
    return f"{APP_PREFIX}:{_site_key(frappe)}"


def _host_name() -> str:
    try:
        return socket.gethostname()[:140] or "unknown"
    except Exception:
        return "unknown"


def _minute_epoch() -> int:
    return int(time.time()) // 60


def _safe_number(value: Any) -> float:
    try:
        number = float(value)
        if number == number and abs(number) != float("inf"):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return 0.0


def _decode(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _hash_values(redis, key: str) -> dict[str, str]:
    try:
        raw = redis.hgetall(key) or {}
        return {_decode(k): _decode(v) for k, v in raw.items()}
    except Exception:
        return {}


def _collector_error(frappe, collector: str) -> None:
    """Record only a bounded error counter; never log request/user data."""
    try:
        redis = _redis(frappe)
        prefix = _prefix(frappe)
        minute = _minute_epoch()
        pipe = redis.pipeline(transaction=False)
        pipe.hincrby(f"{prefix}:cumulative", "collector_errors", 1)
        pipe.hincrby(f"{prefix}:minute:{minute}", "collector_errors", 1)
        pipe.hincrby(f"{prefix}:sample:{minute}", f"collector_errors|{collector}", 1)
        pipe.expire(f"{prefix}:cumulative", BUCKET_TTL_SECONDS)
        pipe.expire(f"{prefix}:minute:{minute}", BUCKET_TTL_SECONDS)
        pipe.expire(f"{prefix}:sample:{minute}", BUCKET_TTL_SECONDS)
        pipe.execute()
    except Exception:
        pass


def _host_values() -> dict[str, float]:
    values: dict[str, float] = {}
    try:
        import psutil

        values["host_cpu_percent"] = float(psutil.cpu_percent(interval=None))
        memory = psutil.virtual_memory()
        values.update({
            "host_memory_used_bytes": float(memory.used),
            "host_memory_available_bytes": float(memory.available),
            "host_memory_percent": float(memory.percent),
        })
        swap = psutil.swap_memory()
        values.update({"host_swap_used_bytes": float(swap.used), "host_swap_percent": float(swap.percent)})
        root = psutil.disk_usage(os.path.abspath(os.sep))
        values.update({
            "host_root_disk_used_bytes": float(root.used),
            "host_root_disk_free_bytes": float(root.free),
            "host_root_disk_percent": float(root.percent),
        })
        load = getattr(os, "getloadavg", None)
        if callable(load):
            one, five, fifteen = load()
            values.update({"host_load_1m": float(one), "host_load_5m": float(five), "host_load_15m": float(fifteen)})
        disk_io = psutil.disk_io_counters()
        if disk_io:
            values.update({"host_disk_read_bytes_total": float(disk_io.read_bytes), "host_disk_write_bytes_total": float(disk_io.write_bytes)})
        net_io = psutil.net_io_counters()
        if net_io:
            values.update({"host_network_in_bytes_total": float(net_io.bytes_recv), "host_network_out_bytes_total": float(net_io.bytes_sent)})
        seen_mounts: set[str] = set()
        for partition in psutil.disk_partitions(all=False):
            mount = str(partition.mountpoint or "")
            if not mount or mount in seen_mounts:
                continue
            seen_mounts.add(mount)
            try:
                usage = psutil.disk_usage(mount)
            except (OSError, PermissionError):
                continue
            values.update({
                f"host_disk_used_bytes|{mount}": float(usage.used),
                f"host_disk_free_bytes|{mount}": float(usage.free),
                f"host_disk_percent|{mount}": float(usage.percent),
            })
    except Exception:
        # The caller wraps each collector so one unavailable psutil field does
        # not prevent the database or Redis probes from completing.
        raise
    return values


def _query_pairs(frappe, query: str) -> dict[str, float]:
    rows = frappe.db.sql(query, as_list=True)
    result = {}
    for row in rows or []:
        if len(row) >= 2:
            result[str(row[0])] = _safe_number(row[1])
    return result


def _database_runtime_values(frappe) -> dict[str, float]:
    values: dict[str, float] = {}
    status = _query_pairs(frappe, "SHOW GLOBAL STATUS")
    variables = _query_pairs(frappe, "SHOW GLOBAL VARIABLES")
    connected = status.get("Threads_connected")
    maximum = variables.get("max_connections")
    if connected is not None:
        values["database_connections"] = connected
    if maximum is not None:
        values["database_max_connections"] = maximum
    if maximum and connected is not None:
        values["database_connections_percent"] = min(100.0, 100.0 * connected / maximum)
    for status_key, metric in (
        ("Threads_running", "database_threads_running"),
        ("Slow_queries", "database_slow_queries_total"),
        ("Questions", "database_questions_total"),
    ):
        if status_key in status:
            values[metric] = status[status_key]
    reads = status.get("Innodb_buffer_pool_reads")
    requests = status.get("Innodb_buffer_pool_read_requests")
    if reads is not None and requests is not None and requests > 0:
        values["database_buffer_pool_hit_percent"] = max(0.0, min(100.0, 100.0 * (1.0 - reads / requests)))
    try:
        row = frappe.db.sql(
            "SELECT COUNT(*) FROM information_schema.PROCESSLIST "
            "WHERE COMMAND <> 'Sleep' AND TIME >= 5 AND ID <> CONNECTION_ID()",
            as_list=True,
        )
        values["database_long_queries"] = _safe_number(row[0][0]) if row and row[0] else 0.0
    except Exception:
        raise
    return values


def _redis_info_metrics(redis, prefix: str) -> dict[str, float]:
    info = redis.info() or {}
    normalized = {str(key).lower(): value for key, value in info.items()}
    used = _safe_number(normalized.get("used_memory"))
    maximum = _safe_number(normalized.get("maxmemory"))
    values = {
        f"{prefix}_memory_used_bytes": used,
        f"{prefix}_clients_connected": _safe_number(normalized.get("connected_clients")),
        f"{prefix}_cache_hits_total": _safe_number(normalized.get("keyspace_hits")),
        f"{prefix}_cache_misses_total": _safe_number(normalized.get("keyspace_misses")),
        f"{prefix}_evicted_keys_total": _safe_number(normalized.get("evicted_keys")),
    }
    if maximum > 0:
        values[f"{prefix}_memory_max_bytes"] = maximum
        values[f"{prefix}_memory_percent"] = min(100.0, used * 100.0 / maximum)
    hits, misses = values[f"{prefix}_cache_hits_total"], values[f"{prefix}_cache_misses_total"]
    if hits + misses > 0:
        values[f"{prefix}_cache_hit_percent"] = 100.0 * hits / (hits + misses)
    return values


def _redis_info_values(frappe) -> dict[str, float]:
    values = _redis_info_metrics(_redis(frappe), "redis")
    try:
        from frappe.utils.background_jobs import get_redis_conn

        queue_redis = get_redis_conn()
        values.update(_redis_info_metrics(queue_redis, "redis_queue"))
    except Exception:
        # Some small/custom installations share or omit a separately reachable
        # RQ Redis service; cache probes still report independently.
        pass
    return values


def _queue_values() -> dict[str, float]:
    values: dict[str, float] = {}
    from frappe.utils.background_jobs import get_redis_conn
    from rq import Queue
    from rq.registry import FailedJobRegistry

    connection = get_redis_conn()
    total_depth = 0
    total_failed = 0
    for queue_name in ("short", "default", "long"):
        queue = Queue(queue_name, connection=connection)
        depth = int(queue.count or 0)
        values[f"queue_{queue_name}_depth"] = float(depth)
        total_depth += depth
        try:
            failed = int(FailedJobRegistry(queue=queue, connection=connection).count or 0)
        except Exception:
            failed = 0
        values[f"queue_{queue_name}_failed"] = float(failed)
        total_failed += failed
    values["queue_depth_total"] = float(total_depth)
    values["queue_failed_total"] = float(total_failed)
    return values


def _error_count(frappe, doctype: str, filters: dict) -> float:
    try:
        if not frappe.db.exists("DocType", doctype):
            return 0.0
        return float(frappe.db.count(doctype, filters=filters) or 0)
    except Exception:
        return 0.0


def _application_values(frappe) -> dict[str, float]:
    from datetime import datetime, timedelta

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    filters = {"creation": (">=", since)}
    values = {"application_error_logs_per_minute": _error_count(frappe, "Error Log", filters)}
    # Frappe's current core tree has no Login Attempt DocType or stable failure
    # hook. Do not infer failed logins from generic Error Log text.
    for status, metric in (("Not Sent", "email_queue_pending"), ("Error", "email_queue_errors")):
        values[metric] = _error_count(frappe, "Email Queue", {"status": status})
    return values


def _walk_size(root: str) -> tuple[int, int, bool]:
    total = files = 0
    truncated = False
    if not os.path.isdir(root):
        return 0, 0, False
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
        for name in names:
            path = os.path.join(base, name)
            try:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                total += os.path.getsize(path)
                files += 1
            except OSError:
                continue
            if files >= _MAX_WALK_FILES:
                truncated = True
                return total, files, truncated
    return total, files, truncated


def _hourly_values(frappe) -> dict[str, float]:
    """Expensive DB table, backup, file-storage, and log-tree measurements."""
    values: dict[str, float] = {}
    # MariaDB-only details are an explicit app compatibility constraint.
    row = frappe.db.sql(
        "SELECT COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) "
        "FROM information_schema.tables WHERE TABLE_SCHEMA = DATABASE()",
        as_list=True,
    )
    values["database_size_bytes"] = _safe_number(row[0][0]) if row and row[0] else 0.0
    table_rows = frappe.db.sql(
        "SELECT TABLE_NAME, COALESCE(DATA_LENGTH,0) + COALESCE(INDEX_LENGTH,0) "
        "FROM information_schema.tables WHERE TABLE_SCHEMA = DATABASE() "
        "ORDER BY COALESCE(DATA_LENGTH,0) + COALESCE(INDEX_LENGTH,0) DESC LIMIT 10",
        as_list=True,
    ) or []
    for name, size in table_rows:
        label = re.sub(r"[^A-Za-z0-9_.$-]", "_", str(name))[:120]
        values[f"database_table_size_bytes|{label}"] = _safe_number(size)

    backups = os.path.abspath(frappe.get_site_path("private", "backups"))
    newest: tuple[float, int] | None = None
    backup_count = 0
    try:
        for name in os.listdir(backups):
            path = os.path.join(backups, name)
            try:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                stat = os.stat(path)
                backup_count += 1
                if newest is None or stat.st_mtime > newest[0]:
                    newest = (stat.st_mtime, stat.st_size)
            except OSError:
                continue
    except OSError:
        pass
    values["backup_count"] = float(backup_count)
    if newest is not None:
        values["backup_age_hours"] = max(0.0, (time.time() - newest[0]) / 3600.0)
        values["backup_latest_size_bytes"] = float(newest[1])

    for scope in ("public", "private"):
        root = os.path.abspath(frappe.get_site_path(scope, "files"))
        size, count, truncated = _walk_size(root)
        values[f"storage_{scope}_bytes"] = float(size)
        values[f"storage_{scope}_files"] = float(count)
        if truncated:
            values[f"storage_{scope}_scan_truncated"] = 1.0

    site_path = os.path.abspath(frappe.get_site_path())
    bench_path = os.path.dirname(os.path.dirname(site_path))
    log_roots = {"site": os.path.join(site_path, "logs"), "bench": os.path.join(bench_path, "logs")}
    total_log_bytes = 0
    for scope, root in log_roots.items():
        size, count, truncated = _walk_size(os.path.abspath(root))
        values[f"log_directory_bytes|{scope}"] = float(size)
        values[f"log_directory_files|{scope}"] = float(count)
        total_log_bytes += size
        if truncated:
            values[f"log_directory_scan_truncated|{scope}"] = 1.0
    values["log_directory_bytes"] = float(total_log_bytes)
    return values


def _sample_bundle(frappe, *, hourly: bool = False) -> dict[str, float]:
    values: dict[str, float] = {}
    probes = [
        ("host", _host_values),
        ("database", lambda: _database_runtime_values(frappe)),
        ("redis", lambda: _redis_info_values(frappe)),
        ("queues", _queue_values),
        ("application", lambda: _application_values(frappe)),
    ]
    if hourly:
        probes.append(("hourly", lambda: _hourly_values(frappe)))
    for name, probe in probes:
        try:
            values.update(probe())
        except Exception:
            _collector_error(frappe, name)
    # Derived gauges are emitted only when the underlying inputs exist.
    minute = _hash_values(_redis(frappe), f"{_prefix(frappe)}:minute:{_minute_epoch()}")
    requests = _safe_number(minute.get("requests"))
    errors = _safe_number(minute.get("errors"))
    if requests:
        values["request_error_rate_percent"] = min(100.0, 100.0 * errors / requests)
    try:
        settings = get_settings(frappe)
        if settings.get("mode") != "external":
            last_flush = _redis(frappe).get(f"{_prefix(frappe)}:last_flush")
            last_flush = _decode(last_flush) if last_flush else ""
            if last_flush:
                try:
                    parsed = datetime.fromisoformat(last_flush.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    values["flush_age_seconds"] = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
                except ValueError:
                    pass
    except Exception:
        _collector_error(frappe, "flush_age")
    return values


def _write_samples(frappe, values: dict[str, float]) -> None:
    if not values:
        return
    redis = _redis(frappe)
    prefix = _prefix(frappe)
    minute = _minute_epoch()
    key = f"{prefix}:sample:{minute}"
    serialized = {str(name): _safe_number(value) for name, value in values.items()}
    sampled_at = datetime.now(timezone.utc).isoformat()
    pipe = redis.pipeline(transaction=False)
    pipe.hset(key, mapping=serialized)
    pipe.expire(key, BUCKET_TTL_SECONDS)
    pipe.hset(f"{prefix}:current_sample", mapping={**serialized, "sampled_at": sampled_at})
    pipe.expire(f"{prefix}:current_sample", BUCKET_TTL_SECONDS)
    pipe.set(f"{prefix}:last_sample", sampled_at, ex=BUCKET_TTL_SECONDS)
    pipe.execute()


def sample_minute() -> None:
    """Write host, DB, Redis, queue, and app-health gauges to Redis."""
    frappe = _frappe()
    if frappe is None:
        return
    try:
        settings = get_settings(frappe)
        if not settings.get("enable_collection", True):
            return
        _write_samples(frappe, _sample_bundle(frappe))
    except Exception:
        _collector_error(frappe, "sample")


def sample_hourly() -> None:
    """Add DB table sizes, backup age, storage, and log sizes hourly."""
    frappe = _frappe()
    if frappe is None:
        return
    try:
        settings = get_settings(frappe)
        if not settings.get("enable_collection", True):
            return
        _write_samples(frappe, _sample_bundle(frappe, hourly=True))
    except Exception:
        _collector_error(frappe, "hourly_sample")


def _time_string(epoch_minute: int) -> str:
    return datetime.fromtimestamp(epoch_minute * 60, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _record_name(site: str, identity: str) -> str:
    return hashlib.sha1(f"{site}\0{identity}".encode("utf-8", "replace")).hexdigest()[:20]


def _base_db_fields(frappe, identity: str, now: datetime) -> dict[str, Any]:
    owner = "Administrator"
    return {"name": _record_name(_site_name(frappe), identity), "owner": owner, "creation": now,
            "modified": now, "modified_by": owner, "docstatus": 0, "idx": 0}


def _metric_row(frappe, ts: str, metric: str, host: str, label: str,
                count: float, total: float, maximum: float, now: datetime) -> dict[str, Any]:
    identity = f"metric\0{ts}\0{metric}\0{host}\0{label}"
    row = _base_db_fields(frappe, identity, now)
    row.update({"ts": ts, "metric": metric[:140], "host": host[:140], "label": label[:140],
                "count": count, "sum": total, "max": maximum})
    return row


def _top_row(frappe, hour_epoch: int, dimension: str, member: str,
             requests: float, byte_count: float, errors: float, now: datetime) -> dict[str, Any]:
    hour = datetime.fromtimestamp(hour_epoch * 3600, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    identity = f"top\0{hour}\0{dimension}\0{member}"
    row = _base_db_fields(frappe, identity, now)
    row.update({"hour": hour, "dimension": dimension, "key": member[:140], "requests": int(max(0, requests)),
                "bytes": int(max(0, byte_count)), "errors": int(max(0, errors))})
    return row


def _zrange_top(redis, key: str) -> list[tuple[str, float]]:
    try:
        return [(_decode(member), _safe_number(score)) for member, score in (redis.zrevrange(key, 0, TOP_N - 1, withscores=True) or [])]
    except Exception:
        return []


def _zscore(redis, key: str, member: str) -> float:
    try:
        return _safe_number(redis.zscore(key, member))
    except Exception:
        return 0.0


def _scan_keys(redis, pattern: str, limit: int = MAX_FLUSH_KEYS) -> list[str]:
    result = []
    try:
        for key in redis.scan_iter(match=pattern, count=500):
            result.append(_decode(key))
            if len(result) >= limit:
                break
    except Exception:
        pass
    return result


def _add_counter_metrics(frappe, rows: list[dict[str, Any]], fields: dict[str, str],
                         ts: str, host: str, now: datetime, label: str = "all") -> None:
    def number(name: str) -> float:
        return _safe_number(fields.get(name, 0))

    requests, errors = number("requests"), number("errors")
    duration_sum, bytes_in, bytes_out = number("duration_ms_sum"), number("bytes_in"), number("bytes_out")
    if requests:
        max_duration = next((float(bound) for bound in reversed(HISTOGRAM_BOUNDS_MS) if number(f"hist_le_{bound}")), 0.0)
        if number("hist_gt_5000"):
            max_duration = 5000.0
        rows.extend((_metric_row(frappe, ts, "requests", host, label, 1, requests, requests, now),
                     _metric_row(frappe, ts, "request_errors", host, label, 1, errors, errors, now),
                     _metric_row(frappe, ts, "request_duration_ms", host, label, requests, duration_sum, max_duration, now),
                     _metric_row(frappe, ts, "request_bytes_in", host, label, 1, bytes_in, bytes_in, now),
                     _metric_row(frappe, ts, "request_bytes_out", host, label, 1, bytes_out, bytes_out, now)))
    for status in ("1xx", "2xx", "3xx", "4xx", "5xx", "other"):
        count = number(f"status_{status}")
        if count:
            rows.append(_metric_row(frappe, ts, f"request_status_{status}", host, label, 1, count, count, now))
    for bound in HISTOGRAM_BOUNDS_MS:
        count = number(f"hist_le_{bound}")
        if count:
            rows.append(_metric_row(frappe, ts, f"request_duration_le_{bound}_ms", host, label, 1, count, count, now))
    if number("hist_gt_5000"):
        count = number("hist_gt_5000")
        rows.append(_metric_row(frappe, ts, "request_duration_gt_5000_ms", host, label, 1, count, count, now))
    job_count = number("jobs_count")
    if job_count:
        failed, duration = number("jobs_failed"), number("jobs_duration_ms_sum")
        rows.extend((_metric_row(frappe, ts, "jobs", host, "all", 1, job_count, job_count, now),
                     _metric_row(frappe, ts, "job_failures", host, "all", 1, failed, failed, now),
                     _metric_row(frappe, ts, "job_duration_ms", host, "all", job_count, duration, 0, now)))
    for field, raw in fields.items():
        if field.startswith("queue_") and field.endswith("_jobs") and _safe_number(raw):
            queue = field[len("queue_"):-len("_jobs")]
            rows.append(_metric_row(frappe, ts, "jobs_by_queue", host, queue, 1, _safe_number(raw), _safe_number(raw), now))
    for endpoint in ("api_method", "api_resource", "api_versioned", "api_other", "login", "logout", "assets", "files", "desk_app", "website_home", "other"):
        count = number(f"endpoint_{endpoint}_requests")
        if count:
            endpoint_label = endpoint.replace("_", ".")
            rows.extend((_metric_row(frappe, ts, "requests", host, endpoint_label, 1, count, count, now),
                         _metric_row(frappe, ts, "request_duration_ms", host, endpoint_label, count, number(f"endpoint_{endpoint}_duration_ms_sum"), 0, now),
                         _metric_row(frappe, ts, "request_bytes_in", host, endpoint_label, 1, number(f"endpoint_{endpoint}_bytes_in"), 0, now),
                         _metric_row(frappe, ts, "request_bytes_out", host, endpoint_label, 1, number(f"endpoint_{endpoint}_bytes_out"), 0, now)))
    for field, raw in fields.items():
        if field.startswith("collector_errors|") and _safe_number(raw):
            rows.append(_metric_row(frappe, ts, "collector_errors", host, field.split("|", 1)[1], 1, _safe_number(raw), _safe_number(raw), now))


def _sample_metric_row(frappe, rows, values, ts, host, now):
    for field, raw in values.items():
        metric, separator, label = field.partition("|")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,139}", metric):
            continue
        value = _safe_number(raw)
        rows.append(_metric_row(frappe, ts, metric, host, label if separator else "", 1, value, value, now))


def flush_metrics() -> None:
    """Bulk-insert completed Redis buckets; keep source keys on DB failure."""
    frappe = _frappe()
    if frappe is None:
        return
    try:
        settings = get_settings(frappe, refresh=True)
        if not settings.get("enable_collection", True) or settings.get("mode") == "external":
            return
        prefix, redis = _prefix(frappe), _redis(frappe)
        current_minute = _minute_epoch()
        current_hour = current_minute // 60
        now, host = datetime.now(timezone.utc).replace(tzinfo=None), _host_name()
        metric_rows: list[dict[str, Any]] = []
        usage_rows: list[dict[str, Any]] = []
        consumed_keys: list[str] = []
        for key in _scan_keys(redis, f"{prefix}:minute:*"):
            try:
                minute = int(key.rsplit(":", 1)[-1])
                if minute >= current_minute:
                    continue
                fields = _hash_values(redis, key)
                if fields:
                    _add_counter_metrics(frappe, metric_rows, fields, _time_string(minute), host, now)
                    consumed_keys.append(key)
            except (TypeError, ValueError):
                continue
        for key in _scan_keys(redis, f"{prefix}:sample:*"):
            try:
                minute = int(key.rsplit(":", 1)[-1])
                if minute >= current_minute:
                    continue
                values = _hash_values(redis, key)
                _sample_metric_row(frappe, metric_rows, values, _time_string(minute), host, now)
                if values:
                    consumed_keys.append(key)
            except (TypeError, ValueError):
                continue
        for dimension in ("user", "ip", "endpoint"):
            for key in _scan_keys(redis, f"{prefix}:top:*:{dimension}"):
                try:
                    hour = int(key.split(":")[-2])
                    if hour >= current_hour:
                        continue
                    byte_key = f"{key}:bytes"
                    error_key = f"{key}:errors"
                    for member, score in _zrange_top(redis, key):
                        usage_rows.append(_top_row(
                            frappe, hour, dimension, member, score,
                            _zscore(redis, byte_key, member), _zscore(redis, error_key, member), now,
                        ))
                    consumed_keys.extend((key, byte_key, error_key))
                except (TypeError, ValueError):
                    continue
        for key in _scan_keys(redis, f"{prefix}:jobtop:*:count"):
            try:
                hour = int(key.split(":")[-2])
                if hour >= current_hour:
                    continue
                duration_key = key.rsplit(":", 1)[0] + ":duration_ms_sum"
                failure_key = key.rsplit(":", 1)[0] + ":failures"
                ts = datetime.fromtimestamp(hour * 3600, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                members = _zrange_top(redis, key)
                for method, count in members:
                    duration = _zscore(redis, duration_key, method)
                    failures = _zscore(redis, failure_key, method)
                    metric_rows.extend((
                        _metric_row(frappe, ts, "jobs_by_method", host, method, 1, count, count, now),
                        _metric_row(frappe, ts, "job_duration_ms_by_method", host, method, count, duration, 0, now),
                        _metric_row(frappe, ts, "job_failures_by_method", host, method, 1, failures, failures, now),
                    ))
                consumed_keys.extend((key, duration_key, failure_key))
            except (TypeError, ValueError):
                continue
        for start in range(0, len(metric_rows), DB_INSERT_CHUNK_SIZE):
            chunk = metric_rows[start:start + DB_INSERT_CHUNK_SIZE]
            frappe.db.bulk_insert("Metric Bucket", fields=list(chunk[0]), values=[list(row.values()) for row in chunk], ignore_duplicates=True)
        for start in range(0, len(usage_rows), DB_INSERT_CHUNK_SIZE):
            chunk = usage_rows[start:start + DB_INSERT_CHUNK_SIZE]
            frappe.db.bulk_insert("Usage Top", fields=list(chunk[0]), values=[list(row.values()) for row in chunk], ignore_duplicates=True)
        if metric_rows or usage_rows:
            # Do not acknowledge Redis source buckets until their DB transaction
            # is durable; if this commit fails, the outer handler leaves them.
            frappe.db.commit()
        # Deterministic record names + ignore_duplicates make retry safe. Delete
        # Redis keys only after every DB insert succeeded.
        if consumed_keys:
            for start in range(0, len(consumed_keys), 500):
                redis.delete(*consumed_keys[start:start + 500])
        try:
            redis.set(f"{prefix}:last_flush", datetime.now(timezone.utc).isoformat(), ex=BUCKET_TTL_SECONDS)
        except Exception:
            pass
    except Exception:
        try:
            frappe.log_error(title="Frappe Observability flush failed", message=frappe.get_traceback())
        except Exception:
            pass


def prune_metrics() -> None:
    """Delete expired DB rows in bounded chunks, separately from the flush."""
    frappe = _frappe()
    if frappe is None:
        return
    try:
        retention_days = int(get_settings(frappe, refresh=True).get("retention_days", 7))
        retention_days = min(30, max(1, retention_days))
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)
        for doctype, date_field in (("Metric Bucket", "ts"), ("Usage Top", "hour"), ("Log Access Entry", "time")):
            while True:
                rows = frappe.get_all(doctype, filters={date_field: ("<", cutoff)}, pluck="name", limit_page_length=PRUNE_CHUNK_SIZE)
                if not rows:
                    break
                frappe.db.delete(doctype, {"name": ["in", rows]})
                frappe.db.commit()
    except Exception:
        try:
            frappe.log_error(title="Frappe Observability prune failed", message=frappe.get_traceback())
        except Exception:
            pass


def run_minute() -> None:
    """Run the configured interval with a per-site Redis scheduler lock."""
    frappe = _frappe()
    if frappe is None:
        return
    try:
        settings = get_settings(frappe, refresh=True)
        if not settings.get("enable_collection", True):
            return
        try:
            interval = min(3600, max(60, int(settings.get("flush_interval_seconds", 60))))
        except (TypeError, ValueError):
            interval = 60
        redis = _redis(frappe)
        lock_key = f"{_prefix(frappe)}:scheduler_lock"
        if not redis.set(lock_key, str(time.time()), nx=True, ex=interval):
            return
    except Exception:
        # A Redis outage must not fail the scheduler callback; sample/flush
        # functions independently handle the degraded state.
        pass
    sample_minute()
    flush_metrics()
    try:
        from .alerts import evaluate_alerts

        evaluate_alerts()
    except Exception:
        _collector_error(frappe, "alerts")


def run_hourly() -> None:
    """Hourly heavy scans, isolated from the one-minute collection loop."""
    sample_hourly()
    flush_metrics()
