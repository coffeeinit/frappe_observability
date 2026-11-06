"""Installation helpers for Frappe Observability."""


def ensure_settings():
    import frappe

    if not frappe.db.exists("Observability Settings", "Observability Settings"):
        frappe.get_doc({"doctype": "Observability Settings"}).insert(ignore_permissions=True)


def ensure_default_alert_rules():
    """Install defaults once; never overwrite a rule edited by an operator."""
    import frappe
    from .alerts import DEFAULT_ALERT_RULES

    if not frappe.db.exists("DocType", "Alert Rule"):
        return
    for rule in DEFAULT_ALERT_RULES:
        title = str(rule["name"])
        if frappe.db.exists("Alert Rule", title):
            continue
        try:
            frappe.get_doc({
                "doctype": "Alert Rule",
                "title": title,
                "enabled": rule.get("enabled", 1),
                "metric_name": rule["metric_name"],
                "operator": rule.get("operator", ">="),
                "threshold": rule["threshold"],
                "window_minutes": rule.get("window_minutes", 1),
                "cooldown_minutes": rule.get("cooldown_minutes", 30),
                "severity": rule.get("severity", "Warning"),
                "message": rule.get("message", ""),
                "description": rule.get("message", ""),
            }).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(title=f"Observability default alert rule failed: {title}", message=frappe.get_traceback())


def after_install():
    """Create app roles/settings and a useful first set of editable alerts."""
    from frappe_observability.permissions import ensure_roles

    ensure_roles()
    ensure_settings()
    ensure_default_alert_rules()


def after_migrate():
    """Idempotently restore app-owned defaults after framework migration."""
    from frappe_observability.permissions import ensure_roles

    ensure_roles()
    ensure_settings()
    ensure_default_alert_rules()
