"""Least-privilege roles used by Frappe Observability."""

VIEWER_ROLE = "Observability Viewer"
ADMIN_ROLE = "Observability Admin"
CUSTOM_ROLES = (VIEWER_ROLE, ADMIN_ROLE)


def ensure_roles():
    """Create the app's Desk roles after installation, if needed."""
    try:
        import frappe
    except ImportError:
        return False

    for role_name in CUSTOM_ROLES:
        if not frappe.db.exists("Role", role_name):
            frappe.get_doc(
                {"doctype": "Role", "role_name": role_name, "desk_access": 1}
            ).insert(ignore_permissions=True)
    return True
