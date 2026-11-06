import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class LogBrowserSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.whitelist = lambda **_kwargs: (lambda function: function)
        fake_frappe.local = types.SimpleNamespace(conf={"encryption_key": "isolated-test-key"})
        with patch.dict(sys.modules, {"frappe": fake_frappe}):
            cls.browser = importlib.import_module("frappe_observability.log_browser")

    def test_filename_allowlist_accepts_logs_and_rejects_arbitrary_files(self):
        self.assertTrue(self.browser._name_allowed("worker.log"))
        self.assertTrue(self.browser._name_allowed("monitor.json.log.1.gz"))
        self.assertFalse(self.browser._name_allowed("secrets.txt"))
        self.assertFalse(self.browser._name_allowed("../worker.log"))

    def test_directory_listing_omits_symlinks_and_only_returns_regular_logs(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root_path = Path(temporary)
            (root_path / "frappe.log").write_text("safe\n")
            (root_path / "notes.txt").write_text("not a log\n")
            secret = Path(outside) / "secret.log"
            secret.write_text("secret\n")
            (root_path / "linked.log").symlink_to(secret)
            root = self.browser._Root(str(root_path), "site", "test")
            entries, total = self.browser._directory_listing(root, 0)
        self.assertEqual([item["name"] for item in entries], ["frappe.log"])
        self.assertEqual(total, 5)
        self.assertTrue(entries[0]["id"])

    def test_open_checked_fd_rejects_internal_and_external_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root_path = Path(temporary)
            normal = root_path / "normal.log"
            normal.write_bytes(b"ok")
            target_inside = root_path / "target.log"
            target_inside.write_bytes(b"inside")
            inside_link = root_path / "inside-link.log"
            inside_link.symlink_to(target_inside)
            outside_target = Path(outside) / "outside.log"
            outside_target.write_bytes(b"outside")
            outside_link = root_path / "outside-link.log"
            outside_link.symlink_to(outside_target)
            root = self.browser._Root(str(root_path), "site", "test")

            file_fd, _stat = self.browser._open_checked_fd(root, str(normal))
            os.close(file_fd)
            for candidate in (inside_link, outside_link):
                with self.subTest(path=candidate.name), self.assertRaises(self.browser._LogBrowserError):
                    self.browser._open_checked_fd(root, str(candidate))

    def test_view_redaction_replaces_sensitive_lines(self):
        text = self.browser._redact(b'token="private-value"\nordinary log row\n')
        self.assertIn("[REDACTED]", text)
        self.assertIn("ordinary log row", text)
        self.assertNotIn("private-value", text)


if __name__ == "__main__":
    unittest.main()
