#!/bin/bash
# Costruisce il pacchetto Linux
#
# dalla root del progetto
#   bash packaging/build_linux.sh
#
# Serve un venv con l'extra [packaging]:
#   python -m venv .venv && .venv/bin/pip install -e ".[gui,packaging]"
#
# Produce packaging/out/ecomonitor-<versione>-linux-<arch>.tar.gz, che contiene
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

"${PYTHON}" "${PACKAGING_DIR}/make_icon.py"
"${PYTHON}" -m PyInstaller --clean --noconfirm \
    --distpath "${DIST_DIR}" --workpath "${BUILD_DIR}" \
    "${PACKAGING_DIR}/ecomonitor.spec"

rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}" "${OUT_DIR}"
cp -r "${DIST_DIR}/EcoMonitor" "${STAGE_DIR}/EcoMonitor"
cp "${PACKAGING_DIR}/linux_install.sh" "${STAGE_DIR}/install.sh"
cp "${PACKAGING_DIR}/ecomonitor.desktop" "${PACKAGING_DIR}/ecomonitor.png" "${STAGE_DIR}/"
#le regole udev per RAPL servono all'utente dopo l'installazione
mkdir -p "${STAGE_DIR}/scripts"
cp "${PROJECT_ROOT}/scripts/install_rapl_permissions.sh" "${STAGE_DIR}/scripts/"
echo -n "${VERSION}" >"${STAGE_DIR}/version.txt"
chmod +x "${STAGE_DIR}/install.sh" "${STAGE_DIR}/EcoMonitor/EcoMonitor"

"${DIST_DIR}/ecomonitor-cli" --help >/dev/null

TARBALL="${OUT_DIR}/ecomonitor-${VERSION}-linux-${ARCH}.tar.gz"
rm -f "${TARBALL}"
tar -czf "${TARBALL}" -C "${STAGE_DIR}" .

echo
echo "Pacchetto pronto: ${TARBALL} ($(du -h "${TARBALL}" | cut -f1))"
echo "Per installarlo:  tar -xzf $(basename "${TARBALL}") -C /tmp/ecomonitor && bash /tmp/ecomonitor/install.sh"
