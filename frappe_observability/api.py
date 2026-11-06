"""Authenticated Prometheus text export over site-scoped Redis counters.

Metrics are sourced exclusively from Redis. The request/job hot path updates
``frappe_obs:v1:<site-hash>:cumulative``; the exporter maps only explicitly
allowlisted Prometheus names to a fixed set of aggregate fields. No user or IP
is exposed as a metric label.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import re
import secrets
from collections.abc import Mapping

from .collectors import _prefix, _redis

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_FIELDS = {
    "frappe_obs_requests_total": ("requests", "Total observed web requests"),
    "frappe_obs_request_errors_total": ("errors", "Total observed HTTP 4xx and 5xx responses"),
    "frappe_obs_request_bytes_in_total": ("bytes_in", "Total observed request bytes"),
    "frappe_obs_request_bytes_out_total": ("bytes_out", "Total observed response bytes"),
    "frappe_obs_jobs_total": ("jobs_count", "Total completed background jobs"),
    "frappe_obs_job_failures_total": ("jobs_failed", "Total failed background jobs"),
    "frappe_obs_job_duration_milliseconds_total": ("jobs_duration_ms_sum", "Cumulative observed background job duration in milliseconds"),
    "frappe_obs_collector_errors_total": ("collector_errors", "Total fail-open collector errors"),
}
_GAUGES = {
    "frappe_obs_host_cpu_percent": ("host_cpu_percent", "Host CPU utilization percentage"),
    "frappe_obs_host_memory_used_bytes": ("host_memory_used_bytes", "Host memory used in bytes"),
    "frappe_obs_host_memory_available_bytes": ("host_memory_available_bytes", "Host memory available in bytes"),
    "frappe_obs_host_memory_percent": ("host_memory_percent", "Host memory utilization percentage"),
    "frappe_obs_host_swap_used_bytes": ("host_swap_used_bytes", "Host swap used in bytes"),
    "frappe_obs_host_swap_percent": ("host_swap_percent", "Host swap utilization percentage"),
    "frappe_obs_host_root_disk_used_bytes": ("host_root_disk_used_bytes", "Root filesystem used bytes"),
    "frappe_obs_host_root_disk_free_bytes": ("host_root_disk_free_bytes", "Root filesystem free bytes"),
    "frappe_obs_host_root_disk_percent": ("host_root_disk_percent", "Root filesystem utilization percentage"),
    "frappe_obs_host_load_1m": ("host_load_1m", "Host load average over one minute"),
    "frappe_obs_host_load_5m": ("host_load_5m", "Host load average over five minutes"),
    "frappe_obs_host_load_15m": ("host_load_15m", "Host load average over fifteen minutes"),
    "frappe_obs_host_disk_read_bytes_total": ("host_disk_read_bytes_total", "Host cumulative disk bytes read"),
    "frappe_obs_host_disk_write_bytes_total": ("host_disk_write_bytes_total", "Host cumulative disk bytes written"),
    "frappe_obs_host_network_in_bytes_total": ("host_network_in_bytes_total", "Host cumulative network bytes received"),
    "frappe_obs_host_network_out_bytes_total": ("host_network_out_bytes_total", "Host cumulative network bytes sent"),
    "frappe_obs_database_connections": ("database_connections", "Current MariaDB connections"),
    "frappe_obs_database_max_connections": ("database_max_connections", "MariaDB max connections"),
    "frappe_obs_database_connections_percent": ("database_connections_percent", "MariaDB connection capacity used percentage"),
    "frappe_obs_database_threads_running": ("database_threads_running", "MariaDB running threads"),
    "frappe_obs_database_long_queries": ("database_long_queries", "MariaDB queries running for at least five seconds"),
    "frappe_obs_database_buffer_pool_hit_percent": ("database_buffer_pool_hit_percent", "MariaDB InnoDB buffer pool hit percentage"),
    "frappe_obs_database_size_bytes": ("database_size_bytes", "Site database table and index size in bytes"),
    "frappe_obs_redis_memory_used_bytes": ("redis_memory_used_bytes", "Redis cache memory used in bytes"),
    "frappe_obs_redis_memory_max_bytes": ("redis_memory_max_bytes", "Redis maxmemory configuration in bytes"),
    "frappe_obs_redis_memory_percent": ("redis_memory_percent", "Redis maxmemory used percentage"),
    "frappe_obs_redis_clients_connected": ("redis_clients_connected", "Redis connected clients"),
    "frappe_obs_redis_cache_hit_percent": ("redis_cache_hit_percent", "Redis cache hit percentage"),
    "frappe_obs_redis_evicted_keys_total": ("redis_evicted_keys_total", "Redis evicted keys since server start"),
    "frappe_obs_redis_queue_memory_used_bytes": ("redis_queue_memory_used_bytes", "RQ Redis memory used in bytes"),
    "frappe_obs_redis_queue_memory_max_bytes": ("redis_queue_memory_max_bytes", "RQ Redis maxmemory in bytes"),
    "frappe_obs_redis_queue_memory_percent": ("redis_queue_memory_percent", "RQ Redis maxmemory used percentage"),
    "frappe_obs_redis_queue_clients_connected": ("redis_queue_clients_connected", "RQ Redis connected clients"),
    "frappe_obs_redis_queue_cache_hit_percent": ("redis_queue_cache_hit_percent", "RQ Redis cache hit percentage"),
    "frappe_obs_redis_queue_evicted_keys_total": ("redis_queue_evicted_keys_total", "RQ Redis evicted keys since server start"),
    "frappe_obs_queue_depth_total": ("queue_depth_total", "Total queued background jobs"),
    "frappe_obs_queue_failed_total": ("queue_failed_total", "Failed background jobs currently retained in RQ"),
    "frappe_obs_error_logs_per_minute": ("application_error_logs_per_minute", "Frappe Error Log records created in the last minute"),
    "frappe_obs_email_queue_pending": ("email_queue_pending", "Pending Frappe emails"),
    "frappe_obs_email_queue_errors": ("email_queue_errors", "Frappe email queue errors"),
    "frappe_obs_request_error_rate_percent": ("request_error_rate_percent", "HTTP request error rate in the current minute"),
    "frappe_obs_backup_age_hours": ("backup_age_hours", "Age of the newest site backup in hours"),
    "frappe_obs_backup_latest_size_bytes": ("backup_latest_size_bytes", "Size of the newest site backup in bytes"),
    "frappe_obs_storage_public_bytes": ("storage_public_bytes", "Public site files storage in bytes"),
    "frappe_obs_storage_private_bytes": ("storage_private_bytes", "Private site files storage in bytes"),
    "frappe_obs_log_directory_bytes": ("log_directory_bytes", "Site and bench log directory size in bytes"),
    "frappe_obs_flush_age_seconds": ("flush_age_seconds", "Seconds since the last successful database flush"),
}


def _settings(frappe):
    try:
        return frappe.get_cached_doc("Observability Settings")
    except Exception:
        return None


def _setting(frappe, key, default=None):
    settings = _settings(frappe)
    if settings is not None:
        try:
            value = settings.get(key)
            if value not in (None, ""):
                return value
        except Exception:
            pass
    config_key = {
        "export_token_sha256": "observability_metrics_token_sha256",
        "export_cidrs": "observability_metrics_ip_allowlist",
        "metric_allowlist": "observability_metric_allowlist",
        "metric_label_allowlist": "observability_metric_label_allowlist",
        "metric_help": "observability_metric_help",
        "mode": "observability_metrics_mode",
    }.get(key, key)
    try:
        return frappe.conf.get(config_key, default)
    except Exception:
        return default


def _items(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,\n;]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def _client_ip(frappe):
    request = getattr(getattr(frappe, "local", None), "request", None)
    value = getattr(request, "remote_addr", None)
    try:
        return ipaddress.ip_address(value) if value else None
    except (TypeError, ValueError):
        return None


def _ip_allowed(frappe):
    configured = _setting(frappe, "export_cidrs", None)
    if configured in (None, "", [], (), {}):
        return True
    address = _client_ip(frappe)
    if address is None:
        return False
    try:
        networks = [ipaddress.ip_network(str(item), strict=False) for item in _items(configured)]
    except (TypeError, ValueError):
        return False
    return any(address.version == network.version and address in network for network in networks)


def _token_valid(frappe):
    expected = str(_setting(frappe, "export_token_sha256", "") or "").strip().lower()
    valid_config = bool(re.fullmatch(r"[0-9a-f]{64}", expected))
    expected = expected if valid_config else "0" * 64
    headers = getattr(getattr(getattr(frappe, "local", None), "request", None), "headers", {})
    authorization = str(headers.get("Authorization", ""))
    scheme, separator, token = authorization.partition(" ")
    token = token.strip()
    supplied = hashlib.sha256((token if separator and scheme.lower() == "bearer" else "").encode()).hexdigest()
    return valid_config and bool(separator and scheme.lower() == "bearer" and token and " " not in token) and hmac.compare_digest(supplied, expected)


def _parse_json(value, default):
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            import json
            loaded = json.loads(value)
            return loaded if isinstance(loaded, Mapping) else default
        except (TypeError, ValueError):
            return default
    return default


def _metric_text(frappe):
    configured = _items(_setting(frappe, "metric_allowlist", None))
    names = [name for name in configured if isinstance(name, str) and _METRIC_NAME.fullmatch(name) and name in (_FIELDS | _GAUGES)]
    if not names:
        return None
    redis = _redis(frappe)
    values = redis.hgetall(f"{_prefix(frappe)}:cumulative") or {}
    clean = {(key.decode() if isinstance(key, bytes) else str(key)): value for key, value in values.items()}
    sample_values = redis.hgetall(f"{_prefix(frappe)}:current_sample") or {}
    sample = {(key.decode() if isinstance(key, bytes) else str(key)): value for key, value in sample_values.items()}
    help_map = _parse_json(_setting(frappe, "metric_help", {}), {})
    output = []
    for metric in names:
        is_counter = metric in _FIELDS
        field, default_help = (_FIELDS if is_counter else _GAUGES)[metric]
        raw = clean.get(field, 0) if is_counter else sample.get(field)
        if raw is None:
            continue
        try:
            value = float(raw.decode() if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError, UnicodeError):
            continue
        if not math.isfinite(value) or value < 0:
            continue
        help_text = str(help_map.get(metric) or default_help).replace("\\", "\\\\").replace("\n", "\\n")
        kind = "counter" if is_counter else "gauge"
        output.extend((f"# HELP {metric} {help_text}", f"# TYPE {metric} {kind}", f"{metric} {value:.15g}"))
    return "\n".join(output) + ("\n" if output else "")


def metrics():
    """Return a token-protected Prometheus scrape body; never expose app errors."""
    import frappe

    request = getattr(getattr(frappe, "local", None), "request", None)
    if str(getattr(request, "method", "GET")).upper() != "GET":
        return _respond(frappe, "# GET required\n", 405)
    if not _token_valid(frappe):
        return _respond(frappe, "# authentication required\n", 401)
    settings = _settings(frappe)
    mode = str((settings.get("mode") if settings else _setting(frappe, "mode", "internal")) or "internal").lower()
    if mode not in {"external", "both"}:
        return _respond(frappe, "# metrics export disabled\n", 403)
    if not _ip_allowed(frappe):
        return _respond(frappe, "# source address not allowed\n", 403)
    try:
        body = _metric_text(frappe)
    except Exception:
        body = None
    if body is None:
        return _respond(frappe, "# metrics unavailable or no allowlisted metrics configured\n", 503)
    return _respond(frappe, body, 200)


def rotate_export_token():
    """Generate a new scrape token; persist only its digest and reveal it once."""
    import frappe

    user = str(getattr(frappe.session, "user", "Guest") or "Guest")
    roles = set(frappe.get_roles(user) or [])
    if user != "Administrator" and not roles.intersection({"System Manager", "Observability Admin"}):
        frappe.throw("Only observability administrators can rotate the export token.", frappe.PermissionError)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    settings = frappe.get_single("Observability Settings")
    settings.export_token_sha256 = digest
    settings.save(ignore_permissions=True)
    return {"token": token, "warning": "Copy this token now; it is not stored in plaintext."}


def _respond(frappe, body, status):
    response = frappe.local.response
    response["type"] = "txt"
    response["message"] = body
    response["http_status_code"] = status
    response["content_type"] = PROMETHEUS_CONTENT_TYPE
    response["headers"] = {"Content-Type": PROMETHEUS_CONTENT_TYPE, "Cache-Control": "no-store"}
    return body


try:
    import frappe as _frappe

    metrics = _frappe.whitelist(allow_guest=True)(metrics)
    rotate_export_token = _frappe.whitelist()(rotate_export_token)
except ImportError:
    pass
