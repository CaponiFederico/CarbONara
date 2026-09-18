#!/bin/bash
#   sudo bash scripts/install_powermetrics_permissions.sh
#   sudo bash scripts/install_powermetrics_permissions.sh --uninstall

set -euo pipefail #per protezione 

SUDOERS_PATH=/etc/sudoers.d/ecomonitor-powermetrics
POWERMETRICS=/usr/bin/powermetrics

if [[ $(uname -s) != Darwin ]]; then
    echo "ERRORE: questo script e' per macOS." >&2
    exit 2
fi

if [[ ${EUID} -ne 0 ]]; then
    echo "Serve root: sudo bash $0 $*" >&2
    exit 1
fi

if [[ ${1:-} == "--uninstall" ]]; then
    rm -f "${SUDOERS_PATH}"
    echo "Regola rimossa: EcoMonitor tornera' a stimare il consumo."
    exit 0
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z ${TARGET_USER} || ${TARGET_USER} == root ]]; then
    echo "ERRORE: lanciarlo con sudo dall'utente che usa EcoMonitor, non da root." >&2
    exit 2
fi
if [[ ! ${TARGET_USER} =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "ERRORE: nome utente '${TARGET_USER}' non adatto a una regola sudoers." >&2
    exit 2
fi

if [[ ! -x ${POWERMETRICS} ]]; then
    echo "ERRORE: ${POWERMETRICS} non esiste su questo sistema." >&2
    exit 2
fi

if ! grep -Eq '^[#@]includedir[[:space:]]+(/private)?/etc/sudoers.d' /etc/sudoers; then
    echo "ATTENZIONE: /etc/sudoers non include /etc/sudoers.d: la regola non avra' effetto." >&2
fi

candidate="$(mktemp)"
trap 'rm -f "${candidate}"' EXIT

cat >"${candidate}" <<RULE
# Installata da ecomonitor/scripts/install_powermetrics_permissions.sh
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/powermetrics -s cpu_power\,gpu_power -f plist -i 1000
RULE
chmod 0440 "${candidate}"

# Verifica sintassi sudoers
if ! visudo -cf "${candidate}"; then
    echo "ERRORE: visudo ha rifiutato la regola, niente installato." >&2
    exit 3
fi
install -m 0440 -o root -g wheel "${candidate}" "${SUDOERS_PATH}"
echo "Regola installata: ${SUDOERS_PATH}"

echo
echo "Verifica: ${TARGET_USER} puo' lanciare powermetrics senza password"
if sudo -u "${TARGET_USER}" sudo -n -l "${POWERMETRICS}" -s cpu_power,gpu_power -f plist -i 1000 >/dev/null 2>&1; then
    echo "OK: sudo -n accetta la riga di comando dell'app"
else
    echo "FALLITO: sudo chiede ancora la password per quella riga di comando." >&2
    exit 3
fi
