#!/bin/bash
#   sudo bash scripts/install_powermetrics_permissions.sh
#   sudo bash scripts/install_powermetrics_permissions.sh --uninstall
#   ... --user NOME   regola per un utente diverso da chi ha lanciato sudo:
#                     lo usa il postinstall del .pkg, dove SUDO_USER non c'e'

set -euo pipefail

SUDOERS_PATH=/etc/sudoers.d/ecomonitor-powermetrics
POWERMETRICS=/usr/bin/powermetrics

if [[ $(uname -s) != Darwin ]]; then
    echo "ERRORE: questo script e' per macOS" >&2
    exit 2
fi

if [[ ${EUID} -ne 0 ]]; then
    echo "Serve root: sudo bash $0 $*" >&2
    exit 1
fi

uninstall=0
TARGET_USER=""
while [[ $# -gt 0 ]]; do
    case "${1}" in
    --uninstall) uninstall=1 ;;
    --user)
        TARGET_USER="${2:-}"
        shift
        ;;
    *)
        echo "ERRORE: argomento sconosciuto '${1}'." >&2
        exit 2
        ;;
    esac
    shift
done

if [[ ${uninstall} -eq 1 ]]; then
    rm -f "${SUDOERS_PATH}"
    echo "Regola rimossa: EcoMonitor tornera' a stimare il consumo."
    exit 0
fi

#l'utente che usa l'app
TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"
if [[ -z ${TARGET_USER} || ${TARGET_USER} == root ]]; then
    echo "ERRORE: lanciarlo con sudo dall'utente che usa EcoMonitor, non da root." >&2
    echo "        Oppure indicarlo:  --user NOME" >&2
    exit 2
fi
if [[ ! ${TARGET_USER} =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "ERRORE: nome utente '${TARGET_USER}' non adatto a una regola sudoers." >&2
    exit 2
fi
if ! id -u "${TARGET_USER}" >/dev/null 2>&1; then
    echo "ERRORE: l'utente '${TARGET_USER}' non esiste su questo Mac." >&2
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

#un file sudoers sbagliato in sudoers.d puo' rompere sudo per tutti
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
    echo
    echo "Ora, SENZA sudo:  python scripts/diagnose_macos_sensors.py"
    echo "Deve dire 'backend sensori: MacosPowermetricsBackend'."
else
    echo "FALLITO: sudo chiede ancora la password per quella riga di comando." >&2
    exit 3
fi
