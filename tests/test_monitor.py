"""Unit tests for website monitoring behavior."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib import error as urllib_error

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
    def email_environment():
        return {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls",
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
            "EMAIL_FROM": "monitor@example.com",
            "EMAIL_TO": "alerts@example.net",
        }

    @staticmethod
    def telegram_environment():
        return {
            "TELEGRAM_MODE": "webhook",
            "TELEGRAM_WEBHOOK_URL": "https://workflow.example.net/webhook/test-id",
            "TELEGRAM_WEBHOOK_SECRET": "a" * 64,
            "TELEGRAM_TIMEOUT": "5",
        }

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
            mock.patch.dict(os.environ, self.email_environment(), clear=True),
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

    def test_legacy_pending_email_is_migrated_with_event_id(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "sites": {
                        "site-a": {
                            "consecutive_failures": 2,
                            "alerted_down": False,
                            "pending_notifications": [
                                {"event": "down", "result": {"detail": "DNS lookup failed"}}
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        event = monitor.load_state()["site-a"]["pending_notifications"][0]
        self.assertTrue(event["event_id"])
        self.assertEqual(event["channels"], {"email": "pending"})
        self.assertEqual(event["status"], "DOWN")
        self.assertIn("T", event["timestamp"])

    def test_email_only_settings_are_valid(self):
        settings = monitor.validate_notification_settings(self.email_environment())
        self.assertTrue(settings["email"])
        self.assertEqual(settings["telegram"], {})

    def test_telegram_only_settings_are_valid_even_with_example_smtp_host(self):
        settings = monitor.validate_notification_settings(
            {"SMTP_HOST": "smtp.gmail.com", **self.telegram_environment()}
        )
        self.assertEqual(settings["email"], {})
        self.assertEqual(settings["telegram"]["TELEGRAM_TIMEOUT"], 5)

    def test_at_least_one_notification_channel_is_required(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            monitor.validate_notification_settings({})

    def test_telegram_webhook_requires_https_url_and_long_secret(self):
        valid = self.telegram_environment()
        for values in (
            {**valid, "TELEGRAM_WEBHOOK_URL": "http://workflow.example.net/webhook/id"},
            {**valid, "TELEGRAM_WEBHOOK_URL": "https://workflow.example.net/webhook/id?secret=bad"},
            {**valid, "TELEGRAM_WEBHOOK_URL": "https://workflow.example.net/webhook/bad path"},
            {**valid, "TELEGRAM_WEBHOOK_SECRET": "short"},
            {**valid, "TELEGRAM_TIMEOUT": "61"},
            {"TELEGRAM_WEBHOOK_URL": valid["TELEGRAM_WEBHOOK_URL"]},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    monitor.validate_telegram_settings(values)

    def test_direct_bot_api_settings_are_valid(self):
        values = {
            "TELEGRAM_MODE": "bot_api",
            "TELEGRAM_BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            "TELEGRAM_CHAT_ID": "-1001234567890",
            "TELEGRAM_TIMEOUT": "5",
        }
        settings = monitor.validate_telegram_settings(values)
        self.assertEqual(settings["TELEGRAM_MODE"], "bot_api")
        self.assertEqual(settings["TELEGRAM_CHAT_ID"], "-1001234567890")
        self.assertEqual(settings["TELEGRAM_TIMEOUT"], 5)

    def test_direct_bot_api_requires_token_and_chat_id(self):
        valid = {
            "TELEGRAM_MODE": "bot_api",
            "TELEGRAM_BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            "TELEGRAM_CHAT_ID": "123456789",
        }
        for values in (
            {**valid, "TELEGRAM_BOT_TOKEN": "bad token"},
            {**valid, "TELEGRAM_BOT_TOKEN": ""},
            {**valid, "TELEGRAM_CHAT_ID": ""},
            {**valid, "TELEGRAM_CHAT_ID": "chat id with spaces"},
            {**valid, "TELEGRAM_MODE": "unknown"},
            {**valid, "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_WEBHOOK_URL": "https://workflow.example.net/hook"},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    monitor.validate_telegram_settings(values)

    def test_legacy_n8n_settings_remain_supported(self):
        settings = monitor.validate_telegram_settings(
            {
                "N8N_WEBHOOK_URL": "https://n8n.example.net/webhook/test-id",
                "N8N_WEBHOOK_SECRET": "a" * 64,
                "N8N_TIMEOUT": "5",
            }
        )
        self.assertEqual(settings["TELEGRAM_MODE"], "webhook")
        self.assertEqual(settings["TELEGRAM_TIMEOUT"], 5)

    def test_telegram_webhook_sends_json_and_secret_header(self):
        response = mock.MagicMock()
        response.status = 200
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        event = {
            "event_id": "event-123",
            "server": "monitor-vm",
            "service": "site-a",
            "status": "DOWN",
            "severity": "CRITICAL",
            "timestamp": "2026-10-31T10:00:00Z",
        }
        with mock.patch.object(monitor.urllib_request, "build_opener", return_value=opener):
            monitor.send_telegram_webhook(
                event,
                {
                    "TELEGRAM_WEBHOOK_URL": "https://workflow.example.net/webhook/test-id",
                    "TELEGRAM_WEBHOOK_SECRET": "a" * 64,
                    "TELEGRAM_TIMEOUT": 5,
                },
            )

        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("X-webhook-secret"), "a" * 64)
        self.assertEqual(json.loads(request.data), event)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)

    def test_direct_bot_api_sends_plain_text_to_chat_id(self):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        event = {
            "event_id": "event-123",
            "server": "monitor-vm",
            "service": "site-a",
            "status": "DOWN",
            "severity": "CRITICAL",
            "timestamp": "2026-10-31T10:00:00Z",
            "detail": "DNS lookup failed",
            "http_status": "000",
            "redirects": 0,
            "final_host": "example.com",
            "consecutive_failures": 2,
        }
        with mock.patch.object(monitor.urllib_request, "build_opener", return_value=opener):
            monitor.send_telegram_bot_api(
                event,
                {
                    "TELEGRAM_BOT_TOKEN": token,
                    "TELEGRAM_CHAT_ID": "-1001234567890",
                    "TELEGRAM_TIMEOUT": 5,
                },
            )

        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, f"https://api.telegram.org/bot{token}/sendMessage")
        payload = json.loads(request.data)
        self.assertEqual(payload["chat_id"], "-1001234567890")
        self.assertIn("Website DOWN", payload["text"])
        self.assertIn("DNS lookup failed", payload["text"])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)

    def test_direct_bot_api_rejection_does_not_expose_token(self):
        token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        opener = mock.MagicMock()
        opener.open.side_effect = urllib_error.HTTPError(
            f"https://api.telegram.org/bot{token}/sendMessage",
            401,
            "Unauthorized",
            {},
            None,
        )
        with mock.patch.object(monitor.urllib_request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(monitor.TelegramDeliveryError, "HTTP 401") as raised:
                monitor.send_telegram_bot_api(
                    {"status": "DOWN"},
                    {
                        "TELEGRAM_BOT_TOKEN": token,
                        "TELEGRAM_CHAT_ID": "123456789",
                        "TELEGRAM_TIMEOUT": 5,
                    },
                )
        self.assertNotIn(token, str(raised.exception))

    def test_telegram_dispatch_uses_selected_mode(self):
        event = {"status": "DOWN"}
        bot_settings = {"TELEGRAM_MODE": "bot_api"}
        webhook_settings = {"TELEGRAM_MODE": "webhook"}
        with (
            mock.patch.object(monitor, "send_telegram_bot_api") as bot_api,
            mock.patch.object(monitor, "send_telegram_webhook") as webhook,
        ):
            monitor.send_telegram_notification(event, bot_settings)
            bot_api.assert_called_once_with(event, bot_settings)
            webhook.assert_not_called()

            monitor.send_telegram_notification(event, webhook_settings)
            webhook.assert_called_once_with(event, webhook_settings)

    def test_telegram_http_failure_does_not_log_webhook_url(self):
        opener = mock.MagicMock()
        opener.open.side_effect = urllib_error.HTTPError(
            "https://workflow.example.net/webhook/private-id", 401, "Unauthorized", {}, None
        )
        with mock.patch.object(monitor.urllib_request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(monitor.WebhookDeliveryError, "HTTP 401") as raised:
                monitor.send_telegram_webhook(
                    {},
                    {
                        "TELEGRAM_WEBHOOK_URL": "https://workflow.example.net/webhook/private-id",
                        "TELEGRAM_WEBHOOK_SECRET": "a" * 64,
                        "TELEGRAM_TIMEOUT": 5,
                    },
                )
        self.assertNotIn("private-id", str(raised.exception))

    def test_telegram_timeout_is_reported_without_exposing_url(self):
        opener = mock.MagicMock()
        opener.open.side_effect = TimeoutError("timed out")
        with mock.patch.object(monitor.urllib_request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(monitor.WebhookDeliveryError, "timed out") as raised:
                monitor.send_telegram_webhook(
                    {},
                    {
                        "TELEGRAM_WEBHOOK_URL": "https://workflow.example.net/webhook/private-id",
                        "TELEGRAM_WEBHOOK_SECRET": "a" * 64,
                        "TELEGRAM_TIMEOUT": 1,
                    },
                )
        self.assertNotIn("private-id", str(raised.exception))

    def test_webhook_redirects_are_not_followed(self):
        handler = monitor._NoRedirectHandler()
        redirected = handler.redirect_request(
            mock.Mock(), mock.Mock(), 307, "Temporary Redirect", {}, "https://other.example.net/"
        )
        self.assertIsNone(redirected)

    def test_successful_email_is_not_repeated_while_telegram_retries(self):
        notification = monitor.make_notification(
            "site-a",
            "down",
            {"detail": "DNS lookup failed", "consecutive_failures": 2},
            {"email": "pending", "telegram": "pending"},
        )
        current = {
            "consecutive_failures": 2,
            "alerted_down": False,
            "pending_notifications": [notification],
        }
        state = {"site-a": current}
        email = mock.Mock()
        telegram = mock.Mock(side_effect=monitor.WebhookDeliveryError("Telegram workflow returned HTTP 503"))
        with (
            mock.patch.object(monitor, "send_email", email),
            mock.patch.object(monitor, "send_telegram_webhook", telegram),
        ):
            self.assertTrue(
                monitor.deliver_pending_notifications(
                    "site-a", current, state,
                    {"email": {"configured": True}, "telegram": {"TELEGRAM_MODE": "webhook", "TELEGRAM_TIMEOUT": 5}}
                )
            )
        self.assertEqual(notification["channels"], {"email": "sent", "telegram": "pending"})
        self.assertEqual(email.call_count, 1)

        # Simulate a new process reading the queue after a VM/program restart.
        restored_state = monitor.load_state()
        restored_current = restored_state["site-a"]
        restored_event = restored_current["pending_notifications"][0]
        self.assertEqual(
            restored_event["channels"], {"email": "sent", "telegram": "pending"}
        )
        email.reset_mock()
        telegram.reset_mock(side_effect=True)
        with (
            mock.patch.object(monitor, "send_email", email),
            mock.patch.object(monitor, "send_telegram_webhook", telegram),
        ):
            self.assertFalse(
                monitor.deliver_pending_notifications(
                    "site-a", restored_current, restored_state,
                    {"email": {"configured": True}, "telegram": {"TELEGRAM_MODE": "webhook", "TELEGRAM_TIMEOUT": 5}}
                )
            )
        email.assert_not_called()
        telegram.assert_called_once()
        self.assertEqual(restored_current["pending_notifications"], [])

    def test_telegram_succeeds_when_email_fails(self):
        notification = monitor.make_notification(
            "site-a",
            "recovery",
            {"detail": "HTTP 200"},
            {"email": "pending", "telegram": "pending"},
        )
        current = {
            "consecutive_failures": 0,
            "alerted_down": True,
            "pending_notifications": [notification],
        }
        state = {"site-a": current}
        with (
            mock.patch.object(monitor, "send_email", side_effect=OSError("SMTP unavailable")),
            mock.patch.object(monitor, "send_telegram_webhook") as telegram,
        ):
            self.assertTrue(
                monitor.deliver_pending_notifications(
                    "site-a", current, state,
                    {"email": {"configured": True}, "telegram": {"TELEGRAM_MODE": "webhook", "TELEGRAM_TIMEOUT": 5}}
                )
            )
        telegram.assert_called_once()
        self.assertEqual(notification["channels"], {"email": "pending", "telegram": "sent"})

    def test_telegram_retries_down_before_recovery(self):
        down = monitor.make_notification(
            "site-a", "down", {"detail": "DNS lookup failed"}, {"telegram": "pending"}
        )
        recovery = monitor.make_notification(
            "site-a", "recovery", {"detail": "HTTP 200"}, {"telegram": "pending"}
        )
        current = {
            "consecutive_failures": 0,
            "alerted_down": False,
            "pending_notifications": [down, recovery],
        }
        state = {"site-a": current}
        send = mock.Mock(side_effect=[monitor.WebhookDeliveryError("Telegram workflow returned HTTP 503"), None, None])
        with mock.patch.object(monitor, "send_telegram_webhook", send):
            self.assertTrue(
                monitor.deliver_pending_notifications(
                    "site-a", current, state,
                    {"email": {}, "telegram": {"TELEGRAM_MODE": "webhook", "TELEGRAM_TIMEOUT": 5}},
                )
            )
            self.assertEqual(len(current["pending_notifications"]), 2)
            self.assertFalse(monitor.deliver_pending_notifications(
                "site-a", current, state,
                {"email": {}, "telegram": {"TELEGRAM_MODE": "webhook", "TELEGRAM_TIMEOUT": 5}},
            ))

        self.assertEqual(
            [call.args[0]["status"] for call in send.call_args_list],
            ["DOWN", "DOWN", "RECOVERY"],
        )
        self.assertEqual(send.call_count, 3)
        self.assertEqual(current["pending_notifications"], [])

    def test_both_channels_send_success_and_clear_event(self):
        notification = monitor.make_notification(
            "site-a",
            "down",
            {"detail": "DNS lookup failed"},
            {"email": "pending", "telegram": "pending"},
        )
        current = {
            "consecutive_failures": 2,
            "alerted_down": False,
            "pending_notifications": [notification],
        }
        state = {"site-a": current}
        with (
            mock.patch.object(monitor, "send_email") as email,
            mock.patch.object(monitor, "send_telegram_webhook") as telegram,
        ):
            self.assertFalse(
                monitor.deliver_pending_notifications(
                    "site-a", current, state,
                    {"email": {"configured": True}, "telegram": {"TELEGRAM_MODE": "webhook", "TELEGRAM_TIMEOUT": 5}},
                )
            )
        email.assert_called_once()
        telegram.assert_called_once()
        self.assertEqual(current["pending_notifications"], [])

    def test_telegram_gets_down_then_recovery_events(self):
        self.write_config(self.config(threshold=1))
        failed_result = {
            "healthy": False,
            "detail": "DNS lookup failed",
            "http_status": "000",
            "redirects": 0,
            "final_host": "example.com",
        }
        healthy_result = {
            "healthy": True,
            "detail": "HTTP 200",
            "http_status": "200",
            "redirects": 0,
            "final_host": "example.com",
        }
        telegram_payloads = []
        with (
            mock.patch.dict(os.environ, self.telegram_environment(), clear=True),
            mock.patch.object(monitor.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(monitor, "check_site", side_effect=[failed_result, healthy_result]),
            mock.patch.object(monitor, "send_email") as email,
            mock.patch.object(
                monitor,
                "send_telegram_webhook",
                side_effect=lambda payload, settings: telegram_payloads.append(payload.copy()),
            ),
        ):
            self.assertEqual(monitor.main(), 0)
            self.assertEqual(monitor.main(), 0)

        email.assert_not_called()
        self.assertEqual([event["status"] for event in telegram_payloads], ["DOWN", "RECOVERY"])
        self.assertEqual(telegram_payloads[0]["severity"], "CRITICAL")
        self.assertEqual(telegram_payloads[1]["severity"], "INFO")
        self.assertEqual(telegram_payloads[0]["service"], "site-a")
        self.assertNotEqual(telegram_payloads[0]["event_id"], telegram_payloads[1]["event_id"])

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
