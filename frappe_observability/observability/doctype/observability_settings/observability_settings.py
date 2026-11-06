"""Validation for the singleton observability configuration."""

import ipaddress
import json
import re

import frappe
from frappe.model.document import Document


class ObservabilitySettings(Document):
    def validate(self):
        self._validate_numbers()
        self._validate_export()
        self._validate_log_configuration()

    def _validate_numbers(self):
        retention = int(self.retention_days if self.retention_days is not None else 7)
        if not 1 <= retention <= 30:
            frappe.throw("Retention must be between 1 and 30 days.")
        self.retention_days = retention

        interval = int(self.flush_interval_seconds if self.flush_interval_seconds is not None else 60)
        if not 60 <= interval <= 3600:
            frappe.throw("The collection/flush interval must be between 60 and 3600 seconds.")
        self.flush_interval_seconds = interval

        threshold = float(self.download_confirm_threshold if self.download_confirm_threshold is not None else 50)
        if threshold < 0.1 or threshold > 10240:
            frappe.throw("The large-log download confirmation threshold must be between 0.1 and 10240 MB.")

    def _validate_export(self):
        if str(self.mode or "internal") not in {"internal", "external", "both"}:
            frappe.throw("Mode must be internal, external, or both.")
        if str(self.ip_storage or "masked") not in {"masked", "full"}:
            frappe.throw("IP storage must be masked or full.")
        if self.export_token_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", str(self.export_token_sha256).strip()):
            frappe.throw("The Prometheus token field must contain a SHA-256 hex digest, not a plaintext token.")
        cidrs = [part.strip() for part in re.split(r"[,;\n]", str(self.export_cidrs or "")) if part.strip()]
        try:
            for cidr in cidrs:
                ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            frappe.throw("Prometheus CIDR allowlist contains an invalid network.")
        if self.metric_allowlist:
            from frappe_observability.api import _FIELDS, _GAUGES

            names = [part.strip() for part in re.split(r"[,;\n]", str(self.metric_allowlist)) if part.strip()]
            unsupported = sorted(set(names) - (_FIELDS.keys() | _GAUGES.keys()))
            if unsupported:
                frappe.throw("Unsupported metric(s) in allowlist: " + ", ".join(unsupported[:10]))
            self.metric_allowlist = "\n".join(dict.fromkeys(names))

    def _validate_log_configuration(self):
        if self.log_roots:
            try:
                roots = json.loads(self.log_roots)
            except (TypeError, ValueError):
                frappe.throw("Additional log roots must be valid JSON.")
            if not isinstance(roots, list) or len(roots) > 20:
                frappe.throw("Additional log roots must be a JSON list of at most 20 entries.")
            for item in roots:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].startswith("/"):
                    frappe.throw("Each additional log root must have an absolute path and a scope.")
                if item.get("scope") not in {"site", "bench"}:
                    frappe.throw("Log-root scope must be site or bench.")
        if self.redaction_patterns:
            try:
                patterns = json.loads(self.redaction_patterns)
            except (TypeError, ValueError):
                frappe.throw("Additional redaction patterns must be valid JSON.")
            if not isinstance(patterns, list) or len(patterns) > 50:
                frappe.throw("Additional redaction patterns must be a JSON list of at most 50 entries.")
            try:
                for pattern in patterns:
                    if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
                        frappe.throw("Each additional redaction pattern must be a non-empty string of at most 512 characters.")
                    re.compile(pattern)
            except re.error:
                frappe.throw("An additional redaction pattern is not a valid regular expression.")
