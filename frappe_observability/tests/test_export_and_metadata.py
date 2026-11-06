import hashlib
import json
import sys
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from frappe_observability import api


ROOT = Path(__file__).resolve().parents[2]


class FakeRedis:
    def __init__(self, values=None):
        self.values = values or {}

    def hgetall(self, key):
        if key.endswith(":cumulative"):
            return self.values
        return {}


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class ExportTests(unittest.TestCase):
    def make_frappe(self, *, token="scrape-secret", mode="both", allowlist=None, cidrs="127.0.0.1/32"):
        digest = hashlib.sha256(token.encode()).hexdigest()
        settings = FakeSettings({
            "export_token_sha256": digest,
            "mode": mode,
            "metric_allowlist": allowlist or "frappe_obs_requests_total,frappe_obs_job_failures_total",
            "export_cidrs": cidrs,
        })
        redis = FakeRedis({b"requests": b"12", b"jobs_failed": b"2"})
        cache = types.SimpleNamespace(redis=redis)
        request = types.SimpleNamespace(
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
            remote_addr="127.0.0.1",
        )
        local = types.SimpleNamespace(site="example.test", request=request, response={})
        fake = types.ModuleType("frappe")
        fake.local = local
        fake.session = types.SimpleNamespace(user="Administrator")
        fake.conf = {}
        fake.cache = lambda: cache
        fake.get_cached_doc = lambda *_args, **_kwargs: settings
        fake.get_roles = lambda _user: ["System Manager"]
        fake.whitelist = lambda **_kwargs: (lambda function: function)
        fake.throw = lambda message, *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(message))
        return fake

    def test_exports_only_allowlisted_aggregate_counters(self):
        fake = self.make_frappe()
        with patch.dict(sys.modules, {"frappe": fake}):
            body = api.metrics()
        self.assertIn("frappe_obs_requests_total 12", body)
        self.assertIn("frappe_obs_job_failures_total 2", body)
        self.assertEqual(fake.local.response["http_status_code"], 200)
        self.assertEqual(fake.local.response["content_type"], api.PROMETHEUS_CONTENT_TYPE)

    def test_rejects_bad_token_and_cidr(self):
        fake = self.make_frappe(token="real-secret")
        fake.local.request.headers = {"Authorization": "Bearer wrong"}
        with patch.dict(sys.modules, {"frappe": fake}):
            api.metrics()
        self.assertEqual(fake.local.response["http_status_code"], 401)

        fake = self.make_frappe(cidrs="10.0.0.0/8")
        with patch.dict(sys.modules, {"frappe": fake}):
            api.metrics()
        self.assertEqual(fake.local.response["http_status_code"], 403)

    def test_export_off_by_default(self):
        fake = self.make_frappe(mode="internal")
        with patch.dict(sys.modules, {"frappe": fake}):
            api.metrics()
        self.assertEqual(fake.local.response["http_status_code"], 403)


class MetadataTests(unittest.TestCase):
    def test_doctype_json_is_parseable_and_targets_current_develop_compatibility(self):
        for path in (ROOT / "frappe_observability").rglob("*.json"):
            with self.subTest(path=path):
                json.loads(path.read_text(encoding="utf-8"))
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["tool"]["bench"]["frappe-dependencies"]["frappe"], ">=17.0.0-dev,<18.0.0")
        self.assertEqual(config["project"]["requires-python"], ">=3.14,<3.15")
        self.assertIn("psutil>=5.9", config["project"]["dependencies"])

    def test_scheduler_and_lifecycle_hooks_resolve_to_app_functions(self):
        from frappe_observability import hooks

        self.assertTrue(hooks.before_request)
        self.assertTrue(hooks.after_request)
        self.assertTrue(hooks.before_job)
        self.assertTrue(hooks.after_job)
        self.assertEqual(hooks.scheduler_events["cron"]["* * * * *"], ["frappe_observability.scheduled_tasks.run_minute"])
        self.assertEqual(hooks.scheduler_events["hourly"], ["frappe_observability.scheduled_tasks.run_hourly"])
        self.assertEqual(hooks.scheduler_events["daily"], ["frappe_observability.scheduled_tasks.prune_metrics"])
        scheduled = [path for event in hooks.scheduler_events.values()
                     for paths in (event.values() if isinstance(event, dict) else [event])
                     for path in (paths if isinstance(paths, list) else [paths])]
        install_hooks = [hooks.after_install, hooks.after_migrate]
        for dotted_path in scheduled + install_hooks:
            module, function = dotted_path.rsplit(".", 1)
            imported = __import__(module, fromlist=[function])
            self.assertTrue(callable(getattr(imported, function)))
        for group in ("before_request", "after_request", "before_job", "after_job"):
            for dotted_path in getattr(hooks, group):
                module, function = dotted_path.rsplit(".", 1)
                imported = __import__(module, fromlist=[function])
                self.assertTrue(callable(getattr(imported, function)))

    def test_desk_page_is_shipped_and_linked_with_roles(self):
        from frappe_observability.config.desktop import get_data

        page = json.loads((ROOT / "frappe_observability/observability/page/observability_dashboard/observability_dashboard.json").read_text())
        self.assertEqual(page["name"], "observability-dashboard")
        self.assertEqual({item["role"] for item in page["roles"]}, {"Observability Viewer", "Observability Admin", "System Manager"})
        items = get_data()[0]["items"]
        self.assertTrue(any(item.get("type") == "page" and item.get("name") == page["name"] for item in items))


if __name__ == "__main__":
    unittest.main()
