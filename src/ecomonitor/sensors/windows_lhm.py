from collections.abc import Iterable
from dataclasses import dataclass

from ecomonitor.sensors.base import PowerReading, SensorUnavailableError, now_utc

#Backend Windows via il pacchetto HardwareMonitor (PyPI, BSD-3), che include
#gia' LibreHardwareMonitorLib.dll e il wrapper pythonnet: basta l'extra
#"windows" del progetto, nessuna DLL da vendorizzare a mano. Richiede .NET 4.7+

#Vincoli verificati su macchine reali:
#- senza privilegi admin Computer.Update() NON solleva eccezioni: il sensore
#  "CPU Package" esiste ma il suo Value resta 0.0. Il fallimento e' silenzioso,
#  quindi 0.0 sul Package viene trattato come "non valido" e non come CPU a
#  consumo nullo (fisicamente implausibile)
#- i sensori GPU (NVAPI/ADL, non MSR) funzionano anche senza admin
#- su un desktop AMD con APU + Radeon dedicata compaiono due hardware GpuAmd
#  distinti, entrambi con potenza valida: e' il motivo per cui integrata e
#  dedicata sono due campi separati in PowerReading. Piu' GPU dedicate vengono
#  invece sommate in un unico gpu_dedicated_w
#- dram_w resta None su qualunque CPU client: su AMD non esiste un dominio
#  DRAM, su Intel il sensore "CPU Memory" esiste (MSR 0x619) ma riporta
#  costantemente 0.0 perche' il RAPL DRAM PowerPlane e' popolato solo sulle
#  piattaforme server. La via PMIC/SPD5 usata da HWiNFO non e' implementata da
#  LibreHardwareMonitor per nessun vendor (verificato nel suo codice sorgente)

#Pattern di nome osservati per le GPU integrate (case-insensitive): la libreria
#non distingue integrata e dedicata a livello di HardwareType, un'APU AMD e una
#Radeon dedicata sono entrambe GpuAmd. Euristica, non una regola garantita
_INTEGRATED_GPU_NAME_HINTS = (
    "radeon(tm) graphics",  # APU AMD generiche
    "vega graphics",
    "uhd graphics",
    "iris",
)


def _is_integrated_gpu_name(hardware_name: str) -> bool:
    name = hardware_name.lower()
    return any(hint in name for hint in _INTEGRATED_GPU_NAME_HINTS)


@dataclass
class _ClassifiedPower:
    package_w: float
    dram_w: float | None
    gpu_integrated_w: float | None
    gpu_dedicated_w: float | None
    found_package: bool


def _classify_hardware(hardware_iterable: Iterable, hardware_type, sensor_type) -> _ClassifiedPower:
    #Logica pura di classificazione, separata da read() per essere verificabile
    #con oggetti finti, senza il pacchetto HardwareMonitor ne' hardware reale.
    #Presuppone che hardware.Update() sia gia' stato chiamato
    package_w = 0.0
    dram_w: float | None = None
    gpu_integrated_w: float | None = None
    gpu_dedicated_w: float | None = None
    found_package = False

    for hardware in hardware_iterable:
        if hardware.HardwareType == hardware_type.Cpu:
            for sensor in hardware.Sensors:
                if sensor.SensorType != sensor_type.Power or not sensor.Value:
                    continue  # not sensor.Value scarta sia None che 0.0
                if "Package" in sensor.Name:
                    #il Package e' la somma di tutti i core: e' l'aggregato che
                    #corrisponde al dominio "package" di RAPL. I singoli
                    #"CPU Core #N" sarebbero un doppio conteggio
                    package_w += float(sensor.Value)
                    found_package = True
                elif "Memory" in sensor.Name:
                    #su Intel il dominio DRAM e' esposto come sensore della CPU,
                    #non dell'hardware Memory. Assente su AMD
                    dram_w = (dram_w or 0.0) + float(sensor.Value)

        elif hardware.HardwareType in (
            hardware_type.GpuNvidia,
            hardware_type.GpuAmd,
            hardware_type.GpuIntel,
        ):
            gpu_power = sum(
                float(sensor.Value)
                for sensor in hardware.Sensors
                if sensor.SensorType == sensor_type.Power and sensor.Value is not None
            )
            if gpu_power == 0.0:
                continue

            #NVIDIA non produce grafica integrata in CPU: e' sempre dedicata
            is_integrated = hardware.HardwareType != hardware_type.GpuNvidia and (
                _is_integrated_gpu_name(hardware.Name)
            )
            if is_integrated:
                gpu_integrated_w = (gpu_integrated_w or 0.0) + gpu_power
            else:
                gpu_dedicated_w = (gpu_dedicated_w or 0.0) + gpu_power

        elif hardware.HardwareType == hardware_type.Memory:
            for sensor in hardware.Sensors:
                if sensor.SensorType == sensor_type.Power and sensor.Value is not None:
                    dram_w = (dram_w or 0.0) + float(sensor.Value)

    return _ClassifiedPower(package_w, dram_w, gpu_integrated_w, gpu_dedicated_w, found_package)


class WindowsLhmBackend:
    def __init__(self) -> None:
        try:
            from HardwareMonitor.Hardware import HardwareType, SensorType
            from HardwareMonitor.Util import OpenComputer
        except ImportError as exc:
            raise SensorUnavailableError(
                "Pacchetto HardwareMonitor non installato — "
                "eseguire: pip install -e \".[windows]\""
            ) from exc

        self._HardwareType = HardwareType
        self._SensorType = SensorType

        try:
            self._computer = OpenComputer(cpu=True, gpu=True, memory=True)
        except Exception as exc:  # pythonnet propaga eccezioni .NET generiche
            raise SensorUnavailableError(
                "Impossibile aprire LibreHardwareMonitor — servono privilegi di "
                "amministratore (riavviare il processo come admin)"
            ) from exc

        if not self._has_cpu_package_sensor():
            self._computer.Close()
            raise SensorUnavailableError(
                "Nessun sensore 'CPU Package' trovato — CPU non supportata o "
                "privilegi insufficienti per leggere gli MSR"
            )

    def _has_cpu_package_sensor(self) -> bool:
        #True solo se il Package esiste E riporta un valore non nullo: un 0.0
        #indica quasi sempre permessi insufficienti, non consumo reale
        try:
            for hardware in self._computer.Hardware:
                if hardware.HardwareType == self._HardwareType.Cpu:
                    hardware.Update()
                    for sensor in hardware.Sensors:
                        if (
                            sensor.SensorType == self._SensorType.Power
                            and "Package" in sensor.Name
                            and sensor.Value
                        ):
                            return True
            return False
        except Exception:
            return False

    def is_available(self) -> bool:
        try:
            for hardware in self._computer.Hardware:
                hardware.Update()
            return True
        except Exception:
            return False

    def read(self) -> PowerReading:
        for hardware in self._computer.Hardware:
            hardware.Update()

        result = _classify_hardware(self._computer.Hardware, self._HardwareType, self._SensorType)

        if not result.found_package:
            raise SensorUnavailableError(
                "Il sensore 'CPU Package' non ha restituito un valore in questo tick "
                "(hardware non supportato o permessi persi a runtime)"
            )

        return PowerReading(
            package_w=result.package_w,
            dram_w=result.dram_w,
            gpu_integrated_w=result.gpu_integrated_w,
            gpu_dedicated_w=result.gpu_dedicated_w,
            timestamp=now_utc(),
            is_estimated=False,
        )

    def close(self) -> None:
        self._computer.Close()
