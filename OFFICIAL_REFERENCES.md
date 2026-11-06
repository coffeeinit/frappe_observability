# Official Frappe references used for implementation

- Frappe Framework GitHub repository: https://github.com/frappe/frappe — the repository reports `develop` as its default branch. Its current `develop/pyproject.toml` requires Python `>=3.14,<3.15`.
- ERPNext GitHub repository: https://github.com/frappe/erpnext — the repository reports `develop` as its default branch. Its current `develop/pyproject.toml` declares Frappe compatibility `>=17.0.0-dev,<18.0.0`.
- Hooks: https://docs.frappe.io/framework/user/en/python-api/hooks — official request hooks, background-job hooks, scheduler events, and Desk extension mechanisms.
- Workspaces: https://docs.frappe.io/framework/user/en/desk/navigation/workspace — app-shipped workspaces are standard records, can contain charts and number cards, and must respect role visibility and block permissions.
- Database API: https://docs.frappe.io/framework/user/en/api/database — `get_list` applies permissions; `get_all` bypasses them; `bulk_insert` is available for batched writes; `db.delete` supports filter-based deletion.
- Background jobs: https://docs.frappe.io/framework/user/en/api/background_jobs — recurring work uses `scheduler_events`; the `all` scheduler event is the framework's frequent event; queue names and worker behavior are documented.
- Chart API: https://docs.frappe.io/framework/user/en/api/chart — Frappe exposes SVG chart support for Desk/client-side pages, including real-time chart integrations.
- Chart guide: https://docs.frappe.io/framework/user/en/guides/desk/making_charts — Frappe Charts supports common line, bar, and percentage chart types.

## Design decision

The monitoring UI and charts belong inside this single Frappe app's Desk. No separate Grafana deployment or Grafana dashboard package is part of the requested target. Prometheus export remains an optional integration, not a dependency for viewing history or charts in Desk.

## Additional source checks (2026-11-06)

The current `frappe.db.get_list` reference documents version-16+ aggregate fields such as `[{"SUM": "sum", "as": "value"}]`, grouping, and user-permission filtering; `get_all` bypasses those permissions. The dashboard therefore uses fixed, parameterized, site-local time-series aggregation after its explicit role check, and permission-aware `get_list` for top-usage rows: https://docs.frappe.io/framework/user/en/api/database

The current `frappe/database/database.py` source documents `bulk_insert(doctype, fields, values, ignore_duplicates=False, *, chunk_size=1000)`. MariaDB uses an ignore-on-conflict insert when `ignore_duplicates=True`, enabling a failed flush to be retried without duplicating deterministic sample rows: https://github.com/frappe/frappe/blob/develop/frappe/database/database.py

The current `frappe/app.py` source calls registered `after_request` hooks with keyword arguments `response` and `request` and returns the original response, which is the app's hook contract: https://github.com/frappe/frappe/blob/develop/frappe/app.py

Frappe Charts documents `new frappe.Chart(element, {title, data: {labels, datasets}, type, height, colors})`; this is the widget used by the Desk page: https://frappe.io/charts/docs

A read of the current Frappe `develop` core DocType tree found `Access Log` but no stable core `Login Attempt` record for authentication failures. The app reports this as unsupported rather than deriving an unreliable count from error-log text: https://api.github.com/repos/frappe/frappe/contents/frappe/core/doctype?ref=develop
