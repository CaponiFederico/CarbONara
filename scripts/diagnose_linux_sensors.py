# Elenca tutte le zone RAPL sotto /sys/class/powercap e la potenza istantanea
# di ciascuna. Serve a verificare quali sensori vengono davvero letti e quali
# riportano errori di lettura o permessi.
#
# package-N e' il dominio completo, comprende PP0 (core) e PP1 (uncore).
#
# nvidia-smi per la GPU dedicata NVIDIA [NON TESTATA, MANCA L'HW]
# hwmon per la GPU dedicata AMD
# hwmon (driver i915/xe) per Intel Arc/Flex: li' e' un contatore di energia
# (energy1_input), non potenza istantanea come AMD/NVIDIA, quindi serve un
# oggetto con stato che tenga il campione precedente (IntelArcGpuReader)
# [NON TESTATA, MANCA L'HW]
#
# permessi: sudo chmod -R a+r /sys/class/powercap/intel-rapl:*

import time

from ecomonitor.sensors.linux_gpu import (
    IntelArcGpuReader,
    read_amd_gpu_power_w,
    read_nvidia_gpu_power_w,
)
from ecomonitor.sensors.linux_rapl import RAPL_ROOT, _discover_domains

_KNOWN_ROLE = {
    "package": "-> package_w (dominio completo)",
    "core": "(sotto-dominio di package, non sommato per evitare doppio conteggio)",
    "uncore": "-> gpu_integrated_w su CPU client Intel (PP1 = graphics uncore)",
    "dram": "-> dram_w (atteso assente su CPU client)",
    "psys": "(sovrainsieme platform, ignorato: fuori scope CPU+GPU)",
}


def _role_hint(name: str) -> str:
    for prefix, hint in _KNOWN_ROLE.items():
        if name.startswith(prefix):
            return hint
    return "(nome non riconosciuto dal nostro codice, da investigare)"


def main() -> None:
    if not RAPL_ROOT.exists():
        print(f"{RAPL_ROOT} non esiste su questo sistema: RAPL non disponibile.")
        return

    domains = _discover_domains()
    if not domains:
        print(
            f"Nessuna zona RAPL trovata sotto {RAPL_ROOT} "
            "(hardware non supportato, o 'name'/'energy_uj' non leggibili)."
        )
        return

    print(f"Zone RAPL trovate: {', '.join(sorted(domains))}\n")

    # warm-up e attesa di 1s: RAPL richiede un intervallo reale fra due campioni
    # per una potenza media stabile, altrimenti il valore e' rumore (visto in
    # pratica: 78W e poi 0.0 di fila sulla stessa CPU a riposo)
    for domain in domains.values():
        try:
            domain.sample_watts()
        except OSError:
            pass
    time.sleep(1)

    for name, domain in sorted(domains.items()):
        try:
            watts = domain.sample_watts()
        except OSError as exc:
            print(f"  {name}: N/D ({exc.__class__.__name__}: {exc})  {_role_hint(name)}")
            continue
        value = f"{watts:.3f} W" if watts is not None else "N/D (nessun delta disponibile)"
        print(f"  {name}: {value}  {_role_hint(name)}")

    print("\nGPU dedicata (non passa da RAPL, letta a parte):")

    nvidia_w = read_nvidia_gpu_power_w()
    if nvidia_w is not None:
        print(f"  nvidia (nvidia-smi): {nvidia_w:.3f} W")
    else:
        print("  nvidia (nvidia-smi): non rilevata (nessun driver NVIDIA o comando assente)")

    amd_w = read_amd_gpu_power_w()
    if amd_w is not None:
        print(f"  amd (hwmon/amdgpu): {amd_w:.3f} W")
    else:
        print("  amd (hwmon/amdgpu): non rilevata (nessuna scheda amdgpu con sensore di potenza)")

    # anche qui energy1_input e' un contatore cumulativo: serve un intervallo
    # reale fra due campioni
    intel_reader = IntelArcGpuReader()
    intel_reader.read_power_w()  # warm-up
    time.sleep(1)
    intel_w = intel_reader.read_power_w()
    if intel_w is not None:
        print(f"  intel (hwmon/i915-xe): {intel_w:.3f} W")
    else:
        print(
            "  intel (hwmon/i915-xe): non rilevata (nessuna Arc/Flex trovata, "
            "o scambiata per GPU integrata dall'euristica PCI)"
        )


if __name__ == "__main__":
    main()
