#!/usr/bin/env bash
# Run on the Lightsail box to redeploy after code changes.
#   git pull → reinstall deps if requirements/package-lock changed → rebuild → restart
#
# Idempotent. Cheap when nothing changed.

set -euo pipefail
APP_DIR="${APP_DIR:-/opt/betedge}"
APP_USER="${APP_USER:-betedge}"

cd "$APP_DIR"
sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet
LOCAL=$(sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse @)
REMOTE=$(sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse '@{u}')

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "Already up to date ($LOCAL). Nothing to deploy."
    exit 0
fi

echo "Deploying: $LOCAL → $REMOTE"

# Detect dependency changes from the diff so we only reinstall when needed
CHANGED=$(sudo -u "$APP_USER" git -C "$APP_DIR" diff --name-only "$LOCAL..$REMOTE")
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only

if grep -q "^backend/requirements.txt$" <<<"$CHANGED"; then
    echo "  pip deps changed — reinstalling"
    sudo -u "$APP_USER" "$APP_DIR/backend/.venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"
fi

if grep -qE "^frontend/(package(-lock)?\.json|src/|index\.html|vite\.config\.js|tailwind\.config\.js|postcss\.config\.js)" <<<"$CHANGED"; then
    echo "  frontend changed — rebuilding"
    sudo -u "$APP_USER" bash -lc "cd '$APP_DIR/frontend' && npm ci --silent && npm run build"
fi

# Always reload systemd in case the unit file changed and bounce the service
if grep -q "^infra/setup.sh$\|^infra/.*\.service$" <<<"$CHANGED"; then
    echo "  infra changed — reload systemd"
    sudo systemctl daemon-reload
fi

sudo systemctl restart betedge.service
sudo systemctl reload caddy.service || sudo systemctl restart caddy.service

echo "✓ Deployed. Backend status:"
systemctl --no-pager -l status betedge.service | head -10
