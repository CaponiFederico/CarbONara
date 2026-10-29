#!/bin/bash
# Rende leggibili i contatori di energia RAPL a un utente non privilegiato
# USO
#   sudo bash scripts/install_rapl_permissions.sh
#   sudo bash scripts/install_rapl_permissions.sh --uninstall

set -euo pipefail

RULE_PATH=/etc/udev/rules.d/99-carbonara-rapl.rules
OLD_RULE=/etc/udev/rules.d/99-rapl.rules

if [[ ${EUID} -ne 0 ]]; then
    echo "Serve root: sudo bash $0 $*" >&2
    exit 1
fi

if [[ ${1:-} == "--uninstall" ]]; then
    rm -f "${RULE_PATH}"
    udevadm control --reload-rules || true
    #ripristina il valore predefinito del kernel
    chmod 0400 /sys/class/powercap/intel-rapl:*/energy_uj 2>/dev/null || true
    echo "Regola rimossa e permessi riportati a 0400. Riavviare per essere certi."
    exit 0
fi

if [[ ! -d /sys/class/powercap ]]; then
    echo "ERRORE: /sys/class/powercap non esiste." >&2
    echo "Questa CPU non espone RAPL (AMD pre-Zen, ARM, macchina virtuale)." >&2
    exit 2
fi

# i permessi vengono dati subito senza riavvio
applied=0
for energy_file in /sys/class/powercap/intel-rapl:*/energy_uj; do
    [[ -e ${energy_file} ]] || continue
    chmod a+r "${energy_file}"
    applied=$((applied + 1))
done

if [[ ${applied} -eq 0 ]]; then
    echo "ERRORE: nessun file energy_uj trovato sotto /sys/class/powercap." >&2
    exit 2
fi
echo "Permessi applicati a ${applied} domini RAPL nella sessione corrente."

# cambio la regola udev per mantenere le modifiche tra i riavvi
cat >"${RULE_PATH}" <<'RULE'
# Installata da carbonara/scripts/install_rapl_permissions.sh
SUBSYSTEM=="powercap", ACTION=="add", KERNEL=="intel-rapl:*", RUN+="/bin/chmod a+r /sys%p/energy_uj"
RULE

if command -v udevadm >/dev/null; then
    if udevadm verify "${RULE_PATH}"; then
        echo "Regola udev installata e verificata: ${RULE_PATH}"
    else
        echo "ATTENZIONE: udevadm verify ha segnalato problemi sulla regola." >&2
    fi
    udevadm control --reload-rules || true
fi

if [[ -f ${OLD_RULE} ]]; then
    echo
    echo "NOTA: esiste ancora ${OLD_RULE}"
fi

# verifica
echo
echo "Verifica: lettura di un contatore come utente non privilegiato"
target=$(ls /sys/class/powercap/intel-rapl:*/energy_uj 2>/dev/null | head -1)

target_user="${SUDO_USER:-nobody}"
if runuser -u "${target_user}" -- cat "${target}" >/dev/null 2>&1; then
    echo "OK: ${target_user} riesce a leggere ${target}"
    echo
    echo "Ora, SENZA sudo:  python -m ecomonitor.diagnostics env"
    echo "Deve dire 'backend sensori: LinuxRaplBackend, lettura hardware reale'."
else
    echo "FALLITO: il file resta illeggibile. Controllare AppArmor/SELinux o un lockdown del kernel." >&2
    exit 3
fi
