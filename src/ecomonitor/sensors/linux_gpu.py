import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#GPU dedicata su Linux: RAPL non la copre (non e' un dominio del die della
#CPU). NVIDIA via nvidia-smi, AMD via hwmon (potenza istantanea), Intel
#Arc/Flex via hwmon ma con un contatore di energia, quindi serve un delta
#(IntelArcGpuReader). Non verificato su hardware Arc reale

DRM_ROOT = Path("/sys/class/drm")
_CARD_DIR_PATTERN = re.compile(r"^card\d+$")
_PCI_BDF_PATTERN = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$")


def read_nvidia_gpu_power_w() -> float | None:
    #Somma la potenza di tutte le GPU NVIDIA. None se il comando non esiste
    #(nessun driver) o fallisce
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    try:
        values = [float(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    except ValueError:
        return None

    return sum(values) if values else None


def read_amd_gpu_power_w() -> float | None:
    #Somma la potenza delle sole schede amdgpu dedicate via hwmon
    if not DRM_ROOT.exists():
        return None

    total_w = 0.0
    found = False

    for card_dir in sorted(DRM_ROOT.iterdir()):
        if not _CARD_DIR_PATTERN.match(card_dir.name):
            continue  # scarta i connettori, es. "card0-DP-1"

        device_dir = card_dir / "device"
        if not _is_discrete_pci_device(device_dir):
            continue  # probabile integrata, gia' dentro RAPL "package"

        hwmon_root = device_dir / "hwmon"
        if not hwmon_root.exists():
            continue

        for hwmon_dir in sorted(hwmon_root.glob("hwmon*")):
            name_file = hwmon_dir / "name"
            try:
                driver_name = name_file.read_text().strip()
            except OSError:
                continue
            if driver_name != "amdgpu":
                continue

            #power1_average o power1_input a seconda di kernel e ASIC
            for power_file_name in ("power1_average", "power1_input"):
                power_file = hwmon_dir / power_file_name
                if not power_file.exists():
                    continue
                try:
                    microwatts = int(power_file.read_text().strip())
                except (OSError, ValueError):
                    continue
                total_w += microwatts / 1e6
                found = True
                break  # non sommare average e input dello stesso hwmon

    return total_w if found else None


def _count_pci_bdf_segments(resolved_path_str: str) -> int:
    #Quanti segmenti del path assomigliano a un indirizzo PCI BDF
    #(dominio:bus:device.funzione). PurePosixPath perche' e' sola manipolazione
    #di stringhe: resta verificabile anche su Windows, dove un percorso con ':'
    #nel nome non sarebbe creabile
    return sum(1 for part in PurePosixPath(resolved_path_str).parts if _PCI_BDF_PATTERN.match(part))


def _is_discrete_pci_device(device_dir: Path) -> bool:
    #Euristica: una GPU raggiungibile con un solo hop dal bus root e' quasi
    #sempre integrata nel package della CPU, una dietro un bridge PCIe
    #aggiuntivo e' quasi sempre una scheda dedicata. Il nome del driver da solo
    #non basta: integrata e Arc dedicata usano entrambe i915/xe
    try:
        resolved = device_dir.resolve()
    except OSError:
        return False
    return _count_pci_bdf_segments(str(resolved)) > 1


@dataclass
class _EnergyCounterState:
    last_energy_uj: int | None = None
    last_timestamp: float | None = None


class IntelArcGpuReader:
    #come rapl

    def __init__(self) -> None:
        self._energy_paths: list[Path] = []
        self._state: dict[Path, _EnergyCounterState] = {}
        self._discover()

    def _discover(self) -> None:
        if not DRM_ROOT.exists():
            return

        for card_dir in sorted(DRM_ROOT.iterdir()):
            if not _CARD_DIR_PATTERN.match(card_dir.name):
                continue

            device_dir = card_dir / "device"
            hwmon_root = device_dir / "hwmon"
            if not hwmon_root.exists():
                continue

            if not _is_discrete_pci_device(device_dir):
                continue  # probabile integrata, gia' coperta da RAPL "uncore"

            for hwmon_dir in sorted(hwmon_root.glob("hwmon*")):
                name_file = hwmon_dir / "name"
                try:
                    driver_name = name_file.read_text().strip()
                except OSError:
                    continue
                if driver_name not in ("i915", "xe"):
                    continue

                energy_file = hwmon_dir / "energy1_input"
                if not energy_file.exists():
                    continue

                label_file = hwmon_dir / "energy1_label"
                if label_file.exists():
                    try:
                        label = label_file.read_text().strip()
                    except OSError:
                        label = ""
                    if label and label != "card":
                        continue  # es. "pkg": dominio piu' ampio della sola GPU

                self._energy_paths.append(energy_file)

    def read_power_w(self) -> float | None:
        if not self._energy_paths:
            return None

        now = time.monotonic()
        total_w = 0.0
        got_any = False

        for path in self._energy_paths:
            try:
                energy_uj = int(path.read_text().strip())
            except (OSError, ValueError):
                continue

            state = self._state.setdefault(path, _EnergyCounterState())
            if state.last_energy_uj is not None and state.last_timestamp is not None:
                delta_t = now - state.last_timestamp
                delta_energy = energy_uj - state.last_energy_uj
                if delta_energy < 0:
                    #wraparound: hwmon non documenta un file "max" come RAPL, si
                    #assume lo stesso limite usato altrove
                    delta_energy += 2**32
                if delta_t > 0:
                    total_w += (delta_energy * 1e-6) / delta_t
                    got_any = True

            state.last_energy_uj, state.last_timestamp = energy_uj, now

        return total_w if got_any else None
