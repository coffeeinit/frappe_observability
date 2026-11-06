import types
import unittest

from frappe_observability import collectors


class FakePipeline:
    def __init__(self):
        self.commands = []
        self.executed = 0

    def __getattr__(self, name):
        def add(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self
        return add

    def execute(self):
        self.executed += 1
        return []


class FakeRedis:
    def __init__(self):
        self.pipeline_calls = 0
        self.pipe = FakePipeline()

    def pipeline(self, transaction=False):
        self.pipeline_calls += 1
        return self.pipe


class CollectorTests(unittest.TestCase):
    def test_endpoint_labels_are_bounded(self):
        self.assertEqual(collectors._bounded_endpoint("/api/method/custom.run?x=secret"), "api.method")
        self.assertEqual(collectors._bounded_endpoint("/api/resource/Secret DocType/123"), "api.resource")
        self.assertEqual(collectors._bounded_endpoint("/some/custom/page/123"), "other")
        self.assertEqual(collectors._bounded_endpoint("/"), "website.home")

    def test_ip_masking(self):
        self.assertEqual(collectors.mask_ip("192.168.1.99"), "192.168.1.0/24")
        self.assertEqual(collectors.mask_ip("2001:db8::1234"), "2001:db8::/64")
        self.assertEqual(collectors.mask_ip("not an ip"), "")
        self.assertEqual(collectors.mask_ip("192.168.1.99", "full"), "192.168.1.99")

    def test_request_path_uses_one_pipeline_and_returns_response(self):
        redis = FakeRedis()
        local = types.SimpleNamespace(
            site="example.test",
            request=types.SimpleNamespace(
                path="/api/method/frappe.desk.search.search_link?doctype=User",
                remote_addr="10.1.2.3",
                headers={"Content-Length": "12"},
            ),
            response={"http_status_code": 503},
        )
        fake_frappe = types.SimpleNamespace(
            local=local,
            session=types.SimpleNamespace(user="alice@example.test"),
            cache=lambda: types.SimpleNamespace(redis=redis),
            get_cached_doc=lambda *_args: {"mode": "both", "ip_storage": "masked"},
        )
        original_frappe = collectors._frappe
        original_settings_cache = collectors._settings_cache.copy()
        try:
            collectors._frappe = lambda: fake_frappe
            collectors._settings_cache.clear()
            collectors.before_request()
            response = types.SimpleNamespace(status_code=503, headers={"Content-Length": "20"}, data=b"failure body")
            returned = collectors.after_request(response)
            self.assertIs(returned, response)
            self.assertEqual(redis.pipeline_calls, 1)
            self.assertEqual(redis.pipe.executed, 1)
            commands = [command[0] for command in redis.pipe.commands]
            self.assertIn("hincrbyfloat", commands)
            self.assertIn("pfadd", commands)
            self.assertIn("zincrby", commands)
            members = [str(command[1][2]) for command in redis.pipe.commands if command[0] == "zincrby"]
            self.assertIn("10.1.2.0/24", members)
        finally:
            collectors._frappe = original_frappe
            collectors._settings_cache.clear()
            collectors._settings_cache.update(original_settings_cache)

    def test_collector_exceptions_are_fail_open(self):
        original_frappe = collectors._frappe
        try:
            collectors._frappe = lambda: types.SimpleNamespace(
                local=types.SimpleNamespace(
                    site="example.test",
                    _frappe_observability_request=0,
                    request=types.SimpleNamespace(path="/", headers={}),
                    response={},
                ),
                session=types.SimpleNamespace(user="Guest"),
                cache=lambda: (_ for _ in ()).throw(RuntimeError("redis down")),
                get_cached_doc=lambda *_args: {},
            )
            response = object()
            self.assertIs(collectors.after_request(response), response)
            collectors.before_job(method="frappe.test.run")
            collectors.after_job(exception=RuntimeError("failed"))
        finally:
            collectors._frappe = original_frappe


if __name__ == "__main__":
    unittest.main()
