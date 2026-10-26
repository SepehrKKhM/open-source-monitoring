#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="website-monitor"
SERVICE_GROUP="website-monitor"
INSTALL_ROOT="/opt/open-source-monitoring"
STATE_DIR="/var/lib/website-monitor"
CONFIG_DIR="/etc/website-monitor"
UNIT_DIR="/etc/systemd/system"

SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_SITES="${SOURCE_ROOT}/config/sites.json"
SOURCE_NOTIFICATIONS="${SOURCE_ROOT}/config/notifications.env"
INSTALLED_SITES="${INSTALL_ROOT}/config/sites.json"
INSTALLED_NOTIFICATIONS="${CONFIG_DIR}/notifications.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo: sudo ${SOURCE_ROOT}/scripts/install.sh" >&2
  exit 1
fi

for required_command in python3 curl systemctl runuser; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Required command not found: ${required_command}" >&2
    exit 1
  fi
done

for required_file in \
  "${SOURCE_ROOT}/monitor.py" \
  "${SOURCE_ROOT}/systemd/website-monitor.service" \
  "${SOURCE_ROOT}/systemd/website-monitor.timer"; do
  if [[ ! -r "${required_file}" ]]; then
    echo "Required project file is missing: ${required_file}" >&2
    exit 1
  fi
done

if [[ ! -r "${SOURCE_SITES}" && ! -r "${INSTALLED_SITES}" ]]; then
  echo "Missing site configuration. Create config/sites.json from config/sites.example.json, or install it at ${INSTALLED_SITES}." >&2
  exit 1
fi
if [[ ! -r "${SOURCE_NOTIFICATIONS}" && ! -r "${INSTALLED_NOTIFICATIONS}" ]]; then
  echo "Missing SMTP configuration. Create config/notifications.env from config/notifications.env.example, or install it at ${INSTALLED_NOTIFICATIONS}." >&2
  exit 1
fi

SITES_TO_VALIDATE="${SOURCE_SITES}"
if [[ ! -r "${SITES_TO_VALIDATE}" ]]; then
  SITES_TO_VALIDATE="${INSTALLED_SITES}"
fi
python3 -m json.tool "${SITES_TO_VALIDATE}" >/dev/null || {
  echo "Site configuration is not valid JSON: ${SITES_TO_VALIDATE}" >&2
  exit 1
}

if ! getent group "${SERVICE_GROUP}" >/dev/null; then
  groupadd --system "${SERVICE_GROUP}"
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${SERVICE_GROUP}" --home-dir "${STATE_DIR}" \
    --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
usermod --append --groups "${SERVICE_GROUP}" "${SERVICE_USER}"

install -d -o root -g "${SERVICE_GROUP}" -m 0750 \
  "${INSTALL_ROOT}" "${INSTALL_ROOT}/config"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${STATE_DIR}"
install -d -o root -g root -m 0700 "${CONFIG_DIR}"

install -o root -g "${SERVICE_GROUP}" -m 0640 \
  "${SOURCE_ROOT}/monitor.py" "${INSTALL_ROOT}/monitor.py"

# Preserve operator-specific settings during repeat installations.
if [[ ! -e "${INSTALLED_SITES}" && -r "${SOURCE_SITES}" ]]; then
  install -o root -g "${SERVICE_GROUP}" -m 0640 \
    "${SOURCE_SITES}" "${INSTALLED_SITES}"
fi
if [[ ! -e "${INSTALLED_NOTIFICATIONS}" && -r "${SOURCE_NOTIFICATIONS}" ]]; then
  install -o root -g root -m 0600 \
    "${SOURCE_NOTIFICATIONS}" "${INSTALLED_NOTIFICATIONS}"
fi

install -o root -g root -m 0644 \
  "${SOURCE_ROOT}/systemd/website-monitor.service" \
  "${UNIT_DIR}/website-monitor.service"
install -o root -g root -m 0644 \
  "${SOURCE_ROOT}/systemd/website-monitor.timer" \
  "${UNIT_DIR}/website-monitor.timer"

runuser -u "${SERVICE_USER}" -- test -r "${INSTALL_ROOT}/monitor.py"
runuser -u "${SERVICE_USER}" -- test -r "${INSTALLED_SITES}"
python3 -m json.tool "${INSTALLED_SITES}" >/dev/null

systemctl daemon-reload
systemctl enable --now website-monitor.timer

echo "Website monitor installed and timer enabled."
echo "Timer status: systemctl status website-monitor.timer --no-pager"
echo "Service logs: sudo journalctl -u website-monitor.service -n 30 --no-pager"
