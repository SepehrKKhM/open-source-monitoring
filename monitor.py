#!/usr/bin/env python3
"""Check configured websites and notify independent channels on state changes."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import socket
import smtplib
import ssl
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config" / "sites.json"
STATE_PATH = Path(os.environ.get("WEBSITE_MONITOR_STATE_FILE", PROJECT_DIR / "state.json"))
LOGGER = logging.getLogger("website-monitor")


def load_config() -> tuple[list[dict[str, str]], float, int]:
    """Read and validate the site configuration."""
    try:
        with CONFIG_PATH.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration file not found: {CONFIG_PATH}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read configuration file {CONFIG_PATH}: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")

    sites = config.get("sites")
    timeout = config.get("timeout_seconds", 10)
    failure_threshold = config.get("failure_threshold", 2)

    if not isinstance(sites, list) or not sites:
        raise ValueError("Configuration must contain a non-empty 'sites' list")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or (isinstance(timeout, float) and not math.isfinite(timeout))
        or timeout <= 0
        or timeout > 3600
    ):
        raise ValueError("timeout_seconds must be greater than 0 and at most 3600")
    if (
        isinstance(failure_threshold, bool)
        or not isinstance(failure_threshold, int)
        or failure_threshold < 1
    ):
        raise ValueError("failure_threshold must be a positive integer")

    names: set[str] = set()
    for site in sites:
        if not isinstance(site, dict):
            raise ValueError("Each site must be an object with a name and URL")

        name = site.get("name")
        url = site.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each site must have a non-empty string name")
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError("Site names must not contain control characters")
        if name in names:
            raise ValueError(f"Duplicate site name: {name}")
        names.add(name)
        if (
            not isinstance(url, str)
            or not url.strip()
            or any(char.isspace() for char in url)
        ):
            raise ValueError(f"Site {name!r} must have a non-empty URL without whitespace")

        try:
            parsed = urlsplit(url)
            port = parsed.port
            valid_url = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and (port is None or 1 <= port <= 65535)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError(
                f"Site {name!r} must use a valid HTTP or HTTPS URL without credentials"
            )

    return sites, float(timeout), failure_threshold


def load_state() -> dict[str, dict[str, object]]:
    """Load site state and migrate legacy email-only queue entries."""
    try:
        with STATE_PATH.open(encoding="utf-8") as state_file:
            raw_state = json.load(state_file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read state file {STATE_PATH}: {exc}") from exc

    if not isinstance(raw_state, dict) or not isinstance(raw_state.get("sites"), dict):
        raise ValueError(f"State file {STATE_PATH} has an invalid format")

    state: dict[str, dict[str, object]] = {}
    for name, item in raw_state["sites"].items():
        if not isinstance(name, str) or not isinstance(item, dict):
            continue
        failures = item.get("consecutive_failures", 0)
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
            failures = 0
        pending_notifications = []
        raw_pending = item.get("pending_notifications", [])
        if isinstance(raw_pending, list):
            for notification in raw_pending:
                if not isinstance(notification, dict):
                    continue
                event = notification.get("event")
                result = notification.get("result")
                if event not in ("down", "recovery") or not isinstance(result, dict):
                    continue
                detail = result.get("detail", "Unknown result")
                status = result.get("http_status", "")
                redirects = result.get("redirects", 0)
                event_failures = result.get("consecutive_failures", failures)
                final_host = result.get("final_host", "unknown")
                if not isinstance(detail, str):
                    detail = "Unknown result"
                if not isinstance(status, str):
                    status = ""
                if isinstance(redirects, bool) or not isinstance(redirects, int) or redirects < 0:
                    redirects = 0
                if (
                    isinstance(event_failures, bool)
                    or not isinstance(event_failures, int)
                    or event_failures < 0
                ):
                    event_failures = failures
                if not isinstance(final_host, str):
                    final_host = "unknown"
                event_id = notification.get("event_id")
                try:
                    event_id = str(uuid.UUID(event_id))
                except (ValueError, TypeError, AttributeError):
                    event_id = str(uuid.uuid4())
                timestamp = notification.get("timestamp")
                if not isinstance(timestamp, str) or not timestamp.strip():
                    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                server = notification.get("server", "")
                if not isinstance(server, str) or not server.strip():
                    server = socket.gethostname() or "unknown"
                service = notification.get("service", name)
                if not isinstance(service, str) or not service.strip():
                    service = name
                severity = notification.get(
                    "severity", "CRITICAL" if event == "down" else "INFO"
                )
                if severity not in ("CRITICAL", "INFO"):
                    severity = "CRITICAL" if event == "down" else "INFO"

                raw_channels = notification.get("channels")
                if not isinstance(raw_channels, dict):
                    # Existing state files only queued email notifications.
                    channels = {"email": "pending"}
                else:
                    channels = {
                        channel: state_value
                        for channel in ("email", "telegram")
                        if (state_value := raw_channels.get(channel)) in ("pending", "sent")
                    }
                    for channel in raw_channels:
                        if channel in ("email", "telegram") and channel not in channels:
                            channels[channel] = "pending"
                    if not channels:
                        channels = {"email": "pending"}

                pending_notifications.append(
                    {
                        "event_id": event_id[:128],
                        "event": event,
                        "timestamp": timestamp[:64],
                        "server": server[:255],
                        "service": service[:255],
                        "status": "DOWN" if event == "down" else "RECOVERY",
                        "severity": severity,
                        "channels": channels,
                        "result": {
                            "detail": detail[:500],
                            "http_status": status[:16],
                            "redirects": redirects,
                            "final_host": final_host[:253],
                            "consecutive_failures": event_failures,
                        },
                    }
                )
        state[name] = {
            "consecutive_failures": failures,
            "alerted_down": item.get("alerted_down") is True,
            "pending_notifications": pending_notifications,
        }
    return state


def save_state(state: dict[str, dict[str, object]]) -> None:
    """Atomically save state with owner-only file permissions."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=STATE_PATH.parent, delete=False
        ) as state_file:
            temporary_path = Path(state_file.name)
            os.fchmod(state_file.fileno(), 0o600)
            json.dump({"sites": state}, state_file, indent=2)
            state_file.write("\n")
        temporary_path.replace(STATE_PATH)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def safe_host(url: str) -> str:
    """Return a hostname without URL paths, queries, or credentials."""
    try:
        return urlsplit(url).hostname or "unknown"
    except ValueError:
        return "unknown"


def load_notification_settings(path: Path) -> dict[str, str]:
    """Read KEY=VALUE notification settings without evaluating shell code."""
    settings: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as settings_file:
            for line_number, raw_line in enumerate(settings_file, start=1):
                line = raw_line.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if "=" not in line:
                    raise ValueError(
                        f"Invalid notification settings line {line_number}: expected KEY=VALUE"
                    )
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise ValueError(
                        f"Invalid notification settings line {line_number}: invalid variable name"
                    )
                if value.startswith(("'", '"')):
                    quote = value[0]
                    if len(value) < 2 or value[-1] != quote:
                        raise ValueError(
                            f"Invalid notification settings line {line_number}: unmatched quote"
                        )
                    value = value[1:-1]
                settings[key] = value
    except OSError as exc:
        raise ValueError(f"Cannot read notification settings file {path}: {exc}") from exc
    return settings


def validate_email_settings(settings: Mapping[str, str]) -> dict[str, str | int]:
    """Validate SMTP values and return normalized settings without secrets in errors."""
    required = ("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO")
    missing = [key for key in required if not settings.get(key, "").strip()]
    if missing:
        raise ValueError(f"Missing email settings: {', '.join(missing)}")

    security = settings.get("SMTP_SECURITY", "starttls").strip().lower()
    if security not in {"starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY must be 'starttls' or 'ssl'")
    default_port = "587" if security == "starttls" else "465"
    raw_port = settings.get("SMTP_PORT", default_port).strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535")

    username = settings.get("SMTP_USERNAME", "")
    password = settings.get("SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together")

    return {
        "SMTP_HOST": settings["SMTP_HOST"].strip(),
        "SMTP_PORT": port,
        "SMTP_SECURITY": security,
        "SMTP_USERNAME": username,
        "SMTP_PASSWORD": password,
        "EMAIL_FROM": settings["EMAIL_FROM"].strip(),
        "EMAIL_TO": settings["EMAIL_TO"].strip(),
    }


def validate_telegram_settings(settings: Mapping[str, str]) -> dict[str, str | int]:
    """Validate optional direct Bot API or generic workflow webhook settings."""
    mode = settings.get("TELEGRAM_MODE", "").strip().lower()
    bot_token = settings.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = settings.get("TELEGRAM_CHAT_ID", "").strip()
    webhook_url = settings.get("TELEGRAM_WEBHOOK_URL", "").strip()
    webhook_secret = settings.get("TELEGRAM_WEBHOOK_SECRET", "").strip()

    # Keep existing n8n installations working while the public configuration
    # uses provider-neutral webhook names.
    legacy_url = settings.get("N8N_WEBHOOK_URL", "").strip()
    legacy_secret = settings.get("N8N_WEBHOOK_SECRET", "").strip()
    legacy_configured = bool(legacy_url or legacy_secret)
    if legacy_configured:
        if webhook_url or webhook_secret or bot_token or chat_id or mode == "bot_api":
            raise ValueError("Use either the legacy n8n settings or one Telegram mode, not both")
        webhook_url = webhook_url or legacy_url
        webhook_secret = webhook_secret or legacy_secret
        mode = mode or "webhook"

    if not mode:
        if bot_token or chat_id:
            mode = "bot_api"
        elif webhook_url or webhook_secret:
            mode = "webhook"
        else:
            return {}

    if mode not in {"bot_api", "webhook"}:
        raise ValueError("TELEGRAM_MODE must be 'bot_api' or 'webhook'")

    timeout_name = "TELEGRAM_TIMEOUT"
    raw_timeout = settings.get(timeout_name, settings.get("N8N_TIMEOUT", "10")).strip() or "10"
    try:
        timeout = int(raw_timeout)
    except ValueError as exc:
        raise ValueError("TELEGRAM_TIMEOUT must be an integer") from exc
    if not 1 <= timeout <= 60:
        raise ValueError("TELEGRAM_TIMEOUT must be between 1 and 60 seconds")

    if mode == "bot_api":
        if webhook_url or webhook_secret:
            raise ValueError("Do not configure webhook values when TELEGRAM_MODE=bot_api")
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required for Telegram Bot API mode")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", bot_token):
            raise ValueError("TELEGRAM_BOT_TOKEN has an invalid format")
        if not chat_id or len(chat_id) > 128 or any(
            not char.isprintable() or char.isspace() for char in chat_id
        ):
            raise ValueError("TELEGRAM_CHAT_ID must be a non-empty chat ID without whitespace")
        return {
            "TELEGRAM_MODE": "bot_api",
            "TELEGRAM_BOT_TOKEN": bot_token,
            "TELEGRAM_CHAT_ID": chat_id,
            "TELEGRAM_TIMEOUT": timeout,
        }

    if bot_token or chat_id:
        raise ValueError("Do not configure bot token or chat ID when TELEGRAM_MODE=webhook")
    if not webhook_url:
        raise ValueError("TELEGRAM_WEBHOOK_URL is required for Telegram webhook mode")
    if not webhook_secret:
        raise ValueError("TELEGRAM_WEBHOOK_SECRET is required for Telegram webhook mode")
    if len(webhook_secret) < 32 or any(
        not 33 <= ord(char) <= 126 for char in webhook_secret
    ):
        raise ValueError(
            "TELEGRAM_WEBHOOK_SECRET must contain at least 32 printable ASCII characters"
        )

    try:
        parsed = urlsplit(webhook_url)
        valid_url = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not any(char.isspace() or ord(char) < 32 for char in webhook_url)
            and parsed.username is None
            and parsed.password is None
            and parsed.query == ""
            and parsed.fragment == ""
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError(
            "TELEGRAM_WEBHOOK_URL must be a valid HTTPS URL without credentials, query, or fragment"
        )

    return {
        "TELEGRAM_MODE": "webhook",
        "TELEGRAM_WEBHOOK_URL": webhook_url,
        "TELEGRAM_WEBHOOK_SECRET": webhook_secret,
        "TELEGRAM_TIMEOUT": timeout,
    }


def validate_notification_settings(settings: Mapping[str, str]) -> dict[str, object]:
    """Validate configured channels and require at least one notification path."""
    email_fields = ("EMAIL_FROM", "EMAIL_TO", "SMTP_USERNAME", "SMTP_PASSWORD")
    email_configured = any(settings.get(key, "").strip() for key in email_fields)
    email = validate_email_settings(settings) if email_configured else {}
    telegram = validate_telegram_settings(settings)
    if not email and not telegram:
        raise ValueError("Configure at least one notification channel: email or Telegram")
    return {"email": email, "telegram": telegram}


def check_site(site: dict[str, str], timeout: float) -> dict[str, object]:
    """Check a URL with curl, follow HTTP(S) redirects, and summarize safely."""
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--proto",
        "=http,https",
        "--proto-redir",
        "=http,https",
        "--max-redirs",
        "10",
        "--max-time",
        str(timeout),
        "--output",
        "/dev/null",
        "--write-out",
        "%{http_code}\t%{url_effective}\t%{num_redirects}",
        site["url"],
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return {"healthy": False, "detail": f"Could not run curl: {exc}"}

    output = result.stdout.strip().split("\t")
    status = output[0] if output else "000"
    final_url = output[1] if len(output) > 1 else ""
    try:
        redirects = int(output[2]) if len(output) > 2 else 0
    except ValueError:
        redirects = 0

    if result.returncode != 0:
        errors = {
            5: "proxy DNS lookup failed",
            6: "DNS lookup failed",
            7: "connection failed",
            28: "request timed out",
            35: "TLS handshake failed",
            47: "too many redirects",
            51: "TLS certificate verification failed",
            58: "TLS client certificate problem",
            59: "TLS cipher configuration failed",
            60: "TLS certificate verification failed",
        }
        return {
            "healthy": False,
            "detail": errors.get(result.returncode, f"curl failed (exit code {result.returncode})"),
            "http_status": status,
            "redirects": redirects,
            "final_host": safe_host(final_url),
        }

    final_host = safe_host(final_url)
    if status != "200":
        return {
            "healthy": False,
            "detail": f"HTTP {status or 'unknown'}",
            "http_status": status,
            "redirects": redirects,
            "final_host": final_host,
        }

    return {
        "healthy": True,
        "detail": "HTTP 200",
        "http_status": status,
        "redirects": redirects,
        "final_host": final_host,
    }


def send_email(site_name: str, event: str, result: dict[str, object]) -> None:
    """Send one DOWN or RECOVERY message using SMTP environment settings."""
    settings = validate_email_settings(os.environ)
    security = settings["SMTP_SECURITY"]
    port = settings["SMTP_PORT"]
    username = settings["SMTP_USERNAME"]
    password = settings["SMTP_PASSWORD"]

    message = EmailMessage()
    message["From"] = settings["EMAIL_FROM"]
    message["To"] = settings["EMAIL_TO"]
    message["Subject"] = f"Website {event}: {site_name}"

    redirect_note = ""
    if result.get("redirects", 0):
        redirect_note = (
            f"Redirects followed: {result['redirects']}\n"
            f"Final host: {result.get('final_host', 'unknown')}\n"
        )

    if event == "down":
        message.set_content(
            f"Monitoring confirmed that {site_name} is unavailable.\n"
            f"Consecutive failed checks: {result['consecutive_failures']}\n"
            f"Latest result: {result['detail']}\n"
            f"{redirect_note}"
        )
    else:
        message.set_content(
            f"The first successful check confirms that {site_name} has recovered.\n"
            f"Latest result: {result['detail']}\n"
            f"{redirect_note}"
        )

    context = ssl.create_default_context()
    if security == "ssl":
        with smtplib.SMTP_SSL(
            host=settings["SMTP_HOST"], port=port, timeout=15, context=context
        ) as client:
            if username:
                client.login(username, password)
            client.send_message(message)
    else:
        with smtplib.SMTP(host=settings["SMTP_HOST"], port=port, timeout=15) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            if username:
                client.login(username, password)
            client.send_message(message)


class TelegramDeliveryError(Exception):
    """A sanitized Telegram delivery failure that never exposes credentials."""


class WebhookDeliveryError(TelegramDeliveryError):
    """A workflow-webhook delivery failure."""


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Do not forward authentication material to a redirected host."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def send_telegram_webhook(
    notification: dict[str, object], settings: Mapping[str, str | int]
) -> None:
    """POST one notification to a workflow using a shared-secret header."""
    webhook_url = settings.get(
        "TELEGRAM_WEBHOOK_URL", settings.get("N8N_WEBHOOK_URL", "")
    )
    webhook_secret = settings.get(
        "TELEGRAM_WEBHOOK_SECRET", settings.get("N8N_WEBHOOK_SECRET", "")
    )
    timeout = settings.get("TELEGRAM_TIMEOUT", settings.get("N8N_TIMEOUT", 10))
    try:
        request = urllib_request.Request(
            str(webhook_url),
            data=json.dumps(notification, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": str(webhook_secret),
            },
            method="POST",
        )
        opener = urllib_request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=int(timeout)) as response:
            if not 200 <= response.status < 300:
                raise WebhookDeliveryError(f"Telegram workflow returned HTTP {response.status}")
            # Consume the bounded response stream before closing the connection.
            response.read(4096)
    except urllib_error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise WebhookDeliveryError(f"Telegram workflow returned HTTP {status}") from None
    except urllib_error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, ssl.SSLError)):
            description = "connection timed out" if isinstance(reason, TimeoutError) else "TLS connection failed"
        else:
            description = "connection failed"
        raise WebhookDeliveryError(f"Telegram workflow {description}") from None
    except TimeoutError:
        raise WebhookDeliveryError("Telegram workflow timed out") from None
    except (ValueError, UnicodeError):
        raise WebhookDeliveryError("Telegram workflow request configuration is invalid") from None
    except OSError:
        raise WebhookDeliveryError("Telegram workflow connection failed") from None


def telegram_message_text(notification: Mapping[str, object]) -> str:
    """Format a plain-text DOWN or RECOVERY message for the Telegram Bot API."""
    status = str(notification.get("status", ""))
    if status == "DOWN":
        title = "🚨 CRITICAL — Website DOWN"
    elif status == "RECOVERY":
        title = "✅ Website RECOVERY"
    else:
        raise TelegramDeliveryError("Telegram notification has an invalid status")

    detail = str(notification.get("detail", "Unknown result"))
    lines = [
        title,
        f"Server: {notification.get('server', 'unknown')}",
        f"Site: {notification.get('service', 'unknown')}",
        f"Detail: {detail}",
        f"Time: {notification.get('timestamp', 'unknown')}",
    ]
    if notification.get("http_status"):
        lines.append(f"HTTP status: {notification['http_status']}")
    if notification.get("redirects", 0):
        lines.append(f"Redirects: {notification['redirects']}")
        lines.append(f"Final host: {notification.get('final_host', 'unknown')}")
    if notification.get("consecutive_failures", 0):
        lines.append(f"Consecutive failures: {notification['consecutive_failures']}")
    lines.append(f"Event ID: {notification.get('event_id', 'unknown')}")
    text = "\n".join(lines)
    return text if len(text) <= 4096 else f"{text[:4093]}..."


def send_telegram_bot_api(
    notification: dict[str, object], settings: Mapping[str, str | int]
) -> None:
    """Send one notification directly to the Telegram Bot API."""
    token = str(settings["TELEGRAM_BOT_TOKEN"])
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": settings["TELEGRAM_CHAT_ID"],
        "text": telegram_message_text(notification),
    }
    try:
        request = urllib_request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib_request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=int(settings["TELEGRAM_TIMEOUT"])) as response:
            if not 200 <= response.status < 300:
                raise TelegramDeliveryError(
                    f"Telegram Bot API returned HTTP {response.status}"
                )
            try:
                result = json.loads(response.read(65536))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise TelegramDeliveryError(
                    "Telegram Bot API returned an invalid response"
                ) from None
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise TelegramDeliveryError("Telegram Bot API rejected the message")
    except urllib_error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise TelegramDeliveryError(
            f"Telegram Bot API returned HTTP {status}"
        ) from None
    except urllib_error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, ssl.SSLError)):
            description = (
                "connection timed out"
                if isinstance(reason, TimeoutError)
                else "TLS connection failed"
            )
        else:
            description = "connection failed"
        raise TelegramDeliveryError(f"Telegram Bot API {description}") from None
    except TimeoutError:
        raise TelegramDeliveryError("Telegram Bot API timed out") from None
    except (ValueError, UnicodeError):
        raise TelegramDeliveryError(
            "Telegram Bot API request configuration is invalid"
        ) from None
    except OSError:
        raise TelegramDeliveryError("Telegram Bot API connection failed") from None


def send_telegram_notification(
    notification: dict[str, object], settings: Mapping[str, str | int]
) -> None:
    """Send through the selected direct Bot API or workflow webhook mode."""
    if settings.get("TELEGRAM_MODE") == "bot_api":
        send_telegram_bot_api(notification, settings)
    else:
        send_telegram_webhook(notification, settings)


def make_notification(
    site_name: str,
    event: str,
    result: dict[str, object],
    channels: dict[str, str],
) -> dict[str, object]:
    """Create a durable event with a stable ID and channel-specific statuses."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    server = os.environ.get("MONITOR_SERVER_NAME", "").strip() or socket.gethostname()
    return {
        "event_id": str(uuid.uuid4()),
        "event": event,
        "timestamp": now,
        "server": server[:255] or "unknown",
        "service": site_name,
        "status": "DOWN" if event == "down" else "RECOVERY",
        "severity": "CRITICAL" if event == "down" else "INFO",
        "channels": {channel: "pending" for channel in channels},
        "result": result,
    }


def notification_payload(notification: dict[str, object]) -> dict[str, object]:
    """Build the workflow payload while keeping delivery bookkeeping internal."""
    result = notification["result"]
    assert isinstance(result, dict)
    return {
        "event_id": notification["event_id"],
        "server": notification["server"],
        "service": notification["service"],
        "status": notification["status"],
        "severity": notification["severity"],
        "timestamp": notification["timestamp"],
        "detail": result.get("detail", "Unknown result"),
        "http_status": result.get("http_status", ""),
        "redirects": result.get("redirects", 0),
        "final_host": result.get("final_host", "unknown"),
        "consecutive_failures": result.get("consecutive_failures", 0),
    }


def deliver_pending_notifications(
    site_name: str,
    current: dict[str, object],
    state: dict[str, dict[str, object]],
    channels: dict[str, object],
) -> bool:
    """Retry each channel in order, without one channel blocking another."""
    pending = current.get("pending_notifications", [])
    if not isinstance(pending, list):
        return False

    senders = {
        "email": lambda item: send_email(
            site_name, str(item["event"]), item["result"]
        ),
        "telegram": lambda item: send_telegram_notification(
            notification_payload(item), channels["telegram"]
        ),
    }
    had_error = False
    for channel_name, sender in senders.items():
        if not channels.get(channel_name):
            continue
        for notification in list(pending):
            statuses = notification.get("channels")
            if not isinstance(statuses, dict) or statuses.get(channel_name) != "pending":
                continue
            try:
                sender(notification)
            except (OSError, ValueError, smtplib.SMTPException, TelegramDeliveryError) as exc:
                LOGGER.error(
                    "%s %s notification %s failed: %s",
                    site_name,
                    channel_name,
                    notification.get("event_id", "unknown"),
                    exc,
                )
                had_error = True
                # Preserve FIFO delivery for this channel, while trying the other channel.
                break

            statuses[channel_name] = "sent"
            if notification.get("event") in ("down", "recovery"):
                current["alerted_down"] = notification["event"] == "down"
            try:
                save_state(state)
            except OSError as exc:
                LOGGER.error("%s state save failed after %s delivery: %s", site_name, channel_name, exc)
                had_error = True
                break
            LOGGER.info(
                "%s %s notification %s sent for %s",
                site_name,
                channel_name,
                notification.get("event_id", "unknown"),
                notification.get("event", "unknown"),
            )

    remaining = [
        notification
        for notification in pending
        if not isinstance(notification.get("channels"), dict)
        or any(value != "sent" for value in notification["channels"].values())
    ]
    if remaining != pending:
        current["pending_notifications"] = remaining
        try:
            save_state(state)
        except OSError as exc:
            LOGGER.error("%s state save failed after queue cleanup: %s", site_name, exc)
            had_error = True
    return had_error


def transition(
    previous: dict[str, object], healthy: bool, threshold: int
) -> tuple[dict[str, object], str | None]:
    """Update one site's failure count and determine whether to notify."""
    failures = previous.get("consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        failures = 0
    alerted_down = previous.get("alerted_down") is True
    pending_notifications = previous.get("pending_notifications", [])
    logical_down = alerted_down
    if isinstance(pending_notifications, list) and pending_notifications:
        latest = pending_notifications[-1]
        if isinstance(latest, dict) and latest.get("event") in ("down", "recovery"):
            logical_down = latest["event"] == "down"

    if healthy:
        event = "recovery" if logical_down else None
        return {
            "consecutive_failures": 0,
            "alerted_down": alerted_down,
            "pending_notifications": pending_notifications,
        }, event

    failures += 1
    event = "down" if failures >= threshold and not logical_down else None
    return {
        "consecutive_failures": failures,
        "alerted_down": alerted_down,
        "pending_notifications": pending_notifications,
    }, event


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if shutil.which("curl") is None:
        LOGGER.error("curl is not installed or not available in PATH")
        return 2

    try:
        sites, timeout, threshold = load_config()
        state = load_state()
        channels = validate_notification_settings(os.environ)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    run_error = False
    for site in sites:
        result = check_site(site, timeout)
        healthy = result["healthy"] is True
        redirects = result.get("redirects", 0)
        final_host = result.get("final_host", "unknown")

        if redirects:
            LOGGER.info(
                "%s: followed %s redirect(s); final host=%s",
                site["name"],
                redirects,
                final_host,
            )
        LOGGER.info("%s: %s (%s)", site["name"], "UP" if healthy else "check failed", result["detail"])

        current, event = transition(state.get(site["name"], {}), healthy, threshold)
        pending_notifications = list(current["pending_notifications"])
        if event:
            event_result = {
                "detail": result["detail"],
                "http_status": result.get("http_status", ""),
                "redirects": redirects,
                "final_host": final_host,
                "consecutive_failures": current["consecutive_failures"],
            }
            configured_channels = {
                channel_name: "pending"
                for channel_name in ("email", "telegram")
                if channels.get(channel_name)
            }
            pending_notifications.append(
                make_notification(site["name"], event, event_result, configured_channels)
            )
        current["pending_notifications"] = pending_notifications
        state[site["name"]] = current
        try:
            save_state(state)
            run_error = deliver_pending_notifications(
                site["name"], current, state, channels
            ) or run_error
        except (OSError, ValueError, smtplib.SMTPException, TelegramDeliveryError) as exc:
            LOGGER.error("%s notification/state error: %s", site["name"], exc)
            run_error = True

    return 1 if run_error else 0


if __name__ == "__main__":
    sys.exit(main())
