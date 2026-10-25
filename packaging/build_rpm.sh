#!/bin/bash
# Costruisce il pacchetto .rpm per Fedora
#
# dalla root del progetto dopo packaging/build_linux.sh
#   bash packaging/build_rpm.sh
#
# Produce packaging/out/ecomonitor-<versione>-1.<dist>.<arch>.rpm
#
# Serve rpmbuild, che non chiede root ma deve esserci:
#   sudo dnf install rpm-build

set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${PACKAGING_DIR}")"

DIST_DIR="${PACKAGING_DIR}/dist"
OUT_DIR="${PACKAGING_DIR}/out"
RPM_DIR="${PACKAGING_DIR}/build/rpm"
STAGE_DIR="${RPM_DIR}/root"
TOP_DIR="${RPM_DIR}/topdir"
SPEC_TEMPLATE="${PACKAGING_DIR}/rpm/ecomonitor.spec.in"
ORIGIN_FILE="${DIST_DIR}/EcoMonitor.origin"
RAPL_SCRIPT="${PROJECT_ROOT}/scripts/install_rapl_permissions.sh"
LICENSE_FILE="${PROJECT_ROOT}/LICENSE"
#Il nome del file e' l'id del componente AppStream li vuole uguali
METAINFO_ID="io.github.caponifederico.ecomonitor"
METAINFO_FILE="${PACKAGING_DIR}/${METAINFO_ID}.metainfo.xml"

source "${PACKAGING_DIR}/bundle_origin.sh"

if ! command -v rpmbuild >/dev/null; then
    echo "ERRORE: rpmbuild non e' installato." >&2
    echo "  sudo dnf install rpm-build" >&2
    exit 2
fi

VERSION="$(grep -m1 -E '^version = ' "${PROJECT_ROOT}/pyproject.toml" | cut -d'"' -f2)"
[[ -n ${VERSION} ]] || { echo "ERRORE: versione non leggibile da pyproject.toml" >&2; exit 2; }
ARCH="$(uname -m)"
#il suffisso della distribuzione (.fc44) lo decide rpm 
DIST_TAG="$(rpm --eval '%{?dist}')"

#Il bundle e' quello della macchina che l'ha costruito
require_bundle_origin "${ORIGIN_FILE}" fedora \
    "su Fedora:  bash packaging/build_linux.sh && bash packaging/build_rpm.sh" || exit 2

RPM="${OUT_DIR}/ecomonitor-${VERSION}-1${DIST_TAG}.${ARCH}.rpm"
mkdir -p "${OUT_DIR}"
rm -f "${RPM}"

#L'.rpm impacchetta quello che ha prodotto PyInstaller
if [[ ! -x ${DIST_DIR}/EcoMonitor/EcoMonitor ]]; then
    echo "ERRORE: manca ${DIST_DIR}/EcoMonitor/EcoMonitor." >&2
    echo "Eseguire prima:  bash packaging/build_linux.sh" >&2
    exit 2
fi

if ! compgen -G "${DIST_DIR}/EcoMonitor/_internal/libxcb-cursor.so*" >/dev/null; then
    echo "ERRORE: libxcb-cursor.so.0 non e' nella cartella prodotta da PyInstaller." >&2
    echo "Senza, l'interfaccia grafica aborta su qualunque sessione X11 (su Wayland parte)." >&2
    echo "Installarla sulla macchina di build e ricostruire:" >&2
    echo "  sudo dnf install xcb-util-cursor && bash packaging/build_linux.sh" >&2
    exit 2
fi

#Un bundle copiato da un'altra macchina puo' arrivare con delle librerie troncate
truncated="$(find "${DIST_DIR}/EcoMonitor" -type f -name '*.so*' -empty | head -5)"
if [[ -n ${truncated} ]]; then
    echo "ERRORE: nel bundle ci sono librerie vuote, la copia e' incompleta:" >&2
    echo "${truncated}" >&2
    echo "Ricostruire con:  bash packaging/build_linux.sh" >&2
    exit 2
fi

#La regola udev ha una fonte sola
rule_count="$(grep -c -E '^SUBSYSTEM=="powercap"' "${RAPL_SCRIPT}" || true)"
if [[ ${rule_count} -ne 1 ]]; then
    echo "ERRORE: in ${RAPL_SCRIPT} le righe di regola udev sono ${rule_count}, ne serve una." >&2
    exit 2
fi
RULE_LINE="$(grep -E '^SUBSYSTEM=="powercap"' "${RAPL_SCRIPT}")"

if [[ ! -f ${LICENSE_FILE} ]]; then
    echo "ERRORE: manca ${LICENSE_FILE}, ma lo spec dichiara una licenza." >&2
    exit 2
fi

if [[ ! -f ${METAINFO_FILE} ]]; then
    echo "ERRORE: manca ${METAINFO_FILE}." >&2
    exit 2
fi

if command -v appstreamcli >/dev/null; then
    if ! appstreamcli validate --no-net "${METAINFO_FILE}" >/dev/null 2>&1; then
        echo "ERRORE: ${METAINFO_FILE} non supera la convalida AppStream:" >&2
        appstreamcli validate --no-net "${METAINFO_FILE}" >&2 || true
        exit 2
    fi
else
    echo "NOTA: appstreamcli non installato, metadati non convalidati (sudo dnf install appstream)."
fi

rm -rf "${RPM_DIR}"
mkdir -p "${STAGE_DIR}/opt/ecomonitor" \
    "${STAGE_DIR}/usr/bin" \
    "${STAGE_DIR}/usr/share/applications" \
    "${STAGE_DIR}/usr/share/icons/hicolor/256x256/apps" \
    "${STAGE_DIR}/usr/lib/udev/rules.d" \
    "${STAGE_DIR}/usr/share/licenses/ecomonitor" \
    "${STAGE_DIR}/usr/share/metainfo" \
    "${TOP_DIR}/SPECS" "${TOP_DIR}/BUILD" "${TOP_DIR}/RPMS"

cp -r "${DIST_DIR}/EcoMonitor/." "${STAGE_DIR}/opt/ecomonitor/"
chmod +x "${STAGE_DIR}/opt/ecomonitor/EcoMonitor"
ln -sf /opt/ecomonitor/EcoMonitor "${STAGE_DIR}/usr/bin/ecomonitor"

"${PACKAGING_DIR}/read_study_endpoint.sh" >"${STAGE_DIR}/opt/ecomonitor/study_endpoint"
chmod 0644 "${STAGE_DIR}/opt/ecomonitor/study_endpoint"

sed "s|__EXEC__|/opt/ecomonitor/EcoMonitor|" "${PACKAGING_DIR}/ecomonitor.desktop" \
    >"${STAGE_DIR}/usr/share/applications/ecomonitor.desktop"
chmod 0644 "${STAGE_DIR}/usr/share/applications/ecomonitor.desktop"
cp "${PACKAGING_DIR}/ecomonitor.png" \
    "${STAGE_DIR}/usr/share/icons/hicolor/256x256/apps/ecomonitor.png"

cp "${LICENSE_FILE}" "${STAGE_DIR}/usr/share/licenses/ecomonitor/LICENSE"
chmod 0644 "${STAGE_DIR}/usr/share/licenses/ecomonitor/LICENSE"

cp "${METAINFO_FILE}" "${STAGE_DIR}/usr/share/metainfo/${METAINFO_ID}.metainfo.xml"
chmod 0644 "${STAGE_DIR}/usr/share/metainfo/${METAINFO_ID}.metainfo.xml"

{
    echo "# Installata dal pacchetto ecomonitor: rende leggibili i contatori RAPL"
    echo "# deve restare su una riga sola"
    echo "${RULE_LINE}"
} >"${STAGE_DIR}/usr/lib/udev/rules.d/99-ecomonitor-rapl.rules"
chmod 0644 "${STAGE_DIR}/usr/lib/udev/rules.d/99-ecomonitor-rapl.rules"

find "${STAGE_DIR}" -type d -exec chmod 0755 {} +
chmod 0644 "${STAGE_DIR}/usr/share/icons/hicolor/256x256/apps/ecomonitor.png"

icon_name="$(sed -n 's/^Icon=//p' "${STAGE_DIR}/usr/share/applications/ecomonitor.desktop")"
if [[ ! -f ${STAGE_DIR}/usr/share/icons/hicolor/256x256/apps/${icon_name}.png ]]; then
    echo "ERRORE: il file .desktop chiede l'icona '${icon_name}', che non e' nel pacchetto." >&2
    exit 2
fi
launchable="$(sed -n 's|.*<launchable type="desktop-id">\(.*\)</launchable>.*|\1|p' "${METAINFO_FILE}")"
if [[ ! -f ${STAGE_DIR}/usr/share/applications/${launchable} ]]; then
    echo "ERRORE: il metainfo rimanda a '${launchable}', che non e' nel pacchetto." >&2
    exit 2
fi

SPEC="${TOP_DIR}/SPECS/ecomonitor.spec"
sed -e "s|__VERSION__|${VERSION}|" -e "s|__ARCH__|${ARCH}|" -e "s|__STAGE_DIR__|${STAGE_DIR}|" \
    "${SPEC_TEMPLATE}" >"${SPEC}"

RPMBUILD_LOG="${RPM_DIR}/rpmbuild.log"
LC_ALL=C rpmbuild -bb --quiet --define "_topdir ${TOP_DIR}" "${SPEC}" 2>&1 \
    | tee "${RPMBUILD_LOG}"
if grep -q "Couldn't exec" "${RPMBUILD_LOG}"; then
    echo "ERRORE: rpmbuild non ha trovato i propri script di controllo:" >&2
    grep "Couldn't exec" "${RPMBUILD_LOG}" >&2
    echo "Il pacchetto sarebbe uscito senza quelle verifiche." >&2
    echo "Installare rpm-build (sudo dnf install rpm-build), oppure, se e' stato" >&2
    echo "estratto a mano, indicare dove sono i suoi script:" >&2
    echo "  RPM_CONFIGDIR=/percorso/con/check-files bash packaging/build_rpm.sh" >&2
    exit 2
fi

BUILT="${TOP_DIR}/RPMS/${ARCH}/ecomonitor-${VERSION}-1${DIST_TAG}.${ARCH}.rpm"
if [[ ! -f ${BUILT} ]]; then
    echo "ERRORE: rpmbuild non ha prodotto ${BUILT}." >&2
    exit 2
fi
mv "${BUILT}" "${RPM}"

rpm -qp --info "${RPM}" >/dev/null
contents="$(rpm -qpl "${RPM}")"
for required in /opt/ecomonitor/EcoMonitor /usr/bin/ecomonitor \
    /usr/share/applications/ecomonitor.desktop \
    /usr/share/licenses/ecomonitor/LICENSE \
    "/usr/share/metainfo/${METAINFO_ID}.metainfo.xml" \
    /usr/lib/udev/rules.d/99-ecomonitor-rapl.rules; do
    if ! grep -qxF "${required}" <<<"${contents}"; then
        echo "ERRORE: ${required} non e' finito nel pacchetto." >&2
        rm -f "${RPM}"
        exit 2
    fi
done

#Le librerie del bundle non devono comparire fra le dipendenze
if rpm -qp --provides "${RPM}" | grep -q 'libQt6'; then
    echo "ERRORE: il pacchetto dichiara di fornire le librerie Qt del bundle." >&2
    rm -f "${RPM}"
    exit 2
fi

#L'eseguibile deve restare quello di PyInstaller
original_size="$(stat -c %s "${DIST_DIR}/EcoMonitor/EcoMonitor")"
packed_size="$(rpm -qp --qf '[%{FILENAMES} %{FILESIZES}\n]' "${RPM}" |
    awk '$1 == "/opt/ecomonitor/EcoMonitor" { print $2 }')"
if [[ ${packed_size} != "${original_size}" ]]; then
    echo "ERRORE: l'eseguibile impacchettato misura ${packed_size} byte" >&2
    echo "        invece dei ${original_size} prodotti da PyInstaller." >&2
    rm -f "${RPM}"
    exit 2
fi

echo
echo "Pacchetto pronto: ${RPM} ($(du -h "${RPM}" | cut -f1))"
echo "Per installarlo:  doppio clic sul file, oppure"
echo "                  sudo dnf install ${RPM}"
echo "Per rimuoverlo:   sudo dnf remove ecomonitor"
