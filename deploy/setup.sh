#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu/Debian VM to run the ORB bot as an always-on
# systemd service (plus its settings UI, once you've set a password for it).
#
# Usage - clone the repo directly to /opt/orb-bot, then run this from inside
# that checkout:
#
#   sudo mkdir -p /opt/orb-bot && sudo chown "$USER" /opt/orb-bot
#   git clone <your-repo-url> /opt/orb-bot
#   cd /opt/orb-bot
#   sudo bash deploy/setup.sh
#
# Safe to re-run: it only creates the service user/.env if they don't
# already exist, and always refrehes the venv/package install and the
# systemd unit files, so re-running after `git pull` is the normal way to
# pick up an update.
#
# Override APP_USER/APP_DIR/PYTHON_BIN as env vars if you want something
# other than the defaults these deploy/*.service files assume.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash deploy/setup.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-orb-bot}"
APP_DIR="${APP_DIR:-/opt/orb-bot}"

if [[ "$REPO_DIR" != "$APP_DIR" ]]; then
  echo "This script expects to be run from inside a clone located at" >&2
  echo "\$APP_DIR ($APP_DIR) - the deploy/*.service files hardcode that path." >&2
  echo "You ran it from $REPO_DIR instead. Either:" >&2
  echo "  git clone <repo> $APP_DIR && cd $APP_DIR && sudo bash deploy/setup.sh" >&2
  echo "or re-run with a matching APP_DIR:" >&2
  echo "  APP_DIR=$REPO_DIR sudo -E bash deploy/setup.sh" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq git ca-certificates build-essential >/dev/null

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  apt-get install -y -qq python3.11 python3.11-venv >/dev/null 2>&1 || true
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON_BIN="$candidate"
        apt-get install -y -qq "${candidate}-venv" >/dev/null 2>&1 || true
        break
      fi
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]]; then
  cat >&2 <<'MSG'
No Python 3.11+ found (and none could be installed from your distro's repos
- this bot requires it). On Ubuntu 22.04 (which ships 3.10), add the
deadsnakes PPA first:
  sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt-get update
  sudo apt-get install -y python3.11 python3.11-venv
Then re-run this script. Ubuntu 24.04+ ships Python 3.12 and doesn't need this.
MSG
  exit 1
fi
echo "    using $PYTHON_BIN ($("$PYTHON_BIN" --version))"

echo "==> Ensuring service user '$APP_USER' exists"
if ! id "$APP_USER" &>/dev/null; then
  useradd --system --create-home --shell /bin/bash "$APP_USER"
  echo "    created system user $APP_USER"
else
  echo "    user $APP_USER already exists, leaving as-is"
fi

echo "==> Setting ownership of $APP_DIR to $APP_USER"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Creating the virtualenv and installing the package"
sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "==> Creating .env from .env.example - YOU MUST EDIT THIS before the bot can do anything useful"
  sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
else
  echo "==> .env already exists - leaving your existing values in place"
fi

echo "==> Installing systemd units"
for unit in orb-bot orb-bot-webui; do
  sed -e "s#/opt/orb-bot#${APP_DIR}#g" -e "s#User=orb-bot#User=${APP_USER}#g" \
    "$APP_DIR/deploy/${unit}.service" > "/etc/systemd/system/${unit}.service"
done
systemctl daemon-reload

echo "==> Enabling and starting the trading bot (dry-run by default - see .env)"
systemctl enable --now orb-bot

if grep -qE '^ORB_WEBUI_PASSWORD=\S+' "$APP_DIR/.env"; then
  echo "==> ORB_WEBUI_PASSWORD is set - enabling the settings UI too"
  systemctl enable --now orb-bot-webui
else
  echo "==> ORB_WEBUI_PASSWORD not set in .env - skipping the settings UI service for now"
  echo "    (set it, then: sudo systemctl enable --now orb-bot-webui)"
fi

cat <<EOF

Done.

  Edit secrets/config:  sudo -u $APP_USER nano $APP_DIR/.env
  Restart after edits:  sudo systemctl restart orb-bot
  Check it's running:   sudo systemctl status orb-bot
  Watch logs live:      journalctl -u orb-bot -f
  Settings UI (run from your laptop, not the VM):
      ssh -L 8787:127.0.0.1:8787 $(logname 2>/dev/null || echo "you")@<this-vm-ip>
      then open http://127.0.0.1:8787

The bot starts in ORB_MODE=dry_run and stays there until you explicitly
change it - it will not place a real order until you set both
ORB_MODE=live and TRADOVATE_ENV=live in .env. See README.md before doing
that.
EOF
