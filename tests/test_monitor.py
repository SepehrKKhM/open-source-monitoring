"""Unit tests for website monitoring behavior."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "sites.json"
        self.state_path = self.root / "state.json"
        self.config_patch = mock.patch.object(monitor, "CONFIG_PATH", self.config_path)
        self.state_patch = mock.patch.object(monitor, "STATE_PATH", self.state_path)
        self.config_patch.start()
        self.state_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.state_patch.stop)

    def write_config(self, config):
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    @staticmethod
    def config(sites=None, timeout=10, threshold=2):
        return {
            "timeout_seconds": timeout,
            "failure_threshold": threshold,
            "sites": (
                [{"name": "site-a", "url": "https://example.com"}]
                if sites is None
                else sites
            ),
        }

    def test_load_valid_config(self):
        self.write_config(self.config())
        sites, timeout, threshold = monitor.load_config()
        self.assertEqual(sites[0]["name"], "site-a")
        self.assertEqual(timeout, 10.0)
        self.assertEqual(threshold, 2)

    def test_load_config_uses_defaults(self):
        self.write_config({"sites": [{"name": "site-a", "url": "http://example.com"}]})
        _, timeout, threshold = monitor.load_config()
        self.assertEqual(timeout, 10.0)
        self.assertEqual(threshold, 2)

    def test_missing_config_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            monitor.load_config()

    def test_invalid_json_is_rejected(self):
        self.config_path.write_text("{invalid", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Cannot read"):
            monitor.load_config()

    def test_empty_sites_are_rejected(self):
        self.write_config(self.config(sites=[]))
        with self.assertRaisesRegex(ValueError, "non-empty"):
            monitor.load_config()

    def test_duplicate_site_names_are_rejected(self):
        sites = [
            {"name": "same", "url": "https://one.example"},
            {"name": "same", "url": "https://two.example"},
        ]
        self.write_config(self.config(sites=sites))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            monitor.load_config()

    def test_invalid_urls_are_rejected(self):
        for url in ("ftp://example.com", "https://user:pass@example.com", "https://bad host"):
            with self.subTest(url=url):
                self.write_config(self.config(sites=[{"name": "bad", "url": url}]))
                with self.assertRaises(ValueError):
                    monitor.load_config()

    def test_invalid_timeout_is_rejected(self):
        for timeout in (True, 0, -1, 3601, float("inf")):
            with self.subTest(timeout=timeout):
                self.write_config(self.config(timeout=timeout))
                with self.assertRaisesRegex(ValueError, "timeout_seconds"):
                    monitor.load_config()

    def test_invalid_failure_threshold_is_rejected(self):
        for threshold in (True, 0, -1, 1.5):
            with self.subTest(threshold=threshold):
                self.write_config(self.config(threshold=threshold))
                with self.assertRaisesRegex(ValueError, "failure_threshold"):
                    monitor.load_config()

    def test_missing_state_starts_empty(self):
        self.assertEqual(monitor.load_state(), {})

    def test_invalid_state_is_rejected(self):
        self.state_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid format"):
            monitor.load_state()

    def test_save_state_uses_owner_only_permissions(self):
        monitor.save_state({"site-a": {"consecutive_failures": 1, "alerted_down": False}})
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        self.assertEqual(monitor.load_state()["site-a"]["consecutive_failures"], 1)

    def test_safe_host_omits_path_and_query(self):
        self.assertEqual(
            monitor.safe_host("https://example.com/private/path?token=secret"),
            "example.com",
        )

    def test_check_site_accepts_http_200(self):
        result = mock.Mock(returncode=0, stdout="200\thttps://example.com/\t0", stderr="")
        with mock.patch.object(monitor.subprocess, "run", return_value=result) as run:
            checked = monitor.check_site({"name": "site-a", "url": "https://example.com"}, 10)
        self.assertTrue(checked["healthy"])
        self.assertEqual(checked["detail"], "HTTP 200")
        self.assertIn("--location", run.call_args.args[0])

    def test_check_site_rejects_non_200_status(self):
        result = mock.Mock(returncode=0, stdout="503\thttps://example.com/\t0", stderr="")
        with mock.patch.object(monitor.subprocess, "run", return_value=result):
            checked = monitor.check_site({"name": "site-a", "url": "https://example.com"}, 10)
        self.assertFalse(checked["healthy"])
        self.assertEqual(checked["detail"], "HTTP 503")

    def test_check_site_reports_dns_error(self):
        result = mock.Mock(returncode=6, stdout="000\t\t0", stderr="Could not resolve")
        with mock.patch.object(monitor.subprocess, "run", return_value=result):
            checked = monitor.check_site({"name": "site-a", "url": "https://example.com"}, 10)
        self.assertFalse(checked["healthy"])
        self.assertEqual(checked["detail"], "DNS lookup failed")

    def test_first_failure_does_not_alert(self):
        current, event = monitor.transition({}, healthy=False, threshold=2)
        self.assertEqual(current["consecutive_failures"], 1)
        self.assertIsNone(event)

    def test_threshold_failure_sends_down_event(self):
        current, event = monitor.transition(
            {"consecutive_failures": 1, "alerted_down": False}, healthy=False, threshold=2
        )
        self.assertEqual(current["consecutive_failures"], 2)
        self.assertEqual(event, "down")

    def test_down_event_is_not_repeated(self):
        _, event = monitor.transition(
            {"consecutive_failures": 2, "alerted_down": True}, healthy=False, threshold=2
        )
        self.assertIsNone(event)

    def test_recovery_event_follows_alerted_outage(self):
        current, event = monitor.transition(
            {"consecutive_failures": 4, "alerted_down": True}, healthy=True, threshold=2
        )
        self.assertEqual(current["consecutive_failures"], 0)
        self.assertEqual(event, "recovery")

    def test_pending_down_event_is_not_queued_twice(self):
        previous = {
            "consecutive_failures": 2,
            "alerted_down": False,
            "pending_notifications": [{"event": "down", "result": {"detail": "DNS lookup failed"}}],
        }
        _, event = monitor.transition(previous, healthy=False, threshold=2)
        self.assertIsNone(event)

    def test_success_after_pending_down_queues_recovery(self):
        previous = {
            "consecutive_failures": 2,
            "alerted_down": False,
            "pending_notifications": [{"event": "down", "result": {"detail": "DNS lookup failed"}}],
        }
        _, event = monitor.transition(previous, healthy=True, threshold=2)
        self.assertEqual(event, "recovery")

    def test_pending_down_is_retried_before_recovery_event(self):
        self.write_config(self.config())
        self.state_path.write_text(
            json.dumps(
                {
                    "sites": {
                        "site-a": {
                            "consecutive_failures": 2,
                            "alerted_down": False,
                            "pending_notifications": [
                                {
                                    "event": "down",
                                    "result": {
                                        "detail": "DNS lookup failed",
                                        "http_status": "000",
                                        "redirects": 0,
                                        "final_host": "example.com",
                                        "consecutive_failures": 2,
                                    },
                                }
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        delivered = []
        healthy_result = {
            "healthy": True,
            "detail": "HTTP 200",
            "http_status": "200",
            "redirects": 0,
            "final_host": "example.com",
        }
        with (
            mock.patch.object(monitor.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(monitor, "check_site", return_value=healthy_result),
            mock.patch.object(
                monitor,
                "send_email",
                side_effect=lambda site, event, result: delivered.append((event, result["detail"])),
            ),
        ):
            self.assertEqual(monitor.main(), 0)

        self.assertEqual(
            delivered,
            [("down", "DNS lookup failed"), ("recovery", "HTTP 200")],
        )
        final_state = monitor.load_state()["site-a"]
        self.assertFalse(final_state["alerted_down"])
        self.assertEqual(final_state["pending_notifications"], [])

    def test_healthy_site_without_down_alert_does_not_recover(self):
        _, event = monitor.transition(
            {"consecutive_failures": 1, "alerted_down": False}, healthy=True, threshold=2
        )
        self.assertIsNone(event)

    def test_mail_settings_require_host_sender_and_recipient(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SMTP_HOST"):
                monitor.send_email("site-a", "down", {"detail": "DNS lookup failed"})

    def test_username_and_password_must_be_paired(self):
        values = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "monitor@example.com",
            "EMAIL_FROM": "monitor@example.com",
            "EMAIL_TO": "recipient@example.com",
        }
        with mock.patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(ValueError, "set together"):
                monitor.send_email("site-a", "down", {"detail": "DNS lookup failed"})

    def test_load_and_validate_complete_notification_file(self):
        path = self.root / "notifications.env"
        path.write_text(
            "# SMTP settings\n"
            "SMTP_HOST=smtp.example.com\n"
            "SMTP_PORT=587\n"
            "SMTP_SECURITY=starttls\n"
            "SMTP_USERNAME=monitor@example.com\n"
            "SMTP_PASSWORD=app-password=with-equals\n"
            "EMAIL_FROM=monitor@example.com\n"
            "EMAIL_TO=alerts@example.net\n",
            encoding="utf-8",
        )

        settings = monitor.load_notification_settings(path)
        validated = monitor.validate_email_settings(settings)
        self.assertEqual(validated["SMTP_PORT"], 587)
        self.assertEqual(validated["SMTP_SECURITY"], "starttls")
        self.assertEqual(validated["SMTP_PASSWORD"], "app-password=with-equals")

    def test_notification_file_rejects_missing_required_values(self):
        path = self.root / "notifications.env"
        path.write_text(
            "SMTP_HOST=smtp.example.com\nEMAIL_FROM=\nEMAIL_TO=alerts@example.net\n",
            encoding="utf-8",
        )
        settings = monitor.load_notification_settings(path)
        with self.assertRaisesRegex(ValueError, "EMAIL_FROM"):
            monitor.validate_email_settings(settings)

    def test_notification_file_rejects_invalid_security_and_port(self):
        base = {
            "SMTP_HOST": "smtp.example.com",
            "EMAIL_FROM": "monitor@example.com",
            "EMAIL_TO": "alerts@example.net",
        }
        for values in (
            {**base, "SMTP_SECURITY": "plain"},
            {**base, "SMTP_PORT": "not-a-port"},
            {**base, "SMTP_PORT": "70000"},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    monitor.validate_email_settings(values)

    def test_environment_file_parser_does_not_evaluate_shell_syntax(self):
        path = self.root / "notifications.env"
        path.write_text("SMTP_PASSWORD=$(echo should-not-run)\n", encoding="utf-8")
        self.assertEqual(
            monitor.load_notification_settings(path)["SMTP_PASSWORD"],
            "$(echo should-not-run)",
        )


if __name__ == "__main__":
    unittest.main()
