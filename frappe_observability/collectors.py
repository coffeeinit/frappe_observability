"""Fail-open Redis collectors for request and background-job telemetry.

All event-path writes are queued in one Redis pipeline. Dimensions on metric
buckets are deliberately fixed/bounded; user and IP identifiers are confined
to top-N Redis sets and privacy-preserving active-user/IP sketches.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from datetime import datetime, timezone
from typing import Any


APP_PREFIX = "frappe_obs:v1"
BUCKET_TTL_SECONDS = 32 * 24 * 60 * 60
TOP_N = 50
HISTOGRAM_BOUNDS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
_SETTINGS_CACHE_TTL = 60.0
_settings_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# Only these route families become metric labels. Arbitrary Frappe methods,
# DocTypes, page slugs, query strings, and custom routes collapse into a fixed
# bucket rather than creating unbounded series.
_ENDPOINT_RULES = (
    (re.compile(r"^/(?:api/)?method(?:/|$)"), "api.method"),
    (re.compile(r"^/(?:api/)?resource(?:/|$)"), "api.resource"),
    (re.compile(r"^/api/(?:mrapi|v[0-9]+)(?:/|$)"), "api.versioned"),
    (re.compile(r"^/api(?:/|$)"), "api.other"),
    (re.compile(r"^/login(?:/|$)"), "login"),
    (re.compile(r"^/logout(?:/|$)"), "logout"),
    (re.compile(r"^/assets(?:/|$)"), "assets"),
    (re.compile(r"^/files(?:/|$)"), "files"),
    (re.compile(r"^/app(?:/|$)"), "desk.app"),
    (re.compile(r"^/$"), "website.home"),
)
_STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx", "other")


def _frappe():
    try:
        import frappe  # type: ignore

        return frappe
    except Exception:
        return None


def _site_name(frappe) -> str:
    try:
        local_site = getattr(getattr(frappe, "local", None), "site", None)
        if local_site:
            return str(local_site)
        conf = getattr(frappe, "conf", None)
        site = getattr(conf, "site", None) if conf is not None else None
        return str(site or "default")
    except Exception:
        return "default"


def _site_key(frappe) -> str:
    # Hashing the site name keeps keys unambiguous and avoids exposing hostnames
    # in Redis key listings. Every collector and task uses the same namespace.
    return hashlib.sha256(_site_name(frappe).encode("utf-8", "replace")).hexdigest()[:20]


def _prefix(frappe) -> str:
    return f"{APP_PREFIX}:{_site_key(frappe)}"


def _redis(frappe):
    """Return the underlying Redis client, accommodating Frappe cache wrappers."""
    cache = frappe.cache()
    for attr in ("redis", "redis_client", "_redis"):
        try:
            candidate = getattr(cache, attr, None)
            if candidate is not None:
                return candidate() if callable(candidate) and not hasattr(candidate, "pipeline") else candidate
        except Exception:
            continue
    if hasattr(cache, "pipeline"):
        return cache
    raise RuntimeError("Frappe Redis cache client is unavailable")


def get_settings(frappe=None, *, refresh: bool = False) -> dict[str, Any]:
    """Read the optional singleton once per process/site and fail to defaults.

    Request collectors only perform Redis updates. Settings are cached locally
    for at most a minute, so a cache miss is not paid on every request.
    """
    frappe = frappe or _frappe()
    defaults: dict[str, Any] = {
        "mode": "internal",
        "retention_days": 7,
        "ip_storage": "masked",
        "enable_collection": True,
    }
    if frappe is None:
        return defaults
    site = _site_name(frappe)
    now = time.monotonic()
    cached = _settings_cache.get(site)
    if not refresh and cached and cached[0] > now:
        return {**defaults, **cached[1]}

    settings: dict[str, Any] = {}
    try:
        doc = frappe.get_cached_doc("Observability Settings")
        for field in ("mode", "retention_days", "flush_interval_seconds", "ip_storage", "ip_masking", "mask_ips", "enable_collection", "alerts_enabled", "notification_recipients"):
            try:
                value = doc.get(field) if hasattr(doc, "get") else getattr(doc, field, None)
            except Exception:
                value = None
            if value is not None:
                settings[field] = value
    except Exception:
        # The app can be installed before its singleton exists, and the
        # collectors must not turn that setup gap into a request/job failure.
        pass
    normalized_mode = str(settings.get("mode", "internal") or "internal").lower()
    if normalized_mode not in {"internal", "external", "both"}:
        normalized_mode = "internal"
    settings["mode"] = normalized_mode
    try:
        settings["retention_days"] = min(30, max(1, int(settings.get("retention_days", 7))))
    except (TypeError, ValueError):
        settings["retention_days"] = 7
    _settings_cache[site] = (now + _SETTINGS_CACHE_TTL, settings)
    return {**defaults, **settings}


def _ip_storage_setting(settings: dict[str, Any]) -> str:
    raw = str(settings.get("ip_storage") or "").strip().lower()
    if raw in {"full", "unmasked", "false", "0"}:
        return "full"
    # Accept an explicit boolean-style field used by early settings drafts.
    if settings.get("mask_ips") is False or str(settings.get("ip_masking", "")).lower() in {"off", "false", "0"}:
        return "full"
    return "masked"


def mask_ip(value: Any, mode: str = "masked") -> str:
    """Normalize an IP before Redis storage; invalid input is discarded."""
    try:
        address = ipaddress.ip_address(str(value).split(",", 1)[0].strip())
        if mode == "full":
            return address.compressed
        prefix = 24 if address.version == 4 else 64
        return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False).network_address) + ("/24" if address.version == 4 else "/64")
    except Exception:
        return ""


def _bounded_endpoint(path: Any) -> str:
    try:
        path = str(path or "/").split("?", 1)[0]
        if len(path) > 2048:
            return "other"
        path = "/" + "/".join(part for part in path.split("/") if part)
        if path == "//":
            path = "/"
        for pattern, label in _ENDPOINT_RULES:
            if pattern.match(path):
                return label
    except Exception:
        pass
    return "other"


def _request_path(frappe) -> str:
    local = getattr(frappe, "local", None)
    request = getattr(local, "request", None)
    try:
        # Prefer matched_route when available; labels are still reduced to a
        # fixed family by _bounded_endpoint.
        route = getattr(request, "path", None) or getattr(request, "url", None)
        if route:
            return str(route)
    except Exception:
        pass
    try:
        path = getattr(local, "request_path", None)
        if path:
            return str(path)
    except Exception:
        pass
    return "/"


def _get_header(headers, name: str):
    try:
        return headers.get(name) or headers.get(name.lower()) or headers.get(name.title())
    except Exception:
        return None


def _int_nonnegative(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _status_class(status: Any) -> str:
    try:
        code = int(status)
        klass = f"{code // 100}xx"
        return klass if klass in _STATUS_CLASSES else "other"
    except (TypeError, ValueError):
        return "other"


def _site_now() -> tuple[int, int, str]:
    now = datetime.now(timezone.utc)
    return int(now.timestamp()) // 60, int(now.timestamp()) // 3600, now.strftime("%Y-%m-%d %H:%M:%S")


def _safe_identity(value: Any, max_length: int = 140) -> str:
    text = str(value or "").strip()
    # Reject control characters and cap attacker-controlled member size.
    text = "".join(char for char in text if char.isprintable())
    return text[:max_length]


def _user(frappe) -> str:
    try:
        return _safe_identity(getattr(getattr(frappe, "session", None), "user", ""))
    except Exception:
        return ""


def _expiry(pipe, keys) -> None:
    for key in keys:
        pipe.expire(key, BUCKET_TTL_SECONDS)


def _record_collector_error(frappe) -> None:
    """Count a bounded hook failure without ever propagating it to the caller."""
    try:
        prefix = _prefix(frappe)
        minute, _hour, _timestamp = _site_now()
        pipe = _redis(frappe).pipeline(transaction=False)
        pipe.hincrby(f"{prefix}:cumulative", "collector_errors", 1)
        pipe.hincrby(f"{prefix}:minute:{minute}", "collector_errors", 1)
        pipe.expire(f"{prefix}:cumulative", BUCKET_TTL_SECONDS)
        pipe.expire(f"{prefix}:minute:{minute}", BUCKET_TTL_SECONDS)
        pipe.execute()
    except Exception:
        pass


def _increment_top(
    pipe, key: str, member: str, amount: float = 1, *, byte_count: int = 0, errors: int = 0
) -> None:
    if not member:
        return
    pipe.zincrby(key, amount, member)
    byte_key, error_key = f"{key}:bytes", f"{key}:errors"
    if byte_count:
        pipe.zincrby(byte_key, byte_count, member)
    if errors:
        pipe.zincrby(error_key, errors, member)
    # Keep top sets bounded even if a site receives arbitrary identities.
    pipe.zremrangebyrank(key, 0, -(TOP_N + 1))
    if byte_count:
        pipe.zremrangebyrank(byte_key, 0, -(TOP_N + 1))
    if errors:
        pipe.zremrangebyrank(error_key, 0, -(TOP_N + 1))
    pipe.expire(key, BUCKET_TTL_SECONDS)
    if byte_count:
        pipe.expire(byte_key, BUCKET_TTL_SECONDS)
    if errors:
        pipe.expire(error_key, BUCKET_TTL_SECONDS)


def _metric_field(label: str) -> str:
    # labels passed here are from a small internal vocabulary.
    return re.sub(r"[^a-z0-9_]", "_", label.lower())[:48]


def _store_request(frappe, *, elapsed_ms: float, status: str, bytes_in: int, bytes_out: int) -> None:
    prefix = _prefix(frappe)
    minute, hour, _ = _site_now()
    bucket = f"{prefix}:minute:{minute}"
    cumulative = f"{prefix}:cumulative"
    endpoint = _bounded_endpoint(_request_path(frappe))
    status_field = f"status_{status}"
    endpoint_field = _metric_field(endpoint)
    settings = get_settings(frappe)
    if not settings.get("enable_collection", True):
        return
    ip_mode = _ip_storage_setting(settings)
    user = _user(frappe)
    request = getattr(getattr(frappe, "local", None), "request", None)
    try:
        remote_ip = getattr(request, "remote_addr", None) or getattr(request, "client_ip", None)
    except Exception:
        remote_ip = None
    ip = mask_ip(remote_ip, ip_mode)
    user_digest = hashlib.sha256(user.encode("utf-8", "replace")).hexdigest() if user else ""
    pipe = _redis(frappe).pipeline(transaction=False)
    expiring = [bucket]

    # Global-per-site aggregates, fixed status classes, and endpoint families.
    for key, field, amount in (
        (bucket, "requests", 1),
        (bucket, "errors", 1 if status in {"4xx", "5xx"} else 0),
        (bucket, status_field, 1),
        (bucket, "duration_ms_sum", elapsed_ms),
        (bucket, "bytes_in", bytes_in),
        (bucket, "bytes_out", bytes_out),
        (bucket, f"endpoint_{endpoint_field}_requests", 1),
        (bucket, f"endpoint_{endpoint_field}_duration_ms_sum", elapsed_ms),
        (bucket, f"endpoint_{endpoint_field}_bytes_in", bytes_in),
        (bucket, f"endpoint_{endpoint_field}_bytes_out", bytes_out),
        (cumulative, "requests", 1),
        (cumulative, "errors", 1 if status in {"4xx", "5xx"} else 0),
        (cumulative, "bytes_in", bytes_in),
        (cumulative, "bytes_out", bytes_out),
    ):
        if isinstance(amount, float):
            pipe.hincrbyfloat(key, field, amount)
        else:
            pipe.hincrby(key, field, amount)
    expiring.append(cumulative)

    hist_bound = next((bound for bound in HISTOGRAM_BOUNDS_MS if elapsed_ms <= bound), None)
    hist_field = f"hist_le_{hist_bound}" if hist_bound is not None else "hist_gt_5000"
    pipe.hincrby(bucket, hist_field, 1)
    pipe.hincrby(cumulative, hist_field, 1)

    minute_user = f"{prefix}:active:user:m:{minute}"
    minute_ip = f"{prefix}:active:ip:m:{minute}"
    hour_user = f"{prefix}:active:user:h:{hour}"
    hour_ip = f"{prefix}:active:ip:h:{hour}"
    # Hash PII before HLL insertion: sketches are for counts, not identities.
    if user_digest:
        pipe.pfadd(minute_user, user_digest)
        pipe.pfadd(hour_user, user_digest)
    if ip:
        ip_digest = hashlib.sha256(ip.encode("utf-8", "replace")).hexdigest()
        pipe.pfadd(minute_ip, ip_digest)
        pipe.pfadd(hour_ip, ip_digest)

    # Sorted sets are approximate top-N views, never metric labels. Member
    # cardinality is capped after each update; a racing burst can exceed 50 only
    # transiently until its queued trim commands execute.
    failed = 1 if status in {"4xx", "5xx"} else 0
    for dimension, member in (("user", user), ("ip", ip), ("endpoint", endpoint)):
        if member:
            _increment_top(
                pipe,
                f"{prefix}:top:{hour}:{dimension}",
                member,
                byte_count=bytes_in + bytes_out,
                errors=failed,
            )
    _expiry(pipe, expiring + [minute_user, minute_ip, hour_user, hour_ip])
    pipe.execute()


def before_request(*args, **kwargs) -> None:
    """Frappe request hook: capture a monotonic start timestamp, fail-open."""
    frappe = _frappe()
    if frappe is None:
        return
    try:
        local = frappe.local
        if not get_settings(frappe).get("enable_collection", True):
            return
        local._frappe_observability_request = time.perf_counter()
    except Exception:
        _record_collector_error(frappe)
        return


def after_request(*args, **kwargs):
    """Frappe request hook: record one Redis-only request sample, fail-open.

    Returns the response unchanged when one was passed by the hook dispatcher.
    """
    response = args[0] if args else kwargs.get("response")
    frappe = _frappe()
    if frappe is None:
        return response
    try:
        local = frappe.local
        started = getattr(local, "_frappe_observability_request", None)
        # If the before hook was not installed, do not invent a zero duration.
        if started is None:
            return response
        delattr(local, "_frappe_observability_request")
        elapsed_ms = max(0.0, (time.perf_counter() - float(started)) * 1000.0)
        local_response = getattr(local, "response", None)
        status_value = None
        for source in (response, local_response):
            if source is None:
                continue
            status_value = getattr(source, "status_code", None)
            if status_value is None and isinstance(source, dict):
                status_value = source.get("http_status_code") or source.get("status_code")
            if status_value is not None:
                break
        status = _status_class(status_value or 200)
        request = getattr(local, "request", None)
        headers = getattr(request, "headers", {}) if request is not None else {}
        bytes_in = _int_nonnegative(_get_header(headers, "Content-Length"))
        response_headers = getattr(response, "headers", None) if response is not None else None
        if response_headers is None:
            response_headers = getattr(local_response, "headers", {}) if local_response is not None else {}
        bytes_out = _int_nonnegative(_get_header(response_headers, "Content-Length"))
        if not bytes_out and response is not None:
            body = getattr(response, "data", None)
            if isinstance(body, (bytes, bytearray, memoryview, str)):
                try:
                    bytes_out = len(body.encode("utf-8")) if isinstance(body, str) else len(body)
                except Exception:
                    bytes_out = 0
        _store_request(
            frappe,
            elapsed_ms=elapsed_ms,
            status=status,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
        )
    except Exception:
        # Collection is explicitly fail-open; no logger call in the hot path.
        _record_collector_error(frappe)
    return response


def _job_parts(args, kwargs) -> tuple[str, str, Any]:
    job = kwargs.get("job")
    method = kwargs.get("method") or kwargs.get("func") or kwargs.get("method_name")
    queue = kwargs.get("queue")
    for arg in args:
        if isinstance(arg, str) and not method:
            method = arg
        elif job is None and not isinstance(arg, (str, bytes, int, float)):
            job = arg
    if job is not None:
        method = method or getattr(job, "func_name", None) or getattr(job, "description", None)
        queue = queue or getattr(job, "queue_name", None)
        queue = queue or getattr(getattr(job, "origin", None), "name", None)
    method = _safe_identity(method or "other", max_length=180)
    # RQ/Frappe deployments commonly use these names; custom queues collapse.
    queue_text = str(queue or "default").lower()
    if queue_text not in {"short", "default", "long", "standard", "workers"}:
        queue_text = "other"
    return method, queue_text, job


def before_job(*args, **kwargs) -> None:
    """Frappe background-job hook; stores only process-local timing context."""
    frappe = _frappe()
    if frappe is None:
        return
    try:
        if not get_settings(frappe).get("enable_collection", True):
            return
        method, queue, _job = _job_parts(args, kwargs)
        frappe.local._frappe_observability_job = {
            "started": time.perf_counter(),
            "method": method,
            "queue": queue,
        }
    except Exception:
        _record_collector_error(frappe)


def _job_failed(args, kwargs, job) -> bool:
    for key in ("exception", "exc", "error", "exc_info"):
        if kwargs.get(key):
            return True
    for arg in args:
        if isinstance(arg, BaseException):
            return True
    try:
        if job is not None and (getattr(job, "exc_info", None) or getattr(job, "is_failed", False)):
            return True
    except Exception:
        pass
    return False


def after_job(*args, **kwargs) -> None:
    """Frappe background-job hook: duration/failure counters in Redis only."""
    frappe = _frappe()
    if frappe is None:
        return
    state = None
    try:
        local = frappe.local
        state = getattr(local, "_frappe_observability_job", None)
        if state is None:
            return
        delattr(local, "_frappe_observability_job")
        if not get_settings(frappe).get("enable_collection", True):
            return
        elapsed_ms = max(0.0, (time.perf_counter() - float(state["started"])) * 1000.0)
        _method, _queue, job = _job_parts(args, kwargs)
        # Hook callbacks may not pass the original job object to after_job.
        method = state["method"]
        queue = state["queue"]
        failed = _job_failed(args, kwargs, job)
        prefix = _prefix(frappe)
        minute, hour, _ = _site_now()
        bucket = f"{prefix}:minute:{minute}"
        cumulative = f"{prefix}:cumulative"
        pipe = _redis(frappe).pipeline(transaction=False)
        for key, field, amount in (
            (bucket, "jobs_count", 1),
            (bucket, "jobs_failed", 1 if failed else 0),
            (bucket, "jobs_duration_ms_sum", elapsed_ms),
            (bucket, f"queue_{queue}_jobs", 1),
            (cumulative, "jobs_count", 1),
            (cumulative, "jobs_failed", 1 if failed else 0),
            (cumulative, "jobs_duration_ms_sum", elapsed_ms),
        ):
            if isinstance(amount, float):
                pipe.hincrbyfloat(key, field, amount)
            else:
                pipe.hincrby(key, field, amount)
        # Per-method views use capped hourly sorted sets instead of dynamic
        # metric fields, bounding cardinality while retaining the busiest jobs.
        count_key = f"{prefix}:jobtop:{hour}:count"
        duration_key = f"{prefix}:jobtop:{hour}:duration_ms_sum"
        failure_key = f"{prefix}:jobtop:{hour}:failures"
        _increment_top(pipe, count_key, method, 1)
        _increment_top(pipe, duration_key, method, elapsed_ms)
        if failed:
            _increment_top(pipe, failure_key, method, 1)
        _expiry(pipe, [bucket, cumulative])
        pipe.execute()
    except Exception:
        _record_collector_error(frappe)
