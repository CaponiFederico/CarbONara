import time
from dataclasses import dataclass
from pathlib import Path

from ecomonitor.sensors.base import (
    PowerReading,
    SensorUnavailableError,
    now_utc,
    sample_cpu_percent,
)
from ecomonitor.sensors.linux_gpu import (
    IntelArcGpuReader,
    read_amd_gpu_power_w,
    read_nvidia_gpu_power_w,
)

#RAPL espone contatori di energia in microjoule, non potenza istantanea: i Watt
#sono il delta fra due letture diviso il tempo. Il wraparound e' gestito qui.
#Servono permessi su /sys/class/powercap/intel-rapl*/energy_uj (root o udev)

RAPL_ROOT = Path("/sys/class/powercap")


@dataclass
class _RaplDomain:
    name: str  # es. "package-0", "uncore"
    energy_path: Path
    max_range_uj: int
    last_energy_uj: int | None = None
    last_timestamp: float | None = None

    def read_energy_uj(self) -> int:
        return int(self.energy_path.read_text().strip())

    def sample_watts(self) -> float | None:
        #Potenza media dall'ultima chiamata, None alla prima: serve un delta
        now = time.monotonic()
        energy = self.read_energy_uj()

        if self.last_energy_uj is None or self.last_timestamp is None:
            self.last_energy_uj, self.last_timestamp = energy, now
            return None

        delta_t = now - self.last_timestamp
        delta_energy = energy - self.last_energy_uj
        if delta_energy < 0:  # wraparound del contatore
            delta_energy += self.max_range_uj

        self.last_energy_uj, self.last_timestamp = energy, now
        if delta_t <= 0:
            return None
        return (delta_energy * 1e-6) / delta_t  # J / s = W


def _discover_domains() -> dict[str, _RaplDomain]:
    #La chiave e' la cartella sysfs (intel-rapl:0:1)
    domains: dict[str, _RaplDomain] = {}
    if not RAPL_ROOT.exists():
        return domains

    #rglob perche' i sottodomini stanno dentro la cartella del package
    #(intel-rapl:0/intel-rapl:0:1) 
    for entry in sorted(RAPL_ROOT.glob("intel-rapl:*")):
        for directory in (entry, *sorted(entry.glob("intel-rapl:*"))):
            name_file = directory / "name"
            energy_file = directory / "energy_uj"
            max_range_file = directory / "max_energy_range_uj"
            if not (name_file.exists() and energy_file.exists()):
                continue
            key = directory.name
            if key in domains:
                continue
            name = name_file.read_text().strip()
            max_range = (
                int(max_range_file.read_text().strip()) if max_range_file.exists() else 2**32
            )
            domains[key] = _RaplDomain(
                name=name, energy_path=energy_file, max_range_uj=max_range
            )

    return domains


class LinuxRaplBackend:
    def __init__(self) -> None:
        #_discover_domains() trova i file anche senza root 
        try:
            self._domains = _discover_domains()
            if not self._domains:
                raise SensorUnavailableError(
                    "Nessun dominio RAPL trovato in /sys/class/powercap — "
                    "hardware non supportato o permessi insufficienti"
                )
            #warm-up: fissa il campione base, cosi' la prima read() ha un delta
            for domain in self._domains.values():
                domain.sample_watts()
        except OSError as exc:
            raise SensorUnavailableError(
                "Permessi insufficienti per leggere /sys/class/powercap/*/energy_uj "
                "(serve root, o una regola udev)"
            ) from exc

        #Intel Arc dedicata: contatore cumulativo come RAPL, quindi il reader
        #va creato una volta sola qui e non a ogni read()
        self._intel_gpu_reader = IntelArcGpuReader()
        self._intel_gpu_reader.read_power_w()  # warm-up, stesso motivo dei domini

    def read(self) -> PowerReading:
        package_w = 0.0
        dram_w: float | None = None
        gpu_integrated_w: float | None = None

        for domain in self._domains.values():
            #il nome del dominio RAPL, non la chiave del dizionario: quella e'
            #la cartella sysfs, unica ma priva di significato
            name = domain.name
            watts = domain.sample_watts()
            if watts is None:
                continue
            if name.startswith("package"):
                package_w += watts
            elif name == "dram":
                #atteso assente su CPU client: il dominio DRAM di RAPL e'
                #popolato solo sulle piattaforme server
                dram_w = (dram_w or 0.0) + watts
            elif name == "uncore":
                #PP1, "graphics uncore" nel driver del kernel: su CPU client
                #Intel e' la GPU integrata
                gpu_integrated_w = (gpu_integrated_w or 0.0) + watts
            #"core" (PP0) e' un sotto-dominio gia' incluso in "package-N":

        #"uncore" (PP1) e' anch'esso DENTRO il package, esattamente come "core".
        #Va diviso e non sommato
        if gpu_integrated_w is not None:
            package_w = max(0.0, package_w - gpu_integrated_w)

        #la GPU dedicata non passa da RAPL in nessun caso
        nvidia_w = read_nvidia_gpu_power_w()
        amd_w = read_amd_gpu_power_w()
        intel_w = self._intel_gpu_reader.read_power_w()
        gpu_dedicated_w: float | None = None
        if nvidia_w is not None or amd_w is not None or intel_w is not None:
            gpu_dedicated_w = (nvidia_w or 0.0) + (amd_w or 0.0) + (intel_w or 0.0)

        return PowerReading(
            package_w=package_w,
            dram_w=dram_w,
            gpu_integrated_w=gpu_integrated_w,
            gpu_dedicated_w=gpu_dedicated_w,
            timestamp=now_utc(),
            is_estimated=False,
            #registrato accanto alla potenza misurata: e' l'ascissa su cui si
            #calibra il ripiego per quando questi sensori non saranno leggibili
            cpu_percent=sample_cpu_percent(),
        )
