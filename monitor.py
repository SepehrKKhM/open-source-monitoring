#!/usr/bin/env python3
"""Check configured websites and email on confirmed outages and recovery."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path
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
    """Load consecutive failure counters and notification state."""
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
        state[name] = {
            "consecutive_failures": failures,
            "alerted_down": item.get("alerted_down") is True,
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
    required = ("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError(f"Missing email settings: {', '.join(missing)}")

    security = os.environ.get("SMTP_SECURITY", "starttls").lower()
    if security not in {"starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY must be 'starttls' or 'ssl'")
    default_port = "587" if security == "starttls" else "465"
    try:
        port = int(os.environ.get("SMTP_PORT", default_port))
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535")

    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together")

    message = EmailMessage()
    message["From"] = os.environ["EMAIL_FROM"]
    message["To"] = os.environ["EMAIL_TO"]
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
            host=os.environ["SMTP_HOST"], port=port, timeout=15, context=context
        ) as client:
            if username:
                client.login(username, password)
            client.send_message(message)
    else:
        with smtplib.SMTP(host=os.environ["SMTP_HOST"], port=port, timeout=15) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            if username:
                client.login(username, password)
            client.send_message(message)


def transition(
    previous: dict[str, object], healthy: bool, threshold: int
) -> tuple[dict[str, object], str | None]:
    """Update one site's failure count and determine whether to notify."""
    failures = previous.get("consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        failures = 0
    alerted_down = previous.get("alerted_down") is True

    if healthy:
        event = "recovery" if alerted_down else None
        return {"consecutive_failures": 0, "alerted_down": alerted_down}, event

    failures += 1
    event = "down" if failures >= threshold and not alerted_down else None
    return {"consecutive_failures": failures, "alerted_down": alerted_down}, event


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if shutil.which("curl") is None:
        LOGGER.error("curl is not installed or not available in PATH")
        return 2

    try:
        sites, timeout, threshold = load_config()
        state = load_state()
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
        state[site["name"]] = current
        try:
            save_state(state)
            if event:
                email_result = dict(result)
                email_result["consecutive_failures"] = current["consecutive_failures"]
                send_email(site["name"], event, email_result)
                current["alerted_down"] = event == "down"
                save_state(state)
                LOGGER.info("%s email sent for %s", site["name"], event)
        except (OSError, ValueError, smtplib.SMTPException) as exc:
            LOGGER.error("%s notification/state error: %s", site["name"], exc)
            run_error = True

    return 1 if run_error else 0


if __name__ == "__main__":
    sys.exit(main())
