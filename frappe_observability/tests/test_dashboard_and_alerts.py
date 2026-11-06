import types
import unittest
from datetime import datetime, timedelta, timezone

from frappe_observability import alerts, dashboard, scheduled_tasks


class FakePipeline:
    def __init__(self, result):
        self.commands = []
        self.result = result

    def hget(self, key, field):
        self.commands.append((key, field))
        return self

    def execute(self):
        return list(self.result)


class FakeRedis:
    def __init__(self, *, gauge=None, pipeline_result=()):
        self.gauge = gauge
        self.pipe = FakePipeline(pipeline_result)

    def hget(self, key, field):
        if key.endswith(":current_sample"):
            return self.gauge
        return None

    def pipeline(self, transaction=False):
        return self.pipe


class DashboardTests(unittest.TestCase):
    def test_window_is_limited_to_supported_ranges(self):
        self.assertEqual(dashboard._clamp_window("15"), 15)
        self.assertEqual(dashboard._clamp_window("60"), 60)
        self.assertEqual(dashboard._clamp_window(900), 360)
        self.assertEqual(dashboard._clamp_window(99999), 10080)
        self.assertEqual(dashboard._clamp_window("bad"), 60)

    def test_sample_age_handles_missing_and_stale_timestamps(self):
        self.assertIsNone(dashboard._age_seconds(None))
        old = (datetime.now(timezone.utc) - timedelta(seconds=210)).isoformat()
        self.assertGreaterEqual(dashboard._age_seconds(old), 200)

    def test_downsampled_gauges_average_while_counters_sum(self):
        ts = "2026-11-06 10:00:00"
        result = dashboard._time_series([
            {"ts": ts, "metric": "host_cpu_percent", "label": "", "value": 350, "sample_count": 5},
            {"ts": ts, "metric": "requests", "label": "all", "value": 25, "sample_count": 5},
            {"ts": ts, "metric": "host_disk_read_bytes_total", "label": "", "value": 2500,
             "sample_count": 5, "max_value": 800},
        ], 1440, {})
        self.assertEqual(result["cpu_percent"][0]["value"], 70)
        self.assertEqual(result["requests"][0]["value"], 25)
        self.assertEqual(result["disk_read_bytes_total"][0]["value"], 800)


    def test_request_series_calculates_error_rate_and_histogram_p95(self):
        ts = "2026-11-06 10:00:00"
        rows = [
            {"ts": ts, "metric": "requests", "label": "all", "value": 100},
            {"ts": ts, "metric": "request_errors", "label": "all", "value": 10},
            {"ts": ts, "metric": "request_duration_ms", "label": "all", "value": 5000, "sample_count": 100},
            {"ts": ts, "metric": "request_duration_le_5_ms", "label": "all", "value": 10},
            {"ts": ts, "metric": "request_duration_le_10_ms", "label": "all", "value": 20},
            {"ts": ts, "metric": "request_duration_le_25_ms", "label": "all", "value": 30},
            {"ts": ts, "metric": "request_duration_le_50_ms", "label": "all", "value": 40},
        ]
        result = dashboard._time_series(rows, 60, {})
        self.assertEqual(result["requests"][0]["value"], 100)
        self.assertEqual(result["errors"][0]["value"], 10)
        self.assertEqual(result["error_rate_percent"][0]["value"], 10)
        self.assertEqual(result["p95_ms"][0]["value"], 50)

    def test_history_query_uses_fixed_metrics_and_hourly_downsampling(self):
        class DB:
            def __init__(self):
                self.query = None
                self.values = None

            def sql(self, query, values, as_dict=False):
                self.query, self.values = query, values
                return [{"bucket": datetime(2026, 11, 1, 0, 0), "metric": "requests", "label": "all", "value": 12, "sample_count": 1}]

        fake = types.SimpleNamespace(db=DB())
        result = dashboard._query_history(fake, datetime(2026, 11, 1), 10080)
        self.assertIn("UNIX_TIMESTAMP(`ts`) / 3600", fake.db.query)
        self.assertIn("MAX(`max`) AS max_value", fake.db.query)
        self.assertIn("LIMIT 25000", fake.db.query)
        self.assertEqual(fake.db.values[0], datetime(2026, 11, 1))
        self.assertEqual(result[0]["value"], 12)
        self.assertEqual(result[0]["label"], "all")


class AlertMetricTests(unittest.TestCase):
    def make_frappe(self, redis):
        return types.SimpleNamespace(
            local=types.SimpleNamespace(site="example.test"),
            conf={},
            cache=lambda: types.SimpleNamespace(redis=redis),
        )

    def test_default_rules_include_capacity_and_backup_alerts(self):
        metrics = {rule["metric_name"] for rule in alerts.DEFAULT_ALERT_RULES}
        self.assertIn("frappe_obs_host_root_disk_percent", metrics)
        self.assertIn("frappe_obs_backup_age_hours", metrics)
        self.assertIn("frappe_obs_database_connections_percent", metrics)
        self.assertGreaterEqual(len(alerts.DEFAULT_ALERT_RULES), 9)

    def test_stale_flush_threshold_scales_with_configured_interval(self):
        rule = next(rule for rule in alerts._load_rules(types.SimpleNamespace(conf={}))
                    if rule["metric"] == "frappe_obs_flush_age_seconds")
        self.assertEqual(alerts._effective_threshold(rule, {"flush_interval_seconds": 60}), 180)
        self.assertEqual(alerts._effective_threshold(rule, {"flush_interval_seconds": 600}), 1200)

    def test_live_gauge_alert_reads_latest_sample(self):
        fake = self.make_frappe(FakeRedis(gauge=b"92.5"))
        present, value = alerts._read_metric_total(fake, "frappe_obs_host_root_disk_percent")
        self.assertTrue(present)
        self.assertEqual(value, 92.5)

    def test_counter_alert_sums_only_configured_recent_minutes(self):
        redis = FakeRedis(pipeline_result=[b"3", b"5"])
        fake = self.make_frappe(redis)
        present, value = alerts._read_metric_total(fake, "frappe_obs_request_errors_total", 1)
        self.assertTrue(present)
        self.assertEqual(value, 3)
        self.assertEqual(len(redis.pipe.commands), 2)
        self.assertTrue(all(":minute:" in key for key, _field in redis.pipe.commands))


class RedisSamplerTests(unittest.TestCase):
    def test_cache_and_queue_redis_metrics_are_separate(self):
        import sys
        from unittest.mock import patch

        cache = types.SimpleNamespace(info=lambda: {"used_memory": 100, "maxmemory": 200, "connected_clients": 2,
                                                    "keyspace_hits": 8, "keyspace_misses": 2, "evicted_keys": 1})
        queue = types.SimpleNamespace(info=lambda: {"used_memory": 900, "maxmemory": 1000, "connected_clients": 5,
                                                    "keyspace_hits": 3, "keyspace_misses": 1, "evicted_keys": 0})
        frappe_utils = types.ModuleType("frappe.utils")
        background_jobs = types.ModuleType("frappe.utils.background_jobs")
        background_jobs.get_redis_conn = lambda: queue
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.__path__ = []
        with patch.dict(sys.modules, {"frappe": fake_frappe, "frappe.utils": frappe_utils,
                                      "frappe.utils.background_jobs": background_jobs}):
            values = scheduled_tasks._redis_info_values(types.SimpleNamespace(
                cache=lambda: types.SimpleNamespace(redis=cache), local=types.SimpleNamespace(site="site.test")))
        self.assertEqual(values["redis_memory_percent"], 50)
        self.assertEqual(values["redis_queue_memory_percent"], 90)
        self.assertEqual(values["redis_queue_clients_connected"], 5)


if __name__ == "__main__":
    unittest.main()
