#!/bin/bash
# Collaudo del bundle macOS
#
#   bash packaging/smoke_macos.sh packaging/dist


set -euo pipefail

DIST_DIR="$(cd "${1:?serve la cartella dist di PyInstaller}" && pwd)"
APP="${DIST_DIR}/EcoMonitor.app"
EXECUTABLE="${APP}/Contents/MacOS/EcoMonitor"
CLI="${DIST_DIR}/ecomonitor-cli"
GUI_SECONDS="${GUI_SECONDS:-20}"
EXPECTED_IDENTIFIER=io.github.caponifederico.ecomonitor

fail() {
    echo "FALLITO: $*" >&2
    exit 1
}

echo "=== Struttura del bundle"
[[ -x ${EXECUTABLE} ]] || fail "manca ${EXECUTABLE}"
plutil -lint "${APP}/Contents/Info.plist"
identifier="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${APP}/Contents/Info.plist")"
[[ ${identifier} == "${EXPECTED_IDENTIFIER}" ]] || fail "identificativo ${identifier}"
echo "identificativo: ${identifier}"
file "${EXECUTABLE}"
codesign --verify --deep --strict "${APP}" ||
    fail "firma non valida: su Apple Silicon l'app verrebbe uccisa all'avvio"
echo "firma: valida"
#macOS lega il permesso delle notifiche a chi e' intestata la firma
signed_identifier="$(codesign -dv "${APP}" 2>&1 | sed -n 's/^Identifier=//p')"
if [[ ${signed_identifier} == "${EXPECTED_IDENTIFIER}" ]]; then
    echo "firma intestata a: ${signed_identifier}"
else
    echo "ATTENZIONE: firma intestata a «${signed_identifier}», non a ${EXPECTED_IDENTIFIER}:"
    echo "  macOS potrebbe negare le notifiche native"
fi

#senza la chiave il pacchetto esce 
RESOURCES="${APP}/Contents/Resources"
[[ -f ${RESOURCES}/install_powermetrics_permissions.sh ]] ||
    fail "manca lo script che autorizza powermetrics, a cui punta il consiglio dell'app"
[[ -s ${RESOURCES}/api_key ]] || fail "manca ${RESOURCES}/api_key"
[[ -s ${RESOURCES}/study_endpoint ]] || fail "manca ${RESOURCES}/study_endpoint"
echo "dati spediti: api_key, study_endpoint"

SMOKE_HOME="$(mktemp -d)"
export HOME="${SMOKE_HOME}"
DATA_DIR="${HOME}/Library/Application Support/ecomonitor"
LOG_FILE="${DATA_DIR}/ecomonitor.log"

echo
echo "=== CLI impacchettata"
"${CLI}" --help >/dev/null
"${CLI}" --minutes 0.2 --interval 3 --db "${SMOKE_HOME}/cli.db" | tee "${SMOKE_HOME}/cli.txt"
grep -q "^backend sensori" "${SMOKE_HOME}/cli.txt" || fail "la CLI non ha stampato il backend"
grep -q "^tick eseguiti" "${SMOKE_HOME}/cli.txt" || fail "la CLI non ha concluso"

echo
echo "=== Interfaccia grafica per ${GUI_SECONDS} s (${QT_QPA_PLATFORM:-cocoa})"
"${EXECUTABLE}" >"${SMOKE_HOME}/gui.txt" 2>&1 &
gui_pid=$!
sleep "${GUI_SECONDS}"

if ! kill -0 "${gui_pid}" 2>/dev/null; then
    cat "${SMOKE_HOME}/gui.txt" >&2
    [[ -f ${LOG_FILE} ]] && cat "${LOG_FILE}" >&2
    fail "l'interfaccia grafica e' uscita prima di ${GUI_SECONDS} s"
fi

kill -TERM "${gui_pid}"
status=0
for _ in $(seq 1 20); do
    kill -0 "${gui_pid}" 2>/dev/null || break
    sleep 0.5
done
if kill -0 "${gui_pid}" 2>/dev/null; then
    kill -KILL "${gui_pid}"
    fail "l'interfaccia grafica non si chiude con SIGTERM entro 10 s"
fi
wait "${gui_pid}" || status=$?

echo "--- registro"
[[ -f ${LOG_FILE} ]] || fail "nessun registro in ${LOG_FILE}"
cat "${LOG_FILE}"
echo "---"
[[ ${status} -eq 0 ]] || fail "uscita con codice ${status} dopo SIGTERM"
if grep -q "Traceback" "${LOG_FILE}" "${SMOKE_HOME}/gui.txt"; then
    fail "eccezione nel registro"
fi

#il primo avvio dell'app impacchettata attiva l'avvio automatico
AGENT="${HOME}/Library/LaunchAgents/${EXPECTED_IDENTIFIER}.plist"
if [[ -f ${AGENT} ]]; then
    plutil -lint "${AGENT}"
    target="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "${AGENT}")"
    [[ ${target} == "$(cd "$(dirname "${EXECUTABLE}")" && pwd -P)/EcoMonitor" ]] ||
        fail "l'avvio automatico punta a ${target}"
    echo "avvio automatico: ${target}"
else
    fail "il primo avvio non ha registrato l'avvio automatico in ${AGENT}"
fi

if grep -q "powermetrics attivo" "${LOG_FILE}"; then
    echo "misura: nativa (powermetrics)"
else
    echo "misura: stima (powermetrics non disponibile, vedi registro)"
fi

if grep -q "Notifiche native di macOS attive" "${LOG_FILE}"; then
    echo "notifiche: native"
elif grep -q "Notifiche native di macOS non disponibili" "${LOG_FILE}"; then
    echo "notifiche: NON native, resta la barra dei menu (vedi registro)"
else
    echo "notifiche: non ancora create, nessun tick in ${GUI_SECONDS} s"
fi

rm -rf "${SMOKE_HOME}"
echo
echo "Collaudato con successo: ${APP} e ${CLI}"
