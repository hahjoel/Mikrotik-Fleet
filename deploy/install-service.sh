#!/usr/bin/env bash
# Installs MikroTik Fleet Controller as a systemd service.
# Run from the project directory:  sudo bash deploy/install-service.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SVC_USER="mtfleet"
ENV_FILE="/etc/mtfleet.env"

echo "Project directory: $APP_DIR"

# 1. system packages
if ! command -v python3 >/dev/null; then echo "python3 not found"; exit 1; fi
apt-get update -qq && apt-get install -y -qq python3-venv python3-full

# 2. virtualenv + dependencies
if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# 3. service account (no login shell, owns the app dir so it can write routers.json/backups)
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

# 3b. make sure the service account can traverse into the app dir.
# Home dirs are often 0750, which would block it and cause systemd 200/CHDIR.
d="$APP_DIR"
while [ "$d" != "/" ]; do
  if ! sudo -u "$SVC_USER" test -x "$d"; then
    chmod o+x "$d"
    echo "Added traverse (o+x) on $d so $SVC_USER can reach the app"
  fi
  d="$(dirname "$d")"
done

# 4. environment file with the web password
if [ ! -f "$ENV_FILE" ]; then
  read -rsp "Choose a web login password: " PW; echo
  cat > "$ENV_FILE" <<EOT
FLEET_USER=admin
FLEET_PASSWORD=$PW
EOT
  chmod 600 "$ENV_FILE"
  chown "$SVC_USER:$SVC_USER" "$ENV_FILE"
  echo "Wrote $ENV_FILE"
else
  echo "$ENV_FILE already exists, leaving it as-is"
fi

# 5. install and start the unit (rewrites paths/user to match this install)
sed -e "s#/home/joel/fleet#$APP_DIR#g" \
    -e "s/^User=.*/User=$SVC_USER/" -e "s/^Group=.*/Group=$SVC_USER/" \
    "$APP_DIR/deploy/mtfleet.service" > /etc/systemd/system/mtfleet.service

systemctl daemon-reload
systemctl enable --now mtfleet
sleep 1
systemctl --no-pager --full status mtfleet | head -n 12
echo
echo "Done. Open http://<this-server>:8080  (user: admin)"
echo "Logs:    journalctl -u mtfleet -f"
echo "Restart: systemctl restart mtfleet"
