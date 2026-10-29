#!/bin/bash
# Disinstalla CarbO'Nara
# 
#   doppio clic su CarbONaraUninstall.command in Applicazioni
#   sudo bash /Applications/CarbONaraUninstall.command
#   ... --all-users   toglie anche i residui degli altri account

set -uo pipefail

APP="/Applications/CarbONara.app"
EXECUTABLE="${APP}/Contents/MacOS/CarbONara"
SUDOERS_PATH=/etc/sudoers.d/ecomonitor-powermetrics
RECEIPT=io.github.caponifederico.carbonara
AGENT_RELATIVE="Library/LaunchAgents/${RECEIPT}.plist"
DATA_RELATIVE="Library/Application Support/carbonara"

if [[ $(uname -s) != Darwin ]]; then
    echo "ERRORE: questo script e' per macOS." >&2
    exit 2
fi

all_users=0
for argument in "$@"; do
    case "${argument}" in
    --all-users) all_users=1 ;;
    *)
        echo "ERRORE: argomento sconosciuto '${argument}'." >&2
        exit 2
        ;;
    esac
done

#doppio clic dal Finder
if [[ ${EUID} -ne 0 ]]; then
    echo "CarbO'Nara — disinstallazione"
    echo
    echo "Serve la password di amministratore: l'autorizzazione a powermetrics"
    echo "sta in ${SUDOERS_PATH} e altrimenti resterebbe indietro."
    echo
    exec sudo "$0" "$@"
fi

TARGET_USER="${SUDO_USER:-}"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

report() { printf '  %s\n' "$*"; }

home_of() {
    local name="$1" home
    home="$(dscl . -read "/Users/${name}" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
    printf '%s' "${home:-/Users/${name}}"
}

echo "CarbO'Nara — disinstallazione"
echo

#un'applicazione viva continuerebbe a scrivere su file gia' cancellati
#( |$): l'avvio automatico nascosto la lancia con --background
running_pids() { pgrep -f "^${EXECUTABLE}( |$)" 2>/dev/null || true; }
pids="$(running_pids)"
if [[ -n ${pids} ]]; then
    report "chiudo l'applicazione in esecuzione"
    # shellcheck disable=SC2086
    kill -TERM ${pids} 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [[ -z "$(running_pids)" ]] && break
        sleep 0.5
    done
    pids="$(running_pids)"
    # shellcheck disable=SC2086
    [[ -n ${pids} ]] && kill -KILL ${pids} 2>/dev/null
fi

if [[ -d ${APP} ]]; then
    rm -rf "${APP}" && report "rimossa ${APP}"
else
    report "${APP} non c'era"
fi

#il residuo che conta: sopravvive al Cestino e nessuno se ne accorge
if [[ -f ${SUDOERS_PATH} ]]; then
    rm -f "${SUDOERS_PATH}" && report "rimossa l'autorizzazione a powermetrics"
else
    report "nessuna autorizzazione a powermetrics da togliere"
fi

#senza, pkgutil continua a dichiarare il pacchetto installato
if pkgutil --pkg-info "${RECEIPT}" >/dev/null 2>&1; then
    pkgutil --forget "${RECEIPT}" >/dev/null 2>&1 && report "dimenticata la ricevuta del pacchetto"
fi

#le misure sono dati dell'utente: non si cancellano senza chiederlo
keep_data=1
echo
echo "Le misure raccolte restano in ~/${DATA_RELATIVE}."
read -r -p "Cancellare anche quelle? [s/N] " answer || answer=""
case "${answer}" in
[sS]*) keep_data=0 ;;
esac
echo

forget_in_dock() {
    local name="$1" uid="$2" plist array count index identifier removed=0
    pgrep -u "${uid}" -x Dock >/dev/null 2>&1 || return 0
    plist="$(mktemp)" || return 0
    if ! launchctl asuser "${uid}" sudo -u "${name}" defaults export com.apple.dock - \
        >"${plist}" 2>/dev/null; then
        rm -f "${plist}"
        return 0
    fi
    for array in recent-apps persistent-apps; do
        count="$(plutil -extract "${array}" raw -o - "${plist}" 2>/dev/null)" || continue
        [[ ${count} =~ ^[0-9]+$ ]] || continue
        for ((index = count - 1; index >= 0; index--)); do
            identifier="$(plutil -extract "${array}.${index}.tile-data.bundle-identifier" raw \
                -o - "${plist}" 2>/dev/null)" || continue
            if [[ ${identifier} == "${RECEIPT}" ]]; then
                plutil -remove "${array}.${index}" "${plist}" && removed=$((removed + 1))
            fi
        done
    done
    if [[ ${removed} -gt 0 ]] &&
        launchctl asuser "${uid}" sudo -u "${name}" defaults import com.apple.dock - \
            <"${plist}" 2>/dev/null; then
        #launchd rilancia il Dock
        pkill -u "${uid}" -x Dock 2>/dev/null
        report "${name}: tolta l'icona dal Dock"
    fi
    rm -f "${plist}"
}

remove_user_files() {
    local home="$1" name="$2"
    [[ -d ${home} ]] || return 0
    local uid
    uid="$(id -u "${name}" 2>/dev/null)" || uid=""
    if [[ -n ${uid} ]] && launchctl bootout "gui/${uid}/${RECEIPT}" >/dev/null 2>&1; then
        #altrimenti resta caricato
        report "${name}: avvio automatico scaricato da launchd"
    fi
    [[ -n ${uid} ]] && forget_in_dock "${name}" "${uid}"
    if [[ -f ${home}/${AGENT_RELATIVE} ]]; then
        rm -f "${home}/${AGENT_RELATIVE}" && report "${name}: rimosso l'avvio automatico"
    fi
    if [[ -d ${home}/${DATA_RELATIVE} ]]; then
        if [[ ${keep_data} -eq 1 ]]; then
            report "${name}: misure conservate in ${home}/${DATA_RELATIVE}"
        else
            rm -rf "${home}/${DATA_RELATIVE}" && report "${name}: rimosse le misure"
        fi
    fi
}

if [[ ${all_users} -eq 1 ]]; then
    for home in /Users/*; do
        [[ -d ${home} ]] || continue
        [[ $(basename "${home}") == Shared ]] && continue
        remove_user_files "${home}" "$(basename "${home}")"
    done
elif [[ -n ${TARGET_USER} && ${TARGET_USER} != root ]]; then
    remove_user_files "$(home_of "${TARGET_USER}")" "${TARGET_USER}"
else
    report "nessun account da ripulire: rilanciarlo con sudo dall'utente che usa CarbO'Nara"
fi

echo
if [[ ${all_users} -eq 0 ]]; then
    echo "Fatto. Se altri account hanno usato CarbO'Nara, per i loro residui:"
    echo "  sudo bash \"${SELF}\" --all-users"
else
    echo "Fatto."
fi

#se stesso
rm -f "${SELF}"
