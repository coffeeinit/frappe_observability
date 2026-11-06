"""Desk module navigation for Frappe Observability."""


def get_data():
    return [
        {
            "module_name": "Observability",
            "type": "module",
            "label": "Observability",
            "icon": "octicon octicon-pulse",
            "color": "blue",
            "description": "Metrics, logs, and alert operations.",
            "items": [
                {"type": "page", "name": "observability-dashboard", "label": "Overview"},
                {"type": "doctype", "name": "Metric Bucket", "label": "Metric Buckets"},
                {"type": "doctype", "name": "Usage Top", "label": "Usage Top"},
                {"type": "doctype", "name": "Alert Rule", "label": "Alert Rules"},
                {"type": "doctype", "name": "Alert Event", "label": "Alert Events"},
                {"type": "doctype", "name": "Log Access Entry", "label": "Log Access Audit"},
                {"type": "doctype", "name": "Observability Settings", "label": "Settings"},
            ],
        }
    ]
