# Open Source Website Monitoring

Licensed under the MIT License. See [LICENSE](LICENSE).

A small website monitor for Ubuntu Server. It uses Python's standard library
and `curl`; it does not need Docker, a database, or third-party Python packages.

This guide starts after you have installed Ubuntu Server 24.04 LTS in a VM or
on a machine, logged in with a normal user that has `sudo` access, and connected
the machine to the internet. It does not cover creating the VM or installing
Ubuntu.

## What the monitor does

- Checks each configured HTTP or HTTPS URL once per minute.
- Follows HTTP(S) redirects and logs the redirect count and final hostname.
  A redirect by itself does not trigger an email.
- Treats only a final HTTP `200` response as healthy.
- Uses a 10-second request timeout and a threshold of two consecutive failures
  by default.
- Sends DOWN and RECOVERY notifications through email and optional Telegram.
- Supports direct Telegram Bot API delivery or a workflow webhook; n8n is one
  possible workflow platform, not a requirement.
- Saves each event and each channel's delivery status separately. A failure in
  SMTP does not block Telegram, and a Telegram failure does not block SMTP.
- Retries pending notifications in order for that channel after a temporary
  delivery failure; successful channels are not sent again during normal retry.
- Writes logs to the systemd journal and runs as the unprivileged
  `website-monitor` account.

## Requirements

- Ubuntu Server 24.04 LTS, or another Linux distribution with systemd.
- Python 3.10 or newer, `curl`, `git`, Bash, systemd, and the standard user
  and group management commands available on Ubuntu Server.
- A normal user account with `sudo` access.
- Outbound network access for DNS, HTTP/HTTPS checks, and whichever notification
  services you configure. The monitor does not need any inbound ports.

On Ubuntu Server, install the tools used to download and run the project:

```sh
sudo apt update
sudo apt install -y git curl python3
```

Ubuntu Server includes systemd and the account management tools. The installer
checks for Python 3.10 or newer, `curl`, systemd, and `runuser`; it stops with an
error if one is missing.

## 1. Download the project

Run these commands in the VM's terminal:

```sh
git clone https://github.com/SepehrKKhM/open-source-monitoring.git
cd open-source-monitoring
```

The repository must be public, or your GitHub account must have permission to
read it. These steps download the project files; they do not install or enable
the monitor yet.

## 2. Configure the websites

Make a private copy of the sample configuration:

```sh
cp config/sites.example.json config/sites.json
nano config/sites.json
```

Replace the sample names and URLs with the sites you want to monitor. Keep the
JSON punctuation, quotation marks, and commas. A site entry looks like this:

```json
{"name": "my-site", "url": "https://www.example.com"}
```

You can monitor more or fewer than two sites. Each site needs a unique name and
a complete HTTP or HTTPS URL. The default timeout is 10 seconds and the
failure threshold is 2. You can change these values in the same file:

```json
{
  "timeout_seconds": 10,
  "failure_threshold": 2,
  "sites": [
    {"name": "my-site", "url": "https://www.example.com"}
  ]
}
```

Save in Nano with **Ctrl+O**, press **Enter**, then exit with **Ctrl+X**.

## 3. Configure notification channels

Create the private settings file and open it:

```sh
cp config/notifications.env.example config/notifications.env
chmod 600 config/sites.json config/notifications.env
nano config/notifications.env
```

Email is the recommended default channel. Configure a working email sender, or
configure at least one other complete notification channel. Telegram is
optional; leave its fields empty if you do not use it.

### Email (recommended default)

For Gmail, fill the SMTP fields like this and replace the example addresses and
password:

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=monitor-sender@gmail.com
SMTP_PASSWORD=your-app-password-without-spaces
EMAIL_FROM=monitor-sender@gmail.com
EMAIL_TO=your-address@yahoo.com
```

For Gmail, `SMTP_USERNAME` and `EMAIL_FROM` should be the sender's Gmail
address. Use a Gmail App Password if required; Google requires 2-Step
Verification for App Passwords. Enter the App Password without spaces or
quotation marks. See [Google's App Password help](https://support.google.com/mail/answer/185833).
The recipient can use another mail provider, such as Yahoo; `EMAIL_FROM` remains
the Gmail sender address. Other SMTP providers can use their own host, port, and
security settings. `SMTP_SECURITY` accepts `starttls` or `ssl`. Username and
password must either both be filled or both be empty.

### Telegram directly through the Bot API (optional)

This mode sends HTTPS requests directly from the monitor to Telegram. It does
not require n8n or another workflow service.

1. Create a bot with Telegram's [@BotFather](https://t.me/BotFather) and copy
   its token privately.
2. Choose a destination. For a private chat, the user must start the bot first.
   For a team, add the bot to a group where it can send messages. Telegram bots
   can't start private conversations on their own.
3. Set `TELEGRAM_MODE=bot_api`, then fill in `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID` in `config/notifications.env`. Set `TELEGRAM_TIMEOUT=10`
   or another value from 1 to 60 seconds. Do not publish the token.

The Bot API needs a destination chat ID. For a group, use its numeric chat ID;
for a public group or channel, Telegram also accepts its `@username`. See the
[Telegram Bot API](https://core.telegram.org/bots/api#sendmessage).

### Telegram through a workflow webhook (optional, advanced)

If you already use n8n or another workflow service, choose
`TELEGRAM_MODE=webhook` and set `TELEGRAM_WEBHOOK_URL` and
`TELEGRAM_WEBHOOK_SECRET`. The URL must be HTTPS; the secret must contain at
least 32 printable characters. The monitor sends the secret in the
`X-Webhook-Secret` header and posts the event JSON. Put the Telegram bot token
in the workflow's own credential store, not in the monitor settings.

For n8n, create a `POST` Webhook with Header Auth, validate the payload, send a
Telegram message, and return success only after the send succeeds. A persistent
Data Table keyed by `event_id` is optional protection against duplicate sends
when a delivery is retried after an ambiguous network result. It is not needed
for the two-failure threshold, which the monitor handles itself. Use the
Production URL after publishing the workflow, not its temporary Test URL. See
the [n8n Webhook documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/),
[Telegram node documentation](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.telegram/),
and [Data Tables](https://docs.n8n.io/build/work-with-data/data-tables/).

Select one Telegram mode at a time. Email can be used by itself or alongside
either Telegram mode. If Telegram is not configured, email-only operation does
not depend on a bot or workflow.

Generate a workflow secret on the VM with:

```sh
openssl rand -hex 32
```

Do not put the webhook secret in the URL or JSON body. The monitor does not log
Telegram tokens, webhook URLs, or webhook secrets.

In webhook mode, the JSON body contains `event_id`, `server`, `service`,
`status`, `severity`, `timestamp`, and check details. `server` is the monitor VM
hostname (or `MONITOR_SERVER_NAME` if set); `service` is the configured website
name. DOWN has severity `CRITICAL`; RECOVERY has severity `INFO`.

Each delivery has a stable event ID, and the monitor persists each channel's
status. This prevents normal retries from resending a channel that already
reported success. In webhook mode, a workflow can also use the event ID to skip
requests it has already processed. Since sending to Telegram and saving a row
are separate operations, a crash between them can still cause a duplicate;
delivery is retryable, not exactly-once.

Replace the values after `=` in the sample file; keep the variable names. Save
in Nano with **Ctrl+O**, press **Enter**, then exit with **Ctrl+X**.

These local files contain installation-specific information. Do not publish or
send them to anyone. They are excluded by `.gitignore`. The installed settings
file will be owned by root and have mode `0600`.

## 4. Check the configuration and install

Check that the site file is valid JSON. No output means the JSON syntax is
valid:

```sh
python3 -m json.tool config/sites.json > /dev/null
```

Now install the service:

```sh
sudo ./scripts/install.sh
```

Enter your Linux user's password if `sudo` asks for it. The installer creates
the `website-monitor` system user and group, creates the application and state
directories, copies the settings with restricted permissions, installs the
systemd service and timer, and enables the timer. Before changing the system,
it validates the site and notification settings. It stops if no complete
notification channel is configured or values are invalid, without printing
secrets. It does not replace settings already installed on that machine. It
does not prompt for site or notification values; it uses the configuration
files you edited in the previous steps.

On success, the installer prints `Website monitor installed and timer enabled.`

## 5. Confirm that it is running

Check the timer and its next scheduled run:

```sh
systemctl status website-monitor.timer --no-pager
systemctl list-timers --all | grep website-monitor
```

The timer should show `active (waiting)` and a next-run time. It may run the
first check shortly after installation.

Read recent check results:

```sh
sudo journalctl -u website-monitor.service -n 30 --no-pager
```

You should see a result for each configured site. To run a check immediately:

```sh
sudo systemctl start website-monitor.service
```

The service is a `oneshot` unit, so it may show `inactive (dead)` after the
check finishes. That is normal; the timer should remain `active (waiting)`.
Notifications are sent only when the site changes state: a failure must reach
the configured threshold for DOWN, and a successful check after a notified
outage triggers RECOVERY.

## Configuration reference

`config/sites.json` supports:

| Setting | Meaning |
| --- | --- |
| `timeout_seconds` | Maximum request time; greater than 0 and at most 3600 |
| `failure_threshold` | Consecutive failures before a DOWN notification; positive integer |
| `sites` | List of unique site names and HTTP/HTTPS URLs |

A final status other than `200`, or a connection, DNS, TLS, or timeout error,
counts as a failed check.

SMTP settings:

| Variable | Meaning |
| --- | --- |
| `SMTP_HOST` | SMTP server, such as `smtp.gmail.com` |
| `SMTP_PORT` | Usually `587` for STARTTLS or `465` for SSL |
| `SMTP_SECURITY` | `starttls` or `ssl` |
| `SMTP_USERNAME` | SMTP login name; optional if login is not required |
| `SMTP_PASSWORD` | SMTP password or app password; paired with username |
| `EMAIL_FROM` | Sender address |
| `EMAIL_TO` | Alert recipient address |

Telegram settings:

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_MODE` | `bot_api` for direct Bot API or `webhook` for a workflow endpoint |
| `TELEGRAM_BOT_TOKEN` | BotFather token; used only in `bot_api` mode |
| `TELEGRAM_CHAT_ID` | Private chat, group, or channel target; used only in `bot_api` mode |
| `TELEGRAM_WEBHOOK_URL` | HTTPS URL; used only in `webhook` mode |
| `TELEGRAM_WEBHOOK_SECRET` | Shared secret sent as `X-Webhook-Secret`; used only in `webhook` mode |
| `TELEGRAM_TIMEOUT` | Telegram request timeout in seconds, from 1 to 60 |
| `MONITOR_SERVER_NAME` | Optional display name; defaults to the VM hostname |

## Change settings or update the program

After installation, the monitor reads its site settings from
`/opt/open-source-monitoring/config/sites.json` and notification settings from
`/etc/website-monitor/notifications.env`. Edit those installed files with
`sudo nano` if you want to change the running installation. This file contains
SMTP and/or Telegram secrets and must remain protected. When adding Telegram
to an existing installation, add the desired `TELEGRAM_*` values to this
installed file; the installer intentionally preserves it and does not
overwrite it with the checkout's example or local settings file.
Existing installations using `N8N_WEBHOOK_URL`, `N8N_WEBHOOK_SECRET`, and
`N8N_TIMEOUT` continue to work as webhook mode; new installations should use
the provider-neutral `TELEGRAM_*` variables described above.

To install a newer version, update the project checkout and rerun the installer:

```sh
cd ~/open-source-monitoring
git pull
sudo ./scripts/install.sh
```

The installer updates the program and systemd units, while preserving the
installed site and SMTP settings.

## Logs and tests

View logs at any time with:

```sh
sudo journalctl -u website-monitor.service --since today --no-pager
```

To run the project's unit tests from the project directory:

```sh
python3 -m unittest discover -s tests -v
```

## Security notes

- The program runs as the non-root `website-monitor` account.
- No inbound port is required; only outbound site and SMTP connections are
  used.
- The real site and notification settings are excluded by `.gitignore` and
  copied to their system paths with restricted permissions.
- Do not share screenshots or files that reveal SMTP credentials.
