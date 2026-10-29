#!/bin/bash
# Costruisce il pacchetto Linux
#
# dalla root del progetto
#   bash packaging/build_linux.sh
#
# Serve un venv con l'extra [packaging]:
#   python -m venv .venv && .venv/bin/pip install -e ".[gui,packaging]"
#
# Produce packaging/out/carbonara-<versione>-linux-<arch>.tar.gz, che contiene
# l'applicazione e install.sh


set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${PACKAGING_DIR}")"

PYTHON="${PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
if [[ ! -x ${PYTHON} ]]; then
    echo "ERRORE: nessun interprete in ${PYTHON}. Impostare PYTHON=/percorso/python." >&2
    exit 2
fi

VERSION="$(grep -m1 -E '^version = ' "${PROJECT_ROOT}/pyproject.toml" | cut -d'"' -f2)"
[[ -n ${VERSION} ]] || { echo "ERRORE: versione non leggibile da pyproject.toml" >&2; exit 2; }
ARCH="$(uname -m)"

BUILD_DIR="${PACKAGING_DIR}/build"
DIST_DIR="${PACKAGING_DIR}/dist"
OUT_DIR="${PACKAGING_DIR}/out"
STAGE_DIR="${BUILD_DIR}/stage"
ORIGIN_FILE="${DIST_DIR}/CarbONara.origin"

#dichiara chi ha costruito il bundle, cosi' build_deb.sh e build_rpm.sh non
#impacchettano quello di un'altra distribuzione
source "${PACKAGING_DIR}/bundle_origin.sh"

#Il pacchetto della build precedente non deve sopravvivere a una build fallita
TARBALL="${OUT_DIR}/carbonara-${VERSION}-linux-${ARCH}.tar.gz"
rm -f "${TARBALL}"

#se PyInstaller fallisce non deve restare la dichiarazione della build precedente
#accanto al bundle vecchio
rm -f "${ORIGIN_FILE}"

"${PYTHON}" "${PACKAGING_DIR}/make_icon.py"
"${PYTHON}" -m PyInstaller --clean --noconfirm \
    --distpath "${DIST_DIR}" --workpath "${BUILD_DIR}" \
    "${PACKAGING_DIR}/ecomonitor.spec"

#Il plugin Qt "xcb" dipende da libxcb-cursor.so.0, che non e' una dipendenza pip di
#PySide6: se manca dalla macchina di build PyInstaller non la incapsula
if ! compgen -G "${DIST_DIR}/CarbONara/_internal/libxcb-cursor.so*" >/dev/null; then
    echo "ERRORE: libxcb-cursor.so.0 non e' finita nel pacchetto." >&2
    echo "Senza, l'interfaccia grafica aborta su qualunque sessione X11" >&2
    echo "Installarla sulla macchina di build e ricostruire:" >&2
    echo "  sudo apt install libxcb-cursor0" >&2
    exit 2
fi

write_bundle_origin "${ORIGIN_FILE}" "${PYTHON}"

rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}" "${OUT_DIR}"
cp -r "${DIST_DIR}/CarbONara" "${STAGE_DIR}/CarbONara"
cp "${PACKAGING_DIR}/linux_install.sh" "${STAGE_DIR}/install.sh"
bash "${PACKAGING_DIR}/read_study_endpoint.sh" >"${STAGE_DIR}/CarbONara/study_endpoint"
chmod 0644 "${STAGE_DIR}/CarbONara/study_endpoint"
cp "${PACKAGING_DIR}/carbonara.desktop" "${PACKAGING_DIR}/carbonara.png" "${STAGE_DIR}/"
#le regole udev per RAPL servono all'utente dopo l'installazione
mkdir -p "${STAGE_DIR}/scripts"
cp "${PROJECT_ROOT}/scripts/install_rapl_permissions.sh" "${STAGE_DIR}/scripts/"
echo -n "${VERSION}" >"${STAGE_DIR}/version.txt"
chmod +x "${STAGE_DIR}/install.sh" "${STAGE_DIR}/CarbONara/CarbONara"

"${DIST_DIR}/ecomonitor-cli" --help >/dev/null

tar -czf "${TARBALL}" -C "${STAGE_DIR}" .

echo
echo "Pacchetto pronto: ${TARBALL} ($(du -h "${TARBALL}" | cut -f1))"
echo "Per installarlo:  mkdir -p /tmp/carbonara && tar -xzf $(basename "${TARBALL}") -C /tmp/carbonara && bash /tmp/carbonara/install.sh"
