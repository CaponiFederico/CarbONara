#!/bin/bash
#   sudo bash scripts/install_powermetrics_permissions.sh
#   sudo bash scripts/install_powermetrics_permissions.sh --uninstall
#                     toglie la regola a tutti; con --user NOME solo a lui
#   ... --user NOME   regola per un utente diverso da chi ha lanciato sudo:
#                     lo usa il postinstall del .pkg, dove SUDO_USER non c'e'
#
# La regola e' per utente e si aggiunge a quelle gia' presenti: un aggiornamento
# installato da un altro amministratore non toglie la misura a chi l'aveva.

set -euo pipefail

SUDOERS_PATH=/etc/sudoers.d/ecomonitor-powermetrics
POWERMETRICS=/usr/bin/powermetrics

POWERMETRICS_ARGS=(-s cpu_power,gpu_power -f plist -i 1000)

#la riga di comando come la vuole sudoers, con le virgole protette
command_spec() {
    local spec="${POWERMETRICS}" argument
    for argument in "${POWERMETRICS_ARGS[@]}"; do
        spec+=" $(printf '%s' "${argument}" | sed 's/,/\\,/g')"
    done
    printf '%s' "${spec}"
}

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

if [[ $(uname -s) != Darwin ]]; then
    echo "ERRORE: questo script e' per macOS" >&2
    exit 2
fi

if [[ ${EUID} -ne 0 ]]; then
    echo "Serve root: sudo bash $0 $*" >&2
    exit 1
fi

valid_user_name() {
    [[ $1 =~ ^[A-Za-z0-9._-]+$ ]]
}

#Gli utenti che hanno gia' la regola e che esistono ancora
existing_users() {
    [[ -f ${SUDOERS_PATH} ]] || return 0
    local name
    while read -r name _; do
        valid_user_name "${name}" || continue
        [[ ${name} != root ]] || continue
        if id -u "${name}" >/dev/null 2>&1; then
            printf '%s\n' "${name}"
        fi
    done < <(grep -E '^[A-Za-z0-9._-]+ ALL=\(root\) NOPASSWD: ' "${SUDOERS_PATH}" || true)
}

#Riscrive il file con una riga per utente
write_rules() {
    if [[ $# -eq 0 ]]; then
        rm -f "${SUDOERS_PATH}"
        return 0
    fi
    local name
    #globale e non local
    candidate="$(mktemp)"
    trap 'rm -f "${candidate}"' EXIT
    {
        echo "# Installata da ecomonitor/scripts/install_powermetrics_permissions.sh"
        for name in "$@"; do
            echo "${name} ALL=(root) NOPASSWD: $(command_spec)"
        done
    } >"${candidate}"
    chmod 0440 "${candidate}"

    #un file sudoers sbagliato in sudoers.d puo' rompere sudo per tutti
    if ! visudo -cf "${candidate}"; then
        echo "ERRORE: visudo ha rifiutato la regola, niente installato." >&2
        exit 3
    fi
    install -m 0440 -o root -g wheel "${candidate}" "${SUDOERS_PATH}"
    rm -f "${candidate}"
}

users=()
while IFS= read -r name; do
    users+=("${name}")
done < <(existing_users)

if [[ ${uninstall} -eq 1 ]]; then
    if [[ -z ${TARGET_USER} ]]; then
        rm -f "${SUDOERS_PATH}"
        echo "Regola rimossa per tutti: EcoMonitor tornera' a stimare il consumo."
        exit 0
    fi
    remaining=()
    for name in ${users[@]+"${users[@]}"}; do
        if [[ ${name} != "${TARGET_USER}" ]]; then
            remaining+=("${name}")
        fi
    done
    write_rules ${remaining[@]+"${remaining[@]}"}
    echo "Regola rimossa per ${TARGET_USER}: su quell'account EcoMonitor tornera' a stimare il consumo."
    exit 0
fi

#l'utente che usa l'app
TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"
if [[ -z ${TARGET_USER} || ${TARGET_USER} == root ]]; then
    echo "ERRORE: lanciarlo con sudo dall'utente che usa EcoMonitor, non da root." >&2
    echo "        Oppure indicarlo:  --user NOME" >&2
    exit 2
fi
if ! valid_user_name "${TARGET_USER}"; then
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

already=0
for name in ${users[@]+"${users[@]}"}; do
    if [[ ${name} == "${TARGET_USER}" ]]; then
        already=1
    fi
done
[[ ${already} -eq 1 ]] || users+=("${TARGET_USER}")

write_rules "${users[@]}"
echo "Regola installata: ${SUDOERS_PATH} (utenti: ${users[*]})"

echo
echo "Verifica: ${TARGET_USER} puo' lanciare powermetrics senza password"
if sudo -u "${TARGET_USER}" sudo -n -l "${POWERMETRICS}" "${POWERMETRICS_ARGS[@]}" >/dev/null 2>&1; then
    echo "OK: sudo -n accetta la riga di comando dell'app"
    echo
    echo "Ora, SENZA sudo:  python scripts/diagnose_macos_sensors.py"
    echo "Deve dire 'backend sensori: MacosPowermetricsBackend'."
else
    echo "FALLITO: sudo chiede ancora la password per quella riga di comando." >&2
    exit 3
fi
