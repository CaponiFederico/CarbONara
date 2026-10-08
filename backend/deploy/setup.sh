#!/usr/bin/env bash
# Installa o aggiorna il server dello studio sulla VM
#
#   sudo bash deploy/setup.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "va lanciato con sudo" >&2
    exit 1
fi
ADMIN_USER="${SUDO_USER:?lanciare con sudo da un utente normale, non da root}"
SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR=/opt/ecomonitor-study
DATABASE=ecomonitor_study

echo "== pacchetti"
apt-get update -q
apt-get install -y -q postgresql caddy python3-venv ufw unattended-upgrades fail2ban

echo "== utenti di servizio"
for service_user in ecomonitor_api ecomonitor_grid; do
    if ! id "$service_user" >/dev/null 2>&1; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "$service_user"
    fi
done

echo "== codice e ambiente Python"
install -d -m 755 "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/src"
install -d -m 755 "$INSTALL_DIR/src"
cp -r "$SOURCE_DIR/study_server" "$SOURCE_DIR/sql" "$SOURCE_DIR/pyproject.toml" "$INSTALL_DIR/src/"
find "$INSTALL_DIR/src" -name '__pycache__' -prune -exec rm -rf {} +
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q "$INSTALL_DIR/src"
ln -sf "$INSTALL_DIR/venv/bin/ecomonitor-study-admin" /usr/local/bin/ecomonitor-study-admin
ln -sf "$INSTALL_DIR/venv/bin/ecomonitor-study-grid" /usr/local/bin/ecomonitor-study-grid

echo "== database"
systemctl enable --now postgresql
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '$DATABASE'" | grep -q 1; then
    sudo -u postgres createdb "$DATABASE"
fi
# in ordine di nome; ogni file si puo' rieseguire
for schema in "$INSTALL_DIR"/src/sql/*.sql; do
    sudo -u postgres psql -v ON_ERROR_STOP=1 -q -d "$DATABASE" -f "$schema"
done
sudo -u postgres psql -v ON_ERROR_STOP=1 -q -d "$DATABASE" <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$ADMIN_USER') THEN
        CREATE ROLE "$ADMIN_USER" LOGIN;
    END IF;
END
\$\$;
GRANT ecomonitor_admin TO "$ADMIN_USER";
REVOKE CONNECT ON DATABASE $DATABASE FROM PUBLIC;
GRANT CONNECT ON DATABASE $DATABASE TO ecomonitor_api, ecomonitor_grid, ecomonitor_admin;
SQL

echo "== servizio"
install -m 644 "$SOURCE_DIR/deploy/ecomonitor-study.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable ecomonitor-study
systemctl restart ecomonitor-study

echo "== backup"
install -d -m 700 -o postgres -g postgres /var/backups/ecomonitor-study
install -m 644 "$SOURCE_DIR/deploy/ecomonitor-study-backup.service" /etc/systemd/system/
install -m 644 "$SOURCE_DIR/deploy/ecomonitor-study-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ecomonitor-study-backup.timer
systemctl start ecomonitor-study-backup.service
ls /var/backups/ecomonitor-study | tail -1

echo "== intensita' della rete"
install -d -m 700 /etc/ecomonitor-study
install -m 644 "$SOURCE_DIR/deploy/ecomonitor-study-grid.service" /etc/systemd/system/
install -m 644 "$SOURCE_DIR/deploy/ecomonitor-study-grid.timer" /etc/systemd/system/
systemctl daemon-reload
# prova dei permessi senza chiamare ElectricityMaps
sudo -u ecomonitor_grid ECOMONITOR_STUDY_DSN="dbname=$DATABASE" /usr/local/bin/ecomonitor-study-grid --dry-run
if [[ -s /etc/ecomonitor-study/em_api_key ]]; then
    chmod 600 /etc/ecomonitor-study/em_api_key
    systemctl enable --now ecomonitor-study-grid.timer
else
    systemctl disable --now ecomonitor-study-grid.timer 2>/dev/null || true
    echo "ATTENZIONE: manca la chiave ElectricityMaps, raccolta spenta. Per accenderla:"
    echo "  sudo install -m 600 /dev/stdin /etc/ecomonitor-study/em_api_key   (incolla, Invio, Ctrl+D)"
    echo "  poi rilancia questo script"
fi

echo "== caddy"
install -m 644 "$SOURCE_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl reload-or-restart caddy

echo "== firewall"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "== verifica"
for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done
curl -fsS http://127.0.0.1:8000/healthz && echo
sudo -u "$ADMIN_USER" ecomonitor-study-admin list >/dev/null && echo "amministrazione: OK"
echo "OK"
