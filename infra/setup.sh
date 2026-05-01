#!/usr/bin/env bash
# BetEdge NY — Lightsail provisioning script.
#
# Run this on a fresh Ubuntu 22.04 Lightsail instance. It:
#   1. Installs Python 3.13, Node 20, Caddy, Tailscale, sqlite3, ufw
#   2. Joins the box to your tailnet (private-only access)
#   3. Generates a GitHub deploy key (re-run this script after adding it)
#   4. Clones the repo, builds frontend, sets up systemd + Caddy
#   5. Locks the firewall down to tailnet + SSH
#
# Idempotent — safe to re-run.
#
# Required env vars:
#   TAILSCALE_AUTH_KEY  — generate at https://login.tailscale.com/admin/settings/keys
#                         (one-off, ephemeral=no, reusable=no, tagged or untagged)
# Optional:
#   REPO_URL  — defaults to git@github.com:stellarsphereai/betedge.git
#   APP_DIR   — defaults to /opt/betedge
#   APP_USER  — defaults to betedge

set -euo pipefail

: "${TAILSCALE_AUTH_KEY:?Set TAILSCALE_AUTH_KEY before running. Generate at https://login.tailscale.com/admin/settings/keys}"
REPO_URL="${REPO_URL:-git@github.com:stellarsphereai/betedge.git}"
APP_DIR="${APP_DIR:-/opt/betedge}"
APP_USER="${APP_USER:-betedge}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
log "0/10  Adding 2 GB swap (npm run build is borderline on 1 GB RAM boxes)"
# ---------------------------------------------------------------------------
if ! sudo swapon --show | grep -q '/swapfile'; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    if ! grep -q '/swapfile' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    fi
fi
sudo sysctl -w vm.swappiness=10 >/dev/null
if ! grep -q '^vm.swappiness' /etc/sysctl.conf; then
    echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf >/dev/null
fi

# ---------------------------------------------------------------------------
log "1/10  Installing system packages"
# ---------------------------------------------------------------------------
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    software-properties-common ca-certificates gnupg curl git sqlite3 \
    build-essential ufw debian-keyring debian-archive-keyring apt-transport-https \
    python3 python3-venv python3-dev
# Note: we use system python3 (3.10 on Ubuntu 22.04) instead of python3.13 from
# deadsnakes — the backend's deps don't need 3.13, and launchpad.net is not
# always reachable from Lightsail's network.

# Node 20
if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs
fi

# Caddy
if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
fi

# ---------------------------------------------------------------------------
log "2/10  Installing Tailscale + joining tailnet"
# ---------------------------------------------------------------------------
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sudo sh
fi
# Re-running tailscale up is idempotent
sudo tailscale up \
    --authkey="$TAILSCALE_AUTH_KEY" \
    --hostname="betedge" \
    --ssh \
    --accept-routes
TAILSCALE_IP=$(tailscale ip --4 | head -1)
log "    tailnet IP: $TAILSCALE_IP"

# ---------------------------------------------------------------------------
log "3/10  Creating app user + directory"
# ---------------------------------------------------------------------------
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    sudo useradd -m -s /bin/bash "$APP_USER"
fi
sudo mkdir -p "$APP_DIR"
sudo chown "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------------------------------
log "4/10  GitHub deploy key (generate or detect)"
# ---------------------------------------------------------------------------
KEY_PATH="/home/$APP_USER/.ssh/id_ed25519"
sudo -u "$APP_USER" mkdir -p "/home/$APP_USER/.ssh"
sudo -u "$APP_USER" chmod 700 "/home/$APP_USER/.ssh"

if ! sudo test -f "$KEY_PATH"; then
    sudo -u "$APP_USER" ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "betedge-deploy@lightsail" >/dev/null
    echo
    echo "════════════════════════════════════════════════════════════════════"
    echo "  ADD THIS DEPLOY KEY TO YOUR GITHUB REPO, THEN RE-RUN THIS SCRIPT"
    echo "  https://github.com/stellarsphereai/betedge/settings/keys/new"
    echo "  ⚠ Do NOT enable 'Allow write access' — read-only is enough."
    echo "════════════════════════════════════════════════════════════════════"
    echo
    sudo cat "${KEY_PATH}.pub"
    echo
    echo "════════════════════════════════════════════════════════════════════"
    exit 0
fi

# Trust github.com host key on first connect
sudo -u "$APP_USER" bash -c "ssh-keyscan -t ed25519,rsa github.com >> /home/$APP_USER/.ssh/known_hosts 2>/dev/null"
sudo -u "$APP_USER" chmod 600 "/home/$APP_USER/.ssh/known_hosts"

# Verify the deploy key works before continuing.
# `ssh -T git@github.com` always exits 1 (GitHub denies shell access), and
# `set -o pipefail` propagates that — so we capture output to a variable
# instead of piping to grep.
SSH_OUT=$(sudo -u "$APP_USER" ssh -o StrictHostKeyChecking=no -T git@github.com 2>&1 || true)
if [[ "$SSH_OUT" != *"successfully authenticated"* ]]; then
    echo "Deploy key not yet recognized by GitHub. Add it at:"
    echo "  https://github.com/stellarsphereai/betedge/settings/keys/new"
    echo "Then re-run this script."
    echo "  ssh response was: $SSH_OUT"
    exit 1
fi

# ---------------------------------------------------------------------------
log "5/10  Cloning / updating repository"
# ---------------------------------------------------------------------------
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
else
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
fi

# ---------------------------------------------------------------------------
log "6/10  Backend Python venv + dependencies"
# ---------------------------------------------------------------------------
if [ ! -d "$APP_DIR/backend/.venv" ]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/backend/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/backend/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/backend/.venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"

# ---------------------------------------------------------------------------
log "7/10  Frontend build (vite → dist/)"
# ---------------------------------------------------------------------------
sudo -u "$APP_USER" bash -lc "cd '$APP_DIR/frontend' && npm ci --silent && npm run build"

# ---------------------------------------------------------------------------
log "8/10  systemd unit for the FastAPI backend"
# ---------------------------------------------------------------------------
sudo tee /etc/systemd/system/betedge.service >/dev/null <<UNIT
[Unit]
Description=BetEdge NY backend (FastAPI + APScheduler)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$APP_DIR/backend/.env
ExecStart=$APP_DIR/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8002
Restart=always
RestartSec=5
# Don't crash-loop forever
StartLimitBurst=10
StartLimitIntervalSec=60

[Install]
WantedBy=multi-user.target
UNIT

# ---------------------------------------------------------------------------
log "9/10  Caddy config (HTTP on port 80, tailnet-only via UFW)"
# ---------------------------------------------------------------------------
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
:80 {
    # API routes proxied to FastAPI on 127.0.0.1:8002.
    # Caddy's named matchers only honor the LAST 'path' directive, so all
    # patterns must be on a single line.
    @api path /predictions* /ev-bets* /bets /bets/* /stats* /fixtures* /run-model /digest-preview /send-digest /backtest* /sync-data* /quota /scheduler* /anomalies* /model-health* /league-config* /portfolio*
    handle @api {
        reverse_proxy 127.0.0.1:8002
    }

    @admin_api path /admin/sync* /admin/scheduler* /admin/health /admin/calibrat* /admin/accuracy* /admin/wc/*
    handle @admin_api {
        reverse_proxy 127.0.0.1:8002
    }

    # Static frontend with SPA fallback so React Router can render /admin etc.
    handle {
        root * $APP_DIR/frontend/dist
        try_files {path} /index.html
        file_server
    }

    encode gzip

    log {
        output file /var/log/caddy/access.log
        format console
    }
}
CADDY
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy

# ---------------------------------------------------------------------------
log "10/10 Firewall (allow SSH + tailnet, block everything else public)"
# ---------------------------------------------------------------------------
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH                      # public SSH for ops convenience
sudo ufw allow in on tailscale0             # all tailnet traffic
sudo ufw --force enable

# Public 80/8002 are NOT explicitly allowed → blocked. Tailnet reaches them
# via the tailscale0 rule above.

sudo systemctl daemon-reload
sudo systemctl enable --now betedge.service caddy.service

echo
echo "════════════════════════════════════════════════════════════════════"
echo "✓ Setup complete."
echo
echo "  Tailnet IP:        $TAILSCALE_IP"
echo "  Tailnet hostname:  betedge.<your-tailnet>.ts.net"
echo "  Dashboard URL:     http://$TAILSCALE_IP/  (from any device on the tailnet)"
echo
echo "  ▸ Next: copy your secrets + DB from your laptop:"
echo "      scp ~/betedge-ny/.env              ubuntu@$TAILSCALE_IP:/tmp/.env"
echo "      scp ~/betedge-ny/backend/betedge.db ubuntu@$TAILSCALE_IP:/tmp/betedge.db"
echo
echo "  ▸ Then on this box, install them:"
echo "      sudo install -o $APP_USER -g $APP_USER -m 600 /tmp/.env       $APP_DIR/backend/.env"
echo "      sudo install -o $APP_USER -g $APP_USER -m 644 /tmp/betedge.db $APP_DIR/backend/betedge.db"
echo "      sudo systemctl restart betedge.service"
echo
echo "  ▸ Verify:"
echo "      curl -s http://127.0.0.1:8002/quota"
echo "      systemctl status betedge.service caddy.service"
echo "      sudo journalctl -u betedge -n 50 --no-pager"
echo "════════════════════════════════════════════════════════════════════"
