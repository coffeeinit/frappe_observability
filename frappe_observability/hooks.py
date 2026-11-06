"""Frappe app metadata, lifecycle hooks, and periodic workers."""

app_name = "frappe_observability"
app_title = "Frappe Observability"
app_publisher = "Frappe Observability Contributors"
app_description = "Observability data models and Desk tools for Frappe"
app_email = ""
app_license = "MIT"

# Framework lifecycle hooks are confirmed in the upstream Frappe develop branch.
before_request = ["frappe_observability.telemetry.before_request"]
after_request = ["frappe_observability.telemetry.after_request"]
before_job = ["frappe_observability.telemetry.before_job"]
after_job = ["frappe_observability.telemetry.after_job"]

# Frappe's `all` event defaults to every four minutes; cron provides the
# required minute cadence. Pruning remains separate from the regular worker.
scheduler_events = {
    "cron": {"* * * * *": ["frappe_observability.scheduled_tasks.run_minute"]},
    "hourly": ["frappe_observability.scheduled_tasks.run_hourly"],
    "daily": ["frappe_observability.scheduled_tasks.prune_metrics"],
}

after_install = "frappe_observability.install.after_install"
after_migrate = "frappe_observability.install.after_migrate"
