#!/bin/bash
# Installa EcoMonitor per l'utente corrente senza root
#
# dalla root del progetto
#   bash install.sh
#   bash install.sh --uninstall
#
# Installa in ~/.local/lib/ecomonitor, con i comandi in ~/.local/bin e la voce
# di menu in ~/.local/share/applications

set -euo pipefail

APP_DIR="${HOME}/.local/lib/ecomonitor"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/256x256/apps"
DATA_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/ecomonitor"
DESKTOP_FILE="${DESKTOP_DIR}/ecomonitor.desktop"

if [[ ${1:-} == "--uninstall" ]]; then
    rm -rf "${APP_DIR}"
    rm -f "${BIN_DIR}/ecomonitor"
    rm -f "${DESKTOP_FILE}" "${ICON_DIR}/ecomonitor.png"
    command -v update-desktop-database >/dev/null && update-desktop-database "${DESKTOP_DIR}" || true
    echo "EcoMonitor rimosso."
    echo "I dati misurati restano in ${DATA_DIR}: cancellarli a mano se non servono piu'."
    exit 0
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x ${SOURCE_DIR}/EcoMonitor/EcoMonitor ]]; then
    echo "ERRORE: non trovo EcoMonitor/EcoMonitor accanto a questo script." >&2
    echo "Eseguirlo dalla cartella estratta dall'archivio." >&2
    exit 2
fi

if pgrep -x EcoMonitor >/dev/null 2>&1; then
    echo "ERRORE: EcoMonitor e' in esecuzione. Chiuderlo e riprovare." >&2
    exit 3
fi

#doppia installazione: prima rimuovo la vecchia, poi installo la nuova
rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}" "${BIN_DIR}" "${DESKTOP_DIR}" "${ICON_DIR}"
cp -r "${SOURCE_DIR}/EcoMonitor/." "${APP_DIR}/"
chmod +x "${APP_DIR}/EcoMonitor"

ln -sf "${APP_DIR}/EcoMonitor" "${BIN_DIR}/ecomonitor"

cp "${SOURCE_DIR}/ecomonitor.png" "${ICON_DIR}/ecomonitor.png"
sed "s|__EXEC__|${APP_DIR}/EcoMonitor|" "${SOURCE_DIR}/ecomonitor.desktop" >"${DESKTOP_FILE}"
chmod 0644 "${DESKTOP_FILE}"

if command -v update-desktop-database >/dev/null; then
    update-desktop-database "${DESKTOP_DIR}" || true
fi
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "EcoMonitor installato in ${APP_DIR}"
echo "Comando: ecomonitor (interfaccia grafica)"
if [[ :${PATH}: != *:${BIN_DIR}:* ]]; then
    echo "NOTA: ${BIN_DIR} non e' nel PATH. Aggiungerlo per usare il comando per nome."
fi
echo "Dati e configurazione: ${DATA_DIR}"
echo
echo "Senza permessi sui contatori RAPL la CPU viene stimata, non misurata."
echo "Per la misura vera:  sudo bash scripts/install_rapl_permissions.sh"
