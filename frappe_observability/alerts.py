"""Periodic alert evaluation for Frappe Observability.

Call ``frappe_observability.alerts.evaluate_alerts`` from the site's scheduler.
Rules may be stored in the ``Alert Rule`` DocType or supplied through the
``observability_alert_rules`` site-config list. If neither is available, the
seedable templates below are used. A rule's metric is optional at runtime: an
absent Redis counter is skipped, never treated as zero or as an error.

Counters use the exporter contract in ``frappe_observability.api``:
``frappe_observability:metrics:v1:<metric_name>`` is a Redis HASH whose values
are cumulative non-negative numeric counts. All label series are summed for
threshold evaluation.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta

from frappe_observability.api import _FIELDS, _GAUGES
from frappe_observability.collectors import _prefix, _redis


RULE_DOCTYPE = "Alert Rule"
EVENT_DOCTYPE = "Alert Event"
SETTINGS_DOCTYPE = "Observability Settings"

# Seedable starting points, deliberately tied to the shared counter contract.
# They are considered only when no persisted or site-configured rules exist;
# missing metrics are ignored until corresponding telemetry is present.
DEFAULT_ALERT_RULES = (
    {
        "name": "Root disk warning",
        "enabled": 1,
        "metric_name": "frappe_obs_host_root_disk_percent",
        "operator": ">",
        "threshold": 85,
        "severity": "Warning",
        "cooldown_minutes": 30,
        "message": "Root filesystem usage exceeded 85%.",
    },
    {
        "name": "Root disk critical",
        "enabled": 1,
        "metric_name": "frappe_obs_host_root_disk_percent",
        "operator": ">",
        "threshold": 95,
        "severity": "Critical",
        "cooldown_minutes": 30,
        "message": "Root filesystem usage exceeded 95%.",
    },
    {
        "name": "Backup older than 48 hours",
        "enabled": 1,
        "metric_name": "frappe_obs_backup_age_hours",
        "operator": ">",
        "threshold": 48,
        "severity": "Critical",
        "cooldown_minutes": 60,
        "message": "The newest site backup is older than 48 hours.",
    },
    {
        "name": "RQ queue backlog",
        "enabled": 1,
        "metric_name": "frappe_obs_queue_depth_total",
        "operator": ">",
        "threshold": 100,
        "severity": "Warning",
        "cooldown_minutes": 15,
        "message": "Background queue depth exceeded 100 jobs.",
    },
    {
        "name": "HTTP error rate",
        "enabled": 1,
        "metric_name": "frappe_obs_request_error_rate_percent",
        "operator": ">",
        "threshold": 5,
        "severity": "Warning",
        "cooldown_minutes": 15,
        "message": "The current-minute HTTP 4xx/5xx rate exceeded 5%.",
    },
    {
        "name": "Database connection pressure",
        "enabled": 1,
        "metric_name": "frappe_obs_database_connections_percent",
        "operator": ">",
        "threshold": 80,
        "severity": "Warning",
        "cooldown_minutes": 15,
        "message": "MariaDB connections exceeded 80% of max_connections.",
    },
    {
        "name": "Redis memory pressure",
        "enabled": 1,
        "metric_name": "frappe_obs_redis_memory_percent",
        "operator": ">",
        "threshold": 90,
        "severity": "Critical",
        "cooldown_minutes": 15,
        "message": "Redis memory exceeded 90% of configured maxmemory.",
    },
    {
        "name": "Queue Redis memory pressure",
        "enabled": 1,
        "metric_name": "frappe_obs_redis_queue_memory_percent",
        "operator": ">",
        "threshold": 90,
        "severity": "Critical",
        "cooldown_minutes": 15,
        "message": "RQ Redis memory exceeded 90% of configured maxmemory.",
    },
    {
        "name": "Stale database flush",
        "enabled": 1,
        "metric_name": "frappe_obs_flush_age_seconds",
        "operator": ">",
        "threshold": 180,
        "severity": "Warning",
        "cooldown_minutes": 15,
        "message": "The last telemetry flush is more than 180 seconds old.",
    },
    {
        "name": "Log directory growth",
        "enabled": 1,
        "metric_name": "frappe_obs_log_directory_bytes",
        "operator": ">",
        "threshold": 10737418240,
        "severity": "Critical",
        "cooldown_minutes": 60,
        "message": "Site and bench log directories exceed 10 GiB.",
    },
    {
        "name": "Frappe error log rate",
        "enabled": 1,
        "metric_name": "frappe_obs_error_logs_per_minute",
        "operator": ">",
        "threshold": 20,
        "severity": "Warning",
        "cooldown_minutes": 15,
        "message": "More than 20 Frappe Error Log records were created in the last minute.",
    },
)

_RULE_FIELD_CANDIDATES = {
    "metric": ("metric_name", "metric", "counter_name"),
    "operator": ("operator", "comparison", "condition"),
    "threshold": ("threshold", "threshold_value"),
    "enabled": ("enabled", "is_enabled", "active"),
    "cooldown": ("cooldown_minutes", "cooldown"),
    "window": ("window_minutes",),
    "severity": ("severity", "priority"),
    "message": ("message", "description"),
    "recipients": ("notification_recipients", "recipients", "email_recipients", "notification_email"),
}
_EVENT_FIELD_CANDIDATES = {
    "rule": ("alert_rule", "rule", "rule_name"),
    "metric": ("metric_name", "metric"),
    "value": ("current_value", "metric_value", "value"),
    "threshold": ("threshold", "threshold_value"),
    "severity": ("severity", "priority"),
    "message": ("message", "description"),
    "status": ("status", "state"),
    "acknowledged": ("acknowledged", "is_acknowledged"),
    "last_notified": ("last_notified", "last_notification_on"),
    "resolved": ("resolved_on", "resolved_at", "recovered_on"),
    "raised": ("raised_on", "fired_at", "triggered_on"),
}


def _doctype_meta(frappe, doctype):
    try:
        meta = frappe.get_meta(doctype)
        fields = {field.fieldname: field for field in getattr(meta, "fields", []) if getattr(field, "fieldname", None)}
        return meta, fields
    except Exception:
        return None, {}


def _first_field(fields, candidates):
    return next((name for name in candidates if name in fields), None)


def _row_value(row, fields, key, default=None):
    fieldname = _first_field(fields, _RULE_FIELD_CANDIDATES.get(key, ()))
    return row.get(fieldname, default) if fieldname else default


def _enabled(value):
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "no", "false", "off", "disabled"}
    return bool(value)


def _normalise_rule(row, fields=None):
    fields = fields or set(row.keys())
    name = row.get("name") or row.get("rule_name")
    metric = _row_value(row, fields, "metric")
    if not isinstance(metric, str) or metric not in (_FIELDS | _GAUGES):
        return None
    try:
        threshold = float(_row_value(row, fields, "threshold"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(threshold) or threshold < 0:
        return None
    operator = str(_row_value(row, fields, "operator", ">=") or ">=").strip().lower()
    operator_aliases = {"gt": ">", "greater than": ">", "gte": ">=", "greater than or equal": ">=", "lt": "<", "less than": "<", "lte": "<=", "less than or equal": "<=", "eq": "==", "equal": "==", "ne": "!="}
    operator = operator_aliases.get(operator, operator)
    if operator not in {">", ">=", "<", "<=", "==", "!="}:
        return None
    enabled_value = _row_value(row, fields, "enabled", 1)
    if not _enabled(enabled_value):
        return None
    cooldown = _row_value(row, fields, "cooldown", 30)
    try:
        cooldown = max(0, min(int(cooldown or 0), 7 * 24 * 60))
    except (TypeError, ValueError):
        cooldown = 30
    try:
        window = max(1, min(int(_row_value(row, fields, "window", 5) or 5), 24 * 60))
    except (TypeError, ValueError):
        window = 5
    recipients = _row_value(row, fields, "recipients", None)
    if isinstance(recipients, str):
        recipients = [item.strip() for item in re.split(r"[,;\n]", recipients) if item.strip()]
    elif isinstance(recipients, (list, tuple, set)):
        recipients = [str(item).strip() for item in recipients if str(item).strip()]
    else:
        recipients = []
    return {
        "name": str(name or metric),
        "metric": metric,
        "operator": operator,
        "threshold": threshold,
        "severity": str(_row_value(row, fields, "severity", "warning") or "warning")[:32],
        "cooldown_minutes": cooldown,
        "window_minutes": window,
        "message": str(_row_value(row, fields, "message", "") or "")[:1000],
        "recipients": recipients,
    }


def _config(frappe, key, default=None):
    try:
        config = getattr(frappe, "conf", None)
        return config.get(key, default) if config is not None else default
    except Exception:
        return default


def _get_settings(frappe):
    """Read optional singleton settings, falling back to non-secret site config."""
    values = {}
    try:
        settings = frappe.get_single(SETTINGS_DOCTYPE)
        for key in ("alerts_enabled", "enable_alerts", "notification_recipients", "alert_cooldown_minutes", "flush_interval_seconds"):
            if hasattr(settings, key):
                values[key] = getattr(settings, key)
    except Exception:
        pass
    if not values:
        values = {
            "alerts_enabled": _config(frappe, "observability_alerts_enabled", True),
            "notification_recipients": _config(frappe, "observability_alert_recipients", None),
            "alert_cooldown_minutes": _config(frappe, "observability_alert_cooldown_minutes", None),
            "flush_interval_seconds": _config(frappe, "observability_flush_interval_seconds", 60),
        }
    enabled = values.get("alerts_enabled", values.get("enable_alerts", True))
    if not _enabled(enabled):
        return {"enabled": False, "recipients": [], "cooldown_minutes": None, "flush_interval_seconds": 60}
    recipients = values.get("notification_recipients")
    if isinstance(recipients, str):
        recipients = [item.strip() for item in re.split(r"[,;\n]", recipients) if item.strip()]
    elif isinstance(recipients, (list, tuple, set)):
        recipients = [str(item).strip() for item in recipients if str(item).strip()]
    else:
        recipients = []
    try:
        cooldown = max(0, min(int(values.get("alert_cooldown_minutes")), 7 * 24 * 60)) if values.get("alert_cooldown_minutes") is not None else None
    except (TypeError, ValueError):
        cooldown = None
    try:
        interval = min(3600, max(60, int(values.get("flush_interval_seconds") or 60)))
    except (TypeError, ValueError):
        interval = 60
    return {"enabled": True, "recipients": recipients, "cooldown_minutes": cooldown,
            "flush_interval_seconds": interval}


def _effective_threshold(rule, settings):
    threshold = rule["threshold"]
    if rule["metric"] == "frappe_obs_flush_age_seconds":
        threshold = max(threshold, float(settings.get("flush_interval_seconds", 60)) * 2)
    return threshold


def _load_rules(frappe):
    meta, fields = _doctype_meta(frappe, RULE_DOCTYPE)
    if meta is not None:
        try:
            rows = frappe.get_all(RULE_DOCTYPE, fields=["name"] + list(fields.keys()), order_by="modified asc", limit_page_length=1000)
            if rows:
                rules = [_normalise_rule(row, set(fields) | {"name"}) for row in rows]
                return [rule for rule in rules if rule]
        except Exception:
            pass
    configured = _config(frappe, "observability_alert_rules", None)
    if isinstance(configured, (list, tuple)) and configured:
        rules = [_normalise_rule(row, set(row.keys())) for row in configured if isinstance(row, Mapping)]
        return [rule for rule in rules if rule]
    return [rule for template in DEFAULT_ALERT_RULES if (rule := _normalise_rule(template, set(template.keys())))]


def _now(frappe):
    try:
        from frappe.utils import now_datetime

        return now_datetime()
    except Exception:
        return datetime.utcnow()


def _read_metric_total(frappe, metric, window_minutes=5):
    """Return a Redis counter window or live gauge; absent data is not zero."""
    metric_info = _FIELDS.get(metric)
    is_counter = metric_info is not None
    if metric_info is None:
        metric_info = _GAUGES.get(metric)
    if not metric_info:
        return False, None
    field = metric_info[0]
    try:
        redis = _redis(frappe)
        if is_counter:
            try:
                window_minutes = max(1, min(int(window_minutes), 24 * 60))
            except (TypeError, ValueError):
                window_minutes = 5
            now_minute = int(datetime.now().timestamp()) // 60
            prefix = _prefix(frappe)
            base_field = {
                "requests": "requests", "errors": "requests", "bytes_in": "requests", "bytes_out": "requests",
                "jobs_count": "jobs_count", "jobs_failed": "jobs_count", "jobs_duration_ms_sum": "jobs_count",
                "collector_errors": "collector_errors",
            }.get(field, field)
            pipe = redis.pipeline(transaction=False)
            for minute in range(max(0, now_minute - window_minutes + 1), now_minute + 1):
                key = f"{prefix}:minute:{minute}"
                pipe.hget(key, field)
                pipe.hget(key, base_field)
            values = pipe.execute()
            total = 0.0
            present = False
            for index in range(0, len(values), 2):
                try:
                    observed = float(values[index + 1] or 0)
                    value = float(values[index] or 0)
                except (TypeError, ValueError, UnicodeError):
                    continue
                if observed > 0:
                    present = True
                    total += value
            if not present:
                return False, None
            raw = total
        else:
            raw = redis.hget(f"{_prefix(frappe)}:current_sample", field)
        if raw is None:
            return False, None
        total = float(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError, UnicodeError, OSError):
        return False, None
    except Exception:
        return False, None
    if not math.isfinite(total) or total < 0:
        return False, None
    return True, total


def _breached(value, operator, threshold):
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "==": value == threshold,
        "!=": value != threshold,
    }[operator]


def _event_fields(frappe):
    meta, fields = _doctype_meta(frappe, EVENT_DOCTYPE)
    return meta, fields


def _event_active(row, fields):
    status_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["status"])
    status = str(row.get(status_field, "") if status_field else "").strip().lower()
    if status:
        return status in {"open", "active", "triggered", "acknowledged", "unresolved", "pending"}
    resolved_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["resolved"])
    return not bool(row.get(resolved_field)) if resolved_field else True


def _event_acknowledged(row, fields):
    ack_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["acknowledged"])
    status_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["status"])
    status = str(row.get(status_field, "") if status_field else "").strip().lower()
    return _enabled(row.get(ack_field)) if ack_field else status in {"acknowledged", "acknowledged by user"}


def _event_rule_name(row, fields):
    field = _first_field(fields, _EVENT_FIELD_CANDIDATES["rule"])
    return str(row.get(field) or "") if field else ""


def _load_active_events(frappe, fields):
    if not fields:
        return {}
    selected = ["name"]
    for candidates in _EVENT_FIELD_CANDIDATES.values():
        field = _first_field(fields, candidates)
        if field and field not in selected:
            selected.append(field)
    if "creation" in fields:
        selected.append("creation")
    try:
        rows = frappe.get_all(EVENT_DOCTYPE, fields=selected, order_by="creation desc", limit_page_length=2000)
    except Exception:
        return {}
    active = {}
    for row in rows:
        rule_name = _event_rule_name(row, fields)
        if rule_name and rule_name not in active and _event_active(row, fields):
            active[rule_name] = row
    return active


def _event_doc_values(rule, current_value, timestamp, fields, settings):
    values = {"doctype": EVENT_DOCTYPE}
    assigned = {
        "rule": rule["name"],
        "metric": rule["metric"],
        "value": current_value,
        "threshold": rule["threshold"],
        "severity": rule["severity"],
        "message": rule["message"] or f"{rule['metric']} {rule['operator']} {rule['threshold']} (current {current_value:g})",
        "status": "Open",
        "acknowledged": 0,
        "raised": timestamp,
    }
    for key, value in assigned.items():
        field = _first_field(fields, _EVENT_FIELD_CANDIDATES[key])
        if field:
            values[field] = value
    return values


def _insert_event(frappe, rule, current_value, timestamp, fields, settings):
    values = _event_doc_values(rule, current_value, timestamp, fields, settings)
    # Use normal document insertion so naming, validation and installed app hooks
    # run correctly; active events were fetched in one batched read above.
    try:
        doc = frappe.get_doc(values)
        doc.insert(ignore_permissions=True)
        return getattr(doc, "name", None)
    except Exception:
        return None


def _set_event_value(frappe, event_name, field, value):
    if not event_name or not field:
        return False
    try:
        frappe.db.set_value(EVENT_DOCTYPE, event_name, field, value, update_modified=False)
        return True
    except Exception:
        return False


def _recipients(rule, settings):
    return rule["recipients"] or settings["recipients"]


def _notify(frappe, rule, value, recipients):
    if not recipients:
        return False
    sendmail = getattr(frappe, "sendmail", None)
    if not callable(sendmail):
        return False
    title = f"[{rule['severity'].upper()}] Frappe observability alert: {rule['name']}"
    detail = rule["message"] or f"{rule['metric']} {rule['operator']} {rule['threshold']} (current value: {value:g})"
    safe_detail = html.escape(detail)
    safe_metric = html.escape(rule["metric"])
    safe_rule = html.escape(rule["name"])
    body = (
        f"<p><strong>Alert:</strong> {safe_rule}</p>"
        f"<p><strong>Metric:</strong> {safe_metric}</p>"
        f"<p><strong>Observed:</strong> {value:g} &nbsp; <strong>Condition:</strong> "
        f"{html.escape(rule['operator'])} {rule['threshold']:g}</p><p>{safe_detail}</p>"
    )
    try:
        sendmail(recipients=recipients, subject=title, message=body, delayed=False)
        return True
    except Exception:
        return False


def _elapsed_minutes(now, previous):
    if not previous:
        return None
    try:
        delta = now - previous
        return delta.total_seconds() / 60.0
    except Exception:
        return None


def _effective_cooldown(rule, settings):
    return settings["cooldown_minutes"] if settings["cooldown_minutes"] is not None else rule["cooldown_minutes"]


def _resolve_event(frappe, event, fields, timestamp):
    name = event.get("name")
    status_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["status"])
    resolved_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["resolved"])
    if status_field:
        _set_event_value(frappe, name, status_field, "Resolved")
    if resolved_field:
        _set_event_value(frappe, name, resolved_field, timestamp)


def evaluate_alerts():
    """Evaluate configured rules; safe to invoke periodically from the scheduler.

    Returns a small result summary for operator-initiated calls. Each rule is
    isolated so Redis, email, or DocType failures do not abort a worker job.
    """
    import frappe

    settings = _get_settings(frappe)
    if not settings["enabled"]:
        return {"evaluated": 0, "triggered": 0, "skipped": 0}

    rules = _load_rules(frappe)
    _meta, fields = _event_fields(frappe)
    if not fields:
        return {"evaluated": 0, "triggered": 0, "skipped": len(rules)}
    active_events = _load_active_events(frappe, fields)
    timestamp = _now(frappe)
    summary = {"evaluated": 0, "triggered": 0, "skipped": 0}

    for rule in rules:
        try:
            present, current_value = _read_metric_total(frappe, rule["metric"], rule.get("window_minutes", 5))
            if not present:
                summary["skipped"] += 1
                continue
            summary["evaluated"] += 1
            threshold = _effective_threshold(rule, settings)
            effective_rule = rule if threshold == rule["threshold"] else {**rule, "threshold": threshold}
            if (threshold != rule["threshold"] and rule["name"] == "Stale database flush"
                    and rule["message"] in {"The last telemetry flush is more than three minutes old.",
                                             "The last telemetry flush is more than 180 seconds old."}):
                effective_rule["message"] = f"The last telemetry flush exceeded its interval-aware threshold ({threshold:g} seconds)."
            event = active_events.get(rule["name"])
            if not _breached(current_value, rule["operator"], threshold):
                if event:
                    _resolve_event(frappe, event, fields, timestamp)
                    active_events.pop(rule["name"], None)
                continue

            summary["triggered"] += 1
            if event:
                _set_event_value(frappe, event.get("name"), _first_field(fields, _EVENT_FIELD_CANDIDATES["value"]), current_value)
                _set_event_value(frappe, event.get("name"), _first_field(fields, _EVENT_FIELD_CANDIDATES["threshold"]), threshold)
                _set_event_value(frappe, event.get("name"), _first_field(fields, _EVENT_FIELD_CANDIDATES["message"]), effective_rule["message"])
            if event is None:
                event_name = _insert_event(frappe, effective_rule, current_value, timestamp, fields, settings)
                if not event_name:
                    summary["skipped"] += 1
                    continue
                event = {"name": event_name, "creation": timestamp}
                rule_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["rule"])
                if rule_field:
                    event[rule_field] = rule["name"]
                active_events[rule["name"]] = event
                elapsed = None
            else:
                if _event_acknowledged(event, fields):
                    continue
                notified_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["last_notified"])
                prior = event.get(notified_field) if notified_field else None
                if prior is None:
                    prior = event.get("creation")
                elapsed = _elapsed_minutes(timestamp, prior)
                cooldown = _effective_cooldown(rule, settings)
                if elapsed is not None and elapsed < cooldown:
                    continue

            sent = _notify(frappe, effective_rule, current_value, _recipients(rule, settings))
            if sent:
                notified_field = _first_field(fields, _EVENT_FIELD_CANDIDATES["last_notified"])
                _set_event_value(frappe, event.get("name"), notified_field, timestamp)
                if notified_field:
                    event[notified_field] = timestamp
        except Exception:
            # Alerting is best effort; no single malformed rule should break a worker.
            summary["skipped"] += 1
            continue

    return summary
