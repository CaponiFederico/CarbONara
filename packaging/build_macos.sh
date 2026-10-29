#!/bin/bash
# Costruisce il pacchetto macOS
#
# dalla root del progetto
#   bash packaging/build_macos.sh
#
# Serve un venv con gli extra [macos] e [packaging]:
#   python3 -m venv .venv && .venv/bin/pip install -e ".[gui,macos,packaging]"
#

set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${PACKAGING_DIR}")"

if [[ $(uname -s) != Darwin ]]; then
    echo "ERRORE: va eseguito su macOS." >&2
    exit 2
fi

PYTHON="${PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
if [[ ! -x ${PYTHON} ]]; then
    echo "ERRORE: nessun interprete in ${PYTHON}. Impostare PYTHON=/percorso/python." >&2
    exit 2
fi

VERSION="$(grep -m1 -E '^version = ' "${PROJECT_ROOT}/pyproject.toml" | cut -d'"' -f2)"
[[ -n ${VERSION} ]] || { echo "ERRORE: versione non leggibile da pyproject.toml" >&2; exit 2; }

#solo Apple Silicon
ARCH="$("${PYTHON}" -c 'import platform; print(platform.machine())')"
if [[ ${ARCH} != arm64 ]]; then
    echo "ERRORE: serve un interprete arm64, ${PYTHON} e' ${ARCH}." >&2
    exit 2
fi

#senza PyObjC il pacchetto esce senza notifiche native, e non lo dice
if ! "${PYTHON}" -c 'import UserNotifications' 2>/dev/null; then
    echo "ERRORE: manca PyObjC per le notifiche native." >&2
    echo "  ${PYTHON} -m pip install -e \".[gui,macos,packaging]\"" >&2
    exit 2
fi

#Qt prima della 6.12 va in crash su macOS 27
if ! "${PYTHON}" -c 'import sys, PySide6; sys.exit(PySide6.__version_info__[:2] < (6, 12))' 2>/dev/null; then
    echo "ERRORE: serve PySide6 6.12 o successiva." >&2
    echo "  ${PYTHON} -m pip install -U -e \".[gui,macos,packaging]\"" >&2
    exit 2
fi

BUILD_DIR="${PACKAGING_DIR}/build"
DIST_DIR="${PACKAGING_DIR}/dist"
OUT_DIR="${PACKAGING_DIR}/out"
MACOS_DIR="${PACKAGING_DIR}/macos"
STAGE_DIR="${BUILD_DIR}/stage-macos"
APP="${DIST_DIR}/CarbONara.app"
BUNDLE_IDENTIFIER=io.github.caponifederico.carbonara
#il nome che l'utente vede in Applicazioni: .command per il doppio clic
UNINSTALLER="CarbONaraUninstall.command"
PKG="${OUT_DIR}/carbonara-${VERSION}-macos-${ARCH}.pkg"

#Il pacchetto della build precedente non deve sopravvivere a una build fallita
rm -f "${PKG}"

STUDY_ENDPOINT="$(bash "${PACKAGING_DIR}/read_study_endpoint.sh")"

PERMISSIONS_SCRIPT="${PROJECT_ROOT}/scripts/install_powermetrics_permissions.sh"
RULE_SPEC="$(bash "${PERMISSIONS_SCRIPT}" --print-rule)"
APP_SPEC="$(PYTHONPATH="${PROJECT_ROOT}/src" "${PYTHON}" -c \
    'from ecomonitor.sensors.macos_powermetrics import sudoers_command_spec; print(sudoers_command_spec())')"
if [[ ${RULE_SPEC} != "${APP_SPEC}" ]]; then
    echo "ERRORE: la regola sudoers e l'app non lanciano la stessa riga di comando." >&2
    echo "  regola: ${RULE_SPEC}" >&2
    echo "  app   : ${APP_SPEC}" >&2
    exit 2
fi

"${PYTHON}" "${PACKAGING_DIR}/make_icon.py"
"${PYTHON}" -m PyInstaller --clean --noconfirm \
    --distpath "${DIST_DIR}" --workpath "${BUILD_DIR}" \
    "${PACKAGING_DIR}/ecomonitor.spec"

if [[ ! -x ${APP}/Contents/MacOS/CarbONara ]]; then
    echo "ERRORE: PyInstaller non ha prodotto ${APP}." >&2
    exit 2
fi

RESOURCES="${APP}/Contents/Resources"
printf '%s' "${STUDY_ENDPOINT}" >"${RESOURCES}/study_endpoint"
chmod 0644 "${RESOURCES}/study_endpoint"
cp "${PERMISSIONS_SCRIPT}" "${RESOURCES}/"

#aggiungere file dopo PyInstaller invalida la sua firma
codesign --force --deep --sign - "${APP}"
codesign --verify --deep --strict --verbose=2 "${APP}"

bash "${PACKAGING_DIR}/smoke_macos.sh" "${DIST_DIR}"

rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}/root" "${STAGE_DIR}/scripts" "${STAGE_DIR}/components" "${OUT_DIR}"
ditto "${APP}" "${STAGE_DIR}/root/CarbONara.app"

cp "${MACOS_DIR}/uninstall.command" "${STAGE_DIR}/root/${UNINSTALLER}"
chmod 0755 "${STAGE_DIR}/root/${UNINSTALLER}"

#git non conserva il bit di esecuzione
cp "${MACOS_DIR}/scripts/preinstall" "${MACOS_DIR}/scripts/postinstall" "${STAGE_DIR}/scripts/"
chmod 0755 "${STAGE_DIR}/scripts/preinstall" "${STAGE_DIR}/scripts/postinstall"

COMPONENT="${STAGE_DIR}/components/CarbONara.pkg"
pkgbuild \
    --root "${STAGE_DIR}/root" \
    --component-plist "${MACOS_DIR}/component.plist" \
    --scripts "${STAGE_DIR}/scripts" \
    --identifier "${BUNDLE_IDENTIFIER}" \
    --version "${VERSION}" \
    --install-location /Applications \
    "${COMPONENT}"

sed -e "s|__VERSION__|${VERSION}|" -e "s|__COMPONENT__|CarbONara.pkg|" \
    "${MACOS_DIR}/distribution.xml" >"${STAGE_DIR}/distribution.xml"
productbuild \
    --distribution "${STAGE_DIR}/distribution.xml" \
    --package-path "${STAGE_DIR}/components" \
    --resources "${MACOS_DIR}/resources" \
    "${PKG}"

#Stesso controllo del .deb
EXPANDED="${STAGE_DIR}/expanded"
rm -rf "${EXPANDED}"
#--expand-full estrae Scripts come cartella; --expand lo lascia archivio, e
#indovinarne il formato non ha funzionato: bsdtar lo rifiuta
pkgutil --expand-full "${PKG}" "${EXPANDED}"
for required in Distribution CarbONara.pkg/Scripts/preinstall CarbONara.pkg/Scripts/postinstall; do
    if [[ ! -e ${EXPANDED}/${required} ]]; then
        echo "ERRORE: ${required} non e' finito in ${PKG}." >&2
        rm -f "${PKG}"
        exit 2
    fi
done
for script in preinstall postinstall; do
    #pkgbuild scarta senza dirlo uno script senza bit di esecuzione
    if [[ ! -x ${EXPANDED}/CarbONara.pkg/Scripts/${script} ]]; then
        echo "ERRORE: ${script} dentro ${PKG} non e' eseguibile." >&2
        rm -f "${PKG}"
        exit 2
    fi
done
rm -rf "${EXPANDED}"

#il disinstallatore sta nel payload
PAYLOAD_LIST="${STAGE_DIR}/payload.txt"
pkgutil --payload-files "${PKG}" >"${PAYLOAD_LIST}"
if ! grep -qF "${UNINSTALLER}" "${PAYLOAD_LIST}"; then
    echo "ERRORE: ${UNINSTALLER} non e' nel payload di ${PKG}." >&2
    rm -f "${PKG}"
    exit 2
fi
rm -f "${PAYLOAD_LIST}"

echo
echo "Pacchetto pronto: ${PKG} ($(du -h "${PKG}" | cut -f1))"
echo "Doppio clic, una password, e l'app e' installata e autorizzata a misurare."
echo "Non e' firmato con un Developer ID: al primo tentativo Gatekeeper lo blocca,"
echo "si apre da Impostazioni di Sistema > Privacy e sicurezza > Apri comunque."
