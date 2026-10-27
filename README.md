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
- Sends one DOWN email after the threshold is reached, then one RECOVERY email
  after the first successful check.
- Saves state and unsent notifications, and retries pending emails in order if
  SMTP is temporarily unavailable.
- Writes logs to the systemd journal and runs as the unprivileged
  `website-monitor` account.

## Requirements

- Ubuntu Server 24.04 LTS, or another Linux distribution with systemd.
- Python 3.10 or newer, `curl`, `git`, Bash, systemd, and the standard user
  and group management commands available on Ubuntu Server.
- A normal user account with `sudo` access.
- Outbound network access for DNS, HTTP/HTTPS checks, and SMTP. The monitor
  does not need any inbound ports.

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

## 3. Configure email notifications

Create the private SMTP settings file and open it:

```sh
cp config/notifications.env.example config/notifications.env
chmod 600 config/sites.json config/notifications.env
nano config/notifications.env
```

Fill in the sender, recipient, and SMTP login details. For Gmail, the settings
normally look like this; replace the example addresses and password:

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
the Gmail sender address.

For another SMTP provider, use that provider's server, port, and security
settings. `SMTP_SECURITY` accepts `starttls` or `ssl`. The username and password
must either both be filled in or both be left empty. `SMTP_HOST`, `EMAIL_FROM`,
and `EMAIL_TO` are required.

Replace the values after `=` in the sample file; keep the variable names. Save
in Nano with **Ctrl+O**, press **Enter**, then exit with **Ctrl+X**.

These local files contain installation-specific information. Do not publish or
send them to anyone. They are excluded by `.gitignore`. The installed SMTP file
will be owned by root and have mode `0600`.

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
it validates the site and SMTP settings. It stops if required email values are
missing or invalid, without printing their values. It does not replace settings
already installed on that machine. It does not prompt for site or email values;
it uses the configuration files you edited in the previous steps.

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
Emails are sent only when the site changes state: a failure must reach the
configured threshold for DOWN, and a successful check after a notified outage
triggers RECOVERY.

## Configuration reference

`config/sites.json` supports:

| Setting | Meaning |
| --- | --- |
| `timeout_seconds` | Maximum request time; greater than 0 and at most 3600 |
| `failure_threshold` | Consecutive failures before a DOWN email; positive integer |
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

## Change settings or update the program

After installation, the monitor reads its site settings from
`/opt/open-source-monitoring/config/sites.json` and SMTP settings from
`/etc/website-monitor/notifications.env`. Edit those installed files with
`sudo nano` if you want to change the running installation. Protect the SMTP
file; it contains a password.

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
- The real site and SMTP settings are excluded by `.gitignore` and copied to
  their system paths with restricted permissions.
- Do not share screenshots or files that reveal SMTP credentials.
