#!/bin/bash
# Costruisce il pacchetto .deb per Debian/Ubuntu
#
# dalla root del progetto dopo packaging/build_linux.sh
#   bash packaging/build_deb.sh
#
# Produce packaging/out/carbonara_<versione>_<arch>.deb

set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${PACKAGING_DIR}")"

DIST_DIR="${PACKAGING_DIR}/dist"
OUT_DIR="${PACKAGING_DIR}/out"
STAGE_DIR="${PACKAGING_DIR}/build/deb"
RAPL_SCRIPT="${PROJECT_ROOT}/scripts/install_rapl_permissions.sh"
ORIGIN_FILE="${DIST_DIR}/CarbONara.origin"
LICENSE_FILE="${PROJECT_ROOT}/LICENSE"
METAINFO_ID="io.github.caponifederico.carbonara"
METAINFO_FILE="${PACKAGING_DIR}/${METAINFO_ID}.metainfo.xml"

source "${PACKAGING_DIR}/bundle_origin.sh"

VERSION="$(grep -m1 -E '^version = ' "${PROJECT_ROOT}/pyproject.toml" | cut -d'"' -f2)"
[[ -n ${VERSION} ]] || { echo "ERRORE: versione non leggibile da pyproject.toml" >&2; exit 2; }
ARCH="$(dpkg --print-architecture)"

require_bundle_origin "${ORIGIN_FILE}" debian \
    "su Debian/Ubuntu:  bash packaging/build_linux.sh && bash packaging/build_deb.sh" || exit 2

DEB="${OUT_DIR}/carbonara_${VERSION}_${ARCH}.deb"
mkdir -p "${OUT_DIR}"
rm -f "${DEB}"

#Il .deb impacchetta quello che ha prodotto PyInstaller
if [[ ! -x ${DIST_DIR}/CarbONara/CarbONara ]]; then
    echo "ERRORE: manca ${DIST_DIR}/CarbONara/CarbONara." >&2
    echo "Eseguire prima:  bash packaging/build_linux.sh" >&2
    exit 2
fi

if ! compgen -G "${DIST_DIR}/CarbONara/_internal/libxcb-cursor.so*" >/dev/null; then
    echo "ERRORE: libxcb-cursor.so.0 non e' nella cartella prodotta da PyInstaller." >&2
    echo "Senza, l'interfaccia grafica aborta su qualunque sessione X11 (su Wayland parte)." >&2
    echo "Installarla sulla macchina di build e ricostruire:" >&2
    echo "  sudo apt install libxcb-cursor0 && bash packaging/build_linux.sh" >&2
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
    echo "ERRORE: manca ${LICENSE_FILE}." >&2
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
    echo "NOTA: appstreamcli non installato, metadati non convalidati (sudo apt install appstream)."
fi

metainfo_version="$(grep -m1 -o '<release version="[^"]*"' "${METAINFO_FILE}" | cut -d'"' -f2)"
if [[ ${metainfo_version} != "${VERSION}" ]]; then
    echo "ERRORE: in ${METAINFO_FILE} la prima release e' '${metainfo_version}', il pacchetto e' ${VERSION}." >&2
    exit 2
fi

rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}/DEBIAN" \
    "${STAGE_DIR}/opt/carbonara" \
    "${STAGE_DIR}/usr/bin" \
    "${STAGE_DIR}/usr/share/applications" \
    "${STAGE_DIR}/usr/share/icons/hicolor/256x256/apps" \
    "${STAGE_DIR}/usr/lib/udev/rules.d" \
    "${STAGE_DIR}/usr/share/doc/carbonara" \
    "${STAGE_DIR}/usr/share/metainfo"

cp -r "${DIST_DIR}/CarbONara/." "${STAGE_DIR}/opt/carbonara/"
chmod +x "${STAGE_DIR}/opt/carbonara/CarbONara"
ln -sf /opt/carbonara/CarbONara "${STAGE_DIR}/usr/bin/carbonara"

bash "${PACKAGING_DIR}/read_study_endpoint.sh" >"${STAGE_DIR}/opt/carbonara/study_endpoint"
chmod 0644 "${STAGE_DIR}/opt/carbonara/study_endpoint"

sed "s|__EXEC__|/opt/carbonara/CarbONara|" "${PACKAGING_DIR}/carbonara.desktop" \
    >"${STAGE_DIR}/usr/share/applications/carbonara.desktop"
chmod 0644 "${STAGE_DIR}/usr/share/applications/carbonara.desktop"
cp "${PACKAGING_DIR}/carbonara.png" \
    "${STAGE_DIR}/usr/share/icons/hicolor/256x256/apps/carbonara.png"

cp "${LICENSE_FILE}" "${STAGE_DIR}/usr/share/doc/carbonara/copyright"
chmod 0644 "${STAGE_DIR}/usr/share/doc/carbonara/copyright"

cp "${METAINFO_FILE}" "${STAGE_DIR}/usr/share/metainfo/${METAINFO_ID}.metainfo.xml"
chmod 0644 "${STAGE_DIR}/usr/share/metainfo/${METAINFO_ID}.metainfo.xml"

{
    echo "# Installata dal pacchetto carbonara: rende leggibili i contatori RAPL"
    echo "# deve restare su una riga sola"
    echo "${RULE_LINE}"
} >"${STAGE_DIR}/usr/lib/udev/rules.d/99-carbonara-rapl.rules"
chmod 0644 "${STAGE_DIR}/usr/lib/udev/rules.d/99-carbonara-rapl.rules"

find "${STAGE_DIR}" -type d -exec chmod 0755 {} +
chmod 0644 "${STAGE_DIR}/usr/share/icons/hicolor/256x256/apps/carbonara.png"

icon_name="$(sed -n 's/^Icon=//p' "${STAGE_DIR}/usr/share/applications/carbonara.desktop")"
if [[ ! -f ${STAGE_DIR}/usr/share/icons/hicolor/256x256/apps/${icon_name}.png ]]; then
    echo "ERRORE: il file .desktop chiede l'icona '${icon_name}', che non e' nel pacchetto." >&2
    exit 2
fi
launchable="$(sed -n 's|.*<launchable type="desktop-id">\(.*\)</launchable>.*|\1|p' "${METAINFO_FILE}")"
if [[ ! -f ${STAGE_DIR}/usr/share/applications/${launchable} ]]; then
    echo "ERRORE: il metainfo rimanda a '${launchable}', che non e' nel pacchetto." >&2
    exit 2
fi

INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "${STAGE_DIR}" | cut -f1)"
sed -e "s|__VERSION__|${VERSION}|" -e "s|__ARCH__|${ARCH}|" -e "s|__SIZE__|${INSTALLED_SIZE}|" \
    "${PACKAGING_DIR}/debian/control" >"${STAGE_DIR}/DEBIAN/control"
cp "${PACKAGING_DIR}/debian/postinst" "${PACKAGING_DIR}/debian/prerm" \
    "${PACKAGING_DIR}/debian/postrm" "${STAGE_DIR}/DEBIAN/"
chmod 0755 "${STAGE_DIR}/DEBIAN/postinst" "${STAGE_DIR}/DEBIAN/prerm" \
    "${STAGE_DIR}/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "${STAGE_DIR}" "${DEB}" >/dev/null

dpkg-deb --info "${DEB}" >/dev/null
ctrl="$(dpkg-deb --ctrl-tarfile "${DEB}" | tar tf -)"
for script in postinst prerm postrm; do
    if ! grep -qx "./${script}" <<<"${ctrl}"; then
        echo "ERRORE: ${script} non e' finito nel pacchetto." >&2
        rm -f "${DEB}"
        exit 2
    fi
done
contents="$(dpkg-deb --contents "${DEB}")"
for required in ./opt/carbonara/CarbONara ./usr/bin/carbonara \
    ./usr/share/applications/carbonara.desktop \
    ./usr/share/doc/carbonara/copyright \
    "./usr/share/metainfo/${METAINFO_ID}.metainfo.xml" \
    ./usr/lib/udev/rules.d/99-carbonara-rapl.rules; do
    #il symlink non chiude la riga
    if ! grep -qE " ${required}( -> |$)" <<<"${contents}"; then
        echo "ERRORE: ${required} non e' finito nel pacchetto." >&2
        rm -f "${DEB}"
        exit 2
    fi
done

echo
echo "Pacchetto pronto: ${DEB} ($(du -h "${DEB}" | cut -f1))"
echo "Per installarlo:  doppio clic sul file, oppure"
echo "                  sudo apt install ${DEB}"
echo "Per rimuoverlo:   sudo apt remove carbonara"
