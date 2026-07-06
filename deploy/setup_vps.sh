#!/usr/bin/env bash
# setup_vps.sh -- one-time (idempotent) VPS provisioning for memebot.
# Run ON the VPS as root. Normally invoked remotely by deploy.sh --setup.
#
# Usage: bash /opt/memebot/deploy/setup_vps.sh [--with-tailscale]
#
# Does: apt packages, uv, /opt/memebot, ufw (SSH only), optional tailscale,
# systemd unit install + enable. Never prints secrets.

set -euo pipefail

WITH_TAILSCALE=0
for arg in "$@"; do
  case "$arg" in
    --with-tailscale) WITH_TAILSCALE=1 ;;
    *) echo "unknown flag: $arg (only --with-tailscale is supported)" >&2; exit 1 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

echo "==> [1/6] apt packages (curl rsync ufw sqlite3)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl rsync ufw sqlite3

echo "==> [2/6] uv"
if ! command -v uv >/dev/null 2>&1 && [[ ! -x /usr/local/bin/uv ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# the astral installer puts uv in ~/.local/bin; make it visible to non-interactive shells
for candidate in /root/.local/bin/uv "$HOME/.local/bin/uv"; do
  if [[ -x "$candidate" && ! -e /usr/local/bin/uv ]]; then
    ln -sf "$candidate" /usr/local/bin/uv
  fi
done
/usr/local/bin/uv --version

echo "==> [3/6] /opt/memebot"
mkdir -p /opt/memebot /opt/memebot/runs /opt/memebot/backups

echo "==> [4/6] firewall: deny all incoming except SSH (dashboard stays private on 127.0.0.1)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
ufw status verbose | head -n 10

if [[ "$WITH_TAILSCALE" -eq 1 ]]; then
  echo "==> [5/6] tailscale"
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  echo "    tailscale installed. NOW RUN: tailscale up"
  echo "    then for a private HTTPS dashboard URL: tailscale serve --bg 8000"
else
  echo "==> [5/6] tailscale skipped (re-run with --with-tailscale to install)"
fi

echo "==> [6/6] systemd units"
if compgen -G "/opt/memebot/deploy/*.service" > /dev/null; then
  cp /opt/memebot/deploy/*.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable memebot-engine memebot-dashboard
  echo "    enabled: memebot-engine memebot-dashboard"
else
  echo "    WARNING: no unit files at /opt/memebot/deploy/*.service yet." >&2
  echo "    Run ./deploy/deploy.sh from the Mac first, then re-run this script." >&2
fi

if [[ ! -f /opt/memebot/.env ]]; then
  echo ""
  echo "!! /opt/memebot/.env is MISSING -- services are enabled but were NOT started."
  echo "   Push it from the Mac (deploy.sh syncs it with mode 600):"
  echo "     ./deploy/deploy.sh root@<this-host>"
else
  echo ""
  echo "==> .env present. Services start/restart on the next deploy, or start now with:"
  echo "    systemctl restart memebot-engine memebot-dashboard"
fi

echo "==> setup complete (idempotent -- safe to re-run)"
