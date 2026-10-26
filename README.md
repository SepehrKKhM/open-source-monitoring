# Open Source Website Monitoring

A lightweight website monitor for Ubuntu Server. It uses Python's standard
library and `curl`; no database, container runtime, or third-party Python
packages are required.

## Behavior

- Checks each configured HTTP or HTTPS URL with `curl`.
- Follows HTTP(S) redirects. Redirect count and final hostname are logged; a
  redirect alone does not trigger an email.
- Treats only a final HTTP `200` as healthy.
- Uses a 10-second request timeout and a threshold of two consecutive failures
  by default.
- Sends one DOWN email when the threshold is reached, then one RECOVERY email
  after the first successful check.
- Runs from a systemd timer every minute. Logs go to journald; persistent
  notification state is stored at `/var/lib/website-monitor/state.json`.
- Runs as the unprivileged `website-monitor` service account.

## Requirements

- Ubuntu Server 24.04 LTS or another Linux system using systemd.
- Python 3, `curl`, systemd, Bash, and the standard account utilities
  (`useradd`, `groupadd`, `usermod`, `getent`, `install`, and `runuser`).
- An account with `sudo` access for installation.
- Outbound DNS and network access to the monitored sites and SMTP server. The
  monitor does not need an inbound port.

The installer checks for Python 3, `curl`, systemd, and `runuser`; it does not
install missing packages. If `curl` is missing on Ubuntu, install it first:

```sh
sudo apt update
sudo apt install -y curl
```

## Configure and install

Run the following commands from the project directory. These commands create
local configuration files. Do not commit the real files or share them with
anyone; `.gitignore` excludes them.

```sh
cp config/sites.example.json config/sites.json
cp config/notifications.env.example config/notifications.env
chmod 600 config/sites.json config/notifications.env
```

Edit `config/sites.json` with the site names and URLs. Edit
`config/notifications.env` with the SMTP settings and email addresses. Gmail
uses `smtp.gmail.com`, port `587`, and `starttls`; use an App Password if Gmail
requires one. Enter the App Password without spaces.

Validate the JSON and install:

```sh
python3 -m json.tool config/sites.json > /dev/null
sudo ./scripts/install.sh
```

The installer creates the service user and group, prepares the application and
state directories, installs the program and systemd units, copies settings
with restricted permissions, reloads systemd, and enables the timer. On repeat
runs it updates the program and units while preserving the installed site and
SMTP settings.

Installed locations:

- Program and site configuration: `/opt/open-source-monitoring/`
- SMTP settings: `/etc/website-monitor/notifications.env` (root-owned, mode
  `0600`)
- Persistent state: `/var/lib/website-monitor/state.json`
- Service: `/etc/systemd/system/website-monitor.service`
- Timer: `/etc/systemd/system/website-monitor.timer`

## Configuration

The site file has this structure:

```json
{
  "timeout_seconds": 10,
  "failure_threshold": 2,
  "sites": [
    {"name": "site-one", "url": "https://example.com"},
    {"name": "site-two", "url": "https://example.org"}
  ]
}
```

Each site needs a unique name and a valid HTTP or HTTPS URL without embedded
credentials. `timeout_seconds` must be greater than zero and at most 3600;
`failure_threshold` must be a positive integer. A non-200 response or a
connection, DNS, TLS, or timeout error counts as a failure.

The SMTP environment file supports:

| Variable | Purpose |
| --- | --- |
| `SMTP_HOST` | SMTP server, such as `smtp.gmail.com` |
| `SMTP_PORT` | Port; normally `587` for STARTTLS or `465` for SSL |
| `SMTP_SECURITY` | `starttls` or `ssl` |
| `SMTP_USERNAME` | SMTP login name; optional when login is not required |
| `SMTP_PASSWORD` | SMTP password or app password; set together with username |
| `EMAIL_FROM` | Sender address |
| `EMAIL_TO` | Alert recipient address |

`SMTP_HOST`, `EMAIL_FROM`, and `EMAIL_TO` are required. The username and
password must either both be set or both be empty.

## Verify and view logs

The timer should show `active (waiting)` and a next-run time:

```sh
systemctl status website-monitor.timer --no-pager
systemctl list-timers --all | grep website-monitor
```

View recent service output:

```sh
sudo journalctl -u website-monitor.service -n 30 --no-pager
```

To run a check immediately:

```sh
sudo systemctl start website-monitor.service
```

The service is `Type=oneshot`; it may show `inactive (dead)` after finishing.
That is expected. The timer should remain `active (waiting)`.

## Run the unit tests

From the project directory:

```sh
python3 -m unittest discover -s tests -v
```

## Security notes

- `config/sites.json`, `config/notifications.env`, and runtime state are
  excluded by `.gitignore`.
- The installer puts SMTP settings in a root-only file with mode `0600`; site
  settings are readable by the service account and not writable by it.
- The systemd service runs without root privileges. Only the timer is enabled.
